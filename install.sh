#!/bin/bash
set -euo pipefail

DRY_RUN=false
SSID="NUC-Router-5G"
PASSWORD=""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIGS_DIR="$SCRIPT_DIR/configs"
SYSTEMD_DIR="$SCRIPT_DIR/systemd"
SCRIPTS_DIR="$SCRIPT_DIR/scripts"
WEBUI_DIR="$SCRIPT_DIR/webui"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $(date '+%H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%H:%M:%S') $*"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $(date '+%H:%M:%S') $*"; }

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --dry-run       Preview mode, do not execute changes
  --ssid NAME     WiFi SSID (default: NUC-Router-5G)
  --password PWD  WiFi WPA2 password (required, min 8 chars)
  --help          Show this help

EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --ssid) SSID="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --help) usage ;;
        *) log_error "Unknown option: $1"; usage ;;
    esac
done

if [ -z "$PASSWORD" ] && [ "$DRY_RUN" = false ]; then
    log_error "--password is required (minimum 8 characters)"
    echo ""
    usage
fi

if [ ${#PASSWORD} -lt 8 ] && [ "$DRY_RUN" = false ]; then
    log_error "Password must be at least 8 characters"
    exit 1
fi

log_info "=============================================="
log_info "  NUC Router Installer"
log_info "  SSID:     $SSID"
log_info "  Dry-run:  $DRY_RUN"
log_info "=============================================="
echo ""

if [ "$DRY_RUN" = true ]; then
    log_warn "DRY-RUN MODE: No changes will be made"
    echo ""
fi

needs_sudo() {
    if [ "$DRY_RUN" = true ]; then
        echo "sudo $*"
        return 0
    fi
    sudo "$@"
}

needs_cp() {
    if [ "$DRY_RUN" = true ]; then
        echo "cp $*"
        return 0
    fi
    sudo mkdir -p "$(dirname "$2")"
    sudo cp "$@"
}

needs_mkdir() {
    if [ "$DRY_RUN" = true ]; then
        echo "mkdir -p $*"
        return 0
    fi
    sudo mkdir -p "$@"
}

needs_systemctl() {
    if [ "$DRY_RUN" = true ]; then
        echo "systemctl $*"
        return 0
    fi
    sudo systemctl "$@"
}

# ─── Step 1: Environment Check ───
log_step "Step 1/7: Checking environment..."

if ! grep -q "24.04" /etc/os-release 2>/dev/null && ! grep -q "noble" /etc/os-release 2>/dev/null; then
    log_warn "This script is designed for Ubuntu 24.04. Continuing anyway..."
fi

if [ "$EUID" -ne 0 ] && [ "$DRY_RUN" = false ]; then
    log_warn "Not running as root. Will use sudo for privileged operations."
    # Use -n (non-interactive) so the check works headless with NOPASSWD; sudo -v
    # still demands a password/tty in some sudoers setups and would fail here.
    sudo -n true || { log_error "sudo access required (NOPASSWD not configured)"; exit 1; }
fi

log_info "Environment OK"

# ─── Step 2: Install Packages ───
log_step "Step 2/7: Installing system packages..."

if [ "$DRY_RUN" = false ]; then
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        hostapd \
        dnsmasq \
        nftables \
        wireless-tools \
        rfkill \
        python3 \
        python3-venv \
        python3-pip \
        curl \
        jq

    # Install xray (China-network friendly: mirror list + manual install)
    install_xray() {
        local version="${XRAY_VERSION:-1.8.23}"
        local arch="64"
        local tmpdir
        tmpdir=$(mktemp -d)
        # 镜像前缀列表（国内可访问的 GitHub 加速代理），支持 XRAY_MIRROR 环境变量覆盖
        local mirrors=(
            "${XRAY_MIRROR:-https://ghfast.top/https://github.com}"
            "https://gh-proxy.com/https://github.com"
            "https://github.com"
        )
        local url=""
        for base in "${mirrors[@]}"; do
            url="${base}/XTLS/Xray-core/releases/download/v${version}/Xray-linux-${arch}.zip"
            log_info "  Trying mirror: ${base}"
            if curl -fsSL --connect-timeout 10 --max-time 120 -o "${tmpdir}/xray.zip" "$url"; then
                log_info "  Download OK from ${base}"
                break
            fi
        done
        if [ ! -s "${tmpdir}/xray.zip" ]; then
            log_warn "Xray download failed from all mirrors. Install manually later."
            rm -rf "$tmpdir"
            return 1
        fi
        # 用 python3 解压（避免依赖 unzip）
        python3 -c "import zipfile; zipfile.ZipFile('${tmpdir}/xray.zip').extractall('${tmpdir}/x')"
        sudo install -m 755 "${tmpdir}/x/xray" /usr/local/bin/xray
        sudo mkdir -p /usr/local/share/xray
        sudo install -m 644 "${tmpdir}/x/geoip.dat" "${tmpdir}/x/geosite.dat" /usr/local/share/xray/ 2>/dev/null || true
        rm -rf "$tmpdir"
        log_info "Xray installed: $(/usr/local/bin/xray version 2>/dev/null | head -1 || echo 'xray') "
    }

    if ! command -v xray &>/dev/null; then
        log_info "Installing Xray (via China-friendly mirrors)..."
        install_xray
        if ! command -v xray &>/dev/null; then
            log_warn "Xray install failed. Please install xray manually."
        fi
    else
        log_info "Xray already installed: $(xray version 2>/dev/null || echo 'unknown')"
    fi

    # Ensure xray is at /usr/local/bin/xray if installed elsewhere
    if ! command -v xray &>/dev/null && [ -f /usr/local/bin/xray ]; then
        :
    fi
else
    echo "Would install: hostapd dnsmasq nftables wireless-tools rfkill python3 python3-venv python3-pip curl jq xray"
fi

log_info "Packages installed"

# ─── Step 3: Render and Deploy Configs ───
log_step "Step 3/7: Deploying configuration files..."

render_template() {
    local src="$1" dst="$2"
    if [ "$DRY_RUN" = true ]; then
        echo "Would render: $src -> $dst"
        return 0
    fi
    local tmp
    tmp=$(mktemp)
    sed -e "s|{{SSID}}|$SSID|g" \
        -e "s|{{PASSWORD}}|$PASSWORD|g" \
        "$src" > "$tmp"
    sudo mkdir -p "$(dirname "$dst")"
    sudo cp "$tmp" "$dst"
    rm -f "$tmp"
    log_info "  Deployed: $dst"
}

render_template "$CONFIGS_DIR/hostapd.conf"            /etc/hostapd/hostapd.conf
needs_cp "$CONFIGS_DIR/dnsmasq.conf"                    /etc/dnsmasq.d/router.conf
needs_cp "$CONFIGS_DIR/nftables-router.nft"             /etc/nftables.d/router.nft
needs_cp "$CONFIGS_DIR/xray-client.json"                /etc/xray/client.json

if [ "$DRY_RUN" = false ]; then
    sudo chmod 600 /etc/hostapd/hostapd.conf
    sudo chmod 600 /etc/xray/client.json
fi

log_info "Configurations deployed"

# ─── Step 4: Deploy Scripts ───
log_step "Step 4/7: Deploying management scripts..."

for script in router-proxy-toggle.sh router-backup.sh router-rollback.sh router-watchdog.sh; do
    needs_cp "$SCRIPTS_DIR/$script" "/usr/local/bin/$script"
    if [ "$DRY_RUN" = false ]; then
        sudo chmod +x "/usr/local/bin/$script"
    fi
    log_info "  Deployed: /usr/local/bin/$script"
done

log_info "Scripts deployed"

# ─── Step 5: Deploy systemd Units ───
log_step "Step 5/7: Installing systemd units..."

for unit in router-firewall.service router-ap.service router-dns.service \
            router-xray.service router-webui.service \
            router-watchdog.service router-watchdog.timer; do
    needs_cp "$SYSTEMD_DIR/$unit" "/etc/systemd/system/$unit"
    log_info "  Installed: $unit"
done

if [ "$DRY_RUN" = false ]; then
    sudo systemctl daemon-reload
fi

log_info "systemd units installed"

# ─── Step 6: Setup WebUI (Python venv) ───
log_step "Step 6/7: Setting up WebUI..."

if [ "$DRY_RUN" = false ]; then
    sudo mkdir -p /opt/router-webui

    # Copy webui files
    for f in main.py subscription.py clash2xray.py requirements.txt; do
        sudo cp "$WEBUI_DIR/$f" /opt/router-webui/
    done
    sudo mkdir -p /opt/router-webui/static
    sudo cp "$WEBUI_DIR/static/index.html" /opt/router-webui/static/

    # Create Python venv and install deps (service runs as root, so venv is root-owned)
    sudo python3 -m venv /opt/router-webui/.venv
    # pip 国内镜像（清华源），可用 PIP_INDEX_URL 覆盖
    PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
    sudo env PIP_INDEX_URL="$PIP_INDEX_URL" /opt/router-webui/.venv/bin/pip install --quiet -r /opt/router-webui/requirements.txt --index-url "$PIP_INDEX_URL"

    # Generate secret token
    sudo mkdir -p /etc/router-webui/state
    TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    echo "$TOKEN" | sudo tee /etc/router-webui/secret > /dev/null
    sudo chmod 600 /etc/router-webui/secret

    # Initialize proxy state
    echo "on" | sudo tee /etc/router-webui/state/proxy > /dev/null

    log_info "WebUI token: $TOKEN"
    log_info "WebUI setup complete"
else
    echo "Would setup WebUI at /opt/router-webui"
fi

# ─── Step 7: First Backup and Service Start ───
log_step "Step 7/7: Creating initial backup and starting services..."

if [ "$DRY_RUN" = false ]; then
    # Create initial backup
    /usr/local/bin/router-backup.sh || log_warn "Initial backup had warnings"

    # Enable nftables and load rules
    sudo nft -f /etc/nftables.d/router.nft 2>/dev/null || log_warn "nft load had warnings"
    sudo systemctl enable nftables 2>/dev/null || true

    # Stop any conflicting services
    sudo systemctl stop hostapd dnsmasq 2>/dev/null || true
    sudo systemctl disable hostapd dnsmasq 2>/dev/null || true

    # Enable and start router services in order
    log_info "Starting service chain: firewall -> ap -> dns -> xray -> webui"

    sudo systemctl enable router-firewall
    sudo systemctl start router-firewall

    sudo systemctl unmask hostapd 2>/dev/null || true
    sudo systemctl enable router-ap
    sudo systemctl start router-ap

    sudo systemctl enable router-dns
    sudo systemctl start router-dns

    sudo systemctl enable router-xray
    sudo systemctl start router-xray

    sudo systemctl enable router-webui
    sudo systemctl start router-webui

    sudo systemctl enable router-watchdog.timer
    sudo systemctl start router-watchdog.timer

    # ─── Self-check ───
    echo ""
    log_info "=============================================="
    log_info "  Self-check: Verifying deployment"
    log_info "=============================================="

    sleep 3

    check_service() {
        local svc="$1"
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            log_info "  $svc: ${GREEN}active${NC}"
        else
            log_warn "  $svc: ${RED}inactive${NC} - check 'journalctl -u $svc'"
        fi
    }

    check_service router-firewall
    check_service router-ap
    check_service router-dns
    check_service router-xray
    check_service router-webui

    # Check WAN connectivity
    echo ""
    if ip addr show eno1 2>/dev/null | grep -q 'inet '; then
        WAN_IP=$(ip -4 addr show eno1 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)
        log_info "WAN IP: $WAN_IP"
    else
        log_warn "eno1 has no IP. Ensure the Ethernet cable is connected."
    fi

    # Check AP
    if iw dev wlp0s20f3 info 2>/dev/null | grep -q "type AP"; then
        log_info "AP wlp0s20f3 is in AP mode"
    else
        log_warn "wlp0s20f3 may not be in AP mode"
    fi

    # Check nftables TPROXY
    if sudo nft list chain inet router prerouting_proxy 2>/dev/null | grep -q "tproxy"; then
        log_info "TPROXY rules loaded"
    else
        log_warn "TPROXY rules not found. Proxy may not work until configured."
    fi

    echo ""
    log_info "=============================================="
    log_info "  Deployment Complete!"
    log_info "=============================================="
    echo ""
    log_info "WebUI:  http://192.168.50.1:8080"
    log_info "Token:  $TOKEN"
    log_info "SSID:   $SSID"
    echo ""
    log_info "Connect to WiFi '$SSID' and open the WebUI."
    log_info "Run 'journalctl -u router-webui -f' to monitor logs."
    echo ""
else
    echo ""
    log_info "Dry-run complete. Run without --dry-run to deploy."
    echo ""
fi
