import numpy as np
from typing import List, Tuple
from config import EMA_SHORT, EMA_LONG, RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT, ENTRY_RSI_DIP


# ---- Trend filter config ----
EMA_TREND = 50          # Long-term trend EMA
MACD_FAST = 12          # MACD fast EMA period
MACD_SLOW = 26          # MACD slow EMA period
MACD_SIGNAL = 9         # MACD signal line period
BB_PERIOD = 20          # Bollinger Bands period
BB_STD = 2.0            # Bollinger Bands standard deviations


def _ema_series(prices: np.ndarray, period: int) -> np.ndarray:
    """Compute full EMA series over a numpy array.
    Returns an array of length len(prices) - period + 1."""
    multiplier = 2.0 / (period + 1)
    out = np.empty(len(prices) - period + 1)
    out[0] = prices[:period].mean()
    for i in range(1, len(out)):
        out[i] = (prices[period - 1 + i] - out[i - 1]) * multiplier + out[i - 1]
    return out


def calculate_ema(prices: List[float], period: int) -> float:
    """Calculate Exponential Moving Average for the given period."""
    if len(prices) < period:
        raise ValueError(f"Need at least {period} prices, got {len(prices)}")
    arr = np.asarray(prices, dtype=np.float64)
    return float(_ema_series(arr, period)[-1])


def _rsi_series(prices: np.ndarray, period: int) -> np.ndarray:
    """Compute full RSI series using Wilder smoothing.
    Returns array of length len(prices) - period."""
    changes = np.diff(prices)
    gains = np.maximum(changes, 0.0)
    losses = np.abs(np.minimum(changes, 0.0))

    n_out = len(changes) - period + 1
    out = np.empty(n_out)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    if avg_loss == 0:
        out[0] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[0] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(1, n_out):
        avg_gain = (avg_gain * (period - 1) + gains[period - 1 + i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[period - 1 + i]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))

    return out


def calculate_rsi(prices: List[float], period: int) -> float:
    """Calculate Relative Strength Index for the given period."""
    if len(prices) < period + 1:
        raise ValueError(f"Need at least {period + 1} prices, got {len(prices)}")
    arr = np.asarray(prices, dtype=np.float64)
    return float(_rsi_series(arr, period)[-1])


def calculate_macd(prices: List[float]) -> Tuple[float, float, float]:
    """Calculate MACD line, signal line, and histogram.
    Returns (macd_line, signal_line, histogram)."""
    if len(prices) < MACD_SLOW + MACD_SIGNAL:
        raise ValueError(f"Need at least {MACD_SLOW + MACD_SIGNAL} prices for MACD")

    arr = np.asarray(prices, dtype=np.float64)
    fast_series = _ema_series(arr, MACD_FAST)
    slow_series = _ema_series(arr, MACD_SLOW)

    # Align: fast_series starts at index MACD_FAST-1, slow at MACD_SLOW-1
    # We need them aligned from MACD_SLOW-1 onward
    offset = MACD_SLOW - MACD_FAST
    macd_series = fast_series[offset:] - slow_series

    # Signal line = EMA of MACD series
    if len(macd_series) < MACD_SIGNAL:
        raise ValueError("Not enough MACD values for signal line")

    signal_series = _ema_series(macd_series, MACD_SIGNAL)

    macd_line = float(macd_series[-1])
    signal = float(signal_series[-1])
    histogram = macd_line - signal

    return macd_line, signal, histogram


def calculate_bollinger(prices: List[float]) -> Tuple[float, float, float]:
    """Calculate Bollinger Bands (upper, middle/SMA, lower)."""
    if len(prices) < BB_PERIOD:
        raise ValueError(f"Need at least {BB_PERIOD} prices for Bollinger Bands")

    recent = np.asarray(prices[-BB_PERIOD:], dtype=np.float64)
    middle = float(np.mean(recent))
    std_dev = float(np.std(recent))

    upper = middle + BB_STD * std_dev
    lower = middle - BB_STD * std_dev

    return upper, middle, lower


def calculate_ema_slope(prices: List[float], period: int) -> str:
    """Calculate EMA slope to detect trend strength.
    Returns "UP" if EMA is rising, "DOWN" if falling, "FLAT" if neutral."""
    if len(prices) < period + 1:
        return "FLAT"

    arr = np.asarray(prices, dtype=np.float64)
    series = _ema_series(arr, period)
    if len(series) < 2:
        return "FLAT"

    diff = series[-1] - series[-2]
    pct_diff = diff / series[-1] if series[-1] != 0 else 0
    if pct_diff > 0.0001:
        return "UP"
    elif pct_diff < -0.0001:
        return "DOWN"
    else:
        return "FLAT"


