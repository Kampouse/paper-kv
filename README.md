# paper-kv

Paper trading bot with zero setup. Real Binance prices, results stored on NEAR blockchain via FastData KV — publicly verifiable, no fake data.

**Two versions:** Node.js (original) and Python (gasless via OutLayer).

## How It Works

- Fetches live prices from Binance every 60s
- Runs momentum strategy (configurable)
- Opens/closes paper positions — no real money
- Saves all state to NEAR KV (positions, trades, balance)
- Anyone can verify results via the free KV HTTP API

---

## Quick Start (Python — recommended)

### Prerequisites

- Python 3.8+ (no pip installs needed — stdlib only)

### What is OutLayer?

[OutLayer](https://outlayer.fastnear.com) is a custody service for AI agents built on NEAR. It gives your bot a wallet secured inside a **TEE (Trusted Execution Environment)** — Intel TDX enclave. Your agent authenticates with an API key, but never sees a private key. All transaction signing happens inside the enclave.

For paper-kv, OutLayer provides:
- **Gasless contract calls** — write to KV without holding NEAR for gas
- **No private key management** — the API key is the only secret
- **Policy controls** — set spending limits, whitelist contracts, freeze wallet

**Important:** While the initial setup and basic usage are free, OutLayer transaction fees may apply depending on usage. Check [dashboard.fastnear.com](https://dashboard.fastnear.com) for current pricing. For heavy usage, you may need to deposit a small amount of NEAR into your OutLayer wallet.

### Step 1: Get an OutLayer wallet

**Option A — Web (for humans):**

Go to [outlayer.fastnear.com](https://outlayer.fastnear.com) and click **Register**.

**Option B — CLI (for agents/CI):**

```bash
curl -s -X POST https://api.outlayer.fastnear.com/register
```

Either way you'll get:
- An **API key** (`wk_...`) — save this, it's shown only once
- A **wallet dashboard** URL — manage policy, view transactions, deposit funds

No NEAR account, no email, no downloads. 10 seconds.

### Step 2: Fund your wallet (optional)

Basic usage is free thanks to trial credits. For 24/7 continuous operation:

1. Open your OutLayer dashboard
2. Copy your NEAR deposit address
3. Send ~0.5 NEAR (lasts weeks at 1 write/minute)

Check pricing at [dashboard.fastnear.com](https://dashboard.fastnear.com).

### Step 3: Set up auto-approve policy

In your OutLayer dashboard:

1. Go to **Policy**
2. Add `paper-kv.near` to the **address whitelist**
3. Allow **`call`** as a transaction type
4. Save

Without this, you'll need to manually approve each KV write from the dashboard. With it, the bot runs hands-free.

### Step 4: Run the bot

**If running manually:**
```bash
export OUTLAYER_API_KEY=wk_your_key_here
python3 paper_kv.py
```

**If running inside an agent (NEAR AI, OpenClaw, etc):**

Store the key in the agent's shell profile so it persists across sessions:

```bash
# Add to .bashrc or .env
echo 'export OUTLAYER_API_KEY=wk_your_key_here' >> ~/.bashrc
source ~/.bashrc
```

Then the agent can just run:
```bash
python3 paper_kv.py
```

The `OUTLAYER_API_KEY` environment variable is automatically picked up. No .env file needed.

**For CI/CD** (GitHub Actions, etc), set it as a repository secret and map it to the environment.

Output:
```
╔═══════════════════════════════════════════════╗
║   paper-kv — Paper Trading Bot (Python)       ║
║   Binance prices + NEAR KV via OutLayer       ║
╚═══════════════════════════════════════════════╝

  Account:    5c571cf253c3edb672df...
  KV store:   paper-kv.near
  Strategy:   momentum
  Leverage:   5.0x
  Trade size: $100.0
  Pairs:      BTCUSDT, ETHUSDT, SOLUSDT, NEARUSDT

── Loading state from KV ──
  New account — starting with $10000.0
  BTCUSDT: $76,978.05
  ETHUSDT: $2,305.99
  ...

▶  Running every 60s (Ctrl+C to stop)
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTLAYER_API_KEY` | — | OutLayer API key (gasless writes) |
| `NEAR_ACCOUNT` | auto-detected | Override account (uses OutLayer account by default) |
| `KV_CONTRACT` | `paper-kv.near` | KV storage contract |
| `INITIAL_BALANCE` | `10000` | Starting paper balance (USD) |
| `TRADE_SIZE` | `100` | USD collateral per trade |
| `DEFAULT_LEVERAGE` | `5` | Leverage multiplier |
| `MAX_OPEN_TRADES` | `5` | Max concurrent positions |
| `CHECK_INTERVAL_MS` | `60000` | Price check interval (ms) |
| `STRATEGY` | `momentum` | Strategy type |
| `MOMENTUM_LOOKBACK_MINUTES` | `30` | Momentum window |
| `MOMENTUM_THRESHOLD_PCT` | `0.5` | Min % move to trigger |
| `TRADE_PAIRS` | `BTCUSDT,ETHUSDT,SOLUSDT,NEARUSDT` | Binance symbols |

Example with custom settings:
```bash
OUTLAYER_API_KEY=wk_... TRADE_SIZE=50 DEFAULT_LEVERAGE=3 INITIAL_BALANCE=5000 python3 paper_kv.py
```

### Fallback: Using with a NEAR account instead of OutLayer

If you already have a NEAR account with a local keychain setup (near-cli-rs), you can skip OutLayer:

```bash
export NEAR_ACCOUNT=your-account.near
python3 paper_kv.py
```

This uses `near contract call-function` under the hood. Costs ~0.001 NEAR per KV write. Requires [near-cli-rs](https://docs.near.org/tools/near-cli-rs) installed and a keychain set up.

---

## Quick Start (Node.js — original)

### Prerequisites

- Node.js 18+
- A NEAR account with ~0.1 NEAR for gas

### Step 1: Install

```bash
git clone https://github.com/Kampouse/paper-kv.git
cd paper-kv
npm install
```

### Step 2: Configure

```bash
cp .env.example .env
```

Edit `.env`:
```
NEAR_ACCOUNT=your-account.near
NEAR_PRIVATE_KEY=ed25519:...
KV_CONTRACT=paper-kv.near
INITIAL_BALANCE=10000
```

Get a NEAR account: https://app.mynearwallet.com/create

### Step 3: Run

```bash
npm start
```

---

## View Results (no wallet needed)

Anyone can verify your trading results — reads are free, no authentication:

```bash
# View state (balance, trades, PnL)
curl https://kv.main.fastnear.com/v0/latest/paper-kv.near/ACCOUNT_ID/state

# View open positions
curl https://kv.main.fastnear.com/v0/latest/paper-kv.near/ACCOUNT_ID/positions

# View trade history
curl https://kv.main.fastnear.com/v0/latest/paper-kv.near/ACCOUNT_ID/trades

# Full history with timestamps
curl -s -X POST https://kv.main.fastnear.com/v0/history/paper-kv.near/ACCOUNT_ID \
  -H "Content-Type: application/json" \
  -d '{"key":"trades","asc":true,"limit":100}'
```

Replace `ACCOUNT_ID` with the Near account ID (for Node.js) or intents account ID (for Python/OutLayer).

---

## Architecture

```
Python version (zero deps):
  paper_kv.py — bot logic + OutLayer gasless KV writes

Node.js version:
  src/bot.js    — bot logic + near-api-js KV writes
  src/status.js — read-only viewer (no wallet needed)
```

**Price feed**: Binance public API (`api.binance.com/api/v3/ticker/price`) — no API key, ~225ms batch

**Storage (Python)**: OutLayer Agent Custody → `POST /wallet/v1/call` → calls `__fastdata_kv` on `paper-kv.near`. Transaction signing happens inside Intel TDX enclave. Agent never has the private key.

**Storage (Node.js)**: NEAR account + near-api-js → direct function call to `__fastdata_kv`. Costs ~0.001 NEAR per write.

```
┌──────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────┐
│ Binance  │────▶│  Python bot  │────▶│  OutLayer    │────▶│ NEAR KV  │
│ (prices) │     │  (strategy)  │     │  (TEE sign)  │     │ (public) │
└──────────┘     └──────────────┘     └──────────────┘     └──────────┘
                                            │
                                     ┌──────┴──────┐
                                     │  Policy     │
                                     │  (limits,   │
                                     │  whitelist) │
                                     └─────────────┘
```

## KV Storage Layout

| Key | Content |
|-----|---------|
| `state` | `{ balance, totalTrades, wins, losses, totalPnl }` |
| `positions` | `[{ id, symbol, direction, entryPrice, leverage, size, collateral, openedAt }]` |
| `trades` | `[{ ...position, exitPrice, pnl, pnlPct, closedAt, exitReason }]` |

## Momentum Strategy

1. Fetch prices from Binance every tick
2. Compare current to cached price from N minutes ago
3. % change = (current - old) / old * 100
4. change >= threshold + direction UP → open LONG
5. change >= threshold + direction DOWN → open SHORT
6. Open position reverses >= threshold → close

## Leverage, Liquidation & Funding

Positions simulate real perp mechanics:

**Liquidation price:**
- Long: `entry * (1 - 1/leverage + 0.005)`
- Short: `entry * (1 + 1/leverage - 0.005)`

| Leverage | Liq distance (from entry) |
|----------|--------------------------|
| 5x       | ~19.5%                   |
| 10x      | ~9.5%                    |
| 25x      | ~3.9%                    |
| 50x      | ~1.5%                    |

**Funding fees**: 0.01% of position size per 8h, prorated per tick.

## Python vs Node.js

| | Python | Node.js |
|---|---|---|
| Dependencies | None (stdlib only) | near-api-js |
| KV writes | OutLayer (gasless, TEE) | NEAR account (costs gas) |
| Private key needed | ❌ No | ✅ Yes |
| Setup time | 30 seconds | 5 minutes |
| Cost | Free tier + small NEAR for heavy use | ~0.001 NEAR per write |
| Best for | Quick start, CI/CD, agents | Full control, custom chains |

## License

MIT
