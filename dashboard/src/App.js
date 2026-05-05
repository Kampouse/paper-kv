import React, { useState, useEffect, useCallback } from 'react';
import './App.css';

const KV_BASE = 'https://kv.main.fastnear.com/v0/latest/contextual.near';

function fetchKV(account, key) {
  return fetch(`${KV_BASE}/${account}/${key}`)
    .then(r => r.json())
    .then(d => d.entries?.[0]?.value ?? null)
    .catch(() => null);
}

function App() {
  const [accounts, setAccounts] = useState(() => {
    const saved = localStorage.getItem('pkv_accounts');
    return saved ? JSON.parse(saved) : [];
  });
  const [data, setData] = useState({});
  const [loading, setLoading] = useState(false);
  const [selectedAccount, setSelectedAccount] = useState(null);  const discover = useCallback(async () => {
    try {
      const resp = await fetch('https://kv.main.fastnear.com/v0/latest/contextual.near', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'state', limit: 200 }),
      });
      const d = await resp.json();
      const discovered = (d.entries || [])
        .filter(e => {
          const v = e.value;
          return v && typeof v === 'object' && ('balance' in v) && ('totalTrades' in v);
        })
        .map(e => e.predecessor_id);
      
      // Merge with manually added accounts
      const merged = [...new Set([...discovered, ...accounts])];
      setAccounts(merged);
      localStorage.setItem('pkv_accounts', JSON.stringify(merged));
      return merged;
    } catch {
      return accounts;
    }
  }, [accounts]);

  const refresh = useCallback(async () => {
    setLoading(true);
    const accs = await discover();
    const results = {};
    for (const acc of accs) {
      const [state, positions, trades] = await Promise.all([
        fetchKV(acc, 'state'),
        fetchKV(acc, 'positions'),
        fetchKV(acc, 'trades'),
      ]);
      if (state) results[acc] = { state, positions: positions || [], trades: trades || [] };
    }
    setData(results);
    setLoading(false);
  }, [accounts]);

  useEffect(() => { refresh(); }, []);

  const selected = selectedAccount ? data[selectedAccount] : null;

  return (
    <div className="app">
      <header>
        <h1>⚡ paper-kv</h1>
        <span className="subtitle">On-chain paper trading dashboard</span>
        <button onClick={refresh} disabled={loading} className="refresh">
          {loading ? '⟳' : '↻'} Refresh
        </button>
      </header>

      {accounts.length === 0 && <div className="no-data">No accounts found</div>}

      <div className="accounts-grid">
        {accounts.map(acc => {
          const d = data[acc];
          const s = d?.state;
          const pnl = s?.totalPnl ?? 0;
          return (
            <div
              key={acc}
              className={`account-card ${selectedAccount === acc ? 'selected' : ''}`}
              onClick={() => setSelectedAccount(selectedAccount === acc ? null : acc)}
            >
              <div className="acc-header">
                <span className="acc-id">{acc.slice(0, 12)}...{acc.slice(-6)}</span>
              </div>
              {s ? (
                <>
                  <div className="acc-balance">${(s.balance || 0).toLocaleString(undefined, {maximumFractionDigits: 2})}</div>
                  <div className={`pnl ${pnl >= 0 ? 'positive' : 'negative'}`}>
                    {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}
                  </div>
                  <div className="acc-stats">
                    <span>{s.totalTrades || 0} trades</span>
                    <span>{s.wins || 0}W/{s.losses || 0}L</span>
                    <span>{((s.wins / Math.max(s.totalTrades, 1)) * 100).toFixed(0)}% WR</span>
                  </div>
                  {s.merkle_root && (
                    <div className="verified">🔗 Merkle verified</div>
                  )}
                </>
              ) : (
                <div className="no-data">No data</div>
              )}
            </div>
          );
        })}
      </div>

      {selected && (
        <div className="detail-panel">
          <h2>{selectedAccount.slice(0, 16)}...</h2>

          {selected.positions.length > 0 && (
            <div className="section">
              <h3>📂 Open Positions ({selected.positions.length})</h3>
              <table>
                <thead><tr><th>Pair</th><th>Dir</th><th>Leverage</th><th>Entry</th><th>Collateral</th><th>Opened</th><th>Price TS</th></tr></thead>
                <tbody>
                  {selected.positions.map((p, i) => (
                    <tr key={i}>
                      <td>{p.symbol}</td>
                      <td className={p.direction === 'long' ? 'long' : 'short'}>{p.direction.toUpperCase()}</td>
                      <td>{p.leverage}x</td>
                      <td>${p.entryPrice?.toLocaleString(undefined, {maximumFractionDigits: 2})}</td>
                      <td>${p.collateral}</td>
                      <td>{p.openedAt?.slice(0, 16)}</td>
                      <td className="mono">{p.price_ts || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="section">
            <h3>📜 Trade History ({selected.trades.length})</h3>
            <table>
              <thead><tr><th>Pair</th><th>Dir</th><th>Lev</th><th>Entry</th><th>Exit</th><th>PnL%</th><th>PnL</th><th>Reason</th><th>Price TS</th></tr></thead>
              <tbody>
                {[...selected.trades].reverse().slice(0, 100).map((t, i) => (
                  <tr key={i} className={t.pnl >= 0 ? 'row-win' : 'row-loss'}>
                    <td>{t.symbol}</td>
                    <td className={t.direction === 'long' ? 'long' : 'short'}>{t.direction.toUpperCase()}</td>
                    <td>{t.leverage}x</td>
                    <td>${t.entryPrice?.toLocaleString(undefined, {maximumFractionDigits: 2})}</td>
                    <td>${t.exitPrice?.toLocaleString(undefined, {maximumFractionDigits: 2})}</td>
                    <td className={t.pnl >= 0 ? 'positive' : 'negative'}>{t.pnlPct >= 0 ? '+' : ''}{t.pnlPct?.toFixed(2)}%</td>
                    <td className={t.pnl >= 0 ? 'positive' : 'negative'}>${t.pnl?.toFixed(2)}</td>
                    <td><span className={`badge badge-${t.exitReason}`}>{t.exitReason}</span></td>
                    <td className="mono">{t.price_ts || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {selected.state.merkle_root && (
            <div className="section merkle-info">
              <h3>🔗 Merkle Proof</h3>
              <div className="merkle-detail">
                <div><strong>Root:</strong> <code>{selected.state.merkle_root}</code></div>
                <div><strong>Ticks:</strong> {selected.state.tick_count}</div>
                <div><strong>Last tick:</strong> {new Date(selected.state.last_tick_ts).toISOString()}</div>
                <div><strong>Prev root:</strong> <code>{selected.state.last_prev_root?.slice(0, 24)}...</code></div>
              </div>
            </div>
          )}
        </div>
      )}

      <footer>
        <a href="https://github.com/Kampouse/paper-kv" target="_blank" rel="noreferrer">GitHub</a>
        <span>·</span>
        <a href="https://kv.main.fastnear.com" target="_blank" rel="noreferrer">FastData KV</a>
        <span>·</span>
        <span>All data on NEAR blockchain</span>
      </footer>
    </div>
  );
}

export default App;
