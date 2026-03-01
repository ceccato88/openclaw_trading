from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hl_client import get_exchange, get_info, get_wallet_address, logger, refresh_hl_client

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
TRADES_DIR = STATE_DIR / "trades"
PENDING_ENTRIES_DIR = STATE_DIR / "pending_entries"
LOCKS_DIR = STATE_DIR / "locks"
RISK_STATE_FILE = STATE_DIR / "daily_risk_state.json"
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 1.0
LOCK_STALE_SECONDS = 900
MIN_PERP_TRADE_NOTIONAL_USD = 10.0
CORRELATION_GROUPS = {
    "BTC": "majors",
    "ETH": "majors",
    "SOL": "majors",
    "BNB": "majors",
    "AVAX": "majors",
    "SUI": "majors",
    "ARB": "beta_l2",
    "OP": "beta_l2",
    "STRK": "beta_l2",
    "TIA": "beta_l1",
    "SEI": "beta_l1",
    "APT": "beta_l1",
    "WIF": "memes",
    "PEPE": "memes",
    "BONK": "memes",
    "DOGE": "memes",
}


def ensure_state_dirs() -> None:
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_ENTRIES_DIR.mkdir(parents=True, exist_ok=True)
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)


def get_env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default
    return float(raw_value)


def trade_state_path(coin: str) -> Path:
    ensure_state_dirs()
    return TRADES_DIR / f"{coin}.json"


def pending_entry_state_path(coin: str) -> Path:
    ensure_state_dirs()
    return PENDING_ENTRIES_DIR / f"{coin}.json"


def lock_file_path(lock_name: str) -> Path:
    ensure_state_dirs()
    return LOCKS_DIR / f"{lock_name}.lock"


def with_retry(
    fn,
    retries: int = DEFAULT_RETRY_ATTEMPTS,
    delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    action_label: str = "api_call",
):
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries:
                break
            sleep_for = delay_seconds * (2 ** (attempt - 1))
            try:
                refresh_hl_client()
            except Exception as refresh_error:  # noqa: BLE001
                logger.warning("Falha ao refrescar clientes Hyperliquid após erro em %s: %s", action_label, refresh_error)
            logger.warning(
                f"{action_label} falhou na tentativa {attempt}/{retries}: {exc}. Novo retry em {sleep_for:.1f}s."
            )
            time.sleep(sleep_for)
    raise last_error if last_error else RuntimeError(f"{action_label} falhou sem exceção.")


def acquire_file_lock(lock_name: str) -> Optional[Path]:
    lock_path = lock_file_path(lock_name)
    if lock_path.exists():
        lock_age_seconds = time.time() - lock_path.stat().st_mtime
        if lock_age_seconds > LOCK_STALE_SECONDS:
            logger.warning(f"Lock stale detetado em {lock_path.name}; a remover lock antigo.")
            lock_path.unlink(missing_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        return lock_path
    except FileExistsError:
        return None


def release_file_lock(lock_path: Optional[Path]) -> None:
    if lock_path and lock_path.exists():
        lock_path.unlink()


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)
    return path


def load_trade_state(coin: str) -> Optional[Dict[str, Any]]:
    path = trade_state_path(coin)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_trade_state(coin: str, payload: Dict[str, Any]) -> Path:
    path = trade_state_path(coin)
    return atomic_write_json(path, payload)


def save_pending_entry_state(coin: str, payload: Dict[str, Any]) -> Path:
    path = pending_entry_state_path(coin)
    return atomic_write_json(path, payload)


def delete_trade_state(coin: str) -> None:
    path = trade_state_path(coin)
    if path.exists():
        path.unlink()


def delete_pending_entry_state(coin: str) -> None:
    path = pending_entry_state_path(coin)
    if path.exists():
        path.unlink()


def load_pending_entry_state(coin: str) -> Optional[Dict[str, Any]]:
    path = pending_entry_state_path(coin)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def list_trade_states() -> List[Dict[str, Any]]:
    ensure_state_dirs()
    states = []
    for path in TRADES_DIR.glob("*.json"):
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload.setdefault("coin", path.stem)
        states.append(payload)
    return states


def list_pending_entry_states() -> List[Dict[str, Any]]:
    ensure_state_dirs()
    states = []
    for path in PENDING_ENTRIES_DIR.glob("*.json"):
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload.setdefault("coin", path.stem)
        states.append(payload)
    return states


