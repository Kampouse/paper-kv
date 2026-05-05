# paper-kv

Paper trading bot with on-chain verifiable results. Real prices from NEAR Intents, every trade stored on NEAR blockchain via FastData KV.

**Zero dependencies** — Python stdlib only.

## Quick Start

```bash
git clone https://github.com/Kampouse/paper-kv.git
cd paper-kv
bash setup.sh
```

`setup.sh` walks you through:
1. **OutLayer wallet** — paste existing key or register new (free)
2. **Verify KV** — test write/read to confirm on-chain works
3. **Local replay** — 7-day simulation with real Binance data, no chain writes
4. **Go live** — start trading with real-time prices

## Architecture

```
paper-kv/
├── engine.py              # Core: Engine, PriceFeed, KV client
├── paper-kv               # CLI entry point
├── strategies/
│   ├── __init__.py
│   ├── momentum.py        # Momentum (default)
│   └── mean_reversion.py  # BB + RSI
├── setup.sh               # Onboarding flow
├── SKILL.md               # Agent skill reference
└── .env                   # Your config (created by setup)
```

The engine is clock-injected — same code path for replay and live. Strategies are pluggable modules, no engine changes needed.

## Commands

```bash
python3 paper-kv.py replay 7          # Replay 7 days + write to chain
STRATEGY=mean_reversion python3 paper-kv.py replay 7  # Different strategy
python3 paper-kv.py live              # Run live (Ctrl+C saves state)
python3 paper-kv.py status            # Check on-chain state
```

## Config

Set in `.env` or via environment variables:

| Var | Default | Description |
|-----|---------|-------------|
| `STRATEGY` | `momentum` | Strategy module in `strategies/` |
| `PAIRS` | `BTC,ETH,SOL,wNEAR` | Trading pairs |
| `LEVERAGE` | `5` | Leverage multiplier |
| `TRADE_SIZE` | `100` | Collateral per trade (USD) |
| `INITIAL_BALANCE` | `10000` | Starting balance for replay |
| `TP_PCT` | `2.5` | Take profit % (leveraged) |
| `SL_PCT` | `1.5` | Stop loss % (leveraged) |
| `LOOKBACK_MIN` | `15` | Momentum lookback window |
| `THRESHOLD` | `0.3` | Min momentum % to trigger |
| `MAX_OPEN` | `10` | Max concurrent positions |
| `MIN_HOLD_S` | `120` | Min hold before closing |

## Writing a Strategy

Drop a file in `strategies/your_strategy.py`:

```python
def step(engine, prices, now_ms):
    """Called once per tick."""
    for sym in engine.config.get("pairs", ["BTC"]):
        price = prices.get(sym)
        if not price:
            continue

        # Momentum indicator
        change, direction = engine.feed.momentum(sym, 15, now_ms)
        if change is None:
            continue  # not enough history yet

        existing = next((p for p in engine.positions if p["symbol"] == sym), None)

        if not existing:
            if abs(change) >= 0.5:
                engine.open(sym, "long" if change > 0 else "short", price, now_ms)
        else:
            # Close conditions...
            engine.close(existing, price, "reason", now_ms)
```

Run: `STRATEGY=your_strategy python3 paper-kv.py replay 7`

### Engine API

| Method | Description |
|--------|-------------|
| `engine.open(sym, direction, price, now_ms)` | Open position |
| `engine.close(pos, price, reason, now_ms)` | Close position |
| `engine.feed.momentum(sym, lookback_min, now_ms)` | `(change_pct, "up"/"down"/"flat")` or `(None, "no_data")` |
| `engine.feed.cache[sym]` | Price history `[{ts, close}, ...]` |
| `engine.positions` | Open positions |
| `engine.state` | `{balance, totalTrades, wins, losses, totalPnl}` |
| `engine.trades` | Closed trades |
| `engine.save()` | Persist to KV + local file |

## On-Chain Data

Every trade is written to NEAR FastData KV — publicly verifiable, no fake data.

```
https://kv.main.fastnear.com/v0/latest/contextual.near/{account}/state
https://kv.main.fastnear.com/v0/latest/contextual.near/{account}/positions
https://kv.main.fastnear.com/v0/latest/contextual.near/{account}/trades
```

### What is FastData KV?

[FastData KV](https://kv.main.fastnear.com) is free key-value storage on NEAR blockchain. Write via `__fastdata_kv` contract call, read via HTTP API. Every write is on-chain, timestamped, publicly readable.

### What is OutLayer?

[OutLayer](https://outlayer.fastnear.com) provides gasless wallets for AI agents inside TEE enclaves. Your bot authenticates with an API key (`wk_...`) but never sees a private key. Supports policy controls (auto-approve, spending limits, contract whitelists).

## Error Handling

The engine is crash-resistant:

- **Strategy throws** → logged, tick skipped, bot continues
- **KV write fails** → retried 2x, local save guaranteed, circuit breaker after 10 consecutive failures
- **KV read fails on load** → falls back to local `state.json`
- **Double close** → returns `None`, logged, no crash
- **Invalid inputs** (negative price, bad direction) → rejected
- **Bad strategy name** → clear ImportError
- **No prices fetched** → tick skipped in live mode
- **Network down** → all curl errors caught, clear error messages

## License

MIT
