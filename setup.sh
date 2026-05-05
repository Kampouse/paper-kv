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

python3 -u replay_local.py 2>&1 || fail "Replay failed. Check error above."

echo ""

# ── Step 4: Install daemon ───────────────────────────────────────────────

echo -e "${CYAN}── Step 4: Install Daemon ──${NC}"
echo ""
echo "  Install as a background service? (auto-restarts on crash, starts on boot)"
echo ""
read -rp "  Install daemon? [y/N] " CONFIRM

if [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ]; then
    load_env .env
    BOT_DIR="$(cd "$SCRIPT_DIR" && pwd)"
    BOT_CMD="$BOT_DIR/paper-kv.py"
    LOG_DIR="$HOME/.paper-kv.py"
    mkdir -p "$LOG_DIR"

    if [[ "$(uname)" == "Darwin" ]]; then
        # ── macOS: launchd ──
        PLIST="$HOME/Library/LaunchAgents/com.paper-kv.bot.plist"
        cat > "$PLIST" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.paper-kv.bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which python3)</string>
        <string>-u</string>
        <string>$BOT_CMD</string>
        <string>live</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$BOT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
PLISTEOF
        # Write env vars
        while IFS='=' read -r key val; do
            [[ -z "$key" || "$key" =~ ^# ]] && continue
            val="${val#\"}" ; val="${val%\"}"
            val="${val#\'}" ; val="${val%\'}"
            cat >> "$PLIST" << PLISTEOF
        <key>$key</key>
        <string>$val</string>
PLISTEOF
        done < .env
        cat >> "$PLIST" << PLISTEOF
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/bot.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/bot.log</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>RestartInterval</key>
    <integer>10</integer>
</dict>
</plist>
PLISTEOF
        launchctl unload "$PLIST" 2>/dev/null || true
        launchctl load "$PLIST" 2>/dev/null || {
            warn "launchctl load failed, falling back to nohup"
            nohup python3 -u "$BOT_CMD" live > "$LOG_DIR/bot.log" 2>&1 &
            echo "$!" > "$LOG_DIR/bot.pid"
            ok "Bot started via nohup (PID: $(cat "$LOG_DIR/bot.pid"))"
            echo "  Log: $LOG_DIR/bot.log"
            echo "  Stop: kill \$(cat $LOG_DIR/bot.pid)"
            CONFIRM="nohup"
        }
        if [ "$CONFIRM" != "nohup" ]; then
            ok "Daemon installed (launchd)"
            echo "  Log: $LOG_DIR/bot.log"
            echo "  Status: launchctl list | grep paper-kv.py"
            echo "  Stop: launchctl unload $PLIST"
            echo "  Restart: launchctl unload $PLIST && launchctl load $PLIST"
        fi

    elif [[ "$(uname)" == "Linux" ]]; then
        # ── Linux: systemd ──
        SERVICE="/etc/systemd/system/paper-kv-bot.service"
        PYTHON_PATH="$(which python3)"
        cat > /tmp/paper-kv-bot.service << SVCEOF
[Unit]
Description=paper-kv trading bot
After=network.target

[Service]
Type=simple
WorkingDirectory=$BOT_DIR
ExecStart=$PYTHON_PATH -u $BOT_CMD live
Restart=always
RestartSec=10
SVCEOF
        # Write env as Environment= lines
        ENV_LINE=""
        while IFS='=' read -r key val; do
            [[ -z "$key" || "$key" =~ ^# ]] && continue
            val="${val#\"}" ; val="${val%\"}"
            val="${val#\'}" ; val="${val%\'}"
            ENV_LINE="${ENV_LINE}${key}=${val} "
        done < .env
        echo "Environment=$ENV_LINE" >> /tmp/paper-kv-bot.service
        cat >> /tmp/paper-kv-bot.service << SVCEOF
StandardOutput=append:$LOG_DIR/bot.log
StandardError=append:$LOG_DIR/bot.log

[Install]
WantedBy=multi-user.target
SVCEOF

        sudo cp /tmp/paper-kv-bot.service "$SERVICE" 2>/dev/null && \
        sudo systemctl daemon-reload && \
        sudo systemctl enable paper-kv-bot && \
        sudo systemctl start paper-kv-bot || {
            warn "systemd install failed (no sudo?), falling back to nohup"
            nohup python3 -u "$BOT_CMD" live > "$LOG_DIR/bot.log" 2>&1 &
            echo "$!" > "$LOG_DIR/bot.pid"
            ok "Bot started via nohup (PID: $(cat "$LOG_DIR/bot.pid"))"
            echo "  Log: $LOG_DIR/bot.log"
            echo "  Stop: kill \$(cat $LOG_DIR/bot.pid)"
            CONFIRM="nohup"
        }
        if [ "$CONFIRM" != "nohup" ]; then
            ok "Daemon installed (systemd)"
            echo "  Log: $LOG_DIR/bot.log"
            echo "  Status: systemctl status paper-kv-bot"
            echo "  Stop: sudo systemctl stop paper-kv-bot"
            echo "  Restart: sudo systemctl restart paper-kv-bot"
        fi

    else
        # ── Unknown OS: nohup fallback ──
        warn "Unknown OS ($(uname)), using nohup"
        nohup python3 -u "$BOT_CMD" live > "$LOG_DIR/bot.log" 2>&1 &
        echo "$!" > "$LOG_DIR/bot.pid"
        ok "Bot started via nohup (PID: $(cat "$LOG_DIR/bot.pid"))"
        echo "  Log: $LOG_DIR/bot.log"
        echo "  Stop: kill \$(cat $LOG_DIR/bot.pid)"
    fi
else
    info "  Run live anytime: python3 paper-kv.py live"
fi

echo ""
echo -e "${CYAN}── Commands ──${NC}"
echo "  bash setup.sh              # Re-run onboarding"
echo "  python3 paper-kv.py replay 7  # Replay (local only)"
echo "  python3 paper-kv.py live      # Run live (foreground)"
echo "  python3 paper-kv.py status    # Check on-chain state"
echo ""
echo "  Daemon log: ~/.paper-kv/bot.log"
echo ""
