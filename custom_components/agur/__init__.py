import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, STARTUP_MESSAGE, CONF_USERNAME, CONF_PASSWORD, PLATFORMS, CONF_CONTRACT_IDS, \
    CONF_IMPORT_STATISTICS, CONF_PROVIDER, DEFAULT_PROVIDER
from .coordinator import AgurDataUpdateCoordinator

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
        import_statistics=import_statistics
    )
    await coordinator.async_config_entry_first_refresh()

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    hass.data[DOMAIN][config_entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Note: the upstream "statistics unit" repair (migration of agur:* statistics to a
    # sensor.* id) is intentionally not registered: statistics are kept as external
    # statistics `agur:water_consumption_<contract>` in m³ (see coordinator).

    config_entry.add_update_listener(async_reload_entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
