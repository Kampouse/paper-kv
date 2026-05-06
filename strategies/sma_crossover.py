"""Dual Moving Average Crossover strategy -- go long when fast SMA crosses above slow SMA,
go short when fast SMA crosses below slow SMA. Close on TP/SL or crossback."""

def _sma(prices, period):
    """Simple moving average from price list."""
    if len(prices) < period:
        return None
    return sum(p["close"] for p in prices[-period:]) / period

def step(engine, prices, now_ms):
    c = engine.config
    lev = c.get("leverage", 5)
    fast_period = c.get("fast_period", 10)
    slow_period = c.get("slow_period", 30)
    tp = c.get("tp_pct", 3.0)
    sl = c.get("sl_pct", 1.5)
    min_hold = c.get("min_hold_s", 180) * 1000
    pairs = c.get("pairs", ["BTC", "ETH", "SOL", "wNEAR"])

    for sym in pairs:
        price = prices.get(sym)
        if not price:
            continue

        cache = engine.feed.cache.get(sym, [])
        if len(cache) < slow_period + 1:
            continue

        fast_now = _sma(cache, fast_period)
        fast_prev = _sma(cache[:-1], fast_period) if len(cache) > fast_period else None
        slow_now = _sma(cache, slow_period)
        slow_prev = _sma(cache[:-1], slow_period) if len(cache) > slow_period else None

        if None in (fast_now, fast_prev, slow_now, slow_prev):
            continue

        cross_up = fast_prev <= slow_prev and fast_now > slow_now
        cross_down = fast_prev >= slow_prev and fast_now < slow_now

        existing = next((p for p in engine.positions if p["symbol"] == sym), None)

        if not existing:
            if cross_up:
                engine.open(sym, "long", price, now_ms)
            elif cross_down:
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
            elif is_long and cross_down:
                reason = "sma_crossback"
            elif not is_long and cross_up:
                reason = "sma_crossback"

            if reason:
                engine.close(existing, price, reason, now_ms)
