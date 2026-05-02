#!/usr/bin/env python3
"""
backtest.py — Replay the paper-kv momentum strategy against real Binance historical data.

Fetches real kline (candlestick) data from Binance public API and replays the
exact momentum strategy from paper_kv.py tick-by-tick. This lets you verify
whether the strategy actually works, and diagnoses why the live bot may never trade.

Usage:
  python3 backtest.py                          # default: BTC,ETH,SOL,NEAR last 7 days
  python3 backtest.py --days 30                # last 30 days
  python3 backtest.py --pairs BTC,ETH          # specific pairs
  python3 backtest.py --threshold 1.0          # custom momentum threshold %
  python3 backtest.py --leverage 10            # custom leverage
  python3 backtest.py --lookback 60            # 60-minute lookback
  python3 backtest.py --interval 5m            # 5-minute candles (faster for long periods)
  python3 backtest.py --save-data              # save fetched Binance data as JSON
  python3 backtest.py --load-data data.json    # reuse previously saved JSON

No dependencies — stdlib only (uses urllib for Binance API).
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone


# ── Symbol Mapping (NEAR Intents → Binance) ────────────────────────────

SYMBOL_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "wNEAR": "NEARUSDT",
    "NEAR": "NEARUSDT",
    "BNB": "BNBUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "ADA": "ADAUSDT",
    "AVAX": "AVAXUSDT",
    "DOT": "DOTUSDT",
    "LINK": "LINKUSDT",
    "MATIC": "MATICUSDT",
    "UNI": "UNIUSDT",
    "ATOM": "ATOMUSDT",
}

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


# ── Binance Kline Fetcher ──────────────────────────────────────────────


def fetch_klines(symbol, interval, start_ms, end_ms):
    """Fetch Binance klines, paginating through 1000-candle pages."""
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        url = (
            f"{BINANCE_KLINES_URL}?symbol={symbol}"
            f"&interval={interval}&startTime={current_start}"
            f"&endTime={end_ms}&limit=1000"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "paper-kv-backtest/1.0"}
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300] if e.fp else ""
            print(f"  ⚠️  Binance HTTP {e.code} for {symbol}: {body}")
            break
        except Exception as e:
            print(f"  ⚠️  Failed to fetch {symbol}: {e}")
            break

        if not data:
            break

        all_klines.extend(data)
        current_start = data[-1][0] + 1  # next ms after last candle open
        time.sleep(0.15)  # rate-limit courtesy

    return all_klines


def parse_klines(raw):
    """Parse raw Binance kline arrays → [{ts, open, high, low, close, volume}]."""
    return [
        {
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in raw
    ]


# ── Backtest Position ──────────────────────────────────────────────────


class BacktestPosition:
    """Mirrors the position logic from paper_kv.py exactly."""

    def __init__(
        self,
        pos_id,
        symbol,
        direction,
        entry_price,
        leverage,
        collateral,
        tick_interval_sec,
        opened_at_ts,
    ):
        self.id = pos_id
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.leverage = leverage
        self.collateral = collateral
        self.size = collateral * leverage
        self.tick_interval_sec = tick_interval_sec
        self.opened_at_ts = opened_at_ts
        self.funding_fees_paid = 0.0
        self.ticks_open = 0

        # Same liquidation formula as paper_kv.py _open_position
        mm = 0.005
        if direction == "long":
            self.liquidation_price = entry_price * (1 - 1 / leverage + mm)
        else:
            self.liquidation_price = entry_price * (1 + 1 / leverage - mm)

    def unrealized_pnl(self, price):
        """Same formula as paper_kv.py _close_position."""
        if self.direction == "long":
            pct = ((price - self.entry_price) / self.entry_price) * self.leverage * 100
        else:
            pct = ((self.entry_price - price) / self.entry_price) * self.leverage * 100
        return self.collateral * (pct / 100)

    def apply_funding(self):
        """One tick of funding — same formula as paper_kv.py _check_liquidations."""
        ticks_per_8h = (8 * 3600) / self.tick_interval_sec
        self.funding_fees_paid += self.size * 0.0001 / ticks_per_8h
        self.ticks_open += 1


# ── Backtest Engine ────────────────────────────────────────────────────


class BacktestEngine:
    """Replays the exact momentum strategy from paper_kv.py on historical prices."""

    def __init__(self, config):
        self.config = config
        self.balance = config["initial_balance"]
        self.positions = []
        self.closed_trades = []
        self.equity_curve = []
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.tick_count = 0

        # Price history per symbol: [{ts, price}, ...]
        self.price_history = defaultdict(list)

    # ── Momentum (exact replica of paper_kv.py PriceFeed.get_momentum) ─

    def _get_momentum(self, symbol, current_ts_ms):
        lookback = self.config["momentum_lookback_min"]
        pts = self.price_history.get(symbol, [])
        if len(pts) < 2:
            return {"current": 0, "change": 0, "dir": "flat"}

        cutoff = current_ts_ms - lookback * 60 * 1000
        window = [p for p in pts if p["ts"] >= cutoff]
        if len(window) < 2:
            return {"current": 0, "change": 0, "dir": "flat"}

        oldest = window[0]["price"]
        newest = window[-1]["price"]
        if oldest == 0:
            return {"current": newest, "change": 0, "dir": "flat"}

        change = ((newest - oldest) / oldest) * 100
        # Same hardcoded thresholds as paper_kv.py get_momentum():
        d = "up" if change > 0.2 else "down" if change < -0.2 else "flat"
        return {"current": newest, "change": change, "dir": d}

    # ── Position management ───────────────────────────────────────────

    def _open_position(self, symbol, direction, price, ts_ms):
        if len(self.positions) >= self.config["max_open_trades"]:
            return None
        # Same guard as paper_kv.py: no duplicate direction on same symbol
        if any(p.symbol == symbol and p.direction == direction for p in self.positions):
            return None
        # paper_kv._run_momentum only opens when no existing position for symbol
        if any(p.symbol == symbol for p in self.positions):
            return None

        if self.config["trade_size"] > self.balance:
            return None

        collateral = self.config["trade_size"]
        leverage = self.config["default_leverage"]

        pos = BacktestPosition(
            pos_id=f"{int(ts_ms / 1000)}-{os.urandom(3).hex()}",
            symbol=symbol,
            direction=direction,
            entry_price=price,
            leverage=leverage,
            collateral=collateral,
            tick_interval_sec=self.config["tick_interval_sec"],
            opened_at_ts=ts_ms,
        )
        self.positions.append(pos)
        return pos

    def _close_position(self, pos, price, ts_ms, reason):
        if pos not in self.positions:
            return None
        self.positions.remove(pos)

        # Same PnL formula as paper_kv.py _close_position
        if pos.direction == "long":
            pnl_pct = ((price - pos.entry_price) / pos.entry_price) * pos.leverage * 100
        else:
            pnl_pct = ((pos.entry_price - price) / pos.entry_price) * pos.leverage * 100
        pnl = pos.collateral * (pnl_pct / 100) - pos.funding_fees_paid

        self.balance += pnl
        self.total_pnl += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1

        trade = {
            "id": pos.id,
            "symbol": pos.symbol,
            "direction": pos.direction,
            "entryPrice": pos.entry_price,
            "exitPrice": price,
            "leverage": pos.leverage,
            "size": pos.size,
            "collateral": pos.collateral,
            "pnl": round(pnl, 2),
            "pnlPct": round(pnl_pct, 2),
            "fundingFeesPaid": round(pos.funding_fees_paid, 4),
            "ticksOpen": pos.ticks_open,
            "exitReason": reason,
            "openedAtTs": pos.opened_at_ts,
            "closedAtTs": ts_ms,
        }
        self.closed_trades.append(trade)
        return trade

    # ── Liquidation check ─────────────────────────────────────────────

    def _check_liquidations(self, prices, ts_ms):
        to_liq = []
        for pos in self.positions:
            price = prices.get(pos.symbol)
            if price is None:
                continue
            if (pos.direction == "long" and price <= pos.liquidation_price) or (
                pos.direction == "short" and price >= pos.liquidation_price
            ):
                to_liq.append((pos, price))

        for pos, price in to_liq:
            if pos not in self.positions:
                continue
            self.positions.remove(pos)
            self.balance -= pos.collateral
            self.total_pnl -= pos.collateral
            self.losses += 1
            self.closed_trades.append(
                {
                    "id": pos.id,
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "entryPrice": pos.entry_price,
                    "exitPrice": price,
                    "leverage": pos.leverage,
                    "size": pos.size,
                    "collateral": pos.collateral,
                    "pnl": round(-pos.collateral, 2),
                    "pnlPct": -100.0,
                    "fundingFeesPaid": round(pos.funding_fees_paid, 4),
                    "ticksOpen": pos.ticks_open,
                    "exitReason": "liquidated",
                    "openedAtTs": pos.opened_at_ts,
                    "closedAtTs": ts_ms,
                }
            )

    # ── Strategy (exact replica of paper_kv._run_momentum) ────────────

    def _run_momentum(self, prices, ts_ms):
        opened = []
        closed = []

        for symbol in self.config["trade_pairs"]:
            price = prices.get(symbol)
            if price is None:
                continue

            mom = self._get_momentum(symbol, ts_ms)
            existing = next((p for p in self.positions if p.symbol == symbol), None)

            if not existing:
                if (
                    mom["dir"] == "up"
                    and abs(mom["change"]) >= self.config["momentum_threshold_pct"]
                ):
                    pos = self._open_position(symbol, "long", price, ts_ms)
                    if pos:
                        opened.append((symbol, "LONG", price, mom["change"]))
                elif (
                    mom["dir"] == "down"
                    and abs(mom["change"]) >= self.config["momentum_threshold_pct"]
                ):
                    pos = self._open_position(symbol, "short", price, ts_ms)
                    if pos:
                        opened.append((symbol, "SHORT", price, mom["change"]))
            else:
                is_long = existing.direction == "long"
                reversed_dir = (is_long and mom["dir"] == "down") or (
                    not is_long and mom["dir"] == "up"
                )
                if (
                    reversed_dir
                    and abs(mom["change"]) >= self.config["momentum_threshold_pct"]
                ):
                    trade = self._close_position(
                        existing, price, ts_ms, "momentum_reversal"
                    )
                    if trade:
                        closed.append(trade)

        return opened, closed

    # ── Main tick ─────────────────────────────────────────────────────

    def tick(self, prices, ts_ms):
        self.tick_count += 1

        # Record prices into history (same as PriceFeed.fetch_prices does)
        for symbol, price in prices.items():
            self.price_history[symbol].append({"ts": ts_ms, "price": price})

        # Funding on open positions (same as _check_liquidations in paper_kv.py)
        for pos in self.positions:
            pos.apply_funding()

        self._check_liquidations(prices, ts_ms)
        opened, closed = self._run_momentum(prices, ts_ms)

        # Track equity
        unrealized = sum(
            p.unrealized_pnl(prices.get(p.symbol, p.entry_price))
            for p in self.positions
        )
        equity = self.balance + unrealized
        self.equity_curve.append(
            {
                "ts": ts_ms,
                "equity": round(equity, 2),
                "balance": round(self.balance, 2),
                "open_positions": len(self.positions),
            }
        )

        return opened, closed


# ── Report Printer ─────────────────────────────────────────────────────


def print_report(engine, config, days):
    trades = engine.closed_trades
    initial = config["initial_balance"]

    print()
    print("═" * 72)
    print("  BACKTEST RESULTS")
    print("═" * 72)
    print()

    if not trades:
        print("  ⚠️  No trades were executed during the backtest period.")
        print()
        print("  This means the momentum threshold was never reached.")
        print("  The strategy requires a price move of")
        print(
            f"  ≥ {config['momentum_threshold_pct']}% within {config['momentum_lookback_min']} minutes."
        )
        print()
        print("  Try adjusting:")
        print(f"    --threshold   (current: {config['momentum_threshold_pct']}%)")
        print(f"    --lookback    (current: {config['momentum_lookback_min']} min)")
        print(f"    --days        (current: {days})")
        print()
        print(f"  Total ticks processed: {engine.tick_count}")
        print()
        _print_bug_diagnosis(config)
        return engine

    wins = sum(1 for t in trades if t["pnl"] >= 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    win_rate = (wins / len(trades)) * 100
    total_pnl = sum(t["pnl"] for t in trades)
    avg_pnl = total_pnl / len(trades)
    best = max(trades, key=lambda t: t["pnl"])
    worst = min(trades, key=lambda t: t["pnl"])
    total_funding = sum(t.get("fundingFeesPaid", 0) for t in trades)
    liquidations = [t for t in trades if t["exitReason"] == "liquidated"]
    reversals = [t for t in trades if t["exitReason"] == "momentum_reversal"]

    final_eq = engine.equity_curve[-1]["equity"] if engine.equity_curve else initial
    ret_pct = ((final_eq - initial) / initial) * 100

    # Max drawdown
    peak = initial
    max_dd = max_dd_pct = 0
    dd_peak_ts = dd_trough_ts = 0
    for pt in engine.equity_curve:
        if pt["equity"] > peak:
            peak = pt["equity"]
        dd = peak - pt["equity"]
        dd_pct = (dd / peak * 100) if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct
            dd_trough_ts = pt["ts"]

    # Direction breakdown
    longs = [t for t in trades if t["direction"] == "long"]
    shorts = [t for t in trades if t["direction"] == "short"]

    # Per-symbol breakdown
    sym_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        s = sym_stats[t["symbol"]]
        s["trades"] += 1
        if t["pnl"] >= 0:
            s["wins"] += 1
        s["pnl"] += t["pnl"]

    # Daily PnL
    daily = defaultdict(float)
    for t in trades:
        day = datetime.fromtimestamp(t["closedAtTs"] / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        daily[day] += t["pnl"]

    # Avg hold time
    hold_times = [t["ticksOpen"] * config["tick_interval_sec"] for t in trades]
    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0

    print(f"  Period:               Last {days} days")
    print(f"  Candle interval:      {config['kline_interval']}")
    print(f"  Total ticks:          {engine.tick_count}")
    print()
    print(f"  💰 Starting balance:  ${initial:>12,.2f}")
    print(f"  💰 Final equity:      ${final_eq:>12,.2f}")
    print(f"  📊 Return:            {ret_pct:>+11.2f}%")
    print()
    print(f"  📈 Total trades:      {len(trades)}")
    print(f"  ✅ Wins:              {wins}")
    print(f"  ❌ Losses:            {losses}")
    print(f"  🎯 Win rate:          {win_rate:.1f}%")
    print(f"  ⏱️  Avg hold time:     {_fmt_duration(avg_hold)}")
    print()
    print(f"  💵 Total PnL:         ${total_pnl:>+11,.2f}")
    print(f"  📊 Avg PnL / trade:   ${avg_pnl:>+11,.2f}")
    print(
        f"  🏆 Best trade:        ${best['pnl']:>+11,.2f}  ({best['symbol']} {best['direction']})"
    )
    print(
        f"  💀 Worst trade:       ${worst['pnl']:>+11,.2f}  ({worst['symbol']} {worst['direction']})"
    )
    print()
    print(f"  💀 Liquidations:      {len(liquidations)}")
    print(f"  🔄 Reversals closed:  {len(reversals)}")
    print(f"  💸 Total funding:     ${total_funding:>11,.4f}")
    print()
    print(f"  📉 Max drawdown:      ${max_dd:>11,.2f}  ({max_dd_pct:.1f}%)")
    print()

    # Direction breakdown
    print("  ┌─ Direction Breakdown ──────────────────────────────────────────┐")
    if longs:
        lwr = (sum(1 for t in longs if t["pnl"] >= 0) / len(longs)) * 100
        lpnl = sum(t["pnl"] for t in longs)
        print(
            f"  │  LONG   {len(longs):>4d} trades  Win rate: {lwr:5.1f}%  PnL: ${lpnl:>+10,.2f}  │"
        )
    if shorts:
        swr = (sum(1 for t in shorts if t["pnl"] >= 0) / len(shorts)) * 100
        spnl = sum(t["pnl"] for t in shorts)
        print(
            f"  │  SHORT  {len(shorts):>4d} trades  Win rate: {swr:5.1f}%  PnL: ${spnl:>+10,.2f}  │"
        )
    print("  └────────────────────────────────────────────────────────────────┘")
    print()

    # Per-symbol breakdown
    print("  ┌─ Per-Symbol Breakdown ─────────────────────────────────────────┐")
    for sym in sorted(sym_stats):
        s = sym_stats[sym]
        wr = (s["wins"] / s["trades"] * 100) if s["trades"] else 0
        print(
            f"  │  {sym:8s}  {s['trades']:>3d} trades  Win: {wr:5.1f}%  PnL: ${s['pnl']:>+10,.2f}  │"
        )
    print("  └────────────────────────────────────────────────────────────────┘")
    print()

    # Daily PnL
    sorted_days = sorted(daily.keys())
    print("  ┌─ Daily PnL ───────────────────────────────────────────────────┐")
    max_daily = max(abs(v) for v in daily.values()) if daily else 1
    for day in sorted_days:
        pnl = daily[day]
        bar_len = min(int(abs(pnl) / max_daily * 30), 30)
        bar = ("█" * bar_len).ljust(30)
        emoji = "🟢" if pnl >= 0 else "🔴"
        print(f"  │  {day}  {emoji} ${pnl:>+9,.2f}  {bar}  │")
    print("  └────────────────────────────────────────────────────────────────┘")
    print()

    # Trade log
    print("  ┌─ Trade Log (last 40) ──────────────────────────────────────────┐")
    print(
        f"  │ {'Dir':5s} {'Symbol':8s} {'Lvg':>4s}  {'Entry':>12s} {'Exit':>12s} {'PnL%':>8s} {'PnL$':>10s} {'Reason':20s} │"
    )
    print(
        f"  │ {'─' * 5} {'─' * 8} {'─' * 4}  {'─' * 12} {'─' * 12} {'─' * 8} {'─' * 10} {'─' * 20} │"
    )
    for t in trades[-40:]:
        e = "🟢" if t["pnl"] >= 0 else "🔴"
        entry = f"${t['entryPrice']:,.2f}"
        exit_p = f"${t['exitPrice']:,.2f}"
        pct = f"{t['pnlPct']:+.1f}%"
        usd = f"${t['pnl']:+,.2f}"
        print(
            f"  │ {e}{t['direction'].upper():4s} {t['symbol']:8s} {t['leverage']:g}x   {entry:>12s} {exit_p:>12s} {pct:>8s} {usd:>10s} {t['exitReason']:20s} │"
        )
    print("  └────────────────────────────────────────────────────────────────┘")
    print()

    _print_bug_diagnosis(config)
    return engine


def _fmt_duration(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _print_bug_diagnosis(config):
    print("═" * 72)
    print("  🔍 BUG DIAGNOSIS: paper_kv.py PriceFeed")
    print("═" * 72)
    print()
    print("  The current paper_kv.py has a critical bug that prevents trading:")
    print()
    print("  Issue: PriceFeed.fetch_prices() returns the SAME price every tick")
    print("  ────────────────────────────────────────────────────────────────────")
    print("  1. fetch_prices() reads _price from self._token_map")
    print("  2. _token_map is cached for 5 minutes (_token_ttl = 300)")
    print("  3. Between refreshes, the IDENTICAL price is returned every tick")
    print("  4. get_momentum() compares newest vs oldest price in window")
    print("  5. Since they're the same: change = 0.0, dir = 'flat'")
    print("  6. The strategy NEVER triggers — zero trades are opened")
    print()
    print("  Even across refreshes (every 5 min), the NEAR Intents token list")
    print("  API returns a snapshot — not real-time tick data. At best you get")
    print("  12 slightly-different prices per hour. With a 30-min lookback,")
    print("  that's only ~6 data points, and the changes are usually < 0.5%.")
    print()
    print("  Fix options:")
    print("  ────────────")
    print("  A) Switch price feed to Binance ticker/mini-ticker API")
    print("     GET /api/v3/ticker/price — returns fresh price every call, no key")
    print()
    print("  B) Use Binance WebSocket stream for real-time prices")
    print("     Best latency, but more complex")
    print()
    print("  C) Fetch kline history from NEAR Intents (if available)")
    print("     Build momentum window from candlestick history")
    print()
    print(
        f"  This backtest used REAL Binance data at {config['kline_interval']} intervals"
    )
    print(f"  and replayed the exact same strategy. If trades appear above, the")
    print(f"  strategy logic is sound — only the price feed in paper_kv.py is broken.")
    print()


# ── Save / Load ────────────────────────────────────────────────────────


def save_data(data, path):
    """Save fetched kline data + metadata to JSON for later reuse."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    size_kb = os.path.getsize(path) / 1024
    print(f"  💾 Saved data to {path} ({size_kb:.0f} KB)")


