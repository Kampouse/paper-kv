#!/usr/bin/env python3
"""paper-kv engine — composable core. No CLI, no side effects on import."""

import fcntl
import json
import logging
import os
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone

from merkle import build_tick_root

log = logging.getLogger("paper-kv")

KV_READ_BASE = "https://kv.main.fastnear.com"
INTENTS_TOKENS_URL = "https://1click.chaindefuser.com/v0/tokens"
LOCAL_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ── KV ──────────────────────────────────────────────────────────────────────

class KVError(Exception):
    """Raised on non-recoverable KV errors."""
    pass

def kv_get(account, contract, key):
    """Read from KV. Raises KVError on server errors. Returns None on 404."""
    url = f"{KV_READ_BASE}/v0/latest/{contract}/{account}/{key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "paper-kv/3.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        entries = json.loads(resp.read()).get("entries", [])
        if not entries:
            return None
        v = entries[0].get("value")
        return json.loads(v) if isinstance(v, str) else v
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log.error("KV get %s failed: HTTP %d", key, e.code)
        raise KVError(f"KV get HTTP {e.code} for {key}") from e
    except Exception as e:
        log.error("KV get %s failed: %s", key, e)
        raise KVError(f"KV get failed for {key}: {e}") from e

def kv_write(account, contract, data_dict, api_key="", api_base="", retries=2):
    """Write to KV via OutLayer. Retries on transient errors. Raises KVError on permanent failure."""
    body = json.dumps({"receiver_id": contract, "method_name": "__fastdata_kv",
                       "args": data_dict, "gas": "300000000000000"}).encode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(f"{api_base}/wallet/v1/call", data=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read())
            if result.get("status") == "success":
                return True
            status = result.get("status", "unknown")
            if status == "pending_approval":
                log.warning("KV write pending approval — wallet needs auto-approve")
                return False
            log.error("KV write returned status: %s", status)
            return False
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (502, 503, 504) and attempt < retries:
                log.warning("KV write retry %d/%d (HTTP %d)", attempt + 1, retries, e.code)
                time.sleep(1 * (attempt + 1))
                continue
            log.error("KV write failed: HTTP %d", e.code)
            return False
        except Exception as e:
            last_err = e
            if attempt < retries:
                log.warning("KV write retry %d/%d: %s", attempt + 1, retries, e)
                time.sleep(1 * (attempt + 1))
                continue
            log.error("KV write failed after %d retries: %s", retries, e)
            return False
    return False

def local_save(data):
    """Atomic write to local state file with file locking."""
    try:
        tmp = LOCAL_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LOCAL_STATE_FILE)
    except Exception as e:
        log.error("Local save failed: %s", e)

def local_load():
    """Load from local state file. Returns None if missing or corrupt."""
    try:
        if os.path.exists(LOCAL_STATE_FILE):
            with open(LOCAL_STATE_FILE) as f:
                return json.load(f)
    except json.JSONDecodeError as e:
        log.error("Local state corrupt: %s", e)
    except Exception as e:
        log.error("Local load failed: %s", e)
    return None

# ── PriceFeed ───────────────────────────────────────────────────────────────

