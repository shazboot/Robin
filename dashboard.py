import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

from backtest import fetch_binance_klines, run_sweep_results, check_better_config
from config import BACKTEST_SYMBOL

DASHBOARD_PORT = 4501

# Shared state — updated by trader, read by dashboard
state = {
    "symbol": "",
    "current_price": 0,
    "signal": "STARTING",
    "ema_short": 0,
    "ema_long": 0,
    "rsi": 0,
    "buying_power": 0,
    "btc_held": 0,
    "data_points": 0,
    "required_points": 52,
    "last_update": "",
    "trades": [],       # list of {time, side, price, quantity, amount}
    "price_history": [], # list of {time, price}
    "total_pnl": 0,
    "starting_value": 0,
    "current_value": 0,
    "log_entries": [],  # list of {time, level, message}
    "ema_gap": 0,           # EMA9 - EMA21
    "rsi_gap_buy": 0,       # distance from RSI to buy threshold
    "rsi_gap_sell": 0,      # distance from RSI to sell threshold
    "buy_readiness": "",    # status text for how close to buy
    "sell_readiness": "",   # status text for how close to sell
    "ema_trend": 0,         # EMA-50 value
    "macd_line": 0,
    "macd_signal": 0,
    "macd_hist": 0,
    "bb_upper": 0,
    "bb_middle": 0,
    "bb_lower": 0,
    "trend_status": "--",
    "macd_status": "--",
    "trend_strength": "--",
    "bb_bandwidth": 0,
    "bb_squeeze": "--",
    "atr": 0,
    "atr_pct": 0,
    "volume_confirmed": True,
    "divergence": "NONE",
    "direction_15m": "WARMUP",
    "looking_for_entry": False,
    "entry_window_remaining": 0,
    "entry_type": "",
    "data_points_15m": 0,
    "candles_15m": 0,
    "ema_short_15m": 0,
    "ema_long_15m": 0,
    "ema_trend_15m": 0,
    "rsi_15m": 0,
    "macd_hist_15m": 0,
    "trend_strength_15m": "--",
    "analytics": {},
    "daily_pnl": 0,
    "daily_drawdown_pct": 0,
    "trading_halted": False,
}
state_lock = threading.Lock()

# Backtest sweep state
backtest_state = {"status": "idle", "progress": "", "results": [], "error": ""}
backtest_lock = threading.Lock()


def update_state(**kwargs):
    with state_lock:
        state.update(kwargs)


def add_trade(side, price, quantity, amount, timestamp=None):
    with state_lock:
        state["trades"].append({
            "time": timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "side": side,
            "price": round(price, 2),
            "quantity": round(quantity, 8),
            "amount": round(amount, 2),
        })
        _recalc_pnl()


def add_price_point(price, ema_short=0, ema_long=0, rsi=0,
                     ema_trend=0, macd_line=0, macd_signal=0, macd_hist=0,
                     bb_upper=0, bb_lower=0):
    with state_lock:
        state["price_history"].append({
            "time": int(datetime.now().timestamp()),
            "price": round(price, 2),
            "ema_short": round(ema_short, 2) if ema_short else None,
            "ema_long": round(ema_long, 2) if ema_long else None,
            "ema_trend": round(ema_trend, 2) if ema_trend else None,
            "rsi": round(rsi, 2) if rsi else None,
            "macd_line": round(macd_line, 2) if macd_line else None,
            "macd_signal": round(macd_signal, 2) if macd_signal else None,
            "macd_hist": round(macd_hist, 2) if macd_hist else None,
            "bb_upper": round(bb_upper, 2) if bb_upper else None,
            "bb_lower": round(bb_lower, 2) if bb_lower else None,
        })
        # Keep last 500 points for the chart
        if len(state["price_history"]) > 500:
            state["price_history"] = state["price_history"][-500:]


def add_log(level, message):
    with state_lock:
        state["log_entries"].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        })
        # Keep last 100 entries
        if len(state["log_entries"]) > 100:
            state["log_entries"] = state["log_entries"][-100:]


def get_pnl() -> float:
    with state_lock:
        return state["total_pnl"]


