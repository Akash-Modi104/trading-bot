from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Lock
import json
from plotly.utils import PlotlyJSONEncoder
import plotly.graph_objects as go





app = Flask(__name__)
CORS(app)

COMPANY_LIST_FILE = "company_list.txt"
PORTFOLIO_FILE = "portfolio.json"
ARCHIVE_FILE = "archive.json"

cache = {}  # In-memory cache for stock data
cache_lock = Lock()  # Ensure thread-safe access to cache

# ------------------------------------------------------------
# 1. File / JSON Helpers
# ------------------------------------------------------------
def ensure_file_exists(filename):
    """Ensure a file exists; if not, create an empty JSON list if .json."""
    if not os.path.exists(filename):
        with open(filename, "w") as f:
            if filename.endswith(".json"):
                json.dump([], f)

def load_company_list():
    if not os.path.exists(COMPANY_LIST_FILE):
        with open(COMPANY_LIST_FILE, "w"):
            pass
    with open(COMPANY_LIST_FILE, "r") as f:
        return [line.strip() for line in f]

def save_company_list(companies):
    with open(COMPANY_LIST_FILE, "w") as f:
        f.write("\n".join(companies))

def load_portfolio():
    ensure_file_exists(PORTFOLIO_FILE)
    with open(PORTFOLIO_FILE, "r") as f:
        try:
            return json.load(f)
        except:
            return []

def save_portfolio(portfolio):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2)

def load_archive():
    ensure_file_exists(ARCHIVE_FILE)
    with open(ARCHIVE_FILE, "r") as f:
        try:
            return json.load(f)
        except:
            return []

def save_archive(archive_list):
    with open(ARCHIVE_FILE, "w") as f:
        json.dump(archive_list, f, indent=2)

# ------------------------------------------------------------
# 2. Fetch & Cache Stock Data
# ------------------------------------------------------------
def fetch_stock_data(ticker):
    if ticker in cache:
        return cache[ticker]
    end_date = datetime.now()
    start_date = end_date - timedelta(days=565)  # 365+200
    try:
        data = yf.download(ticker, start=start_date.strftime('%Y-%m-%d'), end=end_date.strftime('%Y-%m-%d'))
        if not data.empty:
            cache[ticker] = data
            return data
    except Exception as e:
        print(f"Error fetching data for {ticker}: {e}")
    return None

def fetch_stock_data_safe(ticker):
    with cache_lock:
        return fetch_stock_data(ticker)

# ------------------------------------------------------------
# 3. SMA & Buy/Sell Signals
# ------------------------------------------------------------
def calculate_sma(df):
    if 'Close' in df.columns and len(df) >= 200:
        df['SMA20'] = df['Close'].rolling(window=20).mean()
        df['SMA100'] = df['Close'].rolling(window=100).mean()
        df['SMA200'] = df['Close'].rolling(window=200).mean()

        df['BuySignal'] = (
            (df['Close'] < df['SMA20']) &
            (df['SMA20'] < df['SMA100']) &
            (df['SMA100'] < df['SMA200'])
        )
        df['SellSignal'] = (
            (df['Close'] > df['SMA20']) &
            (df['SMA20'] > df['SMA100']) &
            (df['SMA100'] > df['SMA200'])
        )
    return df

# ------------------------------------------------------------
# 4. Single Pair Logic (Legacy)
# ------------------------------------------------------------
def find_first_buy_sell_pair(df):
    df = df.sort_index()
    buy_indices = df.index[df['BuySignal'] == True]
    sell_indices = df.index[df['SellSignal'] == True]
    if buy_indices.empty or sell_indices.empty:
        return None, None

    for bdate in buy_indices:
        valid_sells = sell_indices[sell_indices > bdate]
        if not valid_sells.empty:
            return bdate, valid_sells[0]
    return None, None

