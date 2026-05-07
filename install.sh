#!/usr/bin/env bash
# =============================================================================
# Pi Streamer SDR — One-shot installer for Raspberry Pi 3B
#
# Streams demodulated FM/AM audio from an RTL-SDR Blog V3 to Icecast.
# No physical radio. No USB sound card.
#
# Usage:
#   sudo bash install.sh
#
# Prerequisites:
#   - Raspberry Pi OS Lite (64-bit, Bookworm) — fresh install
#   - RTL-SDR Blog V3 dongle plugged in
#   - Internet connection
#   - SSH access
#
# Reinstalling with Tailscale already running?
#   sudo tailscale down
#   echo "nameserver 1.1.1.1" | sudo tee /etc/resolv.conf
#   sudo bash install.sh
# =============================================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }

if [[ $EUID -ne 0 ]]; then
    err "Run as root: sudo bash install.sh"
    exit 1
fi

REAL_USER="${SUDO_USER:-pi}"
INSTALL_DIR="/opt/pi-streamer"
SERVICE_NAME="pi-streamer"

echo ""
echo "============================================"
echo "  Pi Streamer SDR Installer"
echo "  RTL-SDR → Icecast MP3 Streaming"
echo "============================================"
echo ""

# =============================================================================
# 1. System packages
# =============================================================================
info "Updating package lists..."
apt-get update -qq

info "Installing dependencies..."
echo "icecast2 icecast2/icecast-setup boolean true" | debconf-set-selections

apt-get install -y \
    icecast2 \
    sox \
    ffmpeg \
    rtl-sdr \
    python3 \
    python3-flask \
    curl \
    jq

info "Packages installed."

# =============================================================================
# 2. Blacklist DVB kernel module
#    The default dvb_usb_rtl28xxu driver claims the RTL-SDR device before
#    rtl_fm can open it. Must blacklist it and reload.
# =============================================================================
BLACKLIST_FILE="/etc/modprobe.d/blacklist-rtlsdr.conf"
if [[ ! -f "$BLACKLIST_FILE" ]]; then
    info "Blacklisting DVB kernel module..."
    echo 'blacklist dvb_usb_rtl28xxu' > "$BLACKLIST_FILE"
    # Unload if currently loaded
    modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
    info "DVB module blacklisted."
else
    info "DVB module already blacklisted."
fi

# =============================================================================
# 3. Tailscale
# =============================================================================
if ! command -v tailscale &>/dev/null; then
    info "Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
    info "Tailscale installed."
else
    info "Tailscale already installed."
fi

systemctl enable --now tailscaled 2>/dev/null || true

if ! tailscale status &>/dev/null; then
    echo ""
    echo "============================================"
    echo "  TAILSCALE SETUP"
    echo "============================================"
    echo ""
    info "Starting Tailscale authentication..."
    echo ""
    tailscale up
    echo ""
fi

TS_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
TS_NAME=$(tailscale status --self --json 2>/dev/null | jq -r '.Self.DNSName // "unknown"' | sed 's/\.$//')
info "Tailscale connected: ${TS_IP} (${TS_NAME})"

# =============================================================================
# 4. Detect RTL-SDR device
# =============================================================================
info "Detecting RTL-SDR device..."
RTL_DEVICE_FOUND=0

if rtl_test -t 2>&1 | grep -qi "found.*rtl\|rtl.*found\|blog\|r820"; then
    RTL_DEVICE_FOUND=1
    RTL_DEVICE_INFO=$(rtl_test -t 2>&1 | grep -i "found\|blog\|r820" | head -1 || echo "RTL-SDR detected")
    info "RTL-SDR: ${RTL_DEVICE_INFO}"
else
    warn "RTL-SDR not detected — is the dongle plugged in?"
    warn "If you just blacklisted the DVB module, a reboot may be needed."
    warn "Continuing install. Set frequency in ${INSTALL_DIR}/pi-streamer.conf after reboot."
fi

# =============================================================================
# 5. Configure Icecast
# =============================================================================
info "Configuring Icecast..."
ICECAST_PW=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 16)

