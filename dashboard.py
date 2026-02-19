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
    "required_points": 23,
    "last_update": "",
    "trades": [],       # list of {time, side, price, quantity, amount}
    "price_history": [], # list of {time, price}
    "total_pnl": 0,
    "starting_value": 0,
    "current_value": 0,
    "log_entries": [],  # list of {time, level, message}
}
state_lock = threading.Lock()


def update_state(**kwargs):
    with state_lock:
        state.update(kwargs)


def add_trade(side, price, quantity, amount):
    with state_lock:
        state["trades"].append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "side": side,
            "price": round(price, 2),
            "quantity": round(quantity, 8),
            "amount": round(amount, 2),
        })
        _recalc_pnl()


def add_price_point(price):
    with state_lock:
        state["price_history"].append({
            "time": datetime.now().strftime("%H:%M"),
            "price": round(price, 2),
        })
        # Keep last 200 points for the chart
        if len(state["price_history"]) > 200:
            state["price_history"] = state["price_history"][-200:]


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
  .chart-container { position: relative; height: 300px; padding-top: 10px; }
  canvas { width: 100% !important; height: 100% !important; }
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
</style>
</head>
<body>
<div class="header">
  <h1>BTC Trading Bot</h1>
  <div class="status">Last update: <span id="lastUpdate">--</span> &nbsp; | &nbsp; Auto-refresh: 10s</div>
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
      <div class="ind"><div class="ind-label">EMA-9</div><div class="ind-val" id="ema9">--</div></div>
      <div class="ind"><div class="ind-label">EMA-21</div><div class="ind-val" id="ema21">--</div></div>
      <div class="ind"><div class="ind-label">RSI-14</div><div class="ind-val" id="rsi">--</div></div>
      <div class="ind"><div class="ind-label">Data Points</div><div class="ind-val" id="dataPoints">--</div></div>
    </div>
    <div class="warmup-bar"><div class="warmup-fill" id="warmupBar"></div></div>
  </div>

  <div class="card wide">
    <div class="label">Price History</div>
    <div class="chart-container"><canvas id="chart"></canvas></div>
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

function drawChart(prices, trades) {
  const canvas = document.getElementById('chart');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  ctx.clearRect(0, 0, W, H);
  if (prices.length < 2) { ctx.fillStyle = '#484f58'; ctx.fillText('Collecting data...', W/2 - 50, H/2); return; }

  const vals = prices.map(p => p.price);
  const min = Math.min(...vals) * 0.9999;
  const max = Math.max(...vals) * 1.0001;
  const range = max - min || 1;
  const pad = 60;

  // Grid lines
  ctx.strokeStyle = '#21262d'; ctx.lineWidth = 1;
  for (let i = 0; i < 5; i++) {
    const y = pad + (H - pad*2) * i / 4;
    ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(W - 10, y); ctx.stroke();
    ctx.fillStyle = '#484f58'; ctx.font = '11px monospace';
    ctx.fillText('$' + fmt(max - range * i / 4), 0, y + 4);
  }

  // Price line
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 2;
  ctx.beginPath();
  for (let i = 0; i < vals.length; i++) {
    const x = pad + (W - pad - 10) * i / (vals.length - 1);
    const y = pad + (H - pad*2) * (1 - (vals[i] - min) / range);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Gradient fill
  const last_x = pad + (W - pad - 10);
  ctx.lineTo(last_x, H - pad); ctx.lineTo(pad, H - pad); ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(88,166,255,0.15)'); grad.addColorStop(1, 'rgba(88,166,255,0)');
  ctx.fillStyle = grad; ctx.fill();

  // Trade markers
  const tradesByTime = {};
  trades.forEach(t => { tradesByTime[t.time.split(' ')[1].slice(0,5)] = t; });
  prices.forEach((p, i) => {
    const t = tradesByTime[p.time];
    if (!t) return;
    const x = pad + (W - pad - 10) * i / (vals.length - 1);
    const y = pad + (H - pad*2) * (1 - (p.price - min) / range);
    ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fillStyle = t.side === 'buy' ? '#3fb950' : '#f85149'; ctx.fill();
    ctx.strokeStyle = '#0d1117'; ctx.lineWidth = 2; ctx.stroke();
  });

  // Time labels
  ctx.fillStyle = '#484f58'; ctx.font = '11px monospace';
  const step = Math.max(1, Math.floor(prices.length / 8));
  for (let i = 0; i < prices.length; i += step) {
    const x = pad + (W - pad - 10) * i / (vals.length - 1);
    ctx.fillText(prices[i].time, x - 12, H - pad + 16);
  }
}

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
    if (e.message.includes('BUY SIGNAL') || e.message.includes('BUYING')) cls = 'log-BUY';
    if (e.message.includes('SELL SIGNAL') || e.message.includes('SELLING')) cls = 'log-SELL';
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
    document.getElementById('rsi').textContent = d.rsi ? d.rsi.toFixed(1) : '--';
    document.getElementById('dataPoints').textContent = d.data_points + '/' + d.required_points;
    document.getElementById('warmupBar').style.width = Math.min(100, d.data_points / d.required_points * 100) + '%';
    document.getElementById('lastUpdate').textContent = d.last_update;
    drawChart(d.price_history, d.trades);
    renderTrades(d.trades);
    renderLog(d.log_entries || []);
  } catch(e) { console.error('Refresh failed:', e); }
}

refresh();
setInterval(refresh, 10000);
window.addEventListener('resize', refresh);
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