def calculate_bb_bandwidth(prices: List[float]) -> Tuple[float, str]:
    """Calculate Bollinger Band bandwidth and detect expansion/contraction.
    Returns (bandwidth_pct, "EXPANDING"/"CONTRACTING"/"NEUTRAL")."""
    if len(prices) < BB_PERIOD + 1:
        return 0.0, "NEUTRAL"

    upper, middle, lower = calculate_bollinger(prices)
    current_bw = ((upper - lower) / middle * 100) if middle > 0 else 0

    prev_upper, prev_middle, prev_lower = calculate_bollinger(prices[:-1])
    prev_bw = ((prev_upper - prev_lower) / prev_middle * 100) if prev_middle > 0 else 0

    diff = current_bw - prev_bw
    if diff > 0.01:
        return current_bw, "EXPANDING"
    elif diff < -0.01:
        return current_bw, "CONTRACTING"
    else:
        return current_bw, "NEUTRAL"


ATR_PERIOD = 14             # Average True Range period


def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = ATR_PERIOD) -> float:
    """Calculate Average True Range from OHLC candle data.
    Returns ATR value (average of true ranges over period)."""
    if len(closes) < period + 1 or len(highs) < period + 1:
        return 0.0

    h = np.asarray(highs, dtype=np.float64)
    l = np.asarray(lows, dtype=np.float64)
    c = np.asarray(closes, dtype=np.float64)

    # True range: max(high-low, |high-prev_close|, |low-prev_close|)
    tr = np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
    )

    if len(tr) < period:
        return float(tr.mean()) if len(tr) > 0 else 0.0

    # Wilder smoothing
    atr = float(tr[:period].mean())
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + float(tr[i])) / period

    return atr


def detect_divergence(prices: List[float], rsi_values: List[float], lookback: int = 10) -> str:
    """Detect RSI divergence over the last `lookback` candles.
    Returns "BULLISH", "BEARISH", or "NONE"."""
    if len(prices) < lookback or len(rsi_values) < lookback:
        return "NONE"

    p = np.asarray(prices[-lookback:], dtype=np.float64)
    r = np.asarray(rsi_values[-lookback:], dtype=np.float64)

    mid = lookback // 2

    # Bullish divergence: price lower low, RSI higher low
    if np.min(p[mid:]) < np.min(p[:mid]) and np.min(r[mid:]) > np.min(r[:mid]):
        return "BULLISH"

    # Bearish divergence: price higher high, RSI lower high
    if np.max(p[mid:]) > np.max(p[:mid]) and np.max(r[mid:]) < np.max(r[:mid]):
        return "BEARISH"

    return "NONE"


def is_volume_confirmed(volumes: List[float], lookback: int = 20) -> bool:
    """Check if current volume is above the moving average.
    Returns True if latest candle volume > average of last `lookback` candles."""
    if len(volumes) < lookback + 1:
        return True  # Not enough data, allow trades

    avg_vol = float(np.mean(volumes[-lookback - 1:-1]))
    current_vol = volumes[-1]
    return current_vol > avg_vol


# Minimum data points needed for all indicators
MIN_REQUIRED = max(EMA_TREND + 2, MACD_SLOW + MACD_SIGNAL, EMA_LONG + 2, BB_PERIOD + 2, RSI_PERIOD + 2)
MIN_REQUIRED_15M = MIN_REQUIRED  # Same formula, applied to 15m candle counts


