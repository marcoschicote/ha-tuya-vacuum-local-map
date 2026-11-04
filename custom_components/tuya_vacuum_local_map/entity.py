"""The base for every entity: one device, push updates, nothing polled."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .coordinator import Coordinator


class TuyaVacuumEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: Coordinator, key: str) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.signal, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
