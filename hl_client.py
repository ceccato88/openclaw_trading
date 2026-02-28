import logging
import os
from pathlib import Path

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

# Configuração global de registos (logs)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("HL_Client")


def _normalize_hex_value(raw_value):
    if not raw_value:
        return None
    return raw_value if raw_value.startswith("0x") else f"0x{raw_value}"


def _env_to_bool(name, default=False):
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def get_hl_client(testnet=None):
    """
    Inicializa a ligação com a Hyperliquid lendo a chave privada do ambiente.
    """
    private_key = _normalize_hex_value(os.environ.get("HYPERLIQUID_PRIVATE_KEY"))
    if not private_key:
        raise ValueError("A variável de ambiente 'HYPERLIQUID_PRIVATE_KEY' não está definida.")

    if testnet is None:
        testnet = _env_to_bool("HYPERLIQUID_TESTNET", default=False)

    try:
        account = Account.from_key(private_key)
        trading_address = _normalize_hex_value(os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS")) or account.address
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL

        info_client = Info(base_url, skip_ws=True)
        exchange_client = Exchange(account, base_url, account_address=trading_address)

        if trading_address != account.address:
            logger.info(f"A usar API wallet {account.address} para operar a conta {trading_address}.")

        return info_client, exchange_client, trading_address, account.address
    except Exception as e:
        logger.error(f"Erro ao inicializar clientes Hyperliquid: {e}")
        raise

# Instâncias prontas para serem importadas pelos outros scripts
try:
    info, exchange, wallet_address, signer_address = get_hl_client()
except Exception as e:
    logger.warning("Cliente Hyperliquid não inicializado. Verifique a chave privada.")
    info, exchange, wallet_address, signer_address = None, None, None, None

account_address = wallet_address
