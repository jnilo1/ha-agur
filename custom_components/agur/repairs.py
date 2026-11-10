"""Repairs for the Agur integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_statistics,
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
    """Check each contract's statistics unit and raise a repair issue if still in L."""
    recorder = get_instance(hass)
    for contract_id in contract_ids:
        # Check old external statistics ID
        old_statistic_id = f"{DOMAIN}:water_consumption_{contract_id}"
        last_stats = await recorder.async_add_executor_job(
            get_last_statistics, hass, 1, old_statistic_id, True, {"sum"}
        )
        if not last_stats:
            continue

        units = {stat.get("unit") for stat in last_stats[old_statistic_id]}

        if UnitOfVolume.LITERS in units or (len(units) == 1 and None in units):
            _LOGGER.info(f"Statistics {old_statistic_id} -> Units found: {units}. Need a repair")
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"{ISSUE_STATISTICS_UNIT_L}_{contract_id}",
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_STATISTICS_UNIT_L,
                translation_placeholders={"contract_id": contract_id},
            )


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
    """Read all L statistics from old external ID and rewrite as m³ to new sensor ID."""
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

    converted = [
        StatisticData(
            start=datetime.fromtimestamp(row["start"], tz=timezone.utc) if isinstance(row["start"], (int, float)) else row["start"],
            state=row["state"] / 1000 if row.get("state") is not None else None,
            sum=row["sum"] / 1000 if row.get("sum") is not None else None,
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
