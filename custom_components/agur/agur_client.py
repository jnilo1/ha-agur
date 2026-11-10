from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from requests import Session

REQUEST_TIMEOUT = 30  # seconds


class AgurContract:
    id: str | None = None
    owner: str | None = None
    address: str | None = None
    meter_id: str | None = None
    meter_serial_number: str | None = None
    meter_endpoint_number: str | None = None

    def __init__(self, json: dict[str, Any]) -> None:
        if "numeroContrat" in json:
            self.id = json["numeroContrat"]

        if "nomClientTitulaire" in json:
            self.owner = json["nomClientTitulaire"]

        if "adresseLivraisonConstruite" in json:
            self.address = json["adresseLivraisonConstruite"]

        if "identifiantAppareil" in json and json["identifiantAppareil"] != "0":
            self.meter_id = json["identifiantAppareil"]

        if "numeroPhysiqueAppareil" in json:
            self.meter_serial_number = json["numeroPhysiqueAppareil"]

        if "numeroPointLivraison" in json:
            self.meter_endpoint_number = json["numeroPointLivraison"]


class AgurInvoice:
    number: str | None = None
    total: float | None = None
    issue_date: datetime | None = None
    payment_date: datetime | None = None

    def __init__(self, json: dict[str, Any]) -> None:
        if "numeroFactureClient" in json:
            self.number = json["numeroFactureClient"]

        if "montantTTCFacture" in json:
            self.total = float(json["montantTTCFacture"])

        if "dateEmissionFacture" in json:
            self.issue_date = datetime.fromisoformat(json["dateEmissionFacture"])

        if "dateLimitePaiementFacture" in json:
            self.payment_date = datetime.fromisoformat(json["dateLimitePaiementFacture"])


class AgurDataPoint:
    value: float | None = None  # meter index in liters
    volume: float | None = None  # daily consumption in liters, as computed by the provider
    date: datetime | None = None

    def __init__(self, json: dict[str, Any]) -> None:
        if json.get("valeurIndex") is not None:
            self.value = float(json["valeurIndex"])

        if json.get("volumeConsoEnLitres") is not None:
            self.volume = float(json["volumeConsoEnLitres"])

        if json.get("dateReleve") is not None:
            self.date = datetime.fromisoformat(json["dateReleve"])


class AgurClient:
    app_id = str(uuid.uuid4())
    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/113.0"
    session_token = None
    auth_token = None
    host = None
    base_path = None
    access_key = None
    client_id = None

    def __init__(
        self,
        session_token: str = None,
        auth_token: str = None,
        host: str = None,
        base_path: str = None,
        access_key: str = None,
        client_id: str = None,
        http_session: Session = None,
    ):
        # The Eau de Paris API sits behind a load balancer that sets an affinity cookie on
        # generateToken; every subsequent call must carry it or it fails randomly with
        # "Session inconnue" / "Le token de session a expiré". Share one Session.
        self.http = http_session if http_session is not None else Session()
        self.session_token = session_token
        self.auth_token = auth_token
        self.host = host
        self.base_path = base_path
        self.access_key = access_key
        self.client_id = client_id

    def init(self) -> dict[str, Any]:
        response = self.http.post(f"https://{self.host}/{self.base_path}/Acces/generateToken", headers={
            "ConversationId": self.app_id,
            "Token": self.access_key,
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self.user_agent,
        }, json={
            "ConversationId": self.app_id,
            "ClientId": self.client_id,
            "AccessKey": self.access_key,
        }, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        return response.json()

    def login(self, username, password) -> dict[str, Any]:
        response = self.http.post(f"https://{self.host}/{self.base_path}/Utilisateur/authentification", headers={
            "ConversationId": self.app_id,
            "Token": self.session_token,
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self.user_agent,
        }, json={
            "identifiant": username,
            "motDePasse": password,
        }, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        return response.json()

    def get_contracts(self) -> list[AgurContract]:
        response = self.http.get(
            f"https://{self.host}/{self.base_path}/Abonnement/contrats?userWebId=&recherche=&tri=NumeroContrat&triDecroissant=false&indexPage=0&nbElements=25",
            headers={
                "ConversationId": self.app_id,
                "User-Agent": self.user_agent,
                "Token": self.auth_token,
            }, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        return list(map(lambda contract: AgurContract(json=contract), response.json()["resultats"]))

    def get_contract(self, contract_id: str) -> AgurContract:
        response = self.http.get(
            f"https://{self.host}/{self.base_path}/Abonnement/detailAbonnement/{contract_id}",
            headers={
                "ConversationId": self.app_id,
                "User-Agent": self.user_agent,
                "Token": self.auth_token,
            }, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        return AgurContract(json=response.json())

    def get_data(self, contract_id) -> list[AgurDataPoint]:
        response = self.http.get(f"https://{self.host}/{self.base_path}/Facturation/listeConsommationsFacturees/{contract_id}", headers={
            "ConversationId": self.app_id,
            "User-Agent": self.user_agent,
            "Token": self.auth_token,
        }, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        return list(map(lambda json: AgurDataPoint(json=json), response.json()["resultats"]))

    def get_invoices(self, contract_id) -> list[AgurInvoice]:
        response = self.http.get(
            f"https://{self.host}/{self.base_path}/Facture/listeFactures?numeroContrat={contract_id}&recherche=&tri=&triDecroissant=false&indexPage=0&nbElements=25&dateDebut=&dateFin=&listeColonnes=&profondeurHistorique=-1",
            headers={
                "ConversationId": self.app_id,
                "User-Agent": self.user_agent,
                "Token": self.auth_token,
            }, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        return list(map(lambda json: AgurInvoice(json=json), response.json()["resultats"]))

    def get_balance(self, contract_id) -> float:
        response = self.http.get(f"https://{self.host}/{self.base_path}/Facturation/soldeComptableContratAbonnement/{contract_id}",
                       headers={
                           "ConversationId": self.app_id,
                           "User-Agent": self.user_agent,
                           "Token": self.auth_token,
                       }, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        return float(response.json().replace('.', '').replace(',', '.'))
