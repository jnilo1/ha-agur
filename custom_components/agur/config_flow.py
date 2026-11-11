from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries, exceptions
from homeassistant.core import callback, HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation
from homeassistant.helpers.selector import (
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from requests import Session
from voluptuous import Schema

from .agur_client import AgurClient
from .const import DOMAIN, CONF_USERNAME, CONF_PASSWORD, VERSION, CONF_CONTRACT_IDS, CONF_IMPORT_STATISTICS, \
    CONF_PROVIDER, DEFAULT_PROVIDER, PROVIDERS, SUBENTRY_TYPE_PRICE, CONF_CONTRACT_ID, CONF_START_DATE, CONF_PRICE

_LOGGER: logging.Logger = logging.getLogger(__package__)
AUTH_TOKEN = "auth"
SESSION_TOKEN = "session"
HTTP_SESSION = "http"
USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PROVIDER, default=DEFAULT_PROVIDER): vol.In({k: v["name"] for k, v in PROVIDERS.items()}),
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.EMAIL,
                autocomplete="username"
            )
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD,
                autocomplete="current-password"
            )
        )
    }
)

async def get_agur_tokens(hass: HomeAssistant, username: str, password: str, provider: str = DEFAULT_PROVIDER) -> dict[str, str]:
    try:
        tokens = {}
        provider_config = PROVIDERS[provider]
        # One HTTP session for the whole flow: the API sets an affinity cookie on
        # generateToken that every later call must carry.
        http = Session()
        tokens[HTTP_SESSION] = http

        client = AgurClient(
            host=provider_config["host"],
            base_path=provider_config["base_path"],
            access_key=provider_config["access_key"],
            client_id=provider_config["client_id"],
            http_session=http,
        )
        response = await hass.async_add_executor_job(client.init)
        tokens[SESSION_TOKEN] = response["token"]

        client = AgurClient(
            session_token=tokens[SESSION_TOKEN],
            host=provider_config["host"],
            base_path=provider_config["base_path"],
            access_key=provider_config["access_key"],
            client_id=provider_config["client_id"],
            http_session=http,
        )
        response = await hass.async_add_executor_job(client.login, username, password)
        tokens[AUTH_TOKEN] = response["tokenAuthentique"]

        return tokens
    except Exception as ex:
        raise AuthError(ex)


async def get_agur_contract_options(hass: HomeAssistant, session_token: str, auth_token: str, provider: str = DEFAULT_PROVIDER, http_session: Session | None = None):
    try:
        provider_config = PROVIDERS[provider]
        client = AgurClient(
            session_token=session_token,
            auth_token=auth_token,
            host=provider_config["host"],
            base_path=provider_config["base_path"],
            access_key=provider_config["access_key"],
            client_id=provider_config["client_id"],
            http_session=http_session,
        )
        return {c.id: f"Contract {c.id} ({c.address})" for c in await hass.async_add_executor_job(client.get_contracts)}
    except Exception as ex:
        raise ContractError(ex)


class AgurConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Agur."""

    VERSION = VERSION

    def __init__(self):
        """Initialize."""
        self._provider: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._session_token: str | None = None
        self._auth_token: str | None = None
        self._http_session: Session | None = None
        self._available_contracts: list[dict[str, Any]] = []
        self._contract_ids: str | None = None
        self._import_statistics: bool | None = None
        self.configuration_schema: Schema | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle a login flow, initialized by the user."""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=USER_SCHEMA
            )

        self._provider = user_input.get(CONF_PROVIDER, DEFAULT_PROVIDER)
        self._username = user_input[CONF_USERNAME]
        self._password = user_input[CONF_PASSWORD]

        self._async_abort_entries_match({CONF_USERNAME: user_input[CONF_USERNAME]})

        return await self._async_agur_login(step_id="user")

    async def async_step_configuration(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the options selection flow."""
        available_contracts = await get_agur_contract_options(
            hass=self.hass,
            session_token=self._session_token,
            auth_token=self._auth_token,
            provider=self._provider,
            http_session=self._http_session,
        )
        default_contracts = list(available_contracts.keys())

        self.configuration_schema = vol.Schema(
            {
                vol.Required(
                    CONF_CONTRACT_IDS,
                    default=default_contracts
                ): config_validation.multi_select(available_contracts),
                vol.Optional(
                    CONF_IMPORT_STATISTICS,
                    default=True
                ): config_validation.boolean,
            }
        )

        if user_input is None:
            return self.async_show_form(
                step_id="configuration",
                data_schema=self.configuration_schema
            )

        self._contract_ids = user_input[CONF_CONTRACT_IDS]
        self._import_statistics = user_input[CONF_IMPORT_STATISTICS]

        return await self._async_agur_configuration(step_id="configuration")

    async def _async_agur_login(self, step_id: str) -> FlowResult:
        """Handle login with Agur."""
        errors = {}

        try:
            tokens = await get_agur_tokens(
                hass=self.hass,
                username=self._username,
                password=self._password,
                provider=self._provider
            )
            self._session_token = tokens[SESSION_TOKEN]
            self._auth_token = tokens[AUTH_TOKEN]
            self._http_session = tokens[HTTP_SESSION]

        except Exception as ex:
            _LOGGER.error(ex)
            errors = {"base": "auth"}

        if errors:
            return self.async_show_form(
                step_id=step_id,
                data_schema=USER_SCHEMA,
                errors=errors
            )

        return await self.async_step_configuration()

    async def _async_agur_configuration(self, step_id: str) -> FlowResult:
        """Handle Agur contract selection."""
        try:
            return await self._async_create_entry()

        except Exception as ex:
            _LOGGER.error(ex)
            return self.async_show_form(
                step_id=step_id,
                data_schema=self.configuration_schema,
                errors={"base": "config_entry"}
            )

    async def _async_create_entry(self) -> FlowResult:
        """Create the config entry."""
        config_data = {
            CONF_PROVIDER: self._provider,
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
        }
        config_option = {
            CONF_CONTRACT_IDS: self._contract_ids,
            CONF_IMPORT_STATISTICS: self._import_statistics
        }
        existing_entry = await self.async_set_unique_id(self._username)

        if existing_entry:
            self.hass.config_entries.async_update_entry(
                existing_entry, data=config_data, options=config_option
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(existing_entry.entry_id)
            )

            return self.async_abort(reason="reauth_successful")

        return self.async_create_entry(
            title=self._username, data=config_data, options=config_option
        )

    @staticmethod
    @callback
    def async_get_options_flow(
            config_entry: config_entries.ConfigEntry,
    ) -> AgurOptionFlow:
        """Get the options flow for this handler."""
        return AgurOptionFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
            cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return the subentries supported by this integration: the price periods of the contracts."""
        return {SUBENTRY_TYPE_PRICE: PriceSubentryFlow}


class AgurOptionFlow(config_entries.OptionsFlow):
    """Handle an option flow for Agur."""

    def __init__(
            self,
            config_entry: config_entries.ConfigEntry
    ) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
            self,
            user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""

        errors = {}
        available_contracts = []
        default_contracts = self._config_entry.options.get(CONF_CONTRACT_IDS, [])
        default_import_statistics = self._config_entry.options.get(CONF_IMPORT_STATISTICS, False)

        # We want to save here
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        try:
            provider = self._config_entry.data.get(CONF_PROVIDER, DEFAULT_PROVIDER)
            username = self._config_entry.data.get(CONF_USERNAME, None)
            password = self._config_entry.data.get(CONF_PASSWORD, None)
            if username is None:
                raise ConfigError("Cannot retrieve the username from the config entry")
            if password is None:
                raise ConfigError("Cannot retrieve the password from the config entry")

            tokens = await get_agur_tokens(
                hass=self.hass,
                username=username,
                password=password,
                provider=provider
            )
            available_contracts = await get_agur_contract_options(
                hass=self.hass,
                session_token=tokens[SESSION_TOKEN],
                auth_token=tokens[AUTH_TOKEN],
                provider=provider,
                http_session=tokens[HTTP_SESSION],
            )
        except ConfigError as ex:
            _LOGGER.error(ex)
            errors = {"base": "config"}
        except AuthError as ex:
            _LOGGER.error(ex)
            errors = {"base": "auth"}
        except ContractError as ex:
            _LOGGER.error(ex)
            errors = {"base": "contracts"}
        except Exception as ex:
            _LOGGER.error(ex)
            errors = {"base": "unknown"}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONTRACT_IDS,
                        default=default_contracts,
                    ): config_validation.multi_select(available_contracts),
                    vol.Optional(
                        CONF_IMPORT_STATISTICS,
                        default=default_import_statistics
                    ): config_validation.boolean,
                }
            ),
            errors=errors,
        )


