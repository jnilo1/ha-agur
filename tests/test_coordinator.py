"""Tests for the Agur data update coordinator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.agur.agur_client import AgurDataPoint
from custom_components.agur.const import DOMAIN

from .conftest import CONTRACT_1, CONTRACT_2, TZ, FakeAgurApi, make_entry


async def _setup(hass: HomeAssistant, **kwargs):
    entry = make_entry(**kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return entry


async def _statistics(hass: HomeAssistant, contract_id: str) -> list[dict]:
    statistic_id = f"sensor.agur_water_consumption_{contract_id}"
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


async def test_coordinator_data(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    data = coordinator.data[CONTRACT_1]
    assert data.contract.id == CONTRACT_1
    assert data.last_index_value == 1_000_370
    assert data.last_index_date == datetime(2026, 11, 8, tzinfo=TZ)
    assert data.last_invoice_value == 45.67
    assert data.last_invoice_date == datetime(2026, 10, 1, tzinfo=TZ)
    assert data.last_balance_value == -12.5
    assert set(coordinator.data) == {CONTRACT_1, CONTRACT_2}


async def test_coordinator_data_empty(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    agur_api.data_points[CONTRACT_1] = []
    agur_api.invoices[CONTRACT_1] = []
    entry = await _setup(hass, contract_ids=[CONTRACT_1])
    data = hass.data[DOMAIN][entry.entry_id].data[CONTRACT_1]

    assert data.last_index is None
    assert data.last_index_value is None
    assert data.last_index_date is None
    assert data.last_invoice is None
    assert data.last_invoice_value is None
    assert data.last_invoice_date is None


async def test_tokens_are_reused_until_expired(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = await _setup(hass, contract_ids=[CONTRACT_1])
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert agur_api.calls.count("init") == 1

    agur_api.calls.clear()
    await coordinator.async_refresh()
    assert "init" not in agur_api.calls
    assert "login" not in agur_api.calls
    assert coordinator.last_update_success

    # Session token expired: both tokens are renewed
    coordinator.expiration_date = datetime.now().astimezone() - timedelta(minutes=1)
    agur_api.calls.clear()
    await coordinator.async_refresh()
    assert agur_api.calls.count("init") == 1
    assert agur_api.calls.count("login") == 1
    assert coordinator.last_update_success


async def test_all_clients_share_one_http_session(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = await _setup(hass, contract_ids=[CONTRACT_1])
    coordinator = hass.data[DOMAIN][entry.entry_id]

    sessions = {id(kwargs["http_session"]) for kwargs in agur_api.client_kwargs}
    assert sessions == {id(coordinator.http_session)}


async def test_statistics_are_imported_per_contract(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass)

    for contract_id, base in ((CONTRACT_1, 1_000_000), (CONTRACT_2, 2_000_000)):
        stats = await _statistics(hass, contract_id)
        assert [s["start"] for s in stats] == [
            datetime(2026, 11, 5 + i, tzinfo=TZ).timestamp() for i in range(4)
        ]
        # Cumulative sum, in m³
        assert [s["sum"] for s in stats] == pytest.approx(
            [(base + delta) / 1000 for delta in (0, 100, 250, 370)]
        )


@pytest.mark.xfail(
    strict=True,
    reason="Known bug, fixed in a follow-up PR: `state` indexes the original list while iterating the reversed one",
)
async def test_statistics_state_is_the_daily_consumption(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass, contract_ids=[CONTRACT_1])

    stats = await _statistics(hass, CONTRACT_1)
    assert [s["state"] for s in stats] == pytest.approx([0, 0.1, 0.15, 0.12])


async def test_statistics_skip_readings_without_index(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """The day a meter is replaced, the API returns a reading without index (issue #3)."""
    agur_api.data_points[CONTRACT_1].insert(
        0, AgurDataPoint({"valeurIndex": None, "dateReleve": "2026-11-09T00:00:00+01:00"})
    )
    entry = await _setup(hass, contract_ids=[CONTRACT_1])

    assert hass.data[DOMAIN][entry.entry_id].last_update_success
    assert len(await _statistics(hass, CONTRACT_1)) == 4


async def test_statistics_without_usable_readings(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    agur_api.data_points[CONTRACT_1] = [AgurDataPoint({"valeurIndex": None, "dateReleve": None})]
    entry = await _setup(hass, contract_ids=[CONTRACT_1])

    assert hass.data[DOMAIN][entry.entry_id].last_update_success
    assert await _statistics(hass, CONTRACT_1) == []


async def test_statistics_not_imported_when_disabled(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass, contract_ids=[CONTRACT_1], import_statistics=False)

    assert await _statistics(hass, CONTRACT_1) == []


async def test_statistics_only_reimport_last_30_days(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """Once statistics exist, only readings from the last 30 days (relative to the latest one) are re-imported."""
    old = datetime(2026, 9, 1, tzinfo=TZ)
    agur_api.data_points[CONTRACT_1].append(
        AgurDataPoint({"valeurIndex": 900_000, "dateReleve": old.isoformat()})
    )
    entry = await _setup(hass, contract_ids=[CONTRACT_1])
    stats = await _statistics(hass, CONTRACT_1)
    assert stats[0]["start"] == old.timestamp()
    assert stats[0]["sum"] == pytest.approx(900)

    # The provider now reports a corrected value for the old reading: it is out of the window, so ignored
    agur_api.data_points[CONTRACT_1][-1] = AgurDataPoint({"valeurIndex": 800_000, "dateReleve": old.isoformat()})
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    stats = await _statistics(hass, CONTRACT_1)
    assert stats[0]["start"] == old.timestamp()
    assert stats[0]["sum"] == pytest.approx(900)