def _recalc_pnl():
    """Calculate realized P&L from completed buy/sell pairs."""
    total_cost = 0
    total_revenue = 0
    for trade in state["trades"]:
        if trade["side"] == "buy":
            total_cost += trade["amount"]
        else:
            total_revenue += trade["amount"]
    state["total_pnl"] = round(total_revenue - total_cost, 2)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BTC Trading Bot</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0d1117; color: #c9d1d9; font-family: 'Segoe UI', system-ui, sans-serif; }
  .header { background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  .header h1 { font-size: 20px; color: #58a6ff; }
  .header .status { font-size: 13px; color: #8b949e; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; padding: 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }
  .card .label { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .card .value { font-size: 28px; font-weight: 600; }
  .signal-BUY { color: #3fb950; }
  .signal-SELL { color: #f85149; }
  .signal-HOLD { color: #d29922; }
  .signal-WARMUP, .signal-STARTING { color: #8b949e; }
  .pnl-pos { color: #3fb950; }
  .pnl-neg { color: #f85149; }
  .pnl-zero { color: #8b949e; }
  .wide { grid-column: 1 / -1; }
  #priceChart { width: 100%; height: 400px; }
  #rsiChart { width: 100%; height: 120px; }
  #macdChart { width: 100%; height: 120px; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  th { text-align: left; font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; padding: 8px 12px; border-bottom: 1px solid #30363d; }
  td { padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 14px; }
  tr:hover { background: #1c2128; }
  .side-buy { color: #3fb950; font-weight: 600; }
  .side-sell { color: #f85149; font-weight: 600; }
  .warmup-bar { background: #21262d; border-radius: 4px; height: 8px; margin-top: 8px; overflow: hidden; }
  .warmup-fill { background: #58a6ff; height: 100%; border-radius: 4px; transition: width 0.5s; }
  .indicators { display: flex; flex-wrap: wrap; gap: 24px; margin-top: 8px; }
  .ind { text-align: center; }
  .ind .ind-label { font-size: 11px; color: #8b949e; }
  .ind .ind-val { font-size: 18px; font-weight: 600; margin-top: 2px; }
  .ind .ind-gap { font-size: 11px; margin-top: 2px; color: #8b949e; }
  .gap-pos { color: #3fb950; }
  .gap-neg { color: #f85149; }
  .gap-near { color: #d29922; }
  .status-panel { margin-top: 16px; display: flex; gap: 16px; }
  .status-box { flex: 1; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 12px 16px; }
  .status-box .status-title { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
  .status-box .status-text { font-size: 14px; font-weight: 600; }
  .ready-close { color: #d29922; }
  .ready-far { color: #8b949e; }
  .ready-go { color: #3fb950; }
  .no-trades { color: #484f58; text-align: center; padding: 32px; font-style: italic; }
  .log-panel { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 12px; max-height: 300px; overflow-y: auto; font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace; font-size: 12px; line-height: 1.6; margin-top: 8px; }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .log-time { color: #484f58; }
  .log-INFO { color: #8b949e; }
  .log-WARNING { color: #d29922; }
  .log-ERROR { color: #f85149; }
  .log-BUY { color: #3fb950; font-weight: 600; }
  .log-SELL { color: #f85149; font-weight: 600; }
  .log-separator { color: #30363d; }
  @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
  #btTable th { white-space: nowrap; }
  #btTable td { font-size: 13px; }
  .bt-best { background: rgba(35,134,54,0.15); }
  .bt-best td:first-child::after { content: " RECOMMENDED"; font-size: 10px; color: #3fb950; font-weight: 600; }
  .chart-legend { display: flex; gap: 16px; margin-top: 8px; font-size: 12px; }
  .legend-item { display: flex; align-items: center; gap: 4px; }
  .legend-color { width: 12px; height: 3px; border-radius: 2px; }
</style>
</head>
<body>
<div class="header">
  <h1>BTC Trading Bot</h1>
  <div class="status">Last update: <span id="lastUpdate">--</span> &nbsp; | &nbsp; Auto-refresh: 1s</div>
</div>

<div class="grid">
  <div class="card">
    <div class="label">Current Price</div>
    <div class="value" id="price">--</div>
  </div>
  <div class="card">
    <div class="label">Signal</div>
    <div class="value" id="signal">--</div>
  </div>
  <div class="card">
    <div class="label">Buying Power</div>
    <div class="value" id="buyingPower">--</div>
  </div>
  <div class="card">
    <div class="label">BTC Held</div>
    <div class="value" id="btcHeld">--</div>
  </div>
  <div class="card">
    <div class="label">Portfolio Value</div>
    <div class="value" id="portfolioValue">--</div>
  </div>
  <div class="card">
    <div class="label">Realized P&L</div>
    <div class="value" id="pnl">--</div>
  </div>

  <div class="card wide">
    <div class="label">Indicators</div>
    <div class="indicators">
      <div class="ind">
        <div class="ind-label">EMA-9</div>
        <div class="ind-val" id="ema9">--</div>
        <div class="ind-gap" id="emaGap">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">EMA-21</div>
        <div class="ind-val" id="ema21">--</div>
        <div class="ind-gap" id="emaDirection">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">EMA-50 Trend</div>
        <div class="ind-val" id="ema50">--</div>
        <div class="ind-gap" id="trendStatus">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">RSI-14</div>
        <div class="ind-val" id="rsi">--</div>
        <div class="ind-gap" id="rsiGap">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">MACD</div>
        <div class="ind-val" id="macdVal">--</div>
        <div class="ind-gap" id="macdStatus">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">Bollinger</div>
        <div class="ind-val" id="bbVal">--</div>
        <div class="ind-gap" id="bbStatus">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">ATR</div>
        <div class="ind-val" id="atrVal">--</div>
        <div class="ind-gap" id="atrStatus">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">Volume</div>
        <div class="ind-val" id="volVal">--</div>
        <div class="ind-gap" id="volStatus">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">15m Direction</div>
        <div class="ind-val" id="dir15m">--</div>
        <div class="ind-gap" id="dir15mStatus">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">Entry Status</div>
        <div class="ind-val" id="entryVal">--</div>
        <div class="ind-gap" id="entryStatus">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">Divergence</div>
        <div class="ind-val" id="divVal">--</div>
        <div class="ind-gap" id="divStatus">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">Data Points</div>
        <div class="ind-val" id="dataPoints">--</div>
        <div class="ind-gap" id="dataStatus">--</div>
      </div>
    </div>
    <div class="warmup-bar"><div class="warmup-fill" id="warmupBar"></div></div>
    <div class="status-panel">
      <div class="status-box">
        <div class="status-title">Buy Readiness</div>
        <div class="status-text" id="buyReadiness">--</div>
      </div>
      <div class="status-box">
        <div class="status-title">Sell Readiness</div>
        <div class="status-text" id="sellReadiness">--</div>
      </div>
    </div>
  </div>

  <div class="card wide" id="perfCard" style="display:none;">
    <div class="label">Performance</div>
    <div class="indicators">
      <div class="ind">
        <div class="ind-label">Win Rate</div>
        <div class="ind-val" id="perfWinRate">--</div>
        <div class="ind-gap" id="perfWL">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">Profit Factor</div>
        <div class="ind-val" id="perfPF">--</div>
        <div class="ind-gap">&nbsp;</div>
      </div>
      <div class="ind">
        <div class="ind-label">Avg Win</div>
        <div class="ind-val gap-pos" id="perfAvgWin">--</div>
        <div class="ind-gap">Avg Loss: <span id="perfAvgLoss">--</span></div>
      </div>
      <div class="ind">
        <div class="ind-label">Max Drawdown</div>
        <div class="ind-val" id="perfMaxDD">--</div>
        <div class="ind-gap">&nbsp;</div>
      </div>
      <div class="ind">
        <div class="ind-label">Best Trade</div>
        <div class="ind-val gap-pos" id="perfBest">--</div>
        <div class="ind-gap">Worst: <span id="perfWorst">--</span></div>
      </div>
      <div class="ind">
        <div class="ind-label">Streak</div>
        <div class="ind-val" id="perfStreak">--</div>
        <div class="ind-gap">&nbsp;</div>
      </div>
      <div class="ind">
        <div class="ind-label">Daily P&L</div>
        <div class="ind-val" id="perfDailyPnl">--</div>
        <div class="ind-gap" id="perfDailyPct">--</div>
      </div>
      <div class="ind">
        <div class="ind-label">Status</div>
        <div class="ind-val" id="perfStatus">--</div>
        <div class="ind-gap">&nbsp;</div>
      </div>
    </div>
  </div>

  <div class="card wide" id="backtestCard">
    <div class="label">Backtest Sweep</div>
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
      <label style="font-size:13px;color:#8b949e;">Days:</label>
      <input type="number" id="btDays" value="30" min="1" max="365"
             style="width:70px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#c9d1d9;padding:6px 8px;font-size:14px;">
      <button id="btRunBtn" onclick="startBacktest()"
              style="background:#238636;color:#fff;border:none;border-radius:6px;padding:8px 18px;font-size:14px;font-weight:600;cursor:pointer;">
        Run Sweep
      </button>
      <span id="btStatus" style="font-size:13px;color:#8b949e;"></span>
    </div>
    <div id="btProgress" style="font-size:13px;color:#8b949e;margin-bottom:8px;display:none;">
      <span style="display:inline-block;animation:spin 1s linear infinite;margin-right:6px;">&#9881;</span>
      <span id="btProgressText"></span>
    </div>
    <div id="btError" style="color:#f85149;font-size:13px;margin-bottom:8px;display:none;"></div>
    <div id="btResults" style="display:none;overflow-x:auto;">
      <table id="btTable">
        <thead>
          <tr>
            <th>Rank</th><th>SL%</th><th>MinProfit%</th><th>EntryWin</th>
            <th>Trades</th><th>W/L</th><th>WinRate</th><th>PF</th>
            <th>Total P&amp;L</th><th>MaxDD</th>
          </tr>
        </thead>
        <tbody id="btBody"></tbody>
      </table>
    </div>
  </div>

  <div class="card wide" style="padding-bottom:8px;">
    <div class="label">Chart</div>
    <div class="chart-legend">
      <div class="legend-item"><div class="legend-color" style="background:#2962FF"></div> Price</div>
      <div class="legend-item"><div class="legend-color" style="background:#FF6D00"></div> EMA-9</div>
      <div class="legend-item"><div class="legend-color" style="background:#AA00FF"></div> EMA-21</div>
      <div class="legend-item"><div class="legend-color" style="background:#26A69A"></div> EMA-50</div>
      <div class="legend-item"><div class="legend-color" style="background:rgba(33,150,243,0.3)"></div> Bollinger</div>
      <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div> Buy</div>
      <div class="legend-item"><div class="legend-color" style="background:#f85149"></div> Sell</div>
      <div class="legend-item"><div class="legend-color" style="background:#E040FB"></div> RSI</div>
      <div class="legend-item"><div class="legend-color" style="background:#2962FF;height:2px"></div> MACD</div>
    </div>
    <div id="priceChart"></div>
    <div style="color:#8b949e;font-size:11px;padding:4px 0 2px 8px;">RSI (14)</div>
    <div id="rsiChart"></div>
    <div style="color:#8b949e;font-size:11px;padding:4px 0 2px 8px;">MACD (12, 26, 9)</div>
    <div id="macdChart"></div>
  </div>

  <div class="card wide">
    <div class="label">Trade History</div>
    <div id="tradesContainer"></div>
  </div>

  <div class="card wide">
    <div class="label">Bot Activity Log</div>
    <div class="log-panel" id="logPanel"></div>
  </div>
</div>

<script>
function fmt(n) { return n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }

// ---- TradingView Lightweight Charts Setup ----
const chartOptions = {
  layout: { background: { color: '#161b22' }, textColor: '#8b949e' },
  grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: '#30363d' },
  timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false },
};

// Price chart with EMAs + Bollinger Bands
const priceChartOpts = JSON.parse(JSON.stringify(chartOptions));
priceChartOpts.timeScale.visible = false;
const priceChart = LightweightCharts.createChart(document.getElementById('priceChart'), priceChartOpts);
const priceSeries = priceChart.addLineSeries({ color: '#2962FF', lineWidth: 2, priceFormat: { type: 'price', precision: 2, minMove: 0.01 } });
const ema9Series = priceChart.addLineSeries({ color: '#FF6D00', lineWidth: 1 });
const ema21Series = priceChart.addLineSeries({ color: '#AA00FF', lineWidth: 1 });
const ema50Series = priceChart.addLineSeries({ color: '#26A69A', lineWidth: 2, lineStyle: 2 });
const bbUpperSeries = priceChart.addLineSeries({ color: 'rgba(33,150,243,0.3)', lineWidth: 1, lineStyle: 2 });
const bbLowerSeries = priceChart.addLineSeries({ color: 'rgba(33,150,243,0.3)', lineWidth: 1, lineStyle: 2 });

// RSI chart
const rsiChartOpts = JSON.parse(JSON.stringify(chartOptions));
rsiChartOpts.timeScale.visible = false;
const rsiChart = LightweightCharts.createChart(document.getElementById('rsiChart'), rsiChartOpts);
const rsiSeries = rsiChart.addLineSeries({ color: '#E040FB', lineWidth: 2, priceFormat: { type: 'price', precision: 1, minMove: 0.1 } });
const rsiOverbought = rsiChart.addLineSeries({ color: 'rgba(248,81,73,0.4)', lineWidth: 1, lineStyle: 2 });
const rsiOversold = rsiChart.addLineSeries({ color: 'rgba(63,185,80,0.4)', lineWidth: 1, lineStyle: 2 });
const rsiMidLine = rsiChart.addLineSeries({ color: 'rgba(139,148,158,0.3)', lineWidth: 1, lineStyle: 2 });

// MACD chart
const macdChartOpts = JSON.parse(JSON.stringify(chartOptions));
const macdChart = LightweightCharts.createChart(document.getElementById('macdChart'), macdChartOpts);
const macdLineSeries = macdChart.addLineSeries({ color: '#2962FF', lineWidth: 2, priceFormat: { type: 'price', precision: 2, minMove: 0.01 } });
const macdSignalSeries = macdChart.addLineSeries({ color: '#FF6D00', lineWidth: 1 });
const macdHistSeries = macdChart.addHistogramSeries({ priceFormat: { type: 'price', precision: 2, minMove: 0.01 } });
const macdZeroLine = macdChart.addLineSeries({ color: 'rgba(139,148,158,0.3)', lineWidth: 1, lineStyle: 2 });

// Sync crosshairs across all 3 charts
const charts = [priceChart, rsiChart, macdChart];
charts.forEach((c, i) => {
  c.timeScale().subscribeVisibleLogicalRangeChange(range => {
    if (!range) return;
    charts.forEach((other, j) => { if (i !== j) other.timeScale().setVisibleLogicalRange(range); });
  });
});

let lastDataLength = 0;

function updateCharts(priceHistory, trades) {
  if (!priceHistory || priceHistory.length < 2) return;

  const priceData = [], ema9Data = [], ema21Data = [], ema50Data = [];
  const bbUpData = [], bbLowData = [];
  const rsiData = [], rsiObData = [], rsiOsData = [], rsiMdData = [];
  const macdLineData = [], macdSigData = [], macdHistData = [], macdZeroData = [];

  for (const p of priceHistory) {
    const t = p.time;
    priceData.push({ time: t, value: p.price });
    if (p.ema_short) ema9Data.push({ time: t, value: p.ema_short });
    if (p.ema_long) ema21Data.push({ time: t, value: p.ema_long });
    if (p.ema_trend) ema50Data.push({ time: t, value: p.ema_trend });
    if (p.bb_upper) bbUpData.push({ time: t, value: p.bb_upper });
    if (p.bb_lower) bbLowData.push({ time: t, value: p.bb_lower });
    if (p.rsi != null) {
      rsiData.push({ time: t, value: p.rsi });
      rsiObData.push({ time: t, value: 60 });
      rsiOsData.push({ time: t, value: 40 });
      rsiMdData.push({ time: t, value: 50 });
    }
    if (p.macd_line != null) {
      macdLineData.push({ time: t, value: p.macd_line });
      macdSigData.push({ time: t, value: p.macd_signal });
      macdHistData.push({ time: t, value: p.macd_hist, color: p.macd_hist >= 0 ? 'rgba(38,166,154,0.6)' : 'rgba(239,83,80,0.6)' });
      macdZeroData.push({ time: t, value: 0 });
    }
  }

  priceSeries.setData(priceData);
  ema9Series.setData(ema9Data);
  ema21Series.setData(ema21Data);
  ema50Series.setData(ema50Data);
  bbUpperSeries.setData(bbUpData);
  bbLowerSeries.setData(bbLowData);
  rsiSeries.setData(rsiData);
  rsiOverbought.setData(rsiObData);
  rsiOversold.setData(rsiOsData);
  rsiMidLine.setData(rsiMdData);
  macdLineSeries.setData(macdLineData);
  macdSignalSeries.setData(macdSigData);
  macdHistSeries.setData(macdHistData);
  macdZeroLine.setData(macdZeroData);

  // Trade markers
  const markers = [];
  for (const t of trades) {
    let ts;
    if (t.time.includes('-')) {
      ts = Math.floor(new Date(t.time.replace(' ', 'T')).getTime() / 1000);
    } else continue;
    let closest = priceHistory[0].time, minDiff = Math.abs(priceHistory[0].time - ts);
    for (const p of priceHistory) {
      const diff = Math.abs(p.time - ts);
      if (diff < minDiff) { minDiff = diff; closest = p.time; }
    }
    if (minDiff < 300) {
      markers.push({
        time: closest,
        position: t.side === 'buy' ? 'belowBar' : 'aboveBar',
        color: t.side === 'buy' ? '#3fb950' : '#f85149',
        shape: t.side === 'buy' ? 'arrowUp' : 'arrowDown',
        text: t.side.toUpperCase() + ' $' + fmt(t.price),
      });
    }
  }
  markers.sort((a, b) => a.time - b.time);
  priceSeries.setMarkers(markers);

  if (priceHistory.length > lastDataLength) {
    charts.forEach(c => c.timeScale().scrollToRealTime());
    lastDataLength = priceHistory.length;
  }
}

// Handle resize
const resizeObserver = new ResizeObserver(() => {
  const priceEl = document.getElementById('priceChart');
  const rsiEl = document.getElementById('rsiChart');
  const macdEl = document.getElementById('macdChart');
  priceChart.applyOptions({ width: priceEl.clientWidth, height: 400 });
  rsiChart.applyOptions({ width: rsiEl.clientWidth, height: 120 });
  macdChart.applyOptions({ width: macdEl.clientWidth, height: 120 });
});
resizeObserver.observe(document.getElementById('priceChart'));

function renderTrades(trades) {
  const c = document.getElementById('tradesContainer');
  if (!trades.length) { c.innerHTML = '<div class="no-trades">No trades yet</div>'; return; }
  let html = '<table><tr><th>Time</th><th>Side</th><th>Price</th><th>Quantity</th><th>Amount</th></tr>';
  for (let i = trades.length - 1; i >= 0; i--) {
    const t = trades[i];
    html += '<tr><td>' + t.time + '</td><td class="side-' + t.side + '">' + t.side.toUpperCase() +
      '</td><td>$' + fmt(t.price) + '</td><td>' + t.quantity.toFixed(8) +
      '</td><td>$' + fmt(t.amount) + '</td></tr>';
  }
  c.innerHTML = html + '</table>';
}

function renderLog(entries) {
  const panel = document.getElementById('logPanel');
  if (!entries.length) { panel.innerHTML = '<span class="log-INFO">Waiting for bot activity...</span>'; return; }
  let html = '';
  for (const e of entries) {
    let cls = 'log-' + e.level;
    if (e.message.includes('BUY SIGNAL') || e.message.includes('BUYING') || e.message.includes('BUY CONFIRMED')) cls = 'log-BUY';
    if (e.message.includes('SELL SIGNAL') || e.message.includes('SELLING') || e.message.includes('SELL CONFIRMED')) cls = 'log-SELL';
    if (e.message.includes('====')) { html += '<div class="log-line log-separator">' + e.message + '</div>'; continue; }
    html += '<div class="log-line"><span class="log-time">' + e.time + '</span> <span class="' + cls + '">[' + e.level + ']</span> <span class="' + cls + '">' + e.message + '</span></div>';
  }
  panel.innerHTML = html;
  panel.scrollTop = panel.scrollHeight;
}

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    document.getElementById('price').textContent = '$' + fmt(d.current_price);
    const sigEl = document.getElementById('signal');
    sigEl.textContent = d.signal;
    sigEl.className = 'value signal-' + d.signal;
    document.getElementById('buyingPower').textContent = '$' + fmt(d.buying_power);
    document.getElementById('btcHeld').textContent = d.btc_held.toFixed(8);
    const pv = d.buying_power + d.btc_held * d.current_price;
    document.getElementById('portfolioValue').textContent = '$' + fmt(pv);
    const pnlEl = document.getElementById('pnl');
    pnlEl.textContent = (d.total_pnl >= 0 ? '+$' : '-$') + fmt(Math.abs(d.total_pnl));
    pnlEl.className = 'value ' + (d.total_pnl > 0 ? 'pnl-pos' : d.total_pnl < 0 ? 'pnl-neg' : 'pnl-zero');
    document.getElementById('ema9').textContent = d.ema_short ? '$' + fmt(d.ema_short) : '--';
    document.getElementById('ema21').textContent = d.ema_long ? '$' + fmt(d.ema_long) : '--';
    document.getElementById('ema50').textContent = d.ema_trend ? '$' + fmt(d.ema_trend) : '--';
    document.getElementById('rsi').textContent = d.rsi ? d.rsi.toFixed(1) : '--';
    document.getElementById('macdVal').textContent = d.macd_hist ? d.macd_hist.toFixed(2) : '--';
    document.getElementById('bbVal').textContent = d.bb_upper ? '$' + fmt(d.bb_upper) : '--';
    document.getElementById('dataPoints').textContent = d.data_points + '/' + d.required_points;
    document.getElementById('warmupBar').style.width = Math.min(100, d.data_points / d.required_points * 100) + '%';
    document.getElementById('lastUpdate').textContent = d.last_update;

    // EMA gap
    if (d.ema_short && d.ema_long) {
      const gap = d.ema_gap;
      const gapEl = document.getElementById('emaGap');
      gapEl.textContent = 'Gap: ' + (gap >= 0 ? '+' : '') + '$' + fmt(Math.abs(gap));
      gapEl.className = 'ind-gap ' + (Math.abs(gap) < 50 ? 'gap-near' : gap > 0 ? 'gap-pos' : 'gap-neg');
      const dirEl = document.getElementById('emaDirection');
      dirEl.textContent = gap > 0 ? 'EMA9 above' : gap < 0 ? 'EMA9 below' : 'Crossed';
      dirEl.className = 'ind-gap ' + (Math.abs(gap) < 50 ? 'gap-near' : gap > 0 ? 'gap-pos' : 'gap-neg');
    }

    // RSI gap
    if (d.rsi) {
      const rsiGapEl = document.getElementById('rsiGap');
      const buyDist = d.rsi_gap_buy;
      const sellDist = d.rsi_gap_sell;
      if (d.rsi < 50) {
        rsiGapEl.textContent = fmt(Math.abs(buyDist)) + ' from buy zone';
        rsiGapEl.className = 'ind-gap ' + (Math.abs(buyDist) < 5 ? 'gap-near' : buyDist <= 0 ? 'gap-pos' : 'gap-neg');
      } else {
        rsiGapEl.textContent = fmt(Math.abs(sellDist)) + ' from sell zone';
        rsiGapEl.className = 'ind-gap ' + (Math.abs(sellDist) < 5 ? 'gap-near' : sellDist <= 0 ? 'gap-pos' : 'gap-neg');
      }
    }

    // EMA-50 Trend status (slope-based)
    if (d.ema_trend) {
      const trendEl = document.getElementById('trendStatus');
      const ts = d.trend_strength || '--';
      trendEl.textContent = ts === 'UP' ? 'Strengthening' : ts === 'DOWN' ? 'Weakening' : 'Flat';
      trendEl.className = 'ind-gap ' + (ts === 'UP' ? 'gap-pos' : ts === 'DOWN' ? 'gap-neg' : '');
    }

    // MACD status
    if (d.macd_hist) {
      const macdStEl = document.getElementById('macdStatus');
      const ms = d.macd_status || '--';
      macdStEl.textContent = ms;
      macdStEl.className = 'ind-gap ' + (ms === 'Bullish' ? 'gap-pos' : ms === 'Bearish' ? 'gap-neg' : '');
    }

    // Bollinger status (squeeze detection)
    if (d.bb_squeeze) {
      const bbStEl = document.getElementById('bbStatus');
      const sq = d.bb_squeeze || '--';
      if (sq === 'EXPANDING') { bbStEl.textContent = 'Expanding (BW: ' + (d.bb_bandwidth || 0).toFixed(2) + '%)'; bbStEl.className = 'ind-gap gap-pos'; }
      else if (sq === 'CONTRACTING') { bbStEl.textContent = 'Squeezing (BW: ' + (d.bb_bandwidth || 0).toFixed(2) + '%)'; bbStEl.className = 'ind-gap gap-neg'; }
      else { bbStEl.textContent = 'Neutral (BW: ' + (d.bb_bandwidth || 0).toFixed(2) + '%)'; bbStEl.className = 'ind-gap'; }
    }

    // ATR status
    if (d.atr) {
      document.getElementById('atrVal').textContent = '$' + fmt(d.atr);
      const atrStEl = document.getElementById('atrStatus');
      atrStEl.textContent = d.atr_pct.toFixed(3) + '% volatility';
      atrStEl.className = 'ind-gap ' + (d.atr_pct > 2 ? 'gap-neg' : d.atr_pct > 1 ? 'gap-near' : 'gap-pos');
    }

    // Volume status
    const volEl = document.getElementById('volVal');
    const volStEl = document.getElementById('volStatus');
    volEl.textContent = d.volume_confirmed ? 'Confirmed' : 'Low';
    volEl.className = 'ind-val ' + (d.volume_confirmed ? 'gap-pos' : 'gap-neg');
    volStEl.textContent = d.volume_confirmed ? 'Above average' : 'Below average';
    volStEl.className = 'ind-gap ' + (d.volume_confirmed ? 'gap-pos' : 'gap-neg');

    // Divergence status
    const divEl = document.getElementById('divVal');
    const divStEl = document.getElementById('divStatus');
    divEl.textContent = d.divergence || 'NONE';
    if (d.divergence === 'BULLISH') {
      divEl.className = 'ind-val gap-pos';
      divStEl.textContent = 'Hidden strength';
      divStEl.className = 'ind-gap gap-pos';
    } else if (d.divergence === 'BEARISH') {
      divEl.className = 'ind-val gap-neg';
      divStEl.textContent = 'Hidden weakness';
      divStEl.className = 'ind-gap gap-neg';
    } else {
      divEl.className = 'ind-val';
      divStEl.textContent = 'No divergence';
      divStEl.className = 'ind-gap';
    }

    // Data points status
    const dsEl = document.getElementById('dataStatus');
    if (d.data_points < d.required_points) {
      const remaining = d.required_points - d.data_points;
      dsEl.textContent = remaining + ' more needed';
      dsEl.className = 'ind-gap gap-near';
    } else {
      dsEl.textContent = 'Active';
      dsEl.className = 'ind-gap gap-pos';
    }

    // Buy/Sell readiness
    const buyEl = document.getElementById('buyReadiness');
    buyEl.textContent = d.buy_readiness || '--';
    buyEl.className = 'status-text ' + (d.buy_readiness.includes('READY') ? 'ready-go' : d.buy_readiness.includes('Close') ? 'ready-close' : 'ready-far');
    const sellEl = document.getElementById('sellReadiness');
    sellEl.textContent = d.sell_readiness || '--';
    sellEl.className = 'status-text ' + (d.sell_readiness.includes('READY') ? 'ready-go' : d.sell_readiness.includes('Close') ? 'ready-close' : 'ready-far');

    // 15m Direction
    const dir15 = d.direction_15m || 'WARMUP';
    const dir15El = document.getElementById('dir15m');
    dir15El.textContent = dir15;
    dir15El.className = 'ind-val ' + (dir15 === 'BUY' ? 'gap-pos' : dir15 === 'SELL' ? 'gap-neg' : dir15 === 'WARMUP' ? '' : 'gap-near');
    const dir15s = document.getElementById('dir15mStatus');
    if (dir15 === 'WARMUP') {
      dir15s.textContent = (d.candles_15m || 0) + ' candles';
      dir15s.className = 'ind-gap gap-near';
    } else {
      dir15s.textContent = 'RSI: ' + (d.rsi_15m || 0).toFixed(1) + ' | ' + (d.trend_strength_15m || '--');
      dir15s.className = 'ind-gap ' + (dir15 === 'BUY' ? 'gap-pos' : dir15 === 'SELL' ? 'gap-neg' : '');
    }

    // Entry Status
    const entryEl = document.getElementById('entryVal');
    const entrySt = document.getElementById('entryStatus');
    if (d.looking_for_entry) {
      entryEl.textContent = 'SEARCHING';
      entryEl.className = 'ind-val gap-near';
      entrySt.textContent = (d.entry_window_remaining || 0) + 's remaining';
      entrySt.className = 'ind-gap gap-near';
    } else if (d.entry_type) {
      entryEl.textContent = d.entry_type;
      entryEl.className = 'ind-val gap-pos';
      entrySt.textContent = 'Last entry type';
      entrySt.className = 'ind-gap';
    } else {
      entryEl.textContent = 'Idle';
      entryEl.className = 'ind-val';
      entrySt.textContent = 'Waiting for 15m BUY';
      entrySt.className = 'ind-gap';
    }

    // Performance analytics
    const a = d.analytics || {};
    const perfCard = document.getElementById('perfCard');
    if (a.total_trades) {
      perfCard.style.display = '';
      document.getElementById('perfWinRate').textContent = a.win_rate + '%';
      document.getElementById('perfWinRate').className = 'ind-val ' + (a.win_rate >= 50 ? 'gap-pos' : 'gap-neg');
      document.getElementById('perfWL').textContent = a.wins + 'W / ' + a.losses + 'L (' + a.total_trades + ' total)';
      document.getElementById('perfPF').textContent = a.profit_factor;
      document.getElementById('perfPF').className = 'ind-val ' + (parseFloat(a.profit_factor) > 1 ? 'gap-pos' : 'gap-neg');
      document.getElementById('perfAvgWin').textContent = '+' + a.avg_win_pct + '%';
      document.getElementById('perfAvgLoss').textContent = a.avg_loss_pct + '%';
      document.getElementById('perfMaxDD').textContent = a.max_drawdown_pct + '%';
      document.getElementById('perfMaxDD').className = 'ind-val gap-neg';
      document.getElementById('perfBest').textContent = '$' + fmt(a.best_trade);
      document.getElementById('perfWorst').textContent = '$' + fmt(a.worst_trade);
      const streak = a.current_streak;
      document.getElementById('perfStreak').textContent = Math.abs(streak) + (streak > 0 ? ' wins' : ' losses');
      document.getElementById('perfStreak').className = 'ind-val ' + (streak > 0 ? 'gap-pos' : 'gap-neg');
    }

    // Daily P&L
    const dpnl = d.daily_pnl || 0;
    const dpnlEl = document.getElementById('perfDailyPnl');
    dpnlEl.textContent = (dpnl >= 0 ? '+$' : '-$') + fmt(Math.abs(dpnl));
    dpnlEl.className = 'ind-val ' + (dpnl >= 0 ? 'gap-pos' : 'gap-neg');
    document.getElementById('perfDailyPct').textContent = (d.daily_drawdown_pct || 0).toFixed(2) + '%';
    const statusEl = document.getElementById('perfStatus');
    statusEl.textContent = d.trading_halted ? 'HALTED' : 'ACTIVE';
    statusEl.className = 'ind-val ' + (d.trading_halted ? 'gap-neg' : 'gap-pos');
    if (a.total_trades || d.daily_pnl) perfCard.style.display = '';

    updateCharts(d.price_history, d.trades);
    renderTrades(d.trades);
    renderLog(d.log_entries || []);
  } catch(e) { console.error('Refresh failed:', e); }
}

refresh();
setInterval(refresh, 1000);

// ---- Backtest Sweep ----
let btPolling = null;

async function startBacktest() {
  const days = parseInt(document.getElementById('btDays').value) || 30;
  const btn = document.getElementById('btRunBtn');
  btn.disabled = true;
  btn.textContent = 'Running...';
  btn.style.opacity = '0.6';
  document.getElementById('btProgress').style.display = '';
  document.getElementById('btError').style.display = 'none';
  document.getElementById('btResults').style.display = 'none';
  document.getElementById('btStatus').textContent = '';

  try {
    const r = await fetch('/api/backtest', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({days: days})
    });
    const d = await r.json();
    if (d.error) {
      document.getElementById('btError').textContent = d.error;
      document.getElementById('btError').style.display = '';
      resetBtBtn();
      return;
    }
  } catch(e) {
    document.getElementById('btError').textContent = 'Request failed: ' + e;
    document.getElementById('btError').style.display = '';
    resetBtBtn();
    return;
  }

  if (btPolling) clearInterval(btPolling);
  btPolling = setInterval(pollBacktest, 2000);
}

async function pollBacktest() {
  try {
    const r = await fetch('/api/backtest');
    const d = await r.json();
    document.getElementById('btProgressText').textContent = d.progress || '';

    if (d.status === 'done') {
      clearInterval(btPolling);
      btPolling = null;
      document.getElementById('btProgress').style.display = 'none';
      renderBtResults(d.results);
      resetBtBtn();
      document.getElementById('btStatus').textContent = d.progress;
    } else if (d.status === 'error') {
      clearInterval(btPolling);
      btPolling = null;
      document.getElementById('btProgress').style.display = 'none';
      document.getElementById('btError').textContent = d.error;
      document.getElementById('btError').style.display = '';
      resetBtBtn();
    }
  } catch(e) { console.error('Backtest poll failed:', e); }
}

function resetBtBtn() {
  const btn = document.getElementById('btRunBtn');
  btn.disabled = false;
  btn.textContent = 'Run Sweep';
  btn.style.opacity = '1';
}

function renderBtResults(results) {
  if (!results || !results.length) {
    document.getElementById('btResults').style.display = '';
    document.getElementById('btBody').innerHTML = '<tr><td colspan="10" style="text-align:center;color:#484f58;">No combinations produced trades</td></tr>';
    return;
  }
  let html = '';
  const top = Math.min(results.length, 20);
  for (let i = 0; i < top; i++) {
    const r = results[i];
    const sl = (r.stop_loss * 100).toFixed(0) + '%';
    const mp = (r.min_profit * 100).toFixed(1) + '%';
    const sc = r.entry_window + 's';
    const wl = r.wins + 'W/' + r.losses + 'L';
    const wr = r.win_rate.toFixed(1) + '%';
    const pf = (typeof r.profit_factor === 'number' && r.profit_factor < 999) ? r.profit_factor.toFixed(2) : 'Inf';
    const pnl = r.total_pnl;
    const pnlStr = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
    const pnlCls = pnl >= 0 ? 'gap-pos' : 'gap-neg';
    const dd = '$' + r.max_drawdown.toFixed(2);
    const rowCls = i === 0 ? ' class="bt-best"' : '';
    html += '<tr' + rowCls + '><td>' + (i+1) + '</td><td>' + sl + '</td><td>' + mp + '</td><td>' + sc +
      '</td><td>' + r.total_trades + '</td><td>' + wl + '</td><td>' + wr + '</td><td>' + pf +
      '</td><td class="' + pnlCls + '">' + pnlStr + '</td><td class="gap-neg">' + dd + '</td></tr>';
  }
  document.getElementById('btBody').innerHTML = html;
  document.getElementById('btResults').style.display = '';
}
</script>
</body>
</html>"""


def _run_backtest_thread(days):
    """Background thread that runs the backtest sweep."""
    try:
        with backtest_lock:
            backtest_state["progress"] = f"Fetching {days} days of candle data..."

        candles = fetch_binance_klines(BACKTEST_SYMBOL, "1m", days)
        if not candles:
            with backtest_lock:
                backtest_state["status"] = "error"
                backtest_state["error"] = "Failed to fetch candle data from Binance."
            return

        def progress_cb(msg):
            with backtest_lock:
                backtest_state["progress"] = msg

        results = run_sweep_results(candles, days, progress_cb=progress_cb)

        # Check for better config and alert
        alert = check_better_config(results)

        with backtest_lock:
            backtest_state["results"] = results
            backtest_state["status"] = "done"
            backtest_state["progress"] = f"Complete — {len(results)} configs with trades"
            if alert:
                backtest_state["alert"] = alert

        if alert:
            add_log("WARNING", alert)
    except Exception as e:
        with backtest_lock:
            backtest_state["status"] = "error"
            backtest_state["error"] = f"{type(e).__name__}: {e}"


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            with state_lock:
                data = json.dumps(state)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data.encode())
        elif self.path == "/api/backtest":
            with backtest_lock:
                data = json.dumps(backtest_state)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data.encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

    def do_POST(self):
        if self.path == "/api/backtest":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                params = json.loads(body)
            except json.JSONDecodeError:
                params = {}

            days = params.get("days", 30)
            days = max(1, min(days, 365))

            with backtest_lock:
                if backtest_state["status"] == "running":
                    resp = json.dumps({"error": "already running"})
                    self.send_response(409)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(resp.encode())
                    return

                backtest_state["status"] = "running"
                backtest_state["progress"] = "Starting..."
                backtest_state["results"] = []
                backtest_state["error"] = ""

            t = threading.Thread(target=_run_backtest_thread, args=(days,), daemon=True)
            t.start()

            resp = json.dumps({"status": "started"})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP request logs


def start_dashboard():
    server = HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
