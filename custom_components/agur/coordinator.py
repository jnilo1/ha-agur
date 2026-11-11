from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import isclose

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import CURRENCY_EURO, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.unit_conversion import VolumeConverter
from requests import HTTPError, Session

from .agur_client import AgurClient, AgurContract, AgurDataPoint, AgurInvoice
from .const import DOMAIN, PROVIDERS, DEFAULT_PROVIDER

_LOGGER: logging.Logger = logging.getLogger(__name__)
# TODO: This should be configurable?
SCAN_INTERVAL = timedelta(days=1)
# Same rule as Home Assistant for total_increasing sensors: the index of a new meter is lower than 90% of the old one.
# A smaller decrease is a correction of the index, not a reset.
RESET_RATIO = 0.9


def statistic_id_for(contract_id: str) -> str:
    """Id of the external statistic holding the water consumption history of a contract.

    The history is imported as an external statistic (`agur:...`), not under a sensor entity: the recorder
    compiles its own statistics for every sensor with a state_class, with a sum starting at 0 when the sensor
    appears. Written under the same statistic id, those hourly rows get interleaved with the imported ones
    (whose sum is the meter total), and the Energy dashboard, which reads the differences between consecutive
    sums, shows a drop of the whole total followed by jumps of the same size. External statistics are a
    supported water source of the Energy dashboard, and keep the real reading dates.
    """
    return f"{DOMAIN}:water_consumption_{contract_id}"


def cost_statistic_id_for(contract_id: str) -> str:
    """Id of the external statistic holding the water cost of a contract, to use as the cost of the Energy
    dashboard water source. External for the same reasons as the consumption, see statistic_id_for."""
    return f"{DOMAIN}:water_cost_{contract_id}"


@dataclass(frozen=True)
class PricePeriod:
    """A price per m³ (incl. tax, excluding the subscription), in force from its start date until the next one."""
    start: date
    price: float


def price_at(periods: list[PricePeriod], day: date) -> float | None:
    """Return the price in force on that day, or None if it is before the first price period."""
    in_force = [p for p in periods if p.start <= day]
    return max(in_force, key=lambda p: p.start).price if in_force else None


def since_latest_reset(readings: list[AgurReading]) -> list[AgurReading]:
    """Keep only the readings of the current meter, with totals restarting at the consumption of its first reading.

    Local choice: the history of the previous meter is not imported (for this account it was estimated for two
    years while the remote reading was broken). The cost statistic is computed from the same readings.
    """
    start = 0
    for position in range(1, len(readings)):
        if readings[position].point.value < RESET_RATIO * readings[position - 1].point.value:
            start = position
    if start == 0:
        return readings
    base = readings[start].total - readings[start].consumption
    return [
        AgurReading(point=r.point, consumption=r.consumption, total=r.total - base)
        for r in readings[start:]
    ]


@dataclass(frozen=True)
class AgurReading:
    """A meter reading, with the consumption since the previous one and the total consumption, both in liters."""
    point: AgurDataPoint
    consumption: float
    total: float