class PriceSubentryFlow(config_entries.ConfigSubentryFlow):
    """Add or change a price period of a contract: a price per m³ in force from a start date."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.SubentryFlowResult:
        """Add a price period."""
        return await self._async_step_price("user", user_input, None)

    async def async_step_reconfigure(
            self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Change a price period."""
        return await self._async_step_price("reconfigure", user_input, self._get_reconfigure_subentry())

    async def _async_step_price(
            self,
            step_id: str,
            user_input: dict[str, Any] | None,
            subentry: config_entries.ConfigSubentry | None,
    ) -> config_entries.SubentryFlowResult:
        entry = self._get_entry()
        contract_ids: list[str] = list(entry.options.get(CONF_CONTRACT_IDS) or [])
        if not contract_ids:
            return self.async_abort(reason="no_contracts")

        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                # The contract is only asked when there are several
                CONF_CONTRACT_ID: user_input.get(CONF_CONTRACT_ID, contract_ids[0]),
                CONF_START_DATE: user_input[CONF_START_DATE],
                CONF_PRICE: float(user_input[CONF_PRICE]),
            }
            unique_id = f"{data[CONF_CONTRACT_ID]}_{data[CONF_START_DATE]}"
            if any(
                other.unique_id == unique_id and (subentry is None or other.subentry_id != subentry.subentry_id)
                for other in entry.subentries.values()
            ):
                errors[CONF_START_DATE] = "duplicate_start_date"
            else:
                title = price_title(self.hass, data, with_contract=len(contract_ids) > 1)
                if subentry is None:
                    return self.async_create_entry(title=title, data=data, unique_id=unique_id)
                return self.async_update_and_abort(entry, subentry, title=title, data=data, unique_id=unique_id)

        defaults: dict[str, Any] = dict(subentry.data) if subentry is not None else {}
        defaults.update(user_input or {})
        fields: dict[Any, Any] = {}
        if len(contract_ids) > 1:
            fields[vol.Required(CONF_CONTRACT_ID, default=defaults.get(CONF_CONTRACT_ID, contract_ids[0]))] = \
                SelectSelector(SelectSelectorConfig(options=contract_ids, mode=SelectSelectorMode.DROPDOWN))
        fields[vol.Required(CONF_START_DATE, **_default(defaults, CONF_START_DATE))] = DateSelector()
        fields[vol.Required(CONF_PRICE, **_default(defaults, CONF_PRICE))] = NumberSelector(
            NumberSelectorConfig(min=0, step="any", mode=NumberSelectorMode.BOX, unit_of_measurement="€/m³")
        )
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema(fields), errors=errors)


def _default(defaults: dict[str, Any], key: str) -> dict[str, Any]:
    return {"default": defaults[key]} if key in defaults else {}


def price_title(hass: HomeAssistant, data: dict[str, Any], with_contract: bool) -> str:
    """Title of a price subentry, as listed under the config entry. Subentry titles are not translated: it is
    written once, in the language of Home Assistant."""
    price = format(data[CONF_PRICE], "g")
    if hass.config.language.startswith("fr"):
        title = f"{price.replace('.', ',')} €/m³ depuis le {data[CONF_START_DATE]}"
    else:
        title = f"{price} €/m³ from {data[CONF_START_DATE]}"
    return f"{data[CONF_CONTRACT_ID]} · {title}" if with_contract else title


class ConfigError(exceptions.HomeAssistantError):
    """Error to indicate an issue the config entry."""


class AuthError(exceptions.HomeAssistantError):
    """Error to indicate an issue with the authentication."""


class ContractError(exceptions.HomeAssistantError):
    """Error to indicate an issue with the fetching of contracts."""
