# Base component constants
from typing import Final

DOMAIN: Final = "agur"
DEFAULT_NAME: Final = "Agur"
VERSION = 2

# Configuration and options constants
CONF_ENABLED: str = "enabled"
CONF_USERNAME: str = "username"
CONF_PASSWORD: str = "password"
CONF_CONTRACT_IDS: str = "contract_ids"
CONF_IMPORT_STATISTICS: str = "import_statistics"
CONF_PROVIDER: str = "provider"

# Provider configuration
DEFAULT_PROVIDER: str = "ael"

PROVIDERS = {
    "ael": {
        "name": "Eau par Agur",
        "host": "ael.agur.fr",
        "base_path": "webapi",
        "client_id": "AEL-TOKEN-AGR-PRD",
        "access_key": "XX_fr-5DjklsdMM-AGR-PRD",
    },
    "grandparissud": {
        "name": "Grand Paris Sud",
        "host": "abonne-eau.grandparissud.fr",
        "base_path": "webapi",
        "client_id": "AEL-TOKEN-GPS-PRD",
        "access_key": "REGPS-hc-GPS-MP-PRD",
    },
    "eauparis": {
        "name": "Eau Paris",
        "host": "agence.eaudeparis.fr",
        "base_path": "webapi",
        "client_id": "AEL-TOKEN-EDP-PRD",
        "access_key": "EDP-2-hjkUyGHT-M-PRD",
    },
}

# Other constants
SENSOR_PLATFORM: str = "sensor"
PLATFORMS = [SENSOR_PLATFORM]

STARTUP_MESSAGE = f"""
---------------------------------------------------------------------
{DEFAULT_NAME} integration
Custom integration to fetch water data from Agur: https://ael.agur.fr

Domain: {DOMAIN}
Version: {VERSION}
---------------------------------------------------------------------
"""
