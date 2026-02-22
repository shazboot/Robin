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
TRADE_AMOUNT = 2.00        # Fixed dollar amount per trade
STOP_LOSS_PERCENT = 0.02   # Sell if position drops 2%
SIGNAL_CONFIRM = 3         # Require 3 consecutive signals before acting
LIMIT_BUFFER = 10          # Limit order buffer in dollars

# ---- Check Interval ----
CHECK_INTERVAL = 15        # seconds - trading interval
WARMUP_INTERVAL = 15       # seconds - warmup interval

# ---- Strategy Parameters ----
EMA_SHORT = 9
EMA_LONG = 21
RSI_PERIOD = 14
RSI_OVERSOLD = 40
RSI_OVERBOUGHT = 60

# ---- API Version ----
# Change to "v2" when ready to migrate (enables fee tiers)
API_VERSION = "v1"

# ---- Logging ----
LOG_FILE = "trades.log"
