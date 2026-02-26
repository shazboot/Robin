import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

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
}
state_lock = threading.Lock()


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
  #rsiChart { width: 100%; height: 150px; margin-top: 4px; }
  #macdChart { width: 100%; height: 150px; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  th { text-align: left; font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; padding: 8px 12px; border-bottom: 1px solid #30363d; }
  td { padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 14px; }
  tr:hover { background: #1c2128; }
  .side-buy { color: #3fb950; font-weight: 600; }
  .side-sell { color: #f85149; font-weight: 600; }
  .warmup-bar { background: #21262d; border-radius: 4px; height: 8px; margin-top: 8px; overflow: hidden; }
  .warmup-fill { background: #58a6ff; height: 100%; border-radius: 4px; transition: width 0.5s; }
  .indicators { display: flex; gap: 24px; margin-top: 8px; }
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

  <div class="card wide">
    <div class="label">Price Chart</div>
    <div class="chart-legend">
      <div class="legend-item"><div class="legend-color" style="background:#2962FF"></div> Price</div>
      <div class="legend-item"><div class="legend-color" style="background:#FF6D00"></div> EMA-9</div>
      <div class="legend-item"><div class="legend-color" style="background:#AA00FF"></div> EMA-21</div>
      <div class="legend-item"><div class="legend-color" style="background:#26A69A"></div> EMA-50</div>
      <div class="legend-item"><div class="legend-color" style="background:rgba(33,150,243,0.3)"></div> Bollinger</div>
      <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div> Buy</div>
      <div class="legend-item"><div class="legend-color" style="background:#f85149"></div> Sell</div>
    </div>
    <div id="priceChart"></div>
  </div>

  <div class="card wide">
    <div class="label">RSI (14)</div>
    <div id="rsiChart"></div>
  </div>

  <div class="card wide">
    <div class="label">MACD (12, 26, 9)</div>
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
const priceChart = LightweightCharts.createChart(document.getElementById('priceChart'), chartOptions);
const priceSeries = priceChart.addLineSeries({ color: '#2962FF', lineWidth: 2, priceFormat: { type: 'price', precision: 2, minMove: 0.01 } });
const ema9Series = priceChart.addLineSeries({ color: '#FF6D00', lineWidth: 1 });
const ema21Series = priceChart.addLineSeries({ color: '#AA00FF', lineWidth: 1 });
const ema50Series = priceChart.addLineSeries({ color: '#26A69A', lineWidth: 2, lineStyle: 2 });
const bbUpperSeries = priceChart.addLineSeries({ color: 'rgba(33,150,243,0.3)', lineWidth: 1, lineStyle: 2 });
const bbLowerSeries = priceChart.addLineSeries({ color: 'rgba(33,150,243,0.3)', lineWidth: 1, lineStyle: 2 });

// RSI chart
const rsiChartOpts = JSON.parse(JSON.stringify(chartOptions));
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
  rsiChart.applyOptions({ width: rsiEl.clientWidth, height: 150 });
  macdChart.applyOptions({ width: macdEl.clientWidth, height: 150 });
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

    // EMA-50 Trend status
    if (d.ema_trend) {
      const trendEl = document.getElementById('trendStatus');
      const ts = d.trend_status || '--';
      trendEl.textContent = ts === 'UP' ? 'Price above' : ts === 'DOWN' ? 'Price below' : '--';
      trendEl.className = 'ind-gap ' + (ts === 'UP' ? 'gap-pos' : ts === 'DOWN' ? 'gap-neg' : '');
    }

    // MACD status
    if (d.macd_hist) {
      const macdStEl = document.getElementById('macdStatus');
      const ms = d.macd_status || '--';
      macdStEl.textContent = ms;
      macdStEl.className = 'ind-gap ' + (ms === 'Bullish' ? 'gap-pos' : ms === 'Bearish' ? 'gap-neg' : '');
    }

    // Bollinger status
    if (d.bb_upper && d.bb_lower) {
      const bbStEl = document.getElementById('bbStatus');
      const p = d.current_price;
      if (p >= d.bb_upper) { bbStEl.textContent = 'At upper band'; bbStEl.className = 'ind-gap gap-neg'; }
      else if (p <= d.bb_lower) { bbStEl.textContent = 'At lower band'; bbStEl.className = 'ind-gap gap-pos'; }
      else { bbStEl.textContent = '$' + fmt(d.bb_lower) + ' - $' + fmt(d.bb_upper); bbStEl.className = 'ind-gap'; }
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

    updateCharts(d.price_history, d.trades);
    renderTrades(d.trades);
    renderLog(d.log_entries || []);
  } catch(e) { console.error('Refresh failed:', e); }
}

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            with state_lock:
                data = json.dumps(state)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data.encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

    def log_message(self, format, *args):
        pass  # Suppress HTTP request logs


def start_dashboard():
    server = HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
