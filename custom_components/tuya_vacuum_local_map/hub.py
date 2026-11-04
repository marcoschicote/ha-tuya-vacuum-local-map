"""Owns the connection for one config entry and brings its data into Home Assistant.

For now it only keeps what the config and options flows need (connection state, room
names) and counts frames by type. Phase 4's coordinator takes over the routing.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import connection
from .const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_VERSION,
    DOMAIN,
    ISSUE_STALLED,
)
from .core.protocol import decode_dp, parse_rooms

_LOGGER = logging.getLogger(__name__)

type TuyaVacuumConfigEntry = ConfigEntry[Hub]


class Hub:
    def __init__(self, hass: HomeAssistant, entry: TuyaVacuumConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.connected = False
        self.rooms: dict[int, str] = {}  # merged per room ID, never replaced
        self.frame_counts: Counter[int] = Counter()
        self.connection = connection.DeviceConnection(
            entry.data[CONF_DEVICE_ID],
            entry.data[CONF_HOST],
            entry.data[CONF_LOCAL_KEY],
            float(entry.data[CONF_VERSION]),
            on_dps=self._from_thread(self._handle_dps),
            on_connected=self._from_thread(self._handle_connected),
            on_stalled=self._from_thread(self._handle_stalled),
        )

    def _from_thread(self, handler: Any) -> Any:
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
        self.connection.start()

    async def _on_stop(self, _event: Event) -> None:
        await self.async_stop()

    async def async_stop(self) -> None:
        await self.hass.async_add_executor_job(self.connection.stop)
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    # --- on the event loop ------------------------------------------------------

    def _handle_connected(self, connected: bool) -> None:
        self.connected = connected

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

    def _handle_dps(self, dps: dict[str, Any]) -> None:
        for dp, value in dps.items():
            frame = decode_dp(dp, value)
            if frame is None:
                continue
            self.frame_counts[frame.info_type] += 1
            parsed = parse_rooms(frame)
            if parsed is not None:
                self.rooms.update({room.id: room.name for room in parsed[1]})