def calculate_signals_for_company(company):
    df = fetch_stock_data_safe(company)
    if df is None or df.empty:
        return {
            "company": company,
            "buy_date": "N/A",
            "buy_price": "N/A",
            "sell_date": "N/A",
            "sell_price": "N/A",
            "profit": "N/A"
        }
    df = calculate_sma(df)
    bdate, sdate = find_first_buy_sell_pair(df)
    if not bdate or not sdate:
        return {
            "company": company,
            "buy_date": "N/A",
            "buy_price": "N/A",
            "sell_date": "N/A",
            "sell_price": "N/A",
            "profit": "N/A"
        }
    buy_price = round(df.loc[bdate, 'Close'], 2)
    sell_price = round(df.loc[sdate, 'Close'], 2)
    profit = round(((sell_price - buy_price) / buy_price) * 100, 2)
    return {
        "company": company,
        "buy_date": bdate.strftime('%Y-%m-%d'),
        "buy_price": buy_price,
        "sell_date": sdate.strftime('%Y-%m-%d'),
        "sell_price": sell_price,
        "profit": profit
    }

# ------------------------------------------------------------
# 5. Plot Graph
# ------------------------------------------------------------
from plotly.utils import PlotlyJSONEncoder
import plotly.graph_objects as go
import json

def plot_graph(df, ticker, buy_points, sell_points):
    df = df[-365:]
    fig = go.Figure()

    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df['Open'],
        high=df['High'],
        low=df['Low'],
        close=df['Close'],
        name='Candlestick'
    ))

    if 'SMA20' in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df['SMA20'],
                                 mode='lines', name='SMA20',
                                 line=dict(color='yellow')))
    if 'SMA100' in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df['SMA100'],
                                 mode='lines', name='SMA100',
                                 line=dict(color='orange')))
    if 'SMA200' in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df['SMA200'],
                                 mode='lines', name='SMA200',
                                 line=dict(color='cyan')))

    fig.add_trace(go.Scatter(x=buy_points.index, y=buy_points['Close'],
                             mode='markers', name='Buy',
                             marker=dict(symbol='triangle-up', size=10, color='green')))
    fig.add_trace(go.Scatter(x=sell_points.index, y=sell_points['Close'],
                             mode='markers', name='Sell',
                             marker=dict(symbol='triangle-down', size=10, color='red')))

    fig.update_layout(template="plotly_dark", title=f"{ticker} Candlestick Chart")
    return json.dumps(fig, cls=PlotlyJSONEncoder)

# ------------------------------------------------------------
# 6. Flask Routes
# ------------------------------------------------------------
@app.route('/')
def index():
    return render_template("index.html")

# Manage Companies
@app.route('/get_companies', methods=['GET'])
def get_companies():
    return jsonify(load_company_list())

@app.route('/add_company', methods=['POST'])
def add_company():
    data = request.json
    company = data.get("company", "").strip().upper()
    if not company:
        return jsonify({"error": "Company ticker is required"}), 400
    companies = load_company_list()
    if company in companies:
        return jsonify({"message": f"{company} already exists in the list"}), 200
    companies.append(company)
    save_company_list(companies)
    return jsonify({"message": f"Added {company} successfully"}), 200

@app.route('/delete_company', methods=['POST'])
def delete_company():
    data = request.json
    company = data.get("company", "").strip().upper()
    if not company:
        return jsonify({"error": "Company ticker is required"}), 400
    companies = load_company_list()
    if company not in companies:
        return jsonify({"message": f"{company} does not exist in the list"}), 404
    companies.remove(company)
    save_company_list(companies)
    return jsonify({"message": f"Deleted {company} successfully"}), 200

# Single-pair
@app.route('/process_signals', methods=['POST'])
def process_signals():
    c = request.json.get("company")
    return jsonify(calculate_signals_for_company(c))

@app.route('/process_all_signals', methods=['POST'])
def process_all_signals():
    comps = load_company_list()
    results = [calculate_signals_for_company(x) for x in comps]
    return jsonify(results)

