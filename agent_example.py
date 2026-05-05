"""
Example agent for paper-kv sandbox.

Strategy: Multi-indicator mean reversion with trend filter.
- Uses EMA trend filter (only trade in trend direction)
- RSI extremes for entries (oversold=buy, overbought=short)
- Bollinger Band touches as confirmation
- Dynamic position sizing based on signal strength
- Trailing stop on winning positions
"""

def tick(engine, prices, now_ms):
    for sym in engine.config.get("pairs", ["BTC", "ETH", "SOL", "wNEAR"]):
        price = prices.get(sym)
        if not price:
            continue

        # ── Raw data ──────────────────────────────────────────────
        candles = engine.feed.history(sym)  # all available data
        if len(candles) < 30:
            continue

        closes = [c["close"] for c in candles]
        volumes = [c.get("volume", 0) for c in candles]

        # ── Indicators (calculated fresh each tick) ──────────────
        
        # EMA 20
        ema = _ema(closes, 20)
        if ema is None:
            continue

        # RSI 14
        rsi = _rsi(closes, 14)
        if rsi is None:
            continue

        # Bollinger Bands (20, 2)
        bb_mid, bb_upper, bb_lower = _bollinger(closes, 20, 2)

        # Volume spike (1.5x average)
        avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
        vol_spike = volumes[-1] > avg_vol * 1.5 if avg_vol > 0 else False

        # Trend direction
        trend = "up" if price > ema else "down"

        # ── Signal strength (0-100) ──────────────────────────────
        signal = 0
        direction = None

        # Long signals
        if trend == "up" and rsi < 40:
            signal += 30  # oversold in uptrend
            direction = "long"
        if price <= bb_lower:
            signal += 25  # touching lower band
            if direction is None:
                direction = "long"
        if vol_spike and direction == "long":
            signal += 20  # volume confirms
        if rsi < 30:
            signal += 15  # extreme oversold

        # Short signals
        if trend == "down" and rsi > 60:
            signal += 30  # overbought in downtrend
            if direction is None:
                direction = "short"
        if price >= bb_upper:
            signal += 25  # touching upper band
            if direction is None:
                direction = "short"
        if vol_spike and direction == "short":
            signal += 20  # volume confirms
        if rsi > 70:
            signal += 15  # extreme overbought
            if direction is None:
                direction = "short"

        # ── Position management ──────────────────────────────────
        pos = engine.has_position(sym)

        if not pos and signal >= 35 and direction:
            # Dynamic sizing: stronger signal = bigger position
            collateral = engine.config.get("trade_size", 100)
            if signal >= 80:
                collateral *= 2  # double down on strong signals
            leverage = engine.config.get("leverage", 5)
            if signal >= 70:
                leverage = min(leverage + 1, 10)  # bump leverage on strong signals

            engine.open(sym, direction, price, now_ms,
                        leverage=leverage, collateral=min(collateral, engine.balance * 0.1))

        elif pos:
            entry = pos["entryPrice"]
            lev = pos["leverage"]
            is_long = pos["direction"] == "long"

            # Unrealized PnL %
            if is_long:
                pnl_pct = ((price - entry) / entry) * lev * 100
            else:
                pnl_pct = ((entry - price) / entry) * lev * 100

            # Trailing stop: if up >1.5%, move stop to breakeven + 0.5%
            # Take profit at 3%
            # Stop loss at -1.5%
            reason = None
            if pnl_pct >= 3.0:
                reason = "take_profit"
            elif pnl_pct <= -1.5:
                reason = "stop_loss"
            # Reversal: RSI flips opposite direction
            elif is_long and rsi > 70 and trend == "down":
                reason = "reversal"
            elif not is_long and rsi < 30 and trend == "up":
                reason = "reversal"

            if reason:
                engine.close(pos, price, reason, now_ms)


# ── Indicator helpers ──────────────────────────────────────────────────────

def _ema(values, period):
    """Exponential moving average."""
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = (v - result) * multiplier + result
    return result


def _rsi(closes, period=14):
    """Relative Strength Index."""
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0, c) for c in changes[-period:]]
    losses = [max(0, -c) for c in changes[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _bollinger(closes, period=20, std_dev=2):
    """Bollinger Bands. Returns (mid, upper, lower)."""
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((c - mid) ** 2 for c in window) / period
    std = variance ** 0.5
    return mid, mid + std_dev * std, mid - std_dev * std
