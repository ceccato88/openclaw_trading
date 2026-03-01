from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from hl_client import get_info, logger
from skills.support import with_retry

INTERVAL_TO_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

REGIME_CACHE_TTL_SECONDS = 300
DEFAULT_REGIME_LOOKBACK = 400
REGIME_CONFIRMATION_CANDLES = 3
REGIME_EMA50_DEADBAND_PCT = 0.002
MIN_DIRECTIONAL_CONVICTION = 0.25
MIN_SIGNAL_ATR_PCT = 0.0025
ATR_QUALITY_CAP_PCT = 0.015
MAX_ENTRY_ATR_PCT = 0.02
HIGHER_TIMEFRAME_LEVEL_LOOKBACK = 24
HIGHER_TIMEFRAME_LEVEL_BUFFER_PCT = 0.003
TRAILING_BREAKEVEN_PROGRESS = 0.50
TRAILING_PROFIT_LOCK_50_PROGRESS = 0.80
TRAILING_PROFIT_LOCK_75_PROGRESS = 0.95
TRAILING_PROGRESS_EPSILON = 1e-9
SCORE_WEIGHTS = {
    "volume": 0.25,
    "structure": 0.20,
    "funding": 0.35,
    "divergence": 0.20,
}
_REGIME_CACHE: Dict[tuple[str, str, int], Dict[str, Any]] = {}
BTC_FAMILY = {"BTC", "WBTC"}


def coin_trade_profile(coin: str) -> Dict[str, float | int | str]:
    normalized_coin = coin.upper()
    if normalized_coin in BTC_FAMILY:
        return {
            "class": "btc",
            "min_conviction": 0.25,
            "max_entry_atr_pct": 0.018,
            "level_buffer_pct": 0.0035,
            "ema_gap_min_pct": 0.0025,
            "ema200_buffer_pct": 0.0015,
            "confirmation_window": 2,
            "proximity_cap_pct": 0.012,
        }
    return {
        "class": "alt",
        "min_conviction": 0.35,
        "max_entry_atr_pct": 0.014,
        "level_buffer_pct": 0.006,
        "ema_gap_min_pct": 0.005,
        "ema200_buffer_pct": 0.003,
        "confirmation_window": 3,
        "proximity_cap_pct": 0.01,
    }