# Predict signals (Buy/Sell/Hold)
@app.route('/predict_all_signals', methods=['POST'])
def predict_all_signals():
    comps = load_company_list()
    from concurrent.futures import ThreadPoolExecutor

    def check_signal(tkr):
        df = fetch_stock_data_safe(tkr)
        if df is None or df.empty:
            return {"company": tkr, "signal": "No data", "price": "N/A"}
        df = calculate_sma(df)
        last_row = df.iloc[-1]
        closep = round(last_row['Close'], 2)
        if last_row.get('BuySignal'):
            sg = "Buy"
        elif last_row.get('SellSignal'):
            sg = "Sell"
        else:
            sg = "Hold"
        return {"company": tkr, "signal": sg, "price": closep}

    with ThreadPoolExecutor() as executor:
        results = list(executor.map(check_signal, comps))

    def sig_order(item):
        s = item['signal']
        if s == 'Buy': return 1
        elif s == 'Sell': return 2
        elif s == 'Hold': return 3
        return 999
    results.sort(key=sig_order)
    return jsonify(results)

@app.route('/generate_graph', methods=['POST'])
def generate_graph():
    data = request.json
    c = data.get("company")
    if not c:
        return jsonify({"error": "Company ticker is required"}), 400
    df = fetch_stock_data_safe(c)
    if df is None or df.empty:
        return jsonify({"error": f"No data found for {c}"}), 404
    df = calculate_sma(df)
    buy_pts = df[df['BuySignal']]
    sell_pts = df[df['SellSignal']]
    j = plot_graph(df, c, buy_pts, sell_pts)
    return jsonify({"data": json.loads(j)})

# -------------------------------------------------------------------
# 7. Portfolio Endpoints
# -------------------------------------------------------------------
@app.route('/portfolio_add', methods=['POST'])
def portfolio_add():
    """
    Expects JSON:
    {
      "ticker": "AAPL",
      "buy_price": 150,
      "quantity": 10,
      "buy_date": "2024-01-10"  // optional
    }
    If buy_date not provided, defaults to today's date
    """
    data = request.json
    tkr = data.get("ticker", "").upper().strip()
    bp = data.get("buy_price")
    qty = data.get("quantity", 1)
    buy_date = data.get("buy_date")

    if not tkr or bp is None:
        return jsonify({"error": "ticker and buy_price are required"}), 400

    if not buy_date:
        buy_date = datetime.now().strftime("%Y-%m-%d")

    port = load_portfolio()
    new_pos = {
        "ticker": tkr,
        "buy_price": float(bp),
        "quantity": float(qty),
        "buy_date": buy_date
    }
    port.append(new_pos)
    save_portfolio(port)
    return jsonify({"message": f"Added {tkr} to portfolio successfully"}), 200

@app.route('/portfolio_list', methods=['GET'])
def portfolio_list():
    """
    Returns open positions from portfolio.json
    """
    return jsonify(load_portfolio())

@app.route('/predict_portfolio_signals', methods=['POST'])
def predict_portfolio_signals():
    """
    For each open position, fetch last row data, check Buy/Sell/Hold,
    compute profit_percent, etc.
    """
    port = load_portfolio()

    def process_pos(pos):
        tkr = pos['ticker']
        bp = pos['buy_price']
        qty = pos.get('quantity', 1)

        df = fetch_stock_data_safe(tkr)
        if df is None or df.empty:
            return {
                "ticker": tkr, "buy_price": bp, "quantity": qty,
                "current_price": "N/A", "signal": "No data", "profit_percent": "N/A"
            }
        df = calculate_sma(df)
        last_row = df.iloc[-1]
        currp = round(last_row['Close'], 2)

        if last_row.get('BuySignal'):
            sg = "Buy"
        elif last_row.get('SellSignal'):
            sg = "Sell"
        else:
            sg = "Hold"

        profit_percent = round(((currp - bp) / bp) * 100, 2)
        return {
            "ticker": tkr,
            "buy_price": bp,
            "quantity": qty,
            "current_price": currp,
            "signal": sg,
            "profit_percent": profit_percent
        }

    with ThreadPoolExecutor() as executor:
        results = list(executor.map(process_pos, port))

    return jsonify(results)

