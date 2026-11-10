from __future__ import annotations

import asyncio
import logging
from datetime import timedelta, datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import get_last_statistics, async_import_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from requests import HTTPError, Session

from .agur_client import AgurClient, AgurContract, AgurDataPoint, AgurInvoice
from .const import DOMAIN, PROVIDERS, DEFAULT_PROVIDER

_LOGGER: logging.Logger = logging.getLogger(__name__)
# TODO: This should be configurable?
SCAN_INTERVAL = timedelta(days=1)


class AgurDataUpdateCoordinatorData:
    data_points: list[AgurDataPoint]
    invoices: list[AgurInvoice]
    contract: AgurContract
    balance: float

    def __init__(
            self,
            data_points: list[AgurDataPoint],
            invoices: list[AgurInvoice],
            contract: AgurContract,
            balance: float,
    ):
        # TODO: Sort from date descending
        self.data_points = data_points
        self.invoices = invoices
        self.contract = contract
        self.balance = balance

    @property
    def last_index(self) -> AgurDataPoint | None:
        return self.data_points[0] if len(self.data_points) > 0 else None

    @property
    def last_index_value(self) -> float | None:
        return self.last_index.value if self.last_index is not None else None

    @property
    def last_index_date(self) -> datetime | None:
        return self.last_index.date if self.last_index is not None else None

    @property
    def last_invoice(self) -> AgurInvoice | None:
        return self.invoices[0] if len(self.invoices) > 0 else None

    @property
    def last_invoice_value(self) -> float | None:
        return self.last_invoice.total if self.last_invoice is not None else None

    @property
    def last_invoice_date(self) -> datetime | None:
        return self.last_invoice.issue_date if self.last_invoice is not None else None

    @property
    def last_balance_value(self) -> float | None:
        return self.balance


