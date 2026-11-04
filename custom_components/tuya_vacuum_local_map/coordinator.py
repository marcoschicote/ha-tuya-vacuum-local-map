"""Feeds the vacuum's frames into core, and owns the map, its storage and its timers.

Rates (PLAN, phase 4), since the vacuum sends about four messages a second:
entities update at most once a second, the image is redrawn at most every 30 s during
a run and once when the run ends, and the map is saved 30 s after a change and
straight away when a run ends or the entry unloads.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import connection
from .const import (
    CONF_BRUSH,
    CONF_DEVICE_ID,
    CONF_HIDDEN_ROOMS,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_UNITS_PER_METRE,
    CONF_VERSION,
    DEFAULT_BRUSH,
    DEFAULT_UNITS_PER_METRE,
    DOMAIN,
    ISSUE_STALLED,
    STORAGE_VERSION,
    storage_key,
)
from .core.grid import CoverageGrid
from .core.protocol import (
    Room,
    decode_dp,
    parse_path,
    parse_room_command,
    parse_rooms,
    parse_status,
)
from .core.render import RenderInput, render_svg
from .core.rooms import RoomLayer
from .core.runs import IDLE_SECONDS, RunTracker

_LOGGER = logging.getLogger(__name__)

type TuyaVacuumConfigEntry = ConfigEntry[Coordinator]

SAVE_DELAY = 30
RENDER_INTERVAL = 30.0
NOTIFY_INTERVAL = 1.0
TICK = timedelta(seconds=1)

# Types we route or know about. Others are counted, and their field names kept (never
# their values) so diagnostics can describe a new model's messages.
KNOWN_TYPES = frozenset({20001, 21003, 21004, 21005, 21011, 21023, 30000})
_MAX_UNKNOWN_TYPES = 20
_MAX_FIELD_NAMES = 30
COMMAND_WINDOW = 120.0  # seconds a room command waits for its run's first point
KEEP_CALIBRATION = 5

_monotonic = time.monotonic  # replaced in tests

Point = tuple[int, int]


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Coordinator:
    def __init__(self, hass: HomeAssistant, entry: TuyaVacuumConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.signal = f"{DOMAIN}_update_{entry.entry_id}"
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Tuya vacuum"
        )
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        options = entry.options
        self.brush = int(options.get(CONF_BRUSH, DEFAULT_BRUSH))
        self.units_per_metre = int(
            options.get(CONF_UNITS_PER_METRE, DEFAULT_UNITS_PER_METRE)
        )
        self.hidden_rooms = frozenset(
            int(room) for room in options.get(CONF_HIDDEN_ROOMS, [])
        )

        self.connected = False
        self.rooms: dict[int, str] = {}  # merged per room ID, never replaced
        self._pending: tuple[list[int], float] | None = None  # a command, not yet a run
        self._rooms_by_run: dict[int, list[int] | None] = {}
        self._attributed: dict[int, frozenset[Any]] = {}
        self.layer = RoomLayer()  # which cells belong to which room
        self.reported_area: float | None = (
            None  # from a 20001 status, if the model has one
        )
        self.calibration: list[dict[str, Any]] = []
        self.frame_counts: Counter[int] = Counter()
        self.unknown_types: dict[int, list[str]] = {}
        self.largest_frame = 0
        self.render_count = 0
        self.notify_count = 0
        self.save_count = 0

        self.grid = CoverageGrid(brush=self.brush)
        self.tracker = RunTracker(self.grid, idle_seconds=IDLE_SECONDS)
        self._map_hash: str | None = None
        self._previous_grid: bytes | None = None  # one generation back, by hand
        self._stored_last_run: list[list[Point]] = []
        self._stored_points = 0
        self._stored_path_id: int | None = None

        self.svg: bytes | None = None
        self.image_updated: datetime | None = None
        self._last_render = float("-inf")
        self._rendering = False
        self._render_again = False
        self._render_timer: CALLBACK_TYPE | None = None
        self._last_notify = float("-inf")
        self._notify_timer: CALLBACK_TYPE | None = None
        self._save_timer: CALLBACK_TYPE | None = None
        self._revision_seen = 0
        self._stopped = False

        self.connection = connection.DeviceConnection(
            entry.data[CONF_DEVICE_ID],
            entry.data[CONF_HOST],
            entry.data[CONF_LOCAL_KEY],
            float(entry.data[CONF_VERSION]),
            on_dps=self._from_thread(self._handle_dps),
            on_connected=self._from_thread(self._handle_connected),
            on_stalled=self._from_thread(self._handle_stalled),
        )

    # --- what the entities show ---

    @property
    def visible_rooms(self) -> dict[int, str]:
        return {
            i: n for i, n in sorted(self.rooms.items()) if i not in self.hidden_rooms
        }

    @property
    def run_rooms(self) -> list[int] | None:
        """The rooms the current run was asked to clean: a command waiting for its
        run, or the one that started the run that is under way."""
        if self._pending is not None:
            return self._pending[0]
        if self.tracker.live_point() is not None:
            return self._rooms_by_run.get(self.tracker.path_id)
        return None

    @property
    def position(self) -> tuple[int, int] | None:
        """Where the vacuum last was, while a run is under way."""
        return self.tracker.live_point()

    @property
    def current_room(self) -> str | None:
        """The learned room under the vacuum, or None: not running, or the cell
        belongs to no room we have learned."""
        point = self.position
        if point is None:
            return None
        cell = self.grid.cell
        room = self.layer.owner((point[0] // cell, point[1] // cell))
        return self.rooms.get(room) if room is not None else None

    @property
    def covered_area(self) -> float:
        """Square metres, from the cell count and the units-per-metre option."""
        cell = self.grid.cell / self.units_per_metre
        return round(self.grid.covered_cells() * cell * cell, 2)

    @property
    def run_points(self) -> int:
        return self.tracker.point_count if self._live else self._stored_points

    @property
    def run_path_id(self) -> int | None:
        return self.tracker.path_id if self._live else self._stored_path_id

    @property
    def last_run(self) -> list[list[Point]]:
        return self.tracker.last_run if self._live else self._stored_last_run

    @property
    def _live(self) -> bool:
        return self.tracker.path_id is not None

    # --- loading and saving ---

    def _map_key(self, map_id: int) -> str:
        """A salted hash, so the mapId (private, PROTOCOL) is never stored."""
        return hashlib.sha256(f"{self.entry.entry_id}:{map_id}".encode()).hexdigest()[
            :16
        ]

    async def async_load(self) -> None:
        stored = await self.store.async_load()
        if stored is None:
            return
        try:
            grid = CoverageGrid.from_bytes(
                base64.b64decode(stored["grid"], validate=True)
            )
            grid.set_brush(self.brush)
            finished = [int(i) for i in stored["finished_ids"]]
            rooms = {int(i): str(n) for i, n in stored["rooms"].items()}
            last_run = [
                [(int(x), int(y)) for x, y in segment] for segment in stored["last_run"]
            ]
            map_hash = stored.get("map_hash")
            previous = stored.get("previous_grid")
            path_id = stored.get("last_path_id")
            points = int(stored.get("last_points", 0))
            self._previous_grid = (
                base64.b64decode(previous, validate=True) if previous else None
            )
            self._map_hash = map_hash if isinstance(map_hash, str) else None
            self._stored_path_id = int(path_id) if path_id is not None else None
        except (
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
            binascii.Error,
        ):
            # Never include the stored data: it holds the owner's home layout.
            _LOGGER.warning(
                "The stored map could not be read, starting with an empty one"
            )
            self._previous_grid = None
            self._map_hash = None
            self._stored_path_id = None
            return
        self.grid = grid
        try:
            raw = stored.get("room_layer")
            self.layer = (
                RoomLayer.from_bytes(base64.b64decode(raw, validate=True))
                if raw
                else RoomLayer()
            )
        except (ValueError, TypeError, binascii.Error):
            _LOGGER.warning("The stored room outlines could not be read, starting over")
            self.layer = RoomLayer()
        self.tracker = RunTracker(
            grid, finished_ids=finished, idle_seconds=IDLE_SECONDS
        )
        self.rooms = rooms
        self._stored_last_run = last_run
        self._stored_points = points

    def _capture(self) -> dict[str, Any]:
        """What a save needs, copied on the event loop so another thread can encode it
        (compressing a whole-home grid takes tens of milliseconds)."""
        return {
            "grid": self.grid.copy(),
            "layer": self.layer.copy(),
            "previous_grid": self._previous_grid,
            "rooms": dict(self.rooms),
            "map_hash": self._map_hash,
            "finished_ids": self.tracker.finished_ids,
            "last_run": [list(segment) for segment in self.last_run],
            "last_path_id": self.run_path_id,
            "last_points": self.run_points,
        }

    @staticmethod
    def _encode_capture(parts: dict[str, Any]) -> dict[str, Any]:
        previous = parts["previous_grid"]
        return {
            "grid": _encode(parts["grid"].to_bytes()),
            "room_layer": _encode(parts["layer"].to_bytes()),
            "previous_grid": _encode(previous) if previous else None,
            "rooms": {str(i): n for i, n in parts["rooms"].items()},
            "map_hash": parts["map_hash"],
            "finished_ids": parts["finished_ids"],
            "last_run": [[list(p) for p in segment] for segment in parts["last_run"]],
            "last_path_id": parts["last_path_id"],
            "last_points": parts["last_points"],
        }

    @callback
    def _save_later(self) -> None:
        """Save in 30 s, unless a save is already waiting."""
        if self._save_timer is None and not self._stopped:
            self._save_timer = async_call_later(self.hass, SAVE_DELAY, self._save_due)

    @callback
    def _save_due(self, _now: datetime) -> None:
        self._save_timer = None
        self.entry.async_create_background_task(
            self.hass, self._save_now(), "tuya_vacuum_local_map save"
        )

    async def _save_now(self) -> None:
        if self._save_timer is not None:
            self._save_timer()
            self._save_timer = None
        self.save_count += 1
        parts = self._capture()
        data = await self.hass.async_add_executor_job(self._encode_capture, parts)
        await self.store.async_save(data)

    # --- lifecycle ---

    def _from_thread(self, handler: Callable[..., None]) -> Callable[..., None]:
        """Hand a callback from the connection thread to the event loop."""

        def post(*args: Any) -> None:
            self.hass.loop.call_soon_threadsafe(handler, *args)

        return post

    @property
    def _issue_id(self) -> str:
        return f"{ISSUE_STALLED}_{self.entry.entry_id}"

    def start(self) -> None:
        self.entry.async_on_unload(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)
        )
        self.entry.async_on_unload(
            async_track_time_interval(self.hass, self._tick, TICK)
        )
        self._request_render(immediate=True)
        self.connection.start()

    async def _on_stop(self, _event: Event) -> None:
        await self.async_stop()

    async def async_stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for timer in (self._render_timer, self._notify_timer, self._save_timer):
            if timer is not None:
                timer()
        self._render_timer = self._notify_timer = self._save_timer = None
        await self.hass.async_add_executor_job(self.connection.stop)
        self._learn(self.tracker.flush())
        await self._save_now()
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    async def async_reset(self) -> None:
        """Forget the local coverage. Sends nothing to the vacuum."""
        self._new_map()
        self._runs_changed()

    def _new_map(self) -> None:
        """Start an empty grid, keeping the old one for one generation in storage."""
        if self.grid.covered_cells():
            self._previous_grid = self.grid.to_bytes()
        self.grid = CoverageGrid(brush=self.brush)
        # The run IDs stay, so the app re-sending the last run isn't counted again.
        self.tracker = RunTracker(
            self.grid,
            finished_ids=self.tracker.finished_ids,
            idle_seconds=IDLE_SECONDS,
        )
        self._stored_last_run = []
        self._stored_points = 0
        self._stored_path_id = None
        self._revision_seen = 0
        self._pending = None
        self._rooms_by_run = {}
        self._attributed = {}
        self.layer = RoomLayer()

    # --- on the event loop ---

    @callback
    def _handle_connected(self, connected: bool) -> None:
        self.connected = connected
        self._send()

    @callback
    def _handle_stalled(self, stalled: bool) -> None:
        if stalled:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_STALLED,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    @callback
    def _handle_dps(self, dps: dict[str, Any]) -> None:
        if self._stopped:
            return
        now = _monotonic()
        changed = False
        for dp, value in dps.items():
            frame = decode_dp(dp, value)
            if frame is None:
                continue
            self.frame_counts[frame.info_type] += 1
            self.largest_frame = max(self.largest_frame, len(value))
            if frame.info_type not in KNOWN_TYPES:
                self._note_unknown(frame.info_type, list(frame.data))
                continue
            chunk = parse_path(frame)
            if chunk is not None:
                latest = self.tracker.path_id
                finished = self.tracker.add(chunk, now)
                if finished:
                    self._finished(finished)
                if self.tracker.path_id != latest:
                    self._bind_command(self.tracker.path_id, now)
                continue
            status = parse_status(frame)
            if status is not None:
                self.reported_area = status.clean_area
                continue
            parsed = parse_rooms(frame)
            if parsed is not None:
                changed |= self._handle_rooms(*parsed)
                continue
            command = parse_room_command(frame)
            if command is not None:
                self._pending = (command, now)
        if self.tracker.revision != self._revision_seen:
            self._revision_seen = self.tracker.revision
            self._request_render()
            changed = True
        if changed:
            self._notify()

    def _note_unknown(self, info_type: int, fields: list[str]) -> None:
        if info_type not in self.unknown_types:
            if len(self.unknown_types) >= _MAX_UNKNOWN_TYPES:
                return
            self.unknown_types[info_type] = sorted(map(str, fields))[:_MAX_FIELD_NAMES]

    def _handle_rooms(self, map_id: int, rooms: list[Room]) -> bool:
        key = self._map_key(map_id)
        if self._map_hash is not None and key != self._map_hash:
            _LOGGER.info("The vacuum made a new map, starting a new coverage map")
            self._new_map()
            self.rooms = {}
            self._runs_changed()
        before = dict(self.rooms), self._map_hash
        self._map_hash = key
        self.rooms.update({room.id: room.name for room in rooms})
        if (dict(self.rooms), self._map_hash) != before:
            self._save_later()
            return True
        return False

    @callback
    def _tick(self, _now: datetime) -> None:
        finished = self.tracker.poll(_monotonic())
        if finished:
            self._finished(finished)
            self._notify()

    def _bind_command(self, path_id: int | None, now: float) -> None:
        """A run's first point arrived: it belongs to the room command just before it
        (PROTOCOL: about 3 s earlier). A run with no recent command gets none."""
        if path_id is None or path_id in self._rooms_by_run:
            return
        rooms = None
        if self._pending is not None:
            command, seen = self._pending
            if now - seen <= COMMAND_WINDOW:
                rooms = command
            self._pending = None
        self._rooms_by_run[path_id] = rooms
        while len(self._rooms_by_run) > 10:
            del self._rooms_by_run[next(iter(self._rooms_by_run))]

    def _learn(self, run_ids: list[int]) -> None:
        """From runs just counted: learn room outlines, and compare the area with
        what the vacuum reported."""
        for run_id in run_ids:
            cells = self.tracker.run_cells(run_id)
            if not cells:
                continue
            rooms = self._rooms_by_run.get(run_id)
            if rooms:
                self.layer.add_run(
                    rooms, cells, self._attributed.get(run_id, frozenset())
                )
                self._attributed[run_id] = cells
                while len(self._attributed) > 5:
                    del self._attributed[next(iter(self._attributed))]
            self._calibrate(len(cells))

    def _calibrate(self, cells: int) -> None:
        reported = self.reported_area
        if not reported:
            return
        edge = self.grid.cell / self.units_per_metre
        area = cells * edge * edge
        self.calibration.append(
            {
                "map_area": round(area, 2),
                "reported_area": reported,
                "ratio": round(area / reported, 2),
            }
        )
        del self.calibration[:-KEEP_CALIBRATION]

    def _finished(self, run_ids: list[int]) -> None:
        """A run was counted: draw and save now, don't wait for the timers."""
        self._learn(run_ids)
        self._runs_changed()

    def _runs_changed(self) -> None:
        self._request_render(immediate=True)
        self.entry.async_create_background_task(
            self.hass, self._save_now(), "tuya_vacuum_local_map save"
        )
        self._notify()

    # --- entity updates, at most once a second ---

    def _send(self) -> None:
        self._last_notify = _monotonic()
        self.notify_count += 1
        async_dispatcher_send(self.hass, self.signal)

    @callback
    def _notify(self) -> None:
        wait = NOTIFY_INTERVAL - (_monotonic() - self._last_notify)
        if wait <= 0:
            self._send()
        elif self._notify_timer is None:
            self._notify_timer = async_call_later(self.hass, wait, self._notify_due)

    @callback
    def _notify_due(self, _now: datetime) -> None:
        self._notify_timer = None
        if not self._stopped:
            self._send()

    # --- drawing, at most every 30 s ---

    @callback
    def _request_render(self, immediate: bool = False) -> None:
        if self._stopped:
            return
        if self._rendering:
            self._render_again = True
            return
        wait = (
            0.0 if immediate else RENDER_INTERVAL - (_monotonic() - self._last_render)
        )
        if wait <= 0:
            if self._render_timer is not None:
                self._render_timer()
                self._render_timer = None
            self._rendering = True
            self.entry.async_create_background_task(
                self.hass, self._render(), "tuya_vacuum_local_map render"
            )
        elif self._render_timer is None:
            self._render_timer = async_call_later(self.hass, wait, self._render_due)

    @callback
    def _render_due(self, _now: datetime) -> None:
        self._render_timer = None
        self._request_render()

    async def _render(self) -> None:
        # Everything the drawing needs is copied here, on the loop, so the executor
        # never sees the grid while it changes.
        data = RenderInput(
            grid=self.grid.copy(),
            last_run=[list(segment) for segment in self.last_run],
            rooms=[Room(i, n) for i, n in sorted(self.rooms.items())],
            hidden_rooms=self.hidden_rooms,
            position=self.position,
            room_layer=self.layer.copy(),
        )
        try:
            self.svg = await self.hass.async_add_executor_job(render_svg, data)
        finally:
            self._rendering = False
            self._last_render = _monotonic()
        self.render_count += 1
        self.image_updated = dt_util.utcnow()
        self._send()
        if self._render_again:
            self._render_again = False
            self._request_render()

    # --- diagnostics ---

    def diagnostics(self) -> dict[str, Any]:
        """Counts and field names only: no names, coordinates or identifiers."""
        return {
            "connected": self.connected,
            "reconnects": self.connection.reconnects,
            "last_error": self.connection.last_error,
            "frames_by_info_type": {
                str(k): v for k, v in sorted(self.frame_counts.items())
            },
            "unknown_info_types": {str(k): v for k, v in self.unknown_types.items()},
            "largest_frame": self.largest_frame,
            "rooms": len(self.rooms),
            "rooms_with_outlines": len(self.layer.known_rooms()),
            "reported_area": self.reported_area,
            "calibration": list(self.calibration),
            "covered_cells": self.grid.covered_cells(),
            "finished_runs": len(self.tracker.finished_ids),
            "renders": self.render_count,
            "saves": self.save_count,
        }
