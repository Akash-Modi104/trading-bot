/* global React, AT */
const { useState, useEffect, useRef, useMemo } = React;
const { inr, num, pct, sign, cls, sparkPath, buildEquityCurve } = AT;

/* ─────────── Icons (inline, minimal) ─────────── */
const Icon = ({ name, size = 16 }) => {
  const s = { width: size, height: size, fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' };
  const paths = {
    dash:    <><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></>,
    chart:   <><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 6-7"/></>,
    list:    <><path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/></>,
    cog:     <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></>,
    file:    <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h6"/></>,
    pause:   <><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></>,
    play:    <><path d="M6 4l14 8-14 8z"/></>,
    refresh: <><path d="M3 12a9 9 0 0 1 15.5-6.3L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.3L3 16"/><path d="M3 21v-5h5"/></>,
    search:  <><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></>,
    bell:    <><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10 21a2 2 0 0 0 4 0"/></>,
    plus:    <><path d="M12 5v14M5 12h14"/></>,
    arrow:   <><path d="M5 12h14M13 6l6 6-6 6"/></>,
    bot:     <><rect x="4" y="7" width="16" height="12" rx="2"/><path d="M12 7V3M8 11h.01M16 11h.01M9 16h6"/></>,
    portfolio: <><path d="M3 7h18v12H3z"/><path d="M3 11h18M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></>,
    target:  <><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/></>,
  };
  return <svg viewBox="0 0 24 24" style={s}>{paths[name] || null}</svg>;
};

/* ─────────── Sidebar ─────────── */
function Sidebar({ tab, onTab }) {
  const items = [
    { id: 'overview',  label: 'Overview',  icon: 'dash' },
    { id: 'positions', label: 'Positions', icon: 'portfolio' },
    { id: 'trades',    label: 'Trades',    icon: 'list' },
    { id: 'strategy',  label: 'Strategy',  icon: 'cog' },
    { id: 'reports',   label: 'Reports',   icon: 'file' },
  ];
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">A</div>
        <div className="brand-text">
          <div className="t1">AlgoTrader</div>
          <div className="t2">NSE · Paper</div>
        </div>
      </div>
      {items.map(it => (
        <button key={it.id} className={cls('nav-item', tab === it.id && 'active')} onClick={() => onTab(it.id)}>
          <span className="nav-rail" />
          <Icon name={it.icon} size={16} />
          <span>{it.label}</span>
        </button>
      ))}
      <div className="nav-section">Status</div>
      <div className="sidebar-status" style={{ padding: '4px 12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        <Row k="Broker" v={<><span className="dot up pulse" /> Alpaca</>} />
        <Row k="Region" v="NSE · IST" />
        <Row k="Mode"   v={<span className="pill accent" style={{ padding: '2px 6px' }}>PAPER</span>} />
        <Row k="Build"  v={<span className="mono" style={{ color: 'var(--ink-3)' }}>v2.1.4</span>} />
      </div>
    </aside>
  );
}
const Row = ({ k, v }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 12 }}>
    <span style={{ color: 'var(--ink-3)' }}>{k}</span>
    <span style={{ color: 'var(--ink-2)', display: 'flex', alignItems: 'center', gap: 6 }}>{v}</span>
  </div>
);

/* ─────────── Account switcher ─────────── */
const ACCOUNTS = [
  { id: 'paper-1', name: 'Paper · Alpaca',   broker: 'Alpaca',   mode: 'PAPER', equity: 104435.50, mask: '••42', initials: 'PA' },
  { id: 'live-1',  name: 'Live · Zerodha',   broker: 'Zerodha',  mode: 'LIVE',  equity: 287610.20, mask: '••87', initials: 'ZL' },
  { id: 'live-2',  name: 'Live · Upstox F&O',broker: 'Upstox',   mode: 'LIVE',  equity:  62880.00, mask: '••13', initials: 'UF' },
  { id: 'sim',     name: 'Backtest · Sim',   broker: 'Local',    mode: 'SIM',   equity: 100000.00, mask: '••00', initials: 'BT' },
];

