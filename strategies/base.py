"""Strategy plugin interface — all strategies inherit from BaseStrategy."""

import abc


class Signal:
    """A strategy signal — open or close a position."""

    OPEN = "open"
    CLOSE = "close"

    def __init__(
        self,
        action,
        symbol,
        direction=None,
        reason="",
        stop_loss=None,
        take_profit=None,
    ):
        self.action = action
        self.symbol = symbol
        self.direction = direction
        self.reason = reason
        self.stop_loss = stop_loss
        self.take_profit = take_profit

    def __repr__(self):
        if self.action == self.OPEN:
            return f"Signal(OPEN {self.direction} {self.symbol} {self.reason})"
        return f"Signal(CLOSE {self.symbol} {self.reason})"

    @classmethod
    def open_long(cls, symbol, reason="", stop_loss=None, take_profit=None):
        return cls(cls.OPEN, symbol, "long", reason, stop_loss, take_profit)

    @classmethod
    def open_short(cls, symbol, reason="", stop_loss=None, take_profit=None):
        return cls(cls.OPEN, symbol, "short", reason, stop_loss, take_profit)

    @classmethod
    def close_position(cls, symbol, reason=""):
        return cls(cls.CLOSE, symbol, reason=reason)


class BaseStrategy(abc.ABC):
    """All strategies must implement evaluate().

    evaluate() is called every tick. It receives market data and returns
    a list of Signal objects describing what positions to open or close.
    The engine handles the actual position management (balance checks,
    liquidation, etc.).
    """

    @abc.abstractmethod
    def evaluate(self, prices, positions, price_history, config):
        """
        Called every tick.

        Args:
            prices:        dict {symbol: current_price}
            positions:     list of open position dicts with keys:
                               id, symbol, direction, entryPrice, leverage,
                               size, collateral, liquidationPrice,
                               unrealizedPnl, unrealizedPnlPct, ticksOpen,
                               stopLoss, takeProfit
            price_history: dict {symbol: [{ts, open, high, low, close, volume}, ...]}
            config:        the bot CONFIG dict

        Returns:
            list[Signal] — what to open/close this tick
        """
        raise NotImplementedError

    # ── Shared helpers (available to all strategies) ──────────────────

    @staticmethod
    def momentum_of(price_history, symbol, lookback_min, now_ts_ms):
        """Compute momentum from price history.

        Returns dict: {current, change, dir} where dir is "up"/"down"/"flat".
        """
        pts = price_history.get(symbol, [])
        if len(pts) < 2:
            return {"current": 0, "change": 0, "dir": "flat"}
        cutoff = now_ts_ms - lookback_min * 60 * 1000
        window = [p for p in pts if p["ts"] >= cutoff]
        if len(window) < 2:
            return {"current": 0, "change": 0, "dir": "flat"}
        oldest = window[0].get("close", window[0].get("price", 0))
        newest = window[-1].get("close", window[-1].get("price", 0))
        if oldest == 0:
            return {"current": newest, "change": 0, "dir": "flat"}
        change = ((newest - oldest) / oldest) * 100
        d = "up" if change > 0.2 else "down" if change < -0.2 else "flat"
        return {"current": newest, "change": change, "dir": d}

    @staticmethod
    def ema(candles, period):
        """Exponential Moving Average. Returns float or None."""
        if len(candles) < period:
            return None
        closes = [c.get("close", c.get("price", 0)) for c in candles]
        multiplier = 2.0 / (period + 1)
        result = sum(closes[:period]) / period
        for price in closes[period:]:
            result = (price - result) * multiplier + result
        return result

    @staticmethod
    def rsi(candles, period=14):
        """Relative Strength Index. Returns float (0-100) or None."""
        if len(candles) < period + 1:
            return None
        closes = [c.get("close", c.get("price", 0)) for c in candles]
        gains, losses = [], []
        for i in range(1, len(closes)):
            change = closes[i] - closes[i - 1]
            gains.append(max(0.0, change))
            losses.append(max(0.0, -change))
        if len(gains) < period:
            return None
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def bollinger_bands(candles, period=20, num_std=2):
        """Bollinger Bands. Returns {"upper", "middle", "lower", "bandwidth"} or None."""
        if len(candles) < period:
            return None
        closes = [c.get("close", c.get("price", 0)) for c in candles]
        recent = closes[-period:]
        middle = sum(recent) / period
        variance = sum((p - middle) ** 2 for p in recent) / period
        std = variance**0.5
        upper = middle + num_std * std
        lower = middle - num_std * std
        return {
            "upper": upper,
            "middle": middle,
            "lower": lower,
            "bandwidth": (upper - lower) / middle if middle > 0 else 0,
        }

    @staticmethod
    def atr(candles, period=14):
        """Average True Range. Returns float or None."""
        if len(candles) < period + 1:
            return None
        true_ranges = []
        for i in range(1, len(candles)):
            high = candles[i].get("high", candles[i].get("close", 0))
            low = candles[i].get("low", candles[i].get("close", 0))
            prev_close = candles[i - 1].get("close", candles[i - 1].get("price", 0))
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        if len(true_ranges) < period:
            return None
        result = sum(true_ranges[:period]) / period
        for tr in true_ranges[period:]:
            result = (result * (period - 1) + tr) / period
        return result
