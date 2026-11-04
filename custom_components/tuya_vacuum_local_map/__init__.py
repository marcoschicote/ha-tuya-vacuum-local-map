"""Tuya Vacuum Local Map integration: a coverage map from a local connection.

Keep Home Assistant imports out of this module's top level (use TYPE_CHECKING or
import inside functions), so tests of ``core`` can run without Home Assistant.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .hub import TuyaVacuumConfigEntry

PLATFORMS: list[str] = []  # entities arrive in phase 4


async def async_setup_entry(hass: HomeAssistant, entry: TuyaVacuumConfigEntry) -> bool:
    # hub imports tinytuya, which must not be imported inside the event loop.
    hub_module = await hass.async_add_import_executor_job(
        importlib.import_module, f"{__name__}.hub"
    )
    hub = hub_module.Hub(hass, entry)
    entry.runtime_data = hub
    entry.async_on_unload(entry.add_update_listener(_reload_on_options_change))
    hub.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TuyaVacuumConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return True


async def _reload_on_options_change(
    hass: HomeAssistant, entry: TuyaVacuumConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
