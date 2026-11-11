"""Tests for the price subentries and the water cost statistic."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.agur import price_periods_from
from custom_components.agur.const import (
    CONF_CONTRACT_ID,
    CONF_PRICE,
    CONF_START_DATE,
    DOMAIN,
    SUBENTRY_TYPE_PRICE,
)
from custom_components.agur.coordinator import PricePeriod, price_at

from .conftest import CONTRACT_1, CONTRACT_2, TZ, FakeAgurApi, make_entry

COST_ID = f"agur:water_cost_{CONTRACT_1}"


def price(contract_id: str, start: str, value: float) -> dict[str, Any]:
    return {
        "data": {CONF_CONTRACT_ID: contract_id, CONF_START_DATE: start, CONF_PRICE: value},
        "subentry_type": SUBENTRY_TYPE_PRICE,
        "title": f"{value} €/m³ from {start}",
        "unique_id": f"{contract_id}_{start}",
    }


async def _setup(hass: HomeAssistant, prices: list[dict[str, Any]], **kwargs):
    entry = make_entry(subentries_data=prices, **kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return entry


async def _refresh(hass: HomeAssistant, entry) -> None:
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def _costs(hass: HomeAssistant, statistic_id: str = COST_ID) -> list[tuple[datetime, float, float]]:
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
    return [
        (datetime.fromtimestamp(row["start"], TZ), round(row["state"], 4), round(row["sum"], 4))
        for row in result.get(statistic_id, [])
    ]


def day(d: int) -> datetime:
    return datetime(2026, 11, d, tzinfo=TZ)


def test_price_at() -> None:
    periods = [PricePeriod(date(2026, 1, 1), 4.23), PricePeriod(date(2025, 1, 1), 4.09)]
    assert price_at(periods, date(2024, 12, 31)) is None
    assert price_at(periods, date(2025, 1, 1)) == 4.09
    assert price_at(periods, date(2025, 12, 31)) == 4.09
    assert price_at(periods, date(2026, 6, 1)) == 4.23
    assert price_at([], date(2026, 6, 1)) is None


def test_price_periods_from_ignores_other_subentries() -> None:
    entry = make_entry(subentries_data=[
        price(CONTRACT_1, "2026-01-01", 4.23),
        price(CONTRACT_2, "2025-01-01", 5),
        {"data": {}, "subentry_type": "other", "title": "Other", "unique_id": None},
    ])
    assert price_periods_from(entry) == {
        CONTRACT_1: [PricePeriod(date(2026, 1, 1), 4.23)],
        CONTRACT_2: [PricePeriod(date(2025, 1, 1), 5.0)],
    }


async def test_cost_is_consumption_times_price_in_force(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    # Daily consumptions are 0, 0.1, 0.15 and 0.12 m³ from 11-05 to 11-08
    await _setup(hass, [price(CONTRACT_1, "2026-11-06", 2), price(CONTRACT_1, "2026-11-08", 3)],
                 contract_ids=[CONTRACT_1])

    # No row before the first price period
    assert await _costs(hass) == [(day(6), 0.2, 0.2), (day(7), 0.3, 0.5), (day(8), 0.36, 0.86)]

    metadata = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, {COST_ID}
    )
    assert metadata[0]["source"] == DOMAIN
    assert metadata[0]["statistics_unit_of_measurement"] == "€"
    assert metadata[0]["has_sum"] is True
    assert metadata[0]["mean_type"] is StatisticMeanType.NONE


async def test_no_price_no_cost(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass, [price(CONTRACT_2, "2026-01-01", 2)])

    assert await _costs(hass) == []
    assert len(await _costs(hass, f"agur:water_cost_{CONTRACT_2}")) == 4


async def test_no_cost_without_statistics_import(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass, [price(CONTRACT_1, "2026-01-01", 2)], contract_ids=[CONTRACT_1], import_statistics=False)

    assert await _costs(hass) == []


async def test_late_price_rewrites_history(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = await _setup(hass, [price(CONTRACT_1, "2026-01-01", 2)], contract_ids=[CONTRACT_1])

    # A price is entered late: it applies back to its start date, beyond the re-import window of the consumption
    hass.data[DOMAIN][entry.entry_id].price_periods[CONTRACT_1].append(PricePeriod(date(2026, 11, 7), 4))
    await _refresh(hass, entry)

    assert await _costs(hass) == [
        (day(5), 0, 0), (day(6), 0.2, 0.2), (day(7), 0.6, 0.8), (day(8), 0.48, 1.28)
    ]


async def test_unchanged_costs_are_not_written_again(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = await _setup(hass, [price(CONTRACT_1, "2026-01-01", 2)], contract_ids=[CONTRACT_1])

    with patch(
        "custom_components.agur.coordinator.async_add_external_statistics", wraps=async_add_external_statistics
    ) as add:
        await _refresh(hass, entry)
    # Only the consumption is imported again
    assert [call.args[1]["statistic_id"] for call in add.call_args_list] == [f"agur:water_consumption_{CONTRACT_1}"]


async def test_cost_continues_rows_older_than_the_history(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    # A row from before the history sent by the API (e.g. imported long ago) is kept, and the sums continue it
    async_add_external_statistics(hass, {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": None,
        "source": DOMAIN,
        "statistic_id": COST_ID,
        "unit_of_measurement": "€",
        "unit_class": None,
    }, [{"start": datetime(2026, 10, 1, tzinfo=TZ), "state": 1.5, "sum": 10}])
    await async_wait_recording_done(hass)

    await _setup(hass, [price(CONTRACT_1, "2026-01-01", 2)], contract_ids=[CONTRACT_1])

    assert await _costs(hass) == [
        (datetime(2026, 10, 1, tzinfo=TZ), 1.5, 10),
        (day(5), 0, 10), (day(6), 0.2, 10.2), (day(7), 0.3, 10.5), (day(8), 0.24, 10.74),
    ]


@pytest.mark.parametrize(
    ("periods", "expected"),
    [
        # The price period was removed: no cost at all
        ([], []),
        # It now starts later: the readings before have no cost any more
        ([PricePeriod(date(2026, 11, 7), 2)], [(day(7), 0.3, 0.3), (day(8), 0.24, 0.54)]),
    ],
)
async def test_readings_without_price_any_more_rebuild_the_cost(
        hass: HomeAssistant, agur_api: FakeAgurApi, periods: list[PricePeriod], expected: list
) -> None:
    entry = await _setup(hass, [price(CONTRACT_1, "2026-01-01", 2)], contract_ids=[CONTRACT_1])
    assert len(await _costs(hass)) == 4

    hass.data[DOMAIN][entry.entry_id].price_periods[CONTRACT_1] = periods
    await _refresh(hass, entry)

    assert await _costs(hass) == expected


async def test_adding_a_price_subentry_reloads_and_imports_the_cost(
        hass: HomeAssistant, agur_api: FakeAgurApi
) -> None:
    entry = await _setup(hass, [], contract_ids=[CONTRACT_1])
    assert await _costs(hass) == []

    result = await _start_price_flow(hass, entry)
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START_DATE: "2026-01-01", CONF_PRICE: 2}
    )
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    assert len(await _costs(hass)) == 4


async def _start_price_flow(hass: HomeAssistant, entry, source: str = config_entries.SOURCE_USER, **context):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_PRICE), context={"source": source, **context}
    )


async def test_price_flow_single_contract(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = await _setup(hass, [], contract_ids=[CONTRACT_1])

    result = await _start_price_flow(hass, entry)
    assert result["type"] is FlowResultType.FORM
    # No contract to choose
    assert set(result["data_schema"].schema) == {CONF_START_DATE, CONF_PRICE}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START_DATE: "2026-01-01", CONF_PRICE: 4.23}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(iter(entry.subentries.values()))
    assert subentry.title == "4.23 €/m³ from 2026-01-01"
    assert subentry.unique_id == f"{CONTRACT_1}_2026-01-01"
    assert dict(subentry.data) == {CONF_CONTRACT_ID: CONTRACT_1, CONF_START_DATE: "2026-01-01", CONF_PRICE: 4.23}


async def test_price_flow_several_contracts(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    hass.config.language = "fr"
    entry = await _setup(hass, [])

    result = await _start_price_flow(hass, entry)
    assert set(result["data_schema"].schema) == {CONF_CONTRACT_ID, CONF_START_DATE, CONF_PRICE}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_CONTRACT_ID: CONTRACT_2, CONF_START_DATE: "2026-01-01", CONF_PRICE: 4.23}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(iter(entry.subentries.values()))
    assert subentry.title == f"{CONTRACT_2} · 4,23 €/m³ depuis le 2026-01-01"
    assert subentry.data[CONF_CONTRACT_ID] == CONTRACT_2


async def test_price_flow_refuses_a_second_price_on_the_same_date(
        hass: HomeAssistant, agur_api: FakeAgurApi
) -> None:
    entry = await _setup(hass, [price(CONTRACT_1, "2026-01-01", 4.23)])

    result = await _start_price_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_CONTRACT_ID: CONTRACT_1, CONF_START_DATE: "2026-01-01", CONF_PRICE: 5}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_START_DATE: "duplicate_start_date"}

    # Same date on the other contract is fine
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_CONTRACT_ID: CONTRACT_2, CONF_START_DATE: "2026-01-01", CONF_PRICE: 5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_price_flow_reconfigure(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = await _setup(hass, [price(CONTRACT_1, "2026-01-01", 4.23), price(CONTRACT_1, "2025-01-01", 4.09)],
                         contract_ids=[CONTRACT_1])
    subentry = next(s for s in entry.subentries.values() if s.data[CONF_START_DATE] == "2026-01-01")

    result = await _start_price_flow(
        hass, entry, config_entries.SOURCE_RECONFIGURE, subentry_id=subentry.subentry_id
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    # Moving it onto the other period is refused, keeping its own date is fine
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START_DATE: "2025-01-01", CONF_PRICE: 4.5}
    )
    assert result["errors"] == {CONF_START_DATE: "duplicate_start_date"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START_DATE: "2026-01-01", CONF_PRICE: 4.5}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    subentry = entry.subentries[subentry.subentry_id]
    assert subentry.data[CONF_PRICE] == 4.5
    assert subentry.title == "4.5 €/m³ from 2026-01-01"


async def test_price_flow_without_contracts(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = make_entry(contract_ids=[])
    entry.add_to_hass(hass)

    result = await _start_price_flow(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_contracts"


async def test_cost_only_covers_the_current_meter(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """Local choice: the cost follows the consumption statistic, which only holds the readings of the new meter."""
    from custom_components.agur.agur_client import AgurDataPoint

    agur_api.data_points[CONTRACT_1][:0] = [
        AgurDataPoint({"valeurIndex": index, "dateReleve": day(d).isoformat()})
        for d, index in ((11, 180), (10, 30), (9, None))
    ]
    await _setup(hass, [price(CONTRACT_1, "2026-01-01", 2.0)], contract_ids=[CONTRACT_1])

    assert await _costs(hass) == [(day(10), 0.06, 0.06), (day(11), 0.3, 0.36)]
