#!/usr/bin/env python3
"""
paper-kv — Python bot, gasless KV writes via OutLayer Agent Custody
Binance prices for signals, NEAR FastData KV for persistent storage.

No private keys needed. Uses OutLayer TEE-secured wallet for all writes.
"""

import json, urllib.request, time, os, sys, signal, subprocess, base64
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG = {
    "outlayer_api_key": os.environ.get("OUTLAYER_API_KEY", ""),
    "outlayer_api": os.environ.get("OUTLAYER_API_BASE", "https://api.outlayer.fastnear.com"),
    "near_account": os.environ.get("NEAR_ACCOUNT", ""),
    "kv_contract": os.environ.get("KV_CONTRACT", "paper-kv.near"),
    "initial_balance": float(os.environ.get("INITIAL_BALANCE", "10000")),
    "trade_size": float(os.environ.get("TRADE_SIZE", "100")),
    "default_leverage": float(os.environ.get("DEFAULT_LEVERAGE", "5")),
    "max_open_trades": int(os.environ.get("MAX_OPEN_TRADES", "5")),
    "check_interval_ms": int(os.environ.get("CHECK_INTERVAL_MS", "60000")),
    "strategy": os.environ.get("STRATEGY", "momentum"),
    "momentum_lookback_min": int(os.environ.get("MOMENTUM_LOOKBACK_MINUTES", "30")),
    "momentum_threshold_pct": float(os.environ.get("MOMENTUM_THRESHOLD_PCT", "0.5")),
    "trade_pairs": os.environ.get("TRADE_PAIRS", "BTCUSDT,ETHUSDT,SOLUSDT,NEARUSDT").split(","),
}

KV_READ_BASE = "https://kv.main.fastnear.com"
MAX_TRADES_HISTORY = 500

# ── KV Client ───────────────────────────────────────────────────────────────

