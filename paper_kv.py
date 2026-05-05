#!/usr/bin/env python3
"""
paper-kv — Python bot, gasless KV writes via OutLayer Agent Custody
NEAR Intents prices for signals, NEAR FastData KV for persistent storage.

No private keys needed. Uses OutLayer TEE-secured wallet for all writes.
"""

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG = {
    "outlayer_api_key": os.environ.get("OUTLAYER_API_KEY", ""),
    "outlayer_api": os.environ.get(
        "OUTLAYER_API_BASE", "https://api.outlayer.fastnear.com"
    ),
    "near_account": os.environ.get("NEAR_ACCOUNT", ""),
    "kv_contract": os.environ.get("KV_CONTRACT", "contextual.near"),
    "initial_balance": float(os.environ.get("INITIAL_BALANCE", "10000")),
    "trade_size": float(os.environ.get("TRADE_SIZE", "100")),
    "default_leverage": float(os.environ.get("DEFAULT_LEVERAGE", "5")),
    "max_open_trades": int(os.environ.get("MAX_OPEN_TRADES", "5")),
    "check_interval_ms": int(os.environ.get("CHECK_INTERVAL_MS", "60000")),
    "strategy": os.environ.get("STRATEGY", "momentum"),
    "momentum_lookback_min": int(os.environ.get("MOMENTUM_LOOKBACK_MINUTES", "30")),
    "momentum_threshold_pct": float(os.environ.get("MOMENTUM_THRESHOLD_PCT", "0.5")),
    "min_hold_seconds": int(os.environ.get("MIN_HOLD_SECONDS", "300")),  # 5 min minimum hold
    # NEAR Intents symbols — maps to Intents API token symbols
    # Supported: wNEAR, BTC, ETH, SOL, USDC, USDT, and many more
    # See full list: curl https://1click.chaindefuser.com/v0/tokens
    "trade_pairs": os.environ.get("TRADE_PAIRS", "BTC,ETH,SOL,wNEAR").split(","),
}

KV_READ_BASE = "https://kv.main.fastnear.com"
INTENTS_TOKENS_URL = "https://1click.chaindefuser.com/v0/tokens"
MAX_TRADES_HISTORY = 500
LOCAL_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ── KV Client ───────────────────────────────────────────────────────────────


