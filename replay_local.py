#!/usr/bin/env python3
"""Local replay — simulates without KV writes. Used by setup.sh step 3."""
import sys
import os
import logging

logging.basicConfig(level=logging.ERROR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Engine, PriceFeed
from datetime import datetime, timezone

# Load config from .env
env = {}
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

pairs = env.get("PAIRS", "BTC,ETH,SOL,wNEAR").split(",")
config = {
    "strategy": env.get("STRATEGY", "momentum"),
    "pairs": pairs,
    "leverage": float(env.get("LEVERAGE", "5")),
    "trade_size": float(env.get("TRADE_SIZE", "100")),
    "initial_balance": float(env.get("INITIAL_BALANCE", "10000")),
    "tp_pct": float(env.get("TP_PCT", "2.5")),
    "sl_pct": float(env.get("SL_PCT", "1.5")),
    "lookback_min": int(env.get("LOOKBACK_MIN", "5")),
    "threshold": float(env.get("THRESHOLD", "0.15")),
    "min_hold_s": int(env.get("MIN_HOLD_S", "120")),
    "max_open": int(env.get("MAX_OPEN", "10")),
    "near_account": "",
    "kv_contract": "contextual.near",
    "outlayer_api_key": "",
    "outlayer_api": "https://api.outlayer.fastnear.com",
}

print("Fetching candles...")
candles = PriceFeed.fetch_history(pairs, 7, "5m")
print()
print("Running simulation (local only, no chain writes)...")
print()

eng = Engine(config)


def on_save(i, total, ts):
    s = eng.state
    wr = (s["wins"] / s["totalTrades"] * 100) if s["totalTrades"] > 0 else 0
    ts_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    sign = "+" if s["totalPnl"] >= 0 else ""
    print(
        f"  {ts_str} | {i}/{total} | ${s['balance']:.2f} | "
        f"{s['totalTrades']} trades ({wr:.0f}% WR) | PnL: ${sign}{s['totalPnl']:.2f}"
    )


eng.replay(candles, save_every=500, on_save=on_save)
s = eng.state
wr = (s["wins"] / s["totalTrades"] * 100) if s["totalTrades"] > 0 else 0
sign = "+" if s["totalPnl"] >= 0 else ""
print()
print(
    f"  ${s['balance']:.2f} | PnL: ${sign}{s['totalPnl']:.2f} | "
    f"{s['totalTrades']} trades ({s['wins']}W/{s['losses']}L) {wr:.1f}% WR"
)
print("  Results in state.json (local only)")
