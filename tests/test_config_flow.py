"""Tests for the Agur config and options flows."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests import HTTPError

from custom_components.agur.const import (
    CONF_CONTRACT_IDS,
    CONF_IMPORT_STATISTICS,
    CONF_PASSWORD,
    CONF_PROVIDER,
    CONF_USERNAME,
    DOMAIN,
    PROVIDERS,
    VERSION,
)

from .conftest import CONTRACT_1, CONTRACT_2, PASSWORD, USERNAME, FakeAgurApi, make_entry

USER_INPUT = {CONF_PROVIDER: "eauparis", CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


async def _start_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_full_flow(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "configuration"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTRACT_IDS: [CONTRACT_2], CONF_IMPORT_STATISTICS: False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"] == USER_INPUT
    assert result["options"] == {CONF_CONTRACT_IDS: [CONTRACT_2], CONF_IMPORT_STATISTICS: False}
    entry = result["result"]
    assert entry.unique_id == USERNAME
    assert entry.version == VERSION

    # The flow talks to the selected provider, with a single HTTP session (affinity cookie)
    provider = PROVIDERS["eauparis"]
    flow_kwargs = agur_api.client_kwargs[:3]
    assert {k["host"] for k in flow_kwargs} == {provider["host"]}
    assert len({id(k["http_session"]) for k in flow_kwargs}) == 1


async def test_configuration_lists_all_contracts(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    schema = result["data_schema"].schema
    contracts_key = next(k for k in schema if k == CONF_CONTRACT_IDS)
    assert contracts_key.default() == [CONTRACT_1, CONTRACT_2]
    assert set(schema[contracts_key].options) == {CONTRACT_1, CONTRACT_2}


async def test_invalid_credentials(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    agur_api.errors["login"] = Exception("401")
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "auth"}

    # The user can retry
    del agur_api.errors["login"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "configuration"


async def test_already_configured(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    make_entry().add_to_hass(hass)
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_existing_unique_id_updates_entry(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """An entry with the same unique ID but different data is updated in place."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        version=VERSION,
        data={CONF_PROVIDER: "ael", CONF_USERNAME: "legacy", CONF_PASSWORD: "old"},
        options={CONF_CONTRACT_IDS: [CONTRACT_1], CONF_IMPORT_STATISTICS: True},
    )
    entry.add_to_hass(hass)

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTRACT_IDS: [CONTRACT_1, CONTRACT_2], CONF_IMPORT_STATISTICS: True}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == USER_INPUT
    assert entry.options[CONF_CONTRACT_IDS] == [CONTRACT_1, CONTRACT_2]


async def test_create_entry_failure(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    with patch(
        "custom_components.agur.config_flow.AgurConfigFlow._async_create_entry",
        side_effect=Exception("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CONTRACT_IDS: [CONTRACT_1], CONF_IMPORT_STATISTICS: True}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "configuration"
    assert result["errors"] == {"base": "config_entry"}


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_flow(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = make_entry(contract_ids=[CONTRACT_1], import_statistics=False)
    await _setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {}
    schema = result["data_schema"].schema
    contracts_key = next(k for k in schema if k == CONF_CONTRACT_IDS)
    statistics_key = next(k for k in schema if k == CONF_IMPORT_STATISTICS)
    assert contracts_key.default() == [CONTRACT_1]
    assert statistics_key.default() is False
    assert set(schema[contracts_key].options) == {CONTRACT_1, CONTRACT_2}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONTRACT_IDS: [CONTRACT_1, CONTRACT_2], CONF_IMPORT_STATISTICS: True}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_CONTRACT_IDS: [CONTRACT_1, CONTRACT_2], CONF_IMPORT_STATISTICS: True}
    # The entry was reloaded with the new contract
    assert hass.states.get(f"sensor.agur_balance_{CONTRACT_2}") is not None


async def test_options_flow_auth_error(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = make_entry()
    await _setup_entry(hass, entry)
    agur_api.errors["login"] = Exception("401")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "auth"}


async def test_options_flow_contracts_error(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = make_entry()
    await _setup_entry(hass, entry)
    agur_api.errors["get_contracts"] = Exception("500")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "contracts"}


async def test_options_flow_unknown_error(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = make_entry()
    await _setup_entry(hass, entry)

    with patch("custom_components.agur.config_flow.get_agur_tokens", side_effect=RuntimeError("boom")):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "unknown"}


async def test_options_flow_missing_credentials(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    for missing in (CONF_USERNAME, CONF_PASSWORD):
        entry = make_entry(entry_id=f"entry-without-{missing}")
        await _setup_entry(hass, entry)
        hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k != missing}
        )

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["errors"] == {"base": "config"}


async def _setup_loaded_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_reauth_updates_the_password(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = await _setup_loaded_entry(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == USERNAME

    agur_api.calls.clear()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new-secret"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_PROVIDER: "ael", CONF_USERNAME: USERNAME, CONF_PASSWORD: "new-secret"}
    assert entry.state is config_entries.ConfigEntryState.LOADED
    # The password is checked by the flow, then the entry is reloaded once with it
    assert agur_api.calls.count("login") == 2


async def test_reauth_with_a_wrong_password_shows_an_error(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = await _setup_loaded_entry(hass)
    result = await entry.start_reauth_flow(hass)

    agur_api.errors["login"] = HTTPError(response=MagicMock(status_code=401))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "wrong"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "auth"}
    assert entry.data[CONF_PASSWORD] == PASSWORD

    del agur_api.errors["login"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new-secret"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"


async def test_rejected_credentials_start_a_reauth_flow(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """The ConfigEntryAuthFailed raised by the coordinator opens the reauth form."""
    agur_api.errors["login"] = HTTPError(response=MagicMock(status_code=401))
    entry = await _setup_loaded_entry(hass)

    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [(flow["context"]["source"], flow["step_id"]) for flow in flows] == [
        (config_entries.SOURCE_REAUTH, "reauth_confirm")
    ]

    del agur_api.errors["login"]
    agur_api.calls.clear()
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {CONF_PASSWORD: "new-secret"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert agur_api.calls.count("login") == 2
