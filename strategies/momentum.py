"""Momentum strategy — extracted from the original PaperBot._run_momentum()."""

from strategies.base import BaseStrategy, Signal


class Strategy(BaseStrategy):
    """Open on momentum breakouts, close on reversals."""

    def evaluate(self, prices, positions, price_history, config):
        signals = []
        lookback = config.get("momentum_lookback_min", 30)
        threshold = config.get("momentum_threshold_pct", 0.5)

        for symbol in config["trade_pairs"]:
            price = prices.get(symbol)
            if not price:
                continue

            pts = price_history.get(symbol, [])
            now_ts = pts[-1]["ts"] if pts else 0
            mom = self.momentum_of(price_history, symbol, lookback, now_ts)

            existing = next((p for p in positions if p["symbol"] == symbol), None)

            if not existing:
                if mom["dir"] == "up" and abs(mom["change"]) >= threshold:
                    signals.append(
                        Signal.open_long(
                            symbol,
                            reason=f"momentum UP +{mom['change']:.2f}%",
                        )
                    )
                elif mom["dir"] == "down" and abs(mom["change"]) >= threshold:
                    signals.append(
                        Signal.open_short(
                            symbol,
                            reason=f"momentum DOWN {mom['change']:.2f}%",
                        )
                    )
            else:
                is_long = existing["direction"] == "long"
                reversed_dir = (is_long and mom["dir"] == "down") or (
                    not is_long and mom["dir"] == "up"
                )
                if reversed_dir and abs(mom["change"]) >= threshold:
                    signals.append(
                        Signal.close_position(symbol, reason="momentum_reversal")
                    )

        return signals
