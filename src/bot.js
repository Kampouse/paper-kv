// paper-kv — Paper trading bot
// Binance prices for signals, NEAR FastData KV for persistent storage
// No blockchain trades, no gas for trades, just pure paper trading

import { connect, keyStores } from "near-api-js";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── Config ──────────────────────────────────────────────────────────────────

function loadConfig() {
  const raw = fs.readFileSync(path.join(__dirname, "..", ".env"), "utf-8");
  const env = Object.fromEntries(
    raw.split("\n").filter((l) => l && !l.startsWith("#")).map((l) => {
      const [k, ...v] = l.split("=");
      return [k, v.join("=")];
    })
  );
  return {
    nearAccount: env.NEAR_ACCOUNT,
    nearPrivateKey: env.NEAR_PRIVATE_KEY,
    kvContract: env.KV_CONTRACT,
    initialBalance: parseFloat(env.INITIAL_BALANCE),
    tradeSize: parseFloat(env.TRADE_SIZE),
    defaultLeverage: parseFloat(env.DEFAULT_LEVERAGE),
    maxOpenTrades: parseInt(env.MAX_OPEN_TRADES),
    checkInterval: parseInt(env.CHECK_INTERVAL_MS),
    strategy: env.STRATEGY,
    momentumLookback: parseInt(env.MOMENTUM_LOOKBACK_MINUTES),
    momentumThreshold: parseFloat(env.MOMENTUM_THRESHOLD_PCT),
    tradePairs: (env.TRADE_PAIRS || "BTCUSDT,ETHUSDT").split(","),
  };
}

// ── KV Client ───────────────────────────────────────────────────────────────

const KV_BASE = "https://kv.main.fastnear.com";

class KVStore {
  constructor(near, account, contract) {
    this.near = near;
    this.account = account;
    this.contract = contract;
    this.cache = new Map();
  }

  async get(key) {
    if (this.cache.has(key)) return this.cache.get(key);
    try {
      const res = await fetch(
        `${KV_BASE}/v0/latest/${this.contract}/${this.account}/${key}`
      );
      if (res.status === 404) return null;
      const data = await res.json();
      const entry = data.entries?.[0];
      if (!entry) return null;
      const value = typeof entry.value === "string" ? JSON.parse(entry.value) : entry.value;
      this.cache.set(key, value);
      return value;
    } catch (err) {
      console.error(`  ⚠️  KV read failed (${key}):`, err.message);
      return null;
    }
  }

  async multi(keys) {
    const result = new Map();
    try {
      const kvKeys = keys.map((k) => `${this.contract}/${this.account}/${k}`);
      const res = await fetch(`${KV_BASE}/v0/multi`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: kvKeys }),
      });
      const data = await res.json();
      for (let i = 0; i < keys.length; i++) {
        const entry = data.entries?.[i];
        if (entry) {
          const value = typeof entry.value === "string" ? JSON.parse(entry.value) : entry.value;
          result.set(keys[i], value);
          this.cache.set(keys[i], value);
        }
      }
    } catch (err) {
      console.error("  ⚠️  KV multi read failed:", err.message);
    }
    return result;
  }

  async put(key, value) {
    this.cache.set(key, value);
    try {
      const account = await this.near.account(this.account);
      await account.functionCall({
        contractId: this.contract,
        methodName: "__fastdata_kv",
        args: { [key]: value },
        gas: "30000000000000",
      });
    } catch (err) {
      console.error(`  ❌ KV write failed (${key}):`, err.message);
    }
  }

  async putBatch(data) {
    for (const [k, v] of Object.entries(data)) {
      this.cache.set(k, v);
    }
    try {
      const account = await this.near.account(this.account);
      await account.functionCall({
        contractId: this.contract,
        methodName: "__fastdata_kv",
        args: data,
        gas: "300000000000000",
      });
    } catch (err) {
      console.error("  ❌ KV batch write failed:", err.message);
    }
  }
}

// ── Price Feed ──────────────────────────────────────────────────────────────

class PriceFeed {
  constructor() {
    this.cache = new Map();
  }

  async fetchPrices(symbols) {
    const prices = new Map();
    try {
      const res = await fetch(
        `https://api.binance.com/api/v3/ticker/price?symbols=${JSON.stringify(symbols)}`
      );
      const data = await res.json();
      for (const d of data) {
        const price = parseFloat(d.price);
        prices.set(d.symbol, price);
        const pts = this.cache.get(d.symbol) || [];
        pts.push({ ts: Date.now(), price });
        const cutoff = Date.now() - 4 * 60 * 60 * 1000;
        while (pts.length > 0 && pts[0].ts < cutoff) pts.shift();
        this.cache.set(d.symbol, pts);
      }
    } catch (err) {
      console.error("  ⚠️  Binance fetch failed:", err.message);
    }
    return prices;
  }

