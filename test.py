import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from scipy.optimize import newton

# Constants
INITIAL_BALANCE = 100000
MAX_INVESTMENT_PER_STOCK = 10000
START_DATE = datetime(2024, 1, 1)  # Default start date
STOCK_LIST = [
    "MARICO.NS", "DABUR.NS", "HAVELLS.NS", "WHIRLPOOL.NS", "INFY.NS", "TITAN.NS", "TCS.NS",
    "PFIZER.NS", "GILLETTE.NS", "SANOFI.NS", "PGHH.NS", "HDFCBANK.NS", "AXISBANK.NS", "KOTAKBANK.NS",
    "ICICIGI.NS", "BAJFINANCE.NS", "HDFCLIFE.NS", "HDFCAMC.NS", "COLPAL.NS", "HINDUNILVR.NS",
    "NAM-INDIA.NS", "BAJAJHLDNG.NS", "NESTLEIND.NS", "ASIANPAINT.NS", "BAJAJ-AUTO.NS", "BERGEPAINT.NS",
    "BATAINDIA.NS", "PAGEIND.NS", "PIDILITIND.NS", "ABBOTINDIA.NS", "HCLTECH.NS", "GLAXO.NS",
    "AKZOINDIA.NS", "ICICIPRULI.NS", "ITC.NS"
]

# SMA Calculation
def calculate_sma(df):
    if 'Close' in df.columns and len(df) >= 200:
        df['SMA20'] = df['Close'].rolling(window=20).mean()
        df['SMA100'] = df['Close'].rolling(window=100).mean()
        df['SMA200'] = df['Close'].rolling(window=200).mean()
        df['BuySignal'] = (df['Close'] < df['SMA20']) & (df['SMA20'] < df['SMA100']) & (df['SMA100'] < df['SMA200'])
        df['SellSignal'] = (df['Close'] > df['SMA20']) & (df['SMA20'] > df['SMA100']) & (df['SMA100'] > df['SMA200'])
    return df

# XIRR Calculation
def xirr(cash_flows, dates):
    def npv(rate):
        return sum(cf / ((1 + rate) ** ((d - dates[0]).days / 365.0)) for cf, d in zip(cash_flows, dates))
    try:
        return newton(npv, 0.1)
    except:
        return None

# Trading Simulation
def simulate_trading(stock_list, initial_balance, start_date):
    balance = initial_balance
    holdings = {}  # Track active holdings
    transactions = []  # Record all buy/sell actions
    cash_flows = [-initial_balance]  # Initial outflow
    dates = [start_date]
    realized_pnl = 0  # Track realized profits/losses

    # Fetch data for all stocks
    stock_data = {}
    fetch_start_date = (start_date - timedelta(days=200)).strftime("%Y-%m-%d")
    fetch_end_date = datetime.now().strftime("%Y-%m-%d")

    for stock in stock_list:
        print(f"Fetching data for {stock}...")
        try:
            data = yf.download(stock, start=fetch_start_date, end=fetch_end_date)
            stock_data[stock] = calculate_sma(data)
        except Exception as e:
            print(f"Error fetching data for {stock}: {e}")

    # Create a date range
    date_range = pd.date_range(start=start_date.strftime("%Y-%m-%d"), end=datetime.now().strftime("%Y-%m-%d"))

    # Evaluate all stocks daily
    for date in date_range:
        # Recalculate portfolio worth based on current prices
        current_portfolio_worth = sum(
            shares * stock_data[stock].loc[date, 'Close']
            for stock, (shares, _, _) in holdings.items()
            if date in stock_data[stock].index
        )
        for stock, data in stock_data.items():
            if date in data.index:
                row = data.loc[date]

                # Check for buy signal
                if row['BuySignal'] and stock not in holdings and balance >= MAX_INVESTMENT_PER_STOCK:
                    shares_bought = MAX_INVESTMENT_PER_STOCK / row['Close']
                    balance -= MAX_INVESTMENT_PER_STOCK
                    holdings[stock] = (shares_bought, row['Close'], date)
                    current_portfolio_worth = sum(
                        shares * stock_data[s].loc[date, 'Close']
                        for s, (shares, _, _) in holdings.items()
                        if date in stock_data[s].index
                    )
                    transactions.append((date, stock, "BUY", row['Close'], MAX_INVESTMENT_PER_STOCK, balance, date, None, current_portfolio_worth))

                # Check for sell signal
                elif row['SellSignal'] and stock in holdings:
                    shares_bought, buy_price, buy_date = holdings[stock]
                    sell_value = shares_bought * row['Close']
                    profit = sell_value - (shares_bought * buy_price)
                    realized_pnl += profit
                    balance += sell_value
                    del holdings[stock]
                    # Recalculate portfolio worth after selling
                    current_portfolio_worth = sum(
                        shares * stock_data[s].loc[date, 'Close']
                        for s, (shares, _, _) in holdings.items()
                        if date in stock_data[s].index
                    )
                    transactions.append((buy_date, stock, "SELL", row['Close'], sell_value, balance, buy_date, date, current_portfolio_worth))

    # Calculate current portfolio value
    current_value_of_investments = sum(
        shares * yf.download(stock, start=datetime.now() - timedelta(days=1), end=datetime.now().strftime("%Y-%m-%d"))['Close'].iloc[-1]
        for stock, (shares, _, _) in holdings.items()
    )

    # Add portfolio value for XIRR
    cash_flows.append(balance + current_value_of_investments)
    dates.append(datetime.now())

    return balance, current_value_of_investments, realized_pnl, transactions, cash_flows, dates

# Run Simulation
final_balance, current_value_of_investments, realized_pnl, transactions, cash_flows, dates = simulate_trading(STOCK_LIST, INITIAL_BALANCE, START_DATE)

# KPIs
available_cash_balance = final_balance
present_portfolio_value = current_value_of_investments
xirr_value = xirr(cash_flows, dates)

total_current_value = final_balance + current_value_of_investments

kpi_data = {
    "Available Cash Balance": available_cash_balance,
    "Present Portfolio Value": present_portfolio_value,
    "XIRR (%)": xirr_value * 100 if xirr_value else "N/A"
}

# Save Results
current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
file_name = f"stock_analysis_{current_time}.xlsx"

transactions_df = pd.DataFrame(transactions, columns=["Buy Date", "Stock", "Action", "Price", "Amount", "Available Balance", "Buy Date", "Sell Date", "Current Portfolio Worth"])
kpi_df = pd.DataFrame([kpi_data])

with pd.ExcelWriter(file_name) as writer:
    kpi_df.to_excel(writer, sheet_name="KPI", index=False)
    transactions_df.to_excel(writer, sheet_name="Transactions", index=False)

print(f"Simulation complete. Results saved to {file_name}.")
