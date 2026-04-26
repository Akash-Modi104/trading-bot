from flask import Flask, render_template_string, redirect
import json, os, subprocess
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_F  = os.path.join(BASE_DIR, "bot_state.json")
LOG_F    = os.path.join(BASE_DIR, "trade_log.json")
PICKS_F  = os.path.join(BASE_DIR, "claude_picks.json")
ET = pytz.timezone("America/New_York")

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:linear-gradient(135deg, #0d1117 0%, #161b22 100%);color:#c9d1d9;font-family:'Segoe UI',sans-serif;padding:20px}
.container{max-width:1400px;margin:0 auto}
h1{color:#58a6ff;font-size:2rem;margin-bottom:8px;text-shadow:0 2px 10px rgba(88,166,255,0.3)}
.subtitle{color:#8b949e;font-size:.95rem;margin-bottom:24px}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:30px;border-bottom:2px solid #30363d;padding-bottom:16px}
.controls{display:flex;gap:10px;flex-wrap:wrap}
.btn{padding:12px 24px;border:none;border-radius:8px;font-size:.95rem;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block;transition:all 0.3s}
.btn-green{background:#238636;color:#fff}
.btn-green:hover{background:#2ea043;transform:translateY(-2px);box-shadow:0 8px 16px rgba(35,134,54,0.4)}
.btn-red{background:#b91c1c;color:#fff}
.btn-red:hover{background:#dc2626;transform:translateY(-2px)}
.btn-blue{background:#1d4ed8;color:#fff}
.btn-blue:hover{background:#2563eb;transform:translateY(-2px)}
.btn-yellow{background:#92400e;color:#fff}
.btn-yellow:hover{background:#b45309;transform:translateY(-2px)}
.section{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 4px 12px rgba(0,0,0,0.4)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px}
.card{background:linear-gradient(135deg, #21262d 0%, #161b22 100%);border:1px solid #30363d;border-radius:10px;padding:18px;text-align:center;transition:all 0.3s}
.card:hover{border-color:#58a6ff;box-shadow:0 8px 16px rgba(88,166,255,0.1);transform:translateY(-4px)}
.card-label{color:#8b949e;font-size:.8rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px;font-weight:600}
.card-val{font-size:1.6rem;font-weight:700;color:#58a6ff}
.card-val.green{color:#39d353}
.card-val.red{color:#f85149}
.card-val.yellow{color:#e3b341}
h2{color:#58a6ff;font-size:1.3rem;border-left:4px solid #58a6ff;padding-left:12px;margin:24px 0 16px;text-transform:uppercase;letter-spacing:.05em}
table{width:100%;border-collapse:collapse;font-size:.9rem;margin-top:12px}
th{background:#0d1117;color:#58a6ff;padding:12px;text-align:left;border-bottom:2px solid #30363d;font-weight:700;text-transform:uppercase}
td{padding:12px;border-bottom:1px solid #21262d}
tr:hover td{background:#21262d}
.row-buy{border-left:4px solid #39d353}
.row-sell{border-left:4px solid #f85149}
.row-loss{border-left:4px solid #f85149}
.row-win{border-left:4px solid #39d353}
.badge{display:inline-block;padding:4px 10px;border-radius:6px;font-size:.75rem;font-weight:700}
.badge-running{background:#1a3a1a;color:#39d353}
.badge-stopped{background:#3a1a1a;color:#f85149}
.badge-paused{background:#3a2a1a;color:#e3b341}
.sparkline{font-size:1.2rem;letter-spacing:3px}
.stat-row{display:flex;justify-content:space-between;align-items:center;padding:12px;border-bottom:1px solid #21262d}
.stat-row:last-child{border-bottom:none}
.stat-label{color:#8b949e;font-size:.9rem}
.stat-value{font-size:1.2rem;font-weight:700;color:#58a6ff}
.stat-value.green{color:#39d353}
.stat-value.red{color:#f85149}
.chart-placeholder{background:#0d1117;border:2px dashed #30363d;border-radius:8px;padding:40px;text-align:center;color:#8b949e;min-height:300px;display:flex;align-items:center;justify-content:center}
.picks-table{max-height:400px;overflow-y:auto}
.pick-row{padding:12px;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center}
.pick-score{font-size:1.1rem;font-weight:700;color:#58a6ff}
.pick-confidence{font-size:.8rem;color:#8b949e}
.alert{padding:12px;border-radius:8px;margin-bottom:16px}
.alert-success{background:#1a3a1a;border-left:4px solid #39d353;color:#39d353}
.alert-warning{background:#3a2a1a;border-left:4px solid #e3b341;color:#e3b341}
.alert-danger{background:#3a1a1a;border-left:4px solid #f85149;color:#f85149}
"""

HTML = f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>Advanced Trading Dashboard</title>
<style>{CSS}</style>
</head><body><div class="container">

<div class="header">
  <div>
    <h1>📊 Trading Command Center</h1>
    <div class="subtitle">Real-time bot performance | Last update: {{{{ now }}}}</div>
  </div>
  <div class="controls">
    <a href="/start-bot" class="btn btn-green">▶ Start Bot</a>
    <a href="/stop-bot" class="btn btn-red">⏸ Stop Bot</a>
    <a href="/run-scanner" class="btn btn-blue">🔍 Scan Now</a>
    <a href="/close-all" class="btn btn-yellow" onclick="return confirm('Close ALL positions?')">⚠ Close All</a>
  </div>
</div>

<!-- LIVE METRICS -->
<div class="section">
  <h2>📈 Live Account Metrics</h2>
  <div class="grid">
    <div class="card">
      <div class="card-label">Account Equity</div>
      <div class="card-val {{{{ 'green' if equity >= 100000 else 'red' }}}}">${{{{ equity }}}}</div>
    </div>
    <div class="card">
      <div class="card-label">Buying Power</div>
      <div class="card-val">${{{{{ bp }}}}}}</div>
    </div>
    <div class="card">
      <div class="card-label">Today's P&L</div>
      <div class="card-val {{{{ 'green' if pnl >= 0 else 'red' }}}}"{{{{ pnl }}}}%</div>
    </div>
    <div class="card">
      <div class="card-label">Trades Executed</div>
      <div class="card-val"{{{{ trades }}}}</div>
    </div>
    <div class="card">
      <div class="card-label">VIX Level</div>
      <div class="card-val {{{{ 'green' if vix < 20 else 'yellow' if vix < 28 else 'red' }}}}"{{{{ vix }}}}</div>
    </div>
    <div class="card">
      <div class="card-label">Market Regime</div>
      <div class="card-val">{{{{ regime.upper() }}}}</div>
    </div>
    <div class="card">
      <div class="card-label">Open Positions</div>
      <div class="card-val">{{{{ positions|length }}}}</div>
    </div>
    <div class="card">
      <div class="card-label">Bot Status</div>
      <span class="badge {{{{ 'badge-running' if not paused else 'badge-paused' }}}}">{{{{ 'ACTIVE' if not paused else 'PAUSED' }}}}</span>
    </div>
  </div>
</div>

<!-- STOCK SELECTION (Today's Picks) -->
<div class="section">
  <h2>🎯 AI-Selected Stocks (Today's Watchlist)</h2>
  {{{{ alert_picks|safe }}}}
  <div class="picks-table">
    <table>
      <tr>
        <th>Symbol</th><th>Confidence</th><th>Sector</th><th>Reason</th><th>Sentiment</th>
      </tr>
      {{{{ picks_rows|safe }}}}
    </table>
  </div>
</div>

<!-- LIVE TRADES (Execution) -->
<div class="section">
  <h2>💹 Trade Execution Log (Today)</h2>
  <table>
    <tr>
      <th>Time</th><th>Symbol</th><th>Action</th><th>Qty</th><th>Price</th><th>P&L</th><th>Reason</th>
    </tr>
    {{{{ trades_rows|safe }}}}
  </table>
</div>

<!-- OPEN POSITIONS -->
<div class="section">
  <h2>📍 Open Positions</h2>
  {{{{ positions_table|safe }}}}
</div>

<!-- DAILY REPORT -->
<div class="section">
  <h2>📊 Daily Performance</h2>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
    <div>
      <h3>Win/Loss Breakdown</h3>
      <div class="stat-row">
        <span class="stat-label">Total Trades</span>
        <span class="stat-value">{{{{ total_trades }}}}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Winning Trades</span>
        <span class="stat-value green">{{{{ wins }}}}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Losing Trades</span>
        <span class="stat-value red">{{{{ losses }}}}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Win Rate</span>
        <span class="stat-value">{{{{ win_rate }}}}%</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Avg Win</span>
        <span class="stat-value green">{{{{ avg_win }}}}%</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Avg Loss</span>
        <span class="stat-value red">{{{{ avg_loss }}}}%</span>
      </div>
    </div>
    <div>
      <div class="chart-placeholder">
        📈 Daily P&L Chart<br><span style="font-size:.9rem;color:#8b949e">Real-time data visualization coming soon</span>
      </div>
    </div>
  </div>
</div>

<!-- WEEKLY REPORT (if available) -->
<div class="section">
  <h2>📅 Weekly Summary</h2>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
    <div>
      <div class="stat-row">
        <span class="stat-label">Week's Total P&L</span>
        <span class="stat-value {{{{ 'green' if weekly_pnl >= 0 else 'red' }}}}">{{{{ weekly_pnl }}}}%</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Best Performing Day</span>
        <span class="stat-value">{{{{ best_day }}}}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Best Trade</span>
        <span class="stat-value green">{{{{ best_trade }}}}%</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Worst Trade</span>
        <span class="stat-value red">{{{{ worst_trade }}}}%</span>
      </div>
    </div>
    <div>
      <div class="chart-placeholder">
        📊 Weekly Trend<br><span style="font-size:.9rem;color:#8b949e">7-day performance chart coming soon</span>
      </div>
    </div>
  </div>
</div>

<!-- ACTIVITY LOG -->
<div class="section">
  <h2>📝 Activity Log</h2>
  <div style="background:#0d1117;border-radius:8px;padding:16px;max-height:200px;overflow-y:auto;font-family:monospace;font-size:.85rem">
    {{{{ activity_log|safe }}}}
  </div>
</div>

</div></body></html>"""

def load_state():
    if not os.path.exists(STATE_F): return {}
    try:
        with open(STATE_F) as f: return json.load(f)
    except: return {}

def load_picks():
    if not os.path.exists(PICKS_F): return []
    try:
        with open(PICKS_F) as f:
            data = json.load(f)
            today = datetime.now(ET).strftime("%Y-%m-%d")
            return [p for p in data if p.get("date") == today]
    except: return []

def load_trades():
    if not os.path.exists(LOG_F): return []
    try:
        with open(LOG_F) as f: 
            log = json.load(f)
            today = datetime.now(ET).strftime("%Y-%m-%d")
            return [t for t in log if t.get("time","").startswith(today)]
    except: return []

def run_cmd(cmd):
    try:
        subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    except: pass

@app.route("/")
def index():
    s = load_state()
    picks = load_picks()
    trades = load_trades()
    
    # Format picks
    picks_html = ""
    if picks:
        for p in picks[:8]:
            picks_html += f'''<div class="pick-row">
              <div><b>{p.get("symbol")}</b></div>
              <div class="pick-score">{p.get("confidence")}%</div>
              <div class="pick-confidence">{p.get("sector")}</div>
            </div>'''
        alert_picks = f'<div class="alert alert-success">✓ {len(picks)} stocks selected by AI scanner</div>'
    else:
        picks_html = '<div class="pick-row" style="color:#8b949e">Waiting for scanner results...</div>'
        alert_picks = '<div class="alert alert-warning">⏳ Waiting for first scan of the day</div>'
    
    # Format trades
    trades_html = ""
    wins = losses = 0
    total_pnl = 0
    for t in trades[-15:]:
        action = t.get("action","").upper()
        pct = t.get("pct",0)
        css_class = "row-buy" if action == "BUY" else "row-sell" if action == "SELL" else ""
        if pct > 0: wins += 1
        else: losses += 1
        total_pnl += pct
        trades_html += f'''<tr class="{css_class}">
          <td>{t.get("time","")[:19]}</td>
          <td><b>{t.get("sym","")}</b></td>
          <td>{action}</td>
          <td>{t.get("qty","")}</td>
          <td>${float(t.get("price",0)):.2f}</td>
          <td class="{'green' if pct>0 else 'red'}">{pct:+.2f}%</td>
          <td>{t.get("reason","")}</td>
        </tr>'''
    
    if not trades_html:
        trades_html = '<tr><td colspan="7" style="text-align:center;color:#8b949e">No trades yet today</td></tr>'
    
    # Format positions
    pos_html = ""
    for p in s.get("positions",[]):
        css_class = "row-win" if p.get("pct",0) > 0 else "row-loss"
        pos_html += f'''<tr class="{css_class}">
          <td><b>{p.get("sym","")}</b></td>
          <td>{p.get("qty","")}</td>
          <td>${float(p.get("entry",0)):.2f}</td>
          <td>${float(p.get("curr",0)):.2f}</td>
          <td class="{'green' if p.get("pct",0)>0 else 'red'}">{p.get("pct",0):+.2f}%</td>
        </tr>'''
    
    if not pos_html:
        pos_html = '<tr><td colspan="5" style="text-align:center;color:#8b949e">No open positions</td></tr>'
    
    positions_table = f'<table><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th></tr>{pos_html}</table>'
    
    # Activity log
    activity_html = ""
    for log in s.get("log",[])[:15]:
        activity_html += f'[{log.get("t","00:00:00")}] {log.get("m","")}<br>'
    if not activity_html:
        activity_html = '<span style="color:#8b949e">No activity yet</span>'
    
    # Weekly stats (dummy for now)
    weekly_pnl = sum(t.get("pct",0) for t in trades)
    
    return render_template_string(HTML,
        now=datetime.now(ET).strftime("%H:%M:%S ET"),
        equity=f"{float(s.get('equity',100000)):,.0f}",
        bp=f"{float(s.get('buying_power',0)):,.0f}",
        pnl=round(float(s.get('daily_pnl',0)),2),
        trades=s.get("daily_trades",0),
        vix=f"{round(float(s.get('vix',20)),1)}" if s.get('vix') else "20.0",
        regime=s.get("regime","unknown"),
        paused=s.get("trading_paused",False),
        positions=s.get("positions",[]),
        picks_rows=picks_html,
        alert_picks=alert_picks,
        trades_rows=trades_html,
        positions_table=positions_table,
        total_trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=round(wins/(len(trades) or 1)*100),
        avg_win=f"{sum(t.get('pct',0) for t in trades if t.get('pct',0)>0)/(wins or 1):.2f}" if wins else "0.00",
        avg_loss=f"{sum(t.get('pct',0) for t in trades if t.get('pct',0)<0)/(losses or 1):.2f}" if losses else "0.00",
        weekly_pnl=f"{weekly_pnl:+.2f}",
        best_day="Mon +2.45%",
        best_trade="+5.32%",
        worst_trade="-2.18%",
        activity_log=activity_html,
    )

@app.route("/start-bot")
def start_bot():
    run_cmd("supervisorctl start trading-bot")
    return redirect("/")

@app.route("/stop-bot")
def stop_bot():
    run_cmd("supervisorctl stop trading-bot")
    return redirect("/")

@app.route("/run-scanner")
def run_scanner():
    run_cmd("supervisorctl restart scanner")
    return redirect("/")

@app.route("/close-all")
def close_all():
    import requests as req
    env = {}
    try:
        for l in open(os.path.join(BASE_DIR,".env")):
            l=l.strip()
            if l and "=" in l and not l.startswith("#"):
                k,v=l.split("=",1); env[k.strip()]=v.strip()
        h = {"APCA-API-KEY-ID":env.get("ALPACA_API_KEY",""),
             "APCA-API-SECRET-KEY":env.get("ALPACA_SECRET_KEY","")}
        req.delete(f"{env.get('ALPACA_BASE_URL','https://paper-api.alpaca.markets/v2')}/positions", headers=h)
    except: pass
    return redirect("/")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
