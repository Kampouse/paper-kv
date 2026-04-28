// paper-kv status — read-only view of any paper trader's results
// Usage: node src/status.js [account] [contract]
// No wallet needed — reads from KV HTTP API

const KV_BASE = "https://kv.main.fastnear.com";

async function main() {
  const account = process.argv[2] || "jemartel.near";
  const contract = process.argv[3] || "paper-kv.near";

  console.log(`\n── paper-kv Status ──`);
  console.log(`  Account:  ${account}`);
  console.log(`  Contract: ${contract}\n`);

  // Fetch latest state
  const keys = ["state", "positions", "trades"];
  const res = await fetch(`${KV_BASE}/v0/multi`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      keys: keys.map((k) => `${contract}/${account}/${k}`),
    }),
  });

  const data = await res.json();

  let state = null;
  let positions = [];
  let trades = [];

  for (let i = 0; i < keys.length; i++) {
    const entry = data.entries?.[i];
    if (!entry) continue;
    const value = typeof entry.value === "string" ? JSON.parse(entry.value) : entry.value;
    if (keys[i] === "state") state = value;
    if (keys[i] === "positions") positions = value || [];
    if (keys[i] === "trades") trades = value || [];
  }

  if (!state) {
    console.log("  ❌ No data found for this account");
    console.log(`  URL: ${KV_BASE}/v0/latest/${contract}/${account}/state\n`);
    process.exit(0);
  }

  // Summary
  const winRate = state.totalTrades > 0 ? ((state.wins / state.totalTrades) * 100).toFixed(1) : "0.0";
  const pnlEmoji = state.totalPnl >= 0 ? "🟢" : "🔴";

  console.log(`  💰 Balance:    $${state.balance.toFixed(2)}`);
  console.log(`  📊 Total PnL:  ${pnlEmoji} ${state.totalPnl >= 0 ? "+" : ""}$${state.totalPnl.toFixed(2)}`);
  console.log(`  📈 Trades:     ${state.totalTrades} (${state.wins}W/${state.losses}L) — ${winRate}% win rate`);
  console.log("");

  // Open positions
  if (positions.length > 0) {
    console.log(`  📂 Open Positions (${positions.length}):`);
    for (const p of positions) {
      const dir = p.direction === "long" ? "🟢 LONG" : "🔴 SHORT";
      const opened = new Date(p.openedAt).toLocaleString();
      console.log(`    ${dir} ${p.symbol} ${p.leverage}x | Entry: $${p.entryPrice.toLocaleString()} | Size: $${p.size.toLocaleString()} | ${opened}`);
    }
    console.log("");
  }

  // Recent trades
  const recent = trades.slice(-10).reverse();
  if (recent.length > 0) {
    console.log(`  📜 Recent Trades (last 10 of ${trades.length}):`);
    for (const t of recent) {
      const emoji = t.pnl >= 0 ? "🟢" : "🔴";
      const pnl = `${t.pnlPct >= 0 ? "+" : ""}${t.pnlPct.toFixed(2)}% ($${t.pnl.toFixed(2)})`;
      console.log(
        `    ${emoji} ${t.direction.toUpperCase()} ${t.symbol} ${t.leverage}x | $${t.entryPrice.toLocaleString()} → $${t.exitPrice.toLocaleString()} | ${pnl} | ${t.exitReason}`
      );
    }
    console.log("");
  }

  console.log(`  🔗 KV URL: ${KV_BASE}/v0/latest/${contract}/${account}/state\n`);
}

main().catch((err) => {
  console.error("Error:", err.message);
  process.exit(1);
});