def now_ms() -> int:
    return int(time.time() * 1000)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def fetch_candles(
    coin: str,
    interval: str = "15m",
    lookback: int = 60,
    end_time_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    info = get_info()
    if interval not in INTERVAL_TO_MS:
        raise ValueError(f"Intervalo {interval} não suportado.")

    end_time_ms = end_time_ms or now_ms()
    start_time_ms = end_time_ms - (lookback * INTERVAL_TO_MS[interval])
    candles = with_retry(
        lambda: info.candles_snapshot(coin, interval, start_time_ms, end_time_ms),
        action_label=f"candles {coin} {interval}",
    )
    return sorted(candles, key=lambda candle: candle["t"])


def _float_series(candles: Sequence[Dict[str, Any]], key: str) -> List[float]:
    return [float(candle[key]) for candle in candles]


def candle_opens(candles: Sequence[Dict[str, Any]]) -> List[float]:
    return _float_series(candles, "o")


def candle_highs(candles: Sequence[Dict[str, Any]]) -> List[float]:
    return _float_series(candles, "h")


def candle_lows(candles: Sequence[Dict[str, Any]]) -> List[float]:
    return _float_series(candles, "l")


def candle_closes(candles: Sequence[Dict[str, Any]]) -> List[float]:
    return _float_series(candles, "c")


def candle_volumes(candles: Sequence[Dict[str, Any]]) -> List[float]:
    return _float_series(candles, "v")


def calculate_ema(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []

    alpha = 2 / (period + 1)
    ema_values = [float(values[0])]

    for value in values[1:]:
        ema_values.append((float(value) - ema_values[-1]) * alpha + ema_values[-1])

    return ema_values


def calculate_rsi(values: Sequence[float], period: int = 14) -> List[float]:
    if not values:
        return []

    if len(values) < 2:
        return [50.0 for _ in values]

    prices = [float(value) for value in values]
    deltas = [prices[idx] - prices[idx - 1] for idx in range(1, len(prices))]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [abs(min(delta, 0.0)) for delta in deltas]
    rsi_values = [50.0 for _ in prices]

    if len(deltas) < period:
        return rsi_values

    def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0 and avg_gain == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi_values[period] = _rsi_from_averages(avg_gain, avg_loss)

    for idx in range(period + 1, len(prices)):
        gain = gains[idx - 1]
        loss = losses[idx - 1]
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        rsi_values[idx] = _rsi_from_averages(avg_gain, avg_loss)

    return rsi_values


def calculate_signed_volume_ratio(candles: Sequence[Dict[str, Any]]) -> float:
    if not candles:
        return 0.0

    signed_volume = 0.0
    total_volume = 0.0

    for candle in candles:
        candle_open = float(candle["o"])
        candle_close = float(candle["c"])
        volume = float(candle["v"])

        if candle_close > candle_open:
            signed_volume += volume
        elif candle_close < candle_open:
            signed_volume -= volume
        total_volume += volume

    return signed_volume / total_volume if total_volume else 0.0


def calculate_atr(candles: Sequence[Dict[str, Any]], period: int = 14) -> float:
    if not candles:
        return 0.0

    true_ranges: List[float] = []
    previous_close: Optional[float] = None

    for candle in candles:
        high = float(candle["h"])
        low = float(candle["l"])
        close = float(candle["c"])
        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(true_range)
        previous_close = close

    if len(true_ranges) <= period:
        return sum(true_ranges) / max(len(true_ranges), 1)

    atr = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        atr = ((atr * (period - 1)) + true_range) / period
    return atr


def calculate_atr_pct(candles: Sequence[Dict[str, Any]], period: int = 14) -> float:
    if not candles:
        return 0.0
    current_close = float(candles[-1]["c"])
    if current_close == 0:
        return 0.0
    return calculate_atr(candles, period=period) / current_close


def trend_structure(highs: Sequence[float], lows: Sequence[float], n: int = 5) -> int:
    if len(highs) < n or len(lows) < n:
        return 0

    recent_highs = [float(value) for value in highs[-n:]]
    recent_lows = [float(value) for value in lows[-n:]]

    rising_highs = sum(1 for idx in range(1, n) if recent_highs[idx] > recent_highs[idx - 1])
    rising_lows = sum(1 for idx in range(1, n) if recent_lows[idx] > recent_lows[idx - 1])
    falling_highs = sum(1 for idx in range(1, n) if recent_highs[idx] < recent_highs[idx - 1])
    falling_lows = sum(1 for idx in range(1, n) if recent_lows[idx] < recent_lows[idx - 1])

    if rising_highs >= n - 1 and rising_lows >= n - 1:
        return 1
    if falling_highs >= n - 1 and falling_lows >= n - 1:
        return -1
    return 0


def funding_signal(
    funding_rate: float,
    regime: str,
    structure_bias: int,
    threshold: float = 0.001,
) -> Dict[str, Any]:
    if abs(funding_rate) < threshold:
        return {"bias": 0, "label": "NEUTRAL"}

    regime = regime.upper()
    if funding_rate < -threshold:
        if regime == "BULL" and structure_bias >= 0:
            return {"bias": 1, "label": "BULL_REGIME_NEGATIVE_FUNDING"}
        return {"bias": 0, "label": "NEGATIVE_FUNDING_IGNORED"}

    if funding_rate > threshold:
        if regime == "BEAR" and structure_bias <= 0:
            return {"bias": -1, "label": "BEAR_REGIME_POSITIVE_FUNDING"}
        return {"bias": 0, "label": "POSITIVE_FUNDING_IGNORED"}

    return {"bias": 0, "label": "NEUTRAL"}


def rsi_divergence(prices: Sequence[float], rsi_values: Sequence[float], lookback: int = 5) -> Dict[str, Any]:
    if len(prices) <= lookback or len(rsi_values) <= lookback:
        return {"bias": 0, "label": "NONE"}

    price_delta = float(prices[-1]) - float(prices[-lookback])
    rsi_delta = float(rsi_values[-1]) - float(rsi_values[-lookback])

    if price_delta < 0 and rsi_delta > 0:
        return {"bias": 1, "label": "BULLISH_DIVERGENCE"}
    if price_delta > 0 and rsi_delta < 0:
        return {"bias": -1, "label": "BEARISH_DIVERGENCE"}
    return {"bias": 0, "label": "NONE"}


def _classify_regime_point(current_price: float, ema_50: float, ema_200: float, deadband_pct: float) -> str:
    if current_price > (ema_50 * (1 + deadband_pct)) and ema_50 > ema_200:
        return "BULL"
    if current_price < (ema_50 * (1 - deadband_pct)) and ema_50 < ema_200:
        return "BEAR"
    return "CHOP"


def determine_market_regime_from_prices(
    prices: Sequence[float],
    confirm_candles: int = REGIME_CONFIRMATION_CANDLES,
    deadband_pct: float = REGIME_EMA50_DEADBAND_PCT,
) -> Dict[str, Any]:
    minimum_history = max(200, 200 + max(confirm_candles - 1, 0))
    if len(prices) < minimum_history:
        return {
            "regime": "CHOP",
            "reason": "insufficient_history",
            "required_history": minimum_history,
            "available_history": len(prices),
        }

    normalized_prices = [float(price) for price in prices]
    ema_50_series = calculate_ema(normalized_prices, 50)
    ema_200_series = calculate_ema(normalized_prices, 200)
    start_idx = len(normalized_prices) - confirm_candles
    confirmations: List[Dict[str, float | str | int]] = []

    for idx in range(start_idx, len(normalized_prices)):
        current_price = normalized_prices[idx]
        ema_50 = float(ema_50_series[idx])
        ema_200 = float(ema_200_series[idx])
        point_regime = _classify_regime_point(current_price, ema_50, ema_200, deadband_pct)
        confirmations.append(
            {
                "index": idx,
                "price": current_price,
                "ema_50": ema_50,
                "ema_200": ema_200,
                "regime": point_regime,
            }
        )

    confirmation_labels = [str(item["regime"]) for item in confirmations]
    if all(label == "BULL" for label in confirmation_labels):
        regime = "BULL"
    elif all(label == "BEAR" for label in confirmation_labels):
        regime = "BEAR"
    else:
        regime = "CHOP"

    latest = confirmations[-1]
    return {
        "regime": regime,
        "current_price": float(latest["price"]),
        "ema_50": float(latest["ema_50"]),
        "ema_200": float(latest["ema_200"]),
        "confirm_candles": confirm_candles,
        "deadband_pct": deadband_pct * 100,
        "confirmation_path": confirmation_labels,
    }


def _compute_market_regime(coin: str = "BTC", interval: str = "1h", lookback: int = DEFAULT_REGIME_LOOKBACK) -> Dict[str, Any]:
    candles = fetch_candles(coin, interval=interval, lookback=lookback)
    candles_for_regime = candles[:-1] if len(candles) > 1 else candles
    closes = candle_closes(candles_for_regime)
    regime = determine_market_regime_from_prices(closes)
    regime.update(
        {
            "coin": coin,
            "interval": interval,
            "candles": len(candles_for_regime),
            "raw_candles": len(candles),
            "used_closed_candle": len(candles_for_regime) != len(candles),
            "last_closed_at": candles_for_regime[-1]["t"] if candles_for_regime else None,
        }
    )
    return regime


def get_market_regime(coin: str = "BTC", interval: str = "1h", lookback: int = DEFAULT_REGIME_LOOKBACK) -> Dict[str, Any]:
    cache_key = (coin, interval, lookback)
    cached = _REGIME_CACHE.get(cache_key)
    now = time.time()
    if cached and (now - cached["ts"]) < REGIME_CACHE_TTL_SECONDS:
        return dict(cached["data"])

    regime = _compute_market_regime(coin=coin, interval=interval, lookback=lookback)
    _REGIME_CACHE[cache_key] = {"data": regime, "ts": now}
    return dict(regime)


def evaluate_pullback_entry_from_candles(
    direction: str,
    candles: Sequence[Dict[str, Any]],
    coin: str = "BTC",
    proximity_threshold: Optional[float] = None,
    max_atr_pct: float = MAX_ENTRY_ATR_PCT,
) -> Dict[str, Any]:
    if len(candles) < 22:
        return {"triggered": False, "reason": "insufficient_candles"}

    profile = coin_trade_profile(coin)
    closes = candle_closes(candles)
    opens = candle_opens(candles)
    ema21 = calculate_ema(closes, 21)[-1]
    current_price = closes[-1]
    atr_pct = calculate_atr_pct(candles, period=14)
    effective_max_atr_pct = min(max_atr_pct, float(profile["max_entry_atr_pct"]))
    if proximity_threshold is None:
        proximity_threshold = clamp(
            max(atr_pct * 0.45, 0.0025),
            0.0025,
            float(profile["proximity_cap_pct"]),
        )

    if atr_pct > effective_max_atr_pct:
        return {
            "triggered": False,
            "reason": "atr_expansion_filter",
            "ema21": ema21,
            "current_price": current_price,
            "atr_pct": atr_pct * 100,
            "max_atr_pct": effective_max_atr_pct * 100,
            "proximity_threshold_pct": proximity_threshold * 100,
        }

    distance_pct = abs(current_price - ema21) / ema21 if ema21 else 1.0
    near_ema = distance_pct <= proximity_threshold

    direction = direction.upper()
    confirmation_window = int(profile["confirmation_window"])
    if len(candles) < max(22, confirmation_window + 1):
        return {"triggered": False, "reason": "insufficient_confirmation_window"}

    recent_closes = closes[-confirmation_window:]
    recent_opens = opens[-confirmation_window:]
    closes_progressively = all(
        recent_closes[idx] >= recent_closes[idx - 1] for idx in range(1, len(recent_closes))
    )
    closes_progressively_down = all(
        recent_closes[idx] <= recent_closes[idx - 1] for idx in range(1, len(recent_closes))
    )
    if direction == "LONG":
        confirmation = (
            all(recent_closes[idx] > recent_opens[idx] for idx in range(len(recent_closes)))
            and closes_progressively
            and current_price >= ema21
        )
        limit_price = min(current_price, ema21)
    else:
        confirmation = (
            all(recent_closes[idx] < recent_opens[idx] for idx in range(len(recent_closes)))
            and closes_progressively_down
            and current_price <= ema21
        )
        limit_price = max(current_price, ema21)

    triggered = near_ema and confirmation
    return {
        "triggered": triggered,
        "reason": "ok" if triggered else "waiting_pullback_confirmation",
        "ema21": ema21,
        "current_price": current_price,
        "atr_pct": atr_pct * 100,
        "max_atr_pct": effective_max_atr_pct * 100,
        "distance_to_ema_pct": distance_pct * 100,
        "confirmation_candle": confirmation,
        "confirmation_window": confirmation_window,
        "entry_limit_price": limit_price,
        "proximity_threshold_pct": proximity_threshold * 100,
    }


def evaluate_higher_timeframe_context_from_candles(
    direction: str,
    candles: Sequence[Dict[str, Any]],
    coin: str = "BTC",
    level_lookback: int = HIGHER_TIMEFRAME_LEVEL_LOOKBACK,
    level_buffer_pct: float = HIGHER_TIMEFRAME_LEVEL_BUFFER_PCT,
) -> Dict[str, Any]:
    if len(candles) < max(50, level_lookback + 1):
        return {"context_ok": False, "reason": "insufficient_higher_timeframe_candles"}

    profile = coin_trade_profile(coin)
    closes = candle_closes(candles)
    highs = candle_highs(candles)
    lows = candle_lows(candles)
    ema50 = calculate_ema(closes, 50)[-1]
    ema200 = calculate_ema(closes, 200)[-1] if len(closes) >= 200 else calculate_ema(closes, len(closes))[-1]
    current_price = closes[-1]
    recent_high = max(highs[-level_lookback:])
    recent_low = min(lows[-level_lookback:])
    direction = direction.upper()
    ema_gap_pct = abs(ema50 - ema200) / ema200 if ema200 else 0.0
    effective_level_buffer_pct = max(level_buffer_pct, float(profile["level_buffer_pct"]))
    ema_gap_min_pct = float(profile["ema_gap_min_pct"])
    ema200_buffer_pct = float(profile["ema200_buffer_pct"])

    if direction == "LONG":
        trend_ok = (
            current_price >= ema50
            and ema50 >= ema200
            and current_price >= (ema200 * (1 + ema200_buffer_pct))
            and ema_gap_pct >= ema_gap_min_pct
        )
        resistance_distance_pct = max((recent_high - current_price) / current_price, 0.0) if current_price else 0.0
        level_ok = resistance_distance_pct > effective_level_buffer_pct
        reason = "ok" if trend_ok and level_ok else "higher_timeframe_trend_filter" if not trend_ok else "near_higher_timeframe_resistance"
        nearest_level_distance_pct = resistance_distance_pct * 100
        nearest_level = recent_high
    else:
        trend_ok = (
            current_price <= ema50
            and ema50 <= ema200
            and current_price <= (ema200 * (1 - ema200_buffer_pct))
            and ema_gap_pct >= ema_gap_min_pct
        )
        support_distance_pct = max((current_price - recent_low) / current_price, 0.0) if current_price else 0.0
        level_ok = support_distance_pct > effective_level_buffer_pct
        reason = "ok" if trend_ok and level_ok else "higher_timeframe_trend_filter" if not trend_ok else "near_higher_timeframe_support"
        nearest_level_distance_pct = support_distance_pct * 100
        nearest_level = recent_low

    return {
        "context_ok": trend_ok and level_ok,
        "reason": reason,
        "timeframe": "1h",
        "current_price": current_price,
        "ema50": ema50,
        "ema200": ema200,
        "ema_gap_pct": ema_gap_pct * 100,
        "ema_gap_min_pct": ema_gap_min_pct * 100,
        "ema200_buffer_pct": ema200_buffer_pct * 100,
        "trend_ok": trend_ok,
        "level_ok": level_ok,
        "nearest_level": nearest_level,
        "nearest_level_distance_pct": nearest_level_distance_pct,
        "level_buffer_pct": effective_level_buffer_pct * 100,
        "level_lookback_candles": level_lookback,
    }


def evaluate_pullback_entry(
    coin: str,
    direction: str,
    interval: str = "15m",
    lookback: int = 40,
    proximity_threshold: Optional[float] = None,
    max_atr_pct: float = MAX_ENTRY_ATR_PCT,
) -> Dict[str, Any]:
    candles = fetch_candles(coin, interval=interval, lookback=lookback)
    result = evaluate_pullback_entry_from_candles(
        direction,
        candles,
        coin=coin,
        proximity_threshold=proximity_threshold,
        max_atr_pct=max_atr_pct,
    )
    higher_timeframe_candles = fetch_candles(coin, interval="1h", lookback=max(120, HIGHER_TIMEFRAME_LEVEL_LOOKBACK + 10))
    context = evaluate_higher_timeframe_context_from_candles(direction, higher_timeframe_candles, coin=coin)
    result["higher_timeframe_context"] = context
    result["triggered"] = bool(result.get("triggered")) and bool(context.get("context_ok"))
    if result["triggered"]:
        result["reason"] = "ok"
    elif result.get("reason") == "ok":
        result["reason"] = context.get("reason", "higher_timeframe_context_filter")
    result.update({"coin": coin, "interval": interval})
    return result


def calculate_dynamic_stop_price(
    entry: float,
    current: float,
    take_profit: float,
    is_long: bool,
    original_stop: float,
    atr_buffer: float = 0.0,
) -> Dict[str, Any]:
    total_distance = abs(take_profit - entry)
    if total_distance == 0:
        return {"stop_price": original_stop, "progress": 0.0, "stage": "invalid_target"}

    if is_long:
        progress = (current - entry) / total_distance
        lock_50 = entry + ((take_profit - entry) * 0.50)
        lock_75 = entry + ((take_profit - entry) * 0.75)
    else:
        progress = (entry - current) / total_distance
        lock_50 = entry - ((entry - take_profit) * 0.50)
        lock_75 = entry - ((entry - take_profit) * 0.75)

    progress = clamp(progress, 0.0, 1.5)

    if progress < (TRAILING_BREAKEVEN_PROGRESS - TRAILING_PROGRESS_EPSILON):
        return {"stop_price": original_stop, "progress": progress, "stage": "initial_risk"}
    if progress < (TRAILING_PROFIT_LOCK_50_PROGRESS - TRAILING_PROGRESS_EPSILON):
        if is_long:
            buffered_stop = max(original_stop, entry - max(atr_buffer, 0.0))
        else:
            buffered_stop = min(original_stop, entry + max(atr_buffer, 0.0))
        return {"stop_price": buffered_stop, "progress": progress, "stage": "breakeven_buffer"}
    if progress < (TRAILING_PROFIT_LOCK_75_PROGRESS - TRAILING_PROGRESS_EPSILON):
        return {"stop_price": lock_50, "progress": progress, "stage": "profit_lock_50"}
    return {"stop_price": lock_75, "progress": progress, "stage": "profit_lock_75"}


def score_opportunity(
    coin: str,
    candles: Sequence[Dict[str, Any]],
    funding_rate: float,
    volume_24h: float,
    prev_day_px: float,
    regime: str,
) -> Dict[str, Any]:
    profile = coin_trade_profile(coin)
    closes = candle_closes(candles)
    highs = candle_highs(candles)
    lows = candle_lows(candles)
    atr_pct = calculate_atr_pct(candles, period=14)
    rsi_values = calculate_rsi(closes, period=14)
    signed_volume_ratio = calculate_signed_volume_ratio(candles)
    structure_bias = trend_structure(highs, lows)
    funding = funding_signal(funding_rate, regime=regime, structure_bias=structure_bias)
    divergence = rsi_divergence(closes, rsi_values)
    price = closes[-1]
    price_change_pct = ((price - prev_day_px) / prev_day_px) * 100 if prev_day_px > 0 else 0.0

    if atr_pct < MIN_SIGNAL_ATR_PCT:
        return {
            "coin": coin,
            "rejected": True,
            "reason": "atr_compression_filter",
            "atr_pct": round(atr_pct * 100, 4),
            "min_atr_pct": MIN_SIGNAL_ATR_PCT * 100,
        }

    volume_bias = 1 if signed_volume_ratio > 0.05 else -1 if signed_volume_ratio < -0.05 else 0
    directional_score = (
        (volume_bias * SCORE_WEIGHTS["volume"])
        + (structure_bias * SCORE_WEIGHTS["structure"])
        + (funding["bias"] * SCORE_WEIGHTS["funding"])
        + (divergence["bias"] * SCORE_WEIGHTS["divergence"])
    )

    effective_min_conviction = max(MIN_DIRECTIONAL_CONVICTION, float(profile["min_conviction"]))

    if regime == "BULL" and directional_score < effective_min_conviction:
        return {"coin": coin, "rejected": True, "reason": "regime_filter_bull_weak_signal"}
    if regime == "BEAR" and directional_score > -effective_min_conviction:
        return {"coin": coin, "rejected": True, "reason": "regime_filter_bear_weak_signal"}
    if regime == "CHOP":
        return {"coin": coin, "rejected": True, "reason": "regime_filter_chop"}

    suggested_side = "LONG" if directional_score > 0 else "SHORT"
    atr_quality = clamp((atr_pct - MIN_SIGNAL_ATR_PCT) / (ATR_QUALITY_CAP_PCT - MIN_SIGNAL_ATR_PCT), 0.0, 1.0)
    score_strength = abs(directional_score) * (0.85 + (atr_quality * 0.15)) * 100
    rating = "FORTE" if score_strength >= 60 else "MEDIO" if score_strength >= 30 else "FRACO"

    return {
        "coin": coin,
        "price": price,
        "volume_24h_usd": round(volume_24h, 2),
        "change_24h_pct": round(price_change_pct, 2),
        "funding_rate": funding_rate,
        "score": round(score_strength, 2),
        "score_strength": round(score_strength, 2),
        "rating": rating,
        "directional_score": round(directional_score, 4),
        "min_conviction": effective_min_conviction,
        "asset_class": str(profile["class"]),
        "atr_pct": round(atr_pct * 100, 4),
        "min_atr_pct": MIN_SIGNAL_ATR_PCT * 100,
        "atr_quality": round(atr_quality, 4),
        "suggested_side": suggested_side,
        "signals": {
            "signed_volume_ratio": round(signed_volume_ratio, 4),
            "volume_bias": volume_bias,
            "structure_bias": structure_bias,
            "funding_signal": funding["label"],
            "funding_bias": funding["bias"],
            "rsi": round(rsi_values[-1], 2),
            "rsi_divergence": divergence["label"],
            "rsi_divergence_bias": divergence["bias"],
        },
    }
