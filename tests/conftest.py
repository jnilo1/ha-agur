"""Shared fixtures for the Agur tests."""
from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.agur.agur_client import AgurContract, AgurDataPoint, AgurInvoice
from custom_components.agur.const import (
    CONF_CONTRACT_IDS,
    CONF_IMPORT_STATISTICS,
    CONF_PASSWORD,
    CONF_PROVIDER,
    CONF_USERNAME,
    DOMAIN,
    VERSION,
)

USERNAME = "user@example.com"
PASSWORD = "secret"
PROVIDER = "ael"
ENTRY_ID = "01KQWMXNAVHNJC7W6245NC7888"

# Two contracts on the same account, as in issue #6
CONTRACT_1 = "1148463"
CONTRACT_2 = "1316637"
CONTRACTS = [CONTRACT_1, CONTRACT_2]

TZ = timezone(timedelta(hours=1))


def contract_json(contract_id: str) -> dict[str, Any]:
    return {
        "numeroContrat": contract_id,
        "nomClientTitulaire": f"Owner {contract_id}",
        "adresseLivraisonConstruite": f"{contract_id} rue de l'Eau, 64100 Bayonne",
        "identifiantAppareil": f"meter-{contract_id}",
        "numeroPhysiqueAppareil": f"SN-{contract_id}",
        "numeroPointLivraison": f"PDL-{contract_id}",
    }


def data_points_json(base: float) -> list[dict[str, Any]]:
    """Readings sorted from the most recent to the oldest, as returned by the API."""
    start = datetime(2026, 11, 5, tzinfo=TZ)
    # Index (liters) increases with time: 100 L, then 150 L, then 120 L per day
    values = [base, base + 100, base + 250, base + 370]
    return [
        {
            "valeurIndex": value,
            "volumeConsoEnLitres": None,
            "dateReleve": (start + timedelta(days=i)).isoformat(),
        }
        for i, value in enumerate(values)
    ][::-1]


def invoices_json(contract_id: str) -> list[dict[str, Any]]:
    return [
        {
            "numeroFactureClient": f"INV-{contract_id}-2",
            "montantTTCFacture": "45.67",
            "dateEmissionFacture": "2026-10-01T00:00:00+01:00",
            "dateLimitePaiementFacture": "2026-10-31T00:00:00+01:00",
        },
        {
            "numeroFactureClient": f"INV-{contract_id}-1",
            "montantTTCFacture": "12.34",
            "dateEmissionFacture": "2026-04-01T00:00:00+01:00",
            "dateLimitePaiementFacture": "2026-04-30T00:00:00+01:00",
        },
    ]


class FakeAgurApi:
    """In-memory stand-in for the Agur API, shared by every AgurClient created during a test."""

    def __init__(self) -> None:
        self.contracts: dict[str, AgurContract] = {
            c: AgurContract(contract_json(c)) for c in CONTRACTS
        }
        self.data_points: dict[str, list[AgurDataPoint]] = {
            CONTRACT_1: [AgurDataPoint(j) for j in data_points_json(1_000_000)],
            CONTRACT_2: [AgurDataPoint(j) for j in data_points_json(2_000_000)],
        }
        self.invoices: dict[str, list[AgurInvoice]] = {
            c: [AgurInvoice(j) for j in invoices_json(c)] for c in CONTRACTS
        }
        self.balances: dict[str, float] = {CONTRACT_1: -12.5, CONTRACT_2: 0.0}
        # The coordinator compares local wall time with this date: use the local timezone
        self.expiration_date = datetime.now().astimezone() + timedelta(hours=1)
        # Method name -> exception to raise
        self.errors: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.client_kwargs: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FakeAgurClient:
        self.client_kwargs.append(kwargs)
        return FakeAgurClient(self)

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.errors:
            raise self.errors[name]


class FakeAgurClient:
    def __init__(self, api: FakeAgurApi) -> None:
        self._api = api

    def init(self) -> dict[str, Any]:
        self._api._call("init")
        return {"token": "session-token", "expirationDate": self._api.expiration_date.isoformat()}

    def login(self, username: str, password: str) -> dict[str, Any]:
        self._api._call("login")
        return {"tokenAuthentique": "auth-token"}

    def get_contracts(self) -> list[AgurContract]:
        self._api._call("get_contracts")
        return list(self._api.contracts.values())

    def get_contract(self, contract_id: str) -> AgurContract:
        self._api._call("get_contract")
        return self._api.contracts[contract_id]

    def get_data(self, contract_id: str) -> list[AgurDataPoint]:
        self._api._call("get_data")
        return self._api.data_points[contract_id]

    def get_invoices(self, contract_id: str) -> list[AgurInvoice]:
        self._api._call("get_invoices")
        return self._api.invoices[contract_id]

    def get_balance(self, contract_id: str) -> float:
        self._api._call("get_balance")
        return self._api.balances[contract_id]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: Any, enable_custom_integrations: None) -> None:
    """Allow loading custom_components/agur in every test.

    The integration depends on recorder, which must be set up before hass: request it first.
    """


@pytest.fixture
def agur_api() -> Generator[FakeAgurApi]:
    """Patch AgurClient everywhere it is used with an in-memory fake."""
    api = FakeAgurApi()
    with (
        patch("custom_components.agur.coordinator.AgurClient", api),
        patch("custom_components.agur.config_flow.AgurClient", api),
    ):
        yield api


def make_entry(
    contract_ids: list[str] | None = None,
    import_statistics: bool = True,
    version: int = VERSION,
    entry_id: str = ENTRY_ID,
    subentries_data: list[dict[str, Any]] | None = None,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME,
        entry_id=entry_id,
        version=version,
        data={CONF_PROVIDER: PROVIDER, CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        options={
            CONF_CONTRACT_IDS: list(CONTRACTS if contract_ids is None else contract_ids),
            CONF_IMPORT_STATISTICS: import_statistics,
        },
        subentries_data=subentries_data,
    )
