---
name: paper-kv
description: "Paper trading bot — Binance prices + NEAR FastData KV storage. Zero setup, publicly verifiable results."
---

# paper-kv — Paper Trading Bot

Pure paper trading: real Binance prices, results stored on NEAR blockchain via FastData KV. No real money, no blockchain trades, no gas for trades. Only KV writes cost gas (~0.001 NEAR).

## Project Location

`~/dev/paper-kv/` — JavaScript (ESM), repo: Kampouse/paper-kv

## Quick Commands

```bash
cd ~/dev/paper-kv
npm install
cp .env.example .env  # add NEAR account + key
npm start             # run bot
node src/status.js    # view results (no wallet needed)
node src/status.js jemartel.near contextual.near  # view anyone
```

## Architecture

```
src/
├── bot.js     — main bot (Binance feed, momentum strategy, KV storage)
└── status.js  — read-only viewer (no wallet needed, reads KV via HTTP)
```

**Price feed**: Binance public API `api.binance.com/api/v3/ticker/price` — no API key, ~225ms batch
**Storage**: NEAR FastData KV — write via `__fastdata_kv` function call, read via free HTTP GET/POST
**No blockchain trades** — positions are calculated locally, only state is stored on-chain

## KV API

Base: `https://kv.main.fastnear.com`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v0/latest/{contract}/{account}/{key}` | GET | Read latest value |
| `/v0/multi` | POST | Batch read (up to 100 keys) |
| `/v0/history/{contract}/{account}` | POST | Full history (paginated) |

**Writing**: Call `__fastdata_kv` on any NEAR account via transaction. JSON root keys = KV keys. Target account doesn't need a contract or even exist.

## KV Storage Layout

| Key | Content |
|-----|---------|
| `state` | `{ balance, totalTrades, wins, losses, totalPnl }` |
| `positions` | Array of open positions |
| `trades` | Array of closed trades with PnL |

## Config (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `NEAR_ACCOUNT` | — | NEAR account for KV writes |
| `NEAR_PRIVATE_KEY` | — | Account private key |
| `KV_CONTRACT` | `contextual.near` | KV storage target |
| `INITIAL_BALANCE` | `10000` | Starting paper balance (USD) |
| `TRADE_SIZE` | `100` | USD per trade |
| `DEFAULT_LEVERAGE` | `5` | Leverage |
| `MAX_OPEN_TRADES` | `5` | Max concurrent |
| `CHECK_INTERVAL_MS` | `60000` | Tick interval |
| `STRATEGY` | `momentum` | Strategy type |
| `MOMENTUM_LOOKBACK_MINUTES` | `30` | Momentum window |
| `MOMENTUM_THRESHOLD_PCT` | `0.5` | Trigger threshold |
| `TRADE_PAIRS` | `BTCUSDT,ETHUSDT,SOLUSDT,NEARUSDT` | Binance symbols |

## Momentum Strategy

1. Fetch Binance prices every tick
2. Compare to cached price from N minutes ago
3. % change >= threshold + UP → LONG
4. % change >= threshold + DOWN → SHORT
5. Open position reverses >= threshold → close

PnL calculation: `(exitPrice - entryPrice) / entryPrice * leverage * 100` for longs, reversed for shorts.

## Liquidation & Funding

Real perp mechanics — not just a PnL multiplier:

**Liquidation price** (set on open):
- Long: `entry * (1 - 1/leverage + 0.005)` — 0.005 = maintenance margin
- Short: `entry * (1 + 1/leverage - 0.005)`
- Checked every tick against existing price (no extra API call)
- Liquidated = lose entire collateral, -100% PnL, reason: "liquidated"

**Funding fees**: 0.01% of position size per 8h, prorated per tick.

| Leverage | Liq distance |
|----------|-------------|
| 5x       | ~19.5%      |
| 10x      | ~9.5%       |
| 25x      | ~3.9%       |
| 50x      | ~1.5%       |

## Position/Trade Schema

```json
{
  "id": "1714300000000-a8f3k2",
  "symbol": "NEARUSDT",
  "direction": "long",
  "entryPrice": 1.35,
  "leverage": 5,
  "size": 500,
  "collateral": 100,
  "openedAt": "2026-04-28T15:30:00.000Z",
  "exitPrice": 1.42,
  "pnl": 25.93,
  "pnlPct": 25.93,
  "closedAt": "...",
  "exitReason": "momentum_reversal"
}
```

## Monitoring

```bash
# View results
node src/status.js

# View via API (no install needed)
curl -s 'https://kv.main.fastnear.com/v0/latest/contextual.near/jemartel.near/state' | python3 -m json.tool

# Full trade history
curl -s -X POST 'https://kv.main.fastnear.com/v0/history/contextual.near/jemartel.near' \
  -H 'Content-Type: application/json' \
  -d '{"key":"trades","limit":50,"asc":true}' | python3 -m json.tool
```

## Pitfalls

- **NEAR key format**: Must be ed25519 private key (starts with `ed25519:`). NOT the seed phrase.
- **KV gas**: Each state save is ~0.001 NEAR. Not per trade — trades are batched and saved once per tick.
- **KV contract doesn't need to exist**: `__fastdata_kv` works on any account, even non-existent ones. The data is indexed from the transaction, not contract state.
- **Binance rate limits**: 1200 weight/min, batch fetch = 2 weight. No issues at 1/min.
- **Value serialization**: KV stores raw JSON. Values come back as parsed objects (not strings).
- **History dedup**: The history endpoint returns all writes. Same key written multiple times = multiple entries. Dedup by trade `id` or latest `block_height`.
- **Batch writes**: Use single tx with multiple JSON keys to save gas. Don't call put() multiple times per tick.
