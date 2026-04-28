# paper-kv

Paper trading bot with zero setup. Real Binance prices, results stored on NEAR blockchain via FastData KV — publicly verifiable, no fake data.

## How It Works

- Fetches live prices from Binance every 60s
- Runs momentum strategy (configurable)
- Opens/closes paper positions — no real money
- Saves all state to NEAR KV (positions, trades, balance)
- Anyone can verify results via the KV API

## Quick Start

```bash
npm install
cp .env.example .env
# Edit .env — needs a NEAR account (for KV writes, ~$0.001 per trade)
npm start
```

**You need a NEAR account** with a small amount of NEAR for transaction gas (~0.001 NEAR per state save, not per trade). Trades themselves are free — only the KV write costs gas.

Get a NEAR account: https://app.mynearwallet.com/create

## View Anyone's Results (no wallet needed)

```bash
# View your own
node src/status.js

# View anyone's
node src/status.js jemartel.near paper-kv.near
```

Or just hit the API directly:
```
https://kv.main.fastnear.com/v0/latest/paper-kv.near/jemartel.near/state
```

## Architecture

```
src/
├── bot.js     — main bot (price feed, strategy, KV storage)
└── status.js  — read-only viewer (no wallet needed)
```

**Price feed**: Binance public API — `api.binance.com/api/v3/ticker/price` — no API key, ~225ms batch

**Storage**: NEAR FastData KV — write via `__fastdata_kv` function call, read via free HTTP API

## KV Storage Layout

| Key | Content |
|-----|---------|
| `state` | `{ balance, totalTrades, wins, losses, totalPnl }` |
| `positions` | `[{ id, symbol, direction, entryPrice, leverage, size, collateral, openedAt }]` |
| `trades` | `[{ ...position, exitPrice, pnl, pnlPct, closedAt, exitReason }]` |

## Config (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `NEAR_ACCOUNT` | — | NEAR account for KV writes |
| `NEAR_PRIVATE_KEY` | — | Account private key (ed25519) |
| `KV_CONTRACT` | `paper-kv.near` | KV storage target (can be any account) |
| `INITIAL_BALANCE` | `10000` | Starting paper balance (USD) |
| `TRADE_SIZE` | `100` | USD collateral per trade |
| `DEFAULT_LEVERAGE` | `5` | Leverage multiplier |
| `MAX_OPEN_TRADES` | `5` | Max concurrent positions |
| `CHECK_INTERVAL_MS` | `60000` | Price check interval |
| `STRATEGY` | `momentum` | Strategy type |
| `MOMENTUM_LOOKBACK_MINUTES` | `30` | Momentum window |
| `MOMENTUM_THRESHOLD_PCT` | `0.5` | Min % move to trigger |
| `TRADE_PAIRS` | `BTCUSDT,ETHUSDT,SOLUSDT,NEARUSDT` | Binance symbols |

## Momentum Strategy

1. Fetch prices from Binance every tick
2. Compare current to cached price from N minutes ago
3. % change = (current - old) / old * 100
4. change >= threshold + direction UP → open LONG
5. change >= threshold + direction DOWN → open SHORT
6. Open position reverses >= threshold → close

## Public Verification

All trade data is on NEAR blockchain via FastData KV. Verify anyone's results:

```
GET https://kv.main.fastnear.com/v0/latest/{kv_contract}/{account}/trades
GET https://kv.main.fastnear.com/v0/latest/{kv_contract}/{account}/state
```

Full history with timestamps:
```
POST https://kv.main.fastnear.com/v0/history/{kv_contract}/{account}
{"key": "trades", "asc": true, "limit": 100}
```

## Leverage, Liquidation & Funding

Positions are opened with configurable leverage. This isn't just a multiplier — it simulates real perp mechanics:

**Liquidation price** (calculated on open):
- Long: `entry * (1 - 1/leverage + 0.005)`
- Short: `entry * (1 + 1/leverage - 0.005)`

If price crosses liquidation → position force-closed, lose entire collateral.

| Leverage | Liq distance (from entry) |
|----------|--------------------------|
| 5x       | ~19.5%                   |
| 10x      | ~9.5%                    |
| 25x      | ~3.9%                    |
| 50x      | ~1.5%                    |

**Funding fees**: 0.01% of position size per 8h, prorated per tick. Deducted from unrealized PnL continuously.

## License

MIT
