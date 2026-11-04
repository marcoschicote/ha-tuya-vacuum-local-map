"""Rooms, the current or last run, and the covered area."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfArea
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import RUN_POINTS_INTERVAL
from .coordinator import Coordinator, TuyaVacuumConfigEntry
from .entity import TuyaVacuumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuyaVacuumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            RoomsSensor(coordinator),
            CurrentRoomSensor(coordinator),
            RunPointsSensor(coordinator),
            AreaSensor(coordinator),
        ]
    )


class RoomsSensor(TuyaVacuumEntity, SensorEntity):
    # The list could be long and changes rarely: keep it out of the recorder.
    _unrecorded_attributes = frozenset({"rooms"})

    def __init__(self, coordinator: Coordinator) -> None:
        super().__init__(coordinator, "rooms")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.visible_rooms)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rooms = self.coordinator.visible_rooms
        return {"rooms": [{"id": i, "name": n} for i, n in rooms.items()]}


class RunPointsSensor(TuyaVacuumEntity, SensorEntity):
    """Points in the current or last run.

    During a run the count would change every second, and every change is a row in
    the recorder's database, so it is shown at most every RUN_POINTS_INTERVAL
    seconds. Outside a run, and when a different run starts, it is exact.
    """

    def __init__(self, coordinator: Coordinator) -> None:
        super().__init__(coordinator, "run_points")
        self._shown: int | None = None
        self._shown_at = 0.0
        self._shown_run: int | None = None

    @property
    def native_value(self) -> int:
        coordinator = self.coordinator
        now = coordinator.now()
        points = coordinator.run_points
        if (
            self._shown is None
            or not coordinator.run_active
            or coordinator.run_path_id != self._shown_run
            or points < self._shown
            or now - self._shown_at >= RUN_POINTS_INTERVAL
        ):
            self._shown = points
            self._shown_at = now
            self._shown_run = coordinator.run_path_id
        return self._shown

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        path_id = self.coordinator.run_path_id
        if path_id is None:
            return {}
        # pathID is the run's start time in Unix seconds (PROTOCOL).
        return {
            "path_id": path_id,
            "started": datetime.fromtimestamp(path_id, UTC).isoformat(),
        }


class AreaSensor(TuyaVacuumEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.AREA
    _attr_native_unit_of_measurement = UnitOfArea.SQUARE_METERS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Coordinator) -> None:
        super().__init__(coordinator, "covered_area")

    @property
    def native_value(self) -> float:
        return self.coordinator.covered_area


class CurrentRoomSensor(TuyaVacuumEntity, SensorEntity):
    """The room under the vacuum, from outlines learned from single-room runs.

    Unknown when no run is under way, or the vacuum is somewhere we haven't learned.
    """

    def __init__(self, coordinator: Coordinator) -> None:
        super().__init__(coordinator, "current_room")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.current_room
