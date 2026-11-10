"""Tests for the Agur sensors."""
from __future__ import annotations

from datetime import datetime

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.agur.sensor import build_unique_id

from .conftest import CONTRACT_1, CONTRACT_2, ENTRY_ID, TZ, FakeAgurApi, make_entry


async def _setup(hass: HomeAssistant, **kwargs) -> None:
    entry = make_entry(**kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def test_build_unique_id() -> None:
    assert build_unique_id("balance", ENTRY_ID, CONTRACT_1) == f"balance_{ENTRY_ID}_{CONTRACT_1}"


async def test_sensor_values(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass)

    # Latest index of the fixtures: base + 370 L
    assert float(hass.states.get(f"sensor.agur_last_index_{CONTRACT_1}").state) == 1_000_370
    assert float(hass.states.get(f"sensor.agur_last_index_{CONTRACT_2}").state) == 2_000_370
    assert float(hass.states.get(f"sensor.agur_last_invoice_{CONTRACT_1}").state) == 45.67
    assert float(hass.states.get(f"sensor.agur_balance_{CONTRACT_1}").state) == -12.5
    assert float(hass.states.get(f"sensor.agur_balance_{CONTRACT_2}").state) == 0.0


async def test_sensor_units_and_classes(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass)

    last_index = hass.states.get(f"sensor.agur_last_index_{CONTRACT_1}").attributes
    assert last_index["unit_of_measurement"] == "L"
    assert last_index["device_class"] == "water"
    assert last_index["state_class"] == "total_increasing"

    for key in ("last_invoice", "balance"):
        attributes = hass.states.get(f"sensor.agur_{key}_{CONTRACT_1}").attributes
        assert attributes["unit_of_measurement"] == "€"
        assert attributes["device_class"] == "monetary"
        assert attributes["state_class"] == "total"


async def test_sensor_attributes(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass)

    attributes = hass.states.get(f"sensor.agur_balance_{CONTRACT_2}").attributes
    assert attributes["contract_id"] == CONTRACT_2
    assert attributes["contract_address"] == f"{CONTRACT_2} rue de l'Eau, 64100 Bayonne"
    assert attributes["contract_owner"] == f"Owner {CONTRACT_2}"
    assert attributes["meter_serial_number"] == f"SN-{CONTRACT_2}"
    assert "date" not in attributes

    invoice = hass.states.get(f"sensor.agur_last_invoice_{CONTRACT_1}").attributes
    assert invoice["invoice_number"] == f"INV-{CONTRACT_1}-2"
    assert invoice["payment_date"] == datetime(2026, 10, 31, tzinfo=TZ)
    assert "date" in invoice

    assert "date" in hass.states.get(f"sensor.agur_last_index_{CONTRACT_1}").attributes


async def test_sensor_date_attribute_is_a_datetime(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass)

    attributes = hass.states.get(f"sensor.agur_last_index_{CONTRACT_1}").attributes
    assert attributes["date"] == datetime(2026, 11, 8, tzinfo=TZ)
    attributes = hass.states.get(f"sensor.agur_last_invoice_{CONTRACT_1}").attributes
    assert attributes["date"] == datetime(2026, 10, 1, tzinfo=TZ)


async def test_no_statistic_id_attribute_without_statistics(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    await _setup(hass, import_statistics=False)

    assert hass.states.get(f"sensor.agur_water_consumption_{CONTRACT_1}") is None
    assert "statistic_id" not in hass.states.get(f"sensor.agur_last_index_{CONTRACT_1}").attributes
    entity_registry = er.async_get(hass)
    assert len(er.async_entries_for_config_entry(entity_registry, ENTRY_ID)) == 6


async def test_sensors_without_data(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """A contract with no reading nor invoice yet still gets its sensors, with an unknown state."""
    agur_api.data_points[CONTRACT_1] = []
    agur_api.invoices[CONTRACT_1] = []
    await _setup(hass, contract_ids=[CONTRACT_1])

    for key in ("last_index", "last_invoice"):
        assert hass.states.get(f"sensor.agur_{key}_{CONTRACT_1}").state == "unknown"
    assert "invoice_number" not in hass.states.get(f"sensor.agur_last_invoice_{CONTRACT_1}").attributes


async def test_setup_fetches_the_data_once_per_contract(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """Adding the sensors must not refresh the coordinator again right after its first refresh."""
    await _setup(hass)

    for call in ("get_data", "get_invoices", "get_contract", "get_balance"):
        assert agur_api.calls.count(call) == 2, call
