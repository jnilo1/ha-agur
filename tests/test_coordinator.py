"""Tests for the Agur data update coordinator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics, statistics_during_period
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
    statistic_id = f"agur:water_consumption_{contract_id}"
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


async def test_statistics_state_at_the_start_of_the_reimport_window(
    hass: HomeAssistant, agur_api: FakeAgurApi
) -> None:
    """The first reading re-imported is compared with the previous one, even if it is out of the window."""
    old = datetime(2026, 9, 1, tzinfo=TZ)
    agur_api.data_points[CONTRACT_1].append(
        AgurDataPoint({"valeurIndex": 900_000, "dateReleve": old.isoformat()})
    )
    entry = await _setup(hass, contract_ids=[CONTRACT_1])

    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    stats = await _statistics(hass, CONTRACT_1)
    assert [s["state"] for s in stats] == pytest.approx([0, 100, 0.1, 0.15, 0.12])


def _reading(day: int, index: float | None, volume: float | None = None) -> AgurDataPoint:
    return AgurDataPoint({
        "valeurIndex": index,
        "volumeConsoEnLitres": volume,
        "dateReleve": datetime(2026, 11, day, tzinfo=TZ).isoformat(),
    })


async def _refresh(hass: HomeAssistant, entry) -> None:
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def test_statistics_continue_after_meter_replacement(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """Only the readings of the new meter are imported (local choice), its index being the consumption since 0."""
    agur_api.data_points[CONTRACT_1][:0] = [_reading(11, 180), _reading(10, 30), _reading(9, None)]
    await _setup(hass, contract_ids=[CONTRACT_1])

    stats = await _statistics(hass, CONTRACT_1)
    assert [s["start"] for s in stats] == [datetime(2026, 11, d, tzinfo=TZ).timestamp() for d in (10, 11)]
    assert [s["sum"] for s in stats] == pytest.approx([0.03, 0.18])
    assert [s["state"] for s in stats] == pytest.approx([0.03, 0.15])


async def test_statistics_meter_replacement_uses_the_provider_volume(
    hass: HomeAssistant, agur_api: FakeAgurApi
) -> None:
    """The volumes sent by the provider count what the old meter used after its last reading."""
    agur_api.data_points[CONTRACT_1][:0] = [_reading(10, 30, volume=20), _reading(9, None, volume=45)]
    await _setup(hass, contract_ids=[CONTRACT_1])

    stats = await _statistics(hass, CONTRACT_1)
    assert len(stats) == 1
    assert stats[-1]["state"] == pytest.approx(0.065)
    assert stats[-1]["sum"] == pytest.approx(0.065)


async def test_statistics_small_index_decrease_is_not_a_replacement(
    hass: HomeAssistant, agur_api: FakeAgurApi
) -> None:
    """The provider corrects an index a bit down: it is not a new meter, the sum follows the index."""
    agur_api.data_points[CONTRACT_1].insert(0, _reading(9, 1_000_300))
    await _setup(hass, contract_ids=[CONTRACT_1])

    stats = await _statistics(hass, CONTRACT_1)
    assert stats[-1]["state"] == pytest.approx(-0.07)
    assert stats[-1]["sum"] == pytest.approx(1000.3)


async def test_statistics_continue_after_meter_replacement_with_stored_statistics(
    hass: HomeAssistant, agur_api: FakeAgurApi
) -> None:
    """The meter is replaced after statistics were imported, then the old readings leave the API history."""
    entry = await _setup(hass, contract_ids=[CONTRACT_1])
    old_readings = agur_api.data_points[CONTRACT_1]

    # The new meter shows up while the old readings are still returned by the API
    agur_api.data_points[CONTRACT_1] = [_reading(10, 130), _reading(9, 30), *old_readings]
    await _refresh(hass, entry)
    stats = await _statistics(hass, CONTRACT_1)
    assert [s["sum"] for s in stats] == pytest.approx([1000, 1000.1, 1000.25, 1000.37, 1000.4, 1000.5])

    # Later, the API only returns readings of the new meter: the stored sums are kept and continued
    agur_api.data_points[CONTRACT_1] = [_reading(11, 200), _reading(10, 130), _reading(9, 30)]
    await _refresh(hass, entry)
    stats = await _statistics(hass, CONTRACT_1)
    assert [s["sum"] for s in stats] == pytest.approx([1000, 1000.1, 1000.25, 1000.37, 1000.4, 1000.5, 1000.57])

    # Refreshing again changes nothing
    await _refresh(hass, entry)
    assert await _statistics(hass, CONTRACT_1) == stats


async def test_statistics_continue_after_a_gap(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """No update for a while: the readings sent by the API are all newer than the stored statistics."""
    entry = await _setup(hass, contract_ids=[CONTRACT_1])

    # The new meter was installed in the meantime, the provider gives the volume used since the last reading
    agur_api.data_points[CONTRACT_1] = [_reading(20, 100), _reading(19, 40, volume=60)]
    await _refresh(hass, entry)
    stats = await _statistics(hass, CONTRACT_1)
    assert [s["sum"] for s in stats][-3:] == pytest.approx([1000.37, 1000.43, 1000.49])

    # Same without volume: the sum does not drop, what was used before the first reading is unknown
    agur_api.data_points[CONTRACT_1] = [_reading(28, 300), _reading(27, 250)]
    await _refresh(hass, entry)
    stats = await _statistics(hass, CONTRACT_1)
    assert [s["sum"] for s in stats][-3:] == pytest.approx([1000.49, 1000.49, 1000.54])


async def test_statistics_continue_existing_statistics(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """Statistics stored at other times than the readings are continued."""
    async_add_external_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": DOMAIN,
            "statistic_id": f"agur:water_consumption_{CONTRACT_1}",
            "unit_class": "volume",
            "unit_of_measurement": "m³",
        },
        [{"start": datetime(2026, 11, 8, 6, tzinfo=TZ), "state": 0, "sum": 5000}],
    )
    await async_wait_recording_done(hass)
    await _setup(hass, contract_ids=[CONTRACT_1])

    stats = await _statistics(hass, CONTRACT_1)
    assert [s["sum"] for s in stats] == pytest.approx([4999.63, 4999.73, 4999.88, 5000, 5000])


async def test_statistics_are_external_and_no_consumption_sensor(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """The consumption lives in an external statistic only: no sensor whose state the recorder would also compile."""
    await _setup(hass, contract_ids=[CONTRACT_1])

    assert hass.states.get(f"sensor.agur_water_consumption_{CONTRACT_1}") is None
    assert len(await _statistics(hass, CONTRACT_1)) == 4
    attributes = hass.states.get(f"sensor.agur_last_index_{CONTRACT_1}").attributes
    assert attributes["statistic_id"] == f"agur:water_consumption_{CONTRACT_1}"
