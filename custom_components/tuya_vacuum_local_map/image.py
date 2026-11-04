"""The coverage map as an image entity (SVG: see PLAN, phase 4's format check)."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Coordinator, TuyaVacuumConfigEntry
from .core.grid import CoverageGrid
from .core.render import RenderInput, render_svg
from .entity import TuyaVacuumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuyaVacuumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CoverageMap(hass, entry.runtime_data)])


class CoverageMap(TuyaVacuumEntity, ImageEntity):
    _attr_content_type = "image/svg+xml"

    def __init__(self, hass: HomeAssistant, coordinator: Coordinator) -> None:
        ImageEntity.__init__(self, hass)
        TuyaVacuumEntity.__init__(self, coordinator, "coverage_map")
        self._attr_image_last_updated = coordinator.image_updated

    @callback
    def _handle_update(self) -> None:
        self._attr_image_last_updated = self.coordinator.image_updated
        super()._handle_update()

    async def async_image(self) -> bytes | None:
        if self.coordinator.svg is None:  # nothing drawn yet
            empty = RenderInput(CoverageGrid(), [], [], frozenset(), None)
            return render_svg(empty)
        return self.coordinator.svg
