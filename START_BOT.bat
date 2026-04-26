@echo off
title Advanced Intraday Trading Bot v2
color 0A
echo ============================================================
echo   ADVANCED INTRADAY TRADING BOT v2
echo   Powered by Claude AI + Alpaca Paper Trading
echo ============================================================
echo.
echo Starting Live Dashboard on http://localhost:5001 ...
start "Trading Dashboard" /min cmd /c "cd /d "C:\Users\devel\OneDrive\Desktop\stock market" && venv\Scripts\python.exe dashboard.py"
timeout /t 2 /nobreak >nul

echo Starting Trading Bot...
echo.
echo Features active:
echo   * VWAP + ORB + ATR stops + Relative Volume
echo   * Multi-Timeframe + MACD + Bollinger Bands
echo   * Market Regime Detection
echo   * SPY trend filter + VIX fear filter
echo   * Daily loss limit + Trailing stop-loss
echo   * Claude AI stock picks (via scheduled task)
echo   * Telegram alerts (if configured)
echo.
echo Dashboard: http://localhost:5001
echo ============================================================
echo.

cd /d "C:\Users\devel\OneDrive\Desktop\stock market"
call venv\Scripts\activate.bat
python intraday_bot_v2.py

echo.
echo Bot finished. Check reports\ for today's HTML report.
pause
