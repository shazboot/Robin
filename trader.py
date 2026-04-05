import logging
import signal
import sys
import time
from datetime import datetime, timezone

from api_client import CryptoAPITrading
from strategy import (get_signal, get_signal_details, get_15m_direction,
                      check_1m_entry, MIN_REQUIRED, MIN_REQUIRED_15M)
from config import (SYMBOL, ASSET_CODE, TRADE_AMOUNT, CHECK_INTERVAL, WARMUP_INTERVAL,
                     LOG_FILE, RSI_OVERSOLD, RSI_OVERBOUGHT, STOP_LOSS_PERCENT,
                     LIMIT_BUFFER, PAPER_TRADING, PAPER_BALANCE,
                     TRADE_COOLDOWN, MIN_HOLD_TIME, MIN_PROFIT_PCT,
                     MAX_DAILY_DRAWDOWN, ENTRY_WINDOW, ENTRY_RSI_MAX, ENTRY_RSI_DIP,
                     SEED_15M_CANDLES, SEED_15M_HOURS, BACKTEST_SYMBOL)
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
        # Paper trading state
        self.paper_balance = PAPER_BALANCE
        self.paper_btc = 0.0
        # 1m candle aggregation state
        self.candle_closes = []     # List of completed 1-min candle close prices
        self.candle_highs = []      # For ATR calculation
        self.candle_lows = []       # For ATR calculation
        self.candle_volumes = []    # Tick volume per candle
        self.current_candle = None  # {open, high, low, close, ticks, movement}
        self.candle_start_time = 0
        self.tick_count = 0         # For logging interval
        self.last_tick_price = 0    # For tick volume calculation
        # 15m OHLCV aggregation state
        self.candle_15m_closes = []
        self.candle_15m_highs = []
        self.candle_15m_lows = []
        self.candle_15m_volumes = []
        self.candle_15m_buffer = []  # Buffer of 1m candles to aggregate into 15m
        self.candle_1m_count = 0     # Count of completed 1m candles (for 15m aggregation)
        # 15m direction + 1m entry state machine
        self.direction_15m = "WARMUP"      # Current 15m direction signal
        self.looking_for_entry = False      # True when 15m says BUY, waiting for 1m entry
        self.entry_window_start = 0        # Timestamp when entry window opened
        self.consecutive_15m_signal = 0    # Count consecutive same 15m signals
        self.last_15m_signal = None        # Track last 15m signal for confirmation
        self.last_entry_type = ""          # How we entered (for logging)
        # 15m indicator cache (for dashboard)
        self.last_15m_details = {}
        # Daily drawdown state
        self.daily_start_balance = PAPER_BALANCE if PAPER_TRADING else 0
        self.daily_pnl = 0.0
        self.trading_halted = False
        self.last_reset_date = datetime.now(timezone.utc).date()
        # Analytics state
        self.trade_history = []     # List of completed trade dicts

    def shutdown(self, signum, frame):
        log.info("Shutting down gracefully...")
        self.running = False

    def update_candle(self, price: float) -> tuple:
        """Aggregate tick into current 1-minute candle.
        Tracks OHLC and tick volume (sum of absolute price movements).
        Returns (candle_1m_completed, candle_15m_completed)."""
        now = time.time()

        # Track tick volume (absolute price movement)
        movement = abs(price - self.last_tick_price) if self.last_tick_price > 0 else 0
        self.last_tick_price = price

        if self.current_candle is None:
            self.current_candle = {
                'open': price, 'high': price, 'low': price, 'close': price,
                'ticks': 1, 'movement': 0,
            }
            self.candle_start_time = now
            return False, False

        self.current_candle['high'] = max(self.current_candle['high'], price)
        self.current_candle['low'] = min(self.current_candle['low'], price)
        self.current_candle['close'] = price
        self.current_candle['ticks'] += 1
        self.current_candle['movement'] += movement

        if now - self.candle_start_time >= 60.0:
            completed_candle = dict(self.current_candle)
            self.candle_closes.append(completed_candle['close'])
            self.candle_highs.append(completed_candle['high'])
            self.candle_lows.append(completed_candle['low'])
            self.candle_volumes.append(completed_candle['movement'])
            # Cap history at 1000 candles to prevent memory growth
            if len(self.candle_closes) > 1000:
                self.candle_closes = self.candle_closes[-1000:]
                self.candle_highs = self.candle_highs[-1000:]
                self.candle_lows = self.candle_lows[-1000:]
                self.candle_volumes = self.candle_volumes[-1000:]
            self.current_candle = {
                'open': price, 'high': price, 'low': price, 'close': price,
                'ticks': 1, 'movement': 0,
            }
            self.candle_start_time = now

            # 15m OHLCV aggregation: buffer 1m candles, aggregate every 15th
            self.candle_15m_buffer.append(completed_candle)
            self.candle_1m_count += 1
            candle_15m_done = False

            if self.candle_1m_count % 15 == 0:
                buf = self.candle_15m_buffer
                self.candle_15m_closes.append(buf[-1]['close'])
                self.candle_15m_highs.append(max(c['high'] for c in buf))
                self.candle_15m_lows.append(min(c['low'] for c in buf))
                self.candle_15m_volumes.append(sum(c['movement'] for c in buf))
                self.candle_15m_buffer = []
                # Cap 15m history
                if len(self.candle_15m_closes) > 200:
                    self.candle_15m_closes = self.candle_15m_closes[-200:]
                    self.candle_15m_highs = self.candle_15m_highs[-200:]
                    self.candle_15m_lows = self.candle_15m_lows[-200:]
                    self.candle_15m_volumes = self.candle_15m_volumes[-200:]
                log.info(f"15m candle #{len(self.candle_15m_closes)} closed at ${buf[-1]['close']:,.2f}")
                candle_15m_done = True

            return True, candle_15m_done

        return False, False

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

    def _check_daily_reset(self, current_balance):
        """Reset daily drawdown tracking at midnight UTC."""
        today = datetime.now(timezone.utc).date()
        if today != self.last_reset_date:
            self.last_reset_date = today
            self.daily_pnl = 0.0
            self.trading_halted = False
            self.daily_start_balance = current_balance
            log.info(f"Daily reset — drawdown tracking restarted, balance: ${current_balance:.2f}")

    def _record_trade(self, buy_price, sell_price, quantity, exit_type):
        """Record a completed trade for analytics and update daily P&L."""
        profit_usd = (sell_price - buy_price) * quantity
        profit_pct = (sell_price - buy_price) / buy_price if buy_price > 0 else 0
        hold_time = time.time() - self.buy_time if self.buy_time > 0 else 0

        self.trade_history.append({
            "buy_price": round(buy_price, 2),
            "sell_price": round(sell_price, 2),
            "profit_pct": round(profit_pct * 100, 4),
            "profit_usd": round(profit_usd, 2),
            "hold_time_sec": round(hold_time),
            "exit_type": exit_type,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        })

        # Update daily P&L
        self.daily_pnl += profit_usd
        if self.daily_start_balance > 0:
            daily_drawdown = self.daily_pnl / self.daily_start_balance
            if daily_drawdown <= -MAX_DAILY_DRAWDOWN:
                self.trading_halted = True
                log.warning(f"TRADING HALTED — daily drawdown limit hit: ${self.daily_pnl:.2f} ({daily_drawdown*100:.2f}%)")

        # Push analytics to dashboard
        analytics = self._calc_analytics()
        daily_drawdown_pct = (self.daily_pnl / self.daily_start_balance * 100) if self.daily_start_balance > 0 else 0
        update_state(
            analytics=analytics,
            daily_pnl=round(self.daily_pnl, 2),
            daily_drawdown_pct=round(daily_drawdown_pct, 2),
            trading_halted=self.trading_halted,
        )

    def _calc_analytics(self):
        """Calculate performance analytics from trade history."""
        if not self.trade_history:
            return {}

        trades = self.trade_history
        total = len(trades)
        wins = [t for t in trades if t["profit_usd"] > 0]
        losses = [t for t in trades if t["profit_usd"] <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total * 100) if total > 0 else 0

        avg_win_pct = sum(t["profit_pct"] for t in wins) / win_count if wins else 0
        avg_loss_pct = sum(t["profit_pct"] for t in losses) / loss_count if losses else 0

        gross_wins = sum(t["profit_usd"] for t in wins)
        gross_losses = abs(sum(t["profit_usd"] for t in losses))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float('inf') if gross_wins > 0 else 0

        # Max drawdown (peak-to-trough of cumulative P&L)
        cum_pnl = 0
        peak_pnl = 0
        max_dd = 0
        for t in trades:
            cum_pnl += t["profit_usd"]
            if cum_pnl > peak_pnl:
                peak_pnl = cum_pnl
            dd = peak_pnl - cum_pnl
            if dd > max_dd:
                max_dd = dd
        max_dd_pct = (max_dd / self.daily_start_balance * 100) if self.daily_start_balance > 0 else 0

        best = max(trades, key=lambda t: t["profit_usd"])
        worst = min(trades, key=lambda t: t["profit_usd"])

        avg_hold = sum(t["hold_time_sec"] for t in trades) / total

        # Current streak
        streak = 0
        if trades:
            last_win = trades[-1]["profit_usd"] > 0
            for t in reversed(trades):
                if (t["profit_usd"] > 0) == last_win:
                    streak += 1
                else:
                    break
            if not last_win:
                streak = -streak

        return {
            "total_trades": total,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round(win_rate, 1),
            "avg_win_pct": round(avg_win_pct, 2),
            "avg_loss_pct": round(avg_loss_pct, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "Inf",
            "max_drawdown_pct": round(max_dd_pct, 2),
            "best_trade": round(best["profit_usd"], 2),
            "worst_trade": round(worst["profit_usd"], 2),
            "avg_hold_time": round(avg_hold),
            "current_streak": streak,
        }

    def execute_buy(self, price):
        """Buy BTC — paper or real."""
        if self.trading_halted:
            log.info("BUY skipped — trading halted (daily drawdown limit)")
            return

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
            self._record_trade(self.buy_price, price, self.paper_btc, reason)
            self.paper_btc = 0.0
            self.position_open = False
            self.buy_price = 0
            self.last_trade_time = time.time()
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
            reason = "STOP-LOSS" if stop_loss else "signal"
            if fill:
                real_price = float(fill.get("average_price", price))
                real_qty = float(fill.get("filled_asset_quantity", btc_held))
                real_amount = real_price * real_qty
                log.info(f"Sell FILLED - {real_qty:.8f} BTC at ${real_price:,.2f} = ${real_amount:.2f}")
                add_trade("sell", real_price, real_qty, real_amount)
                self._record_trade(self.buy_price, real_price, real_qty, reason)
            else:
                estimated_amount = btc_held * price
                add_trade("sell", price, btc_held, estimated_amount)
                self._record_trade(self.buy_price, price, btc_held, reason)

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

    def seed_historical_15m(self):
        """Seed 15m candle history from Binance to avoid long warmup."""
        try:
            from backtest import fetch_binance_klines
            hours = SEED_15M_HOURS
            days_fraction = hours / 24.0
            log.info(f"Seeding 15m candles from Binance ({hours}h of data)...")
            candles = fetch_binance_klines(BACKTEST_SYMBOL, "15m", days=max(1, int(days_fraction + 0.5)))
            if not candles:
                log.warning("Failed to fetch 15m seed data — will warm up naturally")
                return

            # Take the last N hours worth
            max_candles = int(hours * 4)  # 4 candles per hour at 15m
            candles = candles[-max_candles:]

            for c in candles:
                self.candle_15m_closes.append(c["close"])
                self.candle_15m_highs.append(c["high"])
                self.candle_15m_lows.append(c["low"])
                self.candle_15m_volumes.append(c["volume"])

            log.info(f"Seeded {len(candles)} 15m candles (need {MIN_REQUIRED_15M} for signals)")
        except Exception as e:
            log.warning(f"15m seed failed: {e} — will warm up naturally")

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
        log.info(f"Symbol: {SYMBOL} | Scan: {CHECK_INTERVAL}s | Candle: 60s | Trade: ${TRADE_AMOUNT:.2f} | Stop-loss: {STOP_LOSS_PERCENT*100:.0f}%")
        log.info(f"Strategy: 15m direction + 1m entry | Entry window: {ENTRY_WINDOW}s | Entry RSI dip: {ENTRY_RSI_DIP}")
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
                sys.exit(1)
        else:
            # Live mode — verify account
            try:
                buying_power = self.get_buying_power()
                btc_held = self.get_btc_holdings()
                log.info(f"Account connected - Buying power: ${buying_power:.2f} | {ASSET_CODE} held: {btc_held:.8f}")
                if btc_held > 0:
                    self.position_open = True
                    log.info(f"Existing BTC position detected — marking position as open")
                self.daily_start_balance = buying_power
                update_state(
                    buying_power=buying_power,
                    btc_held=btc_held,
                    starting_value=buying_power,
                )
            except Exception as e:
                log.error(f"Failed to connect to account: {e}")
                log.error("Check your API_KEY and BASE64_PRIVATE_KEY in config.py")
                sys.exit(1)

            # Load trade history from Robinhood (live mode only)
            self.load_trade_history()

        # Seed 15m candle history from Binance
        if SEED_15M_CANDLES:
            self.seed_historical_15m()

        log.info(f"15m candles: {len(self.candle_15m_closes)}/{MIN_REQUIRED_15M} | Collecting 1m candles for entry timing...")

        last_account_sync = 0
        ACCOUNT_SYNC_INTERVAL = 30  # Sync account/holdings every 30s (not every tick)

        while self.running:
            try:
                # Fetch current price (every tick)
                price = self.get_mid_price()
                self.tick_count += 1

                # Update candle aggregation
                candle_1m_done, candle_15m_done = self.update_candle(price)

                # Sync account state periodically (not every tick) to reduce API calls
                now = time.time()
                if PAPER_TRADING:
                    buying_power = self.paper_balance
                    btc_held = self.paper_btc
                    if now - last_account_sync >= ACCOUNT_SYNC_INTERVAL:
                        self._check_daily_reset(self.paper_balance)
                        last_account_sync = now
                else:
                    if now - last_account_sync >= ACCOUNT_SYNC_INTERVAL:
                        self.sync_position()
                        try:
                            buying_power = self.get_buying_power()
                            btc_held = self.get_btc_holdings()
                        except Exception:
                            buying_power = 0
                            btc_held = 0
                        self._check_daily_reset(buying_power if buying_power else self.daily_start_balance)
                        last_account_sync = now
                        self._last_buying_power = buying_power
                        self._last_btc_held = btc_held
                    else:
                        buying_power = getattr(self, '_last_buying_power', 0)
                        btc_held = getattr(self, '_last_btc_held', 0)

                # ALWAYS run trailing stop-loss on every tick (fast protection)
                if self.position_open and self.buy_price > 0:
                    if price > self.peak_price:
                        self.peak_price = price
                    profit_pct = (price - self.buy_price) / self.buy_price
                    peak_profit_pct = (self.peak_price - self.buy_price) / self.buy_price
                    drop_from_peak = (self.peak_price - price) / self.peak_price if self.peak_price > 0 else 0
                    drop_from_buy = (self.buy_price - price) / self.buy_price

                    # === Ratcheting stop-loss system ===
                    # Tier 3: Peak >= 2% — trail 1.5% from peak (let big winners run)
                    if peak_profit_pct >= 0.02 and drop_from_peak >= 0.015 and profit_pct > 0:
                        locked_profit = profit_pct * 100
                        log.info(f"TAKE-PROFIT triggered! Price ${price:,.2f} dropped {drop_from_peak*100:.2f}% from peak ${self.peak_price:,.2f} | Locking in {locked_profit:.2f}% profit (bought ${self.buy_price:,.2f})")
                        self.execute_sell(price, stop_loss=True)
                    # Tier 2: Peak >= 1% — floor at breakeven (never lose a winner)
                    elif peak_profit_pct >= 0.01 and price <= self.buy_price:
                        log.warning(f"BREAKEVEN STOP triggered! Price ${price:,.2f} fell back to buy ${self.buy_price:,.2f} (was up {peak_profit_pct*100:.2f}% at peak ${self.peak_price:,.2f})")
                        self.execute_sell(price, stop_loss=True)
                    # Tier 1: Peak < 1% — 2.5% trailing stop (give room to develop)
                    elif drop_from_peak >= 0.025 and self.peak_price > self.buy_price:
                        profit = self.peak_price - self.buy_price
                        log.warning(f"TRAILING STOP triggered! Price ${price:,.2f} dropped {drop_from_peak*100:.1f}% from peak ${self.peak_price:,.2f} (bought ${self.buy_price:,.2f}, peak profit was ${profit:,.2f})")
                        self.execute_sell(price, stop_loss=True)
                    # Hard stop: 3% below buy price (disaster protection)
                    elif drop_from_buy >= STOP_LOSS_PERCENT:
                        log.warning(f"STOP-LOSS triggered! Price ${price:,.2f} is {drop_from_buy*100:.1f}% below buy ${self.buy_price:,.2f}")
                        self.execute_sell(price, stop_loss=True)
                    if not self.position_open:
                        self.buy_price = 0
                        self.peak_price = 0

                # Update dashboard price every tick (smooth chart)
                entry_remaining = 0
                if self.looking_for_entry and self.entry_window_start > 0:
                    entry_remaining = max(0, ENTRY_WINDOW - (time.time() - self.entry_window_start))

                update_state(
                    current_price=price,
                    buying_power=buying_power,
                    btc_held=btc_held,
                    current_value=buying_power + btc_held * price,
                    data_points=len(self.candle_closes),
                    data_points_15m=len(self.candle_15m_closes),
                    last_update=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    direction_15m=self.direction_15m,
                    looking_for_entry=self.looking_for_entry,
                    entry_window_remaining=round(entry_remaining),
                    entry_type=self.last_entry_type,
                )

                # Log progress every ~30s between candles
                candle_elapsed = int(time.time() - self.candle_start_time) if self.candle_start_time else 0
                if self.tick_count % 15 == 0 and not candle_1m_done:
                    entry_str = f" | Entry window: {int(entry_remaining)}s" if self.looking_for_entry else ""
                    log.info(f"Price: ${price:,.2f} | 15m: {self.direction_15m} | Candle: {candle_elapsed}s/60s | 1m: {len(self.candle_closes)} | 15m: {len(self.candle_15m_closes)}/{MIN_REQUIRED_15M}{entry_str}")

                # === 15m CANDLE COMPLETED — evaluate direction ===
                if candle_15m_done:
                    dir_details = get_15m_direction(
                        self.candle_15m_closes,
                        highs=self.candle_15m_highs,
                        lows=self.candle_15m_lows,
                        volumes=self.candle_15m_volumes,
                    )
                    dir_sig = dir_details["signal"]
                    self.last_15m_details = dir_details

                    log.info(f"15m Signal: {dir_sig} | RSI: {dir_details.get('rsi', 0)} | MACD: {dir_details.get('macd_hist', 0)} | Trend: {dir_details.get('trend_strength', '--')}")

                    if dir_sig == "WARMUP":
                        self.direction_15m = "WARMUP"
                        log.info(f"15m warming up... {dir_details['data_points']}/{dir_details['required']} candles")
                    else:
                        # 15m signal confirmation (2x consecutive)
                        if dir_sig == self.last_15m_signal:
                            self.consecutive_15m_signal += 1
                        else:
                            self.consecutive_15m_signal = 1
                        self.last_15m_signal = dir_sig

                        if dir_sig == "BUY" and self.consecutive_15m_signal >= 2:
                            self.direction_15m = "BUY"
                            if not self.position_open and not self.looking_for_entry:
                                if self.trading_halted:
                                    log.info("15m BUY confirmed — but trading halted (daily drawdown)")
                                else:
                                    self.looking_for_entry = True
                                    self.entry_window_start = time.time()
                                    log.info(f"15m BUY confirmed (2x) — opening entry window ({ENTRY_WINDOW}s)")
                        elif dir_sig == "SELL":
                            self.direction_15m = "SELL"
                            if self.looking_for_entry:
                                self.looking_for_entry = False
                                self.entry_window_start = 0
                                log.info("15m flipped to SELL — cancelling entry window")
                            if self.position_open:
                                log.info(f"15m SELL signal — selling position")
                                self.execute_sell(price)
                        else:
                            self.direction_15m = dir_sig
                            if dir_sig == "HOLD" and self.consecutive_15m_signal >= 2:
                                if self.looking_for_entry:
                                    self.looking_for_entry = False
                                    self.entry_window_start = 0
                                    log.info("15m direction now HOLD — cancelling entry window")

                    # Update dashboard with 15m indicators
                    d15 = dir_details
                    update_state(
                        direction_15m=self.direction_15m,
                        ema_short_15m=d15.get("ema_short", 0),
                        ema_long_15m=d15.get("ema_long", 0),
                        ema_trend_15m=d15.get("ema_trend", 0),
                        rsi_15m=d15.get("rsi", 0),
                        macd_hist_15m=d15.get("macd_hist", 0),
                        trend_strength_15m=d15.get("trend_strength", "--"),
                        candles_15m=len(self.candle_15m_closes),
                    )

                # === 1m CANDLE COMPLETED — check for entry + update dashboard ===
                if candle_1m_done:
                    candle_close = self.candle_closes[-1]
                    log.info(f"1m candle #{len(self.candle_closes)} closed at ${candle_close:,.2f} | 15m dir: {self.direction_15m}")

                    # Get 1m indicators for dashboard display
                    details = get_signal_details(self.candle_closes,
                                                 highs=self.candle_highs,
                                                 lows=self.candle_lows,
                                                 volumes=self.candle_volumes)
                    sig = details["signal"]

                    ema_s = details.get("ema_short", 0)
                    ema_l = details.get("ema_long", 0)
                    ema_t = details.get("ema_trend", 0)
                    rsi = details.get("rsi", 0)
                    macd_hist = details.get("macd_hist", 0)
                    bb_upper = details.get("bb_upper", 0)
                    bb_lower = details.get("bb_lower", 0)
                    trend_strength = details.get("trend_strength", "--")
                    bb_squeeze = details.get("bb_squeeze", "--")
                    bb_bandwidth = details.get("bb_bandwidth", 0)
                    atr = details.get("atr", 0)
                    atr_pct = details.get("atr_pct", 0)
                    vol_confirmed = details.get("volume_confirmed", True)
                    divergence = details.get("divergence", "NONE")

                    # Add candle close with indicators to dashboard chart
                    add_price_point(candle_close, ema_short=ema_s, ema_long=ema_l, rsi=rsi,
                                    ema_trend=ema_t, macd_line=details.get("macd_line", 0),
                                    macd_signal=details.get("macd_signal", 0), macd_hist=macd_hist,
                                    bb_upper=bb_upper, bb_lower=bb_lower)

                    # Build readiness display
                    if self.direction_15m == "WARMUP":
                        buy_readiness = f"15m warming up ({len(self.candle_15m_closes)}/{MIN_REQUIRED_15M})"
                        sell_readiness = "Waiting for 15m..."
                    elif self.looking_for_entry:
                        elapsed_pct = (time.time() - self.entry_window_start) / ENTRY_WINDOW * 100 if self.entry_window_start else 0
                        buy_readiness = f"ENTRY WINDOW OPEN ({int(entry_remaining)}s left, {elapsed_pct:.0f}% elapsed)"
                    elif self.direction_15m == "BUY":
                        buy_readiness = f"15m BUY ({self.consecutive_15m_signal}/2 confirms)"
                    else:
                        buy_readiness = f"15m direction: {self.direction_15m}"

                    if self.position_open:
                        profit_pct = (price - self.buy_price) / self.buy_price * 100 if self.buy_price > 0 else 0
                        sell_readiness = f"Holding ({profit_pct:+.2f}%) | 15m: {self.direction_15m}"
                    elif self.direction_15m == "SELL":
                        sell_readiness = "15m SELL active"
                    else:
                        sell_readiness = f"No position | 15m: {self.direction_15m}"

                    update_state(
                        signal=self.direction_15m,
                        ema_short=ema_s,
                        ema_long=ema_l,
                        ema_trend=ema_t,
                        rsi=rsi,
                        ema_gap=round(ema_s - ema_l, 2) if ema_s and ema_l else 0,
                        rsi_gap_buy=round(rsi - RSI_OVERSOLD, 2) if rsi else 0,
                        rsi_gap_sell=round(rsi - RSI_OVERBOUGHT, 2) if rsi else 0,
                        macd_line=details.get("macd_line", 0),
                        macd_signal=details.get("macd_signal", 0),
                        macd_hist=macd_hist,
                        bb_upper=bb_upper,
                        bb_middle=details.get("bb_middle", 0),
                        bb_lower=bb_lower,
                        trend_status="UP" if price > ema_t else "DOWN" if ema_t else "--",
                        macd_status="Bullish" if macd_hist > 0 else "Bearish" if macd_hist else "--",
                        trend_strength=trend_strength,
                        bb_bandwidth=bb_bandwidth,
                        bb_squeeze=bb_squeeze,
                        atr=atr,
                        atr_pct=atr_pct,
                        volume_confirmed=vol_confirmed,
                        divergence=divergence,
                        buy_readiness=buy_readiness,
                        sell_readiness=sell_readiness,
                        looking_for_entry=self.looking_for_entry,
                        entry_window_remaining=round(entry_remaining),
                    )

                    # === 1m Entry Check (when looking for entry) ===
                    if self.looking_for_entry and not self.position_open:
                        elapsed = time.time() - self.entry_window_start
                        elapsed_pct = elapsed / ENTRY_WINDOW if ENTRY_WINDOW > 0 else 1

                        # Check if entry window expired
                        if elapsed >= ENTRY_WINDOW:
                            self.looking_for_entry = False
                            self.entry_window_start = 0
                            log.info(f"Entry window expired ({ENTRY_WINDOW}s) — no 1m entry found")
                        else:
                            # Check 1m entry conditions
                            entry = check_1m_entry(self.candle_closes,
                                                    highs=self.candle_highs,
                                                    lows=self.candle_lows,
                                                    volumes=self.candle_volumes)

                            if entry["entry_ok"]:
                                self.last_entry_type = entry["entry_type"]
                                log.info(f"1m ENTRY found: {entry['entry_type']} | RSI: {entry['rsi']} | Buying at ${price:,.2f}")
                                self.execute_buy(price)
                                self.looking_for_entry = False
                                self.entry_window_start = 0
                            elif elapsed_pct >= 0.80 and entry["rsi"] < ENTRY_RSI_MAX:
                                # Fallback: >80% of window elapsed, RSI acceptable
                                self.last_entry_type = "FALLBACK"
                                log.info(f"1m FALLBACK entry ({elapsed_pct*100:.0f}% elapsed, RSI {entry['rsi']}) | Buying at ${price:,.2f}")
                                self.execute_buy(price)
                                self.looking_for_entry = False
                                self.entry_window_start = 0
                            else:
                                log.info(f"1m entry: waiting | RSI: {entry['rsi']} | Window: {int(elapsed)}s/{ENTRY_WINDOW}s ({elapsed_pct*100:.0f}%)")

            except Exception as e:
                log.error(f"Error in trading loop: {e}")

            if self.running:
                time.sleep(CHECK_INTERVAL)

        log.info("Bot stopped.")


if __name__ == "__main__":
    trader = Trader()
    trader.run()
