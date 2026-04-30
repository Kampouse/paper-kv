#!/usr/bin/env bash
# ─── paper-kv: one-script agent setup ───
# Clones (if needed), registers OutLayer wallet, configures, and runs.
# Usage: bash setup.sh [run|verify|tokens]
set -euo pipefail

# Must be run from the paper-kv directory (where paper_kv.py lives)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$SCRIPT_DIR/paper_kv.py" ]; then
    echo "❌ paper_kv.py not found — run this script from the paper-kv directory"
    exit 1
fi
cd "$SCRIPT_DIR"

# ── 2. Register OutLayer wallet (get API key) ───────────────────────────
if [ -n "${OUTLAYER_API_KEY:-}" ]; then
    echo "── Using OUTLAYER_API_KEY from environment ──"
elif [ -f "$ENV_FILE" ] && grep -q "OUTLAYER_API_KEY=wk_" "$ENV_FILE" 2>/dev/null; then
    echo "── OutLayer API key already configured ──"
else
    echo "── Registering OutLayer custody wallet ──"
    RESPONSE=$(curl -sf -X POST https://api.outlayer.fastnear.com/register)
    if [ -z "$RESPONSE" ]; then
        echo "❌ Failed to register OutLayer wallet"
        exit 1
    fi
    API_KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")
    echo "  API key: ${API_KEY:0:12}..."

    # Write .env
    cat > "$ENV_FILE" <<EOF
# ── OutLayer custody wallet ──
OUTLAYER_API_KEY=$API_KEY

# ── Trading config ──
KV_CONTRACT=paper-kv.near
INITIAL_BALANCE=10000
TRADE_SIZE=100
DEFAULT_LEVERAGE=5
MAX_OPEN_TRADES=5
CHECK_INTERVAL_MS=60000
STRATEGY=momentum
MOMENTUM_LOOKBACK_MINUTES=30
MOMENTUM_THRESHOLD_PCT=0.5
TRADE_PAIRS=BTC,ETH,SOL,wNEAR
EOF
    echo "  ✅ .env written to $ENV_FILE"
fi

# ── 3. Export env ───────────────────────────────────────────────────────
set -a; source "$ENV_FILE"; set +a

# ── 4. Dispatch ─────────────────────────────────────────────────────────
ACTION="${1:-run}"
case "$ACTION" in
    run)
        echo "── Starting paper-kv bot ──"
        exec python3 "$SCRIPT_DIR/paper_kv.py"
        ;;
    verify)
        echo "── Running integrity check ──"
        exec python3 "$SCRIPT_DIR/paper_kv.py" verify
        ;;
    tokens)
        exec python3 "$SCRIPT_DIR/paper_kv.py" tokens
        ;;
    status)
        exec python3 "$SCRIPT_DIR/paper_kv.py" status
        ;;
    *)
        echo "Usage: bash setup.sh [run|verify|tokens|status]"
        exit 1
        ;;
esac
