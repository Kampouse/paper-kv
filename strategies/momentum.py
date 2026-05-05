"""Momentum strategy — open in direction of momentum, close on reversal/TP/SL."""

def step(engine, prices, now_ms):
    c = engine.config
    lev = c.get("leverage", 5)
    thr = c.get("threshold", 0.3)
    lb = c.get("lookback_min", 15)
    tp = c.get("tp_pct", 2.5)
    sl = c.get("sl_pct", 1.5)
    min_hold = c.get("min_hold_s", 120) * 1000
    pairs = c.get("pairs", ["BTC", "ETH", "SOL", "wNEAR"])

    for sym in pairs:
        price = prices.get(sym)
        if not price:
            continue
        change, direction = engine.feed.momentum(sym, lb, now_ms)
        if change is None:
            continue  # insufficient history
        existing = next((p for p in engine.positions if p["symbol"] == sym), None)

        if not existing:
            if abs(change) >= thr:
                engine.open(sym, "long" if direction == "up" else "short", price, now_ms)
        else:
            from datetime import datetime
            opened_ms = datetime.fromisoformat(existing["openedAt"]).timestamp() * 1000
            if now_ms - opened_ms < min_hold:
                continue
            is_long = existing["direction"] == "long"
            pnl_pct = ((price - existing["entryPrice"]) / existing["entryPrice"] * lev * 100 if is_long
                       else (existing["entryPrice"] - price) / existing["entryPrice"] * lev * 100)
            reason = None
            if pnl_pct >= tp:
                reason = "take_profit"
            elif pnl_pct <= -sl:
                reason = "stop_loss"
            elif (is_long and direction == "down" and abs(change) >= thr) or \
                 (not is_long and direction == "up" and abs(change) >= thr):
                reason = "momentum_reversal"
            if reason:
                engine.close(existing, price, reason, now_ms)
