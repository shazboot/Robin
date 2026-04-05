#!/usr/bin/env python3
"""
Backtesting engine for the BTC trading bot.
Fetches historical kline data from Binance and replays the strategy.

Usage:
    python backtest.py                           # Default: 30 days, current config
    python backtest.py --days 60                 # Custom period
    python backtest.py --stop-loss 0.02 --min-profit 0.005  # Parameter override
"""

import argparse
import json
import logging
import multiprocessing
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

from strategy import (get_signal, get_signal_details, get_15m_direction,
                      check_1m_entry, MIN_REQUIRED, MIN_REQUIRED_15M)
from config import (BACKTEST_SYMBOL, BACKTEST_DAYS, STOP_LOSS_PERCENT,
                    MIN_PROFIT_PCT, MIN_HOLD_TIME, TRADE_COOLDOWN,
                    ENTRY_WINDOW, ENTRY_RSI_MAX)

log = logging.getLogger("backtest")


def fetch_binance_klines(symbol, interval="1m", days=30):
    """Fetch historical kline data from Binance public API.
    Paginates in 1000-candle chunks. No API key required."""
    base_url = "https://api.binance.us/api/v3/klines"
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    all_candles = []
    current_start = start_time

    print(f"Fetching {days} days of {interval} candles for {symbol}...")

    while current_start < end_time:
        params = urllib.parse.urlencode({
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_time,
            "limit": 1000,
        })
        url = f"{base_url}?{params}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"Error fetching klines: {e}")
            break

        if not data:
            break

        for kline in data:
            all_candles.append({
                "timestamp": kline[0],
                "open": float(kline[1]),
                "high": float(kline[2]),
                "low": float(kline[3]),
                "close": float(kline[4]),
                "volume": float(kline[5]),
            })

        # Move start to after the last candle we received
        current_start = data[-1][0] + 1

        # Rate limit
        time.sleep(0.2)

        print(f"  Fetched {len(all_candles)} candles so far...")

    print(f"Total: {len(all_candles)} candles fetched")
    return all_candles


