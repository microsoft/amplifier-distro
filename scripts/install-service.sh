#!/bin/bash
# Install amplifier-distro as a systemd user service.
#
# This script:
#   1. Detects the amp-distro-server binary location
#   2. Generates a service file with the correct ExecStart path
#   3. Enables and starts the service via systemd --user
#
# Usage:
#   bash scripts/install-service.sh
#
# To uninstall:
#   systemctl --user stop amplifier-distro
#   systemctl --user disable amplifier-distro
#   rm ~/.config/systemd/user/amplifier-distro.service

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_TEMPLATE="$SCRIPT_DIR/amplifier-distro.service"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

# Detect amp-distro-server location
AMP_SERVER=$(command -v amp-distro-server 2>/dev/null || true)
if [ -z "$AMP_SERVER" ]; then
    echo "Error: amp-distro-server not found in PATH."
    echo "Install amplifier-distro first:"
    echo "  uv pip install amplifier-distro"
    exit 1
fi

echo "Found amp-distro-server at: $AMP_SERVER"

# Verify template exists
if [ ! -f "$SERVICE_TEMPLATE" ]; then
    echo "Error: Service template not found at $SERVICE_TEMPLATE"
    exit 1
fi

# Create systemd user directory
mkdir -p "$SYSTEMD_USER_DIR"

# Generate service file with correct ExecStart path
sed "s|ExecStart=.*|ExecStart=$AMP_SERVER --host 127.0.0.1 --port 8400|" \
    "$SERVICE_TEMPLATE" > "$SYSTEMD_USER_DIR/amplifier-distro.service"

echo "Installed service file to $SYSTEMD_USER_DIR/amplifier-distro.service"

# Reload, enable, and start
systemctl --user daemon-reload
systemctl --user enable amplifier-distro.service
systemctl --user start amplifier-distro.service

echo ""
echo "Service enabled and started."
echo "  Status:  systemctl --user status amplifier-distro"
echo "  Logs:    journalctl --user -u amplifier-distro -f"
echo "  Stop:    systemctl --user stop amplifier-distro"
echo "  Disable: systemctl --user disable amplifier-distro"
