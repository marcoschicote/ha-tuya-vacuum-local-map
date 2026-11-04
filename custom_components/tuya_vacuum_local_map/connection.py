"""A read-only local connection to one Tuya device, on its own thread.

The only things ever sent to the device are `status()` and `heartbeat()` (AGENTS.md,
rule 2): the device is wrapped so that anything else raises, and a test checks it.
Nothing here imports Home Assistant. Callbacks run on the connection's thread, so the
caller must hand work over to its own event loop.

Nothing that identifies the device or its owner (ID, key, host) is ever logged.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import tinytuya
from tinytuya.core import const, message_helper

_LOGGER = logging.getLogger(__name__)

# tinytuya rejects any message over 1440 bytes as corrupt, and these vacuums send
# more (PROTOCOL, "Raising tinytuya's limit"). The header check reads the copy in
# `message_helper`, so both are set. It is process-wide and only ever raised: another
# integration using tinytuya in the same Home Assistant just accepts more.
MAX_PAYLOAD_LENGTH = 65536
for _module in (const, message_helper):
    _module.MAX_PAYLOAD_LENGTH = max(_module.MAX_PAYLOAD_LENGTH, MAX_PAYLOAD_LENGTH)

HEARTBEAT_SECONDS = 8
SOCKET_TIMEOUT = 5
BACKOFF_START = 5
BACKOFF_MAX = 60
STALLED_AFTER = 600  # seconds without a connection before `on_stalled(True)`
JOIN_TIMEOUT = 10

# tinytuya reports failures as {"Err": "<code>"} instead of raising. In receive mode a
# timeout is `None` and is the normal idle case; no code means "idle". These two say
# the key or protocol version is wrong (see Device.status() in tinytuya 1.20).
_ERR_PAYLOAD = "904"
_ERR_KEY_OR_VERSION = "914"
_BAD_KEY_CODES = frozenset({_ERR_PAYLOAD, _ERR_KEY_OR_VERSION})

# The only methods that may be called on the device.
_ALLOWED = frozenset(
    {"status", "heartbeat", "receive", "set_socketTimeout", "close"},
)


class DeviceError(Exception):
    """The device could not be read. `code` is tinytuya's error code, if any."""

    def __init__(self, code: str | None = None) -> None:
        super().__init__(f"device error {code}" if code else "device error")
        self.code = code


class Unreachable(DeviceError):
    """Could not connect, or the connection dropped."""


class InvalidKey(DeviceError):
    """The device answered, but not in a way the local key and version explain."""