def compute_consumptions(data_points: list[AgurDataPoint]) -> list[AgurReading]:
    """Compute the consumption between consecutive readings, from the oldest to the most recent.

    The total starts at the index of the oldest reading, then adds up the consumptions: as long as the meter is
    not replaced, it is the index. When it is replaced, the new meter starts again from 0. The consumption of
    that reading is then the volume computed by the provider when there is one, else the index of the new meter
    (i.e. what was used since it was installed), plus the volume of the readings without index in between (the
    day of the swap). The consumption of the old meter after its last reading is lost if the provider does not
    count it in these volumes.
    """
    readings: list[AgurReading] = []
    unindexed_volume = 0.0
    for point in sorted((p for p in data_points if p.date is not None), key=lambda p: p.date):
        if point.value is None:
            unindexed_volume += point.volume or 0
            continue

        if not readings:
            consumption = 0.0
            total = point.value
        else:
            previous = readings[-1]
            if point.value < RESET_RATIO * previous.point.value:
                _LOGGER.debug(f"Meter index went from {previous.point.value} L to {point.value} L "
                              f"on {point.date}: meter replaced")
                consumption = unindexed_volume + (point.volume if point.volume is not None else point.value)
            else:
                consumption = point.value - previous.point.value
            total = previous.total + consumption
        unindexed_volume = 0.0
        readings.append(AgurReading(point=point, consumption=consumption, total=total))
    return readings


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
            import_statistics: bool,
            price_periods: dict[str, list[PricePeriod]] | None = None,
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
        self.price_periods = price_periods or {}
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
        statistic_id = statistic_id_for(contract_id)
        recorder = get_instance(self.hass)

        readings = since_latest_reset(compute_consumptions(coordinator_data.data_points))
        if not readings:
            return
        last_stats = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )

        min_start: datetime | None = None
        offset = 0.0
        if last_stats:
            # Only re-import the last 30 days, continuing the statistics already stored
            min_start = readings[-1].point.date - timedelta(days=30)
            offset = await self._async_statistics_offset(
                statistic_id, readings, min_start, last_stats[statistic_id][0]
            )

        statistics = [
            StatisticData(
                start=_statistic_start(reading.point),
                state=reading.consumption / 1000,
                sum=reading.total / 1000 + offset,
            )
            for reading in readings
            if min_start is None or reading.point.date > min_start
        ]

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Water consumption {contract_id}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            unit_class=VolumeConverter.UNIT_CLASS,
        )
        async_add_external_statistics(self.hass, metadata, statistics)

        await self._async_handle_cost_statistics(contract_id, readings)

    async def _async_handle_cost_statistics(self, contract_id: str, readings: list[AgurReading]) -> None:
        """Import the cost of every reading of the history that has a price, i.e. its consumption times the price
        in force on its date. Readings before the first price period have no cost (no row, rather than 0 €).

        Unlike the consumption, the whole history sent by the API is recomputed at each update, so that a price
        added or corrected late applies back to its start date. Only the rows that changed are written. Rows older
        than that history are kept, and the sums continue from them. If a row of that history no longer has a
        price (a price period was removed or now starts later), the recorder cannot delete single rows: the cost
        statistic is cleared and rebuilt from the history alone.
        """
        statistic_id = cost_statistic_id_for(contract_id)
        periods = self.price_periods.get(contract_id, [])
        costs: list[tuple[datetime, float]] = []
        for reading in readings:
            price = price_at(periods, reading.point.date.date())
            if price is not None:
                costs.append((_statistic_start(reading.point), round(reading.consumption / 1000 * price, 4)))

        stored = await self._async_stored_costs(statistic_id)
        history_start = _statistic_start(readings[0].point).timestamp()
        priced = {start.timestamp() for start, _ in costs}
        if any(start >= history_start and start not in priced for start in stored):
            _LOGGER.info(f"Rebuilding {statistic_id}: some readings no longer have a price")
            get_instance(self.hass).async_clear_statistics([statistic_id])
            stored = {}

        if not costs:
            return

        first = costs[0][0].timestamp()
        older = [start for start in stored if start < first]
        total = stored[max(older)][0] if older else 0.0
        statistics = []
        for start, cost in costs:
            total = round(total + cost, 4)
            row = stored.get(start.timestamp())
            if row is None or not isclose(row[0], total, abs_tol=1e-4) or not isclose(row[1], cost, abs_tol=1e-4):
                statistics.append(StatisticData(start=start, state=cost, sum=total))
        if not statistics:
            return

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Water cost {contract_id}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=CURRENCY_EURO,
            unit_class=None,
        )
        async_add_external_statistics(self.hass, metadata, statistics)

    async def _async_stored_costs(self, statistic_id: str) -> dict[float, tuple[float, float]]:
        """Return the stored cost rows, as {start timestamp: (sum, state)}."""
        stored = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(1970, 1, 1, tzinfo=timezone.utc),
            None,
            {statistic_id},
            "hour",
            None,
            {"sum", "state"},
        )
        return {row["start"]: (row["sum"], row["state"]) for row in stored.get(statistic_id, [])}

    async def _async_statistics_offset(
            self,
            statistic_id: str,
            readings: list[AgurReading],
            min_start: datetime,
            last_stat: StatisticsRow,
    ) -> float:
        """Return what to add to the totals (in m³) so that they continue the statistics already stored.

        Without any meter reset, totals equal the index and the offset is 0, as before. After a reset, the
        offset keeps the sum increasing even once the reset is no longer part of the history sent by the API.
        Statistics already stored are never rewritten outside the re-import window.
        """
        window = [r for r in readings if r.point.date > min_start]
        stored = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            _statistic_start(window[0].point),
            None,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        stored_sums = {row["start"]: row["sum"] for row in stored.get(statistic_id, [])}

        # Keep the sum of the oldest reading of the window that is already stored
        for reading in window:
            stored_sum = stored_sums.get(_statistic_start(reading.point).timestamp())
            if stored_sum is not None:
                return round(stored_sum - reading.total / 1000, 3)

        # None is stored yet (e.g. no update for more than 30 days): continue from the last statistic
        previous = [r for r in readings if _statistic_start(r.point).timestamp() <= last_stat["start"]]
        if previous:
            return round(last_stat["sum"] - previous[-1].total / 1000, 3)

        # All the readings are newer than the last statistic: the consumption in between is only known
        # from the volume of the oldest reading, if the provider sends it. When the readings start at a new
        # meter (see since_latest_reset), its first consumption is known: the index of the new meter.
        first = readings[0]
        known = max(first.consumption, first.point.volume or 0)
        return round(last_stat["sum"] - (first.total - known) / 1000, 3)


def _statistic_start(point: AgurDataPoint) -> datetime:
    return point.date.replace(minute=0, second=0, microsecond=0)
