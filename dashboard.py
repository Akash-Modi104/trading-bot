from flask import Flask,render_template_string,redirect
import json,os,subprocess
from datetime import datetime
import pytz

app=Flask(__name__)
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
ET=pytz.timezone("America/New_York")

HTML=r"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="15"><title>Trading Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',sans-serif;padding:20px}
h1{color:#58a6ff;font-size:1.8rem;margin-bottom:6px}
.sub{color:#8b949e;font-size:.9rem;margin-bottom:20px}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px}
.btn{padding:12px 24px;border:none;border-radius:8px;font-weight:700;cursor:pointer;text-decoration:none;font-size:.9rem;display:inline-block;color:#fff}
.btn-green{background:#238636}.btn-red{background:#b91c1c}
.btn-blue{background:#1d4ed8}.btn-yellow{background:#92400e}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px;margin-bottom:24px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px;text-align:center}
.lbl{color:#8b949e;font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}
.val{font-size:1.5rem;font-weight:700;color:#58a6ff}
.green{color:#39d353}.red{color:#f85149}.yellow{color:#e3b341}.purple{color:#bc8cff}
.section{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px;margin-bottom:18px}
h2{color:#58a6ff;font-size:1.1rem;border-left:3px solid #58a6ff;padding-left:10px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th{background:#0d1117;color:#58a6ff;padding:10px;text-align:left;border-bottom:2px solid #30363d}
td{padding:10px;border-bottom:1px solid #21262d}
tr:hover td{background:#21262d}
.badge{padding:3px 9px;border-radius:5px;font-size:.78rem;font-weight:700}
.b-ok{background:#1a3a1a;color:#39d353}.b-stop{background:#3a1a1a;color:#f85149}.b-pause{background:#3a2a1a;color:#e3b341}
.log{background:#0d1117;border-radius:8px;padding:14px;max-height:220px;overflow-y:auto;font-family:monospace;font-size:.82rem;line-height:1.6}
.stat{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #21262d}
.stat:last-child{border-bottom:none}
.alert-ok{background:#1a3a1a;border-left:4px solid #39d353;padding:10px 14px;border-radius:4px;color:#39d353;margin-bottom:12px}
.alert-warn{background:#3a2a1a;border-left:4px solid #e3b341;padding:10px 14px;border-radius:4px;color:#e3b341;margin-bottom:12px}
</style></head><body>
<h1>Trading Command Center</h1>
<div class="sub">Auto-refreshes every 15s | {{ now }}</div>

<div class="controls">
  <a href="/start-bot" class="btn btn-green">&#9654; Start Bot</a>
  <a href="/stop-bot"  class="btn btn-red">&#9646;&#9646; Stop Bot</a>
  <a href="/run-scanner" class="btn btn-blue">&#128269; Scan Now</a>
  <a href="/close-all" class="btn btn-yellow" onclick="return confirm('Close ALL positions?')">&#9888; Close All</a>
</div>

<!-- KPI CARDS -->
<div class="grid">
  <div class="card"><div class="lbl">Equity</div><div class="val">${{ equity }}</div></div>
  <div class="card"><div class="lbl">Buying Power</div><div class="val">${{ bp }}</div></div>
  <div class="card"><div class="lbl">Day P&L</div><div class="val {{ pnl_color }}">{{ pnl }}%</div></div>
  <div class="card"><div class="lbl">Trades</div><div class="val purple">{{ trades }}</div></div>
  <div class="card"><div class="lbl">VIX</div><div class="val {{ vix_color }}">{{ vix }}</div></div>
  <div class="card"><div class="lbl">Regime</div><div class="val">{{ regime }}</div></div>
  <div class="card"><div class="lbl">Positions</div><div class="val">{{ n_pos }}</div></div>
  <div class="card"><div class="lbl">Bot</div><span class="badge {{ bot_badge }}">{{ bot_label }}</span></div>
</div>

<!-- AI PICKS -->
<div class="section">
  <h2>AI Stock Selection (Today)</h2>
  {{ picks_alert|safe }}
  <table>
    <tr><th>Symbol</th><th>Confidence</th><th>Sector</th><th>Sentiment</th><th>Reason</th></tr>
    {{ picks_rows|safe }}
  </table>
</div>

<!-- TRADE EXECUTION -->
<div class="section">
  <h2>Trade Execution Log</h2>
  <table>
    <tr><th>Time</th><th>Symbol</th><th>Action</th><th>Qty</th><th>Price</th><th>P&L</th><th>Reason</th></tr>
    {{ trades_rows|safe }}
  </table>
</div>

<!-- OPEN POSITIONS -->
<div class="section">
  <h2>Open Positions</h2>
  <table>
    <tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L %</th></tr>
    {{ pos_rows|safe }}
  </table>
</div>

<!-- DAILY REPORT -->
<div class="section">
  <h2>Daily Report</h2>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
    <div>
      <div class="stat"><span>Total Trades</span><b>{{ total_t }}</b></div>
      <div class="stat"><span>Wins</span><b class="green">{{ wins }}</b></div>
      <div class="stat"><span>Losses</span><b class="red">{{ losses }}</b></div>
      <div class="stat"><span>Win Rate</span><b>{{ win_rate }}%</b></div>
      <div class="stat"><span>Avg Win</span><b class="green">{{ avg_win }}%</b></div>
      <div class="stat"><span>Avg Loss</span><b class="red">{{ avg_loss }}%</b></div>
      <div class="stat"><span>Total P&L</span><b class="{{ pnl_color }}">{{ total_pnl }}%</b></div>
    </div>
    <div>
      <div class="stat"><span>Best Trade</span><b class="green">{{ best_trade }}%</b></div>
      <div class="stat"><span>Worst Trade</span><b class="red">{{ worst_trade }}%</b></div>
      <div class="stat"><span>Bot Uptime</span><b>{{ uptime }}</b></div>
      <div class="stat"><span>Last Scan</span><b>{{ last_scan }}</b></div>
    </div>
  </div>
</div>

<!-- ACTIVITY LOG -->
<div class="section">
  <h2>Activity Log</h2>
  <div class="log">{{ log_html|safe }}</div>
</div>
</body></html>"""

def load_json(path):
    if not os.path.exists(path): return {}
    try:
        with open(path) as f: return json.load(f)
    except: return {}

def load_list(path):
    if not os.path.exists(path): return []
    try:
        with open(path) as f: return json.load(f)
    except: return []

def run_cmd(cmd):
    try: subprocess.run(cmd,shell=True,capture_output=True,timeout=10)
    except: pass

def today_str():
    return datetime.now(ET).strftime("%Y-%m-%d")

@app.route("/")
def index():
    s      = load_json(BASE_DIR+"/bot_state.json")
    all_t  = load_list(BASE_DIR+"/trade_log.json")
    picks  = load_list(BASE_DIR+"/claude_picks.json")
    today  = today_str()
    trades = [t for t in all_t if t.get("time","").startswith(today)]
    picks  = [p for p in picks if p.get("date")==today]

    pnl   = round(float(s.get("daily_pnl",0)),2)
    vix   = s.get("vix")
    paused= s.get("trading_paused",False)

    # Picks table
    if picks:
        pr = ""
        for p in picks[:8]:
            conf = p.get("confidence",0)
            color = "green" if conf>=75 else "yellow" if conf>=60 else "red"
            pr += f'<tr><td><b>{p.get("symbol","")}</b></td>'
            pr += f'<td class="{color}"><b>{conf}%</b></td>'
            pr += f'<td>{p.get("sector","")}</td>'
            pr += f'<td class="green">{p.get("news_sentiment","")}</td>'
            pr += f'<td>{p.get("reason","")[:60]}</td></tr>'
        pa = f'<div class="alert-ok">✓ {len(picks)} stocks selected by AI scanner today</div>'
    else:
        pr = '<tr><td colspan="5" style="text-align:center;color:#8b949e">Waiting for scanner — market may be closed</td></tr>'
        pa = '<div class="alert-warn">⏳ No picks yet today. Scanner runs every 15 min during market hours.</div>'

    # Trades table
    tr_html = ""
    wins=losses=0
    pcts=[]
    for t in trades[-15:]:
        act = t.get("action","").upper()
        p2  = t.get("pct",0)
        pcts.append(p2)
        if act=="SELL": wins+=1 if p2>0 else 0; losses+=1 if p2<=0 else 0
        col = "green" if act=="BUY" else "green" if p2>0 else "red"
        tr_html += f'<tr><td>{t.get("time","")[:19]}</td><td><b>{t.get("sym","")}</b></td>'
        tr_html += f'<td class="{col}"><b>{act}</b></td><td>{t.get("qty","—")}</td>'
        tr_html += f'<td>${float(t.get("price",0)):.2f}</td>'
        tr_html += f'<td class="{"green" if p2>0 else "red"}">{p2:+.2f}%</td>'
        tr_html += f'<td>{str(t.get("reason",""))[:40]}</td></tr>'
    if not tr_html:
        tr_html='<tr><td colspan="7" style="text-align:center;color:#8b949e">No trades yet today</td></tr>'

    # Positions table
    pos_html=""
    for p in s.get("positions",[]):
        pc=p.get("pct",0)
        pos_html+=f'<tr><td><b>{p.get("sym","")}</b></td><td>{p.get("qty","")}</td>'
        pos_html+=f'<td>${float(p.get("entry",0)):.2f}</td><td>${float(p.get("curr",0)):.2f}</td>'
        pos_html+=f'<td class="{"green" if pc>=0 else "red"}"><b>{pc:+.2f}%</b></td></tr>'
    if not pos_html:
        pos_html='<tr><td colspan="5" style="text-align:center;color:#8b949e">No open positions</td></tr>'

    # Log
    log_html=""
    for l in s.get("log",[])[:20]:
        log_html+=f'[{l.get("t","?")}] {l.get("m","")}<br>'
    if not log_html: log_html='<span style="color:#8b949e">No activity yet</span>'

    # Stats
    sell_pcts=[t.get("pct",0) for t in trades if t.get("action")=="sell"]
    win_pcts =[p for p in sell_pcts if p>0]
    los_pcts =[p for p in sell_pcts if p<=0]
    wr = round(len(win_pcts)/max(len(sell_pcts),1)*100)

    return render_template_string(HTML,
        now       = datetime.now(ET).strftime("%Y-%m-%d %I:%M:%S %p ET"),
        equity    = f"{float(s.get('equity',0)):,.0f}",
        bp        = f"{float(s.get('buying_power',0)):,.0f}",
        pnl       = pnl,
        pnl_color = "green" if pnl>=0 else "red",
        trades    = s.get("daily_trades",0),
        vix       = round(float(vix),1) if vix else "—",
        vix_color = "green" if vix and float(vix)<20 else "yellow" if vix and float(vix)<28 else "red",
        regime    = s.get("regime","unknown").upper(),
        n_pos     = len(s.get("positions",[])),
        bot_badge = "b-ok" if not paused else "b-pause",
        bot_label = "ACTIVE" if not paused else "PAUSED",
        picks_rows= pr,
        picks_alert=pa,
        trades_rows=tr_html,
        pos_rows  = pos_html,
        total_t   = len(trades),
        wins=wins, losses=losses, win_rate=wr,
        avg_win   = f"{sum(win_pcts)/max(len(win_pcts),1):.2f}",
        avg_loss  = f"{sum(los_pcts)/max(len(los_pcts),1):.2f}",
        total_pnl = f"{sum(sell_pcts):+.2f}",
        best_trade= f"{max(sell_pcts,default=0):+.2f}",
        worst_trade=f"{min(sell_pcts,default=0):+.2f}",
        uptime    = s.get("started","—")[:16] if s.get("started") else "—",
        last_scan = s.get("last_scan","—")[:16] if s.get("last_scan") else "—",
        log_html  = log_html,
    )

@app.route("/start-bot")
def start_bot():
    run_cmd("supervisorctl start trading-bot"); return redirect("/")

@app.route("/stop-bot")
def stop_bot():
    run_cmd("supervisorctl stop trading-bot"); return redirect("/")

@app.route("/run-scanner")
def run_scanner():
    run_cmd("supervisorctl restart scanner"); return redirect("/")

@app.route("/close-all")
def close_all():
    import requests as rq
    env={}
    try:
        for line in open(BASE_DIR+"/.env"):
            line=line.strip()
            if line and "=" in line and not line.startswith("#"):
                k,v=line.split("=",1); env[k.strip()]=v.strip()
        h={"APCA-API-KEY-ID":env.get("ALPACA_API_KEY",""),
           "APCA-API-SECRET-KEY":env.get("ALPACA_SECRET_KEY","")}
        rq.delete(env.get("ALPACA_BASE_URL","https://paper-api.alpaca.markets/v2")+"/positions",headers=h)
    except: pass
    return redirect("/")

@app.route("/api/state")
def api_state():
    return json.dumps(load_json(BASE_DIR+"/bot_state.json")),200,{"Content-Type":"application/json"}

if __name__=="__main__":
    print("Dashboard on http://0.0.0.0:5001")
    app.run(host="0.0.0.0",port=5001,debug=False)