class ReadOnlyDevice:
    """Passes `status`, `heartbeat` and `receive` through and refuses the rest."""

    def __init__(self, device: Any) -> None:
        object.__setattr__(self, "_device", device)

    def __getattr__(self, name: str) -> Any:
        if name not in _ALLOWED:
            raise AttributeError(f"{name} is not allowed: this connection is read-only")
        return getattr(self._device, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("this connection is read-only")


DeviceFactory = Callable[[str, str, str, float, bool], Any]


def _tinytuya_device(
    device_id: str, host: str, key: str, version: float, persist: bool
) -> Any:
    return tinytuya.Device(device_id, host, key, version=version, persist=persist)


def check_reply(message: Any) -> None:
    """Raise if a reply from tinytuya is an error dict. `None` and data are fine."""
    if isinstance(message, dict) and "Err" in message:
        code = str(message["Err"])
        raise (InvalidKey if code in _BAD_KEY_CODES else Unreachable)(code)


def probe(
    device_id: str,
    host: str,
    key: str,
    version: float,
    device_factory: DeviceFactory | None = None,
) -> None:
    """Connect once and ask for the status. Blocking: run it in an executor.

    Raises Unreachable or InvalidKey. Never raises anything with the key in it.
    """
    factory = device_factory or _tinytuya_device  # looked up now, so tests can patch it
    device = ReadOnlyDevice(factory(device_id, host, key, version, False))
    try:
        device.set_socketTimeout(SOCKET_TIMEOUT)
        reply = device.status()
    except DeviceError:
        raise
    except Exception:  # noqa: BLE001 - tinytuya can raise anything
        raise Unreachable() from None  # not chained: the message could hold the key
    finally:
        device.close()
    if reply is None:
        raise Unreachable()
    check_reply(reply)


class DeviceConnection:
    """Holds a persistent connection and delivers the device's data points.

    `on_dps(dps)` gets each `dps` dict, including the one in the first status reply.
    `on_connected(bool)` is called when the state changes. `on_stalled(bool)` turns
    on after STALLED_AFTER seconds without a connection, and off when it returns.
    """

    def __init__(
        self,
        device_id: str,
        host: str,
        key: str,
        version: float,
        on_dps: Callable[[dict[str, Any]], None],
        on_connected: Callable[[bool], None] = lambda connected: None,
        on_stalled: Callable[[bool], None] = lambda stalled: None,
        *,
        device_factory: DeviceFactory | None = None,
        heartbeat_seconds: float | None = None,
        backoff_start: float | None = None,
        backoff_max: float | None = None,
        stalled_after: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._args = (device_id, host, key, version)
        self._on_dps = on_dps
        self._on_connected = on_connected
        self._on_stalled = on_stalled
        # Module constants are read here, not at import, so tests can change them.
        self._factory = device_factory or _tinytuya_device
        self._heartbeat = heartbeat_seconds or HEARTBEAT_SECONDS
        self._backoff_start = backoff_start or BACKOFF_START
        self._backoff_max = backoff_max or BACKOFF_MAX
        self._stalled_after = stalled_after or STALLED_AFTER
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._device: ReadOnlyDevice | None = None
        self._connected = False
        self._stalled = False
        self.reconnects = 0
        self.last_error: str | None = None  # a tinytuya code or an exception name

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("already started")
        self._thread = threading.Thread(
            target=self._run, name="tuya_vacuum_local_map", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop and wait for the thread. Blocking: run it in an executor."""
        self._stop.set()
        device = self._device
        if device is not None:
            try:
                device.close()  # wakes a blocked read, so we needn't wait the timeout
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(JOIN_TIMEOUT)
            if self._thread.is_alive():
                _LOGGER.warning("Connection thread did not stop in time")

    # --- the thread --------------------------------------------------------------

    def _call(self, callback: Callable[..., None], *args: Any) -> None:
        try:
            callback(*args)
        except Exception as err:  # noqa: BLE001 - a callback must not kill the thread
            _LOGGER.debug("Callback failed: %s", type(err).__name__)

    def _set_connected(self, connected: bool) -> None:
        if connected != self._connected:
            self._connected = connected
            self._call(self._on_connected, connected)

    def _run(self) -> None:
        backoff = self._backoff_start
        down_since = self._clock()
        warned = False
        while not self._stop.is_set():
            was_connected = False
            try:
                self._session()
            except Exception as err:  # noqa: BLE001 - anything means "reconnect"
                if self._stop.is_set():
                    break
                was_connected = self._connected
                code = err.code if isinstance(err, DeviceError) else None
                self.last_error = code or type(err).__name__
                # One warning per outage, so a vacuum that is off the network
                # doesn't flood the log. Details never include the host or key.
                if was_connected or not warned:
                    _LOGGER.warning("Connection lost (%s)", self.last_error)
                    warned = True
                else:
                    _LOGGER.debug("Connection failed (%s)", self.last_error)
            finally:
                self._device = None
            if self._stop.is_set():
                break
            if was_connected:  # it worked for a while: start the backoff afresh
                down_since = self._clock()
                backoff = self._backoff_start
            self._set_connected(False)
            self.reconnects += 1
            if not self._stalled and self._clock() - down_since >= self._stalled_after:
                self._stalled = True
                self._call(self._on_stalled, True)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self._backoff_max)
        self._set_connected(False)

    def _session(self) -> None:
        """One connection, until it fails. Always ends by raising."""
        device = ReadOnlyDevice(self._factory(*self._args, True))
        self._device = device
        try:
            device.set_socketTimeout(SOCKET_TIMEOUT)
            first = device.status()
            if first is None:
                raise Unreachable()
            check_reply(first)
            if self._stalled:
                self._stalled = False
                self._call(self._on_stalled, False)
            self._set_connected(True)
            self._deliver(first)
            last_beat = self._clock()
            while not self._stop.is_set():
                message = device.receive()  # None after a timeout: just idle
                if message:
                    check_reply(message)
                    self._deliver(message)
                if self._clock() - last_beat > self._heartbeat:
                    check_reply(device.heartbeat(nowait=True))
                    last_beat = self._clock()
        finally:
            device.close()
        raise Unreachable()  # only reached if stop() ended the loop

    def _deliver(self, message: Any) -> None:
        dps = message.get("dps") if isinstance(message, dict) else None
        if isinstance(dps, dict) and dps:
            self._call(self._on_dps, dps)
