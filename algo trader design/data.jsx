/* global React */
const { useState, useEffect, useRef, useMemo } = React;

/* ─────────── Helpers ─────────── */
const inr = (n, dp = 2) => {
  if (n == null || isNaN(n)) return '—';
  return '₹' + Number(n).toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp });
};
const num = (n, dp = 2) => {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp });
};
const sign = n => (n > 0 ? '+' : n < 0 ? '−' : '');
const pct = (n, dp = 2) => (n == null ? '—' : `${sign(n)}${Math.abs(n).toFixed(dp)}%`);
const cls = (...xs) => xs.filter(Boolean).join(' ');

/* deterministic pseudo-random — keeps demo reproducible across reloads */
const seed = (k) => {
  let h = 2166136261;
  for (let i = 0; i < k.length; i++) { h ^= k.charCodeAt(i); h = Math.imul(h, 16777619); }
  return () => { h ^= h << 13; h ^= h >>> 7; h ^= h << 17; return ((h >>> 0) % 10000) / 10000; };
};

/* Generate a smooth equity curve */
const buildEquityCurve = (start, points, biasUp = 0.0006, vol = 0.0025) => {
  const r = seed('eq-' + start);
  const arr = [start];
  for (let i = 1; i < points; i++) {
    const drift = biasUp + (r() - 0.5) * vol;
    arr.push(arr[i - 1] * (1 + drift));
  }
  return arr;
};

/* Sparkline svg path */
const sparkPath = (vals, w, h, pad = 1) => {
  if (!vals?.length) return '';
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  return vals.map((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / span) * (h - pad * 2);
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
};

/* ─────────── Mock data — based on real strategy_params + portfolio ─────────── */
const STRATEGY = {
  ema_fast: 9, ema_slow: 21,
  rsi_period: 14, rsi_buy_min: 50, rsi_buy_max: 70,
  atr_period: 14, atr_multiplier: 1.5,
  rel_vol_min: 1.8,
  stop_loss_pct: 1.5, take_profit_pct: 3.0,
  bar_timeframe: '5Min',
  max_positions: 3, budget_per_trade: 333,
  trade_start_hour: 10, trade_start_min: 0,
  trade_end_hour: 14, trade_end_min: 0,
  close_hour: 15, close_min: 45,
  vwap_required: true, orb_required: true, multi_tf_required: true,
  min_confidence: 60,
};

const POSITIONS_SEED = [
  { ticker: 'MARICO',   exch: 'NSE', qty: 89, buy: 89.00,  ltp: 91.45,  conf: 78, status: 'open',   entry: '10:14' },
  { ticker: 'TATAMOTORS', exch: 'NSE', qty: 12, buy: 982.30, ltp: 1004.20, conf: 84, status: 'open', entry: '10:42' },
  { ticker: 'INFY',     exch: 'NSE', qty: 8,  buy: 1782.50,ltp: 1771.10, conf: 71, status: 'open',   entry: '11:18' },
];

const PICKS_SEED = [
  { ticker: 'HDFCBANK',  conf: 88, signal: 'EMA cross + VWAP+', rsi: 62.4, vol: '2.1×' },
  { ticker: 'RELIANCE',  conf: 81, signal: 'ORB break + VWAP+',  rsi: 58.8, vol: '2.4×' },
  { ticker: 'BAJFINANCE',conf: 76, signal: 'Multi-TF align',     rsi: 55.1, vol: '1.9×' },
  { ticker: 'WIPRO',     conf: 68, signal: 'EMA cross',          rsi: 52.7, vol: '1.8×' },
  { ticker: 'ITC',       conf: 64, signal: 'ORB break',          rsi: 51.0, vol: '1.8×' },
];

const TRADES_SEED = [
  { t: '11:42', side: 'BUY',  ticker: 'INFY',       qty: 8,  px: 1782.50, pnl: null,    note: 'EMA9>EMA21, RSI 61, VWAP+' },
  { t: '11:18', side: 'SELL', ticker: 'SBIN',       qty: 22, px: 812.40,  pnl: +186.20, note: 'TP hit (+3.0%)' },
  { t: '10:42', side: 'BUY',  ticker: 'TATAMOTORS', qty: 12, px: 982.30,  pnl: null,    note: 'ORB break, vol 2.4×' },
  { t: '10:31', side: 'SELL', ticker: 'AXISBANK',   qty: 14, px: 1142.10, pnl: -68.50,  note: 'SL hit (−1.5%)' },
  { t: '10:14', side: 'BUY',  ticker: 'MARICO',     qty: 89, px: 89.00,   pnl: null,    note: 'Multi-TF align' },
  { t: '09:58', side: 'SELL', ticker: 'HCLTECH',    qty: 11, px: 1496.30, pnl: +212.40, note: 'TP hit (+3.0%)' },
];

const ACTIVITY_SEED = [
  { t: '11:48:22', tag: 'scan', msg: 'Scanned 47 symbols · 5 candidates · top: HDFCBANK 88' },
  { t: '11:42:08', tag: 'buy',  msg: 'Filled INFY ×8 @ ₹1782.50 · slip 0.04%' },
  { t: '11:18:51', tag: 'sell', msg: 'TP hit SBIN ×22 @ ₹812.40 · +₹186.20 (+3.02%)' },
  { t: '11:00:00', tag: 'info', msg: 'Regime check: TRENDING — confidence floor 60' },
  { t: '10:42:31', tag: 'buy',  msg: 'Filled TATAMOTORS ×12 @ ₹982.30 · slip 0.02%' },
  { t: '10:31:14', tag: 'sell', msg: 'SL hit AXISBANK ×14 @ ₹1142.10 · −₹68.50 (−1.50%)' },
  { t: '10:14:02', tag: 'buy',  msg: 'Filled MARICO ×89 @ ₹89.00 · entry signal multi-TF' },
  { t: '10:00:00', tag: 'info', msg: 'Trading window opened · ORB locked · VWAP active' },
  { t: '09:58:44', tag: 'sell', msg: 'TP hit HCLTECH ×11 @ ₹1496.30 · +₹212.40 (+3.01%)' },
  { t: '09:30:01', tag: 'info', msg: 'Market open · loaded 47 symbols · paper mode' },
  { t: '09:15:00', tag: 'warn', msg: 'Pre-market: 2 negative-news symbols filtered (YES, IDEA)' },
];

const REPORTS_SEED = [
  { date: '2026-05-05', trades: 6, win: 4, loss: 2, pnl: +1284.60, roi: +1.28 },
  { date: '2026-05-02', trades: 4, win: 3, loss: 1, pnl: +642.10,  roi: +0.64 },
  { date: '2026-05-01', trades: 5, win: 2, loss: 3, pnl: -218.40,  roi: -0.22 },
  { date: '2026-04-30', trades: 7, win: 5, loss: 2, pnl: +1860.30, roi: +1.86 },
  { date: '2026-04-29', trades: 3, win: 2, loss: 1, pnl: +312.50,  roi: +0.31 },
];

window.AT = {
  inr, num, pct, sign, cls,
  sparkPath, buildEquityCurve, seed,
  STRATEGY, POSITIONS_SEED, PICKS_SEED, TRADES_SEED, ACTIVITY_SEED, REPORTS_SEED,
};
