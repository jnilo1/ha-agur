from __future__ import annotations

import logging
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, STARTUP_MESSAGE, CONF_USERNAME, CONF_PASSWORD, PLATFORMS, CONF_CONTRACT_IDS, \
    CONF_IMPORT_STATISTICS, CONF_PROVIDER, DEFAULT_PROVIDER, VERSION, SUBENTRY_TYPE_PRICE, CONF_CONTRACT_ID, \
    CONF_START_DATE, CONF_PRICE
from .coordinator import AgurDataUpdateCoordinator, PricePeriod
from .repairs import async_check_and_create_repair_issues, async_convert_legacy_statistics
from .sensor import SENSORS, build_unique_id

SENSOR_KEYS = {description.key for description in SENSORS}

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigEntry):
    """This integration can be setup only via the UI."""
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.debug(STARTUP_MESSAGE)

    provider: str = config_entry.data.get(CONF_PROVIDER, DEFAULT_PROVIDER)
    username: str = config_entry.data.get(CONF_USERNAME)
    password: str = config_entry.data.get(CONF_PASSWORD)
    contract_ids: list[str] = config_entry.options.get(CONF_CONTRACT_IDS)
    import_statistics: bool = config_entry.options.get(CONF_IMPORT_STATISTICS)

    coordinator = AgurDataUpdateCoordinator(
        hass=hass,
        provider=provider,
        username=username,
        password=password,
        contract_ids=contract_ids,
        import_statistics=import_statistics,
        price_periods=price_periods_from(config_entry),
    )
    if import_statistics:
        # Before the first import, which writes in m³ under the same statistic id
        await async_convert_legacy_statistics(hass, contract_ids)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][config_entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    if import_statistics:
        hass.async_create_task(
            async_check_and_create_repair_issues(hass, contract_ids)
        )

    config_entry.async_on_unload(config_entry.add_update_listener(async_reload_entry))
    return True


def price_periods_from(config_entry: ConfigEntry) -> dict[str, list[PricePeriod]]:
    """Return the price periods of each contract, from the price subentries."""
    periods: dict[str, list[PricePeriod]] = {}
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_PRICE:
            continue
        periods.setdefault(subentry.data[CONF_CONTRACT_ID], []).append(PricePeriod(
            start=date.fromisoformat(subentry.data[CONF_START_DATE]),
            price=float(subentry.data[CONF_PRICE]),
        ))
    return periods


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate an old config entry."""
    if config_entry.version > VERSION:
        # Downgrade from a future version: we cannot handle it
        return False

    if config_entry.version == 1:
        contract_ids: list[str] = config_entry.options.get(CONF_CONTRACT_IDS) or []

        @callback
        def _migrate(entity_entry: er.RegistryEntry) -> dict[str, Any] | None:
            new_unique_id = migrate_unique_id_v1(
                unique_id=entity_entry.unique_id,
                entity_id=entity_entry.entity_id,
                entry_id=config_entry.entry_id,
                contract_ids=contract_ids,
            )
            if new_unique_id is None:
                return None
            _LOGGER.debug(f"Migrating unique ID of {entity_entry.entity_id}: "
                          f"{entity_entry.unique_id} -> {new_unique_id}")
            return {"new_unique_id": new_unique_id}

        await er.async_migrate_entries(hass, config_entry.entry_id, _migrate)
        hass.config_entries.async_update_entry(config_entry, version=2)
        _LOGGER.info(f"Migrated Agur config entry {config_entry.entry_id} to version 2")

    return True


def migrate_unique_id_v1(unique_id: str, entity_id: str, entry_id: str, contract_ids: list[str]) -> str | None:
    """Compute the v2 unique ID of a sensor from its v1 one, or None if it cannot (or must not) be migrated.

    In v1, unique IDs were `{key}_{entry_id}`, which collided as soon as an account had several contracts.
    The contract is recovered from the entity ID (`sensor.agur_{key}_{contract_id}`). If the user renamed
    the entity, we can only guess when there is a single contract; otherwise the entity is left untouched.
    """
    suffix = f"_{entry_id}"
    if not unique_id.endswith(suffix):
        return None
    key = unique_id.removesuffix(suffix)
    if key not in SENSOR_KEYS:
        return None

    matching = [c for c in contract_ids if entity_id.endswith(f"_{c}")]
    if len(matching) == 1:
        contract_id = matching[0]
    elif not matching and len(contract_ids) == 1:
        contract_id = contract_ids[0]
    else:
        return None

    return build_unique_id(key, entry_id, contract_id)
