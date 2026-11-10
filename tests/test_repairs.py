"""Tests for the statistics unit (L -> m³) repair."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.agur.const import DOMAIN
from custom_components.agur.repairs import (
    ISSUE_STATISTICS_UNIT_L,
    StatisticsUnitRepairFlow,
    _migrate_statistics,
    async_create_fix_flow,
)

from .conftest import CONTRACT_1, CONTRACT_2, FakeAgurApi, make_entry

OLD_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _add_old_statistics(hass: HomeAssistant, contract_id: str, unit: str | None) -> None:
    """Statistics as imported by older versions: external, in liters."""
    async_add_external_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": f"Water consumption {contract_id}",
            "source": DOMAIN,
            "statistic_id": f"{DOMAIN}:water_consumption_{contract_id}",
            "unit_class": None,
            "unit_of_measurement": unit,
        },
        [
            {"start": OLD_START + timedelta(hours=i), "state": 100.0 * i, "sum": 1_000.0 + 100.0 * i}
            for i in range(3)
        ],
    )


async def _statistics(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return result.get(statistic_id, [])


async def _setup(hass: HomeAssistant, **kwargs) -> None:
    entry = make_entry(**kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


def _issue_id(contract_id: str) -> str:
    return f"{ISSUE_STATISTICS_UNIT_L}_{contract_id}"


@pytest.mark.parametrize("unit", ["L", None])
async def test_issue_created_for_statistics_in_liters(hass: HomeAssistant, agur_api: FakeAgurApi, unit) -> None:
    _add_old_statistics(hass, CONTRACT_1, unit)
    await async_wait_recording_done(hass)

    await _setup(hass)

    issue_registry = ir.async_get(hass)
    issue = issue_registry.async_get_issue(DOMAIN, _issue_id(CONTRACT_1))
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders == {"contract_id": CONTRACT_1}
    # The other contract has no legacy statistics
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(CONTRACT_2)) is None


async def test_no_check_without_statistics_import(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    _add_old_statistics(hass, CONTRACT_1, "L")
    await async_wait_recording_done(hass)

    await _setup(hass, import_statistics=False)

    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(CONTRACT_1)) is None


async def test_fix_flow_migrates_statistics(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    _add_old_statistics(hass, CONTRACT_1, "L")
    await async_wait_recording_done(hass)
    await _setup(hass, contract_ids=[CONTRACT_1])
    assert await async_setup_component(hass, "repairs", {})

    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": _issue_id(CONTRACT_1)})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    # Old external statistics are gone, their history now lives on the sensor, in m³
    assert await _statistics(hass, f"{DOMAIN}:water_consumption_{CONTRACT_1}") == []
    migrated = [
        s for s in await _statistics(hass, f"sensor.{DOMAIN}_water_consumption_{CONTRACT_1}")
        if s["start"] < datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()
    ]
    assert [s["start"] for s in migrated] == [(OLD_START + timedelta(hours=i)).timestamp() for i in range(3)]
    assert [s["sum"] for s in migrated] == pytest.approx([1.0, 1.1, 1.2])
    assert [s["state"] for s in migrated] == pytest.approx([0.0, 0.1, 0.2])
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(CONTRACT_1)) is None


async def test_create_fix_flow_parses_contract_id(hass: HomeAssistant) -> None:
    flow = await async_create_fix_flow(hass, _issue_id(CONTRACT_2), None)
    assert isinstance(flow, StatisticsUnitRepairFlow)
    assert flow._contract_id == CONTRACT_2


async def test_migrate_without_statistics_is_a_noop(hass: HomeAssistant) -> None:
    await _migrate_statistics(hass, "unknown")
    await async_wait_recording_done(hass)

    assert await _statistics(hass, f"sensor.{DOMAIN}_water_consumption_unknown") == []
