#!/usr/bin/env bash
# paper-kv setup — onboarding flow
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✅ $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; exit 1; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
info() { echo -e "${CYAN}$1${NC}"; }

load_env() {
    local envfile="$1"
    [ ! -f "$envfile" ] && return 1
    while IFS='=' read -r key val; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        val="${val#\"}" ; val="${val%\"}"
        val="${val#\'}" ; val="${val%\'}"
        export "$key=$val"
    done < "$envfile"
}

echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║   paper-kv setup                              ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""

# ── Step 1: API key ──────────────────────────────────────────────────────

if [ -f .env ] && grep -q "^OUTLAYER_API_KEY=wk_" .env 2>/dev/null; then
    load_env .env
    ok "Found API key in .env"
else
    echo -e "${CYAN}── Step 1: OutLayer Wallet ──${NC}"
    echo ""
    echo "  1) Paste existing API key (wk_...)"
    echo "  2) Register new wallet (free)"
    echo ""
    read -rp "  Paste API key (or Enter to register): " API_KEY_INPUT

    if [ -n "$API_KEY_INPUT" ]; then
        if [[ ! "$API_KEY_INPUT" =~ ^wk_[a-f0-9]{64}$ ]]; then
            fail "Invalid key format. Expected wk_ + 64 hex chars."
        fi
        OUTLAYER_API_KEY="$API_KEY_INPUT"
    else
        info "  Registering..."
        RESP=$(curl -sf -X POST https://api.outlayer.fastnear.com/register 2>/dev/null) || \
            fail "Registration failed (network error)"
        OUTLAYER_API_KEY=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('api_key',''))" 2>/dev/null) || true
        [ -z "$OUTLAYER_API_KEY" ] && fail "No key in response: ${RESP:0:200}"
        ACCOUNT_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('near_account_id',''))" 2>/dev/null || echo "")
        HANDOFF=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('handoff_url',''))" 2>/dev/null || echo "")
        ok "Wallet registered!"
        echo ""
        warn "  Save this key (shown once): $OUTLAYER_API_KEY"
        if [ -n "$HANDOFF" ]; then
            echo ""
            echo "  🔗 Set auto-approve: $HANDOFF"
            read -rp "  Press Enter once auto-approve is enabled..."
        fi
    fi

    ACCOUNT_ID=$(curl -sf "https://api.outlayer.fastnear.com/wallet/v1/balance?token=wrap.near&source=intents" \
        -H "Authorization: Bearer $OUTLAYER_API_KEY" 2>/dev/null | \
        python3 -c "import sys,json;print(json.load(sys.stdin).get('account_id',''))" 2>/dev/null || echo "")
    [ -z "$ACCOUNT_ID" ] && warn "Could not fetch account ID"

    cat > .env << ENVFILE
OUTLAYER_API_KEY=$OUTLAYER_API_KEY
NEAR_ACCOUNT=$ACCOUNT_ID
KV_CONTRACT=contextual.near
STRATEGY=momentum
INITIAL_BALANCE=10000
TRADE_SIZE=100
LEVERAGE=5
TP_PCT=2.5
SL_PCT=1.5
PAIRS=BTC,ETH,SOL,wNEAR
ENVFILE
    ok ".env written"
fi

load_env .env
echo ""

# ── Step 2: Verify KV ────────────────────────────────────────────────────

echo -e "${CYAN}── Step 2: Verify On-Chain KV ──${NC}"
echo ""

OUTLAYER_API_KEY="${OUTLAYER_API_KEY:-}"
KV_CONTRACT="${KV_CONTRACT:-contextual.near}"
NEAR_ACCOUNT="${NEAR_ACCOUNT:-}"

[ -z "$OUTLAYER_API_KEY" ] && fail "No API key"

