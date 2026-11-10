"""Repairs for the Agur integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import partial

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util.unit_conversion import VolumeConverter

from .const import DOMAIN
from .coordinator import statistic_id_for

_LOGGER = logging.getLogger(__name__)

ISSUE_STATISTICS_ON_SENSOR = "statistics_on_sensor"


def sensor_statistic_id(contract_id: str) -> str:
    """Statistic id under which versions 0.1.4 and 0.1.5 imported the consumption history."""
    return f"sensor.{DOMAIN}_water_consumption_{contract_id}"


async def async_convert_legacy_statistics(hass: HomeAssistant, contract_ids: list[str]) -> None:
    """Convert to m³ the external statistics imported in liters by versions up to 0.1.3.

    Statistics without unit were in liters too. The rows are read, converted and imported again under the same
    statistic id with m³ metadata: nothing is lost so there is nothing to confirm. (The recorder's own unit
    change service only handles the statistics it compiles itself, not external ones.) This must run before the
    coordinator imports new rows in m³ under the same statistic id, which would relabel the liters as m³.
    """
    recorder = get_instance(hass)
    for contract_id in contract_ids:
        external_id = statistic_id_for(contract_id)
        metadata = await _async_get_metadata(hass, external_id)
        if metadata is None or metadata["unit_of_measurement"] == UnitOfVolume.CUBIC_METERS:
            continue
        unit = metadata["unit_of_measurement"] or UnitOfVolume.LITERS
        rows = await recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            datetime(1970, 1, 1, tzinfo=timezone.utc),
            None,
            {external_id},
            "hour",
            None,
            {"sum", "state"},
        )
        convert = VolumeConverter.converter_factory_allow_none(unit, UnitOfVolume.CUBIC_METERS)
        converted = [
            StatisticData(
                start=datetime.fromtimestamp(row["start"], tz=timezone.utc),
                state=convert(row.get("state")),
                sum=convert(row.get("sum")),
            )
            for row in rows.get(external_id, [])
        ]
        _LOGGER.info(f"Converting {len(converted)} statistics rows of {external_id} from {unit} to m³")
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=metadata["name"],
                source=DOMAIN,
                statistic_id=external_id,
                unit_of_measurement=UnitOfVolume.CUBIC_METERS,
                unit_class=VolumeConverter.UNIT_CLASS,
            ),
            converted,
        )


async def async_check_and_create_repair_issues(hass: HomeAssistant, contract_ids: list[str]) -> None:
    """Raise a repair issue for the statistics imported under the `water_consumption` sensor by 0.1.4 and 0.1.5.

    The recorder also compiled its own rows for that sensor (see `statistic_id_for`): those statistics are
    unusable. They are deleted after confirmation, the external statistics having been imported again from the
    full history sent by the API.
    """
    for contract_id in contract_ids:
        external_id = statistic_id_for(contract_id)
        if await _async_get_metadata(hass, sensor_statistic_id(contract_id)) is not None:
            _LOGGER.info(f"Statistics found on {sensor_statistic_id(contract_id)}: need a repair")
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"{ISSUE_STATISTICS_ON_SENSOR}_{contract_id}",
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_STATISTICS_ON_SENSOR,
                translation_placeholders={
                    "contract_id": contract_id,
                    "sensor_statistic_id": sensor_statistic_id(contract_id),
                    "statistic_id": external_id,
                },
            )


async def _async_get_metadata(hass: HomeAssistant, statistic_id: str) -> StatisticMetaData | None:
    """Return the metadata of a statistic (statistics rows do not hold the unit), or None if it does not exist."""
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    return metadata[statistic_id][1] if statistic_id in metadata else None


class SensorStatisticsRepairFlow(ConfirmRepairFlow):
    """Repair flow deleting the statistics imported under the water_consumption sensor by versions 0.1.4-0.1.5."""

    def __init__(self, contract_id: str) -> None:
        super().__init__()
        self._contract_id = contract_id

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            await _delete_sensor_statistics(self.hass, self._contract_id)
            return self.async_create_entry(data={})
        return await super().async_step_confirm(user_input)


async def _delete_sensor_statistics(hass: HomeAssistant, contract_id: str) -> None:
    """Delete the statistics of the former water_consumption sensor, and the sensor itself from the registry."""
    statistic_id = sensor_statistic_id(contract_id)
    get_instance(hass).async_clear_statistics([statistic_id])

    entity_registry = er.async_get(hass)
    if entity_registry.async_get(statistic_id) is not None:
        entity_registry.async_remove(statistic_id)

    _LOGGER.info("Deleted the statistics of %s, the history lives in %s", statistic_id, statistic_id_for(contract_id))


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    """Create the repair fix flow."""
    # issue_id format: statistics_on_sensor_<contract_id>
    contract_id = issue_id.removeprefix(f"{ISSUE_STATISTICS_ON_SENSOR}_")
    return SensorStatisticsRepairFlow(contract_id)
