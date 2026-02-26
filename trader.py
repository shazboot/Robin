import logging
import signal
import sys
import time
from datetime import datetime

from api_client import CryptoAPITrading
from strategy import get_signal, get_signal_details, MIN_REQUIRED
from config import (SYMBOL, ASSET_CODE, TRADE_AMOUNT, CHECK_INTERVAL, WARMUP_INTERVAL,
                     LOG_FILE, RSI_OVERSOLD, RSI_OVERBOUGHT, STOP_LOSS_PERCENT,
                     SIGNAL_CONFIRM, LIMIT_BUFFER, PAPER_TRADING, PAPER_BALANCE,
                     TRADE_COOLDOWN, MIN_HOLD_TIME, MIN_PROFIT_PCT)
from dashboard import (start_dashboard, update_state, add_trade, add_price_point,
                       add_log, get_pnl, DASHBOARD_PORT)


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
        self.position_open = False  # True after a buy, False after a sell
        self.buy_price = 0          # Actual fill price from Robinhood
        self.peak_price = 0         # Highest price since buy (for trailing stop)
        self.buy_time = 0           # Timestamp of last buy (for min hold time)
        self.last_trade_time = 0    # Timestamp of last trade (for cooldown)
        self.warmup_cooldown = 0    # Skip first N signals after warmup
        self.consecutive_signal = 0 # Count consecutive same signals
        self.last_signal = None     # Track last signal for confirmation
        # Paper trading state
        self.paper_balance = PAPER_BALANCE
        self.paper_btc = 0.0

    def shutdown(self, signum, frame):
        log.info("Shutting down gracefully...")
        self.running = False

    def get_mid_price(self) -> float:
        """Fetch current mid-price (average of best bid and ask)."""
        bid, ask = self.get_bid_ask()
        return (bid + ask) / 2.0

    def get_bid_ask(self) -> tuple:
        """Fetch current best bid and ask prices."""
        data = self.client.get_best_bid_ask(SYMBOL)
        if not data or "results" not in data or not data["results"]:
            raise ValueError("Failed to fetch bid/ask data")

        result = data["results"][0]
        bid = float(result["bid_inclusive_of_sell_spread"])
        ask = float(result["ask_inclusive_of_buy_spread"])
        return bid, ask

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
                return float(holding.get("quantity_available_for_trading", holding.get("quantity_available", 0)))
        return 0.0

    def execute_buy(self, price):
        """Buy BTC — paper or real."""
        if self.position_open:
            log.info("BUY skipped — already holding a position, waiting for sell")
            return

        # Cooldown check — prevent whipsawing
        elapsed = time.time() - self.last_trade_time
        if self.last_trade_time > 0 and elapsed < TRADE_COOLDOWN:
            remaining = int(TRADE_COOLDOWN - elapsed)
            log.info(f"BUY skipped — cooldown ({remaining}s remaining)")
            return

        if PAPER_TRADING:
            quote_amount = min(TRADE_AMOUNT, self.paper_balance)
            if quote_amount < 1.00:
                log.warning(f"Paper balance too low: ${self.paper_balance:.2f}")
                return
            asset_quantity = quote_amount / price
            self.paper_balance -= quote_amount
            self.paper_btc += asset_quantity
            self.buy_price = price
            self.peak_price = price
            self.buy_time = time.time()
            self.last_trade_time = time.time()
            self.position_open = True
            log.info(f"[PAPER] BUY {asset_quantity:.8f} BTC at ${price:,.2f} = ${quote_amount:.2f} | Balance: ${self.paper_balance:.2f}")
            add_trade("buy", price, asset_quantity, quote_amount)
            return

        buying_power = self.get_buying_power()
        quote_amount = min(TRADE_AMOUNT, buying_power)

        if quote_amount < 1.00:
            log.warning(f"Buying power too low: ${buying_power:.2f}")
            return

        # Get fresh ask price and set limit just $LIMIT_BUFFER above it
        try:
            _, ask = self.get_bid_ask()
            limit_price = round(ask + LIMIT_BUFFER, 2)
        except Exception:
            limit_price = round(price + LIMIT_BUFFER, 2)

        # Convert dollar amount to BTC quantity using the limit price
        asset_quantity = quote_amount / limit_price
        asset_quantity_str = f"{asset_quantity:.8f}"
        limit_price_str = f"{limit_price:.2f}"

        log.info(f"BUYING {asset_quantity_str} {ASSET_CODE} (~${quote_amount:.2f}, limit ${limit_price_str})")
        result = self.client.place_order(
            side="buy",
            order_type="limit",
            symbol=SYMBOL,
            order_config={
                "asset_quantity": asset_quantity_str,
                "limit_price": limit_price_str,
                "time_in_force": "gtc",
            },
        )
        if result and result.get("id") and "errors" not in result:
            order_id = result.get("id")
            order_state = result.get("state", "unknown")
            log.info(f"Buy order placed - ID: {order_id}, State: {order_state}")

            # Wait a moment then fetch actual fill details from Robinhood
            fill = self._wait_for_fill(order_id)
            if fill:
                real_price = float(fill.get("average_price", price))
                real_qty = float(fill.get("filled_asset_quantity", asset_quantity))
                real_amount = real_price * real_qty
                log.info(f"Buy FILLED - {real_qty:.8f} BTC at ${real_price:,.2f} = ${real_amount:.2f}")
                add_trade("buy", real_price, real_qty, real_amount)
                self.buy_price = real_price
                self.peak_price = real_price
            else:
                log.info(f"Using estimated fill: {asset_quantity:.8f} BTC at ${price:,.2f}")
                add_trade("buy", price, asset_quantity, quote_amount)
                self.buy_price = price
                self.peak_price = price

            self.buy_time = time.time()
            self.last_trade_time = time.time()
            self.position_open = True
        else:
            errors = result.get("errors", []) if result else []
            log.error(f"Buy order failed: {errors}")

    def execute_sell(self, price, stop_loss=False):
        """Sell all BTC — paper or real."""
        if not self.position_open:
            log.info("SELL skipped — no open position")
            return

        if not stop_loss:
            # Min hold time check
            held_for = time.time() - self.buy_time if self.buy_time > 0 else 999
            if held_for < MIN_HOLD_TIME:
                remaining = int(MIN_HOLD_TIME - held_for)
                log.info(f"SELL skipped — min hold time ({remaining}s remaining)")
                return

            # Min profit check
            if self.buy_price > 0:
                profit_pct = (price - self.buy_price) / self.buy_price
                if profit_pct < MIN_PROFIT_PCT:
                    log.info(f"SELL skipped — profit {profit_pct*100:.3f}% below min {MIN_PROFIT_PCT*100:.2f}%")
                    return

        if PAPER_TRADING:
            if self.paper_btc <= 0:
                log.warning("No paper BTC to sell")
                return
            sell_amount = self.paper_btc * price
            profit = sell_amount - (self.paper_btc * self.buy_price)
            self.paper_balance += sell_amount
            reason = "STOP-LOSS" if stop_loss else "signal"
            log.info(f"[PAPER] SELL {self.paper_btc:.8f} BTC at ${price:,.2f} = ${sell_amount:.2f} ({reason}) | P&L: ${profit:+.2f} | Balance: ${self.paper_balance:.2f}")
            add_trade("sell", price, self.paper_btc, sell_amount)
            self.paper_btc = 0.0
            self.position_open = False
            self.buy_price = 0
            self.last_trade_time = time.time()
            return

        if not stop_loss:
            pnl = get_pnl()
            if pnl < 0:
                log.info(f"SELL blocked — P&L is -${abs(pnl):.2f}, holding until positive")
                return

        btc_held = self.get_btc_holdings()

        if btc_held <= 0:
            log.warning(f"No BTC holdings to sell")
            return

        # Get fresh bid price and set limit just $LIMIT_BUFFER below it
        try:
            bid, _ = self.get_bid_ask()
            limit_price = round(bid - LIMIT_BUFFER, 2)
        except Exception:
            limit_price = round(price - LIMIT_BUFFER, 2)

        # Sell all holdings from this position
        sell_quantity_str = f"{btc_held:.8f}"
        limit_price_str = f"{limit_price:.2f}"

        reason = "STOP-LOSS" if stop_loss else "full position"
        log.info(f"SELLING {sell_quantity_str} {ASSET_CODE} ({reason}, limit ${limit_price_str})")
        result = self.client.place_order(
            side="sell",
            order_type="limit",
            symbol=SYMBOL,
            order_config={
                "asset_quantity": sell_quantity_str,
                "limit_price": limit_price_str,
                "time_in_force": "gtc",
            },
        )
        if result and result.get("id") and "errors" not in result:
            order_id = result.get("id")
            order_state = result.get("state", "unknown")
            log.info(f"Sell order placed - ID: {order_id}, State: {order_state}")

            # Wait then fetch actual fill details
            fill = self._wait_for_fill(order_id)
            if fill:
                real_price = float(fill.get("average_price", price))
                real_qty = float(fill.get("filled_asset_quantity", btc_held))
                real_amount = real_price * real_qty
                log.info(f"Sell FILLED - {real_qty:.8f} BTC at ${real_price:,.2f} = ${real_amount:.2f}")
                add_trade("sell", real_price, real_qty, real_amount)
            else:
                estimated_amount = btc_held * price
                add_trade("sell", price, btc_held, estimated_amount)

            self.position_open = False
        else:
            errors = result.get("errors", []) if result else []
            log.error(f"Sell order failed: {errors}")

    def _wait_for_fill(self, order_id, max_wait=30):
        """Poll order status until filled or timeout."""
        for i in range(max_wait):
            time.sleep(1)
            order = self.client.get_order(order_id)
            if order and order.get("state") == "filled":
                return order
            if order and order.get("state") in ("canceled", "failed"):
                log.warning(f"Order {order_id} {order.get('state')}")
                return None
        log.warning(f"Order {order_id} not filled after {max_wait}s, using estimate")
        return None

    def sync_position(self):
        """Sync position state with Robinhood — checks order history and holdings."""
        try:
            orders = self.client.get_orders()
            if not orders:
                return

            results = orders.get("results", [])
            filled = [o for o in results if o.get("state") == "filled"]
            if not filled:
                return

            filled.sort(key=lambda o: o.get("created_at", ""))
            last_order = filled[-1]
            last_side = last_order.get("side", "")

            if last_side == "sell" and self.position_open:
                self.position_open = False
                self.buy_price = 0
                log.info("Position sync: Robinhood shows last order was SELL — clearing position")
            elif last_side == "buy":
                last_buy_price = float(last_order.get("average_price", 0))
                if not self.position_open:
                    self.position_open = True
                    self.buy_price = last_buy_price
                    self.peak_price = max(self.peak_price, last_buy_price)
                    log.info(f"Position sync: Robinhood shows last order was BUY at ${self.buy_price:,.2f} — marking position open")
                elif self.buy_price == 0 and last_buy_price > 0:
                    self.buy_price = last_buy_price
                    self.peak_price = max(self.peak_price, last_buy_price)
                    log.info(f"Position sync: Set buy_price to ${self.buy_price:,.2f} from Robinhood history")
        except Exception as e:
            log.warning(f"Position sync failed: {e}")

    def load_trade_history(self):
        """Load filled orders from Robinhood into the dashboard.
        Also detects if we have an open position based on the last trade."""
        try:
            orders = self.client.get_orders()
            if not orders:
                return

            results = orders.get("results", [])
            if not results:
                log.info("No previous orders found on Robinhood")
                return

            filled = [o for o in results if o.get("state") == "filled"]
            filled.sort(key=lambda o: o.get("created_at", ""))

            count = 0
            last_side = None
            for order in filled:
                side = order.get("side", "")
                avg_price = float(order.get("average_price", 0))
                qty = float(order.get("filled_asset_quantity", 0))
                amount = avg_price * qty
                created = order.get("created_at", "")

                if avg_price > 0 and qty > 0:
                    # Format Robinhood timestamp for display
                    ts = created[:19].replace("T", " ") if created else None
                    add_trade(side, avg_price, qty, amount, timestamp=ts)
                    count += 1
                    last_side = side

            log.info(f"Loaded {count} filled orders from Robinhood history")

            # If last filled order was a buy, we have an open position
            if last_side == "buy" and not self.position_open:
                self.position_open = True
                log.info("Last Robinhood order was a BUY — marking position as open")

        except Exception as e:
            log.warning(f"Could not load trade history: {e}")

    def run(self):
        """Main trading loop."""
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        # Start web dashboard
        start_dashboard()
        log.info(f"Dashboard running at http://localhost:{DASHBOARD_PORT}")

        mode = "PAPER TRADING" if PAPER_TRADING else "LIVE TRADING"
        log.info("=" * 60)
        log.info(f"Robinhood Crypto Trading Bot Starting [{mode}]")
        log.info(f"Symbol: {SYMBOL} | Interval: {CHECK_INTERVAL}s | Trade: ${TRADE_AMOUNT:.2f} | Confirm: {SIGNAL_CONFIRM}x | Stop-loss: {STOP_LOSS_PERCENT*100:.0f}%")
        log.info(f"Cooldown: {TRADE_COOLDOWN}s | Min hold: {MIN_HOLD_TIME}s | Min profit: {MIN_PROFIT_PCT*100:.2f}%")
        if PAPER_TRADING:
            log.info(f"Paper balance: ${PAPER_BALANCE:.2f} — no real money will be used")
        log.info("=" * 60)

        update_state(symbol=SYMBOL)

        if PAPER_TRADING:
            # Paper mode — just verify API connectivity for price data
            try:
                price = self.get_mid_price()
                log.info(f"API connected — current {SYMBOL}: ${price:,.2f}")
                update_state(
                    buying_power=self.paper_balance,
                    btc_held=0,
                    starting_value=self.paper_balance,
                )
            except Exception as e:
                log.error(f"Failed to connect to API: {e}")
                return
        else:
            # Live mode — verify account
            try:
                buying_power = self.get_buying_power()
                btc_held = self.get_btc_holdings()
                log.info(f"Account connected - Buying power: ${buying_power:.2f} | {ASSET_CODE} held: {btc_held:.8f}")
                if btc_held > 0:
                    self.position_open = True
                    log.info(f"Existing BTC position detected — marking position as open")
                update_state(
                    buying_power=buying_power,
                    btc_held=btc_held,
                    starting_value=buying_power,
                )
            except Exception as e:
                log.error(f"Failed to connect to account: {e}")
                log.error("Check your API_KEY and BASE64_PRIVATE_KEY in config.py")
                return

            # Load trade history from Robinhood (live mode only)
            self.load_trade_history()

        warmup_time = MIN_REQUIRED * WARMUP_INTERVAL // 60
        log.info(f"Collecting price data... need {MIN_REQUIRED} data points (~{warmup_time} min warmup at {WARMUP_INTERVAL}s intervals)")

        while self.running:
            try:
                # Fetch current price
                price = self.get_mid_price()
                self.prices.append(price)
                log.info(f"Price: ${price:,.2f} | Data points: {len(self.prices)}/{MIN_REQUIRED}")

                # Sync position state
                if PAPER_TRADING:
                    buying_power = self.paper_balance
                    btc_held = self.paper_btc
                else:
                    self.sync_position()
                    try:
                        buying_power = self.get_buying_power()
                        btc_held = self.get_btc_holdings()
                    except Exception:
                        buying_power = 0
                        btc_held = 0

                # Get signal with details
                details = get_signal_details(self.prices)
                sig = details["signal"]

                # Calculate gaps and readiness
                ema_s = details.get("ema_short", 0)
                ema_l = details.get("ema_long", 0)
                ema_t = details.get("ema_trend", 0)
                rsi = details.get("rsi", 0)
                macd_line = details.get("macd_line", 0)
                macd_sig = details.get("macd_signal", 0)
                macd_hist = details.get("macd_hist", 0)
                bb_upper = details.get("bb_upper", 0)
                bb_middle = details.get("bb_middle", 0)
                bb_lower = details.get("bb_lower", 0)

                # Add price point with all indicator data for chart
                add_price_point(price, ema_short=ema_s, ema_long=ema_l, rsi=rsi,
                                ema_trend=ema_t, macd_line=macd_line,
                                macd_signal=macd_sig, macd_hist=macd_hist,
                                bb_upper=bb_upper, bb_lower=bb_lower)

                ema_gap = round(ema_s - ema_l, 2) if ema_s and ema_l else 0
                rsi_gap_buy = round(rsi - RSI_OVERSOLD, 2) if rsi else 0
                rsi_gap_sell = round(rsi - RSI_OVERBOUGHT, 2) if rsi else 0
                trend_status = "UP" if price > ema_t else "DOWN" if ema_t else "--"
                macd_status = "Bullish" if macd_hist > 0 else "Bearish" if macd_hist else "--"

                # Determine buy readiness
                if sig == "WARMUP":
                    buy_readiness = "Warming up..."
                    sell_readiness = "Warming up..."
                else:
                    ema_buy_ok = ema_gap < 0
                    rsi_buy_ok = rsi_gap_buy <= 0
                    trend_up = price > ema_t if ema_t else False
                    macd_bullish = macd_hist > 0

                    # Check dip buy conditions
                    dip_conditions = []
                    if ema_buy_ok: dip_conditions.append("EMA9<21")
                    if rsi_buy_ok: dip_conditions.append(f"RSI {rsi:.0f}")
                    if trend_up: dip_conditions.append("Trend UP")
                    if macd_bullish: dip_conditions.append("MACD +")

                    # Check momentum buy conditions
                    mom_ema = ema_gap > 0
                    mom_macd = macd_hist > 0
                    mom_trend = trend_up
                    mom_rsi = 50 <= rsi <= 70
                    mom_conditions = []
                    if mom_ema: mom_conditions.append("EMA9>21")
                    if mom_macd: mom_conditions.append("MACD +")
                    if mom_trend: mom_conditions.append("Trend UP")
                    if mom_rsi: mom_conditions.append(f"RSI {rsi:.0f}")

                    dip_met = len(dip_conditions)
                    mom_met = len(mom_conditions)

                    # Show whichever buy type is closer
                    if dip_met >= 3:
                        buy_readiness = f"DIP READY -- {', '.join(dip_conditions)}"
                    elif mom_met >= 4:
                        buy_readiness = f"MOMENTUM READY -- {', '.join(mom_conditions)}"
                    elif mom_met >= dip_met:
                        missing = []
                        if not mom_ema: missing.append(f"EMA gap ${abs(ema_gap):.0f}")
                        if not mom_macd: missing.append("MACD -")
                        if not mom_trend: missing.append(f"Trend {trend_status}")
                        if not mom_rsi: missing.append(f"RSI {rsi:.0f} (need 50-70)")
                        buy_readiness = f"Momentum {mom_met}/4 -- Need: {', '.join(missing)}"
                    else:
                        missing = []
                        if not ema_buy_ok: missing.append(f"EMA gap ${abs(ema_gap):.0f}")
                        if not rsi_buy_ok: missing.append(f"RSI {rsi_gap_buy:+.1f}")
                        if not trend_up and not macd_bullish: missing.append(f"Trend {trend_status}, MACD {macd_status}")
                        buy_readiness = f"Dip {dip_met}/3 -- Need: {', '.join(missing)}"

                    ema_sell_ok = ema_gap > 0
                    rsi_sell_ok = rsi_gap_sell >= 0
                    if ema_sell_ok and rsi_sell_ok:
                        sell_readiness = f"READY -- EMA9>21, RSI {rsi:.0f}"
                    else:
                        missing = []
                        if not ema_sell_ok: missing.append(f"EMA gap ${abs(ema_gap):.0f}")
                        if not rsi_sell_ok: missing.append(f"RSI {abs(rsi_gap_sell):.1f} from zone")
                        sell_readiness = f"Need: {', '.join(missing)}"

                update_state(
                    current_price=price,
                    signal=sig,
                    ema_short=ema_s,
                    ema_long=ema_l,
                    ema_trend=ema_t,
                    rsi=rsi,
                    ema_gap=ema_gap,
                    rsi_gap_buy=rsi_gap_buy,
                    rsi_gap_sell=rsi_gap_sell,
                    macd_line=macd_line,
                    macd_signal=macd_sig,
                    macd_hist=macd_hist,
                    bb_upper=bb_upper,
                    bb_middle=bb_middle,
                    bb_lower=bb_lower,
                    trend_status=trend_status,
                    macd_status=macd_status,
                    buy_readiness=buy_readiness,
                    sell_readiness=sell_readiness,
                    buying_power=buying_power,
                    btc_held=btc_held,
                    current_value=buying_power + btc_held * price,
                    data_points=len(self.prices),
                    last_update=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )

                # Trailing stop-loss — track peak and sell on drop
                if self.position_open and self.buy_price > 0:
                    # Update peak price
                    if price > self.peak_price:
                        self.peak_price = price
                    # Check drop from peak (trailing) or from buy (initial protection)
                    drop_from_peak = (self.peak_price - price) / self.peak_price if self.peak_price > 0 else 0
                    drop_from_buy = (self.buy_price - price) / self.buy_price
                    if drop_from_peak >= STOP_LOSS_PERCENT and self.peak_price > self.buy_price:
                        profit = self.peak_price - self.buy_price
                        log.warning(f"TRAILING STOP triggered! Price ${price:,.2f} dropped {drop_from_peak*100:.1f}% from peak ${self.peak_price:,.2f} (bought ${self.buy_price:,.2f}, peak profit was ${profit:,.2f})")
                        self.execute_sell(price, stop_loss=True)
                    elif drop_from_buy >= STOP_LOSS_PERCENT:
                        log.warning(f"STOP-LOSS triggered! Price ${price:,.2f} is {drop_from_buy*100:.1f}% below buy ${self.buy_price:,.2f}")
                        self.execute_sell(price, stop_loss=True)
                    # Clear state after successful sell
                    if not self.position_open:
                        self.buy_price = 0
                        self.peak_price = 0

                if sig == "WARMUP":
                    log.info(f"Warming up... {details['data_points']}/{details['required']} data points")
                elif self.warmup_cooldown > 0:
                    # Skip first 10 signals after warmup — data from 15s intervals isn't reliable
                    self.warmup_cooldown -= 1
                    log.info(f"Post-warmup cooldown ({self.warmup_cooldown} remaining) | EMA9: {details['ema_short']} EMA21: {details['ema_long']} RSI: {details['rsi']}")
                elif sig in ("BUY", "SELL"):
                    # Signal confirmation — require SIGNAL_CONFIRM consecutive same signals
                    if sig == self.last_signal:
                        self.consecutive_signal += 1
                    else:
                        self.consecutive_signal = 1
                    self.last_signal = sig

                    if self.consecutive_signal < SIGNAL_CONFIRM:
                        log.info(f"{sig} SIGNAL ({self.consecutive_signal}/{SIGNAL_CONFIRM} confirms) | EMA9: {details['ema_short']} EMA21: {details['ema_long']} RSI: {details['rsi']}")
                    elif sig == "BUY":
                        log.info(f"BUY CONFIRMED ({SIGNAL_CONFIRM}x) | EMA9: {details['ema_short']} EMA21: {details['ema_long']} RSI: {details['rsi']}")
                        self.execute_buy(price)
                        self.consecutive_signal = 0
                    elif sig == "SELL":
                        log.info(f"SELL CONFIRMED ({SIGNAL_CONFIRM}x) | EMA9: {details['ema_short']} EMA21: {details['ema_long']} RSI: {details['rsi']}")
                        self.execute_sell(price)
                        self.consecutive_signal = 0
                else:
                    self.consecutive_signal = 0
                    self.last_signal = None
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
                        self.warmup_cooldown = 10  # Skip first 10 signals (~2.5 min at 15s)
                        log.info(f"Warmup complete! Cooldown for 10 cycles before trading, switching to {CHECK_INTERVAL}s intervals")
                    time.sleep(CHECK_INTERVAL)

        log.info("Bot stopped.")


if __name__ == "__main__":
    trader = Trader()
    trader.run()
