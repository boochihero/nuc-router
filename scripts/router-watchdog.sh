#!/bin/bash
set -euo pipefail

STATE_FILE="/etc/router-webui/state/watchdog-failures"
MAX_FAILURES=3
ROLLBACK_SCRIPT="/usr/local/bin/router-rollback.sh"

log() { echo "[WATCHDOG $(date '+%Y-%m-%d %H:%M:%S')] $*"; }

check_wan_ip() {
    ip addr show eno1 2>/dev/null | grep -q 'inet ' && return 0 || return 1
}

# NOTE: must probe China-reachable endpoints. 1.1.1.1/8.8.8.8 are blocked from
# mainland China and would make this check always fail (triggering false
# rollbacks that wiped the working config).
check_internet() {
    curl -s --max-time 8 --interface eno1 "https://www.baidu.com" >/dev/null 2>&1 && return 0
    curl -s --max-time 8 --interface eno1 "http://223.5.5.5" >/dev/null 2>&1 && return 0
    return 1
}

mkdir -p "$(dirname "$STATE_FILE")"
touch "$STATE_FILE"
FAILURES=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
FAILURES=$((FAILURES + 0))

rollback_guard() {
    # Auto-rollback is dangerous: it restores stale backups and wipes user
    # config (subscription nodes, routing). Only log the failure; rollback
    # must be triggered manually.
    log "CRITICAL: $1 consecutive failures. AUTO-ROLLBACK DISABLED (it would restore stale configs). Manual rollback: $ROLLBACK_SCRIPT"
    echo 0 > "$STATE_FILE"
}

if ! check_wan_ip; then
    log "FAIL: eno1 has no IP address"
    FAILURES=$((FAILURES + 1))
    echo "$FAILURES" > "$STATE_FILE"
    if [ "$FAILURES" -ge "$MAX_FAILURES" ]; then
        rollback_guard "eno1 no IP ($FAILURES)"
    fi
    exit 1
fi

if ! check_internet; then
    log "FAIL: WAN IP OK but internet unreachable (via baidu/223.5.5.5)"
    FAILURES=$((FAILURES + 1))
    echo "$FAILURES" > "$STATE_FILE"
    if [ "$FAILURES" -ge "$MAX_FAILURES" ]; then
        rollback_guard "no internet ($FAILURES)"
    fi
    exit 1
fi

log "OK: WAN IP OK, internet reachable"
echo 0 > "$STATE_FILE"
exit 0