info "  Writing test value..."
TEST_TS=$(date +%s)
WRITE_RESP=$(curl -sf -X POST "https://api.outlayer.fastnear.com/wallet/v1/call" \
    -H "Authorization: Bearer $OUTLAYER_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"receiver_id":"'"$KV_CONTRACT"'","method_name":"__fastdata_kv","args":{"_verify":{"ts":'"$TEST_TS"',"ok":true}},"gas":"300000000000000"}' 2>/dev/null || echo '{"status":"network_error"}')

STATUS=$(echo "$WRITE_RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "parse_error")

if [ "$STATUS" = "success" ]; then
    TX=$(echo "$WRITE_RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tx_hash',''))" 2>/dev/null || echo "?")
    ok "KV write success! tx=$TX"
    sleep 3
    READ_OK=$(curl -sf "https://kv.main.fastnear.com/v0/latest/$KV_CONTRACT/$NEAR_ACCOUNT/_verify" 2>/dev/null | \
        python3 -c "import sys,json;d=json.load(sys.stdin);e=d.get('entries',[]);print('yes' if e and e[0].get('value',{}).get('ok') else 'no')" 2>/dev/null || echo "no")
    [ "$READ_OK" = "yes" ] && ok "KV readback verified!" || warn "Readback pending (may need a few seconds)"
elif [ "$STATUS" = "pending_approval" ]; then
    warn "Wallet needs auto-approve"
    echo "  🔗 https://outlayer.fastnear.com/wallet?key=$OUTLAYER_API_KEY"
    read -rp "  Press Enter once enabled..."
    WRITE_RESP=$(curl -sf -X POST "https://api.outlayer.fastnear.com/wallet/v1/call" \
        -H "Authorization: Bearer $OUTLAYER_API_KEY" -H "Content-Type: application/json" \
        -d '{"receiver_id":"'"$KV_CONTRACT"'","method_name":"__fastdata_kv","args":{"_verify":{"ts":'"$TEST_TS"',"ok":true}},"gas":"300000000000000"}' 2>/dev/null || echo '{"status":"network_error"}')
    STATUS=$(echo "$WRITE_RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "parse_error")
    [ "$STATUS" = "success" ] && ok "KV write now works!" || fail "Still failing. Re-run setup."
elif [ "$STATUS" = "network_error" ]; then
    fail "Network error — check connection"
else
    fail "KV write failed (status: $STATUS)"
fi

echo ""

# ── Step 3: Local replay ─────────────────────────────────────────────────

echo -e "${CYAN}── Step 3: Local Replay (7 days, no chain writes) ──${NC}"
echo ""

load_env .env
info "  Strategy: ${STRATEGY:-momentum} | ${LEVERAGE:-5}x | TP ${TP_PCT:-2.5}% | SL ${SL_PCT:-1.5}%"
info "  Fetching ${PAIRS:-BTC,ETH,SOL,wNEAR} candles..."
echo ""

rm -f state.json

python3 -u -c "
import sys, os, json, logging
logging.basicConfig(level=logging.ERROR)
sys.path.insert(0, '.')

env = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()

pairs = env.get('PAIRS', 'BTC,ETH,SOL,wNEAR').split(',')
config = {
    'strategy': env.get('STRATEGY', 'momentum'),
    'pairs': pairs,
    'leverage': float(env.get('LEVERAGE', '5')),
    'trade_size': float(env.get('TRADE_SIZE', '100')),
    'initial_balance': float(env.get('INITIAL_BALANCE', '10000')),
    'tp_pct': float(env.get('TP_PCT', '2.5')),
    'sl_pct': float(env.get('SL_PCT', '1.5')),
    'lookback_min': int(env.get('LOOKBACK_MIN', '15')),
    'threshold': float(env.get('THRESHOLD', '0.3')),
    'min_hold_s': int(env.get('MIN_HOLD_S', '120')),
    'max_open': int(env.get('MAX_OPEN', '10')),
    'near_account': '', 'kv_contract': 'contextual.near',
    'outlayer_api_key': '', 'outlayer_api': 'https://api.outlayer.fastnear.com',
}

from engine import Engine, PriceFeed
from datetime import datetime, timezone

print('Fetching candles...')
candles = PriceFeed.fetch_history(pairs, 7, '5m')
print()
print('Running simulation (local only)...')
print()

eng = Engine(config)
def on_save(i, total, ts):
    s = eng.state
    wr = (s['wins']/s['totalTrades']*100) if s['totalTrades']>0 else 0
    ts_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
    sign = '+' if s['totalPnl']>=0 else ''
    print(f'  {ts_str} | {i}/{total} | \${s[\"balance\"]:.2f} | {s[\"totalTrades\"]} trades ({wr:.0f}% WR) | PnL: \${sign}{s[\"totalPnl\"]:.2f}')

eng.replay(candles, save_every=500, on_save=on_save)
s = eng.state
wr = (s['wins']/s['totalTrades']*100) if s['totalTrades']>0 else 0
sign = '+' if s['totalPnl']>=0 else ''
print()
print(f'  \${s[\"balance\"]:.2f} | PnL: \${sign}{s[\"totalPnl\"]:.2f} | {s[\"totalTrades\"]} trades ({s[\"wins\"]}W/{s[\"losses\"]}L) {wr:.1f}% WR')
print('  Results in state.json (local only)')
" 2>&1 || fail "Replay failed. Check error above."

echo ""

# ── Step 4: Go live ──────────────────────────────────────────────────────

echo -e "${CYAN}── Step 4: Go Live ──${NC}"
echo ""
echo "  Ready to run live with real prices + on-chain writes."
echo "  Ctrl+C to stop (state saves automatically)."
echo ""
read -rp "  Start live bot? [y/N] " CONFIRM

if [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ]; then
    echo ""
    load_env .env
    info "  Starting live bot..."
    echo ""
    python3 -u paper-kv live
else
    info "  Run live anytime: python3 paper-kv live"
fi

echo ""
echo -e "${CYAN}── Commands ──${NC}"
echo "  bash setup.sh              # Re-run onboarding"
echo "  python3 paper-kv replay 7  # Replay + write to chain"
echo "  python3 paper-kv live      # Run live (Ctrl+C saves state)"
echo "  python3 paper-kv status    # Check on-chain state"
echo ""
