import logging
import signal
import sys
import time
from datetime import datetime

from api_client import CryptoAPITrading
from strategy import get_signal, get_signal_details
from config import SYMBOL, ASSET_CODE, TRADE_PERCENT, CHECK_INTERVAL, WARMUP_INTERVAL, LOG_FILE
from dashboard import start_dashboard, update_state, add_trade, add_price_point, add_log, DASHBOARD_PORT


# ---- Custom log handler that feeds the dashboard ----
class DashboardLogHandler(logging.Handler):
    def emit(self, record):
        add_log(record.levelname, record.getMessage())


# ---- Logging Setup ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("trader")
log.addHandler(DashboardLogHandler())


class Trader:
    def __init__(self):
        self.client = CryptoAPITrading()
        self.prices = []
        self.running = True

    def shutdown(self, signum, frame):
        log.info("Shutting down gracefully...")
        self.running = False

    def get_mid_price(self) -> float:
        """Fetch current mid-price (average of best bid and ask)."""
        data = self.client.get_best_bid_ask(SYMBOL)
        if not data or "results" not in data or not data["results"]:
            raise ValueError("Failed to fetch bid/ask data")

        result = data["results"][0]
        bid = float(result["bid_inclusive_of_sell_spread"])
        ask = float(result["ask_inclusive_of_buy_spread"])
        return (bid + ask) / 2.0

    def get_buying_power(self) -> float:
        """Get available USD buying power."""
        account = self.client.get_account()
        if not account:
            raise ValueError("Failed to fetch account data")

        if "results" in account:
            return float(account["results"][0]["buying_power"])
        else:
            return float(account["buying_power"])

    def get_btc_holdings(self) -> float:
        """Get available BTC quantity."""
        holdings = self.client.get_holdings(ASSET_CODE)
        if not holdings:
            return 0.0

        results = holdings.get("results", [])
        if not results:
            return 0.0

        for holding in results:
            if holding.get("asset_code") == ASSET_CODE:
                return float(holding.get("quantity_available", 0))
        return 0.0

    def execute_buy(self, price):
        """Buy BTC using 90% of available buying power."""
        buying_power = self.get_buying_power()
        quote_amount = round(buying_power * TRADE_PERCENT, 2)

        if quote_amount < 1.00:
            log.warning(f"Buying power too low: ${buying_power:.2f}")
            return

        log.info(f"BUYING {SYMBOL} with ${quote_amount:.2f} (90% of ${buying_power:.2f})")
        result = self.client.place_order(
            side="buy",
            order_type="market",
            symbol=SYMBOL,
            order_config={"quote_amount": str(quote_amount)},
        )
        if result:
            order_id = result.get("id", "unknown")
            order_state = result.get("state", "unknown")
            log.info(f"Buy order placed - ID: {order_id}, State: {order_state}")
            estimated_qty = quote_amount / price if price > 0 else 0
            add_trade("buy", price, estimated_qty, quote_amount)
        else:
            log.error("Buy order failed - no response")

    def execute_sell(self, price):
        """Sell 90% of BTC holdings."""
        btc_held = self.get_btc_holdings()
        sell_quantity = btc_held * TRADE_PERCENT

        if sell_quantity <= 0:
            log.warning(f"No BTC holdings to sell (held: {btc_held})")
            return

        sell_quantity_str = f"{sell_quantity:.8f}"

        log.info(f"SELLING {sell_quantity_str} {ASSET_CODE} (90% of {btc_held:.8f})")
        result = self.client.place_order(
            side="sell",
            order_type="market",
            symbol=SYMBOL,
            order_config={"asset_quantity": sell_quantity_str},
        )
        if result:
            order_id = result.get("id", "unknown")
            order_state = result.get("state", "unknown")
            log.info(f"Sell order placed - ID: {order_id}, State: {order_state}")
            estimated_amount = sell_quantity * price
            add_trade("sell", price, sell_quantity, estimated_amount)
        else:
            log.error("Sell order failed - no response")

    def run(self):
        """Main trading loop."""
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        # Start web dashboard
        start_dashboard()
        log.info(f"Dashboard running at http://localhost:{DASHBOARD_PORT}")

        log.info("=" * 60)
        log.info("Robinhood Crypto Trading Bot Starting")
        log.info(f"Symbol: {SYMBOL} | Interval: {CHECK_INTERVAL}s | Trade: {TRADE_PERCENT*100:.0f}%")
        log.info("=" * 60)

        update_state(symbol=SYMBOL)

        # Verify connectivity
        try:
            buying_power = self.get_buying_power()
            btc_held = self.get_btc_holdings()
            log.info(f"Account connected - Buying power: ${buying_power:.2f} | {ASSET_CODE} held: {btc_held:.8f}")
            update_state(
                buying_power=buying_power,
                btc_held=btc_held,
                starting_value=buying_power,
            )
        except Exception as e:
            log.error(f"Failed to connect to account: {e}")
            log.error("Check your API_KEY and BASE64_PRIVATE_KEY in config.py")
            return

        warmup_time = 23 * WARMUP_INTERVAL // 60
        log.info(f"Collecting price data... need 23 data points (~{warmup_time} min fast warmup at {WARMUP_INTERVAL}s intervals)")

        while self.running:
            try:
                # Fetch current price
                price = self.get_mid_price()
                self.prices.append(price)
                add_price_point(price)
                log.info(f"Price: ${price:,.2f} | Data points: {len(self.prices)}/23")

                # Update account info for dashboard
                try:
                    buying_power = self.get_buying_power()
                    btc_held = self.get_btc_holdings()
                except Exception:
                    buying_power = 0
                    btc_held = 0

                # Get signal with details
                details = get_signal_details(self.prices)
                sig = details["signal"]

                update_state(
                    current_price=price,
                    signal=sig,
                    ema_short=details.get("ema_short", 0),
                    ema_long=details.get("ema_long", 0),
                    rsi=details.get("rsi", 0),
                    buying_power=buying_power,
                    btc_held=btc_held,
                    current_value=buying_power + btc_held * price,
                    data_points=len(self.prices),
                    last_update=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )

                if sig == "WARMUP":
                    log.info(f"Warming up... {details['data_points']}/{details['required']} data points")
                elif sig == "BUY":
                    log.info(f"BUY SIGNAL | EMA9: {details['ema_short']} EMA21: {details['ema_long']} RSI: {details['rsi']}")
                    self.execute_buy(price)
                elif sig == "SELL":
                    log.info(f"SELL SIGNAL | EMA9: {details['ema_short']} EMA21: {details['ema_long']} RSI: {details['rsi']}")
                    self.execute_sell(price)
                else:
                    log.info(f"HOLD | EMA9: {details['ema_short']} EMA21: {details['ema_long']} RSI: {details['rsi']}")

            except Exception as e:
                log.error(f"Error in trading loop: {e}")

            # Use fast interval during warmup, normal interval after
            if self.running:
                if sig == "WARMUP":
                    time.sleep(WARMUP_INTERVAL)
                else:
                    if not hasattr(self, '_warmup_done'):
                        self._warmup_done = True
                        log.info(f"Warmup complete! Switching to {CHECK_INTERVAL}s intervals")
                    time.sleep(CHECK_INTERVAL)

        log.info("Bot stopped.")


if __name__ == "__main__":
    trader = Trader()
    trader.run()