  getMomentum(symbol, lookbackMin) {
    const pts = this.cache.get(symbol) || [];
    if (pts.length < 2) return { current: 0, change: 0, dir: "flat" };

    const cutoff = Date.now() - lookbackMin * 60 * 1000;
    const window = pts.filter((p) => p.ts >= cutoff);
    if (window.length < 2) return { current: 0, change: 0, dir: "flat" };

    const oldest = window[0].price;
    const newest = window[window.length - 1].price;
    const change = ((newest - oldest) / oldest) * 100;

    return {
      current: newest,
      change,
      dir: change > 0.2 ? "up" : change < -0.2 ? "down" : "flat",
    };
  }
}

// ── Bot ─────────────────────────────────────────────────────────────────────

class PaperBot {
  constructor(near, config) {
    this.near = near;
    this.config = config;
    this.kv = new KVStore(near, config.nearAccount, config.kvContract);
    this.priceFeed = new PriceFeed();
    this.state = {
      balance: config.initialBalance,
      totalTrades: 0,
      wins: 0,
      losses: 0,
      totalPnl: 0,
    };
    this.positions = [];
    this.trades = [];
    this.running = false;
  }

  async init() {
    console.log("╔═══════════════════════════════════════════════╗");
    console.log("║   paper-kv — Paper Trading Bot               ║");
    console.log("║   Binance prices + NEAR KV storage            ║");
    console.log("╚═══════════════════════════════════════════════╝");
    console.log("");
    console.log(`  Account:    ${this.config.nearAccount}`);
    console.log(`  KV store:   ${this.config.kvContract}`);
    console.log(`  Strategy:   ${this.config.strategy}`);
    console.log(`  Leverage:   ${this.config.defaultLeverage}x`);
    console.log(`  Trade size: $${this.config.tradeSize}`);
    console.log(`  Pairs:      ${this.config.tradePairs.join(", ")}`);
    console.log("");

    await this.loadState();
    console.log("");
  }

  async loadState() {
    console.log("── Loading state from KV ──");

    const [state, positions, trades] = await Promise.all([
      this.kv.get("state"),
      this.kv.get("positions"),
      this.kv.get("trades"),
    ]);

    if (state) {
      this.state = state;
      console.log(`  Balance:    $${this.state.balance.toFixed(2)}`);
      console.log(`  Trades:     ${this.state.totalTrades} (${this.state.wins}W/${this.state.losses}L)`);
      console.log(`  Total PnL:  $${this.state.totalPnl.toFixed(2)}`);
    } else {
      console.log(`  New account — starting with $${this.config.initialBalance}`);
      await this.saveState();
    }

    this.positions = positions || [];
    this.trades = trades || [];
    console.log(`  Open:       ${this.positions.length} positions`);
    console.log(`  History:    ${this.trades.length} closed trades`);

    const prices = await this.priceFeed.fetchPrices(this.config.tradePairs);
    for (const [sym, price] of prices) {
      console.log(`  ${sym}: $${price.toLocaleString()}`);
    }
  }

  async saveState() {
    await this.kv.putBatch({
      state: this.state,
      positions: this.positions,
      trades: this.trades,
    });
  }

  openPosition(symbol, direction, price) {
    if (this.positions.length >= this.config.maxOpenTrades) {
      console.log(`  ⏸️  Max positions (${this.config.maxOpenTrades})`);
      return null;
    }

    const existing = this.positions.find(
      (p) => p.symbol === symbol && p.direction === direction
    );
    if (existing) return null;

    const collateral = this.config.tradeSize;
    const leverage = this.config.defaultLeverage;
    const size = collateral * leverage;

    const position = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      symbol,
      direction,
      entryPrice: price,
      leverage,
      size,
      collateral,
      openedAt: new Date().toISOString(),
    };

    this.positions.push(position);
    console.log(
      `  🟢 OPENED ${direction.toUpperCase()} ${symbol} ${leverage}x | Entry: $${price.toLocaleString()} | Size: $${size.toLocaleString()}`
    );

