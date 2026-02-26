# Robin - Robinhood Crypto Trading Bot

An automated BTC-USD trading bot using the official [Robinhood Crypto Trading API](https://docs.robinhood.com/crypto/trading/). Uses a multi-indicator strategy with dual buy modes, trailing stop-loss, and a live web dashboard for monitoring.

## Features

- **Dual Buy Strategy** — Dip buying (EMA crossunder + RSI oversold) and momentum buying (EMA crossover + MACD + trend confirmation)
- **Multi-Indicator Analysis** — EMA 9/21/50, RSI 14, MACD (12/26/9), and Bollinger Bands (20, 2σ)
- **Trailing Stop-Loss** — Stop price follows the peak upward, locking in profits as price climbs
- **Trade Controls** — Signal confirmation (5x), trade cooldown (60s), minimum hold time (30s), minimum profit target (0.15%)
- **Paper Trading** — Test strategies with simulated money before going live
- **Live Web Dashboard** — TradingView charts (price + RSI + MACD), indicators, trade history, and activity log
- **Limit Orders** — Uses limit orders with configurable buffer to avoid spread losses
- **Position Sync** — Syncs position state with Robinhood order history every cycle
- **Auto-restart** — Systemd service file for automatic startup on boot and crash recovery
- **V2 Ready** — Built on Robinhood API v1 with a one-line config change to migrate to v2

## Dashboard

The web dashboard on port 4501 includes:
- Real-time price chart with EMA-9, EMA-21, EMA-50, and Bollinger Bands overlays
- RSI chart with overbought/oversold thresholds
- MACD chart with signal line and histogram
- Indicator panel with buy/sell readiness status
- Trade history table and bot activity log

## Prerequisites

- Python 3.9+
- A [Robinhood](https://robinhood.com) account with crypto trading enabled (US only)

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/shazboot/Robin.git
cd Robin
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Generate your API keys

```bash
python3 generate_keys.py
```

This prints an Ed25519 keypair. Save both keys.

### 4. Register your public key with Robinhood

1. Go to your [Robinhood crypto account settings](https://robinhood.com) on **web classic**
2. Click **"Add key"**
3. Paste the **PUBLIC key** from step 3
4. Robinhood will give you an **API key** (starts with `rh-api-`)

### 5. Create your config

```bash
cp config.example.py config.py
```

Edit `config.py` and fill in your credentials:

```python
API_KEY = "rh-api-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
BASE64_PRIVATE_KEY = "your-private-key-from-step-3"
```

### 6. Run the bot

```bash
python3 trader.py
```

The bot will:
- Connect to your Robinhood account (or start in paper mode)
- Collect price data during warmup (~1.5 min at 2s intervals, 52 data points needed)
- Begin evaluating signals and trading once warmup is complete
- Log everything to `trades.log` and the terminal

### 7. View the dashboard

Open your browser to:

```
http://localhost:4501
```

Or from another device on your network:

```
http://<your-ip>:4501
```

## Auto-Start on Boot (Optional)

Install the included systemd service:

```bash
sudo cp robin-trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable robin-trader
sudo systemctl start robin-trader
```

**Note:** Edit `robin-trader.service` first if your username or install path differs.

### Service Management

| Command | Description |
|---|---|
| `sudo systemctl start robin-trader` | Start the bot |
| `sudo systemctl stop robin-trader` | Stop the bot |
| `sudo systemctl restart robin-trader` | Restart the bot |
| `sudo systemctl status robin-trader` | Check status |
| `journalctl -u robin-trader -f` | Watch live logs |

## Trading Strategy

### Buy Signals (two modes)

**Dip Buy** — Buy the pullback in an uptrend:
- EMA-9 below EMA-21 (short-term dip)
- RSI below 40 (oversold)
- Price above EMA-50 OR MACD histogram positive (trend confirmation)

**Momentum Buy** — Catch upward breakouts:
- EMA-9 above EMA-21 (upward momentum)
- MACD histogram positive (momentum confirmed)
- Price above EMA-50 (confirmed uptrend)
- RSI between 50-70 (strong but not overbought)

### Sell Signals

- EMA-9 above EMA-21 (short-term peak)
- RSI above 60 (overbought)
- Must meet minimum hold time (30s) and minimum profit (0.15%)

### Risk Management

- **Trailing Stop-Loss** — Tracks peak price upward; sells if price drops 2% from peak
- **Fixed Stop-Loss** — Sells if price drops 2% below buy price (before any rise)
- **Signal Confirmation** — Requires 5 consecutive same signals before acting
- **Trade Cooldown** — 60 second wait between trades to prevent whipsawing
- **One Position at a Time** — Only holds one position, preventing over-exposure

## Configuration

All settings are in `config.py`:

| Setting | Default | Description |
|---|---|---|
| `PAPER_TRADING` | `True` | `True` = simulated trades, `False` = real money |
| `PAPER_BALANCE` | `1000.00` | Starting balance for paper trading |
| `SYMBOL` | `BTC-USD` | Trading pair |
| `TRADE_AMOUNT` | `2.00` | Fixed dollar amount per trade |
| `STOP_LOSS_PERCENT` | `0.02` | Trailing stop-loss percentage (2%) |
| `SIGNAL_CONFIRM` | `5` | Consecutive signals required before acting |
| `TRADE_COOLDOWN` | `60` | Seconds between trades |
| `MIN_HOLD_TIME` | `30` | Minimum seconds before selling |
| `MIN_PROFIT_PCT` | `0.0015` | Minimum profit % before selling (0.15%) |
| `LIMIT_BUFFER` | `10` | Limit order buffer in dollars |
| `CHECK_INTERVAL` | `2` | Seconds between price checks |
| `EMA_SHORT` | `9` | Short EMA period |
| `EMA_LONG` | `21` | Long EMA period |
| `RSI_PERIOD` | `14` | RSI calculation period |
| `RSI_OVERSOLD` | `40` | RSI buy threshold |
| `RSI_OVERBOUGHT` | `60` | RSI sell threshold |
| `API_VERSION` | `v1` | Change to `v2` for fee tier support |

## Project Structure

```
Robin/
├── config.example.py    # Template config (copy to config.py)
├── config.py            # Your config with API keys (git-ignored)
├── api_client.py        # Robinhood API client with Ed25519 signing
├── strategy.py          # Multi-indicator signal engine (EMA/RSI/MACD/BB)
├── trader.py            # Main trading loop with trailing stop-loss
├── dashboard.py         # Web dashboard with TradingView charts (port 4501)
├── generate_keys.py     # Ed25519 keypair generator
├── robin-trader.service # Systemd service file
└── requirements.txt     # Python dependencies (pynacl, requests)
```

## Disclaimer

This bot is for educational purposes. Cryptocurrency trading involves risk. Use at your own discretion and never trade with money you can't afford to lose.
