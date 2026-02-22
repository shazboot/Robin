from typing import List
from config import EMA_SHORT, EMA_LONG, RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT


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

    # Calculate price changes
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    # Separate gains and losses
    gains = [max(c, 0) for c in changes]
    losses = [abs(min(c, 0)) for c in changes]

    # First average: simple average of first `period` values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Smoothed averages for remaining values
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def get_signal(prices: List[float]) -> str:
    """
    Determine trading signal based on EMA position + RSI confirmation.

    BUY:  EMA-9 below EMA-21 (bearish position) AND RSI < oversold threshold
    SELL: EMA-9 above EMA-21 (bullish position) AND RSI > overbought threshold

    Returns: "BUY", "SELL", or "HOLD"
    """
    min_required = EMA_LONG + 2
    if len(prices) < min_required:
        return "WARMUP"

    # Current EMAs
    ema_short_now = calculate_ema(prices, EMA_SHORT)
    ema_long_now = calculate_ema(prices, EMA_LONG)

    # Current RSI
    rsi = calculate_rsi(prices, RSI_PERIOD)

    # BUY when price is in a dip: EMA9 below EMA21 and RSI confirms oversold
    if ema_short_now < ema_long_now and rsi < RSI_OVERSOLD:
        return "BUY"
    # SELL when price is extended: EMA9 above EMA21 and RSI confirms overbought
    elif ema_short_now > ema_long_now and rsi > RSI_OVERBOUGHT:
        return "SELL"
    else:
        return "HOLD"


def get_signal_details(prices: List[float]) -> dict:
    """Return signal along with indicator values for logging."""
    min_required = EMA_LONG + 2
    if len(prices) < min_required:
        return {
            "signal": "WARMUP",
            "data_points": len(prices),
            "required": min_required,
        }

    ema_short_now = calculate_ema(prices, EMA_SHORT)
    ema_long_now = calculate_ema(prices, EMA_LONG)
    rsi = calculate_rsi(prices, RSI_PERIOD)
    signal = get_signal(prices)

    return {
        "signal": signal,
        "ema_short": round(ema_short_now, 2),
        "ema_long": round(ema_long_now, 2),
        "rsi": round(rsi, 2),
        "price": round(prices[-1], 2),
    }