function ProfileMenu({ account, onPick }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    const onDoc = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('touchstart', onDoc);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('touchstart', onDoc);
    };
  }, []);
  const modePill = m => m === 'LIVE' ? 'down' : m === 'PAPER' ? 'accent' : 'warn';
  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button className="btn btn-sm" onClick={() => setOpen(o => !o)}
        style={{ paddingLeft: 5, gap: 8 }}>
        <span style={{
          width: 24, height: 24, borderRadius: 6,
          background: 'linear-gradient(135deg, var(--accent), var(--up))',
          color: 'oklch(0.16 0.012 250)', fontWeight: 700, fontSize: 11,
          display: 'grid', placeItems: 'center', fontFamily: 'var(--font-mono)',
        }}>{account.initials}</span>
        <span className="hide-sm" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', lineHeight: 1.1 }}>
          <span style={{ fontSize: 12, fontWeight: 600 }}>{account.broker}</span>
          <span style={{ fontSize: 10, color: 'var(--ink-3)', letterSpacing: '0.06em' }}>{account.mask} · {account.mode}</span>
        </span>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ marginLeft: 2, pointerEvents: 'none' }}><path d="M6 9l6 6 6-6"/></svg>
      </button>
      {open && (
        <div className="profile-menu-dropdown" style={{
          position: 'absolute', top: 'calc(100% + 8px)', right: 0,
          width: 320, background: 'var(--bg-1)',
          border: '1px solid var(--line)', borderRadius: 'var(--r-lg)',
          boxShadow: '0 12px 40px rgba(0,0,0,0.4)', zIndex: 60, overflow: 'hidden',
        }}>
          <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--line-soft)' }}>
            <div className="eyebrow">Trading Account</div>
          </div>
          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {ACCOUNTS.map(a => {
              const active = a.id === account.id;
              return (
                <button key={a.id} onClick={() => { onPick(a); setOpen(false); }}
                  style={{
                    width: '100%', display: 'flex', alignItems: 'center', gap: 10,
                    padding: '10px 14px', textAlign: 'left',
                    background: active ? 'var(--bg-2)' : 'transparent',
                  }}
                  onMouseEnter={e => { if (!active) e.currentTarget.style.background = 'var(--bg-2)'; }}
                  onMouseLeave={e => { if (!active) e.currentTarget.style.background = 'transparent'; }}>
                  <span style={{
                    width: 30, height: 30, borderRadius: 7,
                    background: 'var(--bg-3)', border: '1px solid var(--line-soft)',
                    display: 'grid', placeItems: 'center',
                    fontFamily: 'var(--font-mono)', fontWeight: 700, fontSize: 11, color: 'var(--ink-2)',
                  }}>{a.initials}</span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, fontWeight: 600 }}>
                      {a.broker}
                      <span className={`pill ${modePill(a.mode)}`} style={{ padding: '1px 6px', fontSize: 9 }}>{a.mode}</span>
                    </div>
                    <div className="mono" style={{ fontSize: 11, color: 'var(--ink-3)', marginTop: 1 }}>
                      {a.mask} · {inr(a.equity, 0)}
                    </div>
                  </div>
                  {active && (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12l5 5L20 7"/></svg>
                  )}
                </button>
              );
            })}
          </div>
          <div style={{ borderTop: '1px solid var(--line-soft)', padding: 8, display: 'flex', flexDirection: 'column' }}>
            <button className="btn btn-sm ghost" style={{ justifyContent: 'flex-start' }}>
              <Icon name="plus" size={13} /> Connect new broker
            </button>
            <button className="btn btn-sm ghost" style={{ justifyContent: 'flex-start' }}>
              <Icon name="cog" size={13} /> Account settings
            </button>
            <button className="btn btn-sm ghost" style={{ justifyContent: 'flex-start', color: 'var(--down)' }}>
              <Icon name="arrow" size={13} /> Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ─────────── Top bar ─────────── */
