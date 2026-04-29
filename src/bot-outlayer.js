// paper-kv — Paper trading bot (OutLayer custody wallet version)
// Binance prices for signals, NEAR FastData KV for persistent storage
// No private key needed — uses OutLayer custody wallet API for KV writes

import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── Config ──────────────────────────────────────────────────────────────

function loadConfig() {
  const raw = fs.readFileSync(path.join(__dirname, "..", ".env"), "utf-8");
  const env = Object.fromEntries(
    raw.split("\n").filter((l) => l && !l.startsWith("#")).map((l) => {
      const [k, ...v] = l.split("=");
      return [k, v.join("=")];
    })
  );
  return {
    // OutLayer custody wallet (replaces NEAR_PRIVATE_KEY)
    outlayerApiKey: env.OUTLAYER_API_KEY,
    nearAccount: env.NEAR_ACCOUNT,
    kvContract: env.KV_CONTRACT || "paper-kv.near",
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

// ── KV Client (OutLayer custody wallet) ─────────────────────────────────

const KV_BASE = "https://kv.main.fastnear.com";
const OUTLAYER_BASE = "https://api.outlayer.fastnear.com";

class KVStore {
  constructor(config) {
    this.config = config;
    this.cache = new Map();
    this.useOutLayer = !!config.outlayerApiKey;
  }

  async get(key) {
    if (this.cache.has(key)) return this.cache.get(key);
    try {
      const res = await fetch(
        `${KV_BASE}/v0/latest/${this.config.kvContract}/${this.config.nearAccount}/${key}`
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
      const kvKeys = keys.map((k) => `${this.config.kvContract}/${this.config.nearAccount}/${k}`);
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
  }

  async putBatch(data) {
    for (const [k, v] of Object.entries(data)) {
      this.cache.set(k, v);
    }

    if (this.useOutLayer) {
      try {
        const res = await fetch(`${OUTLAYER_BASE}/wallet/v1/call`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${this.config.outlayerApiKey}`,
          },
          body: JSON.stringify({
            contract_id: this.config.kvContract,
            method_name: "__fastdata_kv",
            args: JSON.stringify(data),
            gas: "300000000000000",
          }),
        });
        const result = await res.json();
        if (result.status === "pending_approval") {
          console.log(`  📝 KV write pending approval (request: ${result.approval_id})`);
        } else if (result.error) {
          console.error("  ❌ KV batch write failed:", result.error);
        }
      } catch (err) {
        console.error("  ❌ KV batch write failed:", err.message);
      }
    } else {
      console.error("  ❌ No OutLayer API key configured");
    }
  }
}

// ── Price Feed ──────────────────────────────────────────────────────────

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

  getMomentum(symbol, lookaheadMin) {
    const pts = this.cache.get(symbol) || [];
    if (pts.length < 2) return { current: 0, change: 0, dir: "flat" };

    const cutoff = Date.now() - lookaheadMin * 60 * 1000;
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

// ── Bot ─────────────────────────────────────────────────────────────────

class PaperBot {
  constructor(config) {
    this.config = config;
    this.kv = new KVStore(config);
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
    console.log("══════════════════════════════════════════════════════════");
    console.log("║   paper-kv — Paper Trading Bot (OutLayer Edition)      ║");
    console.log("║   Binance prices + NEAR KV storage                     ║");
    console.log("╚══════════════════════════════════════════════════════════");
    console.log("");
    console.log(`  Account:    ${this.config.nearAccount}`);
    console.log(`  KV store:   ${this.config.kvContract}`);
    console.log(`  Strategy:   ${this.config.strategy}`);
    console.log(`  Leverage:   ${this.config.defaultLeverage}x`);
    console.log(`  Trade size: $${this.config.tradeSize}`);
    console.log(`  Pairs:      ${this.config.tradePairs.join(", ")}`);
    console.log(`  Auth:       ${this.kv.useOutLayer ? "OutLayer custody wallet ✅" : "NOT CONFIGURED ❌"}`);
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
      console.log(`  ⚠️  Max positions (${this.config.maxOpenTrades})`);
      return null;
    }

    const existing = this.positions.find(
      (p) => p.symbol === symbol && p.direction === direction
    );
    if (existing) return null;

    const collateral = this.config.tradeSize;
    const leverage = this.config.defaultLeverage;
    const size = collateral * leverage;

    const maintenanceMargin = 0.005;
    let liquidationPrice;
    if (direction === "long") {
      liquidationPrice = price * (1 - 1 / leverage + maintenanceMargin);
    } else {
      liquidationPrice = price * (1 + 1 / leverage - maintenanceMargin);
    }

    const position = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      symbol,
      direction,
      entryPrice: price,
      leverage,
      size,
      collateral,
      liquidationPrice,
      fundingFeesPaid: 0,
      openedAt: new Date().toISOString(),
    };

    this.positions.push(position);
    console.log(
      `  ✅ OPENED ${direction.toUpperCase()} ${symbol} ${leverage}x | Entry: $${price.toLocaleString()} | Size: $${size.toLocaleString()} | Liq: $${liquidationPrice.toLocaleString()}`
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

    const emoji = pnl >= 0 ? "✅" : "❌";
    console.log(
      `  ${emoji} CLOSED ${position.direction.toUpperCase()} ${position.symbol} | Entry: $${position.entryPrice.toLocaleString()} → Exit: $${price.toLocaleString()} | PnL: ${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}% ($${pnl.toFixed(2)})`
    );

    return closed;
  }

  checkLiquidations(prices) {
    const fundingRate = 0.0001;
    const ticksPerPeriod = (8 * 60 * 60) / (this.config.checkInterval / 1000);

    const toClose = [];

    for (const pos of this.positions) {
      const price = prices.get(pos.symbol);
      if (!price) continue;

      const fee = pos.size * fundingRate / ticksPerPeriod;
      pos.fundingFeesPaid = (pos.fundingFeesPaid || 0) + fee;

      let liquidated = false;
      if (pos.direction === "long" && price <= pos.liquidationPrice) {
        liquidated = true;
      } else if (pos.direction === "short" && price >= pos.liquidationPrice) {
        liquidated = true;
      }

      if (liquidated) {
        toClose.push({ pos, price, reason: "liquidated" });
      }
    }

    for (const { pos, price, reason } of toClose) {
      const idx = this.positions.indexOf(pos);
      if (idx !== -1) this.positions.splice(idx, 1);

      const closed = {
        ...pos,
        exitPrice: price,
        pnl: -pos.collateral,
        pnlPct: -100,
        closedAt: new Date().toISOString(),
        exitReason: reason,
      };

      this.state.balance -= pos.collateral;
      this.state.totalTrades++;
      this.state.totalPnl -= pos.collateral;
      this.state.losses++;
      this.trades.push(closed);

      console.log(
        `  💀 LIQUIDATED ${pos.direction.toUpperCase()} ${pos.symbol} ${pos.leverage}x | Entry: $${pos.entryPrice.toLocaleString()} → $${price.toLocaleString()} | Lost: $${pos.collateral}`
      );
    }
  }

  async runMomentum(prices) {
    for (const symbol of this.config.tradePairs) {
      const price = prices.get(symbol);
      if (!price) continue;

      const momentum = this.priceFeed.getMomentum(symbol, this.config.momentumLookback);
      const existing = this.positions.find((p) => p.symbol === symbol);

      if (!existing) {
        if (momentum.dir === "up" && Math.abs(momentum.change) >= this.config.momentumThreshold) {
          console.log(`  📈 ${symbol} momentum UP ${momentum.change.toFixed(2)}%`);
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

    this.checkLiquidations(prices);

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
        const emoji = unrealized >= 0 ? "✅" : "❌";
        const fees = (p.fundingFeesPaid || 0).toFixed(2);
        console.log(
          `    ${emoji} ${p.symbol} ${p.direction.toUpperCase()} ${p.leverage}x | $${p.entryPrice.toLocaleString()} → $${currentPrice.toLocaleString()} | ${unrealized >= 0 ? "+" : ""}${unrealized.toFixed(2)}% | Liq: $${p.liquidationPrice.toLocaleString()} | Fees: $${fees}`
        );
      }
    }

    console.log(
      `\n  💰 Balance: $${this.state.balance.toFixed(2)} | Trades: ${this.state.totalTrades} (${this.state.wins}W/${this.state.losses}L) | PnL: ${this.state.totalPnl >= 0 ? "+" : ""}$${this.state.totalPnl.toFixed(2)}`
    );

    await this.saveState();
  }

  async start() {
    await this.init();
    this.running = true;

    console.log(`🏃  Running every ${this.config.checkInterval / 1000}s (Ctrl+C to stop)\n`);

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

// ── Main ────────────────────────────────────────────────────────────────

const config = loadConfig();

if (!config.outlayerApiKey) {
  console.error("❌ Set OUTLAYER_API_KEY in .env");
  console.error("   Get a custody wallet: https://outlayer.fastnear.com");
  process.exit(1);
}

if (!config.nearAccount) {
  console.error("❌ Set NEAR_ACCOUNT in .env (your custody wallet account ID)");
  process.exit(1);
}

const bot = new PaperBot(config);
bot.start().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