def _compute_indicators(prices: List[float], highs: List[float] = None,
                         lows: List[float] = None, volumes: List[float] = None) -> dict:
    """Compute all indicators once. Used by both get_signal and get_signal_details."""
    arr = np.asarray(prices, dtype=np.float64)

    ema_short = float(_ema_series(arr, EMA_SHORT)[-1])
    ema_long = float(_ema_series(arr, EMA_LONG)[-1])
    ema_trend = float(_ema_series(arr, EMA_TREND)[-1])

    rsi = float(_rsi_series(arr, RSI_PERIOD)[-1])

    macd_line, macd_signal, macd_hist = calculate_macd(prices)
    bb_upper, bb_middle, bb_lower = calculate_bollinger(prices)

    # EMA slope from series (no redundant recomputation)
    ema_trend_series = _ema_series(arr, EMA_TREND)
    if len(ema_trend_series) >= 2:
        slope_diff = ema_trend_series[-1] - ema_trend_series[-2]
        slope_pct = slope_diff / ema_trend_series[-1] if ema_trend_series[-1] != 0 else 0
        if slope_pct > 0.0001:
            ema50_slope = "UP"
        elif slope_pct < -0.0001:
            ema50_slope = "DOWN"
        else:
            ema50_slope = "FLAT"
    else:
        ema50_slope = "FLAT"

    # BB bandwidth
    if len(prices) >= BB_PERIOD + 1:
        prev_upper, prev_middle, prev_lower = calculate_bollinger(prices[:-1])
        current_bw = ((bb_upper - bb_lower) / bb_middle * 100) if bb_middle > 0 else 0
        prev_bw = ((prev_upper - prev_lower) / prev_middle * 100) if prev_middle > 0 else 0
        bw_diff = current_bw - prev_bw
        if bw_diff > 0.01:
            bb_squeeze = "EXPANDING"
        elif bw_diff < -0.01:
            bb_squeeze = "CONTRACTING"
        else:
            bb_squeeze = "NEUTRAL"
        bb_bandwidth = current_bw
    else:
        bb_bandwidth = 0.0
        bb_squeeze = "NEUTRAL"

    # Volume confirmation
    vol_ok = is_volume_confirmed(volumes) if volumes else True

    # ATR
    atr = calculate_atr(highs, lows, prices) if highs and lows else 0
    atr_pct = (atr / prices[-1] * 100) if atr and prices[-1] else 0

    # RSI divergence — compute last 10+ RSI values from the full series in one pass
    div_lookback = 10
    rsi_series = _rsi_series(arr, RSI_PERIOD)
    if len(rsi_series) >= div_lookback:
        rsi_history = rsi_series[-div_lookback:].tolist()
        divergence = detect_divergence(prices, rsi_history, div_lookback)
    else:
        divergence = "NONE"

    return {
        "ema_short": ema_short,
        "ema_long": ema_long,
        "ema_trend": ema_trend,
        "rsi": rsi,
        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "ema50_slope": ema50_slope,
        "bb_bandwidth": bb_bandwidth,
        "bb_squeeze": bb_squeeze,
        "vol_ok": vol_ok,
        "atr": atr,
        "atr_pct": atr_pct,
        "divergence": divergence,
    }


def _decide_signal(ind: dict, price: float) -> str:
    """Determine BUY/SELL/HOLD from precomputed indicators."""
    ema_short = ind["ema_short"]
    ema_long = ind["ema_long"]
    ema_trend = ind["ema_trend"]
    rsi = ind["rsi"]
    macd_hist = ind["macd_hist"]
    ema50_slope = ind["ema50_slope"]
    bb_squeeze = ind["bb_squeeze"]
    vol_ok = ind["vol_ok"]
    divergence = ind["divergence"]

    trend_strengthening = ema50_slope == "UP"
    volatility_ok = bb_squeeze == "EXPANDING"

    # DIP BUY conditions
    ema_buy = ema_short < ema_long
    rsi_buy = rsi < RSI_OVERSOLD
    trend_up = price > ema_trend
    macd_bullish = macd_hist > 0
    trend_ok = trend_up or macd_bullish
    no_bearish_div = divergence != "BEARISH"

    if ema_buy and rsi_buy and trend_ok and trend_strengthening and vol_ok and no_bearish_div:
        return "BUY"

    # Bullish divergence can also trigger a buy
    if divergence == "BULLISH" and rsi_buy and trend_strengthening and vol_ok:
        return "BUY"

    # MOMENTUM BUY conditions
    ema_momentum = ema_short > ema_long
    macd_strong = macd_hist > 0
    trend_confirmed = price > ema_trend
    rsi_strong = 50 <= rsi <= 72

    if ema_momentum and macd_strong and trend_confirmed and rsi_strong and trend_strengthening and volatility_ok and vol_ok and no_bearish_div:
        return "BUY"

    # SELL conditions
    ema_sell = ema_short > ema_long
    rsi_sell = rsi > RSI_OVERBOUGHT

    if ema_sell and rsi_sell:
        return "SELL"

    # Bearish divergence alone can trigger sell if RSI is elevated
    if divergence == "BEARISH" and rsi > 55:
        return "SELL"

    return "HOLD"


def get_signal(prices: List[float], highs: List[float] = None,
               lows: List[float] = None, volumes: List[float] = None) -> str:
    """
    Determine trading signal with trend, volume, and divergence filtering.
    Returns: "BUY", "SELL", or "HOLD"
    """
    if len(prices) < MIN_REQUIRED:
        return "WARMUP"

    ind = _compute_indicators(prices, highs, lows, volumes)
    return _decide_signal(ind, prices[-1])