def ensure_exchange_ok(response: Optional[Dict[str, Any]], action_label: str) -> Optional[Dict[str, Any]]:
    if response is None:
        raise RuntimeError(f"Nenhuma resposta recebida para {action_label}.")

    if response.get("status") != "ok":
        raise RuntimeError(f"{action_label} falhou: {response}")

    statuses = response.get("response", {}).get("data", {}).get("statuses", [])
    if not statuses:
        return None

    first_status = statuses[0]
    if "error" in first_status:
        raise RuntimeError(f"{action_label} rejeitado: {first_status['error']}")
    return first_status


def get_user_state() -> Dict[str, Any]:
    info = get_info()
    wallet_address = get_wallet_address()
    return with_retry(lambda: info.user_state(wallet_address), action_label="user_state")


def get_account_mode() -> str:
    info = get_info()
    wallet_address = get_wallet_address()
    raw_mode = with_retry(
        lambda: info.query_user_abstraction_state(wallet_address),
        action_label="query_user_abstraction_state",
    )
    if isinstance(raw_mode, str):
        return raw_mode
    if isinstance(raw_mode, dict):
        return str(raw_mode.get("accountType") or raw_mode.get("mode") or "standard")
    return "standard"


def get_spot_user_state() -> Dict[str, Any]:
    info = get_info()
    wallet_address = get_wallet_address()
    return with_retry(lambda: info.spot_user_state(wallet_address), action_label="spot_user_state")


