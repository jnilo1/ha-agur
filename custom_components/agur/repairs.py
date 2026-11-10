"""Repairs for the Agur integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import partial

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.unit_conversion import VolumeConverter

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_STATISTICS_UNIT_L = "statistics_unit_liters"


async def async_check_and_create_repair_issues(hass: HomeAssistant, contract_ids: list[str]) -> None:
    """Check each contract's legacy statistics and raise a repair issue if still in L.

    Older versions imported external statistics (`agur:water_consumption_<contract>`) in liters. If they
    are already in m³ (e.g. converted by the user from the statistics developer tools), they are moved to
    the sensor right away: nothing changes in the values, so there is nothing to confirm.
    """
    for contract_id in contract_ids:
        old_statistic_id = f"{DOMAIN}:water_consumption_{contract_id}"
        metadata = await _async_get_metadata(hass, old_statistic_id)
        if metadata is None:
            continue

        unit = metadata["unit_of_measurement"]
        if unit == UnitOfVolume.CUBIC_METERS:
            _LOGGER.info(f"Statistics {old_statistic_id} are already in m³: moving them to the sensor")
            await _migrate_statistics(hass, contract_id)
            continue

        _LOGGER.info(f"Statistics {old_statistic_id} -> Unit found: {unit}. Need a repair")
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{ISSUE_STATISTICS_UNIT_L}_{contract_id}",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_STATISTICS_UNIT_L,
            translation_placeholders={"contract_id": contract_id},
        )


async def _async_get_metadata(hass: HomeAssistant, statistic_id: str) -> StatisticMetaData | None:
    """Return the metadata of a statistic (statistics rows do not hold the unit), or None if it does not exist."""
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    return metadata[statistic_id][1] if statistic_id in metadata else None


class StatisticsUnitRepairFlow(ConfirmRepairFlow):
    """Repair flow to convert water statistics from L to m³."""

    def __init__(self, contract_id: str) -> None:
        super().__init__()
        self._contract_id = contract_id

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            await _migrate_statistics(self.hass, self._contract_id)
            return self.async_create_entry(data={})
        return await super().async_step_confirm(user_input)


async def _migrate_statistics(hass: HomeAssistant, contract_id: str) -> None:
    """Read all statistics from old external ID and rewrite them in m³ to new sensor ID."""
    old_statistic_id = f"{DOMAIN}:water_consumption_{contract_id}"
    new_statistic_id = f"sensor.{DOMAIN}_water_consumption_{contract_id}"
    recorder = get_instance(hass)

    all_stats = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(1970, 1, 1),
        None,
        {old_statistic_id},
        "hour",
        None,
        {"sum", "state"},
    )

    rows = all_stats.get(old_statistic_id, [])
    if not rows:
        return

    # Legacy statistics without unit were in liters
    unit = (await _async_get_metadata(hass, old_statistic_id))["unit_of_measurement"] or UnitOfVolume.LITERS
    convert = VolumeConverter.converter_factory_allow_none(unit, UnitOfVolume.CUBIC_METERS)
    converted = [
        StatisticData(
            start=datetime.fromtimestamp(row["start"], tz=timezone.utc) if isinstance(row["start"], (int, float)) else row["start"],
            state=convert(row.get("state")),
            sum=convert(row.get("sum")),
        )
        for row in rows
    ]

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"Water consumption {contract_id}",
        source="recorder",
        statistic_id=new_statistic_id,
        unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        unit_class=VolumeConverter.UNIT_CLASS,
    )
    async_import_statistics(hass, metadata, converted)

    # Delete the old external statistics
    recorder.async_clear_statistics([old_statistic_id])

    _LOGGER.info("Migrated statistics for contract %s from L to m³ (external → sensor)", contract_id)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    """Create the repair fix flow."""
    # issue_id format: statistics_unit_liters_<contract_id>
    contract_id = issue_id.removeprefix(f"{ISSUE_STATISTICS_UNIT_L}_")
    return StatisticsUnitRepairFlow(contract_id)
