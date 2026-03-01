from hl_client import get_exchange, logger
from skills.signals import evaluate_pullback_entry, get_market_regime
from skills.risk_manager import check_daily_drawdown
from skills.support import (
    acquire_file_lock,
    calculate_risk_based_notional,
    cancel_orders_for_coin,
    check_portfolio_heat,
    compute_protection_prices,
    delete_pending_entry_state,
    ensure_exchange_ok,
    extract_fill_details,
    get_account_equity_snapshot,
    get_asset_context,
    get_open_position,
    has_pending_entry,
    market_close_position,
    place_limit_entry_order,
    place_trade_protection,
    release_file_lock,
    save_pending_entry_state,
    save_trade_state,
    update_exchange_leverage,
    validate_perp_order_notional,
)

def execute_wolf_strategy_trade(
    coin: str,
    side: str,
    usdt_size: float,
    leverage: int = 10,
    risk_pct: float = 2.0,
    account_risk_pct: float = 1.0,
    reward_pct: float | None = None,
) -> dict:
    """
    Skill OpenClaw: Abre a posição e configura automaticamente o Stop Loss e o Take Profit (Relação 2:1).
    """
    normalized_side = side.upper()
    if normalized_side not in {"LONG", "SHORT"}:
        return {"status": "error", "message": "O parâmetro 'side' deve ser LONG ou SHORT."}

    if usdt_size <= 0:
        return {"status": "error", "message": "O tamanho financeiro deve ser maior do que zero."}

    if leverage <= 0:
        return {"status": "error", "message": "A alavancagem deve ser maior do que zero."}

    if risk_pct <= 0:
        return {"status": "error", "message": "O risco percentual deve ser maior do que zero."}

    if account_risk_pct <= 0:
        return {"status": "error", "message": "O risco de conta percentual deve ser maior do que zero."}

    if reward_pct is not None and reward_pct <= 0:
        return {"status": "error", "message": "O reward percentual deve ser maior do que zero."}

    is_buy = normalized_side == 'LONG'
    
    # 1. Verifica o drawdown diário antes de entrar na secção crítica protegida por lock.
    risk_check = check_daily_drawdown()
    if not risk_check["can_trade"]:
        return {"status": "blocked", "message": risk_check["message"]}

    logger.info(
        "A iniciar Trade: %s em %s | Notional mínimo: $%s | Alavancagem: %sx | Stop: %s%% | Risco da conta: %s%%",
        normalized_side,
        coin,
        usdt_size,
        leverage,
        risk_pct,
        account_risk_pct,
    )

    lock_path = acquire_file_lock(f"wolf-strategy-{coin}")
    if lock_path is None:
        return {"status": "blocked", "message": f"Execução concorrente detetada para {coin}. Tente novamente no próximo ciclo."}

    try:
        get_exchange()
        market_regime = get_market_regime()
        if market_regime["regime"] == "CHOP":
            return {"status": "blocked", "message": "Mercado em CHOP. Sem novas entradas."}

        if market_regime["regime"] == "BULL" and not is_buy:
            return {"status": "blocked", "message": "Regime BULL. Shorts bloqueados."}

        if market_regime["regime"] == "BEAR" and is_buy:
            return {"status": "blocked", "message": "Regime BEAR. Longs bloqueados."}

        current_price, asset_info, _ = get_asset_context(coin)
        sz_decimals = int(asset_info['szDecimals'])
        max_leverage = int(asset_info.get('maxLeverage', leverage))
        is_cross = not bool(asset_info.get('onlyIsolated', False))

        if leverage > max_leverage:
            return {"status": "error", "message": f"A moeda {coin} suporta no máximo {max_leverage}x."}

        existing_position = get_open_position(coin)
        if existing_position:
            return {"status": "error", "message": f"Já existe uma posição aberta em {coin}. Feche-a antes de abrir outra."}

        if has_pending_entry(coin):
            return {"status": "blocked", "message": f"Já existe uma ordem pendente em {coin}."}

        entry_setup = evaluate_pullback_entry(coin, normalized_side, interval="15m", lookback=40)
        if not entry_setup.get("triggered"):
            return {"status": "waiting_entry", "message": "Setup ainda não confirmou pullback na EMA21.", "entry_setup": entry_setup}

        planned_entry_price = float(entry_setup["entry_limit_price"])
        reward_pct = reward_pct if reward_pct is not None else risk_pct * 2.0
        planned_protection = compute_protection_prices(
            coin=coin,
            entry_price=planned_entry_price,
            risk_pct=risk_pct,
            reward_pct=reward_pct,
            is_long=is_buy,
        )
        planned_stop = planned_protection["sl"]
        stop_distance_pct = abs(planned_entry_price - planned_stop) / planned_entry_price * 100 if planned_entry_price > 0 else 0.0
        account_snapshot = get_account_equity_snapshot()
        tradeable_equity = float(account_snapshot["tradeable_equity"])

        leverage_response = update_exchange_leverage(leverage, coin, is_cross=is_cross)
        ensure_exchange_ok(leverage_response, f"ajuste de alavancagem para {coin}")

        planned_notional_usd = max(
            usdt_size,
            calculate_risk_based_notional(
                equity=tradeable_equity,
                risk_budget_pct=account_risk_pct,
                stop_distance_pct=stop_distance_pct,
            ),
        )
        size_in_coins = planned_notional_usd / planned_entry_price
        size_formatted = round(size_in_coins, sz_decimals)
        
        if size_formatted == 0:
            return {"status": "error", "message": f"Tamanho muito pequeno para os decimais da moeda {coin}."}

        notional_error = validate_perp_order_notional(coin, size_formatted, planned_entry_price)
        if notional_error:
            return {"status": "error", "message": notional_error}

        heat_check = check_portfolio_heat(
            new_coin=coin,
            new_side=normalized_side,
            planned_entry=planned_entry_price,
            planned_size=size_formatted,
            planned_stop=planned_stop,
        )
        if not heat_check["can_trade"]:
            return {"status": "blocked", "message": heat_check["message"], "portfolio_heat": heat_check}

        # 2. Envia ordem limitada após confirmação do pullback
        order_result = place_limit_entry_order(coin, is_buy, size_formatted, planned_entry_price)
        order_res = order_result["response"]
        if order_res.get("status") != "ok":
            return {"status": "error", "message": f"Erro na ordem de entrada: {order_res}"}

        entry_status = order_result["status"]
        if "resting" in entry_status:
            entry_oid = entry_status["resting"]["oid"]
            save_pending_entry_state(coin, {
                "coin": coin,
                "side": normalized_side,
                "entry_oid": entry_oid,
                "entry_limit_price": order_result["price"],
                "planned_size": size_formatted,
                "planned_notional_usd": planned_notional_usd,
                "planned_stop": planned_stop,
                "risk_pct": risk_pct,
                "account_risk_pct": account_risk_pct,
                "reward_pct": reward_pct,
                "leverage": leverage,
            })
            logger.info(f"⏳ Ordem limitada em espera para {coin}. OID={entry_oid}")
            return {
                "status": "pending_entry",
                "coin": coin,
                "entry_oid": entry_oid,
                "entry_limit_price": order_result["price"],
                "planned_size": size_formatted,
                "planned_notional_usd": planned_notional_usd,
            }

        logger.info("✅ Posição aberta com sucesso via limit order. A calcular os alvos (2:1)...")

        fill = extract_fill_details(order_res, f"ordem de entrada de {coin}")
        entry_price = fill["avg_px"]
        filled_size = fill["size"]
        save_trade_state(coin, {
            "coin": coin,
            "entry": entry_price,
            "filled_size": filled_size,
            "risk_pct": risk_pct,
            "account_risk_pct": account_risk_pct,
            "reward_pct": reward_pct,
            "breakeven_done": False,
            "management_stage": "filled_awaiting_protection",
            "status": "filled_awaiting_protection",
        })
        protection_prices = compute_protection_prices(
            coin=coin,
            entry_price=entry_price,
            risk_pct=risk_pct,
            reward_pct=reward_pct,
            is_long=is_buy,
        )
        sl_price = protection_prices["sl"]
        tp_price = protection_prices["tp"]

        try:
            protection = place_trade_protection(coin, is_buy, filled_size, sl_price, tp_price)
        except Exception as protection_error:
            logger.error(f"Falha ao plantar proteção em {coin}: {protection_error}")
            recovery = "not_attempted"

            try:
                cancel_orders_for_coin(coin, only_reduce_only=True)
                emergency_close = market_close_position(coin, size=filled_size)
                extract_fill_details(emergency_close, f"fecho de emergência de {coin}")
                recovery = "position_closed_at_market"
            except Exception as emergency_error:
                recovery = f"emergency_close_failed: {emergency_error}"
                logger.error(f"Falha no fecho de emergência em {coin}: {emergency_error}")

            return {
                "status": "error",
                "message": f"Posição aberta mas a proteção falhou: {protection_error}",
                "recovery": recovery,
            }

        logger.info(
            f"🎯 Ordem Completa: Entrada=${entry_price} | SL=${protection['sl']['price']} (-{risk_pct}%) | "
            f"TP=${protection['tp']['price']} (+{reward_pct}%)"
        )

        # 6. Guarda o estado para a função de Breakeven poder ler depois
        save_trade_state(coin, {
            "coin": coin,
            "entry": entry_price,
            "filled_size": filled_size,
            "tp": protection["tp"]["price"],
            "sl": protection["sl"]["price"],
            "risk_pct": risk_pct,
            "account_risk_pct": account_risk_pct,
            "reward_pct": reward_pct,
            "breakeven_done": False,
            "management_stage": "initial_risk",
            "status": "protected",
        })
        delete_pending_entry_state(coin)

        return {
            "status": "success",
            "data": order_res,
            "entry": entry_price,
            "filled_size": filled_size,
            "tp": protection["tp"]["price"],
            "sl": protection["sl"]["price"],
            "protection": protection,
            "entry_setup": entry_setup,
            "portfolio_heat": heat_check,
            "planned_notional_usd": planned_notional_usd,
            "account_risk_pct": account_risk_pct,
        }

    except Exception as e:
        logger.error(f"Erro na execução da estratégia: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        release_file_lock(lock_path)