def load_data(path):
    """Load previously saved kline data from JSON."""
    with open(path) as f:
        data = json.load(f)
    print(f"  📂 Loaded data from {path}")
    return data


# ── Main ───────────────────────────────────────────────────────────────


def run_backtest(config, days, save_json_path=None, load_json_path=None):
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║   paper-kv Backtester — Binance Historical Data                     ║")
    print("║   Replays momentum strategy tick-by-tick on real prices             ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()

    lookback = config["momentum_lookback_min"]
    threshold = config["momentum_threshold_pct"]
    leverage = config["default_leverage"]
    trade_size = config["trade_size"]
    kline_interval = config["kline_interval"]

    print(f"  Period:      Last {days} days")
    print(f"  Candle:      {kline_interval}")
    print(f"  Strategy:    Momentum ({lookback}min lookback, {threshold}% threshold)")
    print(f"  Leverage:    {leverage}x")
    print(f"  Trade size:  ${trade_size}")
    print(f"  Max open:    {config['max_open_trades']}")
    print(f"  Pairs:       {', '.join(config['trade_pairs'])}")
    if save_json_path:
        print(f"  Save JSON:   {save_json_path}")
    if load_json_path:
        print(f"  Load JSON:   {load_json_path}")
    print()

    # ── Fetch or load Binance data ────────────────────────────────────
    kline_data = {}

    if load_json_path:
        saved = load_data(load_json_path)
        for sym, candles in saved.get("klines", {}).items():
            kline_data[sym] = candles
            first_t = datetime.fromtimestamp(candles[0]["ts"] / 1000, tz=timezone.utc)
            last_t = datetime.fromtimestamp(candles[-1]["ts"] / 1000, tz=timezone.utc)
            print(
                f"  ✅ {sym}: {len(candles)} candles | {first_t:%Y-%m-%d %H:%M} → {last_t:%Y-%m-%d %H:%M} UTC"
            )
        days = saved.get("days", days)
    else:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - days * 24 * 3600 * 1000

        for sym in config["trade_pairs"]:
            binance_sym = SYMBOL_MAP.get(sym, f"{sym}USDT")
            print(f"  ⬇️  Fetching {binance_sym} ({days}d, {kline_interval} candles)...")
            raw = fetch_klines(binance_sym, kline_interval, start_ms, now_ms)
            if not raw:
                print(f"     ⚠️  No data — {binance_sym} may not exist on Binance")
                continue
            parsed = parse_klines(raw)
            kline_data[sym] = parsed
            first_t = datetime.fromtimestamp(parsed[0]["ts"] / 1000, tz=timezone.utc)
            last_t = datetime.fromtimestamp(parsed[-1]["ts"] / 1000, tz=timezone.utc)
            print(
                f"     ✅ {len(parsed)} candles | {first_t:%Y-%m-%d %H:%M} → {last_t:%Y-%m-%d %H:%M} UTC"
            )

    if not kline_data:
        print("\n  ❌ No data fetched for any pair. Check symbol names.")
        return None

    # ── Save raw data if requested ────────────────────────────────────
    if save_json_path:
        save_payload = {
            "meta": {
                "source": "Binance",
                "interval": kline_interval,
                "days": days,
                "pairs": list(kline_data.keys()),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "config": {
                    "lookback_min": lookback,
                    "threshold_pct": threshold,
                    "leverage": leverage,
                    "trade_size": trade_size,
                },
            },
            "klines": kline_data,
        }
        save_data(save_payload, save_json_path)
        print()

    print()

    # ── Build tick timeline ───────────────────────────────────────────
    # Each candle close = one simulated tick
    price_lookup = {}
    for sym, klines in kline_data.items():
        price_lookup[sym] = {k["ts"]: k["close"] for k in klines}

    # Collect all unique timestamps across all symbols, sorted
    all_ts = sorted({ts for sym_lookup in price_lookup.values() for ts in sym_lookup})

    if not all_ts:
        print("  ❌ No timestamps found in data.")
        return None

    first_ts = all_ts[0]
    last_ts = all_ts[-1]
    warmup_ms = lookback * 60 * 1000

    print(f"  🏁 Running backtest...")
    print(
        f"     {datetime.fromtimestamp(first_ts / 1000, tz=timezone.utc):%Y-%m-%d %H:%M} → "
        f"{datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc):%Y-%m-%d %H:%M} UTC"
    )
    print(f"     Warm-up: {lookback} min (no trades until momentum window is full)")
    print(f"     Total ticks: {len(all_ts)}")
    print()

    engine = BacktestEngine(config)
    trade_log = []
    last_pct = -1

    for i, ts in enumerate(all_ts):
        # Progress
        pct = int(i / len(all_ts) * 100)
        if pct != last_pct and pct % 10 == 0:
            print(f"  ... {pct}% ({i}/{len(all_ts)} ticks)")
            last_pct = pct

        # Get prices at this timestamp
        prices = {}
        for sym in config["trade_pairs"]:
            price = price_lookup.get(sym, {}).get(ts)
            if price is not None:
                prices[sym] = price

        if not prices:
            continue

        opened, closed = engine.tick(prices, ts)

        for sym, direction, price, change in opened:
            ts_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
            print(
                f"  🟢 OPENED {direction} {sym} @ ${price:,.2f} (momentum: {change:+.2f}%) [{ts_str}]"
            )

        for trade in closed:
            ts_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
            emoji = "🟢" if trade["pnl"] >= 0 else "🔴"
            print(
                f"  {emoji} CLOSED {trade['direction'].upper()} {trade['symbol']} | "
                f"${trade['entryPrice']:,.2f}→${trade['exitPrice']:,.2f} | "
                f"{trade['pnlPct']:+.2f}% (${trade['pnl']:+.2f}) [{ts_str}]"
            )

    print(f"  ... 100% ({len(all_ts)}/{len(all_ts)} ticks)")
    print()

    # ── Print results ─────────────────────────────────────────────────
    engine = print_report(engine, config, days)

    # ── Save results JSON ─────────────────────────────────────────────
    results_path = save_json_path or "backtest_results.json"
    if not results_path.endswith("_results.json"):
        results_path = results_path.replace(".json", "_results.json")

    results_payload = {
        "meta": {
            "source": "Binance",
            "interval": kline_interval,
            "days": days,
            "pairs": list(kline_data.keys()),
            "config": {
                "initial_balance": config["initial_balance"],
                "trade_size": trade_size,
                "leverage": leverage,
                "lookback_min": lookback,
                "threshold_pct": threshold,
                "max_open_trades": config["max_open_trades"],
            },
        },
        "summary": {
            "total_trades": len(engine.closed_trades),
            "wins": engine.wins,
            "losses": engine.losses,
            "total_pnl": round(engine.total_pnl, 2),
            "final_balance": round(engine.balance, 2),
            "final_equity": engine.equity_curve[-1]["equity"]
            if engine.equity_curve
            else config["initial_balance"],
            "total_ticks": engine.tick_count,
        },
        "trades": engine.closed_trades,
        "equity_curve": engine.equity_curve,
    }
    save_data(results_payload, results_path)

    return engine


# ── CLI ────────────────────────────────────────────────────────────────


def parse_args():
    p = argparse.ArgumentParser(
        description="Backtest paper-kv momentum strategy on real Binance data"
    )
    p.add_argument(
        "--days",
        type=int,
        default=7,
        help="How many days of history to fetch (default: 7)",
    )
    p.add_argument(
        "--pairs",
        type=str,
        default="BTC,ETH,SOL,wNEAR",
        help="Comma-separated pairs (default: BTC,ETH,SOL,wNEAR)",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Momentum threshold %% (default: 0.5)",
    )
    p.add_argument(
        "--lookback",
        type=int,
        default=30,
        help="Momentum lookback in minutes (default: 30)",
    )
    p.add_argument(
        "--leverage", type=float, default=5, help="Leverage multiplier (default: 5)"
    )
    p.add_argument(
        "--trade-size",
        type=float,
        default=100,
        help="USD collateral per trade (default: 100)",
    )
    p.add_argument(
        "--balance", type=float, default=10000, help="Starting balance (default: 10000)"
    )
    p.add_argument(
        "--max-open", type=int, default=5, help="Max concurrent positions (default: 5)"
    )
    p.add_argument(
        "--interval",
        type=str,
        default="1m",
        help="Binance kline interval: 1m, 5m, 15m, 1h (default: 1m)",
    )
    p.add_argument(
        "--save-data",
        type=str,
        default=None,
        help="Save raw Binance data to this JSON file",
    )
    p.add_argument(
        "--load-data",
        type=str,
        default=None,
        help="Load previously saved JSON instead of fetching",
    )
    return p.parse_args()


def interval_to_seconds(interval):
    """Convert Binance interval string to seconds."""
    mapping = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "6h": 21600,
        "8h": 28800,
        "12h": 43200,
        "1d": 86400,
        "3d": 259200,
        "1w": 604800,
    }
    return mapping.get(interval, 60)


if __name__ == "__main__":
    args = parse_args()

    tick_sec = interval_to_seconds(args.interval)

    config = {
        "initial_balance": args.balance,
        "trade_size": args.trade_size,
        "default_leverage": args.leverage,
        "max_open_trades": args.max_open,
        "momentum_lookback_min": args.lookback,
        "momentum_threshold_pct": args.threshold,
        "trade_pairs": [s.strip() for s in args.pairs.split(",")],
        "tick_interval_sec": tick_sec,
        "kline_interval": args.interval,
    }

    # Default save path if not specified
    save_path = args.save_data
    if save_path is None and args.load_data is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        save_path = f"data/binance_{args.interval}_{args.days}d_{ts}.json"
        os.makedirs("data", exist_ok=True)

    run_backtest(
        config,
        days=args.days,
        save_json_path=save_path,
        load_json_path=args.load_data,
    )
