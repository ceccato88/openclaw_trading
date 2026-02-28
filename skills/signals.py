from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from hl_client import info, logger
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
MIN_DIRECTIONAL_CONVICTION = 0.15
_REGIME_CACHE: Dict[tuple[str, str, int], Dict[str, Any]] = {}


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
    if not info:
        raise RuntimeError("Cliente Info não inicializado.")

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


def calculate_vfi(candles: Sequence[Dict[str, Any]]) -> float:
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


def funding_signal(funding_rate: float, threshold: float = 0.001) -> Dict[str, Any]:
    if funding_rate < -threshold:
        return {"bias": 1, "label": "LONG_SQUEEZE_RISK"}
    if funding_rate > threshold:
        return {"bias": -1, "label": "SHORT_SQUEEZE_RISK"}
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


def determine_market_regime_from_prices(prices: Sequence[float]) -> Dict[str, Any]:
    if len(prices) < 200:
        return {"regime": "CHOP", "reason": "insufficient_history"}

    ema_50 = calculate_ema(prices, 50)[-1]
    ema_200 = calculate_ema(prices, 200)[-1]
    current_price = float(prices[-1])

    if current_price > ema_50 > ema_200:
        regime = "BULL"
    elif current_price < ema_50 < ema_200:
        regime = "BEAR"
    else:
        regime = "CHOP"

    return {
        "regime": regime,
        "current_price": current_price,
        "ema_50": ema_50,
        "ema_200": ema_200,
    }


def _compute_market_regime(coin: str = "BTC", interval: str = "1h", lookback: int = DEFAULT_REGIME_LOOKBACK) -> Dict[str, Any]:
    candles = fetch_candles(coin, interval=interval, lookback=lookback)
    closes = candle_closes(candles)
    regime = determine_market_regime_from_prices(closes)
    regime.update({"coin": coin, "interval": interval, "candles": len(candles)})
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
    proximity_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    if len(candles) < 22:
        return {"triggered": False, "reason": "insufficient_candles"}

    closes = candle_closes(candles)
    opens = candle_opens(candles)
    ema21 = calculate_ema(closes, 21)[-1]
    current_price = closes[-1]
    previous_close = closes[-2]
    current_open = opens[-1]
    if proximity_threshold is None:
        proximity_threshold = clamp(max(calculate_atr_pct(candles, period=14) * 0.5, 0.003), 0.003, 0.02)

    distance_pct = abs(current_price - ema21) / ema21 if ema21 else 1.0
    near_ema = distance_pct <= proximity_threshold

    direction = direction.upper()
    if direction == "LONG":
        confirmation = current_price > current_open and current_price >= previous_close
        limit_price = min(current_price, ema21)
    else:
        confirmation = current_price < current_open and current_price <= previous_close
        limit_price = current_price

    triggered = near_ema and confirmation
    return {
        "triggered": triggered,
        "reason": "ok" if triggered else "waiting_pullback_confirmation",
        "ema21": ema21,
        "current_price": current_price,
        "distance_to_ema_pct": distance_pct * 100,
        "confirmation_candle": confirmation,
        "entry_limit_price": limit_price,
        "proximity_threshold_pct": proximity_threshold * 100,
    }


def evaluate_pullback_entry(
    coin: str,
    direction: str,
    interval: str = "15m",
    lookback: int = 40,
    proximity_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    candles = fetch_candles(coin, interval=interval, lookback=lookback)
    result = evaluate_pullback_entry_from_candles(direction, candles, proximity_threshold=proximity_threshold)
    result.update({"coin": coin, "interval": interval})
    return result


def calculate_dynamic_stop_price(
    entry: float,
    current: float,
    take_profit: float,
    is_long: bool,
    original_stop: float,
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

    if progress < 0.33:
        return {"stop_price": original_stop, "progress": progress, "stage": "initial_risk"}
    if progress < 0.66:
        return {"stop_price": entry, "progress": progress, "stage": "breakeven"}
    if progress < 0.85:
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
    closes = candle_closes(candles)
    highs = candle_highs(candles)
    lows = candle_lows(candles)
    rsi_values = calculate_rsi(closes, period=14)
    vfi = calculate_vfi(candles)
    structure_bias = trend_structure(highs, lows)
    funding = funding_signal(funding_rate)
    divergence = rsi_divergence(closes, rsi_values)
    price = closes[-1]
    price_change_pct = ((price - prev_day_px) / prev_day_px) * 100 if prev_day_px > 0 else 0.0

    vfi_bias = 1 if vfi > 0.05 else -1 if vfi < -0.05 else 0
    directional_score = (vfi_bias * 0.30) + (structure_bias * 0.30) + (funding["bias"] * 0.20) + (divergence["bias"] * 0.20)

    if regime == "BULL" and directional_score < MIN_DIRECTIONAL_CONVICTION:
        return {"coin": coin, "rejected": True, "reason": "regime_filter_bull_weak_signal"}
    if regime == "BEAR" and directional_score > -MIN_DIRECTIONAL_CONVICTION:
        return {"coin": coin, "rejected": True, "reason": "regime_filter_bear_weak_signal"}
    if regime == "CHOP":
        return {"coin": coin, "rejected": True, "reason": "regime_filter_chop"}

    suggested_side = "LONG" if directional_score > 0 else "SHORT"
    confidence = abs(directional_score) * 100
    rating = "FORTE" if confidence >= 60 else "MEDIO" if confidence >= 30 else "FRACO"

    return {
        "coin": coin,
        "price": price,
        "volume_24h_usd": round(volume_24h, 2),
        "change_24h_pct": round(price_change_pct, 2),
        "funding_rate": funding_rate,
        "score": round(confidence, 2),
        "rating": rating,
        "directional_score": round(directional_score, 4),
        "min_conviction": MIN_DIRECTIONAL_CONVICTION,
        "suggested_side": suggested_side,
        "signals": {
            "vfi": round(vfi, 4),
            "vfi_bias": vfi_bias,
            "structure_bias": structure_bias,
            "funding_signal": funding["label"],
            "funding_bias": funding["bias"],
            "rsi": round(rsi_values[-1], 2),
            "rsi_divergence": divergence["label"],
            "rsi_divergence_bias": divergence["bias"],
        },
    }
