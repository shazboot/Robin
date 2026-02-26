import math
from typing import List, Tuple
from config import EMA_SHORT, EMA_LONG, RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT


# ---- Trend filter config ----
EMA_TREND = 50          # Long-term trend EMA
MACD_FAST = 12          # MACD fast EMA period
MACD_SLOW = 26          # MACD slow EMA period
MACD_SIGNAL = 9         # MACD signal line period
BB_PERIOD = 20          # Bollinger Bands period
BB_STD = 2.0            # Bollinger Bands standard deviations


def calculate_ema(prices: List[float], period: int) -> float:
    """Calculate Exponential Moving Average for the given period."""
    if len(prices) < period:
        raise ValueError(f"Need at least {period} prices, got {len(prices)}")

    multiplier = 2.0 / (period + 1)

    # Start with SMA of the first `period` prices as the seed
    ema = sum(prices[:period]) / period

    # Apply EMA formula for remaining prices
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


def calculate_rsi(prices: List[float], period: int) -> float:
    """Calculate Relative Strength Index for the given period."""
    if len(prices) < period + 1:
        raise ValueError(f"Need at least {period + 1} prices, got {len(prices)}")

    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    gains = [max(c, 0) for c in changes]
    losses = [abs(min(c, 0)) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_macd(prices: List[float]) -> Tuple[float, float, float]:
    """Calculate MACD line, signal line, and histogram.
    Returns (macd_line, signal_line, histogram)."""
    if len(prices) < MACD_SLOW + MACD_SIGNAL:
        raise ValueError(f"Need at least {MACD_SLOW + MACD_SIGNAL} prices for MACD")

    # MACD line = EMA(fast) - EMA(slow)
    # We need the full MACD series to compute the signal line
    macd_series = []
    for i in range(MACD_SLOW, len(prices) + 1):
        subset = prices[:i]
        fast = calculate_ema(subset, MACD_FAST)
        slow = calculate_ema(subset, MACD_SLOW)
        macd_series.append(fast - slow)

    # Signal line = EMA of MACD series
    if len(macd_series) < MACD_SIGNAL:
        raise ValueError("Not enough MACD values for signal line")

    multiplier = 2.0 / (MACD_SIGNAL + 1)
    signal = sum(macd_series[:MACD_SIGNAL]) / MACD_SIGNAL
    for val in macd_series[MACD_SIGNAL:]:
        signal = (val - signal) * multiplier + signal

    macd_line = macd_series[-1]
    histogram = macd_line - signal

    return macd_line, signal, histogram


def calculate_bollinger(prices: List[float]) -> Tuple[float, float, float]:
    """Calculate Bollinger Bands (upper, middle/SMA, lower)."""
    if len(prices) < BB_PERIOD:
        raise ValueError(f"Need at least {BB_PERIOD} prices for Bollinger Bands")

    recent = prices[-BB_PERIOD:]
    middle = sum(recent) / BB_PERIOD
    variance = sum((p - middle) ** 2 for p in recent) / BB_PERIOD
    std_dev = math.sqrt(variance)

    upper = middle + BB_STD * std_dev
    lower = middle - BB_STD * std_dev

    return upper, middle, lower


# Minimum data points needed for all indicators
MIN_REQUIRED = max(EMA_TREND + 2, MACD_SLOW + MACD_SIGNAL, EMA_LONG + 2, BB_PERIOD + 2, RSI_PERIOD + 2)


def get_signal(prices: List[float]) -> str:
    """
    Determine trading signal with trend filtering.

    DIP BUY requires ALL of:
      - EMA-9 below EMA-21 (short-term dip)
      - RSI < oversold threshold
      - Price above EMA-50 (overall uptrend) OR MACD histogram turning positive

    MOMENTUM BUY requires ALL of:
      - EMA-9 above EMA-21 (upward momentum)
      - MACD histogram positive (momentum confirmed)
      - Price above EMA-50 (confirmed uptrend)
      - RSI between 50-70 (strong but not overbought)

    SELL requires ALL of:
      - EMA-9 above EMA-21 (short-term peak)
      - RSI > overbought threshold

    Returns: "BUY", "SELL", or "HOLD"
    """
    if len(prices) < MIN_REQUIRED:
        return "WARMUP"

    ema_short = calculate_ema(prices, EMA_SHORT)
    ema_long = calculate_ema(prices, EMA_LONG)
    ema_trend = calculate_ema(prices, EMA_TREND)
    rsi = calculate_rsi(prices, RSI_PERIOD)
    macd_line, macd_signal, macd_hist = calculate_macd(prices)
    bb_upper, bb_middle, bb_lower = calculate_bollinger(prices)

    price = prices[-1]

    # DIP BUY conditions (buy the dip in an uptrend)
    ema_buy = ema_short < ema_long          # Short-term dip
    rsi_buy = rsi < RSI_OVERSOLD            # RSI oversold
    trend_up = price > ema_trend            # Overall trend is up
    macd_bullish = macd_hist > 0            # MACD momentum turning up
    trend_ok = trend_up or macd_bullish

    if ema_buy and rsi_buy and trend_ok:
        return "BUY"

    # MOMENTUM BUY conditions (catch upward breakouts)
    ema_momentum = ema_short > ema_long     # EMA-9 above EMA-21
    macd_strong = macd_hist > 0             # MACD confirms momentum
    trend_confirmed = price > ema_trend     # Price above EMA-50
    rsi_strong = 50 <= rsi <= 70            # Strong but not overbought

    if ema_momentum and macd_strong and trend_confirmed and rsi_strong:
        return "BUY"

    # SELL conditions
    ema_sell = ema_short > ema_long          # Short-term peak
    rsi_sell = rsi > RSI_OVERBOUGHT          # RSI overbought

    if ema_sell and rsi_sell:
        return "SELL"

    return "HOLD"


def get_signal_details(prices: List[float]) -> dict:
    """Return signal along with all indicator values."""
    if len(prices) < MIN_REQUIRED:
        return {
            "signal": "WARMUP",
            "data_points": len(prices),
            "required": MIN_REQUIRED,
        }

    ema_short = calculate_ema(prices, EMA_SHORT)
    ema_long = calculate_ema(prices, EMA_LONG)
    ema_trend = calculate_ema(prices, EMA_TREND)
    rsi = calculate_rsi(prices, RSI_PERIOD)
    macd_line, macd_signal_line, macd_hist = calculate_macd(prices)
    bb_upper, bb_middle, bb_lower = calculate_bollinger(prices)
    signal = get_signal(prices)

    return {
        "signal": signal,
        "ema_short": round(ema_short, 2),
        "ema_long": round(ema_long, 2),
        "ema_trend": round(ema_trend, 2),
        "rsi": round(rsi, 2),
        "price": round(prices[-1], 2),
        "macd_line": round(macd_line, 2),
        "macd_signal": round(macd_signal_line, 2),
        "macd_hist": round(macd_hist, 2),
        "bb_upper": round(bb_upper, 2),
        "bb_middle": round(bb_middle, 2),
        "bb_lower": round(bb_lower, 2),
    }
