---
name: paper-kv
description: On-chain paper trading bot with Merkle-verified trade history. Use when asked about paper trading, backtesting crypto strategies, viewing on-chain trades, or setting up paper-kv for a user.
---

# paper-kv

On-chain paper trading bot. Every trade is written to NEAR KV with Merkle proofs — tamper-proof and publicly verifiable.

## Quick Start

```bash
git clone https://github.com/Kampouse/paper-kv.git
cd paper-kv
bash setup.sh
```

Setup walks through: wallet setup → KV verification → local replay → install daemon.

## Commands

```bash
# Replay 7 days of real Binance data (local only, no chain writes)
python3 paper-kv.py replay 7

# Run live (writes every tick on-chain)
python3 paper-kv.py live

# Check on-chain state
python3 paper-kv.py status
```

## Config

All config is in `.env` (created by setup):

```bash
STRATEGY=momentum       # Strategy name (from strategies/ folder)
PAIRS=BTC,ETH,SOL,wNEAR # Trading pairs
LEVERAGE=5              # Leverage
TRADE_SIZE=100          # Collateral per trade
TP_PCT=2.5              # Take profit % (leveraged)
SL_PCT=1.5              # Stop loss % (leveraged)
LOOKBACK_MIN=5          # Momentum lookback
THRESHOLD=0.15          # Min momentum % to trigger
MAX_OPEN=10             # Max concurrent positions
OUTLAYER_API_KEY=wk_... # OutLayer wallet key (from setup)
NEAR_ACCOUNT=           # Auto-filled by setup
```

## Strategies

Drop a file in `strategies/your_strategy.py`:

```python
def step(engine, prices, now_ms):
    """
    Called once per tick.
    
    engine.open(sym, direction, price, now_ms)  -> position or None
    engine.close(pos, price, reason, now_ms)     -> pnl or None
    engine.feed.momentum(sym, lookback_min, now_ms) -> (change_pct, "up"/"down"/"flat")
    engine.feed.cache[sym]  -> [{ts, close}, ...]
    engine.positions        -> [pos, ...]
    engine.state            -> {balance, totalTrades, wins, losses, totalPnl}
    """
    for sym in engine.config.get("pairs", []):
        price = prices.get(sym)
        if not price:
            continue
        change, direction = engine.feed.momentum(sym, 5, now_ms)
        if change is None:
            continue
        existing = next((p for p in engine.positions if p["symbol"] == sym), None)
        if not existing and abs(change) >= 0.5:
            engine.open(sym, "long" if change > 0 else "short", price, now_ms)
```

Run: `STRATEGY=your_strategy python3 paper-kv.py replay 7`

## On-Chain Data

All data is on NEAR blockchain via FastData KV, publicly readable:

```
https://kv.main.fastnear.com/v0/latest/contextual.near/{account}/state
https://kv.main.fastnear.com/v0/latest/contextual.near/{account}/positions
https://kv.main.fastnear.com/v0/latest/contextual.near/{account}/trades
```

### Verification

Every save includes a Merkle root chained with timestamps:
- `state.merkle_root` — hash of all trades + state
- `state.last_tick_ts` — timestamp of last save
- `state.last_prev_root` — previous root (chain)
- Each trade has `price_ts` — verifiable against Binance

Anyone can verify:
1. Recompute Merkle root from data (excluding merkle fields)
2. Match against stored root
3. Check `price_ts` against Binance candle history
4. Walk the root chain for integrity

## Architecture

```
paper-kv/
├── engine.py          # Core engine (no strategy logic)
├── paper-kv.py        # CLI: replay / live / status
├── merkle.py          # Merkle tree for tamper-proofing
├── replay_local.py    # Local replay (used by setup.sh)
├── setup.sh           # Onboarding flow
├── strategies/
│   ├── momentum.py
│   └── mean_reversion.py
├── dashboard/         # React dashboard (deployed to Cloudflare Pages)
└── .env               # Config (created by setup)
```

## Dashboard

Live at: https://paper-kv-dashboard.pages.dev

Auto-discovers all paper-kv accounts from KV. Shows balance, PnL, trade history, Merkle proofs.

## Daemon

Setup installs a background service:
- **macOS:** launchd (`~/Library/LaunchAgents/com.paper-kv.bot.plist`)
- **Linux:** systemd (`/etc/systemd/system/paper-kv-bot.service`)
- **Fallback:** nohup with PID file at `~/.paper-kv/bot.pid`

Logs: `~/.paper-kv/bot.log`

## Requirements

- Python 3.8+ (stdlib only, zero dependencies)
- OutLayer wallet (free, includes gas — created during setup)
- No pip installs, no node_modules for the bot
