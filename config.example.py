# =============================================================================
# Robinhood Crypto Trading Bot - Configuration
# =============================================================================
# Copy this file to config.py and fill in your credentials:
#   cp config.example.py config.py

# ---- API Credentials (fill these in) ----
API_KEY = "ADD YOUR API KEY HERE"
BASE64_PRIVATE_KEY = "ADD YOUR PRIVATE KEY HERE"

# ---- Trading Mode ----
PAPER_TRADING = True       # True = simulate trades, False = real money
PAPER_BALANCE = 1000.00    # Starting paper balance

# ---- Trading Parameters ----
SYMBOL = "BTC-USD"
ASSET_CODE = "BTC"
TRADE_AMOUNT = 2.00        # Fixed dollar amount per trade
STOP_LOSS_PERCENT = 0.02   # Trailing stop-loss triggers at 2% drop from peak
SIGNAL_CONFIRM = 5         # Require 5 consecutive signals before acting
LIMIT_BUFFER = 10          # Limit order buffer in dollars
TRADE_COOLDOWN = 60        # Seconds to wait between trades (prevent whipsaw)
MIN_HOLD_TIME = 30         # Minimum seconds to hold before selling
MIN_PROFIT_PCT = 0.0015    # Minimum 0.15% profit before selling

# ---- Check Interval ----
CHECK_INTERVAL = 2         # seconds - trading interval (~90 API calls/min)
WARMUP_INTERVAL = 2        # seconds - warmup interval

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