cat > /etc/icecast2/icecast.xml << ICEXML
<icecast>
    <location>Pi Streamer SDR</location>
    <admin>admin@localhost</admin>
    <limits>
        <clients>20</clients>
        <sources>2</sources>
        <queue-size>262144</queue-size>
        <client-timeout>30</client-timeout>
        <header-timeout>15</header-timeout>
        <source-timeout>10</source-timeout>
        <burst-on-connect>1</burst-on-connect>
        <burst-size>65535</burst-size>
    </limits>
    <authentication>
        <source-password>${ICECAST_PW}</source-password>
        <relay-password>${ICECAST_PW}</relay-password>
        <admin-user>admin</admin-user>
        <admin-password>${ICECAST_PW}</admin-password>
    </authentication>
    <hostname>localhost</hostname>
    <listen-socket>
        <port>8000</port>
    </listen-socket>
    <mount>
        <mount-name>/scanner</mount-name>
    </mount>
    <fileserve>1</fileserve>
    <paths>
        <basedir>/usr/share/icecast2</basedir>
        <logdir>/var/log/icecast2</logdir>
        <webroot>/usr/share/icecast2/web</webroot>
        <adminroot>/usr/share/icecast2/admin</adminroot>
        <alias source="/" destination="/status.xsl"/>
    </paths>
    <logging>
        <accesslog>access.log</accesslog>
        <errorlog>error.log</errorlog>
        <loglevel>3</loglevel>
        <logsize>10000</logsize>
    </logging>
    <security>
        <chroot>0</chroot>
    </security>
</icecast>
ICEXML

sed -i 's/ENABLE=false/ENABLE=true/' /etc/default/icecast2 2>/dev/null || true
systemctl enable icecast2
systemctl restart icecast2
info "Icecast configured (password: ${ICECAST_PW})"

# =============================================================================
# 6. Install application
# =============================================================================
info "Installing Pi Streamer SDR to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}/templates"

cat > "${INSTALL_DIR}/pi-streamer.conf" << CONF
# Pi Streamer SDR Configuration
# Edit and restart: sudo systemctl restart pi-streamer

# RTL-SDR settings
RTL_FREQUENCY=164.750M
RTL_MODULATION=fm
RTL_GAIN=40
RTL_PPM=0
RTL_SAMPLE_RATE=22050

# Icecast connection
ICECAST_HOST=localhost
ICECAST_PORT=8000
ICECAST_SOURCE_PASSWORD=${ICECAST_PW}

# Web UI port
WEB_UI_PORT=5080
CONF

cp "$(dirname "$0")/app.py" "${INSTALL_DIR}/app.py"
cp "$(dirname "$0")/templates/index.html" "${INSTALL_DIR}/templates/index.html"

chown -R root:root "${INSTALL_DIR}"
chmod 644 "${INSTALL_DIR}/pi-streamer.conf"
info "Application installed."

# =============================================================================
# 7. Systemd service
# =============================================================================
info "Creating systemd service..."

cat > "/etc/systemd/system/${SERVICE_NAME}.service" << UNIT
[Unit]
Description=Pi Streamer SDR — RTL-SDR to Icecast
After=network.target icecast2.service
Wants=icecast2.service

[Service]
Type=simple
EnvironmentFile=${INSTALL_DIR}/pi-streamer.conf
ExecStart=/usr/bin/python3 -u ${INSTALL_DIR}/app.py
WorkingDirectory=${INSTALL_DIR}
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pi-streamer

NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
info "Service installed and started."

# =============================================================================
# 8. Summary
# =============================================================================
echo ""
echo "============================================"
echo "  INSTALLATION COMPLETE"
echo "============================================"
echo ""
if [[ $RTL_DEVICE_FOUND -eq 1 ]]; then
    echo "  RTL-SDR:   detected"
else
    echo "  RTL-SDR:   NOT detected — reboot may be needed"
fi
echo ""
echo "  Web UI:"
echo "    Local:     http://localhost:5080"
echo "    Tailscale: http://${TS_IP}:5080"
echo "    DNS:       http://${TS_NAME}:5080"
echo ""
echo "  Stream URL:"
echo "    Local:     http://localhost:8000/scanner"
echo "    Tailscale: http://${TS_IP}:8000/scanner"
echo "    DNS:       http://${TS_NAME}:8000/scanner"
echo ""
echo "  Icecast admin:"
echo "    URL:       http://localhost:8000/admin/"
echo "    Password:  ${ICECAST_PW}"
echo ""
echo "  Config:  ${INSTALL_DIR}/pi-streamer.conf"
echo "  Logs:    journalctl -u pi-streamer -f"
echo ""
if [[ $RTL_DEVICE_FOUND -eq 0 ]]; then
    echo "  ACTION REQUIRED: Reboot the Pi before starting."
    echo "  The DVB kernel module was blacklisted — needs a reboot"
    echo "  to release the RTL-SDR device."
    echo ""
fi
echo "============================================"
