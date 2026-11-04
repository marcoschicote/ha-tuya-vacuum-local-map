"""Reset the map. It clears local data only and sends nothing to the vacuum."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Coordinator, TuyaVacuumConfigEntry
from .entity import TuyaVacuumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuyaVacuumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ResetMap(entry.runtime_data)])


class ResetMap(TuyaVacuumEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: Coordinator) -> None:
        super().__init__(coordinator, "reset_map")

    async def async_press(self) -> None:
        # Local data only. Nothing is sent to the vacuum: DP 129 would wipe its map.
        await self.coordinator.async_reset()
