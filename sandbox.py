#!/usr/bin/env python3
"""
paper-kv sandbox — give an agent raw market data and let it trade.

The agent receives price data and calls open/close/save.
No strategy logic in the core. No indicators. Just the raw tools.

Usage:
  python3 sandbox.py replay 7          # Feed 7 days of candles to agent
  python3 sandbox.py live              # Feed live prices to agent
  python3 sandbox.py status            # Check on-chain state

The agent is a Python file that exposes a `tick(engine, prices, now_ms)` function.
Set AGENT=path/to/agent.py or pass --agent.
"""

import json
import os
import sys
import time
import logging
import importlib.util
import urllib.request
import urllib.error
import fcntl
from collections import defaultdict
from datetime import datetime, timezone
from merkle import build_tick_root

log = logging.getLogger("paper-kv")

# ── Config ──────────────────────────────────────────────────────────────────

KV_READ_BASE = "https://kv.main.fastnear.com"
INTENTS_TOKENS_URL = "https://1click.chaindefuser.com/v0/tokens"
LOCAL_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ── KV ──────────────────────────────────────────────────────────────────────

def kv_get(account, contract, key):
    try:
        url = f"{KV_READ_BASE}/v0/latest/{contract}/{account}/{key}"
        req = urllib.request.Request(url, headers={"User-Agent": "paper-kv/3.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        entries = json.loads(resp.read()).get("entries", [])
        if not entries: return None
        v = entries[0].get("value")
        return json.loads(v) if isinstance(v, str) else v
    except urllib.error.HTTPError as e:
        if e.code == 404: return None
        raise RuntimeError(f"KV get HTTP {e.code}") from e
    except Exception as e:
        raise RuntimeError(f"KV get failed: {e}") from e

def kv_write(account, contract, data_dict, api_key="", api_base="", retries=2):
    body = json.dumps({"receiver_id": contract, "method_name": "__fastdata_kv",
                       "args": data_dict, "gas": "300000000000000"}).encode()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(f"{api_base}/wallet/v1/call", data=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read())
            return result.get("status") == "success"
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) and attempt < retries:
                time.sleep((attempt + 1)); continue
            return False
        except Exception:
            if attempt < retries: time.sleep((attempt + 1)); continue
            return False
    return False

def local_save(data):
    try:
        tmp = LOCAL_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(data, f, indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, LOCAL_STATE_FILE)
    except Exception as e: log.error("Local save: %s", e)

def local_load():
    try:
        if os.path.exists(LOCAL_STATE_FILE):
            with open(LOCAL_STATE_FILE) as f: return json.load(f)
    except: pass
    return None

# ── Price Feed ───────────────────────────────────────────────────────────────

class PriceFeed:
    """Raw price data. Agent reads cache directly, no pre-computed indicators."""
    def __init__(self):
        self.cache = defaultdict(list)  # sym -> [{ts, open, high, low, close, volume}, ...]

    def push(self, sym, ts_ms, candle):
        """Push a full candle. candle = {open, high, low, close, volume} or just a price."""
        if isinstance(candle, (int, float)):
            candle = {"close": float(candle)}
        candle = {k: float(v) for k, v in candle.items() if k != "ts"}
        candle["ts"] = ts_ms
        if candle.get("close", 0) <= 0: return
        self.cache[sym].append(candle)
        cutoff = ts_ms - 24 * 3600 * 1000  # keep 24h
        self.cache[sym] = [c for c in self.cache[sym] if c["ts"] >= cutoff]

    def latest(self, sym):
        """Get the latest candle for a symbol."""
        pts = self.cache.get(sym, [])
        return pts[-1] if pts else None

    def history(self, sym, lookback_ms=None):
        """Get price history for a symbol. lookback_ms = how far back."""
        pts = self.cache.get(sym, [])
        if lookback_ms and pts:
            now = pts[-1]["ts"]
            return [p for p in pts if p["ts"] >= now - lookback_ms]
        return list(pts)

    def fetch_live(self, symbols):
        """Fetch live prices from NEAR Intents."""
        try:
            req = urllib.request.Request(INTENTS_TOKENS_URL, headers={"User-Agent": "paper-kv/3.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            token_map = {t["symbol"]: t.get("price", 0) for t in json.loads(resp.read())}
        except Exception as e:
            log.error("Live fetch: %s", e); return {}
        now_ms = int(time.time() * 1000)
        prices = {}
        for s in symbols:
            p = token_map.get(s, 0)
            if p > 0:
                prices[s] = p
                self.push(s, now_ms, {"close": p})
        return prices

    def seed_binance(self, symbols, minutes):
        """Seed cache from Binance 1m candles."""
        smap = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","wNEAR":"NEARUSDT","NEAR":"NEARUSDT"}
        now_ms = int(time.time() * 1000); start_ms = now_ms - (minutes + 5) * 60 * 1000
        for sym in symbols:
            try:
                url = f"https://api.binance.com/api/v3/klines?symbol={smap.get(sym, sym+'USDT')}&interval=1m&startTime={start_ms}&endTime={now_ms}&limit=1000"
                raw = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"paper-kv/3.0"}), timeout=10).read())
                for k in raw:
                    self.push(sym, k[0], {"open": float(k[1]), "high": float(k[2]),
                                           "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])})
                print(f"  📊 {sym}: {len(raw)} candles")
            except Exception as e:
                print(f"  ⚠️ {sym}: {e}")

    @staticmethod
    def fetch_history(symbols, days, interval="5m"):
        """Fetch full history for replay."""
        smap = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","wNEAR":"NEARUSDT","NEAR":"NEARUSDT"}
        result = {}
        now_ms = int(time.time() * 1000); start_ms = now_ms - days * 24 * 3600 * 1000
        for sym in symbols:
            bsym = smap.get(sym, f"{sym}USDT"); all_c = []; cur = start_ms
            while cur < now_ms:
                url = f"https://api.binance.com/api/v3/klines?symbol={bsym}&interval={interval}&startTime={cur}&limit=1000"
                try:
                    raw = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"paper-kv/3.0"}), timeout=15).read())
                except: break
                if not raw: break
                for k in raw:
                    all_c.append({"ts": k[0], "open": float(k[1]), "high": float(k[2]),
                                  "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])})
                cur = raw[-1][0] + 1; time.sleep(0.15)
            result[sym] = all_c; print(f"  {sym}: {len(all_c)} candles")
        return result

# ── Engine ──────────────────────────────────────────────────────────────────

class Engine:
    """Raw trading sandbox. Agent has full access to price data and position management."""
    
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
        self._kv_fails = 0
        self._max_kv_fails = 10
        self._tick_roots = []

    # ── Raw data access (agent reads these directly) ────────────────────
    
    @property
    def balance(self): return self.state["balance"]
    
    @property  
    def open_count(self): return len(self.positions)
    
    def has_position(self, symbol, direction=None):
        """Check if there's an open position for a symbol (optionally filtered by direction)."""
        for p in self.positions:
            if p["symbol"] == symbol:
                if direction is None or p["direction"] == direction:
                    return p
        return None

    def unrealized_pnl(self, pos, current_price):
        """Calculate unrealized PnL for a position at given price."""
        if pos["direction"] == "long":
            return ((current_price - pos["entryPrice"]) / pos["entryPrice"]) * pos["leverage"] * pos["collateral"]
        return ((pos["entryPrice"] - current_price) / pos["entryPrice"]) * pos["leverage"] * pos["collateral"]

    # ── Position management ─────────────────────────────────────────────

    def open(self, symbol, direction, price, now_ms=None, **kwargs):
        """Open a position. Returns pos dict or None.
        
        kwargs can override: leverage, collateral, take_profit, stop_loss"""
        now_ms = now_ms or int(time.time() * 1000)
        if direction not in ("long", "short"): return None
        if price <= 0: return None
        
        leverage = kwargs.get("leverage", self.config.get("leverage", 5))
        collateral = kwargs.get("collateral", self.config.get("trade_size", 100))
        max_open = self.config.get("max_open", 10)
        
        if len(self.positions) >= max_open: return None
        if any(p["symbol"] == symbol and p["direction"] == direction for p in self.positions): return None
        if collateral > self.state["balance"]: return None
        
        mm = 0.005
        liq = price * (1 - 1/leverage + mm) if direction == "long" else price * (1 + 1/leverage - mm)
        
        pos = {
            "id": f"{int(now_ms)}-{os.urandom(3).hex()}",
            "symbol": symbol, "direction": direction,
            "entryPrice": price, "leverage": leverage,
            "size": collateral * leverage, "collateral": collateral,
            "liquidationPrice": liq,
            "openedAt": datetime.fromtimestamp(now_ms/1000, tz=timezone.utc).isoformat(),
            "price_ts": now_ms,
            "price_source": kwargs.get("price_source", ""),
        }
        if "take_profit" in kwargs: pos["take_profit"] = kwargs["take_profit"]
        if "stop_loss" in kwargs: pos["stop_loss"] = kwargs["stop_loss"]
        
        self.positions.append(pos)
        self.state["balance"] -= collateral
        self._dirty = True
        return pos

    def close(self, pos, price, reason, now_ms=None):
        """Close a position. Returns pnl or None."""
        if pos not in self.positions: return None
        if price <= 0: return None
        self.positions.remove(pos)
        
        lev = pos["leverage"]
        pnl_pct = ((price - pos["entryPrice"]) / pos["entryPrice"] * lev * 100
                   if pos["direction"] == "long" else
                   (pos["entryPrice"] - price) / pos["entryPrice"] * lev * 100)
        pnl = pos["collateral"] * pnl_pct / 100
        
        self.state["balance"] += pos["collateral"] + pnl
        self.state["totalTrades"] += 1
        self.state["totalPnl"] += pnl
        self.state["wins" if pnl >= 0 else "losses"] += 1
        self.trades.append({**pos, "exitPrice": price, "pnl": round(pnl, 2),
            "pnlPct": round(pnl_pct, 2),
            "closedAt": datetime.fromtimestamp((now_ms or int(time.time()*1000))/1000, tz=timezone.utc).isoformat(),
            "exitReason": reason, "close_price_ts": now_ms or int(time.time()*1000)})
        self._dirty = True
        return pnl

    def check_liquidations(self, prices, now_ms=None):
        """Check and liquidate any positions past their liquidation price."""
        for pos in list(self.positions):
            price = prices.get(pos["symbol"])
            if not price: continue
            if (pos["direction"] == "long" and price <= pos["liquidationPrice"]) or \
               (pos["direction"] == "short" and price >= pos["liquidationPrice"]):
                self.close(pos, price, "liquidated", now_ms)

    # ── Persistence ─────────────────────────────────────────────────────

    def load(self):
        try: state = kv_get(self.account, self.contract, "state")
        except: state = None
        if state:
            self.state = state
            self.positions = kv_get(self.account, self.contract, "positions") or []
            self.trades = kv_get(self.account, self.contract, "trades") or []
        else:
            local = local_load()
            if local:
                self.state = local.get("state", self.state)
                self.positions = local.get("positions", [])
                self.trades = local.get("trades", [])
        return self

    def save(self, now_ms=None):
        if not self._dirty: return self
        if len(self.trades) > 500: self.trades = self.trades[-500:]
        
        data = {"state": dict(self.state), "positions": self.positions, "trades": self.trades}
        
        # Merkle root
        clean = {k:v for k,v in self.state.items() if k not in ('merkle_root','tick_count','last_tick_ts','last_prev_root')}
        prev_root = self._tick_roots[-1]["root"] if self._tick_roots else ""
        tick_ts = now_ms or int(time.time() * 1000)
        tick_root = build_tick_root(clean, self.positions, self.trades, tick_ts=tick_ts, prev_root=prev_root)
        self._tick_roots.append({"root": tick_root, "ts": tick_ts, "prev_root": prev_root})
        self.state["merkle_root"] = tick_root
        self.state["tick_count"] = len(self._tick_roots)
        self.state["last_tick_ts"] = tick_ts
        self.state["last_prev_root"] = prev_root
        
        local_save(data)
        
        if self._kv_fails >= self._max_kv_fails or not self.api_key:
            self._dirty = False; return self
        
        ok = kv_write(self.account, self.contract,
                       {"state": self.state, "positions": self.positions, "trades": self.trades},
                       api_key=self.api_key, api_base=self.api_base)
        self._kv_fails = 0 if ok else self._kv_fails + 1
        self._dirty = False
        return self

    # ── Run modes ───────────────────────────────────────────────────────

    def replay(self, candles_by_sym, agent_tick, save_every=200, on_save=None):
        """Feed historical candles to agent. agent_tick(engine, prices, now_ms)"""
        all_ts = sorted(set(c["ts"] for cs in candles_by_sym.values() for c in cs))
        for i, ts in enumerate(all_ts):
            prices = {}
            for sym, cs in candles_by_sym.items():
                for c in cs:
                    if c["ts"] == ts:
                        prices[sym] = c["close"]
                        self.feed.push(sym, ts, c)
                        break
            self.check_liquidations(prices, ts)
            self._dirty = True  # auto-save every tick
            try: agent_tick(self, prices, ts)
            except Exception as e: log.error("Agent tick %d: %s", i, e, exc_info=True)
            self.save(now_ms=ts)
            if on_save: on_save(i + 1, len(all_ts), ts)
        # Close remaining
        for pos in list(self.positions):
            last = candles_by_sym.get(pos["symbol"], [{}])[-1].get("close", pos["entryPrice"])
            self.close(pos, last, "replay_end", all_ts[-1] if all_ts else int(time.time()*1000))
        self._dirty = True; self.save(now_ms=all_ts[-1] if all_ts else int(time.time()*1000))
        return self

    def run_live(self, agent_tick, poll_s=60, tick_s=300, on_tick=None):
        """Feed live prices to agent. agent_tick(engine, prices, now_ms)"""
        pairs = self.config.get("pairs", ["BTC","ETH","SOL","wNEAR"])
        last_tick = 0
        while True:
            now_ms = int(time.time() * 1000)
            try:
                prices = self.feed.fetch_live(pairs)
                if not prices: time.sleep(poll_s); continue
            except: time.sleep(poll_s); continue
            
            # Liquidation check every poll
            self.check_liquidations(prices, now_ms)
            
            # Agent tick every tick_s
            if now_ms / 1000 - last_tick >= tick_s:
                last_tick = now_ms / 1000
                try: agent_tick(self, prices, now_ms)
                except Exception as e: log.error("Agent tick: %s", e, exc_info=True)
                self._dirty = True; self.save(now_ms=now_ms)
                if on_tick: on_tick(prices)
            elif self._dirty:
                # Auto-save even between agent ticks (e.g. after liquidation)
                self.save(now_ms=now_ms)
            time.sleep(poll_s)

# ── Agent loader ────────────────────────────────────────────────────────────

def load_agent(path):
    """Load an agent module from a file path. Must expose tick(engine, prices, now_ms)."""
    spec = importlib.util.spec_from_file_location("agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "tick"):
        raise ImportError(f"Agent {path} has no tick(engine, prices, now_ms) function")
    return mod.tick

# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "live"

    env = {}
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    
    # Env overrides
    for k, v in env.items(): os.environ.setdefault(k, v)

    config = {
        "pairs": os.environ.get("PAIRS", "BTC,ETH,SOL,wNEAR").split(","),
        "leverage": float(os.environ.get("LEVERAGE", "5")),
        "trade_size": float(os.environ.get("TRADE_SIZE", "100")),
        "initial_balance": float(os.environ.get("INITIAL_BALANCE", "10000")),
        "max_open": int(os.environ.get("MAX_OPEN", "10")),
        "near_account": os.environ.get("NEAR_ACCOUNT", ""),
        "kv_contract": os.environ.get("KV_CONTRACT", "contextual.near"),
        "outlayer_api_key": os.environ.get("OUTLAYER_API_KEY", ""),
        "outlayer_api": os.environ.get("OUTLAYER_API_BASE", "https://api.outlayer.fastnear.com"),
        "poll_interval_s": float(os.environ.get("POLL_INTERVAL_S", "60")),
        "check_interval_s": float(os.environ.get("CHECK_INTERVAL_S", "300")),
    }

    # Load agent
    agent_path = None
    for i, a in enumerate(args):
        if a in ("--agent", "-a") and i + 1 < len(args):
            agent_path = args[i + 1]; break
    agent_path = agent_path or os.environ.get("AGENT", "")
    
    # Try default locations
    if not agent_path:
        for p in ["agent.py", "strategies/momentum.py"]:
            full = os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
            if os.path.exists(full): agent_path = full; break

    if cmd in ("replay", "live") and not agent_path:
        print("No agent specified. Create agent.py or use --agent path/to/agent.py")
        print("Agent file must have: def tick(engine, prices, now_ms)")
        sys.exit(1)

    if cmd == "replay":
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
        agent_tick = load_agent(agent_path)
        print(f"⏳ Fetching {days}d of {', '.join(config['pairs'])}...")
        candles = PriceFeed.fetch_history(config["pairs"], days, "5m")
        print(f"── Feeding to agent: {agent_path} ──\n")
        
        eng = Engine(config)
        def on_save(i, total, ts):
            s = eng.state
            ts_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"  📊 {ts_str} | {i}/{total} | ${s['balance']:.2f} | {s['totalTrades']} trades | PnL: ${s['totalPnl']:+.2f}")
        
        eng.replay(candles, agent_tick, save_every=500, on_save=on_save)
        s = eng.state
        wr = (s["wins"]/s["totalTrades"]*100) if s["totalTrades"] > 0 else 0
        e = "🟢" if s["totalPnl"] >= 0 else "🔴"
        print(f"\n═══ Done ═══")
        print(f"  💰 ${s['balance']:.2f} | {e} ${s['totalPnl']:+.2f} | {s['totalTrades']} trades ({s['wins']}W/{s['losses']}L) {wr:.1f}%")

    elif cmd == "live":
        agent_tick = load_agent(agent_path)
        eng = Engine(config).load()
        print(f"⚡ paper-kv live | Agent: {agent_path}")
        eng.feed.seed_binance(config["pairs"], 5)
        import signal
        signal.signal(signal.SIGINT, lambda s,f: (setattr(eng,'_dirty',True), eng.save(), sys.exit(0)))
        eng.run_live(agent_tick, poll_s=config["poll_interval_s"], tick_s=config["check_interval_s"])

    elif cmd == "status":
        acc = args[1] if len(args) > 1 else config["near_account"]
        contract = config["kv_contract"]
        if not acc: print("  ❌ No account"); sys.exit(0)
        try:
            state = kv_get(acc, contract, "state")
            trades = kv_get(acc, contract, "trades") or []
        except Exception as e:
            print(f"  ❌ {e}"); sys.exit(1)
        if not state: print("  ❌ No data"); sys.exit(0)
        wr = (state["wins"]/state["totalTrades"]*100) if state["totalTrades"] > 0 else 0
        e = "🟢" if state["totalPnl"] >= 0 else "🔴"
        print(f"\n  💰 ${state['balance']:.2f} | {e} ${state['totalPnl']:+.2f} | {state['totalTrades']} trades ({state['wins']}W/{state['losses']}L) {wr:.1f}%")
        print(f"  🔗 {KV_READ_BASE}/v0/latest/{contract}/{acc}/state\n")
    
    else:
        print("Usage: sandbox.py [--agent path] [replay DAYS|live|status]")