def kv_get(account, contract, key):
    """Read from KV via HTTP (free, no auth)."""
    try:
        url = f"{KV_READ_BASE}/v0/latest/{contract}/{account}/{key}"
        req = urllib.request.Request(url, headers={"User-Agent": "paper-kv/2.0"})
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
        body = json.dumps(
            {
                "receiver_id": contract,
                "method_name": "__fastdata_kv",
                "args": data_dict,
                "gas": "300000000000000",
            }
        ).encode()
        req = urllib.request.Request(
            f"{api_base}/wallet/v1/call",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        status = result.get("status", "unknown")
        if status == "pending_approval":
            print(f"  ⏳ KV write pending approval (wallet needs auto-approve policy)")
            return False
        if status == "success":
            print(f"  ✅ KV saved via Outlayer ({len(data_dict)} keys)")
            return True
        # Unknown status — treat as failure
        print(f"  ⚠️  KV write returned unexpected status: {status}")
        return False
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
        "near",
        "contract",
        "call-function",
        "as-transaction",
        contract,
        "__fastdata_kv",
        "base64-args",
        args_b64,
        "prepaid-gas",
        "300 Tgas",
        "attached-deposit",
        "0 NEAR",
        "sign-as",
        account,
        "network-config",
        "mainnet",
        "sign-with-keychain",
        "send",
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


def local_state_save(data):
    """Persist state to local JSON file (always works, no network needed)."""
    try:
        with open(LOCAL_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


def local_state_load():
    """Load state from local JSON file."""
    try:
        if os.path.exists(LOCAL_STATE_FILE):
            with open(LOCAL_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return None


# ── Price Feed (NEAR Intents) ──────────────────────────────────────────────


class PriceFeed:
    """Price feed from NEAR Intents 1Click API — no API key needed.

    Fetches fresh prices every tick so momentum gets real data.
    Previous bug: _token_map was cached for 5 minutes, returning the same
    price every tick — momentum always calculated 0% change = "flat".
    """

    def __init__(self):
        self.cache = {}  # symbol -> [{ts, price}, ...]
        self._token_map = {}  # symbol -> {assetId, blockchain, decimals, _price}

    def _fetch_fresh_prices(self):
        """Fetch fresh prices from NEAR Intents every tick."""
        try:
            req = urllib.request.Request(
                INTENTS_TOKENS_URL, headers={"User-Agent": "paper-kv/2.0"}
            )
            resp = urllib.request.urlopen(req, timeout=15)
            tokens = json.loads(resp.read())
            for t in tokens:
                sym = t["symbol"]
                price = t.get("price", 0)
                if price <= 0:
                    continue
                existing = self._token_map.get(sym)
                if existing and existing.get("_blockchain") == "near":
                    existing["_price"] = price
                    continue
                if existing is None or t["blockchain"] == "near":
                    self._token_map[sym] = {
                        "assetId": t["assetId"],
                        "blockchain": t["blockchain"],
                        "decimals": t["decimals"],
                        "_price": price,
                    }
        except Exception as e:
            print(f"  ⚠️  NEAR Intents price fetch failed: {e}")

    def fetch_prices(self, symbols):
        """Fetch fresh prices for given symbols from NEAR Intents API."""
        self._fetch_fresh_prices()

        prices = {}
        ts = int(time.time() * 1000)
        for sym in symbols:
            entry = self._token_map.get(sym)
            if not entry:
                continue
            price = entry["_price"]
            if price <= 0:
                continue
            prices[sym] = price
            # Update history cache for momentum calculations
            pts = self.cache.get(sym, [])
            pts.append(
                {
                    "ts": ts,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 0,
                }
            )
            cutoff = ts - 4 * 60 * 60 * 1000
            self.cache[sym] = [p for p in pts if p["ts"] >= cutoff]

        return prices

    def get_momentum(self, symbol, lookback_min):
        pts = self.cache.get(symbol, [])
        if len(pts) < 2:
            return {"current": 0, "change": 0, "dir": "flat"}
        cutoff = int(time.time() * 1000) - lookback_min * 60 * 1000
        window = [p for p in pts if p["ts"] >= cutoff]
        if len(window) < 2:
            return {"current": 0, "change": 0, "dir": "flat"}
        oldest = window[0]["close"]
        newest = window[-1]["close"]
        change = ((newest - oldest) / oldest) * 100
        return {
            "current": newest,
            "change": change,
            "dir": "up" if change > 0 else "down" if change < 0 else "flat",
        }

    def list_supported_tokens(self):
        """Return list of all supported token symbols from NEAR Intents."""
        if not self._token_map:
            self._fetch_fresh_prices()
        return [
            {"symbol": sym, "blockchain": v["blockchain"], "price": v["_price"]}
            for sym, v in sorted(self._token_map.items())
            if v["_price"] > 0
        ]

    def seed_from_binance(self, symbols, lookback_min):
        """Seed price history with Binance klines so strategies work on first tick."""
        symbol_map = {
            "BTC": "BTCUSDT",
            "ETH": "ETHUSDT",
            "SOL": "SOLUSDT",
            "wNEAR": "NEARUSDT",
            "NEAR": "NEARUSDT",
        }
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (lookback_min + 5) * 60 * 1000  # +5min buffer

        for sym in symbols:
            binance_sym = symbol_map.get(sym, f"{sym}USDT")
            try:
                url = (
                    f"https://api.binance.com/api/v3/klines?symbol={binance_sym}"
                    f"&interval=1m&startTime={start_ms}&endTime={now_ms}&limit=1000"
                )
                req = urllib.request.Request(
                    url, headers={"User-Agent": "paper-kv/2.0"}
                )
                resp = urllib.request.urlopen(req, timeout=10)
                raw = json.loads(resp.read())
                candles = [
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
                if candles:
                    self.cache[sym] = candles
                    print(
                        f"  📊 Seeded {sym}: {len(candles)} candles ({lookback_min}+ min)"
                    )
            except Exception as e:
                print(f"  ⚠️  Could not seed {sym} from Binance: {e}")


# ── System Integrity ────────────────────────────────────────────────────────


class IntegrityChecker:
    """Verify system health: API reachability, KV connectivity, price feed, state consistency."""

    def __init__(self, bot):
        self.bot = bot
        self.checks = []

    def _check(self, name, ok, detail=""):
        status = "✅" if ok else "❌"
        msg = f"  {status} {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    def run(self):
        """Run all integrity checks, return True if all pass."""
        print("\n── System Integrity Check ──")
        all_ok = True

        # 1. NEAR Intents API reachable
        try:
            req = urllib.request.Request(
                INTENTS_TOKENS_URL, headers={"User-Agent": "paper-kv/2.0"}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            tokens = json.loads(resp.read())
            token_count = len(tokens)
            self._check(
                "NEAR Intents API", token_count > 0, f"{token_count} tokens available"
            )
        except Exception as e:
            all_ok = False
            self._check("NEAR Intents API", False, str(e))

        # 2. KV read endpoint reachable
        try:
            url = (
                f"{KV_READ_BASE}/v0/latest/{self.bot.contract}/{self.bot.account}/state"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "paper-kv/2.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            self._check("KV Read API", True, "reachable")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._check("KV Read API", True, "reachable (no data yet)")
            else:
                all_ok = False
                self._check("KV Read API", False, f"HTTP {e.code}")
        except Exception as e:
            all_ok = False
            self._check("KV Read API", False, str(e))

        # 3. Price feed returns data for configured pairs
        prices = self.bot.price_feed.fetch_prices(self.bot.config["trade_pairs"])
        found = len(prices)
        expected = len(self.bot.config["trade_pairs"])
        if found < expected:
            missing = [s for s in self.bot.config["trade_pairs"] if s not in prices]
            all_ok = False
            self._check(
                "Price Feed",
                False,
                f"{found}/{expected} pairs — missing: {', '.join(missing)}",
            )
        else:
            sample = next(iter(prices.items()))
            self._check(
                "Price Feed",
                True,
                f"{found}/{expected} pairs — {sample[0]}: ${sample[1]:,.2f}",
            )

        # 4. OutLayer API key configured
        if self.bot.api_key:
            try:
                req = urllib.request.Request(
                    f"{self.bot.api_base}/wallet/v1/balance?token=wrap.near&source=intents",
                    headers={"Authorization": f"Bearer {self.bot.api_key}"},
                )
                resp = urllib.request.urlopen(req, timeout=10)
                data = json.loads(resp.read())
                acct = data.get("account_id", "unknown")
                self._check("OutLayer Auth", True, f"account: {acct}")
            except Exception as e:
                all_ok = False
                self._check("OutLayer Auth", False, str(e))
        else:
            all_ok = False
            self._check("OutLayer Auth", False, "no API key set")

        # 5. State consistency: balance non-negative, no negative PnL overflow
        state = self.bot.state
        issues = []
        if state["balance"] < 0:
            issues.append(f"negative balance: ${state['balance']:.2f}")
        if state["totalTrades"] != state["wins"] + state["losses"]:
            issues.append(
                f"trade count mismatch: {state['totalTrades']} vs {state['wins'] + state['losses']}"
            )
        if state["totalPnl"] < -self.bot.config["initial_balance"] * 2:
            issues.append(f"suspicious PnL: ${state['totalPnl']:.2f}")
        self._check(
            "State Consistency",
            len(issues) == 0,
            "; ".join(issues) if issues else "clean",
        )

        # 6. Open positions: validate structure and liquidation math
        pos_issues = []
        for p in self.bot.positions:
            if "symbol" not in p or "entryPrice" not in p:
                pos_issues.append(f"malformed position: {p.get('id', '?')}")
                continue
            # Verify liquidation price math
            lev = p.get("leverage", self.bot.config["default_leverage"])
            entry = p["entryPrice"]
            expected_liq_long = entry * (1 - 1 / lev + 0.005)
            expected_liq_short = entry * (1 + 1 / lev - 0.005)
            actual = p.get("liquidationPrice", 0)
            if p["direction"] == "long" and abs(actual - expected_liq_long) > 0.01:
                pos_issues.append(
                    f"{p['symbol']} long liq mismatch: {actual} vs {expected_liq_long:.2f}"
                )
            elif p["direction"] == "short" and abs(actual - expected_liq_short) > 0.01:
                pos_issues.append(
                    f"{p['symbol']} short liq mismatch: {actual} vs {expected_liq_short:.2f}"
                )
        self._check(
            "Position Validation",
            len(pos_issues) == 0,
            "; ".join(pos_issues)
            if pos_issues
            else f"{len(self.bot.positions)} positions valid",
        )

        # 7. KV write roundtrip: write a probe value, read it back, confirm match
        if not self.bot.api_key:
            self._check("KV Write Roundtrip", False, "no OutLayer key, cannot test")
        else:
            probe_key = "_integrity_probe"
            probe_val = {"ts": int(time.time()), "v": "ok"}
            try:
                write_ok = kv_write_batch(
                    self.bot.account,
                    self.bot.contract,
                    {probe_key: probe_val},
                    api_key=self.bot.api_key,
                    api_base=self.bot.api_base,
                )
            except Exception as write_err:
                write_ok = False
                write_err_msg = str(write_err)

            if not write_ok:
                all_ok = False
                self._check(
                    "KV Write Roundtrip",
                    False,
                    "write failed — wallet may need funding or auto-approve policy (see README)",
                )
            else:
                # Wait for propagation and read back
                time.sleep(5)
                read_back = kv_get(self.bot.account, self.bot.contract, probe_key)
                if read_back is None:
                    all_ok = False
                    self._check(
                        "KV Write Roundtrip",
                        False,
                        "write succeeded but read returned null — "
                        "KV propagation may be slow or write was not finalized",
                    )
                elif read_back.get("v") != probe_val["v"]:
                    all_ok = False
                    self._check(
                        "KV Write Roundtrip",
                        False,
                        f"value mismatch: wrote {probe_val}, read {read_back}",
                    )
                else:
                    self._check("KV Write Roundtrip", True, "write → read verified")

        # Summary
        passed = sum(1 for c in self.checks if c["ok"])
        total = len(self.checks)
        emoji = "✅" if all_ok else "❌"
        print(f"\n  {emoji} {passed}/{total} checks passed")
        return all_ok


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
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            return data.get("account_id", "unknown")
        except Exception as err:
            print(f"  ⚠️  Failed to get account from OutLayer: {err}")
            return "unknown"

    def verify_integrity(self):
        """Run system integrity checks."""
        checker = IntegrityChecker(self)
        return checker.run()

    def init(self):
        print("╔═══════════════════════════════════════════════╗")
        print("║   paper-kv — Paper Trading Bot (Python)       ║")
        print("║   NEAR Intents prices + NEAR KV via OutLayer  ║")
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
        print("── Loading state ──")
        # Try KV first, then local file
        state = kv_get(self.account, self.contract, "state")
        positions = kv_get(self.account, self.contract, "positions")
        trades = kv_get(self.account, self.contract, "trades")

        if not state:
            local = local_state_load()
            if local:
                state = local.get("state")
                positions = local.get("positions")
                trades = local.get("trades")
                print("  📂 Loaded from local state.json")

        if state:
            self.state = state
            print(f"  Balance:    ${self.state['balance']:.2f}")
            print(
                f"  Trades:     {self.state['totalTrades']} ({self.state['wins']}W/{self.state['losses']}L)"
            )
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
        # Trim trades history to prevent unbounded growth
        if len(self.trades) > MAX_TRADES_HISTORY:
            self.trades = self.trades[-MAX_TRADES_HISTORY:]
        # For KV, only send recent trades to keep payload small
        kv_trades = self.trades[-20:]
        data = {
            "state": self.state,
            "positions": self.positions,
            "trades": kv_trades,
        }
        # Always save locally first (guaranteed to work)
        local_state_save(data)
        # Try KV (best effort — may fail if wallet unfunded)
        if self.api_key or self.config.get("near_account"):
            kv_write_batch(
                self.account,
                self.contract,
                {
                    "state": self.state,
                    "positions": self.positions,
                    "trades": self.trades,
                },
                api_key=self.api_key,
                api_base=self.api_base,
            )
        self._dirty = False

    def _open_position(self, symbol, direction, price):
        if len(self.positions) >= self.config["max_open_trades"]:
            return None
        if any(
            p["symbol"] == symbol and p["direction"] == direction
            for p in self.positions
        ):
            return None

        # Check available balance
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
            "symbol": symbol,
            "direction": direction,
            "entryPrice": price,
            "leverage": leverage,
            "size": size,
            "collateral": collateral,
            "liquidationPrice": liq,
            "fundingFeesPaid": 0,
            "openedAt": datetime.now(timezone.utc).isoformat(),
        }
        self.positions.append(pos)
        self.state["balance"] -= collateral  # Lock up collateral
        self._dirty = True
        d = direction.upper()
        print(
            f"  🟢 OPENED {d} {symbol} {leverage}x | ${price:,.2f} → Liq: ${liq:,.2f}"
        )
        return pos

    def _close_position(self, pos, price, reason):
        self.positions.remove(pos)
        if pos["direction"] == "long":
            pnl_pct = (
                ((price - pos["entryPrice"]) / pos["entryPrice"])
                * pos["leverage"]
                * 100
            )
        else:
            pnl_pct = (
                ((pos["entryPrice"] - price) / pos["entryPrice"])
                * pos["leverage"]
                * 100
            )
        pnl = pos["collateral"] * (pnl_pct / 100)

        closed = {
            **pos,
            "exitPrice": price,
            "pnl": round(pnl, 2),
            "pnlPct": round(pnl_pct, 2),
            "closedAt": datetime.now(timezone.utc).isoformat(),
            "exitReason": reason,
        }

        self.state["balance"] += pos["collateral"] + pnl  # Return collateral + profit/loss
        self.state["totalTrades"] += 1
        self.state["totalPnl"] += pnl
        self.state["wins" if pnl >= 0 else "losses"] += 1
        self.trades.append(closed)
        self._dirty = True

        emoji = "🟢" if pnl >= 0 else "🔴"
        print(
            f"  {emoji} CLOSED {pos['direction'].upper()} {pos['symbol']} | ${pos['entryPrice']:,.2f}→${price:,.2f} | {pnl_pct:+.2f}% (${pnl:+.2f}) [{reason}]"
        )

    def _check_liquidations(self, prices):
        ticks_per = (8 * 3600) / (self.config["check_interval_ms"] / 1000)
        fr = 0.0001
        to_close = []
        for pos in self.positions:
            price = prices.get(pos["symbol"])
            if not price:
                continue
            pos["fundingFeesPaid"] = (
                pos.get("fundingFeesPaid", 0) + pos["size"] * fr / ticks_per
            )
            if (pos["direction"] == "long" and price <= pos["liquidationPrice"]) or (
                pos["direction"] == "short" and price >= pos["liquidationPrice"]
            ):
                to_close.append((pos, price))
        for pos, price in to_close:
            # Guard against double-remove when concurrent liquidations fire
            if pos not in self.positions:
                continue
            self.positions.remove(pos)
            closed = {
                **pos,
                "exitPrice": price,
                "pnl": -pos["collateral"],
                "pnlPct": -100,
                "closedAt": datetime.now(timezone.utc).isoformat(),
                "exitReason": "liquidated",
            }
            self.state["balance"] -= pos["collateral"]
            self.state["totalTrades"] += 1
            self.state["totalPnl"] -= pos["collateral"]
            self.state["losses"] += 1
            self.trades.append(closed)
            self._dirty = True
            print(
                f"  💀 LIQUIDATED {pos['direction'].upper()} {pos['symbol']} {pos['leverage']}x | Lost: ${pos['collateral']}"
            )

    def _run_momentum(self, prices):
        for symbol in self.config["trade_pairs"]:
            price = prices.get(symbol)
            if not price:
                continue
            mom = self.price_feed.get_momentum(
                symbol, self.config["momentum_lookback_min"]
            )
            existing = next((p for p in self.positions if p["symbol"] == symbol), None)

            if not existing:
                if (
                    mom["dir"] == "up"
                    and abs(mom["change"]) >= self.config["momentum_threshold_pct"]
                ):
                    print(f"  📈 {symbol} momentum UP +{mom['change']:.2f}%")
                    self._open_position(symbol, "long", price)
                elif (
                    mom["dir"] == "down"
                    and abs(mom["change"]) >= self.config["momentum_threshold_pct"]
                ):
                    print(f"  📉 {symbol} momentum DOWN {mom['change']:.2f}%")
                    self._open_position(symbol, "short", price)
            else:
                # Enforce minimum hold time to prevent churning
                opened_ts = datetime.fromisoformat(existing["openedAt"]).timestamp()
                held_seconds = time.time() - opened_ts
                min_hold = self.config.get("min_hold_seconds", 300)
                if held_seconds < min_hold:
                    continue

                is_long = existing["direction"] == "long"
                reversed_dir = (is_long and mom["dir"] == "down") or (
                    not is_long and mom["dir"] == "up"
                )
                if (
                    reversed_dir
                    and abs(mom["change"]) >= self.config["momentum_threshold_pct"]
                ):
                    print(f"  🔄 {symbol} reversed ({mom['change']:.2f}%)")
                    self._close_position(existing, price, "momentum_reversal")

    def tick(self):
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
                print(
                    f"    {emoji} {p['symbol']} {p['direction'].upper()} {p['leverage']}x | {u:+.2f}%"
                )

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
            print(
                f"  Balance: ${self.state['balance']:.2f} | Trades: {self.state['totalTrades']} | PnL: ${self.state['totalPnl']:+.2f}"
            )
            print(
                f"  View: {KV_READ_BASE}/v0/latest/{self.contract}/{self.account}/state\n"
            )
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)

        # Seed price history with real Binance data so strategies work immediately
        lookback = self.config.get("momentum_lookback_min", 30)
        print(f"  ⏳ Seeding {lookback}min of price history from Binance...")
        self.price_feed.seed_from_binance(self.config["trade_pairs"], lookback)

        self.tick()

        while self.running:
            time.sleep(interval)
            try:
                self.tick()
            except Exception as err:
                print(f"  ❌ Tick error: {err}")


if __name__ == "__main__":
    args = sys.argv[1:]
    subcommand = args[0] if args else None

    if subcommand == "verify":
        # Integrity check mode — just run checks and exit
        bot = PaperBot(CONFIG)
        bot.account = CONFIG["near_account"] or "unknown"
        if not bot.account and CONFIG["outlayer_api_key"]:
            bot.account = bot._get_account_id()
        ok = bot.verify_integrity()
        sys.exit(0 if ok else 1)
    elif subcommand == "tokens":
        # List supported tokens
        pf = PriceFeed()
        tokens = pf.list_supported_tokens()
        print(f"\n── Supported Tokens ({len(tokens)}) ──")
        for t in tokens:
            print(f"  {t['symbol']:8s} | {t['blockchain']:6s} | ${t['price']:>12,.4f}")
        sys.exit(0)
    elif subcommand == "status":
        # Read-only status view (no wallet needed)
        account = args[1] if len(args) > 1 else (CONFIG["near_account"] or "unknown")
        contract = CONFIG["kv_contract"]
        print(f"\n── paper-kv Status ──")
        print(f"  Account:  {account}")
        print(f"  Contract: {contract}\n")

        state = kv_get(account, contract, "state")
        positions = kv_get(account, contract, "positions") or []
        trades = kv_get(account, contract, "trades") or []

        if not state:
            print("  ❌ No data found for this account")
            print(f"  URL: {KV_READ_BASE}/v0/latest/{contract}/{account}/state\n")
            sys.exit(0)

        win_rate = (
            ((state["wins"] / state["totalTrades"]) * 100)
            if state["totalTrades"] > 0
            else 0
        )
        pnl_emoji = "🟢" if state["totalPnl"] >= 0 else "🔴"
        print(f"  💰 Balance:    ${state['balance']:.2f}")
        print(f"  📊 Total PnL:  {pnl_emoji} {state['totalPnl']:+.2f}")
        print(
            f"  📈 Trades:     {state['totalTrades']} ({state['wins']}W/{state['losses']}L) — {win_rate:.1f}% win rate"
        )
        print()

        if positions:
            print(f"  📂 Open Positions ({len(positions)}):")
            for p in positions:
                d = "🟢 LONG" if p["direction"] == "long" else "🔴 SHORT"
                print(
                    f"    {d} {p['symbol']} {p['leverage']}x | Entry: ${p['entryPrice']:,.2f} | Size: ${p['size']:,.2f} | {p.get('openedAt', '?')}"
                )
            print()

        recent = trades[-10:][::-1]
        if recent:
            print(f"  📜 Recent Trades (last {len(recent)} of {len(trades)}):")
            for t in recent:
                e = "🟢" if t["pnl"] >= 0 else "🔴"
                print(
                    f"    {e} {t['direction'].upper()} {t['symbol']} {t['leverage']}x | ${t['entryPrice']:,.2f}→${t['exitPrice']:,.2f} | {t['pnlPct']:+.2f}% (${t['pnl']:+.2f}) | {t['exitReason']}"
                )
            print()

        print(f"  🔗 {KV_READ_BASE}/v0/latest/{contract}/{account}/state\n")
        sys.exit(0)
    else:
        # Normal bot mode
        if not CONFIG["outlayer_api_key"] and not CONFIG["near_account"]:
            print("❌ Set OUTLAYER_API_KEY or NEAR_ACCOUNT")
            print("   export OUTLAYER_API_KEY=***  (recommended, gasless)")
            print(
                "   export NEAR_ACCOUNT=you.near     (fallback, needs local keychain)"
            )
            sys.exit(1)
        bot = PaperBot(CONFIG)
        bot.start()
