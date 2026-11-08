"""Set-up and options flows."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .connection import InvalidKey, Unreachable, probe
from .const import (
    CONF_BRUSH,
    CONF_DEVICE_ID,
    CONF_HIDDEN_ROOMS,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_ROOM_NAMES,
    CONF_UNITS_PER_METRE,
    CONF_VERSION,
    DEFAULT_BRUSH,
    DEFAULT_UNITS_PER_METRE,
    DEFAULT_VERSION,
    DOMAIN,
    VERSIONS,
)
from .coordinator import TuyaVacuumConfigEntry

_LOGGER = logging.getLogger(__name__)

TITLE = "Tuya vacuum"  # never the device ID: titles show up in logs and screenshots


def _schema(user_input: dict[str, Any] | None) -> vol.Schema:
    defaults = user_input or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_DEVICE_ID, default=defaults.get(CONF_DEVICE_ID, "")): str,
            vol.Required(CONF_LOCAL_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_VERSION, default=defaults.get(CONF_VERSION, DEFAULT_VERSION)
            ): SelectSelector(
                SelectSelectorConfig(options=VERSIONS, mode=SelectSelectorMode.DROPDOWN)
            ),
        }
    )


class TuyaVacuumConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            device_id = user_input[CONF_DEVICE_ID].strip()
            key = user_input[CONF_LOCAL_KEY].strip()
            version = user_input[CONF_VERSION]
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()
            if float(version) > 3.1 and len(key) != 16:
                errors["base"] = "invalid_auth"  # tinytuya needs exactly 16 characters
            else:
                try:
                    await self.hass.async_add_executor_job(
                        probe, device_id, host, key, float(version)
                    )
                except InvalidKey:
                    errors["base"] = "invalid_auth"
                except Unreachable:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=TITLE,
                        data={
                            CONF_HOST: host,
                            CONF_DEVICE_ID: device_id,
                            CONF_LOCAL_KEY: key,
                            CONF_VERSION: version,
                        },
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: TuyaVacuumConfigEntry) -> OptionsFlow:
        return TuyaVacuumOptionsFlow()


def _parse_room_names(text: str) -> dict[str, str] | None:
    """Lines of `id: name`. None if a line isn't one."""
    names: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        room, sep, name = line.partition(":")
        room, name = room.strip(), name.strip()
        if not sep or not room.isdigit():
            return None
        if name:
            names[str(int(room))] = name
    return names


class TuyaVacuumOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self.config_entry.options
        hub = getattr(self.config_entry, "runtime_data", None)
        rooms = dict(getattr(hub, "named_rooms", {}))
        learned = set(getattr(hub, "known_room_ids", set()))
        errors: dict[str, str] = {}
        if user_input is not None:
            names = _parse_room_names(user_input.get(CONF_ROOM_NAMES, ""))
            if names is None:
                errors[CONF_ROOM_NAMES] = "invalid_room_names"
        if user_input is not None and not errors:
            return self.async_create_entry(
                data={
                    CONF_ROOM_NAMES: names,
                    CONF_HIDDEN_ROOMS: sorted(
                        int(room) for room in user_input.get(CONF_HIDDEN_ROOMS, [])
                    ),
                    CONF_BRUSH: int(user_input[CONF_BRUSH]),
                    CONF_UNITS_PER_METRE: int(user_input[CONF_UNITS_PER_METRE]),
                }
            )
        for room_id in learned:
            rooms.setdefault(room_id, f"Room {room_id}")
        hidden = [int(room) for room in options.get(CONF_HIDDEN_ROOMS, [])]
        for room_id in hidden:
            rooms.setdefault(room_id, f"Room {room_id}")  # keep unseen ones selectable
        choices = [
            SelectOptionDict(value=str(room_id), label=name)
            for room_id, name in sorted(rooms.items())
        ]
        # One line per room that has no name from the vacuum, so it is easy to fill in.
        saved = options.get(CONF_ROOM_NAMES, {})
        listed = set(getattr(hub, "rooms", {}))
        lines = [
            f"{room}: {saved.get(str(room), '')}".rstrip()
            for room in sorted({int(room) for room in saved} | (learned - listed))
        ]
        schema = vol.Schema(
            {
                vol.Optional(CONF_ROOM_NAMES, default="\n".join(lines)): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Optional(
                    CONF_HIDDEN_ROOMS, default=[str(room) for room in hidden]
                ): SelectSelector(SelectSelectorConfig(options=choices, multiple=True)),
                vol.Required(
                    CONF_BRUSH, default=options.get(CONF_BRUSH, DEFAULT_BRUSH)
                ): NumberSelector(
                    NumberSelectorConfig(min=50, max=1000, step=10, mode="box")
                ),
                vol.Required(
                    CONF_UNITS_PER_METRE,
                    default=options.get(CONF_UNITS_PER_METRE, DEFAULT_UNITS_PER_METRE),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=100000, step=1, mode="box")
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
