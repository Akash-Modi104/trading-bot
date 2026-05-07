/* global React, AT, ATC1 */
const { useState, useEffect, useMemo } = React;
const { inr, num, pct, sign, cls } = AT;
const { Icon } = ATC1;

/* ─────────── Picks / Watchlist ─────────── */
function PicksCard({ picks, minConf }) {
  return (
    <div className="card">
      <div className="card-h">
        <h3>Today's Picks <span style={{ color: 'var(--ink-4)', fontWeight: 500, marginLeft: 6 }}>· min conf {minConf}</span></h3>
        <span className="pill accent"><span className="dot" style={{ background: 'currentColor' }} /> SCANNING</span>
      </div>
      <div className="card-b flush">
        <table>
          <thead>
            <tr>
              <th>Ticker</th><th>Signal</th><th>RSI</th><th>Vol</th><th>Conf</th><th></th>
            </tr>
          </thead>
          <tbody>
            {picks.map(p => (
              <tr key={p.ticker}>
                <td data-label="Ticker">
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span className="mono" style={{ fontWeight: 600 }}>{p.ticker}</span>
                  </div>
                </td>
                <td data-label="Signal" style={{ color: 'var(--ink-2)' }}>{p.signal}</td>
                <td data-label="RSI" className="mono">{p.rsi.toFixed(1)}</td>
                <td data-label="Vol" className="mono">{p.vol}</td>
                <td data-label="Conf">
                  <div className="conf">
                    <div className="conf-bar"><span style={{ width: `${p.conf}%`, background: p.conf >= 80 ? 'var(--up)' : p.conf >= 70 ? 'var(--accent)' : 'var(--warn)' }} /></div>
                    <span className="mono" style={{ fontWeight: 600 }}>{p.conf}</span>
                  </div>
                </td>
                <td data-label="">
                  <button className="btn btn-sm">Trade</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ─────────── Trade history ─────────── */
function TradesCard({ trades, full }) {
  const list = full ? trades : trades.slice(0, 6);
  return (
    <div className="card">
      <div className="card-h">
        <h3>Trade History <span style={{ color: 'var(--ink-4)', fontWeight: 500, marginLeft: 6 }}>· today</span></h3>
        {!full && <button className="btn btn-sm ghost">View all <Icon name="arrow" size={12} /></button>}
      </div>
      <div className="card-b flush">
        <table>
          <thead>
            <tr><th>Time</th><th>Side</th><th>Ticker</th><th>Qty</th><th>Price</th><th>P&amp;L</th><th>Note</th></tr>
          </thead>
          <tbody>
            {list.map((t, i) => (
              <tr key={i}>
                <td data-label="Time" className="mono" style={{ color: 'var(--ink-3)' }}>{t.t}</td>
                <td data-label="Side"><span className={cls('pill', t.side === 'BUY' ? 'up' : 'down')} style={{ padding: '2px 7px' }}>{t.side}</span></td>
                <td data-label="Ticker" className="mono" style={{ fontWeight: 600 }}>{t.ticker}</td>
                <td data-label="Qty" className="mono">{t.qty}</td>
                <td data-label="Price" className="mono">{inr(t.px)}</td>
                <td data-label="P&L" className={cls('mono', t.pnl == null ? '' : t.pnl >= 0 ? 'up' : 'down')}>
                  {t.pnl == null ? <span style={{ color: 'var(--ink-4)' }}>open</span> : `${sign(t.pnl)}${inr(Math.abs(t.pnl))}`}
                </td>
                <td data-label="Note" style={{ color: 'var(--ink-3)', fontSize: 12 }}>{t.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ─────────── Activity log ─────────── */
function ActivityCard({ activity }) {
  return (
    <div className="card">
      <div className="card-h">
        <h3>Activity Log</h3>
        <span className="pill"><span className="dot up pulse" /> LIVE</span>
      </div>
      <div className="log">
        {activity.map((a, i) => (
          <div key={i} className="log-row">
            <span className="log-time">{a.t}</span>
            <span className={cls('log-tag', a.tag)}>{a.tag}</span>
            <span className="log-msg">{a.msg}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ─────────── Strategy editor ─────────── */
function StrategyCard({ params, onChange, compact }) {
  const fields = [
    { k: 'ema_fast', label: 'EMA Fast', step: 1 },
    { k: 'ema_slow', label: 'EMA Slow', step: 1 },
    { k: 'rsi_period', label: 'RSI Period', step: 1 },
    { k: 'rsi_buy_min', label: 'RSI Min', step: 1 },
    { k: 'rsi_buy_max', label: 'RSI Max', step: 1 },
    { k: 'atr_multiplier', label: 'ATR ×', step: 0.1 },
    { k: 'rel_vol_min', label: 'Rel Vol ≥', step: 0.1 },
    { k: 'stop_loss_pct', label: 'Stop Loss %', step: 0.1 },
    { k: 'take_profit_pct', label: 'Take Profit %', step: 0.1 },
    { k: 'max_positions', label: 'Max Positions', step: 1 },
    { k: 'budget_per_trade', label: 'Budget / Trade', step: 50, prefix: '₹' },
    { k: 'min_confidence', label: 'Min Confidence', step: 5 },
  ];
  const flags = [
    { k: 'vwap_required',     label: 'VWAP filter' },
    { k: 'orb_required',      label: 'Opening Range Break' },
    { k: 'multi_tf_required', label: 'Multi-timeframe align' },
  ];

  return (
    <div className="card">
      <div className="card-h">
        <h3>Strategy Parameters</h3>
        <div style={{ display: 'flex', gap: 6 }}>
          <button className="btn btn-sm ghost"><Icon name="refresh" size={12} /> Reset</button>
          <button className="btn btn-sm primary">Save</button>
        </div>
      </div>
      <div className="card-b" style={{ display: 'grid', gap: 14 }}>
        <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr 1fr' : 'repeat(3, 1fr)', gap: 12 }}>
          {fields.map(f => (
            <div className="field" key={f.k}>
              <label>{f.label}</label>
              <input
                className="input" type="number" step={f.step}
                value={params[f.k]}
                onChange={e => onChange({ ...params, [f.k]: Number(e.target.value) })}
              />
            </div>
          ))}
        </div>
        <div style={{ height: 1, background: 'var(--line-soft)' }} />
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="eyebrow">Signal Filters</div>
          {flags.map(f => (
            <div key={f.k} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontSize: 13 }}>{f.label}</span>
              <div className={cls('toggle', params[f.k] && 'on')} onClick={() => onChange({ ...params, [f.k]: !params[f.k] })} />
            </div>
          ))}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontSize: 13 }}>Bar timeframe</span>
            <select className="select" value={params.bar_timeframe} onChange={e => onChange({ ...params, bar_timeframe: e.target.value })}>
              <option>1Min</option><option>5Min</option><option>15Min</option><option>1H</option>
            </select>
          </div>
        </div>
        <div style={{ height: 1, background: 'var(--line-soft)' }} />
        <div className="eyebrow">Trade Window (IST)</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10, fontFamily: 'var(--font-mono)', fontSize: 13 }}>
          <Win label="Entries" v={`${pad(params.trade_start_hour)}:${pad(params.trade_start_min)} → ${pad(params.trade_end_hour)}:${pad(params.trade_end_min)}`} />
          <Win label="Force Close" v={`${pad(params.close_hour)}:${pad(params.close_min)}`} />
          <Win label="TF" v={params.bar_timeframe} />
        </div>
      </div>
    </div>
  );
}
const pad = n => String(n).padStart(2, '0');
const Win = ({ label, v }) => (
  <div style={{ background: 'var(--bg-2)', border: '1px solid var(--line-soft)', borderRadius: 'var(--r)', padding: '10px 12px' }}>
    <div className="eyebrow" style={{ marginBottom: 4 }}>{label}</div>
    <div className="mono">{v}</div>
  </div>
);

/* ─────────── Reports ─────────── */
function ReportsCard({ reports }) {
  return (
    <div className="card">
      <div className="card-h"><h3>End-of-day Reports</h3><button className="btn btn-sm ghost">Generate now</button></div>
      <div className="card-b flush">
        <table>
          <thead>
            <tr><th>Date</th><th>Trades</th><th>W/L</th><th>P&amp;L</th><th>ROI</th><th></th></tr>
          </thead>
          <tbody>
            {reports.map(r => (
              <tr key={r.date}>
                <td data-label="Date" className="mono">{r.date}</td>
                <td data-label="Trades" className="mono">{r.trades}</td>
                <td data-label="W/L" className="mono"><span className="up">{r.win}</span> / <span className="down">{r.loss}</span></td>
                <td data-label="P&L" className={cls('mono', r.pnl >= 0 ? 'up' : 'down')}>{sign(r.pnl)}{inr(Math.abs(r.pnl))}</td>
                <td data-label="ROI" className={cls('mono', r.roi >= 0 ? 'up' : 'down')}>{pct(r.roi)}</td>
                <td data-label=""><button className="btn btn-sm ghost">Open</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ─────────── Bot Controls (small panel) ─────────── */
function ControlsCard({ paused, onPause, onFlatten, onScan }) {
  return (
    <div className="card">
      <div className="card-h"><h3>Bot Controls</h3>
        <span className={cls('pill', paused ? 'warn' : 'up')}>
          <span className={cls('dot', paused ? 'warn' : 'up', !paused && 'pulse')} />
          {paused ? 'PAUSED' : 'RUNNING'}
        </span>
      </div>
      <div className="card-b" style={{ display: 'grid', gap: 10 }}>
        <button className={cls('btn', paused ? 'success' : 'danger')} onClick={onPause}>
          <Icon name={paused ? 'play' : 'pause'} size={14} />
          {paused ? 'Resume bot' : 'Pause bot'}
        </button>
        <button className="btn" onClick={onScan}><Icon name="refresh" size={14} /> Force scan</button>
        <button className="btn danger" onClick={onFlatten}><Icon name="target" size={14} /> Flatten all positions</button>
        <div style={{ height: 1, background: 'var(--line-soft)', margin: '4px 0' }} />
        <Row k="Regime" v={<span className="pill up" style={{ padding: '2px 7px' }}>TRENDING</span>} />
        <Row k="Symbols loaded" v={<span className="mono">47</span>} />
        <Row k="Last scan" v={<span className="mono" style={{ color: 'var(--ink-3)' }}>11:48:22 IST</span>} />
        <Row k="Cooldown" v={<span className="mono" style={{ color: 'var(--ink-3)' }}>0 / 3</span>} />
      </div>
    </div>
  );
}
const Row = ({ k, v }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 13 }}>
    <span style={{ color: 'var(--ink-3)' }}>{k}</span>
    <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>{v}</span>
  </div>
);

/* ─────────── Mobile bottom bar ─────────── */
function MobileBar({ tab, onTab }) {
  const items = [
    { id: 'overview',  label: 'Home',     icon: 'dash' },
    { id: 'positions', label: 'Holdings', icon: 'portfolio' },
    { id: 'trades',    label: 'Trades',   icon: 'list' },
    { id: 'strategy',  label: 'Strategy', icon: 'cog' },
    { id: 'reports',   label: 'Reports',  icon: 'file' },
  ];
  return (
    <nav className="mobile-bar">
      {items.map(it => (
        <button key={it.id} className={cls(tab === it.id && 'active')} onClick={() => onTab(it.id)}>
          <Icon name={it.icon} size={20} />
          {it.label}
        </button>
      ))}
    </nav>
  );
}

window.ATC2 = { PicksCard, TradesCard, ActivityCard, StrategyCard, ReportsCard, ControlsCard, MobileBar };