class AgurDataUpdateCoordinator(DataUpdateCoordinator[dict[str, AgurDataUpdateCoordinatorData]]):
    """Class to manage fetching data from the API."""

    def __init__(
            self,
            hass: HomeAssistant,
            provider: str,
            username: str,
            password: str,
            contract_ids: list[str],
            import_statistics: bool
    ) -> None:
        """Initialize."""
        self.platforms = []
        self.expiration_date: datetime | None = None
        self.session_token: datetime | None = None
        self.auth_token: str | None = None
        self.provider = provider
        self.provider_config = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
        self.username = username
        self.password = password
        self.contract_ids = contract_ids
        self.import_statistics = import_statistics
        # One HTTP session for the lifetime of the coordinator: some providers (Eau de Paris)
        # sit behind a load balancer that sets an affinity cookie on generateToken, which
        # every later call must carry.
        self.http_session = Session()

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)

    async def _async_update_data(self) -> dict[str, AgurDataUpdateCoordinatorData]:
        """Update data via library."""
        # If we do not have any session token (first time)
        if self.session_token is None or self.expiration_date is None:
            _LOGGER.debug("First sync: fetching session token")
            await self._async_get_session_token()

        try:
            # If we do not have any auth token (first time)
            if self.auth_token is None:
                _LOGGER.debug("First sync: fetching auth token")
                await self._async_get_auth_token()

            # If the session token is expired, retrieve one
            if datetime.now().replace(tzinfo=self.expiration_date.tzinfo) > self.expiration_date:
                _LOGGER.debug("Session token expired: fetching a new one")
                await self._async_refresh_tokens()

            data: dict[str, AgurDataUpdateCoordinatorData] = {}
            for contract_id in self.contract_ids:
                _LOGGER.debug(f"Fetching details and history for contract '{contract_id}'")
                results = await asyncio.gather(
                    self._async_get_data_points(contract_id=contract_id),
                    self._async_get_invoices(contract_id=contract_id),
                    self._async_get_contract(contract_id=contract_id),
                    self._async_get_balance(contract_id=contract_id),
                )
                data[contract_id] = AgurDataUpdateCoordinatorData(
                    data_points=results[0],
                    invoices=results[1],
                    contract=results[2],
                    balance=results[3],
                )

                await self._handle_statistics(coordinator_data=data[contract_id])

            return data

        except HTTPError as exception:
            if exception.response.status_code == 401:
                raise ConfigEntryAuthFailed(f"Invalid credentials for Agur account {self.username}")
            raise exception
        except Exception as exception:
            raise UpdateFailed(f"Error communicating with API: {exception}")

    async def _async_get_session_token(self) -> None:
        client = AgurClient(
            host=self.provider_config["host"],
            base_path=self.provider_config["base_path"],
            access_key=self.provider_config["access_key"],
            client_id=self.provider_config["client_id"],
            http_session=self.http_session,
        )
        response = await self.hass.async_add_executor_job(client.init)
        self.session_token = response["token"]
        self.expiration_date = datetime.fromisoformat(response["expirationDate"])

    async def _async_get_auth_token(self) -> None:
        client = AgurClient(
            session_token=self.session_token,
            host=self.provider_config["host"],
            base_path=self.provider_config["base_path"],
            access_key=self.provider_config["access_key"],
            client_id=self.provider_config["client_id"],
            http_session=self.http_session,
        )
        response = await self.hass.async_add_executor_job(client.login, self.username, self.password)
        self.auth_token = response["tokenAuthentique"]

    async def _async_refresh_tokens(self) -> None:
        await self._async_get_session_token()
        await self._async_get_auth_token()

    async def _async_get_data_points(self, contract_id) -> list[AgurDataPoint]:
        client = AgurClient(
            session_token=self.session_token,
            auth_token=self.auth_token,
            host=self.provider_config["host"],
            base_path=self.provider_config["base_path"],
            access_key=self.provider_config["access_key"],
            client_id=self.provider_config["client_id"],
            http_session=self.http_session,
        )
        return await self.hass.async_add_executor_job(client.get_data, contract_id)

    async def _async_get_invoices(self, contract_id) -> list[AgurInvoice]:
        client = AgurClient(
            session_token=self.session_token,
            auth_token=self.auth_token,
            host=self.provider_config["host"],
            base_path=self.provider_config["base_path"],
            access_key=self.provider_config["access_key"],
            client_id=self.provider_config["client_id"],
            http_session=self.http_session,
        )
        return await self.hass.async_add_executor_job(client.get_invoices, contract_id)

    async def _async_get_contract(self, contract_id) -> AgurContract:
        client = AgurClient(
            session_token=self.session_token,
            auth_token=self.auth_token,
            host=self.provider_config["host"],
            base_path=self.provider_config["base_path"],
            access_key=self.provider_config["access_key"],
            client_id=self.provider_config["client_id"],
            http_session=self.http_session,
        )
        return await self.hass.async_add_executor_job(client.get_contract, contract_id)

    async def _async_get_balance(self, contract_id) -> float:
        client = AgurClient(
            session_token=self.session_token,
            auth_token=self.auth_token,
            host=self.provider_config["host"],
            base_path=self.provider_config["base_path"],
            access_key=self.provider_config["access_key"],
            client_id=self.provider_config["client_id"],
            http_session=self.http_session,
        )
        return await self.hass.async_add_executor_job(client.get_balance, contract_id)

    async def _handle_statistics(self, coordinator_data: AgurDataUpdateCoordinatorData) -> None:
        if not self.import_statistics:
            return

        contract_id = coordinator_data.contract.id
        statistic_id = f"sensor.{DOMAIN}_water_consumption_{contract_id}"
        recorder = get_instance(self.hass)

        # Readings without index or date (e.g. the day of a meter swap) cannot be used
        daily_indexes_data = [
            p for p in coordinator_data.data_points if p.value is not None and p.date is not None
        ]
        if not daily_indexes_data:
            return
        last_stats = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )

        min_start = (
            daily_indexes_data[0].date - timedelta(days=30)
            if last_stats
            else None
        )

        statistics = []
        # Readings come most recent first: walk them chronologically, so that each one is compared with
        # the reading before it (even when that previous reading is outside the re-import window)
        previous: AgurDataPoint | None = None
        for daily_index_data in reversed(daily_indexes_data):
            consumption = daily_index_data.value - previous.value if previous is not None else 0
            previous = daily_index_data
            if min_start and daily_index_data.date <= min_start:
                continue

            statistics.append(
                StatisticData(
                    start=daily_index_data.date.replace(minute=0, second=0, microsecond=0),
                    state=consumption / 1000,
                    sum=daily_index_data.value / 1000,
                )
            )

        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"Water consumption {contract_id}",
            source="recorder",
            statistic_id=statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )
        async_import_statistics(self.hass, metadata, statistics)
