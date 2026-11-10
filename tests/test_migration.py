"""Tests for the v1 -> v2 config entry migration (unique IDs now include the contract ID)."""
from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.agur import async_migrate_entry, migrate_unique_id_v1

from .conftest import CONTRACT_1, CONTRACT_2, ENTRY_ID, FakeAgurApi, make_entry

KEYS = ["last_index", "last_invoice", "balance"]


def _register_v1(entity_registry: er.EntityRegistry, entry, key: str, object_id: str) -> er.RegistryEntry:
    """Register a sensor the way v1 did: unique ID `{key}_{entry_id}`."""
    return entity_registry.async_get_or_create(
        "sensor",
        "agur",
        f"{key}_{entry.entry_id}",
        config_entry=entry,
        suggested_object_id=object_id,
    )


@pytest.mark.parametrize(
    ("unique_id", "entity_id", "contract_ids", "expected"),
    [
        # Entity ID carries the contract ID
        (f"balance_{ENTRY_ID}", f"sensor.agur_balance_{CONTRACT_2}", [CONTRACT_1, CONTRACT_2],
         f"balance_{ENTRY_ID}_{CONTRACT_2}"),
        (f"last_index_{ENTRY_ID}", f"sensor.agur_last_index_{CONTRACT_1}", [CONTRACT_1],
         f"last_index_{ENTRY_ID}_{CONTRACT_1}"),
        # Renamed entity, single contract: it can only be that one
        (f"balance_{ENTRY_ID}", "sensor.my_balance", [CONTRACT_1], f"balance_{ENTRY_ID}_{CONTRACT_1}"),
        # Renamed entity, several contracts: ambiguous, left untouched
        (f"balance_{ENTRY_ID}", "sensor.my_balance", [CONTRACT_1, CONTRACT_2], None),
        # Entity of a contract that is no longer selected, several contracts left: untouched
        (f"balance_{ENTRY_ID}", "sensor.agur_balance_999", [CONTRACT_1, CONTRACT_2], None),
        # Contract IDs that are suffixes of one another must not be confused
        (f"balance_{ENTRY_ID}", "sensor.agur_balance_11", ["1", "11"], f"balance_{ENTRY_ID}_11"),
        # Already migrated (v2 format)
        (f"balance_{ENTRY_ID}_{CONTRACT_1}", f"sensor.agur_balance_{CONTRACT_1}", [CONTRACT_1], None),
        # Not one of ours
        (f"unknown_{ENTRY_ID}", f"sensor.agur_unknown_{CONTRACT_1}", [CONTRACT_1], None),
        (f"balance_other-entry", f"sensor.agur_balance_{CONTRACT_1}", [CONTRACT_1], None),
        # No contract at all
        (f"balance_{ENTRY_ID}", f"sensor.agur_balance_{CONTRACT_1}", [], None),
    ],
)
def test_migrate_unique_id_v1(unique_id, entity_id, contract_ids, expected) -> None:
    assert migrate_unique_id_v1(unique_id, entity_id, ENTRY_ID, contract_ids) == expected


async def test_migrate_single_contract(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """Existing single-contract users keep their entities (same entity IDs, so same history)."""
    entry = make_entry(contract_ids=[CONTRACT_1], version=1)
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    originals = {
        key: _register_v1(entity_registry, entry, key, f"agur_{key}_{CONTRACT_1}") for key in KEYS
    }

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    for key, original in originals.items():
        migrated = entity_registry.async_get(original.entity_id)
        assert migrated.id == original.id
        assert migrated.unique_id == f"{key}_{ENTRY_ID}_{CONTRACT_1}"
        assert hass.states.get(original.entity_id) is not None
    # No duplicate entity was created
    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == len(KEYS)


async def test_migrate_two_contracts_as_in_issue_6(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """With two contracts, v1 only kept one entity per key, split across contracts (issue #6).

    After migration, each surviving entity is bound to the contract its entity ID names, and the
    missing ones are created with their expected entity IDs.
    """
    entry = make_entry(version=1)
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    survivors = {
        "last_invoice": CONTRACT_1,
        "balance": CONTRACT_1,
        "last_index": CONTRACT_2,
    }
    originals = {
        key: _register_v1(entity_registry, entry, key, f"agur_{key}_{contract}")
        for key, contract in survivors.items()
    }

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    for key, original in originals.items():
        migrated = entity_registry.async_get(original.entity_id)
        assert migrated.id == original.id
        assert migrated.unique_id == f"{key}_{ENTRY_ID}_{survivors[key]}"

    entity_ids = sorted(e.entity_id for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    assert entity_ids == sorted(f"sensor.agur_{key}_{c}" for key in KEYS for c in (CONTRACT_1, CONTRACT_2))
    for entity_id in entity_ids:
        assert hass.states.get(entity_id) is not None


async def test_migrate_leaves_ambiguous_entities_untouched(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = make_entry(version=1)
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    renamed = _register_v1(entity_registry, entry, "balance", "my_balance")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert entity_registry.async_get(renamed.entity_id).unique_id == f"balance_{ENTRY_ID}"
    # Both contracts still get their balance sensor
    assert hass.states.get(f"sensor.agur_balance_{CONTRACT_1}") is not None
    assert hass.states.get(f"sensor.agur_balance_{CONTRACT_2}") is not None


async def test_migrate_from_future_version_fails(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    entry = make_entry(version=3)
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.MIGRATION_ERROR


@pytest.mark.parametrize(("version", "expected"), [(2, True), (3, False)])
async def test_async_migrate_entry_guards(hass: HomeAssistant, version: int, expected: bool) -> None:
    """Home Assistant does not call the migration for these versions; the guards are defensive."""
    entry = make_entry(version=version)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is expected
    assert entry.version == version


async def test_migrate_leaves_legacy_consumption_sensor_alone(hass: HomeAssistant, agur_api: FakeAgurApi) -> None:
    """This fork has no water_consumption sensor any more: its legacy registry entry is neither migrated nor recreated."""
    entry = make_entry(contract_ids=[CONTRACT_1], version=1)
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    legacy = _register_v1(entity_registry, entry, "water_consumption", f"agur_water_consumption_{CONTRACT_1}")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entity_registry.async_get(legacy.entity_id).unique_id == legacy.unique_id
    assert hass.states.get(f"sensor.agur_water_consumption_{CONTRACT_1}") is None
