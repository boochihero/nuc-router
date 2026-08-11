#!/bin/bash
set -euo pipefail

STATE_FILE="/etc/router-webui/state/watchdog-failures"
MAX_FAILURES=2
ROLLBACK_SCRIPT="/usr/local/bin/router-rollback.sh"

log() { echo "[WATCHDOG $(date '+%Y-%m-%d %H:%M:%S')] $*"; }

check_wan_ip() {
    ip addr show eno1 2>/dev/null | grep -q 'inet ' && return 0 || return 1
}

check_internet() {
    curl -s --max-time 10 --interface eno1 "https://1.1.1.1" >/dev/null 2>&1 && return 0
    curl -s --max-time 10 --interface eno1 "https://8.8.8.8" >/dev/null 2>&1 && return 0
    return 1
}

mkdir -p "$(dirname "$STATE_FILE")"
touch "$STATE_FILE"
FAILURES=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
FAILURES=$((FAILURES + 0))

if ! check_wan_ip; then
    log "FAIL: eno1 has no IP address"
    FAILURES=$((FAILURES + 1))
    echo "$FAILURES" > "$STATE_FILE"

    if [ "$FAILURES" -ge "$MAX_FAILURES" ]; then
        log "CRITICAL: $FAILURES consecutive failures, triggering auto-rollback..."
        if [ -x "$ROLLBACK_SCRIPT" ]; then
            "$ROLLBACK_SCRIPT" && log "Auto-rollback completed"
        else
            log "ERROR: rollback script not found at $ROLLBACK_SCRIPT"
        fi
        echo 0 > "$STATE_FILE"
    fi
    exit 1
fi

if ! check_internet; then
    log "FAIL: WAN IP OK but no internet"
    FAILURES=$((FAILURES + 1))
    echo "$FAILURES" > "$STATE_FILE"

    if [ "$FAILURES" -ge "$MAX_FAILURES" ]; then
        log "CRITICAL: $FAILURES consecutive failures, triggering auto-rollback..."
        if [ -x "$ROLLBACK_SCRIPT" ]; then
            "$ROLLBACK_SCRIPT" && log "Auto-rollback completed"
        else
            log "ERROR: rollback script not found at $ROLLBACK_SCRIPT"
        fi
        echo 0 > "$STATE_FILE"
    fi
    exit 1
fi

log "OK: WAN IP OK, internet reachable"
echo 0 > "$STATE_FILE"
exit 0