class Backtester:
    def __init__(self, candles, config_overrides=None):
        self.candles = candles
        self.overrides = config_overrides or {}

        # Config (with overrides)
        self.stop_loss_pct = self.overrides.get("stop_loss", STOP_LOSS_PERCENT)
        self.min_profit_pct = self.overrides.get("min_profit", MIN_PROFIT_PCT)
        self.entry_window = self.overrides.get("entry_window", ENTRY_WINDOW)
        self.min_hold_time = self.overrides.get("min_hold_time", MIN_HOLD_TIME)
        self.trade_cooldown = self.overrides.get("trade_cooldown", TRADE_COOLDOWN)

        # 1m state
        self.closes = []
        self.highs = []
        self.lows = []
        self.volumes = []
        self.candle_count = 0

        # 15m OHLCV aggregation
        self.closes_15m = []
        self.highs_15m = []
        self.lows_15m = []
        self.volumes_15m = []
        self.buffer_15m = []  # Buffer of 1m candles for current 15m period

        # State machine
        self.direction_15m = "WARMUP"
        self.looking_for_entry = False
        self.entry_window_start = 0  # Timestamp (ms) when entry window opened
        self.consecutive_15m_signal = 0
        self.last_15m_signal = None

        # Position state
        self.position_open = False
        self.buy_price = 0
        self.peak_price = 0
        self.buy_time = 0
        self.last_trade_time = 0
        self.trades = []

    def run(self):
        """Replay candles through the strategy."""
        print(f"\nRunning backtest on {len(self.candles)} candles...")
        print(f"  Stop-loss: {self.stop_loss_pct*100:.1f}% | Min profit: {self.min_profit_pct*100:.2f}% | Entry window: {self.entry_window}s")

        for i, candle in enumerate(self.candles):
            price = candle["close"]
            ts = candle["timestamp"]

            self.closes.append(price)
            self.highs.append(candle["high"])
            self.lows.append(candle["low"])
            self.volumes.append(candle["volume"])

            # 15m OHLCV aggregation
            self.candle_count += 1
            self.buffer_15m.append(candle)
            candle_15m_done = False

            if self.candle_count % 15 == 0:
                buf = self.buffer_15m
                self.closes_15m.append(buf[-1]["close"])
                self.highs_15m.append(max(c["high"] for c in buf))
                self.lows_15m.append(min(c["low"] for c in buf))
                self.volumes_15m.append(sum(c["volume"] for c in buf))
                self.buffer_15m = []
                candle_15m_done = True

            # Trailing stop-loss check on every candle
            if self.position_open and self.buy_price > 0:
                if price > self.peak_price:
                    self.peak_price = price
                profit_pct = (price - self.buy_price) / self.buy_price
                peak_profit_pct = (self.peak_price - self.buy_price) / self.buy_price
                drop_from_peak = (self.peak_price - price) / self.peak_price if self.peak_price > 0 else 0
                drop_from_buy = (self.buy_price - price) / self.buy_price

                # Ratcheting stop-loss system (same as trader.py)
                if peak_profit_pct >= 0.02 and drop_from_peak >= 0.015 and profit_pct > 0:
                    self._sell(price, ts, "TAKE-PROFIT")
                elif peak_profit_pct >= 0.01 and price <= self.buy_price:
                    self._sell(price, ts, "BREAKEVEN-STOP")
                elif drop_from_peak >= 0.025 and self.peak_price > self.buy_price:
                    self._sell(price, ts, "TRAILING-STOP")
                elif drop_from_buy >= self.stop_loss_pct:
                    self._sell(price, ts, "STOP-LOSS")

            # === 15m direction evaluation ===
            if candle_15m_done and len(self.closes_15m) >= MIN_REQUIRED_15M:
                window = min(200, len(self.closes_15m))
                dir_details = get_15m_direction(
                    self.closes_15m[-window:],
                    highs=self.highs_15m[-window:],
                    lows=self.lows_15m[-window:],
                    volumes=self.volumes_15m[-window:],
                )
                dir_sig = dir_details["signal"]

                if dir_sig != "WARMUP":
                    # 15m signal confirmation (2x consecutive)
                    if dir_sig == self.last_15m_signal:
                        self.consecutive_15m_signal += 1
                    else:
                        self.consecutive_15m_signal = 1
                    self.last_15m_signal = dir_sig

                    if dir_sig == "BUY" and self.consecutive_15m_signal >= 2:
                        self.direction_15m = "BUY"
                        if not self.position_open and not self.looking_for_entry:
                            # Cooldown check
                            if self.last_trade_time > 0 and (ts - self.last_trade_time) < self.trade_cooldown * 1000:
                                pass
                            else:
                                self.looking_for_entry = True
                                self.entry_window_start = ts
                    elif dir_sig == "SELL":
                        self.direction_15m = "SELL"
                        if self.looking_for_entry:
                            self.looking_for_entry = False
                            self.entry_window_start = 0
                        if self.position_open:
                            # Min hold time check
                            if self.buy_time > 0 and (ts - self.buy_time) < self.min_hold_time * 1000:
                                pass
                            else:
                                # Min profit check
                                if self.buy_price > 0:
                                    pct = (price - self.buy_price) / self.buy_price
                                    if pct >= self.min_profit_pct:
                                        self._sell(price, ts, "15M-SIGNAL")
                    else:
                        self.direction_15m = dir_sig
                        if dir_sig == "HOLD" and self.consecutive_15m_signal >= 2:
                            if self.looking_for_entry:
                                self.looking_for_entry = False
                                self.entry_window_start = 0

            # === 1m entry check (when looking for entry) ===
            if self.looking_for_entry and not self.position_open:
                entry_window_ms = self.entry_window * 1000
                elapsed_ms = ts - self.entry_window_start
                elapsed_pct = elapsed_ms / entry_window_ms if entry_window_ms > 0 else 1

                if elapsed_ms >= entry_window_ms:
                    self.looking_for_entry = False
                    self.entry_window_start = 0
                elif len(self.closes) >= MIN_REQUIRED:
                    window = min(200, len(self.closes))
                    entry = check_1m_entry(
                        self.closes[-window:],
                        highs=self.highs[-window:],
                        lows=self.lows[-window:],
                        volumes=self.volumes[-window:],
                    )
                    if entry["entry_ok"]:
                        self._buy(price, ts)
                        self.looking_for_entry = False
                        self.entry_window_start = 0
                    elif elapsed_pct >= 0.80 and entry["rsi"] > 0 and entry["rsi"] < ENTRY_RSI_MAX:
                        # Fallback entry
                        self._buy(price, ts)
                        self.looking_for_entry = False
                        self.entry_window_start = 0

    def _buy(self, price, timestamp):
        self.position_open = True
        self.buy_price = price
        self.peak_price = price
        self.buy_time = timestamp
        self.last_trade_time = timestamp

    def _sell(self, price, timestamp, exit_type):
        if not self.position_open:
            return
        profit_pct = (price - self.buy_price) / self.buy_price if self.buy_price > 0 else 0
        hold_ms = timestamp - self.buy_time if self.buy_time > 0 else 0
        self.trades.append({
            "buy_price": round(self.buy_price, 2),
            "sell_price": round(price, 2),
            "profit_pct": round(profit_pct * 100, 4),
            "profit_usd": round(profit_pct * 250, 2),  # Assuming $250 trade size
            "hold_time_sec": round(hold_ms / 1000),
            "exit_type": exit_type,
            "timestamp": datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        })
        self.position_open = False
        self.buy_price = 0
        self.peak_price = 0
        self.last_trade_time = timestamp

    def results(self):
        """Calculate analytics from backtest trades."""
        if not self.trades:
            return {"total_trades": 0}

        trades = self.trades
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

        total_pnl = sum(t["profit_usd"] for t in trades)

        # Max drawdown
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

        best = max(trades, key=lambda t: t["profit_usd"])
        worst = min(trades, key=lambda t: t["profit_usd"])
        avg_hold = sum(t["hold_time_sec"] for t in trades) / total

        # Current streak
        streak = 0
        last_win = trades[-1]["profit_usd"] > 0
        for t in reversed(trades):
            if (t["profit_usd"] > 0) == last_win:
                streak += 1
            else:
                break
        if not last_win:
            streak = -streak

        # Exit type breakdown
        exit_types = {}
        for t in trades:
            et = t["exit_type"]
            exit_types[et] = exit_types.get(et, 0) + 1

        return {
            "total_trades": total,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round(win_rate, 1),
            "avg_win_pct": round(avg_win_pct, 2),
            "avg_loss_pct": round(avg_loss_pct, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "Inf",
            "total_pnl": round(total_pnl, 2),
            "max_drawdown": round(max_dd, 2),
            "best_trade": round(best["profit_usd"], 2),
            "worst_trade": round(worst["profit_usd"], 2),
            "avg_hold_time": round(avg_hold),
            "current_streak": streak,
            "exit_types": exit_types,
        }

    def print_report(self):
        """Print formatted backtest results to console."""
        r = self.results()

        print("\n" + "=" * 60)
        print("  BACKTEST RESULTS")
        print("=" * 60)

        if r["total_trades"] == 0:
            print("  No trades executed during backtest period.")
            print("=" * 60)
            return

        print(f"  Period:          {len(self.candles)} candles ({len(self.candles)/1440:.1f} days)")
        print(f"  Total Trades:    {r['total_trades']}")
        print(f"  Wins / Losses:   {r['wins']}W / {r['losses']}L")
        print(f"  Win Rate:        {r['win_rate']}%")
        print(f"  Profit Factor:   {r['profit_factor']}")
        print("-" * 60)
        print(f"  Total P&L:       ${r['total_pnl']:+.2f}")
        print(f"  Max Drawdown:    ${r['max_drawdown']:.2f}")
        print(f"  Best Trade:      ${r['best_trade']:+.2f}")
        print(f"  Worst Trade:     ${r['worst_trade']:+.2f}")
        print(f"  Avg Win:         +{r['avg_win_pct']:.2f}%")
        print(f"  Avg Loss:        {r['avg_loss_pct']:.2f}%")
        print(f"  Avg Hold Time:   {r['avg_hold_time']}s ({r['avg_hold_time']/60:.1f}m)")
        print(f"  Current Streak:  {abs(r['current_streak'])} {'wins' if r['current_streak'] > 0 else 'losses'}")
        print("-" * 60)
        print("  Exit Type Breakdown:")
        for exit_type, count in sorted(r["exit_types"].items()):
            print(f"    {exit_type:20s} {count:4d} ({count/r['total_trades']*100:.1f}%)")
        print("=" * 60)

        # Print individual trades
        print(f"\n  Trade Log ({r['total_trades']} trades):")
        print(f"  {'#':>3}  {'Time':>16}  {'Buy':>10}  {'Sell':>10}  {'P&L':>8}  {'%':>7}  {'Hold':>6}  {'Exit'}")
        print("  " + "-" * 85)
        for i, t in enumerate(self.trades, 1):
            hold_str = f"{t['hold_time_sec']//60}m" if t['hold_time_sec'] >= 60 else f"{t['hold_time_sec']}s"
            pnl_str = f"${t['profit_usd']:+.2f}"
            print(f"  {i:>3}  {t['timestamp']:>16}  ${t['buy_price']:>9,.2f}  ${t['sell_price']:>9,.2f}  {pnl_str:>8}  {t['profit_pct']:>+6.2f}%  {hold_str:>6}  {t['exit_type']}")


def _run_one_backtest(args):
    """Worker function for multiprocessing — runs a single backtest config.
    Must be a top-level function for pickling."""
    candles, overrides = args
    bt = Backtester(candles, overrides)
    bt.run()
    r = bt.results()
    r["stop_loss"] = overrides["stop_loss"]
    r["min_profit"] = overrides["min_profit"]
    r["entry_window"] = overrides["entry_window"]
    return r


def run_sweep_results(candles, days, progress_cb=None):
    """Run parameter sweep and return sorted list of result dicts.

    Args:
        candles: List of candle dicts from fetch_binance_klines
        days: Number of days (for display only)
        progress_cb: Optional callback(message_str) for progress updates

    Returns:
        List of result dicts sorted by total P&L descending, each containing
        stop_loss, min_profit, entry_window plus all Backtester.results() fields.
    """
    stop_losses = [0.01, 0.02, 0.03, 0.04]
    min_profits = [0.003, 0.005, 0.01, 0.015]
    entry_windows = [600, 900, 1200]

    combos = []
    for sl in stop_losses:
        for mp in min_profits:
            for ew in entry_windows:
                combos.append({"stop_loss": sl, "min_profit": mp, "entry_window": ew})

    total = len(combos)
    if progress_cb:
        progress_cb(f"Running {total} combinations on {len(candles)} candles ({days} days)...")

    # Run in parallel across CPU cores
    n_workers = min(os.cpu_count() or 1, total)
    if progress_cb:
        progress_cb(f"Using {n_workers} worker processes...")

    work_items = [(candles, overrides) for overrides in combos]

    # Use 'spawn' context to avoid fork-related crashes when called from
    # a threaded environment (e.g., the dashboard's HTTP server thread).
    ctx = multiprocessing.get_context('spawn')
    with ctx.Pool(processes=n_workers) as pool:
        all_results = pool.map(_run_one_backtest, work_items)

    if progress_cb:
        progress_cb(f"All {total} combinations complete.")

    # Filter to only configs that had trades
    with_trades = [r for r in all_results if r["total_trades"] > 0]

    if not with_trades:
        return []

    # Sort by total P&L (primary), then profit factor (secondary)
    with_trades.sort(key=lambda r: (
        r["total_pnl"],
        r["profit_factor"] if isinstance(r["profit_factor"], (int, float)) else 999,
    ), reverse=True)

    return with_trades


def run_sweep(candles, days):
    """Run parameter sweep across combinations and rank results (CLI output)."""
    stop_losses = [0.01, 0.02, 0.03, 0.04]
    min_profits = [0.003, 0.005, 0.01, 0.015]
    entry_windows = [600, 900, 1200]
    total = len(stop_losses) * len(min_profits) * len(entry_windows)

    print(f"\n{'='*80}")
    print(f"  PARAMETER SWEEP — {total} combinations x {len(candles)} candles ({days} days)")
    print(f"{'='*80}")
    print(f"  Stop-loss:      {[f'{x*100:.0f}%' for x in stop_losses]}")
    print(f"  Min profit:     {[f'{x*100:.1f}%' for x in min_profits]}")
    print(f"  Entry window:   {[f'{x}s' for x in entry_windows]}")
    print(f"{'='*80}\n")

    def cli_progress(msg):
        print(f"  {msg}", flush=True)

    with_trades = run_sweep_results(candles, days, progress_cb=cli_progress)

    if not with_trades:
        print("\n  No combinations produced any trades.")
        return

    # Print ranked table
    print(f"\n{'='*110}")
    print(f"  RANKED RESULTS (by Total P&L)")
    print(f"{'='*110}")
    print(f"  {'Rank':>4}  {'SL':>4}  {'MinP':>5}  {'EntWin':>6}  {'Trades':>6}  {'W/L':>7}  {'WinRate':>7}  {'PF':>6}  {'Total P&L':>10}  {'MaxDD':>8}  {'AvgWin':>7}  {'AvgLoss':>8}")
    print(f"  {'-'*104}")

    for rank, r in enumerate(with_trades[:20], 1):
        sl_str = f"{r['stop_loss']*100:.0f}%"
        mp_str = f"{r['min_profit']*100:.1f}%"
        ew_str = f"{r['entry_window']}s"
        wl = f"{r['wins']}W/{r['losses']}L"
        pf = r['profit_factor'] if isinstance(r['profit_factor'], (int, float)) else 999
        pf_str = f"{pf:.2f}" if pf < 999 else "Inf"

        marker = " <-- BEST" if rank == 1 else ""
        print(f"  {rank:>4}  {sl_str:>4}  {mp_str:>5}  {ew_str:>6}  {r['total_trades']:>6}  {wl:>7}  {r['win_rate']:>6.1f}%  {pf_str:>6}  ${r['total_pnl']:>+9.2f}  ${r['max_drawdown']:>7.2f}  {r['avg_win_pct']:>+6.2f}%  {r['avg_loss_pct']:>+7.2f}%{marker}")

    print(f"  {'-'*104}")

    # Print recommendation
    best = with_trades[0]
    print(f"\n  RECOMMENDED CONFIG:")
    print(f"    STOP_LOSS_PERCENT = {best['stop_loss']}")
    print(f"    MIN_PROFIT_PCT    = {best['min_profit']}")
    print(f"    ENTRY_WINDOW      = {best['entry_window']}")
    print(f"")
    print(f"    Expected: {best['total_trades']} trades, ${best['total_pnl']:+.2f} P&L, {best['win_rate']}% win rate, {best['profit_factor']} PF")
    print(f"{'='*110}")

    # Also show the best by profit factor (minimum 5 trades)
    pf_sorted = [r for r in with_trades if r["total_trades"] >= 5]
    if pf_sorted:
        pf_sorted.sort(key=lambda r: r["profit_factor"] if isinstance(r["profit_factor"], (int, float)) else 999, reverse=True)
        bp = pf_sorted[0]
        if bp != best:
            print(f"\n  BEST BY PROFIT FACTOR (min 5 trades):")
            print(f"    STOP_LOSS_PERCENT = {bp['stop_loss']}")
            print(f"    MIN_PROFIT_PCT    = {bp['min_profit']}")
            print(f"    ENTRY_WINDOW      = {bp['entry_window']}")
            print(f"    Expected: {bp['total_trades']} trades, ${bp['total_pnl']:+.2f} P&L, {bp['win_rate']}% win rate, {bp['profit_factor']} PF")
            print(f"{'='*110}")

    # Check if a significantly better config exists and alert
    alert = check_better_config(with_trades)
    if alert:
        print(f"\n{'!'*80}")
        print(alert)
        print(f"{'!'*80}\n")
        log.warning(alert)


def check_better_config(sweep_results):
    """Compare sweep results against current config.
    Returns alert message if a significantly better config exists, else None."""
    if not sweep_results:
        return None

    best = sweep_results[0]

    # Run current config to get baseline performance
    current_match = None
    for r in sweep_results:
        if (r["stop_loss"] == STOP_LOSS_PERCENT and
            r["min_profit"] == MIN_PROFIT_PCT and
            r["entry_window"] == ENTRY_WINDOW):
            current_match = r
            break

    if not current_match:
        return None

    # Skip if best IS the current config
    if (best["stop_loss"] == STOP_LOSS_PERCENT and
        best["min_profit"] == MIN_PROFIT_PCT and
        best["entry_window"] == ENTRY_WINDOW):
        return None

    # Only alert if best config has at least 5 trades (statistical relevance)
    if best["total_trades"] < 5:
        return None

    # Alert if best P&L is >25% better than current
    current_pnl = current_match["total_pnl"]
    best_pnl = best["total_pnl"]

    if current_pnl >= 0:
        improvement = (best_pnl - current_pnl) / max(current_pnl, 1) * 100
    else:
        # Current is negative — any positive result is a big improvement
        improvement = 100 if best_pnl > current_pnl else 0

    if improvement < 25:
        return None

    alert = (
        f"BACKTEST ALERT: Better config found!\n"
        f"  Current:  SL={STOP_LOSS_PERCENT*100:.0f}% MP={MIN_PROFIT_PCT*100:.1f}% EW={ENTRY_WINDOW}s "
        f"-> {current_match['total_trades']} trades, ${current_pnl:+.2f} P&L, {current_match['win_rate']}% WR\n"
        f"  Better:   SL={best['stop_loss']*100:.0f}% MP={best['min_profit']*100:.1f}% EW={best['entry_window']}s "
        f"-> {best['total_trades']} trades, ${best_pnl:+.2f} P&L, {best['win_rate']}% WR\n"
        f"  Improvement: {improvement:.0f}% better P&L\n"
        f"  To apply, update config.py:\n"
        f"    STOP_LOSS_PERCENT = {best['stop_loss']}\n"
        f"    MIN_PROFIT_PCT    = {best['min_profit']}\n"
        f"    ENTRY_WINDOW      = {best['entry_window']}"
    )
    return alert


def main():
    parser = argparse.ArgumentParser(description="Backtest BTC trading strategy on historical data")
    parser.add_argument("--days", type=int, default=BACKTEST_DAYS,
                        help=f"Number of days to backtest (default: {BACKTEST_DAYS})")
    parser.add_argument("--symbol", type=str, default=BACKTEST_SYMBOL,
                        help=f"Binance symbol (default: {BACKTEST_SYMBOL})")
    parser.add_argument("--stop-loss", type=float, default=None,
                        help=f"Stop-loss percent (default: {STOP_LOSS_PERCENT})")
    parser.add_argument("--min-profit", type=float, default=None,
                        help=f"Minimum profit percent (default: {MIN_PROFIT_PCT})")
    parser.add_argument("--entry-window", type=int, default=None,
                        help=f"Entry window seconds (default: {ENTRY_WINDOW})")
    parser.add_argument("--sweep", action="store_true",
                        help="Run parameter sweep to find optimal settings")

    args = parser.parse_args()

    # Fetch data
    candles = fetch_binance_klines(args.symbol, "1m", args.days)
    if not candles:
        print("Failed to fetch candle data. Exiting.")
        return

    if args.sweep:
        run_sweep(candles, args.days)
        return

    # Build config overrides
    overrides = {}
    if args.stop_loss is not None:
        overrides["stop_loss"] = args.stop_loss
    if args.min_profit is not None:
        overrides["min_profit"] = args.min_profit
    if args.entry_window is not None:
        overrides["entry_window"] = args.entry_window

    # Run backtest
    bt = Backtester(candles, overrides)
    bt.run()
    bt.print_report()


if __name__ == "__main__":
    main()
