"""Tests for setting up, unloading and reloading the Agur integration."""
from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from requests import HTTPError

from custom_components.agur.const import CONF_CONTRACT_IDS, CONF_IMPORT_STATISTICS, DOMAIN

from .conftest import CONTRACT_1, CONTRACT_2, ENTRY_ID, FakeAgurApi, make_entry

KEYS = ["last_index", "water_consumption", "last_invoice", "balance"]


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_two_contracts_create_all_entities(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """Regression test for issue #6: every contract of an account gets its own full set of sensors."""
    entry = make_entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    entity_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert sorted(e.unique_id for e in entries) == sorted(
        f"{key}_{ENTRY_ID}_{contract}" for key in KEYS for contract in (CONTRACT_1, CONTRACT_2)
    )
    for contract in (CONTRACT_1, CONTRACT_2):
        for key in KEYS:
            state = hass.states.get(f"sensor.agur_{key}_{contract}")
            assert state is not None, f"sensor.agur_{key}_{contract} is missing"
            assert state.attributes["contract_id"] == contract

    # One device per meter, each holding the sensors of its own contract
    device_registry = dr.async_get(hass)
    for contract in (CONTRACT_1, CONTRACT_2):
        device = device_registry.async_get_device_by_identifier((DOMAIN, f"meter-{contract}"), entry.entry_id)
        assert device is not None
        assert device.serial_number == f"SN-{contract}"
        device_entities = er.async_entries_for_device(entity_registry, device.id)
        assert sorted(e.entity_id for e in device_entities) == sorted(
            f"sensor.agur_{key}_{contract}" for key in KEYS
        )


async def test_unload(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = make_entry()
    await _setup(hass, entry)
    assert entry.entry_id in hass.data[DOMAIN]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_options_update_reloads_once(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """Changing options reloads the entry cleanly, without stacking update listeners (issue #6)."""
    entry = make_entry(contract_ids=[CONTRACT_1])
    await _setup(hass, entry)
    assert hass.states.get(f"sensor.agur_balance_{CONTRACT_2}") is None

    for contract_ids in ([CONTRACT_1, CONTRACT_2], [CONTRACT_2], [CONTRACT_1, CONTRACT_2]):
        agur_api.calls.clear()
        hass.config_entries.async_update_entry(
            entry, options={CONF_CONTRACT_IDS: contract_ids, CONF_IMPORT_STATISTICS: True}
        )
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        # A single reload means a single login
        assert agur_api.calls.count("init") == 1
        assert agur_api.calls.count("login") == 1
        assert len(entry.update_listeners) == 1

    assert hass.states.get(f"sensor.agur_balance_{CONTRACT_2}") is not None


async def test_setup_retries_when_api_fails(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    agur_api.errors["get_data"] = ConnectionError("boom")
    entry = make_entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_on_non_auth_http_error(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    agur_api.errors["get_balance"] = HTTPError(response=MagicMock(status_code=500))
    entry = make_entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_fails_on_invalid_credentials(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    agur_api.errors["get_contract"] = HTTPError(response=MagicMock(status_code=401))
    entry = make_entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
