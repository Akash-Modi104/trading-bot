/* global React, ReactDOM, AT, ATC1, ATC2, useTweaks, TweaksPanel, TweakSection, TweakColor, TweakRadio, TweakToggle */
const { useState, useEffect, useMemo } = React;
const { inr, num, pct, sign, cls, buildEquityCurve,
  STRATEGY, POSITIONS_SEED, PICKS_SEED, TRADES_SEED, ACTIVITY_SEED, REPORTS_SEED } = AT;
const { Sidebar, TopBar, Kpis, EquityChart, PositionsCard, Icon } = ATC1;
const { PicksCard, TradesCard, ActivityCard, StrategyCard, ReportsCard, ControlsCard, MobileBar } = ATC2;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "accent": "#5BA8FF",
  "density": "comfortable"
}/*EDITMODE-END*/;

function App() {
  const [tweaks, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [tab, setTab] = useState('overview');
  const [paused, setPaused] = useState(false);
  const [account, setAccount] = useState({ id: 'paper-1', name: 'Paper · Alpaca', broker: 'Alpaca', mode: 'PAPER', equity: 104435.50, mask: '••42', initials: 'PA' });
  const [params, setParams] = useState(STRATEGY);
  const [positions, setPositions] = useState(POSITIONS_SEED);
  const [picks] = useState(PICKS_SEED);
  const [trades] = useState(TRADES_SEED);
  const [activity, setActivity] = useState(ACTIVITY_SEED);

  // apply accent color
  useEffect(() => {
    document.documentElement.style.setProperty('--accent', tweaks.accent);
    // make a soft variant
    document.documentElement.style.setProperty('--accent-soft',
      tweaks.accent + '33');
  }, [tweaks.accent]);

  // Live tick simulation — update LTP slightly so UI feels alive
  useEffect(() => {
    if (paused) return;
    const id = setInterval(() => {
      setPositions(prev => prev.map(p => ({
        ...p,
        ltp: +(p.ltp * (1 + (Math.random() - 0.49) * 0.0015)).toFixed(2),
      })));
    }, 2200);
    return () => clearInterval(id);
  }, [paused]);

  // Aggregates
  const exposure = positions.reduce((s, p) => s + p.ltp * p.qty, 0);
  const dayPnl = positions.reduce((s, p) => s + (p.ltp - p.buy) * p.qty, 0) + 312.10; // + closed trades
  const equity = 100000 + dayPnl + 4123.40; // mock baseline
  const dayPct = (dayPnl / (equity - dayPnl)) * 100;
  const equityCurve = useMemo(() => buildEquityCurve(equity * 0.985, 96, 0.0008, 0.0024), []);

  const handlePause = () => {
    setPaused(p => {
      const next = !p;
      setActivity(a => [{
        t: new Date().toLocaleTimeString('en-GB'),
        tag: next ? 'warn' : 'info',
        msg: next ? 'Bot paused by user · open positions held' : 'Bot resumed · scanning at next bar close',
      }, ...a]);
      return next;
    });
  };
  const handleScan = () => {
    setActivity(a => [{ t: new Date().toLocaleTimeString('en-GB'), tag: 'scan', msg: 'Manual scan triggered · 47 symbols' }, ...a]);
  };
  const handleFlatten = () => {
    setActivity(a => [{ t: new Date().toLocaleTimeString('en-GB'), tag: 'sell', msg: `Flatten requested · ${positions.length} positions to close` }, ...a]);
  };

  return (
    <div className="page" style={{ minHeight: '100vh' }}>
      <div className="shell">
        <Sidebar tab={tab} onTab={setTab} />
        <div className="main">
          <TopBar
            equity={equity} dayPnl={dayPnl} dayPct={dayPct}
            paused={paused} onPause={handlePause}
            account={account} onAccount={setAccount}
          />
          <main className="content">
            {tab === 'overview' && (
              <>
                <Kpis
                  equity={equity}
                  equityCurve={equityCurve}
                  dayPnl={dayPnl}
                  dayPct={dayPct}
                  openPositions={positions.length}
                  exposure={exposure}
                  winRate={64.2}
                  budget={params.budget_per_trade}
                />
                <div className="row-2">
                  <EquityChart curve={equityCurve} dayPct={dayPct} />
                  <PositionsCard positions={positions} />
                </div>
                <div className="row-2-eq">
                  <PicksCard picks={picks} minConf={params.min_confidence} />
                  <TradesCard trades={trades} />
                </div>
                <div className="row-2-eq">
                  <ActivityCard activity={activity} />
                  <ControlsCard paused={paused} onPause={handlePause} onFlatten={handleFlatten} onScan={handleScan} />
                </div>
              </>
            )}

            {tab === 'positions' && (
              <>
                <Kpis
                  equity={equity} equityCurve={equityCurve}
                  dayPnl={dayPnl} dayPct={dayPct}
                  openPositions={positions.length} exposure={exposure}
                  winRate={64.2} budget={params.budget_per_trade}
                />
                <PositionsCard positions={positions} />
              </>
            )}

            {tab === 'trades' && (
              <TradesCard trades={trades} full />
            )}

            {tab === 'strategy' && (
              <div className="row-2">
                <StrategyCard params={params} onChange={setParams} />
                <ControlsCard paused={paused} onPause={handlePause} onFlatten={handleFlatten} onScan={handleScan} />
              </div>
            )}

            {tab === 'reports' && (
              <ReportsCard reports={REPORTS_SEED} />
            )}
          </main>
        </div>
      </div>

      <MobileBar tab={tab} onTab={setTab} />

      <TweaksPanel>
        <TweakSection title="Accent">
          <TweakColor
            t={tweaks} setTweak={setTweak} k="accent"
            label="Brand accent"
            options={['#5BA8FF', '#6FE3B0', '#F3B23A', '#C792FF']}
          />
        </TweakSection>
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
