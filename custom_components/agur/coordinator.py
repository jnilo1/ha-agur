from __future__ import annotations

import logging
from datetime import timedelta, datetime, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData, StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from requests import HTTPError, Session

from .agur_client import AgurClient, AgurContract, AgurDataPoint, AgurInvoice
from .const import DOMAIN, PROVIDERS, DEFAULT_PROVIDER
from .statistics_calc import DailyReading, build_statistics

_LOGGER: logging.Logger = logging.getLogger(__name__)
# TODO: This should be configurable?
SCAN_INTERVAL = timedelta(days=1)
# Readings of the last N days are re-imported on every refresh so that late corrections
# from the provider are picked up. The cumulative sum is anchored on the row stored just
# before this window so that the series stays consistent.
REIMPORT_WINDOW = timedelta(days=30)


def statistic_id_for(contract_id: str) -> str:
    """External statistic id used for the water consumption of a contract."""
    return f"{DOMAIN}:water_consumption_{contract_id}"


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
        # Most recent first. Points without a date cannot be placed and are dropped.
        self.data_points = sorted(
            (p for p in data_points if p.date is not None), key=lambda p: p.date, reverse=True
        )
        self.invoices = sorted(
            invoices, key=lambda i: i.issue_date or datetime.min.replace(tzinfo=None), reverse=True
        ) if all(i.issue_date is not None for i in invoices) else invoices
        self.contract = contract
        self.balance = balance

    @property
    def last_index(self) -> AgurDataPoint | None:
        # Last reading that actually carries an index (the meter swap day has none).
        for point in self.data_points:
            if point.value is not None:
                return point
        return None

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
        self.session_token: str | None = None
        self.auth_token: str | None = None
        self.provider = provider
        self.provider_config = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
        self.username = username
        self.password = password
        self.contract_ids = contract_ids
        self.import_statistics = import_statistics
        # One HTTP session for the lifetime of the coordinator: keeps the load-balancer
        # affinity cookie set by generateToken (mandatory for Eau de Paris).
        self.http_session = Session()

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)

    def _client(self) -> AgurClient:
        return AgurClient(
            session_token=self.session_token,
            auth_token=self.auth_token,
            host=self.provider_config["host"],
            base_path=self.provider_config["base_path"],
            access_key=self.provider_config["access_key"],
            client_id=self.provider_config["client_id"],
            http_session=self.http_session,
        )

    async def _async_update_data(self) -> dict[str, AgurDataUpdateCoordinatorData]:
        """Update data via library."""
        try:
            # If we do not have any session token (first time)
            if self.session_token is None or self.expiration_date is None:
                _LOGGER.debug("First sync: fetching session token")
                await self._async_get_session_token()

            # If we do not have any auth token (first time)
            if self.auth_token is None:
                _LOGGER.debug("First sync: fetching auth token")
                await self._async_get_auth_token()

            # If the session token is expired, retrieve one
            if datetime.now(tz=self.expiration_date.tzinfo) > self.expiration_date:
                _LOGGER.debug("Session token expired: fetching a new one")
                await self._async_refresh_tokens()

            data: dict[str, AgurDataUpdateCoordinatorData] = {}
            for contract_id in self.contract_ids:
                _LOGGER.debug(f"Fetching details and history for contract '{contract_id}'")
                # Sequential calls: the shared requests.Session is not meant for concurrent use.
                data_points = await self._async_get_data_points(contract_id=contract_id)
                invoices = await self._async_get_invoices(contract_id=contract_id)
                contract = await self._async_get_contract(contract_id=contract_id)
                balance = await self._async_get_balance(contract_id=contract_id)
                data[contract_id] = AgurDataUpdateCoordinatorData(
                    data_points=data_points,
                    invoices=invoices,
                    contract=contract,
                    balance=balance,
                )

                await self._handle_statistics(contract_id=contract_id, coordinator_data=data[contract_id])

            return data

        except HTTPError as exception:
            if exception.response is not None and exception.response.status_code == 401:
                # Force a full re-authentication on the next attempt
                self.session_token = None
                self.auth_token = None
                raise ConfigEntryAuthFailed(f"Invalid credentials for Agur account {self.username}")
            raise UpdateFailed(f"Error communicating with API: {exception}")
        except ConfigEntryAuthFailed:
            raise
        except Exception as exception:
            raise UpdateFailed(f"Error communicating with API: {exception}")

    async def _async_get_session_token(self) -> None:
        response = await self.hass.async_add_executor_job(self._client().init)
        self.session_token = response["token"]
        self.expiration_date = datetime.fromisoformat(response["expirationDate"])

    async def _async_get_auth_token(self) -> None:
        response = await self.hass.async_add_executor_job(self._client().login, self.username, self.password)
        self.auth_token = response["tokenAuthentique"]

    async def _async_refresh_tokens(self) -> None:
        await self._async_get_session_token()
        await self._async_get_auth_token()

    async def _async_get_data_points(self, contract_id) -> list[AgurDataPoint]:
        return await self.hass.async_add_executor_job(self._client().get_data, contract_id)

    async def _async_get_invoices(self, contract_id) -> list[AgurInvoice]:
        return await self.hass.async_add_executor_job(self._client().get_invoices, contract_id)

    async def _async_get_contract(self, contract_id) -> AgurContract:
        return await self.hass.async_add_executor_job(self._client().get_contract, contract_id)

    async def _async_get_balance(self, contract_id) -> float:
        return await self.hass.async_add_executor_job(self._client().get_balance, contract_id)

    async def _handle_statistics(self, contract_id: str, coordinator_data: AgurDataUpdateCoordinatorData) -> None:
        """Import the daily readings as external long-term statistics.

        The statistics are stored under `agur:water_consumption_<contract>` (external
        statistics), not under a sensor entity: a sensor with a state_class would also get
        statistics compiled by the recorder from its state, and the two series would be
        inconsistent. External statistics are supported as a water source by the Energy
        dashboard, with the real reading dates.
        """
        if not self.import_statistics:
            return

        statistic_id = statistic_id_for(contract_id)
        recorder = get_instance(self.hass)

        readings = [
            DailyReading(date=p.date, index_l=p.value, volume_l=p.volume)
            for p in coordinator_data.data_points
        ]
        if not readings:
            _LOGGER.warning("No reading available for contract %s, statistics not updated", contract_id)
            return

        last_stats = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )

        anchor_start: datetime | None = None
        anchor_sum = 0.0
        if last_stats and last_stats.get(statistic_id):
            # Recorder returns epoch timestamps; tz-aware UTC datetimes compare fine with
            # the provider's local-time reading dates.
            last_start = datetime.fromtimestamp(last_stats[statistic_id][0]["start"], tz=timezone.utc)
            window_start = last_start - REIMPORT_WINDOW
            # Row stored just before the re-import window: it anchors the cumulative sum.
            previous_rows = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                window_start - timedelta(days=400),
                window_start + timedelta(seconds=1),
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            rows = previous_rows.get(statistic_id, [])
            if rows:
                anchor = rows[-1]
                anchor_start = datetime.fromtimestamp(anchor["start"], tz=timezone.utc)
                anchor_sum = anchor["sum"] or 0.0
            else:
                # No row before the window (young history): rebuild everything from scratch,
                # existing rows are overwritten with identical values.
                anchor_start = None

        stat_rows = build_statistics(readings, anchor_start=anchor_start, anchor_sum_m3=anchor_sum)
        if not stat_rows:
            _LOGGER.debug("No new statistics for contract %s", contract_id)
            return

        statistics = [
            StatisticData(start=row.start, state=row.state_m3, sum=row.sum_m3)
            for row in stat_rows
        ]
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Water consumption {contract_id}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            unit_class="volume",
        )
        _LOGGER.debug(
            "Importing %d statistic rows for %s (%s -> %s)",
            len(statistics), statistic_id, stat_rows[0].start, stat_rows[-1].start,
        )
        async_add_external_statistics(self.hass, metadata, statistics)