class PriceFeed:
    def __init__(self):
        self.cache = defaultdict(list)  # sym -> [{ts, close}]

    def push(self, sym, ts_ms, price):
        """Push a price point into the cache. Used by both live and replay."""
        if price <= 0:
            return
        self.cache[sym].append({"ts": ts_ms, "close": price})
        cutoff = ts_ms - 4 * 3600 * 1000
        self.cache[sym] = [p for p in self.cache[sym] if p["ts"] >= cutoff]

    def momentum(self, sym, lookback_min, now_ms):
        """Returns (change_pct, 'up'/'down'/'flat'). Returns (None, 'no_data') if insufficient history."""
        pts = [p for p in self.cache.get(sym, []) if p["ts"] >= now_ms - lookback_min * 60 * 1000]
        if len(pts) < 2:
            return None, "no_data"
        change = ((pts[-1]["close"] - pts[0]["close"]) / pts[0]["close"]) * 100
        return change, "up" if change > 0 else "down" if change < 0 else "flat"

    def fetch_live(self, symbols):
        try:
            req = urllib.request.Request(INTENTS_TOKENS_URL, headers={"User-Agent": "paper-kv/3.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            token_map = {t["symbol"]: t.get("price", 0) for t in json.loads(resp.read())}
        except Exception as e:
            log.error("Live price fetch failed: %s", e)
            token_map = {}
        now_ms = int(time.time() * 1000)
        prices = {}
        for s in symbols:
            p = token_map.get(s, 0)
            if p > 0:
                prices[s] = p
                self.push(s, now_ms, p)
        return prices

    def seed_binance(self, symbols, lookback_min):
        smap = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","wNEAR":"NEARUSDT","NEAR":"NEARUSDT"}
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (lookback_min + 5) * 60 * 1000
        for sym in symbols:
            try:
                url = (f"https://api.binance.com/api/v3/klines?symbol={smap.get(sym, sym+'USDT')}"
                       f"&interval=1m&startTime={start_ms}&endTime={now_ms}&limit=1000")
                raw = json.loads(urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": "paper-kv/3.0"}), timeout=10).read())
                for k in raw:
                    self.push(sym, k[0], float(k[4]))
                print(f"  📊 Seeded {sym}: {len(raw)} candles")
            except Exception as e:
                print(f"  ⚠️ Seed {sym}: {e}")

    @staticmethod
    def fetch_history(symbols, days, interval="5m"):
        smap = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","wNEAR":"NEARUSDT","NEAR":"NEARUSDT"}
        result = {}
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - days * 24 * 3600 * 1000
        for sym in symbols:
            bsym = smap.get(sym, f"{sym}USDT")
            all_c = []
            cur = start_ms
            while cur < now_ms:
                url = (f"https://api.binance.com/api/v3/klines?symbol={bsym}"
                       f"&interval={interval}&startTime={cur}&limit=1000")
                try:
                    raw = json.loads(urllib.request.urlopen(
                        urllib.request.Request(url, headers={"User-Agent": "paper-kv/3.0"}), timeout=15).read())
                except Exception as e:
                    log.error("Binance fetch %s failed at %d: %s", sym, cur, e)
                    break
                if not raw:
                    break
                for k in raw:
                    all_c.append({"ts": k[0], "open": float(k[1]), "high": float(k[2]),
                                  "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])})
                cur = raw[-1][0] + 1
                time.sleep(0.15)
            result[sym] = all_c
            print(f"  {sym}: {len(all_c)} candles")
        return result

# ── Engine ──────────────────────────────────────────────────────────────────

class Engine:
    def __init__(self, config):
        self.config = config
        self.feed = PriceFeed()
        self.account = config.get("near_account", "")
        self.contract = config.get("kv_contract", "contextual.near")
        self.api_key = config.get("outlayer_api_key", "")
        self.api_base = config.get("outlayer_api", "https://api.outlayer.fastnear.com")
        self.state = {"balance": config.get("initial_balance", 10000), "totalTrades": 0,
                      "wins": 0, "losses": 0, "totalPnl": 0}
        self.positions = []
        self.trades = []
        self._dirty = False
        self._kv_fail_count = 0
        self._max_kv_fails = 10
        self._strategy_mod = None
        self._tick_roots = []
        self._last_tick_ts = 0  # timestamp of last price observation

    def _now_ms(self):
        return int(time.time() * 1000)

    def _now_iso(self, now_ms=None):
        return datetime.fromtimestamp((now_ms or self._now_ms()) / 1000, tz=timezone.utc).isoformat()

    # ── Persistence ─────────────────────────────────────────────────────

    def load(self):
        """Load state from KV, falling back to local file. Raises on total failure."""
        state = None
        try:
            state = kv_get(self.account, self.contract, "state")
            positions = kv_get(self.account, self.contract, "positions")
            trades = kv_get(self.account, self.contract, "trades")
        except KVError as e:
            log.warning("KV load failed, trying local: %s", e)
        
        if state is None:
            local = local_load()
            if local:
                state = local.get("state")
                positions = local.get("positions")
                trades = local.get("trades")
                if state:
                    log.info("Loaded from local state.json")
        
        if state is None:
            log.info("Fresh account — starting with $%.2f", self.config.get("initial_balance", 10000))
            self._dirty = True
        else:
            self.state = state
            self.positions = positions or []
            self.trades = trades or []
        return self

    def save(self):
        """Persist state. Local save is guaranteed. KV is best-effort with circuit breaker."""
        if not self._dirty:
            return self
        if len(self.trades) > 500:
            self.trades = self.trades[-500:]
        
        # Always save locally first (atomic)
        data = {"state": self.state, "positions": self.positions, "trades": self.trades}
        
        # Compute Merkle root for tamper-proofing (chained with prev root + timestamp)
        prev_root = self._tick_roots[-1] if self._tick_roots else ""
        tick_root = build_tick_root(self.state, self.positions, self.trades,
                                     tick_ts=int(time.time() * 1000), prev_root=prev_root)
        self._tick_roots.append(tick_root)
        data["merkle_root"] = tick_root
        data["tick_count"] = len(self._tick_roots)
        
        local_save(data)
        
        # Circuit breaker: skip KV if too many consecutive failures
        if self._kv_fail_count >= self._max_kv_fails:
            log.warning("KV circuit breaker open (%d consecutive failures), skipping", self._kv_fail_count)
            self._dirty = False
            return self
        
        if not self.api_key:
            self._dirty = False
            return self
        
        ok = kv_write(self.account, self.contract,
                       {"state": self.state, "positions": self.positions, "trades": self.trades},
                       api_key=self.api_key, api_base=self.api_base)
        if ok:
            self._kv_fail_count = 0
        else:
            self._kv_fail_count += 1
            log.warning("KV write failed (%d/%d consecutive)", self._kv_fail_count, self._max_kv_fails)
        
        self._dirty = False
        return self

    # ── Core actions ────────────────────────────────────────────────────

    def open(self, sym, direction, price, now_ms=None):
        """Open a position. Returns pos dict or None if blocked."""
        if direction not in ("long", "short"):
            log.error("Invalid direction: %s", direction)
            return None
        if price <= 0:
            log.error("Invalid price: %s", price)
            return None
        now_ms = now_ms or self._now_ms()
        c = self.config
        collateral = c.get("trade_size", 100)
        lev = c.get("leverage", 5)
        if len(self.positions) >= c.get("max_open", 10):
            return None
        if any(p["symbol"] == sym and p["direction"] == direction for p in self.positions):
            return None
        if collateral > self.state["balance"]:
            return None
        mm = 0.005
        liq = price * (1 - 1 / lev + mm) if direction == "long" else price * (1 + 1 / lev - mm)
        pos = {
            "id": f"{int(now_ms)}-{os.urandom(3).hex()}",
            "symbol": sym, "direction": direction,
            "entryPrice": price, "leverage": lev,
            "size": collateral * lev, "collateral": collateral,
            "liquidationPrice": liq, "fundingFeesPaid": 0,
            "openedAt": self._now_iso(now_ms),
            "price_ts": now_ms,
            "price_source": "binance" if now_ms < self._now_ms() - 60000 else "intents",
        }
        self.positions.append(pos)
        self.state["balance"] -= collateral
        self._dirty = True
        return pos

    def close(self, pos, price, reason, now_ms=None):
        """Close a position. Returns pnl or None if position not found."""
        if pos not in self.positions:
            log.warning("Attempted to close position not in list: %s %s", pos.get("symbol"), pos.get("id"))
            return None
        if price <= 0:
            log.error("Invalid close price: %s", price)
            return None
        self.positions.remove(pos)
        lev = pos["leverage"]
        if pos["direction"] == "long":
            pnl_pct = ((price - pos["entryPrice"]) / pos["entryPrice"]) * lev * 100
        else:
            pnl_pct = ((pos["entryPrice"] - price) / pos["entryPrice"]) * lev * 100
        pnl = pos["collateral"] * pnl_pct / 100
        self.state["balance"] += pos["collateral"] + pnl
        self.state["totalTrades"] += 1
        self.state["totalPnl"] += pnl
        self.state["wins" if pnl >= 0 else "losses"] += 1
        self.trades.append({
            **pos, "exitPrice": price, "pnl": round(pnl, 2), "pnlPct": round(pnl_pct, 2),
            "closedAt": self._now_iso(now_ms), "exitReason": reason,
            "close_price_ts": now_ms,
        })
        self._dirty = True
        return pnl

    # ── Strategy step ───────────────────────────────────────────────────

    def _load_strategy(self, name):
        """Load and cache strategy module. Raises ImportError on bad name."""
        if self._strategy_mod is None or self._strategy_mod.__name__ != f"strategies.{name}":
            import importlib
            try:
                self._strategy_mod = importlib.import_module(f"strategies.{name}")
            except ImportError as e:
                raise ImportError(f"Strategy '{name}' not found in strategies/ — {e}") from e
            if not hasattr(self._strategy_mod, "step"):
                raise ImportError(f"Strategy '{name}' has no step(engine, prices, now_ms) function")
        return self._strategy_mod

    def step(self, prices, now_ms=None):
        """Run one strategy tick: liquidations first, then strategy.
        Stores now_ms as price_ts for trade provenance."""
        now_ms = now_ms or self._now_ms()
        self._last_tick_ts = now_ms  # for price provenance

        # Liquidations (always run, use copy to avoid mutation during iteration)
        for pos in list(self.positions):
            price = prices.get(pos["symbol"])
            if not price:
                continue
            if (pos["direction"] == "long" and price <= pos["liquidationPrice"]) or \
               (pos["direction"] == "short" and price >= pos["liquidationPrice"]):
                self.close(pos, price, "liquidated", now_ms)

        # Run strategy
        strategy = self.config.get("strategy")
        if callable(strategy):
            try:
                strategy(self, prices, now_ms)
            except Exception as e:
                log.error("Strategy error: %s", e, exc_info=True)
        elif isinstance(strategy, str):
            try:
                mod = self._load_strategy(strategy)
                mod.step(self, prices, now_ms)
            except Exception as e:
                log.error("Strategy '%s' error: %s", strategy, e, exc_info=True)
        else:
            log.error("No strategy configured")

    # ── Runners ─────────────────────────────────────────────────────────

    def replay(self, candles_by_sym, save_every=200, on_save=None):
        """Replay candles through the engine. candles_by_sym = {sym: [{ts, close}, ...]}"""
        all_ts = sorted(set(c["ts"] for cs in candles_by_sym.values() for c in cs))
        for i, ts in enumerate(all_ts):
            prices = {}
            for sym, cs in candles_by_sym.items():
                for c in cs:
                    if c["ts"] == ts:
                        prices[sym] = c["close"]
                        self.feed.push(sym, ts, c["close"])
                        break
            try:
                self.step(prices, ts)
            except Exception as e:
                log.error("Tick %d error: %s", i, e, exc_info=True)
            if (i + 1) % save_every == 0 or i == len(all_ts) - 1:
                self._dirty = True
                self.save()
                if on_save:
                    on_save(i + 1, len(all_ts), ts)
        # Close remaining positions
        last_ts = all_ts[-1] if all_ts else int(time.time() * 1000)
        for pos in list(self.positions):
            last = candles_by_sym.get(pos["symbol"], [{}])[-1].get("close", pos["entryPrice"])
            self.close(pos, last, "replay_end", last_ts)
        self._dirty = True
        self.save()
        return self

    def run_live(self, on_tick=None):
        """Run live. Polls prices every poll_s, runs strategy every tick_s.
        Price polling is fast (keeps cache fresh for TP/SL checks).
        Strategy runs less often (avoids overtrading)."""
        tick_s = self.config.get("check_interval_s", 300)   # strategy every 5min
        poll_s = self.config.get("poll_interval_s", 60)      # price poll every 1min
        pairs = self.config.get("pairs", ["BTC", "ETH", "SOL", "wNEAR"])
        print(f"  ⏳ Seeding {self.config.get('lookback_min', 15)}min...")
        self.feed.seed_binance(pairs, self.config.get("lookback_min", 15))
        
        last_tick = 0
        while True:
            now_ms = int(time.time() * 1000)
            now_s = now_ms / 1000
            
            # Always fetch prices (keeps cache fresh for TP/SL)
            try:
                prices = self.feed.fetch_live(pairs)
                if not prices:
                    log.warning("No prices fetched")
                    time.sleep(poll_s)
                    continue
            except Exception as e:
                log.error("Price poll error: %s", e)
                time.sleep(poll_s)
                continue
            
            # Check liquidations every poll (fast, just price comparison)
            for pos in list(self.positions):
                price = prices.get(pos["symbol"])
                if not price:
                    continue
                if (pos["direction"] == "long" and price <= pos["liquidationPrice"]) or \
                   (pos["direction"] == "short" and price >= pos["liquidationPrice"]):
                    self.close(pos, price, "liquidated", now_ms)
                    self._dirty = True
                    self.save()
                    if on_tick:
                        on_tick(prices)
            
            # Run full strategy only every tick_s
            if now_s - last_tick >= tick_s:
                last_tick = now_s
                try:
                    self.step(prices, now_ms)
                except Exception as e:
                    log.error("Strategy tick error: %s", e, exc_info=True)
                self._dirty = True
                self.save()
                if on_tick:
                    on_tick(prices)
            
            time.sleep(poll_s)
