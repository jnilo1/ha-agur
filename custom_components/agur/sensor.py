"""Sensor platform for Agur."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume, CURRENCY_EURO
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .const import SENSOR_PLATFORM
from .coordinator import AgurDataUpdateCoordinator

_LOGGER: logging.Logger = logging.getLogger(__name__)

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="last_index",
        translation_key="last_index",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
    ),
    SensorEntityDescription(
        key="water_consumption",
        translation_key="water_consumption",
        icon="mdi:water",
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
    ),
    SensorEntityDescription(
        key="last_invoice",
        translation_key="last_invoice",
        icon="mdi:receipt-text-check-outline",
        state_class=SensorStateClass.TOTAL,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_EURO,
    ),
    SensorEntityDescription(
        key="balance",
        translation_key="balance",
        icon="mdi:bank-transfer-out",
        state_class=SensorStateClass.TOTAL,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_EURO,
    ),
)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Add Agur sensors from a config_entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for contract_id in coordinator.contract_ids:
        _LOGGER.debug(f"Add sensor for Agur contract {contract_id}")
        for entity_description in SENSORS:
            # Skip water_consumption sensor if statistics import is disabled
            if entity_description.key == "water_consumption" and not coordinator.import_statistics:
                continue
            entities.append(AgurSensor(
                coordinator=coordinator,
                contract_id=contract_id,
                entry_id=config_entry.entry_id,
                entity_description=entity_description
            ))
    async_add_entities(entities, True)


def build_unique_id(key: str, entry_id: str, contract_id: str) -> str:
    """Build the unique ID of a sensor.

    It must include the contract ID: a single config entry (account) can hold several contracts,
    each of them exposing the same set of sensors.
    """
    return f"{key}_{entry_id}_{contract_id}"


class AgurSensor(CoordinatorEntity[AgurDataUpdateCoordinator], SensorEntity):
    """Agur sensor class."""
    _attr_has_entity_name = True

    def __init__(
            self,
            coordinator: AgurDataUpdateCoordinator,
            entry_id: str,
            contract_id: str,
            entity_description: SensorEntityDescription
    ) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator=coordinator)

        self.entity_description = entity_description

        self.entity_id = f"{SENSOR_PLATFORM}.{DOMAIN}_{entity_description.key}_{contract_id}"
        self._attr_unique_id = build_unique_id(entity_description.key, entry_id, contract_id)
        self._contract_id = contract_id

        self._attr_extra_state_attributes = {
            "contract_id": contract_id,
            "contract_address": self.coordinator.data[contract_id].contract.address,
            "contract_owner": self.coordinator.data[contract_id].contract.owner,
            "meter_serial_number": self.coordinator.data[contract_id].contract.meter_serial_number,
        }

        if entity_description.key.startswith("last_"):
            self._attr_extra_state_attributes["date"] = getattr(
                self.coordinator.data[self._contract_id],
                f"{self.entity_description.key}_date"
            )

        if entity_description.key == "last_invoice":
            invoice = self.coordinator.data[contract_id].last_invoice
            if invoice is not None:
                self._attr_extra_state_attributes["invoice_number"] = invoice.number
                self._attr_extra_state_attributes["payment_date"] = invoice.payment_date

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.data[contract_id].contract.meter_id)},
            name=self.coordinator.data[contract_id].contract.meter_serial_number,
            serial_number=self.coordinator.data[contract_id].contract.meter_serial_number
        )

    @property
    def native_value(self) -> float:
        """Return the state of the sensor."""
        if self.entity_description.key == "water_consumption":
            # Return the latest cumulative value in cubic meters
            data_points = self.coordinator.data[self._contract_id].data_points
            return data_points[0].value / 1000 if data_points else None

        return getattr(
            self.coordinator.data[self._contract_id],
            self.entity_description.key if self.entity_description.key == "balance" else f"{self.entity_description.key}_value"
        )
