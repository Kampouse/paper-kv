---
name: paper-kv
description: Paper trading bot with pluggable strategies, on-chain KV storage via OutLayer, and historical replay. Use when asked about paper-kv, backtesting crypto strategies, or replaying trades on-chain.
---

# paper-kv

Paper trading bot that writes every trade to NEAR KV (on-chain, verifiable). Strategies are pluggable — swap them without touching the core engine.

## Quick Commands

```bash
cd ~/.openclaw/workspace/paper-kv

# Replay 7 days of real Binance data through the bot
python3 paper-kv.py replay 7

# Replay with different strategy
STRATEGY=mean_reversion python3 paper-kv.py replay 7

# Run live (fetches prices from NEAR Intents, writes to KV)
python3 paper-kv.py live

# Check on-chain status
python3 paper-kv.py status
```

## Architecture

```
paper-kv/
├── engine.py              # Core: Engine, PriceFeed, KV client — no strategy logic
├── paper-kv               # CLI: replay / live / status
├── strategies/
│   ├── __init__.py
│   ├── momentum.py        # Momentum (default)
│   └── mean_reversion.py  # BB + RSI (needs tuning)
└── state.json             # Local cache
```

## On-Chain Data

- **Account:** `REDACTED_ACCOUNT`
- **Contract:** `contextual.near`
- **OutLayer key:** `wk_169d...` (in `.env`)
- **KV URL:** `https://kv.main.fastnear.com/v0/latest/contextual.near/{account}/state`
- **Keys:** `state` (balance, PnL, win/loss), `positions` (open), `trades` (history)

## Config (env vars)

| Var | Default | Description |
|-----|---------|-------------|
| `STRATEGY` | `momentum` | Strategy module name in `strategies/` |
| `PAIRS` | `BTC,ETH,SOL,wNEAR` | Comma-separated trading pairs |
| `LEVERAGE` | `5` | Leverage multiplier |
| `TRADE_SIZE` | `100` | Collateral per trade (USD) |
| `INITIAL_BALANCE` | `10000` | Starting balance for replay |
| `TP_PCT` | `2.5` | Take profit % (leveraged) |
| `SL_PCT` | `1.5` | Stop loss % (leveraged) |
| `LOOKBACK_MIN` | `15` | Momentum lookback window |
| `THRESHOLD` | `0.3` | Min momentum % to trigger |
| `MAX_OPEN` | `10` | Max concurrent positions |
| `MIN_HOLD_S` | `120` | Min hold time before closing |
| `OUTLAYER_API_KEY` | (in .env) | OutLayer wallet key |
| `NEAR_ACCOUNT` | (hex) | OutLayer wallet account |

## Writing a Strategy

Drop a file in `strategies/your_strategy.py`:

```python
def step(engine, prices, now_ms):
    """
    Called once per tick with current prices.
    
    Args:
        engine: Engine instance — use engine.open(), engine.close(), engine.feed
        prices: {symbol: current_price} dict
        now_ms: Current timestamp in milliseconds
    """
    config = engine.config
    leverage = config.get("leverage", 5)
    pairs = config.get("pairs", ["BTC", "ETH", "SOL", "wNEAR"])
    
    for sym in pairs:
        price = prices.get(sym)
        if not price:
            continue
        
        # Your logic here...
        # Open: engine.open(symbol, direction, price, now_ms)
        # Close: engine.close(position, price, reason, now_ms)
        # Momentum: change, direction = engine.feed.momentum(symbol, lookback_min, now_ms)
        # History: engine.feed.cache[symbol] -> [{ts, close}, ...]
```

Then run: `STRATEGY=your_strategy python3 paper-kv.py replay 7`

### Engine API

| Method | Description |
|--------|-------------|
| `engine.open(sym, direction, price, now_ms)` | Open position, returns pos dict or None |
| `engine.close(pos, price, reason, now_ms)` | Close position, returns pnl or None |
| `engine.feed.momentum(sym, lookback_min, now_ms)` | Returns `(change_pct, "up"/"down"/"flat")` |
| `engine.feed.push(sym, ts_ms, price)` | Add price to cache |
| `engine.feed.cache[sym]` | List of `[{ts, close}, ...]` |
| `engine.positions` | List of open position dicts |
| `engine.state` | `{balance, totalTrades, wins, losses, totalPnl}` |
| `engine.trades` | List of closed trade dicts |
| `engine.save()` | Persist to KV + local file |

## Known Issues

- `kampouse.near` key file was corrupted (base64 instead of base58) — use OutLayer wallet instead
- Mean reversion strategy needs parameter tuning (doesn't trigger with default RSI 30/70 on 5m candles)
- Momentum strategy has ~36% WR — strategy itself needs improvement, not the engine
