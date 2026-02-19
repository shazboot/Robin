# Robin - Robinhood Crypto Trading Bot

An automated BTC-USD trading bot using the official [Robinhood Crypto Trading API](https://docs.robinhood.com/crypto/trading/). Uses EMA 9/21 crossover + RSI 14 strategy to generate buy/sell signals, with a live web dashboard for monitoring.

## Features

- **EMA + RSI Strategy** — Buys when EMA-9 crosses above EMA-21 with RSI confirming oversold, sells on the reverse
- **Live Web Dashboard** — Real-time price chart, signals, indicators, P&L, and trade history
- **Fast Warmup** — Collects price data every 30s during warmup (~12 min), then switches to 5-minute intervals
- **Auto-restart** — Systemd service file included for automatic startup on boot and crash recovery
- **V2 Ready** — Built on Robinhood API v1 with a one-line config change to migrate to v2 (fee tiers)

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

This prints an Ed25519 keypair. Save both keys — you'll need them in the next steps.

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
- Connect to your Robinhood account
- Start collecting BTC-USD prices every 30 seconds (warmup phase)
- After 23 data points (~12 min), switch to 5-minute intervals and begin trading
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

Install the included systemd service to auto-start the bot and restart on crashes:

```bash
sudo cp robin-trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable robin-trader
sudo systemctl start robin-trader
```

**Note:** Edit `robin-trader.service` first if your username or install path differs from the defaults.

### Service Management

| Command | Description |
|---|---|
| `sudo systemctl start robin-trader` | Start the bot |
| `sudo systemctl stop robin-trader` | Stop the bot |
| `sudo systemctl restart robin-trader` | Restart the bot |
| `sudo systemctl status robin-trader` | Check status |
| `journalctl -u robin-trader -f` | Watch live logs |

## Configuration

All settings are in `config.py`:

| Setting | Default | Description |
|---|---|---|
| `SYMBOL` | `BTC-USD` | Trading pair |
| `TRADE_PERCENT` | `0.50` | Fraction of funds to use per trade |
| `CHECK_INTERVAL` | `300` | Seconds between checks after warmup |
| `WARMUP_INTERVAL` | `30` | Seconds between checks during warmup |
| `EMA_SHORT` | `9` | Short EMA period |
| `EMA_LONG` | `21` | Long EMA period |
| `RSI_PERIOD` | `14` | RSI calculation period |
| `RSI_OVERSOLD` | `30` | RSI threshold for buy confirmation |
| `RSI_OVERBOUGHT` | `70` | RSI threshold for sell confirmation |
| `API_VERSION` | `v1` | Change to `v2` for fee tier support |

## Project Structure

```
Robin/
├── config.example.py    # Template config (copy to config.py)
├── config.py            # Your config with API keys (git-ignored)
├── api_client.py        # Robinhood API client
├── strategy.py          # EMA/RSI signal engine
├── trader.py            # Main trading loop
├── dashboard.py         # Web dashboard (port 4501)
├── generate_keys.py     # Ed25519 keypair generator
├── robin-trader.service # Systemd service file
└── requirements.txt     # Python dependencies
```

## Disclaimer

This bot is for educational purposes. Cryptocurrency trading involves risk. Use at your own discretion and never trade with money you can't afford to lose.
