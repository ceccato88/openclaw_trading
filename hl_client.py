from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

from eth_account import Account
from project_env import load_project_env

PROJECT_DIR = Path(__file__).resolve().parent
load_project_env(PROJECT_DIR)
try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
except ImportError as exc:
    raise ImportError(
        "Pacote 'hyperliquid-python-sdk' não instalado. Instale as dependências do projeto antes de rodar."
    ) from exc


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("HL_Client")

_CLIENT_STATE: Dict[str, Any] = {
    "info": None,
    "exchange": None,
    "wallet_address": None,
    "signer_address": None,
    "last_error": None,
}


def _normalize_hex_value(raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    return raw_value if raw_value.startswith("0x") else f"0x{raw_value}"


def _env_to_bool(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _build_hl_client(testnet: bool | None = None) -> tuple[Any, Any, str, str]:
    private_key = _normalize_hex_value(os.environ.get("HYPERLIQUID_PRIVATE_KEY"))
    if not private_key:
        raise ValueError("A variável de ambiente 'HYPERLIQUID_PRIVATE_KEY' não está definida.")

    if testnet is None:
        testnet = _env_to_bool("HYPERLIQUID_TESTNET", default=False)

    account = Account.from_key(private_key)
    trading_address = _normalize_hex_value(os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS")) or account.address
    base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL

    info_client = Info(base_url, skip_ws=True)
    exchange_client = Exchange(account, base_url, account_address=trading_address)

    if trading_address != account.address:
        logger.info("A usar API wallet %s para operar a conta %s.", account.address, trading_address)

    return info_client, exchange_client, trading_address, account.address


def ensure_hl_client(force_refresh: bool = False) -> Dict[str, Any]:
    if not force_refresh and _CLIENT_STATE["info"] is not None and _CLIENT_STATE["exchange"] is not None:
        return dict(_CLIENT_STATE)

    try:
        info_client, exchange_client, wallet_address, signer_address = _build_hl_client()
    except Exception as exc:  # noqa: BLE001
        _CLIENT_STATE.update(
            {
                "info": None,
                "exchange": None,
                "wallet_address": None,
                "signer_address": None,
                "last_error": str(exc),
            }
        )
        raise RuntimeError(f"Falha ao inicializar clientes Hyperliquid: {exc}") from exc

    _CLIENT_STATE.update(
        {
            "info": info_client,
            "exchange": exchange_client,
            "wallet_address": wallet_address,
            "signer_address": signer_address,
            "last_error": None,
        }
    )
    return dict(_CLIENT_STATE)


def refresh_hl_client() -> Dict[str, Any]:
    return ensure_hl_client(force_refresh=True)


def get_info():
    return ensure_hl_client()["info"]


def get_exchange():
    return ensure_hl_client()["exchange"]


def get_wallet_address() -> str:
    return str(ensure_hl_client()["wallet_address"])


def get_signer_address() -> str:
    return str(ensure_hl_client()["signer_address"])


def get_client_state() -> Dict[str, Any]:
    try:
        return ensure_hl_client()
    except Exception:  # noqa: BLE001
        return dict(_CLIENT_STATE)