@app.route('/portfolio_sell', methods=['POST'])
def portfolio_sell():
    """
    Expects JSON:
    {
      "ticker": "AAPL",
      "sell_price": 160.25,   // optional
      "sell_date": "2024-05-01"  // optional
    }
    Moves the first matching open position to archive with final details.
    """
    data = request.json
    tkr = data.get("ticker", "").upper().strip()
    user_sell_price = data.get("sell_price")
    user_sell_date = data.get("sell_date")

    if not tkr:
        return jsonify({"error": "ticker is required"}), 400

    port = load_portfolio()
    sold_item = None
    remaining = []
    for pos in port:
        if pos['ticker'] == tkr and sold_item is None:
            sold_item = pos
        else:
            remaining.append(pos)

    if not sold_item:
        return jsonify({"message": f"No open position found for {tkr}"}), 404

    # If user didn't specify sell_price, fallback to last close
    final_sell_price = None
    if user_sell_price is not None:
        final_sell_price = float(user_sell_price)
    else:
        df = fetch_stock_data_safe(tkr)
        if df is not None and not df.empty:
            final_sell_price = round(df.iloc[-1]['Close'], 2)

    # Sell date default to today if not provided
    if user_sell_date:
        final_sell_date = user_sell_date.strip()
    else:
        final_sell_date = datetime.now().strftime("%Y-%m-%d")

    buy_price = sold_item['buy_price']
    quantity = sold_item.get('quantity', 1)
    buy_date_str = sold_item.get('buy_date')  # string e.g. "2024-01-10"

    # Compute profit if we have final_sell_price
    profit_percent = "N/A"
    profit_value = "N/A"
    if final_sell_price is not None:
        diff = final_sell_price - buy_price
        profit_value = round(diff * quantity, 2)  # absolute profit in currency
        profit_percent = round((diff / buy_price) * 100, 2)

    # Compute duration of hold if we have buy_date
    duration_days = "N/A"
    if buy_date_str:
        try:
            buy_dt = datetime.strptime(buy_date_str, "%Y-%m-%d")
            sell_dt = datetime.strptime(final_sell_date, "%Y-%m-%d")
            duration_days = (sell_dt - buy_dt).days
        except:
            pass

    # Update open portfolio
    save_portfolio(remaining)

    # Add to archive
    archived = load_archive()
    archived.append({
        "ticker": tkr,
        "buy_price": buy_price,
        "quantity": quantity,
        "buy_date": buy_date_str,
        "sell_price": final_sell_price if final_sell_price is not None else "N/A",
        "sell_date": final_sell_date,
        "profit_value": profit_value,
        "profit_percent": profit_percent,
        "duration_days": duration_days
    })
    save_archive(archived)

    return jsonify({
        "message": f"Sold {tkr} from portfolio",
        "sell_price": final_sell_price,
        "sell_date": final_sell_date,
        "profit_value": profit_value,
        "profit_percent": profit_percent,
        "duration_days": duration_days
    }), 200

@app.route('/portfolio_archive', methods=['GET'])
def portfolio_archive():
    return jsonify(load_archive())


@app.route('/load_zerodha_portfolio', methods=['GET'])
def load_zerodha_portfolio():
    try:
        # Fetch positions from Zerodha
        access_token = "<your_access_token>"  # Store securely in production
        kite.set_access_token(access_token)
        positions = kite.positions()

        portfolio = []
        for pos in positions['net']:
            portfolio.append({
                "ticker": pos['tradingsymbol'],
                "buy_price": pos['average_price'],
                "quantity": pos['quantity'],
                "current_price": pos['last_price'],
                "profit_percent": round(((pos['last_price'] - pos['average_price']) / pos['average_price']) * 100, 2),
            })

        return jsonify(portfolio)
    except Exception as e:
        print(f"Error loading Zerodha portfolio: {e}")
        return jsonify({"error": "Failed to load Zerodha portfolio"}), 500






# Run
if __name__ == "__main__":
    app.run(debug=True)
