"""HTTP-level tests for the Agur API client."""
from __future__ import annotations

from datetime import datetime

import pytest
import requests_mock as rm
from requests import HTTPError, Session

from custom_components.agur.agur_client import (
    REQUEST_TIMEOUT,
    AgurClient,
    AgurContract,
    AgurDataPoint,
    AgurInvoice,
)

from .conftest import CONTRACT_1, TZ, contract_json, data_points_json, invoices_json

BASE = "https://ael.example/webapi"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """These are plain unit tests: no hass nor recorder needed (overrides the conftest fixture)."""


def _client(**kwargs) -> AgurClient:
    return AgurClient(
        host="ael.example",
        base_path="webapi",
        access_key="access-key",
        client_id="client-id",
        **kwargs,
    )


def test_init(requests_mock: rm.Mocker) -> None:
    requests_mock.post(
        f"{BASE}/Acces/generateToken",
        json={"token": "session-token", "expirationDate": "2026-11-10T12:00:00+01:00"},
    )
    assert _client().init() == {"token": "session-token", "expirationDate": "2026-11-10T12:00:00+01:00"}

    request = requests_mock.last_request
    assert request.headers["Token"] == "access-key"
    assert request.json() == {
        "ConversationId": AgurClient.app_id,
        "ClientId": "client-id",
        "AccessKey": "access-key",
    }
    assert request.timeout == REQUEST_TIMEOUT


def test_login(requests_mock: rm.Mocker) -> None:
    requests_mock.post(f"{BASE}/Utilisateur/authentification", json={"tokenAuthentique": "auth-token"})
    assert _client(session_token="session-token").login("user", "pass") == {"tokenAuthentique": "auth-token"}

    request = requests_mock.last_request
    assert request.headers["Token"] == "session-token"
    assert request.json() == {"identifiant": "user", "motDePasse": "pass"}
    assert request.timeout == REQUEST_TIMEOUT


def test_get_contracts(requests_mock: rm.Mocker) -> None:
    requests_mock.get(
        f"{BASE}/Abonnement/contrats",
        json={"resultats": [contract_json(CONTRACT_1), contract_json("42")]},
    )
    contracts = _client(auth_token="auth-token").get_contracts()

    assert [c.id for c in contracts] == [CONTRACT_1, "42"]
    assert requests_mock.last_request.headers["Token"] == "auth-token"
    assert requests_mock.last_request.timeout == REQUEST_TIMEOUT


def test_get_contract(requests_mock: rm.Mocker) -> None:
    requests_mock.get(f"{BASE}/Abonnement/detailAbonnement/{CONTRACT_1}", json=contract_json(CONTRACT_1))
    contract = _client(auth_token="auth-token").get_contract(CONTRACT_1)

    assert contract.id == CONTRACT_1
    assert contract.owner == f"Owner {CONTRACT_1}"
    assert contract.address == f"{CONTRACT_1} rue de l'Eau, 64100 Bayonne"
    assert contract.meter_id == f"meter-{CONTRACT_1}"
    assert contract.meter_serial_number == f"SN-{CONTRACT_1}"
    assert contract.meter_endpoint_number == f"PDL-{CONTRACT_1}"


def test_get_data(requests_mock: rm.Mocker) -> None:
    requests_mock.get(
        f"{BASE}/Facturation/listeConsommationsFacturees/{CONTRACT_1}",
        json={"resultats": data_points_json(1000)},
    )
    data_points = _client(auth_token="auth-token").get_data(CONTRACT_1)

    assert [p.value for p in data_points] == [1370, 1250, 1100, 1000]
    assert data_points[0].date == datetime(2026, 11, 8, tzinfo=TZ)