    return position;
  }

  closePosition(position, price, reason) {
    const idx = this.positions.indexOf(position);
    if (idx === -1) return null;

    this.positions.splice(idx, 1);

    let pnlPct;
    if (position.direction === "long") {
      pnlPct = ((price - position.entryPrice) / position.entryPrice) * position.leverage * 100;
    } else {
      pnlPct = ((position.entryPrice - price) / position.entryPrice) * position.leverage * 100;
    }

    const pnl = position.collateral * (pnlPct / 100);

    const closed = {
      ...position,
      exitPrice: price,
      pnl,
      pnlPct,
      closedAt: new Date().toISOString(),
      exitReason: reason,
    };

    this.state.balance += pnl;
    this.state.totalTrades++;
    this.state.totalPnl += pnl;
    if (pnl >= 0) this.state.wins++;
    else this.state.losses++;

    this.trades.push(closed);

    const emoji = pnl >= 0 ? "🟢" : "🔴";
    console.log(
      `  ${emoji} CLOSED ${position.direction.toUpperCase()} ${position.symbol} | Entry: $${position.entryPrice.toLocaleString()} → Exit: $${price.toLocaleString()} | PnL: ${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}% ($${pnl.toFixed(2)})`
    );

    return closed;
  }

  async runMomentum(prices) {
    for (const symbol of this.config.tradePairs) {
      const price = prices.get(symbol);
      if (!price) continue;

      const momentum = this.priceFeed.getMomentum(symbol, this.config.momentumLookback);
      const existing = this.positions.find((p) => p.symbol === symbol);

      if (!existing) {
        if (momentum.dir === "up" && Math.abs(momentum.change) >= this.config.momentumThreshold) {
          console.log(`  📈 ${symbol} momentum UP +${momentum.change.toFixed(2)}%`);
          this.openPosition(symbol, "long", price);
        } else if (momentum.dir === "down" && Math.abs(momentum.change) >= this.config.momentumThreshold) {
          console.log(`  📉 ${symbol} momentum DOWN ${momentum.change.toFixed(2)}%`);
          this.openPosition(symbol, "short", price);
        }
      } else {
        const isLong = existing.direction === "long";
        const reversed =
          (isLong && momentum.dir === "down") ||
          (!isLong && momentum.dir === "up");

        if (reversed && Math.abs(momentum.change) >= this.config.momentumThreshold) {
          console.log(`  🔄 ${symbol} reversed (${momentum.change.toFixed(2)}%)`);
          this.closePosition(existing, price, "momentum_reversal");
        }
      }
    }
  }

  async tick() {
    const now = new Date().toISOString().replace("T", " ").slice(0, 19);
    console.log(`\n── ${now} ──`);

    const prices = await this.priceFeed.fetchPrices(this.config.tradePairs);
    for (const [sym, price] of prices) {
      console.log(`  💲 ${sym}: $${price.toLocaleString()}`);
    }

    switch (this.config.strategy) {
      case "momentum":
        await this.runMomentum(prices);
        break;
    }

    if (this.positions.length > 0) {
      console.log(`\n  📊 Open Positions (${this.positions.length}):`);
      for (const p of this.positions) {
        const currentPrice = prices.get(p.symbol) || p.entryPrice;
        const unrealized =
          p.direction === "long"
            ? ((currentPrice - p.entryPrice) / p.entryPrice) * p.leverage * 100
            : ((p.entryPrice - currentPrice) / p.entryPrice) * p.leverage * 100;
        const emoji = unrealized >= 0 ? "🟢" : "🔴";
        console.log(
          `    ${emoji} ${p.symbol} ${p.direction.toUpperCase()} ${p.leverage}x | $${p.entryPrice.toLocaleString()} → $${currentPrice.toLocaleString()} | ${unrealized >= 0 ? "+" : ""}${unrealized.toFixed(2)}%`
        );
      }
    }

    console.log(`\n  💰 Balance: $${this.state.balance.toFixed(2)} | Trades: ${this.state.totalTrades} (${this.state.wins}W/${this.state.losses}L) | PnL: ${this.state.totalPnl >= 0 ? "+" : ""}$${this.state.totalPnl.toFixed(2)}`);

    await this.saveState();
  }

  async start() {
    await this.init();
    this.running = true;

    console.log(`▶  Running every ${this.config.checkInterval / 1000}s (Ctrl+C to stop)\n`);

    await this.tick();

    const interval = setInterval(async () => {
      if (!this.running) return;
      try {
        await this.tick();
      } catch (err) {
        console.error("Tick error:", err.message);
      }
    }, this.config.checkInterval);

    process.on("SIGINT", async () => {
      console.log("\n\n⏹  Shutting down...");
      this.running = false;
      clearInterval(interval);

      await this.saveState();
      console.log(`  State saved to KV`);
      console.log(`  Final balance: $${this.state.balance.toFixed(2)}`);
      console.log(`  Total trades: ${this.state.totalTrades} (${this.state.wins}W/${this.state.losses}L)`);
      console.log(`  Total PnL: ${this.state.totalPnl >= 0 ? "+" : ""}$${this.state.totalPnl.toFixed(2)}`);
      console.log(`  View: https://kv.main.fastnear.com/v0/latest/${this.config.kvContract}/${this.config.nearAccount}/state\n`);
      process.exit(0);
    });
  }
}

// ── Main ────────────────────────────────────────────────────────────────────

const config = loadConfig();

if (!config.nearPrivateKey || config.nearPrivateKey === "your_private_key_here") {
  console.error("❌ Set NEAR_ACCOUNT and NEAR_PRIVATE_KEY in .env");
  console.error("   cp .env.example .env && edit .env");
  process.exit(1);
}

const keyStore = new keyStores.InMemoryKeyStore();
await keyStore.setKey("mainnet", config.nearAccount, config.nearPrivateKey);

const near = await connect({
  networkId: "mainnet",
  keyStore,
  nodeUrl: "https://rpc.mainnet.near.org",
});

const bot = new PaperBot(near, config);
bot.start().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
