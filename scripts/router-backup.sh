#!/bin/bash
set -euo pipefail

BACKUP_ROOT="/etc/router-backups"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$TIMESTAMP"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "Creating backup: $BACKUP_DIR"
sudo mkdir -p "$BACKUP_DIR"

copy_if_exists() {
    local src="$1" dst="$2"
    [ -e "$src" ] && sudo cp -r "$src" "$dst" && log "  backed up: $src" || log "  skipped (not found): $src"
}

copy_if_exists /etc/hostapd/hostapd.conf       "$BACKUP_DIR/hostapd.conf"
copy_if_exists /etc/dnsmasq.d/router.conf       "$BACKUP_DIR/router-dnsmasq.conf"
copy_if_exists /etc/nftables.d/router.nft       "$BACKUP_DIR/router.nft"
copy_if_exists /etc/nftables.d/router-proxy.nft "$BACKUP_DIR/router-proxy.nft"
copy_if_exists /etc/xray/client.json            "$BACKUP_DIR/client.json"
copy_if_exists /etc/router-webui/secret         "$BACKUP_DIR/secret"
copy_if_exists /etc/router-webui/state          "$BACKUP_DIR/state"
copy_if_exists /etc/router-webui/subscriptions.json "$BACKUP_DIR/subscriptions.json"

if [ "$(ls -A "$BACKUP_DIR" 2>/dev/null)" ]; then
    log "Backup completed: $BACKUP_DIR"
else
    log "WARNING: No configuration files found to back up"
    sudo rmdir "$BACKUP_DIR" 2>/dev/null || true
fi

echo "$BACKUP_DIR"
