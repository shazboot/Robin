# =============================================================================
# Robinhood Crypto Trading Bot - Configuration
# =============================================================================
# Copy this file to config.py and fill in your credentials:
#   cp config.example.py config.py

# ---- API Credentials (fill these in) ----
API_KEY = "ADD YOUR API KEY HERE"
BASE64_PRIVATE_KEY = "ADD YOUR PRIVATE KEY HERE"

# ---- Trading Parameters ----
SYMBOL = "BTC-USD"
ASSET_CODE = "BTC"
TRADE_PERCENT = 0.50  # Use 50% of available funds per trade

# ---- Check Interval ----
CHECK_INTERVAL = 300       # seconds (5 minutes) - used after warmup
WARMUP_INTERVAL = 30       # seconds - used during warmup for faster data collection

# ---- Strategy Parameters ----
EMA_SHORT = 9
EMA_LONG = 21
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# ---- API Version ----
# Change to "v2" when ready to migrate (enables fee tiers)
API_VERSION = "v1"

# ---- Logging ----
LOG_FILE = "trades.log"
