#!/bin/bash
set -euo pipefail

STATE_DIR="/etc/router-webui/state"
STATE_FILE="$STATE_DIR/proxy"
NFT_RULES_PROXY="/etc/nftables.d/router-proxy.nft"
BACKUP_DIR="/etc/router-backups/.proxy-toggle"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

usage() {
    echo "Usage: $0 [on|off]"
    echo "  on   Enable transparent proxy (TPROXY rules)"
    echo "  off  Disable transparent proxy (remove TPROXY rules)"
    exit 1
}

restore_nft() {
    log "ERROR: Failed to apply nftables rules, restoring backup..."
    if [ -f "$BACKUP_DIR/ruleset.bak" ]; then
        nft -f "$BACKUP_DIR/ruleset.bak" 2>/dev/null || log "FATAL: restore failed"
    fi
}

enable_proxy() {
    log "Enabling transparent proxy..."
    mkdir -p "$BACKUP_DIR"
    nft list ruleset > "$BACKUP_DIR/ruleset.bak"

    cat > "$NFT_RULES_PROXY" <<'NFTEOF'
table inet router {
    chain prerouting_proxy {
        type filter hook prerouting priority mangle; policy accept;
        ip daddr { 127.0.0.0/8, 192.168.50.0/24 } return
        iifname != "wlp0s20f3" return
        tcp dport { 22, 80 } return
        meta l4proto tcp tproxy to 127.0.0.1:12345
        meta l4proto udp tproxy to 127.0.0.1:12345
    }
}
NFTEOF

    nft -f "$NFT_RULES_PROXY" || { restore_nft; exit 1; }
    mkdir -p "$STATE_DIR"
    echo "on" > "$STATE_FILE"
    log "Proxy ENABLED"
}

disable_proxy() {
    log "Disabling transparent proxy..."
    mkdir -p "$BACKUP_DIR"
    nft list ruleset > "$BACKUP_DIR/ruleset.bak"

    nft delete chain inet router prerouting_proxy 2>/dev/null || true

    mkdir -p "$STATE_DIR"
    echo "off" > "$STATE_FILE"
    log "Proxy DISABLED"
}

[ "$#" -eq 1 ] || usage
case "$1" in
    on)  enable_proxy ;;
    off) disable_proxy ;;
    *)   usage ;;
esac