def get_spot_balance_snapshot(spot_user_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    spot_user_state = spot_user_state or {"balances": [], "tokenToAvailableAfterMaintenance": []}
    available_by_token = {
        int(token): float(available)
        for token, available in spot_user_state.get("tokenToAvailableAfterMaintenance", [])
    }

    balances = []
    for balance in spot_user_state.get("balances", []):
        token = int(balance.get("token", -1))
        normalized_balance = {
            "coin": balance.get("coin"),
            "token": token,
            "total": float(balance.get("total", 0.0) or 0.0),
            "hold": float(balance.get("hold", 0.0) or 0.0),
            "entry_ntl": float(balance.get("entryNtl", 0.0) or 0.0),
            "available_after_maintenance": available_by_token.get(token, 0.0),
        }
        if any(
            normalized_balance[field] != 0.0
            for field in ("total", "hold", "entry_ntl", "available_after_maintenance")
        ):
            balances.append(normalized_balance)

    usdc_balance = next((balance for balance in balances if balance.get("coin") == "USDC"), None)
    return {
        "balances": balances,
        "usdc_total": usdc_balance["total"] if usdc_balance else 0.0,
        "usdc_available": usdc_balance["available_after_maintenance"] if usdc_balance else 0.0,
    }


def get_account_equity_snapshot() -> Dict[str, Any]:
    user_state = get_user_state()
    account_mode = get_account_mode()
    margin_summary = user_state.get("marginSummary", {})
    perp_equity = float(margin_summary.get("accountValue", 0.0) or 0.0)
    perp_withdrawable = float(user_state.get("withdrawable", 0.0) or 0.0)

    spot_user_state: Optional[Dict[str, Any]] = None
    spot_snapshot = {"balances": [], "usdc_total": 0.0, "usdc_available": 0.0}
    if account_mode == "unifiedAccount":
        spot_user_state = get_spot_user_state()
        spot_snapshot = get_spot_balance_snapshot(spot_user_state)

    if account_mode == "unifiedAccount":
        total_equity = max(perp_equity, spot_snapshot["usdc_total"])
        tradeable_equity = max(
            spot_snapshot["usdc_available"],
            spot_snapshot["usdc_total"],
            perp_withdrawable,
            perp_equity,
        )
        withdrawable = max(spot_snapshot["usdc_available"], perp_withdrawable)
    else:
        total_equity = perp_equity
        tradeable_equity = max(perp_withdrawable, perp_equity)
        withdrawable = perp_withdrawable

    return {
        "account_mode": account_mode,
        "user_state": user_state,
        "spot_user_state": spot_user_state,
        "spot_balances": spot_snapshot["balances"],
        "spot_usdc_total": spot_snapshot["usdc_total"],
        "spot_usdc_available": spot_snapshot["usdc_available"],
        "perp_equity": perp_equity,
        "perp_withdrawable": perp_withdrawable,
        "total_equity": total_equity,
        "tradeable_equity": tradeable_equity,
        "withdrawable": withdrawable,
    }


def extract_fill_details(response: Dict[str, Any], action_label: str) -> Dict[str, Any]:
    status = ensure_exchange_ok(response, action_label)
    if not status or "filled" not in status:
        raise RuntimeError(f"{action_label} não foi executado imediatamente: {status}")

    filled = status["filled"]
    filled_size = filled.get("totalSz") or filled.get("sz")
    if filled_size is None:
        raise RuntimeError(f"{action_label} retornou fill sem tamanho: {filled}")

    return {
        "avg_px": float(filled["avgPx"]),
        "size": float(filled_size),
        "oid": filled.get("oid"),
        "raw": filled,
    }


def get_meta_context() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    info = get_info()
    meta, asset_contexts = with_retry(info.meta_and_asset_ctxs, action_label="meta_and_asset_ctxs")
    return meta["universe"], asset_contexts


def get_asset_context(coin: str) -> Tuple[float, Dict[str, Any], Dict[str, Any]]:
    universe, asset_contexts = get_meta_context()
    for idx, asset in enumerate(universe):
        if asset["name"] != coin:
            continue

        context = asset_contexts[idx]
        current_price = context.get("markPx") or context.get("midPx") or context.get("oraclePx")
        if current_price is None:
            raise RuntimeError(f"A Hyperliquid não retornou preço utilizável para {coin}.")
        return float(current_price), asset, context

    raise RuntimeError(f"Moeda {coin} não encontrada na Hyperliquid.")


def round_price_for_order(coin: str, price: float) -> float:
    rounded_significant = float(f"{price:.5g}")
    _current_price, asset_info, _asset_context = get_asset_context(coin)
    sz_decimals = int(asset_info["szDecimals"])
    decimals = 6 - sz_decimals
    return round(rounded_significant, max(decimals, 0))


def compute_protection_prices(
    coin: str,
    entry_price: float,
    risk_pct: float,
    reward_pct: float,
    is_long: bool,
) -> Dict[str, float]:
    if is_long:
        stop_price = entry_price * (1 - (risk_pct / 100))
        take_profit_price = entry_price * (1 + (reward_pct / 100))
    else:
        stop_price = entry_price * (1 + (risk_pct / 100))
        take_profit_price = entry_price * (1 - (reward_pct / 100))

    return {
        "sl": round_price_for_order(coin, stop_price),
        "tp": round_price_for_order(coin, take_profit_price),
    }


def calculate_risk_based_notional(
    equity: float,
    risk_budget_pct: float,
    stop_distance_pct: float,
    minimum_notional_usd: float = MIN_PERP_TRADE_NOTIONAL_USD,
) -> float:
    if equity <= 0:
        raise ValueError("Equity inválida para cálculo de posição.")
    if risk_budget_pct <= 0:
        raise ValueError("risk_budget_pct deve ser maior que zero.")
    if stop_distance_pct <= 0:
        raise ValueError("stop_distance_pct deve ser maior que zero.")

    max_loss_usd = equity * (risk_budget_pct / 100)
    notional = max_loss_usd / (stop_distance_pct / 100)
    return max(notional, minimum_notional_usd)


def validate_perp_order_notional(
    coin: str,
    size: float,
    price: float,
    min_notional_usd: float = MIN_PERP_TRADE_NOTIONAL_USD,
) -> Optional[str]:
    order_notional = abs(size * price)
    if order_notional < min_notional_usd:
        return (
            f"Ordem em {coin} abaixo do nocional mínimo de ${min_notional_usd:.2f}: "
            f"${order_notional:.4f}."
        )
    return None


def is_protection_order(order: Dict[str, Any]) -> bool:
    order_type = str(order.get("orderType") or "").strip().lower()
    has_reduce_only = bool(order.get("reduceOnly"))
    has_position_tpsl_flag = bool(order.get("isPositionTpsl"))
    has_trigger_flag = bool(order.get("isTrigger"))
    has_trigger_price = order.get("triggerPx") is not None
    looks_like_tpsl_type = any(
        token in order_type
        for token in ("stop", "take profit", "tpsl")
    )

    return any(
        (
            has_reduce_only,
            has_position_tpsl_flag,
            has_trigger_flag,
            has_trigger_price,
            looks_like_tpsl_type,
        )
    )


def protection_order_kind(order: Dict[str, Any]) -> Optional[str]:
    if not is_protection_order(order):
        return None

    order_type = str(order.get("orderType") or "").strip().lower()
    if "take profit" in order_type:
        return "tp"
    if "stop" in order_type:
        return "sl"
    return None


def get_active_protection_levels(coin: str) -> Dict[str, Any]:
    protection_orders = []
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_order: Optional[Dict[str, Any]] = None
    tp_order: Optional[Dict[str, Any]] = None

    for order in get_frontend_open_orders(coin):
        if not is_protection_order(order):
            continue

        trigger_price_raw = order.get("triggerPx")
        limit_price_raw = order.get("limitPx")
        price = None
        if trigger_price_raw is not None:
            price = float(trigger_price_raw)
        elif limit_price_raw is not None:
            price = float(limit_price_raw)

        kind = protection_order_kind(order)
        if kind == "sl" and price is not None:
            sl_price = price
            sl_order = order
        elif kind == "tp" and price is not None:
            tp_price = price
            tp_order = order

        protection_orders.append(order)

    return {
        "coin": coin,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "sl_order": sl_order,
        "tp_order": tp_order,
        "orders": protection_orders,
    }


def cancel_orders_for_coin(coin: str, only_reduce_only: bool = False) -> List[int]:
    get_info()
    exchange = get_exchange()
    get_wallet_address()

    canceled_oids: List[int] = []
    seen_oids = set()
    open_orders = get_frontend_open_orders()

    for order in open_orders:
        if order.get("coin") != coin:
            continue

        oid = order.get("oid")
        if oid is None or oid in seen_oids:
            continue

        if only_reduce_only:
            if not is_protection_order(order):
                continue

        seen_oids.add(oid)

    cancel_requests = [{"coin": coin, "oid": oid} for oid in seen_oids]
    bulk_cancel = getattr(exchange, "bulk_cancel", None)
    if len(cancel_requests) > 1 and callable(bulk_cancel):
        try:
            response = with_retry(
                lambda: bulk_cancel(cancel_requests),
                action_label=f"bulk_cancel {coin}",
            )
            if response is None or response.get("status") != "ok":
                raise RuntimeError(f"bulk_cancel falhou: {response}")
            return [request["oid"] for request in cancel_requests]
        except Exception as bulk_error:  # noqa: BLE001
            logger.warning(f"Falha no cancelamento em lote de {coin}, a fazer fallback para cancelamento individual: {bulk_error}")

    for oid in seen_oids:
        cancel_response = with_retry(lambda oid=oid: exchange.cancel(coin, oid), action_label=f"cancelar ordem {oid} de {coin}")
        ensure_exchange_ok(cancel_response, f"cancelar ordem {oid} de {coin}")
        canceled_oids.append(oid)

    return canceled_oids


def get_frontend_open_orders(coin: Optional[str] = None) -> List[Dict[str, Any]]:
    info = get_info()
    wallet_address = get_wallet_address()
    orders = with_retry(lambda: info.frontend_open_orders(wallet_address), action_label="frontend_open_orders")
    if coin is None:
        return orders
    return [order for order in orders if order.get("coin") == coin]


def _submit_trigger_order(coin: str, exit_is_buy: bool, size: float, trigger_price: float, tpsl: str) -> Dict[str, Any]:
    exchange = get_exchange()
    rounded_price = round_price_for_order(coin, trigger_price)
    response = with_retry(
        lambda: exchange.order(
            coin,
            exit_is_buy,
            size,
            rounded_price,
            {"trigger": {"isMarket": True, "triggerPx": rounded_price, "tpsl": tpsl}},
            reduce_only=True,
        ),
        action_label=f"ordem de proteção {tpsl.upper()} para {coin}",
    )
    status = ensure_exchange_ok(response, f"ordem de proteção {tpsl.upper()} para {coin}")
    resting = status.get("resting", {}) if status else {}
    return {
        "price": rounded_price,
        "oid": resting.get("oid"),
        "response": response,
        "status": status,
    }


def _modify_trigger_order(
    oid: int,
    coin: str,
    exit_is_buy: bool,
    size: float,
    trigger_price: float,
    tpsl: str,
) -> Dict[str, Any]:
    exchange = get_exchange()
    rounded_price = round_price_for_order(coin, trigger_price)
    response = with_retry(
        lambda: exchange.modify_order(
            oid,
            coin,
            exit_is_buy,
            size,
            rounded_price,
            {"trigger": {"isMarket": True, "triggerPx": rounded_price, "tpsl": tpsl}},
            reduce_only=True,
        ),
        action_label=f"modificar ordem de proteção {tpsl.upper()} para {coin}",
    )
    status = ensure_exchange_ok(response, f"modificar ordem de proteção {tpsl.upper()} para {coin}")
    resting = status.get("resting", {}) if status else {}
    return {
        "price": rounded_price,
        "oid": resting.get("oid", oid),
        "response": response,
        "status": status,
    }


def place_trade_protection(coin: str, is_long: bool, size: float, stop_loss_price: float, take_profit_price: float) -> Dict[str, Any]:
    exit_is_buy = not is_long
    stop_order = _submit_trigger_order(coin, exit_is_buy, size, stop_loss_price, "sl")
    take_profit_order = _submit_trigger_order(coin, exit_is_buy, size, take_profit_price, "tp")
    return {"sl": stop_order, "tp": take_profit_order}


def upsert_trade_protection(
    coin: str,
    is_long: bool,
    size: float,
    stop_loss_price: float,
    take_profit_price: float,
    active_protection: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    active_protection = active_protection or get_active_protection_levels(coin)
    exit_is_buy = not is_long
    sl_order = active_protection.get("sl_order")
    tp_order = active_protection.get("tp_order")

    if sl_order and sl_order.get("oid") is not None:
        stop_result = _modify_trigger_order(int(sl_order["oid"]), coin, exit_is_buy, size, stop_loss_price, "sl")
    else:
        stop_result = _submit_trigger_order(coin, exit_is_buy, size, stop_loss_price, "sl")

    if tp_order and tp_order.get("oid") is not None:
        tp_result = _modify_trigger_order(int(tp_order["oid"]), coin, exit_is_buy, size, take_profit_price, "tp")
    else:
        tp_result = _submit_trigger_order(coin, exit_is_buy, size, take_profit_price, "tp")

    return {"sl": stop_result, "tp": tp_result}


def place_limit_entry_order(coin: str, is_buy: bool, size: float, limit_price: float) -> Dict[str, Any]:
    exchange = get_exchange()
    rounded_price = round_price_for_order(coin, limit_price)
    response = with_retry(
        lambda: exchange.order(coin, is_buy, size, rounded_price, {"limit": {"tif": "Gtc"}}, reduce_only=False),
        action_label=f"ordem de entrada limitada para {coin}",
    )
    status = ensure_exchange_ok(response, f"ordem de entrada limitada para {coin}")
    return {"price": rounded_price, "response": response, "status": status}


def get_open_position(coin: str) -> Optional[Dict[str, Any]]:
    user_state = get_user_state()
    for position in user_state.get("assetPositions", []):
        position_data = position["position"]
        if position_data["coin"] == coin and float(position_data["szi"]) != 0:
            return position_data
    return None


def get_open_positions() -> List[Dict[str, Any]]:
    user_state = get_user_state()
    positions = []
    for wrapped_position in user_state.get("assetPositions", []):
        position_data = wrapped_position["position"]
        if float(position_data["szi"]) != 0:
            positions.append(position_data)
    return positions


def market_close_position(coin: str, size: Optional[float] = None) -> Dict[str, Any]:
    exchange = get_exchange()
    if size is None:
        return with_retry(lambda: exchange.market_close(coin), action_label=f"market_close {coin}")
    return with_retry(lambda: exchange.market_close(coin, sz=size), action_label=f"market_close {coin}")


def update_exchange_leverage(leverage: int, coin: str, is_cross: bool) -> Dict[str, Any]:
    exchange = get_exchange()
    return with_retry(
        lambda: exchange.update_leverage(leverage, coin, is_cross=is_cross),
        action_label=f"update_leverage {coin}",
    )


def estimate_position_risk(
    position: Dict[str, Any],
    trade_state: Optional[Dict[str, Any]] = None,
    default_risk_pct: float = 2.0,
) -> float:
    coin = str(position.get("coin", ""))
    size = abs(float(position["szi"]))
    entry_price = float(position["entryPx"]) if position.get("entryPx") is not None else 0.0
    position_value = abs(float(position.get("positionValue", 0.0)))

    if coin:
        active_protection = get_active_protection_levels(coin)
        active_stop = active_protection.get("sl_price")
        if active_stop is not None and entry_price > 0:
            return abs(entry_price - float(active_stop)) * size

    if trade_state and trade_state.get("sl") is not None:
        state_entry = float(trade_state.get("entry", entry_price) or entry_price)
        return abs(state_entry - float(trade_state["sl"])) * size

    if entry_price > 0:
        stop_distance = entry_price * (default_risk_pct / 100)
        return stop_distance * size

    return position_value * (default_risk_pct / 100)


def estimate_pending_entry_risk(pending_entry: Dict[str, Any]) -> float:
    planned_size = abs(float(pending_entry.get("planned_size", 0.0) or 0.0))
    entry_price = float(pending_entry.get("entry_limit_price", 0.0) or 0.0)
    side = str(pending_entry.get("side", "")).upper()
    risk_pct = float(pending_entry.get("risk_pct", 0.0) or 0.0)
    planned_stop = pending_entry.get("planned_stop")

    if planned_size == 0 or entry_price == 0:
        return 0.0

    if planned_stop is not None:
        stop_price = float(planned_stop)
    elif risk_pct > 0 and side in {"LONG", "SHORT"}:
        stop_price = entry_price * (1 - (risk_pct / 100)) if side == "LONG" else entry_price * (1 + (risk_pct / 100))
    else:
        return 0.0

    return abs(entry_price - stop_price) * planned_size


def has_pending_entry(coin: str) -> bool:
    return load_pending_entry_state(coin) is not None


def correlation_group_for_coin(coin: str) -> str:
    normalized = str(coin).upper()
    return CORRELATION_GROUPS.get(normalized, normalized)


def check_portfolio_heat(
    new_coin: Optional[str] = None,
    new_side: Optional[str] = None,
    planned_entry: Optional[float] = None,
    planned_size: Optional[float] = None,
    planned_stop: Optional[float] = None,
    max_total_risk_pct: float = 6.0,
    max_positions_per_side: int = 2,
    max_correlated_positions_per_side: int = 1,
) -> Dict[str, Any]:
    account_snapshot = get_account_equity_snapshot()
    equity = account_snapshot["tradeable_equity"]
    open_positions = get_open_positions()
    trade_states = {state["coin"]: state for state in list_trade_states()}
    pending_entries = list_pending_entry_states()

    total_open_risk = sum(estimate_position_risk(position, trade_states.get(position["coin"])) for position in open_positions)
    total_pending_risk = sum(estimate_pending_entry_risk(pending) for pending in pending_entries)
    long_exposure = sum(1 for position in open_positions if float(position["szi"]) > 0)
    short_exposure = sum(1 for position in open_positions if float(position["szi"]) < 0)
    correlated_long_exposure = 0
    correlated_short_exposure = 0
    new_group = correlation_group_for_coin(new_coin) if new_coin else None

    for pending in pending_entries:
        pending_side = pending.get("side")
        if pending_side == "LONG":
            long_exposure += 1
        elif pending_side == "SHORT":
            short_exposure += 1

    if new_group:
        correlated_long_exposure = sum(
            1
            for position in open_positions
            if float(position["szi"]) > 0 and correlation_group_for_coin(position["coin"]) == new_group
        )
        correlated_short_exposure = sum(
            1
            for position in open_positions
            if float(position["szi"]) < 0 and correlation_group_for_coin(position["coin"]) == new_group
        )
        for pending in pending_entries:
            pending_coin = pending.get("coin")
            if not pending_coin or correlation_group_for_coin(pending_coin) != new_group:
                continue
            if pending.get("side") == "LONG":
                correlated_long_exposure += 1
            elif pending.get("side") == "SHORT":
                correlated_short_exposure += 1

    new_risk = 0.0
    if planned_entry is not None and planned_size is not None and planned_stop is not None:
        new_risk = abs(planned_entry - planned_stop) * planned_size

    total_risk_after = total_open_risk + total_pending_risk + new_risk
    max_allowed_risk = equity * (max_total_risk_pct / 100)

    if new_side == "LONG" and long_exposure >= max_positions_per_side:
        return {
            "can_trade": False,
            "message": "Exposição direcional LONG no máximo.",
            "account_mode": account_snapshot["account_mode"],
            "equity": equity,
            "total_open_risk": total_open_risk,
            "total_pending_risk": total_pending_risk,
            "planned_risk": new_risk,
            "total_risk_after": total_risk_after,
            "max_allowed_risk": max_allowed_risk,
            "long_exposure": long_exposure,
            "short_exposure": short_exposure,
        }

    if new_side == "SHORT" and short_exposure >= max_positions_per_side:
        return {
            "can_trade": False,
            "message": "Exposição direcional SHORT no máximo.",
            "account_mode": account_snapshot["account_mode"],
            "equity": equity,
            "total_open_risk": total_open_risk,
            "total_pending_risk": total_pending_risk,
            "planned_risk": new_risk,
            "total_risk_after": total_risk_after,
            "max_allowed_risk": max_allowed_risk,
            "long_exposure": long_exposure,
            "short_exposure": short_exposure,
        }

    if new_group and new_side == "LONG" and correlated_long_exposure >= max_correlated_positions_per_side:
        return {
            "can_trade": False,
            "message": f"Exposição correlacionada LONG no grupo {new_group} no máximo.",
            "account_mode": account_snapshot["account_mode"],
            "equity": equity,
            "total_open_risk": total_open_risk,
            "total_pending_risk": total_pending_risk,
            "planned_risk": new_risk,
            "total_risk_after": total_risk_after,
            "max_allowed_risk": max_allowed_risk,
            "long_exposure": long_exposure,
            "short_exposure": short_exposure,
            "correlation_group": new_group,
            "correlated_long_exposure": correlated_long_exposure,
            "correlated_short_exposure": correlated_short_exposure,
        }

    if new_group and new_side == "SHORT" and correlated_short_exposure >= max_correlated_positions_per_side:
        return {
            "can_trade": False,
            "message": f"Exposição correlacionada SHORT no grupo {new_group} no máximo.",
            "account_mode": account_snapshot["account_mode"],
            "equity": equity,
            "total_open_risk": total_open_risk,
            "total_pending_risk": total_pending_risk,
            "planned_risk": new_risk,
            "total_risk_after": total_risk_after,
            "max_allowed_risk": max_allowed_risk,
            "long_exposure": long_exposure,
            "short_exposure": short_exposure,
            "correlation_group": new_group,
            "correlated_long_exposure": correlated_long_exposure,
            "correlated_short_exposure": correlated_short_exposure,
        }

    if total_risk_after >= max_allowed_risk:
        return {
            "can_trade": False,
            "message": f"Portfolio heat excedido: risco ${total_risk_after:.2f} >= limite ${max_allowed_risk:.2f}.",
            "account_mode": account_snapshot["account_mode"],
            "equity": equity,
            "total_open_risk": total_open_risk,
            "total_pending_risk": total_pending_risk,
            "planned_risk": new_risk,
            "total_risk_after": total_risk_after,
            "max_allowed_risk": max_allowed_risk,
            "long_exposure": long_exposure,
            "short_exposure": short_exposure,
            "correlation_group": new_group,
            "correlated_long_exposure": correlated_long_exposure,
            "correlated_short_exposure": correlated_short_exposure,
        }

    return {
        "can_trade": True,
        "message": "Portfolio heat dentro do limite.",
        "account_mode": account_snapshot["account_mode"],
        "equity": equity,
        "total_open_risk": total_open_risk,
        "total_pending_risk": total_pending_risk,
        "planned_risk": new_risk,
        "total_risk_after": total_risk_after,
        "max_allowed_risk": max_allowed_risk,
        "long_exposure": long_exposure,
        "short_exposure": short_exposure,
        "correlation_group": new_group,
        "correlated_long_exposure": correlated_long_exposure,
        "correlated_short_exposure": correlated_short_exposure,
    }
