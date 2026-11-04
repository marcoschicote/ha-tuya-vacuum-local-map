"""Whether the connection to the vacuum is up."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Coordinator, TuyaVacuumConfigEntry
from .entity import TuyaVacuumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuyaVacuumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([Connected(entry.runtime_data)])


class Connected(TuyaVacuumEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: Coordinator) -> None:
        super().__init__(coordinator, "connected")

    @property
    def is_on(self) -> bool:
        return self.coordinator.connected