def kv_get(account, contract, key):
    """Read from KV via HTTP (free, no auth)."""
    try:
        url = f"{KV_READ_BASE}/v0/latest/{contract}/{account}/{key}"
        req = urllib.request.Request(url, headers={"User-Agent": "paper-kv/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        entries = data.get("entries", [])
        if not entries:
            return None
        value = entries[0].get("value")
        if isinstance(value, str):
            value = json.loads(value)
        return value
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"  ⚠️  KV read failed ({key}): HTTP {e.code}")
        return None
    except Exception as e:
        print(f"  ⚠️  KV read failed ({key}): {e}")
        return None

def kv_write_batch(account, contract, data_dict, api_key="", api_base=""):
    """Write to KV via OutLayer gasless contract call, or fallback to near-cli-rs."""
    if api_key:
        return _kv_write_outlayer(contract, data_dict, api_key, api_base)
    return _kv_write_cli(account, contract, data_dict)

def _kv_write_outlayer(contract, data_dict, api_key, api_base):
    """Write via OutLayer /wallet/v1/call (gasless, TEE-secured)."""
    try:
        body = json.dumps({
            "receiver_id": contract,
            "method_name": "__fastdata_kv",
            "args": data_dict,
            "gas": "300000000000000",
        }).encode()
        req = urllib.request.Request(
            f"{api_base}/wallet/v1/call",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        status = result.get("status", "unknown")
        if status == "pending_approval":
            print(f"  ⏳ KV write pending approval")
            return True
        print(f"  ✅ KV saved via Outlayer ({len(data_dict)} keys)")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        print(f"  ⚠️  KV write failed (HTTP {e.code}): {body}")
        return False
    except Exception as e:
        print(f"  ⚠️  KV write error: {e}")
        return False

def _kv_write_cli(account, contract, data_dict):
    """Fallback: write via near-cli-rs (requires local keychain)."""
    args_b64 = base64.b64encode(json.dumps(data_dict).encode()).decode()
    cmd = [
        "near", "contract", "call-function", "as-transaction",
        contract, "__fastdata_kv",
        "base64-args", args_b64,
        "prepaid-gas", "300 Tgas",
        "attached-deposit", "0 NEAR",
        "sign-as", account,
        "network-config", "mainnet",
        "sign-with-keychain", "send",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"  ⚠️  KV write failed: {result.stderr[:200]}")
            return False
        print(f"  ✅ KV saved ({len(data_dict)} keys)")
        return True
    except Exception as e:
        print(f"  ⚠️  KV write error: {e}")
        return False

# ── Price Feed ──────────────────────────────────────────────────────────────

class PriceFeed:
    def __init__(self):
        self.cache = {}

    def fetch_prices(self, symbols):
        prices = {}
        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbols=%5B{('%2C'.join(f'%22{s}%22' for s in symbols))}%5D"
            req = urllib.request.Request(url, headers={"User-Agent": "paper-kv/1.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            for d in data:
                price = float(d["price"])
                symbol = d["symbol"]
                prices[symbol] = price
                pts = self.cache.get(symbol, [])
                pts.append({"ts": int(time.time() * 1000), "price": price})
                cutoff = int(time.time() * 1000) - 4 * 60 * 60 * 1000
                self.cache[symbol] = [p for p in pts if p["ts"] >= cutoff]
        except Exception as e:
            print(f"  ⚠️  Binance fetch failed: {e}")
        return prices

    def get_momentum(self, symbol, lookback_min):
        pts = self.cache.get(symbol, [])
        if len(pts) < 2:
            return {"current": 0, "change": 0, "dir": "flat"}
        cutoff = int(time.time() * 1000) - lookback_min * 60 * 1000
        window = [p for p in pts if p["ts"] >= cutoff]
        if len(window) < 2:
            return {"current": 0, "change": 0, "dir": "flat"}
        oldest = window[0]["price"]
        newest = window[-1]["price"]
        change = ((newest - oldest) / oldest) * 100
        return {
            "current": newest,
            "change": change,
            "dir": "up" if change > 0.2 else "down" if change < -0.2 else "flat",
        }

# ── Bot ─────────────────────────────────────────────────────────────────────

class PaperBot:
    def __init__(self, config):
        self.config = config
        self.api_key = config["outlayer_api_key"]
        self.api_base = config["outlayer_api"]
        self.contract = config["kv_contract"]
        self.account = config["near_account"]
        if not self.account and self.api_key:
            self.account = self._get_account_id()
        self.price_feed = PriceFeed()
        self.state = {
            "balance": config["initial_balance"],
            "totalTrades": 0,
            "wins": 0,
            "losses": 0,
            "totalPnl": 0,
        }
        self.positions = []
        self.trades = []
        self.running = True
        self._dirty = False

    def _get_account_id(self):
        """Get intents account ID from OutLayer."""
        try:
            req = urllib.request.Request(
                f"{self.api_base}/wallet/v1/balance?token=wrap.near&source=intents",
                headers={"Authorization": f"Bearer {self.api_key}"}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            return data.get("account_id", "unknown")
        except Exception as err:
            print(f"  ⚠️  Failed to get account from OutLayer: {err}")
            return "unknown"

    def init(self):
        print("╔═══════════════════════════════════════════════╗")
        print("║   paper-kv — Paper Trading Bot (Python)       ║")
        print("║   Binance prices + NEAR KV via OutLayer       ║")
        print("╚═══════════════════════════════════════════════╝")
        print()
        print(f"  Account:    {self.account}")
        print(f"  KV store:   {self.contract}")
        print(f"  Strategy:   {self.config['strategy']}")
        print(f"  Leverage:   {self.config['default_leverage']}x")
        print(f"  Trade size: ${self.config['trade_size']}")
        print(f"  Pairs:      {', '.join(self.config['trade_pairs'])}")
        print()
        self._load_state()
        print()

    def _load_state(self):
        print("── Loading state from KV ──")
        state = kv_get(self.account, self.contract, "state")
        positions = kv_get(self.account, self.contract, "positions")
        trades = kv_get(self.account, self.contract, "trades")

        if state:
            self.state = state
            print(f"  Balance:    ${self.state['balance']:.2f}")
            print(f"  Trades:     {self.state['totalTrades']} ({self.state['wins']}W/{self.state['losses']}L)")
            print(f"  Total PnL:  ${self.state['totalPnl']:+.2f}")
        else:
            print(f"  New account — starting with ${self.config['initial_balance']}")
            self._dirty = True

        self.positions = positions or []
        self.trades = trades or []
        print(f"  Open:       {len(self.positions)} positions")
        print(f"  History:    {len(self.trades)} closed trades")

        prices = self.price_feed.fetch_prices(self.config["trade_pairs"])
        for sym, price in prices.items():
            print(f"  {sym}: ${price:,.2f}")

    def _save_state(self):
        if not self._dirty:
            return
        # FIX 3: Trim trades history to prevent unbounded KV growth
        if len(self.trades) > MAX_TRADES_HISTORY:
            self.trades = self.trades[-MAX_TRADES_HISTORY:]
        kv_write_batch(self.account, self.contract, {
            "state": self.state,
            "positions": self.positions,
            "trades": self.trades,
        }, api_key=self.api_key, api_base=self.api_base)
        self._dirty = False

    def _open_position(self, symbol, direction, price):
        if len(self.positions) >= self.config["max_open_trades"]:
            return None
        if any(p["symbol"] == symbol and p["direction"] == direction for p in self.positions):
            return None

        # FIX 2: Check balance before opening
        if self.config["trade_size"] > self.state["balance"]:
            print(f"  ⏸️  Insufficient balance (${self.state['balance']:.2f})")
            return None

        collateral = self.config["trade_size"]
        leverage = self.config["default_leverage"]
        size = collateral * leverage
        mm = 0.005

        if direction == "long":
            liq = price * (1 - 1 / leverage + mm)
        else:
            liq = price * (1 + 1 / leverage - mm)

        pos = {
            "id": f"{int(time.time())}-{os.urandom(3).hex()}",
            "symbol": symbol, "direction": direction,
            "entryPrice": price, "leverage": leverage,
            "size": size, "collateral": collateral,
            "liquidationPrice": liq, "fundingFeesPaid": 0,
            # FIX 4: Use timezone-aware UTC instead of deprecated .utcnow()
            "openedAt": datetime.now(timezone.utc).isoformat(),
        }
        self.positions.append(pos)
        self._dirty = True
        d = direction.upper()
        print(f"  🟢 OPENED {d} {symbol} {leverage}x | ${price:,.2f} → Liq: ${liq:,.2f}")
        return pos

    def _close_position(self, pos, price, reason):
        self.positions.remove(pos)
        if pos["direction"] == "long":
            pnl_pct = ((price - pos["entryPrice"]) / pos["entryPrice"]) * pos["leverage"] * 100
        else:
            pnl_pct = ((pos["entryPrice"] - price) / pos["entryPrice"]) * pos["leverage"] * 100
        pnl = pos["collateral"] * (pnl_pct / 100)

        closed = {**pos, "exitPrice": price, "pnl": round(pnl, 2),
                  "pnlPct": round(pnl_pct, 2),
                  # FIX 4: timezone-aware UTC
                  "closedAt": datetime.now(timezone.utc).isoformat(),
                  "exitReason": reason}

        self.state["balance"] += pnl
        self.state["totalTrades"] += 1
        self.state["totalPnl"] += pnl
        self.state["wins" if pnl >= 0 else "losses"] += 1
        self.trades.append(closed)
        self._dirty = True

        emoji = "🟢" if pnl >= 0 else "🔴"
        print(f"  {emoji} CLOSED {pos['direction'].upper()} {pos['symbol']} | ${pos['entryPrice']:,.2f}→${price:,.2f} | {pnl_pct:+.2f}% (${pnl:+.2f}) [{reason}]")

    def _check_liquidations(self, prices):
        ticks_per = (8 * 3600) / (self.config["check_interval_ms"] / 1000)
        fr = 0.0001
        to_close = []
        for pos in self.positions:
            price = prices.get(pos["symbol"])
            if not price:
                continue
            pos["fundingFeesPaid"] = pos.get("fundingFeesPaid", 0) + pos["size"] * fr / ticks_per
            if (pos["direction"] == "long" and price <= pos["liquidationPrice"]) or \
               (pos["direction"] == "short" and price >= pos["liquidationPrice"]):
                to_close.append((pos, price))
        for pos, price in to_close:
            # FIX 6: Guard against double-remove when concurrent liquidations fire
            if pos not in self.positions:
                continue
            self.positions.remove(pos)
            closed = {**pos, "exitPrice": price, "pnl": -pos["collateral"],
                      "pnlPct": -100,
                      # FIX 4: timezone-aware UTC
                      "closedAt": datetime.now(timezone.utc).isoformat(),
                      "exitReason": "liquidated"}
            self.state["balance"] -= pos["collateral"]
            self.state["totalTrades"] += 1
            self.state["totalPnl"] -= pos["collateral"]
            self.state["losses"] += 1
            self.trades.append(closed)
            self._dirty = True
            print(f"  💀 LIQUIDATED {pos['direction'].upper()} {pos['symbol']} {pos['leverage']}x | Lost: ${pos['collateral']}")

    def _run_momentum(self, prices):
        for symbol in self.config["trade_pairs"]:
            price = prices.get(symbol)
            if not price:
                continue
            mom = self.price_feed.get_momentum(symbol, self.config["momentum_lookback_min"])
            existing = next((p for p in self.positions if p["symbol"] == symbol), None)

            if not existing:
                if mom["dir"] == "up" and abs(mom["change"]) >= self.config["momentum_threshold_pct"]:
                    print(f"  📈 {symbol} momentum UP +{mom['change']:.2f}%")
                    self._open_position(symbol, "long", price)
                elif mom["dir"] == "down" and abs(mom["change"]) >= self.config["momentum_threshold_pct"]:
                    print(f"  📉 {symbol} momentum DOWN {mom['change']:.2f}%")
                    self._open_position(symbol, "short", price)
            else:
                is_long = existing["direction"] == "long"
                reversed_dir = (is_long and mom["dir"] == "down") or (not is_long and mom["dir"] == "up")
                if reversed_dir and abs(mom["change"]) >= self.config["momentum_threshold_pct"]:
                    print(f"  🔄 {symbol} reversed ({mom['change']:.2f}%)")
                    self._close_position(existing, price, "momentum_reversal")

    def tick(self):
        # FIX 4: timezone-aware UTC
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n── {now} ──")

        prices = self.price_feed.fetch_prices(self.config["trade_pairs"])
        for sym, price in prices.items():
            print(f"  💲 {sym}: ${price:,.2f}")

        self._check_liquidations(prices)

        if self.config["strategy"] == "momentum":
            self._run_momentum(prices)

        if self.positions:
            print(f"\n  📊 Open ({len(self.positions)}):")
            for p in self.positions:
                cp = prices.get(p["symbol"], p["entryPrice"])
                if p["direction"] == "long":
                    u = ((cp - p["entryPrice"]) / p["entryPrice"]) * p["leverage"] * 100
                else:
                    u = ((p["entryPrice"] - cp) / p["entryPrice"]) * p["leverage"] * 100
                emoji = "🟢" if u >= 0 else "🔴"
                print(f"    {emoji} {p['symbol']} {p['direction'].upper()} {p['leverage']}x | {u:+.2f}%")

        b = self.state["balance"]
        t = self.state["totalTrades"]
        w = self.state["wins"]
        l = self.state["losses"]
        p = self.state["totalPnl"]
        print(f"\n  💰 ${b:.2f} | {t} trades ({w}W/{l}L) | PnL: ${p:+.2f}")

        self._save_state()

    def start(self):
        self.init()
        interval = self.config["check_interval_ms"] / 1000
        print(f"▶  Running every {interval:.0f}s (Ctrl+C to stop)\n")

        def shutdown(sig, frame):
            print("\n\n⏹  Shutting down...")
            self.running = False
            self._dirty = True
            self._save_state()
            print(f"  Balance: ${self.state['balance']:.2f} | Trades: {self.state['totalTrades']} | PnL: ${self.state['totalPnl']:+.2f}")
            print(f"  View: {KV_READ_BASE}/v0/latest/{self.contract}/{self.account}/state\n")
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)

        # FIX 1: Seed a second price point so momentum has 2 data points on first tick
        time.sleep(2)
        self.price_feed.fetch_prices(self.config["trade_pairs"])

        self.tick()

        while self.running:
            time.sleep(interval)
            try:
                self.tick()
            # FIX 5: Catch specific Exception instead of bare except
            except Exception as err:
                print(f"  ❌ Tick error: {err}")

if __name__ == "__main__":
    if not CONFIG["outlayer_api_key"] and not CONFIG["near_account"]:
        print("❌ Set OUTLAYER_API_KEY or NEAR_ACCOUNT")
        print("   export OUTLAYER_API_KEY=wk_...  (recommended, gasless)")
        print("   export NEAR_ACCOUNT=you.near     (fallback, needs local keychain)")
        sys.exit(1)
    bot = PaperBot(CONFIG)
    bot.start()
