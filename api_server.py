from flask import Flask, jsonify, request, render_template_string
import json, os, subprocess
from datetime import datetime
import pytz

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_F  = os.path.join(BASE_DIR, "bot_state.json")
LOG_F    = os.path.join(BASE_DIR, "trade_log.json")
PICKS_F  = os.path.join(BASE_DIR, "claude_picks.json")
ET = pytz.timezone("America/New_York")

def load_json(filepath, default_val):
    if not os.path.exists(filepath): return default_val
    try:
        with open(filepath) as f: return json.load(f)
    except: return default_val

def run_cmd(cmd):
    try:
        subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    except Exception as e: 
        print(f"Error running cmd {cmd}: {e}")

@app.route("/")
def index():
    # Serve the React application HTML
    html_path = os.path.join(BASE_DIR, "templates", "react_index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "Template not found. Please ensure templates/react_index.html exists."

@app.route("/api/data")
def api_data():
    s = load_json(STATE_F, {})
    picks = load_json(PICKS_F, [])
    trades = load_json(LOG_F, [])
    
    today = datetime.now(ET).strftime("%Y-%m-%d")
    today_picks = [p for p in picks if p.get("date") == today]
    today_trades = [t for t in trades if t.get("time","").startswith(today)]
    
    wins = losses = 0
    total_pnl = 0
    for t in today_trades:
        pct = t.get("pct", 0)
        if pct > 0: wins += 1
        else: losses += 1
        total_pnl += pct

    weekly_pnl = sum(t.get("pct",0) for t in trades)

    return jsonify({
        "timestamp": datetime.now(ET).strftime("%H:%M:%S ET"),
        "metrics": {
            "equity": s.get("equity", 100000),
            "buying_power": s.get("buying_power", 0),
            "daily_pnl": s.get("daily_pnl", 0),
            "trades_count": s.get("daily_trades", 0),
            "vix": s.get("vix", 20),
            "regime": s.get("regime", "unknown"),
            "paused": s.get("trading_paused", False)
        },
        "stats": {
            "total_trades": len(today_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / (len(today_trades) or 1) * 100),
            "avg_win": sum(t.get("pct",0) for t in today_trades if t.get("pct",0)>0) / (wins or 1),
            "avg_loss": sum(t.get("pct",0) for t in today_trades if t.get("pct",0)<0) / (losses or 1),
            "weekly_pnl": weekly_pnl
        },
        "picks": today_picks,
        "trades": today_trades[-15:], # Last 15 trades
        "positions": s.get("positions", []),
        "activity_log": s.get("log", [])[:15]
    })

@app.route("/api/action", methods=["POST"])
def api_action():
    data = request.get_json()
    action = data.get("action")
    
    if action == "start":
        run_cmd("supervisorctl start trading-bot")
        return jsonify({"status": "success", "message": "Bot started"})
    elif action == "stop":
        run_cmd("supervisorctl stop trading-bot")
        return jsonify({"status": "success", "message": "Bot stopped"})
    elif action == "scan":
        run_cmd("supervisorctl restart scanner")
        return jsonify({"status": "success", "message": "Scanner restarted"})
    elif action == "close_all":
        close_all_positions()
        return jsonify({"status": "success", "message": "All positions closed"})
    
    return jsonify({"status": "error", "message": "Unknown action"}), 400

def close_all_positions():
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
