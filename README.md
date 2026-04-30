# paper-kv

Paper trading bot with zero setup. Real prices from NEAR Intents, results stored on NEAR blockchain via FastData KV — publicly verifiable, no fake data.

**Python only** — zero dependencies (stdlib), no npm, no node_modules.

## How It Works

- Fetches live prices from NEAR Intents 1Click API every 60s (156 tokens across 25+ chains)
- Runs momentum strategy (configurable)
- Opens/closes paper positions — no real money
- Saves all state to NEAR KV (positions, trades, balance)
- Anyone can verify results via the free KV HTTP API

### Why NEAR Intents for prices?

[Binance is being replaced](https://docs.near-intents.org/api-reference/oneclick/get-supported-tokens) with the NEAR Intents token list API. Benefits:

- **No API key** — public endpoint, no rate limit concerns
- **Multi-chain prices** — BTC, ETH, SOL, wNEAR, USDC, USDT, and 150+ tokens
- **Cross-chain native** — prices from NEAR's intent-based swap infrastructure
- **No vendor lock-in** — not tied to any single CEX

### Why NEAR FastData KV?

[FastData KV](https://kv.main.fastnear.com) is a free, public key-value storage built on NEAR blockchain. It lets any NEAR contract store JSON data via a simple `__fastdata_kv` function call, and anyone can read it back via a free HTTP API.

- **Publicly verifiable** — your trading history is on-chain. Nobody can fake results
- **Free to read** — no API key needed, just `curl` the URL
- **Cheap to write** — one NEAR contract call per tick (~0.001 NEAR, or gasless via OutLayer)
- **No server needed** — no database to host, no backend to maintain
- **Versioned** — every write is timestamped on-chain with full history
- **Built for agents** — simple JSON in/out, designed for programs not humans

---

## Quick Start (Python — only version)

### Prerequisites

- Python 3.8+ (no pip installs needed — stdlib only)

### What is OutLayer?

[OutLayer](https://outlayer.fastnear.com) is a custody service for AI agents built on NEAR. It gives your bot a wallet secured inside a **TEE (Trusted Execution Environment)** — Intel TDX enclave. Your agent authenticates with an API key, but never sees a private key.

For paper-kv, OutLayer provides:
- **Gasless contract calls** — write to KV without holding NEAR for gas
- **No private key management** — the API key is the only secret
- **Policy controls** — set spending limits, whitelist contracts, freeze wallet

### One-Script Setup (for agents)

```bash
bash setup.sh run       # register + configure + run
bash setup.sh verify    # integrity check
bash setup.sh tokens    # list supported tokens
bash setup.sh status    # view results
```

That's it — one command, zero human interaction.

**If you already have an OutLayer API key**, setup.sh detects it automatically:

```bash
# Option 1: exported in shell (won't overwrite)
export OUTLAYER_API_KEY=wk_...
bash setup.sh run

# Option 2: already in .env (won't re-register)
bash setup.sh run

# Option 3: no key at all (registers a new wallet)
bash setup.sh run
```

### Manual Setup (for humans)

#### Step 1: Clone

```bash
git clone https://github.com/Kampouse/paper-kv.git
cd paper-kv
```

#### Step 2: Get an OutLayer wallet

**Option A — Web:**
Go to [outlayer.fastnear.com](https://outlayer.fastnear.com) and click **Register**.

**Option B — CLI:**
```bash
curl -s -X POST https://api.outlayer.fastnear.com/register
```

You'll get an **API key** (`wk_...`) — save it.

#### Step 3: Configure

```bash
cp .env.example .env
# Edit .env — set OUTLAYER_API_KEY
```

#### Step 4: Run

```bash
python3 paper_kv.py
```

### Subcommands

```bash
python3 paper_kv.py          # run the bot
python3 paper_kv.py verify   # system integrity check
python3 paper_kv.py tokens   # list all supported tokens with prices
python3 paper_kv.py status [account]  # read-only view of results
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
| `TRADE_PAIRS` | `BTC,ETH,SOL,wNEAR` | NEAR Intents token symbols |

Example with custom settings:
```bash
OUTLAYER_API_KEY=*** TRADE_SIZE=50 DEFAULT_LEVERAGE=3 python3 paper_kv.py
```

### After Setup: Fund & Configure OutLayer

The bot won't write to KV until the OutLayer wallet is funded and the policy allows it.

#### 1. Fund the wallet

Basic usage is free (trial credits). For 24/7 operation:

1. Open your OutLayer dashboard (shown during registration, or visit [outlayer.fastnear.com](https://outlayer.fastnear.com))
2. Copy your NEAR deposit address
3. Send ~0.5 NEAR (lasts weeks at 1 write/minute)

Check pricing at [dashboard.fastnear.com](https://dashboard.fastnear.com).

#### 2. Set up auto-approve policy

Without this, every KV write requires manual approval from the dashboard. With it, the bot runs hands-free.

1. Go to **Policy** in your OutLayer dashboard
2. Add `paper-kv.near` to the **address whitelist**
3. Allow **`call`** as a transaction type
4. Save

**Verify it works:**
```bash
bash setup.sh verify
```

If `OutLayer Auth` passes, you're good.

### Fallback: Using with a NEAR account instead of OutLayer

```bash
export NEAR_ACCOUNT=your-account.near
python3 paper_kv.py
```

Uses `near contract call-function` under the hood. Requires [near-cli-rs](https://docs.near.org/tools/near-cli-rs).

---

## View Results (no wallet needed)

```bash
# Via subcommand
python3 paper_kv.py status

# Via curl
curl https://kv.main.fastnear.com/v0/latest/paper-kv.near/ACCOUNT_ID/state

# Full history
curl -s -X POST https://kv.main.fastnear.com/v0/history/paper-kv.near/ACCOUNT_ID \
  -H "Content-Type: application/json" \
  -d '{"key":"trades","asc":true,"limit":100}'
```

---

## System Integrity Check

```bash
python3 paper_kv.py verify
```

Checks:
1. **NEAR Intents API** — reachable, tokens available
2. **KV Read API** — reachable
3. **Price Feed** — all configured pairs returning prices
4. **OutLayer Auth** — API key valid, account resolved
5. **State Consistency** — balance non-negative, trade counts match, PnL sane
6. **Position Validation** — liquidation prices mathematically correct
7. **KV Write Roundtrip** — writes a probe value, reads it back, confirms match

If check 7 fails, the wallet needs funding or auto-approve policy (see above). No check is faked — every failure is a real failure.

---

## Architecture

```
paper_kv.py — single file, zero deps
  ├── PriceFeed      — NEAR Intents 1Click API (156 tokens, 25+ chains)
  ├── IntegrityChecker — system health verification
  ├── PaperBot       — strategy engine + position management
  └── KV client      — OutLayer gasless writes / near-cli-rs fallback
```

```
┌───────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────┐
│ NEAR Intents  │────▶│  Python bot  │────▶│  OutLayer    │────▶│ NEAR KV  │
│ (156 tokens)  │     │  (strategy)  │     │  (TEE sign)  │     │ (public) │
└───────────────┘     └──────────────┘     └──────────────┘     └──────────┘
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

1. Fetch prices from NEAR Intents every tick
2. Compare current to cached price from N minutes ago
3. % change = (current - old) / old * 100
4. change >= threshold + direction UP → open LONG
5. change >= threshold + direction DOWN → open SHORT
6. Open position reverses >= threshold → close

## Leverage, Liquidation & Funding

| Leverage | Liq distance (from entry) |
|----------|--------------------------|
| 5x       | ~19.5%                   |
| 10x      | ~9.5%                    |
| 25x      | ~3.9%                    |
| 50x      | ~1.5%                    |

Funding fees: 0.01% of position size per 8h, prorated per tick.

## License

MIT
