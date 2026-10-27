"""Sensor platform for Hydropolis Valbonne water consumption."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CONTRAT_ID, DOMAIN
from .coordinator import HydropolisConfigEntry, HydropolisCoordinator

_LOGGER = logging.getLogger(__name__)

ATTRIBUTION = "Data provided by SPL Hydropolis"


@dataclass
class HydropolisExtraStoredData(ExtraStoredData):
    """Sensor state persisted across HA restarts."""

    native_value: int | None
    last_measurement: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> HydropolisExtraStoredData | None:
        try:
            return cls(
                native_value=restored.get("native_value"),
                last_measurement=restored.get("last_measurement"),
            )
        except Exception:
            _LOGGER.debug("Could not restore sensor data from %s", restored)
            return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HydropolisConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Hydropolis sensors from a config entry."""
    coordinator = entry.runtime_data
    contrat_id = entry.data[CONF_CONTRAT_ID]
    async_add_entities([HydropolisWaterMeterSensor(coordinator, contrat_id)])


class HydropolisWaterMeterSensor(
    CoordinatorEntity[HydropolisCoordinator], RestoreEntity, SensorEntity
):
    """Water meter total reading, ever-increasing.

    Uses RestoreEntity so the last known value is available immediately
    after an HA restart, even before the API returns fresh data.
    """

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION
    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_suggested_display_precision = 0
    _attr_translation_key = "water_meter"

    def __init__(
        self,
        coordinator: HydropolisCoordinator,
        contrat_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{contrat_id}_water_meter"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, contrat_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="SPL Hydropolis",
            name=f"Hydropolis {contrat_id}",
        )
        self._restored_data: HydropolisExtraStoredData | None = None

    @property
    def extra_restore_state_data(self) -> HydropolisExtraStoredData:
        """Return sensor-specific state data to be persisted."""
        if self.coordinator.data is not None:
            return HydropolisExtraStoredData(
                native_value=self.coordinator.data.meter_total_liters,
                last_measurement=self.coordinator.data.last_measurement.isoformat(),
            )
        if self._restored_data is not None:
            return self._restored_data
        return HydropolisExtraStoredData(native_value=None, last_measurement=None)

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()

        if (last_extra := await self.async_get_last_extra_data()) is not None:
            self._restored_data = HydropolisExtraStoredData.from_dict(
                last_extra.as_dict()
            )

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is not None:
            return self.coordinator.data.meter_total_liters
        if self._restored_data is not None:
            return self._restored_data.native_value
        return None

    @property
    def available(self) -> bool:
        """Stay available as long as a reading is known.

        A water meter index does not vanish because the provider's API is
        unreachable; reporting `unavailable` silently removes the entity from
        the Energy dashboard. The `stale` attribute tells the difference.
        """
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is not None:
            return {
                "last_measurement": self.coordinator.data.last_measurement.isoformat(),
                "stale": self.coordinator.data.stale,
            }
        if self._restored_data is not None and self._restored_data.last_measurement:
            return {
                "last_measurement": self._restored_data.last_measurement,
                "stale": True,
            }
        return None
