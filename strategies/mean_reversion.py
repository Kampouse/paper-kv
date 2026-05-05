"""Mean reversion strategy — buy oversold, sell overbought, close on return to mean."""

def step(engine, prices, now_ms):
    c = engine.config
    lev = c.get("leverage", 5)
    lb = c.get("lookback_min", 30)
    tp = c.get("tp_pct", 3.0)
    sl = c.get("sl_pct", 2.0)
    rsi_low = c.get("rsi_low", 30)
    rsi_high = c.get("rsi_high", 70)
    bb_period = c.get("bb_period", 20)
    min_hold = c.get("min_hold_s", 120) * 1000
    pairs = c.get("pairs", ["BTC", "ETH", "SOL", "wNEAR"])

    for sym in pairs:
        price = prices.get(sym)
        if not price:
            continue

        # Get price history from feed cache
        pts = [p for p in engine.feed.cache.get(sym, []) if p["ts"] >= now_ms - lb * 60 * 1000]
        if len(pts) < bb_period:
            continue

        closes = [p["close"] for p in pts[-bb_period:]]
        mean = sum(closes) / len(closes)
        std = (sum((c - mean) ** 2 for c in closes) / len(closes)) ** 0.5
        if std == 0:
            continue
        upper = mean + 2 * std
        lower = mean - 2 * std

        # Simple RSI
        changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        if not changes:
            continue
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        avg_gain = sum(gains) / len(changes) if gains else 0
        avg_loss = sum(losses) / len(changes) if losses else 0.001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        existing = next((p for p in engine.positions if p["symbol"] == sym), None)

        if not existing:
            # Buy at lower band + oversold RSI
            if price <= lower and rsi <= rsi_low:
                engine.open(sym, "long", price, now_ms)
            # Short at upper band + overbought RSI
            elif price >= upper and rsi >= rsi_high:
                engine.open(sym, "short", price, now_ms)
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
            elif is_long and price >= mean:
                reason = "mean_reversion"
            elif not is_long and price <= mean:
                reason = "mean_reversion"
            if reason:
                engine.close(existing, price, reason, now_ms)
