# paper-kv

Paper trading bot with zero setup. Real Binance prices, results stored on NEAR blockchain via FastData KV — publicly verifiable, no fake data.

**Two versions:** Node.js (original) and Python (gasless via OutLayer).

## How It Works

- Fetches live prices from Binance every 60s
- Runs momentum strategy (configurable)
- Opens/closes paper positions — no real money
- Saves all state to NEAR KV (positions, trades, balance)
- Anyone can verify results via the free KV HTTP API

## Quick Start (Python — recommended)

### Prerequisites

- Python 3.8+
- An OutLayer API key (free, gasless — see below)

### Step 1: Get an OutLayer API key

Register a wallet (one command, no NEAR account needed):

```bash
curl -s -X POST https://api.outlayer.fastnear.com/register
```

Response:
```json
{
  "api_key": "wk_15807dbda492...",
  "near_account_id": "36842e2f73d0b7...",
  "handoff_url": "https://outlayer.fastnear.com/wallet?key=wk_..."
}
```

**Save the `api_key` — it's shown only once.** This gives you a gasless, TEE-secured wallet. No private keys, no NEAR account setup, nothing.

### Step 2: Configure the policy (optional but recommended)

Open the `handoff_url` in your browser. In the dashboard, update the policy to auto-approve contract calls to `paper-kv.near`:

```json
{
  "rules": {
    "transaction_types": ["call", "intents_swap", "intents_deposit"],
    "addresses": {
      "mode": "whitelist",
      "list": ["paper-kv.near"]
    }
  }
}
```

This lets the bot write to KV without manual approval each time.

### Step 3: Run the bot

```bash
export OUTLAYER_API_KEY=wk_your_key_here
python3 paper_kv.py
```

That's it. No npm install, no .env file, no wallet setup.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTLAYER_API_KEY` | — | OutLayer API key (gasless writes) |
| `NEAR_ACCOUNT` | auto | Override account (uses OutLayer account by default) |
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

---

## Quick Start (Node.js — original)

### Prerequisites

- Node.js 18+
- A NEAR account with ~0.1 NEAR for gas

### Step 1: Install

```bash
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

```bash
# View your own (Python — use the account ID from register)
curl https://kv.main.fastnear.com/v0/latest/paper-kv.near/YOUR_ACCOUNT_ID/state

# View anyone's
curl https://kv.main.fastnear.com/v0/latest/paper-kv.near/jemartel.near/state

# Get trade history
curl https://kv.main.fastnear.com/v0/latest/paper-kv.near/jemartel.near/trades
```

## Architecture

```
Python version:
  paper_kv.py   — bot logic + OutLayer KV writes (no deps)

Node.js version:
  src/bot.js    — bot logic + near-api-js KV writes
  src/status.js — read-only viewer
```

**Price feed**: Binance public API — `api.binance.com/api/v3/ticker/price` — no API key needed

**Storage (Python)**: OutLayer Agent Custody — gasless contract calls to `paper-kv.near`'s `__fastdata_kv`. Keys stored in TEE, no private key exposure.

**Storage (Node.js)**: NEAR account + near-api-js — direct function call, costs ~0.001 NEAR per write.

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
| Best for | Quick start, CI/CD, agents | Full control, custom chains |

## License

MIT