def test_get_invoices(requests_mock: rm.Mocker) -> None:
    requests_mock.get(f"{BASE}/Facture/listeFactures", json={"resultats": invoices_json(CONTRACT_1)})
    invoices = _client(auth_token="auth-token").get_invoices(CONTRACT_1)

    assert requests_mock.last_request.qs["numerocontrat"] == [CONTRACT_1]
    assert [i.number for i in invoices] == [f"INV-{CONTRACT_1}-2", f"INV-{CONTRACT_1}-1"]
    assert invoices[0].total == 45.67
    assert invoices[0].issue_date == datetime(2026, 10, 1, tzinfo=TZ)
    assert invoices[0].payment_date == datetime(2026, 10, 31, tzinfo=TZ)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12,50", 12.5), ("-1.234,56", -1234.56), ("0", 0.0)],
)
def test_get_balance(requests_mock: rm.Mocker, raw: str, expected: float) -> None:
    """The balance is returned as a French-formatted string."""
    requests_mock.get(f"{BASE}/Facturation/soldeComptableContratAbonnement/{CONTRACT_1}", json=raw)
    assert _client(auth_token="auth-token").get_balance(CONTRACT_1) == expected


@pytest.mark.parametrize(
    ("method", "args", "http_method", "path"),
    [
        ("init", (), "POST", "/Acces/generateToken"),
        ("login", ("user", "pass"), "POST", "/Utilisateur/authentification"),
        ("get_contracts", (), "GET", "/Abonnement/contrats"),
        ("get_contract", (CONTRACT_1,), "GET", f"/Abonnement/detailAbonnement/{CONTRACT_1}"),
        ("get_data", (CONTRACT_1,), "GET", f"/Facturation/listeConsommationsFacturees/{CONTRACT_1}"),
        ("get_invoices", (CONTRACT_1,), "GET", "/Facture/listeFactures"),
        ("get_balance", (CONTRACT_1,), "GET", f"/Facturation/soldeComptableContratAbonnement/{CONTRACT_1}"),
    ],
)
def test_http_errors_are_raised(requests_mock: rm.Mocker, method, args, http_method, path) -> None:
    requests_mock.register_uri(http_method, f"{BASE}{path}", status_code=401, json={"message": "nope"})

    with pytest.raises(HTTPError) as error:
        getattr(_client(), method)(*args)
    assert error.value.response.status_code == 401


def test_shared_session_carries_affinity_cookie(requests_mock: rm.Mocker) -> None:
    """Some providers set a load balancer cookie on generateToken that every later call must carry.

    All calls must go through the shared session, so that its cookie jar is used.
    """
    requests_mock.post(f"{BASE}/Utilisateur/authentification", json={"tokenAuthentique": "a"})
    requests_mock.get(f"{BASE}/Abonnement/contrats", json={"resultats": []})
    session = Session()
    session.cookies.set("BIGipServer", "affinity", domain="ael.example", path="/")

    _client(http_session=session, session_token="t").login("user", "pass")
    _client(http_session=session, auth_token="a").get_contracts()

    assert [r.headers.get("Cookie") for r in requests_mock.request_history] == ["BIGipServer=affinity"] * 2


def test_client_creates_its_own_session() -> None:
    assert isinstance(_client().http, Session)


def test_models_tolerate_missing_fields() -> None:
    contract = AgurContract({})
    assert (contract.id, contract.owner, contract.address, contract.meter_id) == (None, None, None, None)
    assert (contract.meter_serial_number, contract.meter_endpoint_number) == (None, None)

    invoice = AgurInvoice({})
    assert (invoice.number, invoice.total, invoice.issue_date, invoice.payment_date) == (None, None, None, None)

    point = AgurDataPoint({"valeurIndex": None, "volumeConsoEnLitres": None, "dateReleve": None})
    assert (point.value, point.volume, point.date) == (None, None, None)


def test_data_point_volume() -> None:
    point = AgurDataPoint({"valeurIndex": "1000", "volumeConsoEnLitres": "120", "dateReleve": "2026-11-08T00:00:00+01:00"})
    assert (point.value, point.volume) == (1000.0, 120.0)


def test_contract_without_meter() -> None:
    """The API uses "0" when no meter is attached to the contract."""
    contract = AgurContract({**contract_json(CONTRACT_1), "identifiantAppareil": "0"})
    assert contract.meter_id is None
