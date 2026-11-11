"""Tests for the repairs bringing the statistics of earlier versions in line with the current ones."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.recorder import get_instance
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.agur.const import DOMAIN
from custom_components.agur.repairs import (
    ISSUE_STATISTICS_ON_SENSOR,
    SensorStatisticsRepairFlow,
    async_create_fix_flow,
    sensor_statistic_id,
)

from .conftest import CONTRACT_1, CONTRACT_2, ENTRY_ID, FakeAgurApi, make_entry

OLD_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
EXTERNAL_1 = f"{DOMAIN}:water_consumption_{CONTRACT_1}"
SENSOR_1 = sensor_statistic_id(CONTRACT_1)


def _add_legacy_external_statistics(hass: HomeAssistant, contract_id: str, unit: str | None) -> None:
    """Statistics as imported by versions up to 0.1.3: external, in liters (or without unit)."""
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


def _add_sensor_statistics(hass: HomeAssistant, contract_id: str) -> None:
    """Statistics as imported by versions 0.1.4 and 0.1.5: under the water_consumption sensor, in m³."""
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": sensor_statistic_id(contract_id),
            "unit_class": "volume",
            "unit_of_measurement": "m³",
        },
        [{"start": OLD_START + timedelta(hours=i), "state": 0.1 * i, "sum": 1.0 + 0.1 * i} for i in range(3)],
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


async def _metadata(hass: HomeAssistant, statistic_id: str) -> dict | None:
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    return metadata[statistic_id][1] if statistic_id in metadata else None


async def _setup(hass: HomeAssistant, **kwargs) -> None:
    entry = make_entry(**kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    # The check runs as a task once the entry is set up, and queues recorder jobs
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


def _issue_id(contract_id: str) -> str:
    return f"{ISSUE_STATISTICS_ON_SENSOR}_{contract_id}"


@pytest.mark.parametrize("unit", ["L", None])
async def test_legacy_external_statistics_are_converted_to_cubic_meters(
    hass: HomeAssistant, agur_api: FakeAgurApi, unit
) -> None:
    """Liters (or no unit, which meant liters) are converted in place by the recorder, values included."""
    _add_legacy_external_statistics(hass, CONTRACT_1, unit)
    await async_wait_recording_done(hass)

    await _setup(hass, contract_ids=[CONTRACT_1])

    metadata = await _metadata(hass, EXTERNAL_1)
    assert metadata["unit_of_measurement"] == "m³"
    assert metadata["unit_class"] == "volume"
    legacy = [s for s in await _statistics(hass, EXTERNAL_1) if s["start"] < datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()]
    assert [s["sum"] for s in legacy] == pytest.approx([1.0, 1.1, 1.2])
    assert [s["state"] for s in legacy] == pytest.approx([0.0, 0.1, 0.2])
    # The new readings follow, imported by the coordinator
    assert len(await _statistics(hass, EXTERNAL_1)) == 3 + 4
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(CONTRACT_1)) is None


async def test_external_statistics_in_cubic_meters_are_left_alone(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    _add_legacy_external_statistics(hass, CONTRACT_1, "m³")
    await async_wait_recording_done(hass)

    await _setup(hass, contract_ids=[CONTRACT_1])

    legacy = [s for s in await _statistics(hass, EXTERNAL_1) if s["start"] < datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()]
    assert [s["sum"] for s in legacy] == pytest.approx([1_000.0, 1_100.0, 1_200.0])


async def test_issue_created_for_statistics_on_the_sensor(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    _add_sensor_statistics(hass, CONTRACT_1)
    await async_wait_recording_done(hass)

    await _setup(hass)

    issue_registry = ir.async_get(hass)
    issue = issue_registry.async_get_issue(DOMAIN, _issue_id(CONTRACT_1))
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders == {
        "contract_id": CONTRACT_1,
        "sensor_statistic_id": SENSOR_1,
        "statistic_id": EXTERNAL_1,
    }
    # The other contract has no statistics on its sensor
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(CONTRACT_2)) is None
    # Nothing was deleted yet, and the history is already available in the external statistic
    assert len(await _statistics(hass, SENSOR_1)) == 3
    assert len(await _statistics(hass, EXTERNAL_1)) == 4


async def test_no_check_without_statistics_import(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    _add_sensor_statistics(hass, CONTRACT_1)
    await async_wait_recording_done(hass)

    await _setup(hass, import_statistics=False)

    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(CONTRACT_1)) is None


async def test_fix_flow_deletes_the_sensor_statistics(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    _add_sensor_statistics(hass, CONTRACT_1)
    await async_wait_recording_done(hass)
    entry = make_entry(contract_ids=[CONTRACT_1], version=1)
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"water_consumption_{ENTRY_ID}", suggested_object_id=f"agur_water_consumption_{CONTRACT_1}",
        config_entry=entry,
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, "repairs", {})

    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": _issue_id(CONTRACT_1)})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    assert await _statistics(hass, SENSOR_1) == []
    assert await _metadata(hass, SENSOR_1) is None
    assert entity_registry.async_get(SENSOR_1) is None
    assert len(await _statistics(hass, EXTERNAL_1)) == 4
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(CONTRACT_1)) is None


async def test_create_fix_flow_parses_contract_id(hass: HomeAssistant) -> None:
    flow = await async_create_fix_flow(hass, _issue_id(CONTRACT_2), None)
    assert isinstance(flow, SensorStatisticsRepairFlow)
    assert flow._contract_id == CONTRACT_2