def get_signal_details(prices: List[float], highs: List[float] = None,
                       lows: List[float] = None, volumes: List[float] = None) -> dict:
    """Return signal along with all indicator values."""
    if len(prices) < MIN_REQUIRED:
        return {
            "signal": "WARMUP",
            "data_points": len(prices),
            "required": MIN_REQUIRED,
        }

    ind = _compute_indicators(prices, highs, lows, volumes)
    signal = _decide_signal(ind, prices[-1])

    return {
        "signal": signal,
        "ema_short": round(ind["ema_short"], 2),
        "ema_long": round(ind["ema_long"], 2),
        "ema_trend": round(ind["ema_trend"], 2),
        "rsi": round(ind["rsi"], 2),
        "price": round(prices[-1], 2),
        "macd_line": round(ind["macd_line"], 2),
        "macd_signal": round(ind["macd_signal"], 2),
        "macd_hist": round(ind["macd_hist"], 2),
        "bb_upper": round(ind["bb_upper"], 2),
        "bb_middle": round(ind["bb_middle"], 2),
        "bb_lower": round(ind["bb_lower"], 2),
        "trend_strength": ind["ema50_slope"],
        "bb_bandwidth": round(ind["bb_bandwidth"], 2),
        "bb_squeeze": ind["bb_squeeze"],
        "atr": round(ind["atr"], 2),
        "atr_pct": round(ind["atr_pct"], 3),
        "volume_confirmed": ind["vol_ok"],
        "divergence": ind["divergence"],
    }


def get_15m_direction(closes: List[float], highs: List[float] = None,
                      lows: List[float] = None, volumes: List[float] = None) -> dict:
    """Evaluate direction from 15m candle data using existing indicators.
    Returns dict with 'signal' (BUY/SELL/HOLD/WARMUP) and all 15m indicator values."""
    if len(closes) < MIN_REQUIRED_15M:
        return {
            "signal": "WARMUP",
            "data_points": len(closes),
            "required": MIN_REQUIRED_15M,
        }

    ind = _compute_indicators(closes, highs, lows, volumes)
    signal = _decide_signal(ind, closes[-1])

    return {
        "signal": signal,
        "ema_short": round(ind["ema_short"], 2),
        "ema_long": round(ind["ema_long"], 2),
        "ema_trend": round(ind["ema_trend"], 2),
        "rsi": round(ind["rsi"], 2),
        "price": round(closes[-1], 2),
        "macd_line": round(ind["macd_line"], 2),
        "macd_signal": round(ind["macd_signal"], 2),
        "macd_hist": round(ind["macd_hist"], 2),
        "bb_upper": round(ind["bb_upper"], 2),
        "bb_lower": round(ind["bb_lower"], 2),
        "trend_strength": ind["ema50_slope"],
        "bb_squeeze": ind["bb_squeeze"],
        "volume_confirmed": ind["vol_ok"],
        "divergence": ind["divergence"],
    }


def check_1m_entry(prices: List[float], highs: List[float] = None,
                   lows: List[float] = None, volumes: List[float] = None) -> dict:
    """Check if 1m candle data shows a good entry point within a 15m BUY window.
    Looks for 3 conditions:
      1. RSI dip recovery: previous 1m RSI < ENTRY_RSI_DIP, current >= ENTRY_RSI_DIP
      2. BB bounce: price near lower Bollinger Band
      3. EMA-9 support: price crosses above 1m EMA-9
    Returns {entry_ok, entry_type, rsi}."""
    result = {"entry_ok": False, "entry_type": "none", "rsi": 0}

    if len(prices) < MIN_REQUIRED:
        return result

    arr = np.asarray(prices, dtype=np.float64)

    # Current and previous RSI
    rsi_series = _rsi_series(arr, RSI_PERIOD)
    if len(rsi_series) < 2:
        return result
    rsi_current = float(rsi_series[-1])
    rsi_prev = float(rsi_series[-2])
    result["rsi"] = round(rsi_current, 2)

    # Condition 1: RSI dip recovery
    if rsi_prev < ENTRY_RSI_DIP and rsi_current >= ENTRY_RSI_DIP:
        result["entry_ok"] = True
        result["entry_type"] = "RSI_DIP"
        return result

    # Condition 2: BB bounce — price near lower band
    if len(prices) >= BB_PERIOD:
        bb_upper, bb_middle, bb_lower = calculate_bollinger(prices)
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            # Price within bottom 20% of BB range
            price_position = (prices[-1] - bb_lower) / bb_range
            if price_position <= 0.20 and rsi_current < 50:
                result["entry_ok"] = True
                result["entry_type"] = "BB_BOUNCE"
                return result

    # Condition 3: EMA-9 support — price crosses above EMA-9
    if len(prices) >= EMA_SHORT + 1:
        ema9_series = _ema_series(arr, EMA_SHORT)
        if len(ema9_series) >= 2:
            price_prev = prices[-2]
            price_curr = prices[-1]
            ema9_prev = float(ema9_series[-2])
            ema9_curr = float(ema9_series[-1])
            if price_prev <= ema9_prev and price_curr > ema9_curr:
                result["entry_ok"] = True
                result["entry_type"] = "EMA9_CROSS"
                return result

    return result
