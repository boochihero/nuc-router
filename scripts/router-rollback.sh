#!/bin/bash
set -euo pipefail

BACKUP_ROOT="/etc/router-backups"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

LATEST="$(ls -1t "$BACKUP_ROOT" 2>/dev/null | head -1)"
if [ -z "$LATEST" ]; then
    log "ERROR: No backup found in $BACKUP_ROOT"
    exit 1
fi

BACKUP_DIR="$BACKUP_ROOT/$LATEST"
log "Rolling back to backup: $LATEST"

restore_if_exists() {
    local src="$1" dst="$2"
    if [ -f "$src" ]; then
        mkdir -p "$(dirname "$dst")"
        cp "$src" "$dst"
        log "  restored: $dst"
    elif [ -d "$src" ]; then
        mkdir -p "$dst"
        cp -r "$src"/* "$dst"/
        log "  restored (dir): $dst"
    fi
}

restore_if_exists "$BACKUP_DIR/hostapd.conf"          /etc/hostapd/hostapd.conf
restore_if_exists "$BACKUP_DIR/router-dnsmasq.conf"   /etc/dnsmasq.d/router.conf
restore_if_exists "$BACKUP_DIR/router.nft"            /etc/nftables.d/router.nft
restore_if_exists "$BACKUP_DIR/router-proxy.nft"      /etc/nftables.d/router-proxy.nft
restore_if_exists "$BACKUP_DIR/client.json"           /etc/xray/client.json
restore_if_exists "$BACKUP_DIR/secret"                /etc/router-webui/secret
restore_if_exists "$BACKUP_DIR/state"                 /etc/router-webui/state
restore_if_exists "$BACKUP_DIR/subscriptions.json"    /etc/router-webui/subscriptions.json

# Reload services
log "Reloading services..."
nft -f /etc/nftables.d/router.nft 2>/dev/null || log "WARNING: nft reload failed"
systemctl restart router-dns 2>/dev/null || log "WARNING: dns restart failed"
systemctl restart router-xray 2>/dev/null || log "WARNING: xray restart failed"
systemctl restart router-ap 2>/dev/null || log "WARNING: ap restart failed"

log "Rollback completed from $LATEST"
