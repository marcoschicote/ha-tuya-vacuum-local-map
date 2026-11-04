"""A soak test: many runs, many dropped connections, and nothing may grow.

It runs the real connection thread and the real tinytuya against the fake vacuum,
which streams runs faster than a real one (SOAK_RATE messages a second, 4 for a real
vacuum), drops the connection now and then, and lets each run go quiet so it is
counted. A sampler watches memory, threads, tasks and how often things change.

By default it runs for 15 s so it can stay in CI. For a long one:

    SOAK_SECONDS=600 SOAK_RATE=40 pytest tests/ha/test_soak.py -s --timeout=0

The numbers it prints are counts and sizes only.
"""

import asyncio
import logging
import os
import random
import time

import pytest
import tinytuya

from custom_components.tuya_vacuum_local_map import connection
from custom_components.tuya_vacuum_local_map import coordinator as coordinator_module
from custom_components.tuya_vacuum_local_map.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_VERSION,
    DOMAIN,
)

from ..fake_vacuum import (
    DEVICE_ID,
    KEY,
    FakeVacuum,
    command_dps,
    rooms_dps,
    run_messages,
    serpentine,
)
from .common import ENTRY_ID

SECONDS = float(os.environ.get("SOAK_SECONDS", "15"))
RATE = float(os.environ.get("SOAK_RATE", "40"))
IDLE_FOR_TEST = 3.0  # seconds without a point before a run counts as finished
DROP_EVERY = 6.0  # seconds between dropped connections


def rss_mb() -> float:
    with open("/proc/self/statm") as statm:
        return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6


@pytest.fixture
def vacuum(socket_enabled, monkeypatch):
    with FakeVacuum() as fake:

        def device(device_id, host, key, version, persist):
            return tinytuya.Device(
                device_id, host, key, version=version, persist=persist, port=fake.port
            )

        monkeypatch.setattr(connection, "_tinytuya_device", device)
        yield fake


async def test_soak(hass, vacuum, monkeypatch, caplog, hass_storage):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(coordinator_module, "IDLE_SECONDS", IDLE_FOR_TEST)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        unique_id=DEVICE_ID,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_LOCAL_KEY: KEY,
            CONF_VERSION: "3.3",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    for _ in range(100):
        if coordinator.connected:
            break
        await asyncio.sleep(0.05)
    assert coordinator.connected

    changes = []
    hass.bus.async_listen(
        "state_changed", lambda event: changes.append(time.monotonic())
    )
    rng = random.Random(7)
    stop_at = time.monotonic() + SECONDS
    state = {"runs": 0, "messages": 0, "drops": 0}

    def drive():
        """What a vacuum does, in a thread: run after run, with dropped connections."""
        next_drop = time.monotonic() + DROP_EVERY
        path_id = 1_700_000_000
        while time.monotonic() < stop_at:
            path_id += 1000
            room = rng.randint(1, 4)
            vacuum.push(rooms_dps(rooms=[(i, f"Room {i}") for i in range(1, 5)]))
            vacuum.push(command_dps([room]))
            width = rng.choice([3000, 4000, 6000])
            for message in run_messages(path_id, serpentine(width, 3000)):
                if time.monotonic() >= stop_at:
                    break
                vacuum.push(message)
                state["messages"] += 1
                time.sleep(1 / RATE)
                if time.monotonic() >= next_drop:
                    vacuum.drop_clients()
                    state["drops"] += 1
                    next_drop = time.monotonic() + DROP_EVERY
                    time.sleep(0.5)  # the client takes a moment to come back
            state["runs"] += 1
            # the vacuum goes quiet: the run is counted once the idle time passes
            time.sleep(IDLE_FOR_TEST + 1.5)

    samples = []

    async def sample():
        while True:
            samples.append(
                {
                    "t": time.monotonic(),
                    "rss": rss_mb(),
                    "threads": len(__import__("threading").enumerate()),
                    "tasks": len(asyncio.all_tasks()),
                    "cells": coordinator.grid.covered_cells(),
                }
            )
            await asyncio.sleep(1)

    sampler = hass.async_create_background_task(sample(), "soak sampler")
    started = time.monotonic()
    await hass.async_add_executor_job(drive)
    await asyncio.sleep(IDLE_FOR_TEST + 2)  # let the last run be counted
    await hass.async_block_till_done(wait_background_tasks=False)
    sampler.cancel()
    elapsed = time.monotonic() - started

    # --- what it did
    first_quarter = samples[len(samples) // 4]
    last = samples[-1]
    seconds = max(elapsed, 1)
    print(
        f"\nsoak: {elapsed:.0f} s, {state['runs']} runs, {state['messages']} messages, "
        f"{state['drops']} dropped connections, {vacuum.connections} connections"
    )
    print(
        f"  memory {first_quarter['rss']:.0f} -> {last['rss']:.0f} MB, "
        f"threads {samples[0]['threads']} -> {last['threads']}, "
        f"tasks {samples[0]['tasks']} -> {last['tasks']}"
    )
    print(
        f"  renders {coordinator.render_count}, saves {coordinator.save_count}, "
        f"entity updates {coordinator.notify_count}, state changes {len(changes)}, "
        f"covered cells {coordinator.grid.covered_cells()}"
    )

    # --- what must hold
    assert state["runs"] >= 1 and state["messages"] > 50
    assert state["drops"] >= (1 if SECONDS >= 10 else 0)
    assert vacuum.only_reads, (
        "the integration sent something besides a status or a heartbeat"
    )
    assert coordinator.connected or vacuum.connections > state["drops"]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors][:3]
    # one warning per outage, not one per attempt
    outages = [r for r in caplog.records if "Connection lost" in r.getMessage()]
    assert len(outages) <= state["drops"] + 2
    # nothing piles up
    assert last["threads"] <= samples[0]["threads"] + 2
    assert last["tasks"] <= samples[0]["tasks"] + 5
    assert last["rss"] - first_quarter["rss"] < 40, "memory is growing"
    # the rate limits hold, however fast the vacuum talks
    assert coordinator.notify_count <= seconds * 1.2 + 10
    assert coordinator.render_count <= state["runs"] * 3 + seconds / 30 + 5
    assert coordinator.save_count <= state["runs"] * 2 + seconds / 30 + 5
    assert len(changes) <= 7 * (seconds * 1.2 + 10)  # seven entities, once a second
    # the map is bounded, and it did get built
    assert 0 < coordinator.grid.covered_cells() < 200_000
    assert len(coordinator.tracker._runs) <= 3
    assert len(coordinator.tracker.finished_ids) <= 50
    assert len(coordinator._rooms_by_run) <= 10

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert vacuum.only_reads