function TopBar({ equity, dayPnl, dayPct, paused, onPause, account, onAccount }) {
  const [time, setTime] = useState(() => fmtIST());
  useEffect(() => {
    const id = setInterval(() => setTime(fmtIST()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="topbar">
      <div className="search hide-on-tight">
        <Icon name="search" size={14} />
        <input placeholder="Search ticker · NIFTY 50" />
        <span className="mono" style={{ fontSize: 11, color: 'var(--ink-4)' }}>⌘K</span>
      </div>
      <div className="grow" />
      <div className="topbar-stats">
        <div className="topbar-stat hide-sm hide-md">
          <div className="lbl">Market</div>
          <div className="val" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span className="dot up pulse" /> OPEN
          </div>
        </div>
        <div className="topbar-stat hide-sm hide-md">
          <div className="lbl">IST</div>
          <div className="val mono">{time}</div>
        </div>
        <div className="topbar-stat">
          <div className="lbl">Equity</div>
          <div className="val mono">{inr(equity, 0)}</div>
        </div>
        <div className="topbar-stat">
          <div className="lbl">Day P&amp;L</div>
          <div className={cls('val mono', dayPnl >= 0 ? 'up' : 'down')}>
            {sign(dayPnl)}{inr(Math.abs(dayPnl), 0).replace('₹','₹ ')} <span style={{ fontSize: 12, opacity: 0.85 }}>({pct(dayPct)})</span>
          </div>
        </div>
        <button className={cls('btn btn-sm', paused ? 'success' : 'danger')} onClick={onPause} style={{ marginLeft: 6 }}>
          <Icon name={paused ? 'play' : 'pause'} size={13} />
          {paused ? 'Resume' : 'Pause'}
        </button>
        <ProfileMenu account={account} onPick={onAccount} />
      </div>
    </div>
  );
}
function fmtIST() {
  const d = new Date();
  return d.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false }) + ' IST';
}

/* ─────────── KPI cards ─────────── */
function Kpis({ equity, equityCurve, dayPnl, dayPct, openPositions, winRate, exposure, budget }) {
  const W = 80, H = 30;
  return (
    <div className="kpis">
      <div className="kpi">
        <div className="lbl">Equity</div>
        <div className="val">{inr(equity, 0)}</div>
        <div className="sub"><span className={dayPct >= 0 ? 'up' : 'down'}>{pct(dayPct)} today</span></div>
        <div className="spark-wrap">
          <svg className="spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
            <path d={sparkPath(equityCurve, W, H)} stroke={dayPct >= 0 ? 'var(--up)' : 'var(--down)'} strokeWidth="1.5" fill="none" />
          </svg>
        </div>
      </div>
      <div className="kpi">
        <div className="lbl">Day P&amp;L</div>
        <div className={cls('val', dayPnl >= 0 ? 'up' : 'down')}>{sign(dayPnl)}{inr(Math.abs(dayPnl), 2)}</div>
        <div className="sub">vs target <span className="mono">+₹2,000</span></div>
        <div style={{ marginTop: 8 }}>
          <div className={cls('progress', dayPnl >= 0 ? 'up' : 'down')}>
            <span style={{ width: `${Math.min(100, Math.max(2, (dayPnl / 2000) * 100))}%` }} />
          </div>
        </div>
      </div>
      <div className="kpi">
        <div className="lbl">Open Positions</div>
        <div className="val">{openPositions}<span style={{ color: 'var(--ink-4)', fontSize: 18 }}> / 3</span></div>
        <div className="sub">Exposure <span className="mono">{inr(exposure, 0)}</span></div>
        <div style={{ marginTop: 8 }}>
          <div className="progress"><span style={{ width: `${(openPositions / 3) * 100}%` }} /></div>
        </div>
      </div>
      <div className="kpi">
        <div className="lbl">Win Rate · 30d</div>
        <div className="val">{winRate.toFixed(1)}<span style={{ color: 'var(--ink-4)', fontSize: 18 }}>%</span></div>
        <div className="sub">52 trades · avg <span className="mono up">+1.8%</span></div>
        <div style={{ marginTop: 8 }}>
          <div className="progress up"><span style={{ width: `${winRate}%` }} /></div>
        </div>
      </div>
    </div>
  );
}

/* ─────────── Equity chart (custom svg) ─────────── */
function EquityChart({ curve, dayPct }) {
  const [range, setRange] = useState('1D');
  const ref = useRef(null);
  const [size, setSize] = useState({ w: 800, h: 260 });
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      setSize({ w: r.width, h: r.height });
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  const data = useMemo(() => {
    const factor = { '1D': 1, '5D': 2, '1M': 4, '3M': 6, 'ALL': 10 }[range] || 1;
    return buildEquityCurve(curve[0] || 100000, curve.length * factor, 0.0006, 0.0028);
  }, [range, curve]);

  const W = size.w || 800, H = size.h || 260;
  const pad = { l: 8, r: 8, t: 14, b: 22 };
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  const xAt = i => pad.l + (i / (data.length - 1)) * (W - pad.l - pad.r);
  const yAt = v => pad.t + (1 - (v - min) / span) * (H - pad.t - pad.b);
  const path = data.map((v, i) => `${i === 0 ? 'M' : 'L'}${xAt(i).toFixed(2)},${yAt(v).toFixed(2)}`).join(' ');
  const fill = `${path} L${xAt(data.length - 1)},${H - pad.b} L${xAt(0)},${H - pad.b} Z`;
  const last = data[data.length - 1];
  const stroke = dayPct >= 0 ? 'var(--up)' : 'var(--down)';
  const fillId = `fill-${range}`;

  return (
    <div className="card">
      <div className="chart-h">
        <div>
          <div className="eyebrow">Equity Curve</div>
          <div className="big">{inr(last, 0)}</div>
        </div>
        <div className="range-toggle">
          {['1D','5D','1M','3M','ALL'].map(r => (
            <button key={r} className={range === r ? 'active' : ''} onClick={() => setRange(r)}>{r}</button>
          ))}
        </div>
      </div>
      <div ref={ref} style={{ height: 260, padding: '0 12px 12px' }}>
        <svg width="100%" height="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
          <defs>
            <linearGradient id={fillId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%"   stopColor={stroke} stopOpacity="0.35" />
              <stop offset="100%" stopColor={stroke} stopOpacity="0" />
            </linearGradient>
          </defs>
          {[0.25, 0.5, 0.75].map(t => (
            <line key={t} x1={pad.l} x2={W - pad.r} y1={pad.t + t * (H - pad.t - pad.b)} y2={pad.t + t * (H - pad.t - pad.b)}
                  stroke="oklch(0.26 0.014 250)" strokeDasharray="2 4" />
          ))}
          <path d={fill} fill={`url(#${fillId})`} />
          <path d={path} fill="none" stroke={stroke} strokeWidth="2" />
          <circle cx={xAt(data.length - 1)} cy={yAt(last)} r="3.5" fill={stroke} />
          <circle cx={xAt(data.length - 1)} cy={yAt(last)} r="6" fill={stroke} fillOpacity="0.2" />
        </svg>
      </div>
    </div>
  );
}

/* ─────────── Positions ─────────── */
function PositionsCard({ positions }) {
  return (
    <div className="card">
      <div className="card-h">
        <h3>Open Positions <span style={{ color: 'var(--ink-4)', fontWeight: 500, marginLeft: 6 }}>· {positions.length}</span></h3>
        <button className="btn btn-sm ghost"><Icon name="refresh" size={12} /> Sync</button>
      </div>
      <div className="card-b flush">
        {positions.map(p => {
          const pl = (p.ltp - p.buy) * p.qty;
          const plPct = ((p.ltp - p.buy) / p.buy) * 100;
          const up = pl >= 0;
          return (
            <div key={p.ticker} className="pos-row">
              <div className="ticker">
                <div className="tick-glyph">{p.ticker.slice(0, 2)}</div>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>{p.ticker}<span style={{ color: 'var(--ink-4)', fontWeight: 500, fontSize: 11, marginLeft: 6 }}>.{p.exch}</span></div>
                  <div style={{ fontSize: 12, color: 'var(--ink-3)', marginTop: 2 }}>
                    <span className="mono">{p.qty}</span> @ <span className="mono">{inr(p.buy)}</span>
                    <span style={{ margin: '0 8px', color: 'var(--ink-4)' }}>·</span>
                    entry {p.entry}
                  </div>
                </div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div className="num" style={{ fontSize: 14, fontWeight: 600 }}>{inr(p.ltp)}</div>
                <div className={cls('num', up ? 'up' : 'down')} style={{ fontSize: 12, marginTop: 2 }}>
                  {sign(pl)}{inr(Math.abs(pl))} <span style={{ opacity: 0.8 }}>({pct(plPct)})</span>
                </div>
              </div>
            </div>
          );
        })}
        {positions.length === 0 && (
          <div style={{ padding: 32, textAlign: 'center', color: 'var(--ink-3)', fontSize: 13 }}>No open positions</div>
        )}
      </div>
    </div>
  );
}

window.ATC1 = { Icon, Sidebar, TopBar, Kpis, EquityChart, PositionsCard };
