"""Tuya Vacuum Local Map integration: a coverage map from a local connection.

Keep Home Assistant imports out of this module's top level (use TYPE_CHECKING or
import inside functions), so tests of ``core`` can run without Home Assistant.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import TuyaVacuumConfigEntry

PLATFORMS = ["image", "sensor", "binary_sensor", "button"]


async def async_setup_entry(hass: HomeAssistant, entry: TuyaVacuumConfigEntry) -> bool:
    # coordinator imports tinytuya, which must not be imported inside the event loop.
    module = await hass.async_add_import_executor_job(
        importlib.import_module, f"{__name__}.coordinator"
    )
    coordinator = module.Coordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_reload_on_options_change))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TuyaVacuumConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def _reload_on_options_change(
    hass: HomeAssistant, entry: TuyaVacuumConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: TuyaVacuumConfigEntry) -> None:
    """Delete the stored map with the entry: it holds the layout of someone's home."""
    from homeassistant.helpers.storage import Store

    from .const import STORAGE_VERSION, storage_key

    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
