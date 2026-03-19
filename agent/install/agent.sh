#!/usr/bin/env bash
# Sentinel Agent — Linux/macOS Installer
#
# Required environment variables:
#   SENTINEL_AGENT_KEY    Your agent API key from the Sentinel dashboard
#   SENTINEL_SERVER_URL   URL of your Sentinel server (e.g. http://192.168.1.87:8000)
#
# Optional:
#   SENTINEL_AGENT_PORT   Port to listen on (default: 8765)
#
# Usage:
#   SENTINEL_AGENT_KEY="your-key" \
#   SENTINEL_SERVER_URL="http://192.168.1.87:8000" \
#   bash agent.sh

set -euo pipefail

AGENT_KEY="${SENTINEL_AGENT_KEY:-}"
SERVER_URL="${SENTINEL_SERVER_URL:-}"
PORT="${SENTINEL_AGENT_PORT:-8765}"
INSTALL_DIR="/opt/sentinel-agent"
SERVICE_NAME="sentinel-agent"
SERVICE_USER="sentinel"

# ── Validation ────────────────────────────────────────────────────────────────

if [ -z "$AGENT_KEY" ]; then
    echo "ERROR: SENTINEL_AGENT_KEY is required." >&2
    exit 1
fi

if [ -z "$SERVER_URL" ]; then
    echo "ERROR: SENTINEL_SERVER_URL is required." >&2
    exit 1
fi

echo "=== Sentinel Agent Installer ==="
echo "Install directory : $INSTALL_DIR"
echo "Server URL        : $SERVER_URL"
echo "Port              : $PORT"
echo ""

# ── Check root ────────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This installer must be run as root (use sudo)." >&2
    exit 1
fi

# ── Create user and directories ───────────────────────────────────────────────

if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "Created system user: $SERVICE_USER"
fi

mkdir -p "$INSTALL_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── Write environment file ────────────────────────────────────────────────────

cat > "$INSTALL_DIR/.env" <<EOF
SENTINEL_AGENT_KEY=$AGENT_KEY
SENTINEL_SERVER_URL=$SERVER_URL
SENTINEL_AGENT_PORT=$PORT
SENTINEL_AGENT_HOST=0.0.0.0
EOF
chmod 600 "$INSTALL_DIR/.env"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
echo "Environment file written to $INSTALL_DIR/.env"

# ── Create systemd service ────────────────────────────────────────────────────

if command -v systemctl &>/dev/null; then
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Sentinel Backup Agent
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$(which python3) -m uvicorn agent.main:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=sentinel-agent

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl start "$SERVICE_NAME"

    echo ""
    echo "=== Installation complete ==="
    echo ""
    echo "Service status:"
    systemctl status "$SERVICE_NAME" --no-pager || true
    echo ""
    echo "To verify the agent is running:"
    echo "  curl http://localhost:$PORT/health"
    echo ""
    echo "To view logs:"
    echo "  journalctl -u $SERVICE_NAME -f"
else
    # macOS or system without systemd
    echo ""
    echo "=== Installation complete (no systemd detected) ==="
    echo ""
    echo "To start the agent manually:"
    echo "  source $INSTALL_DIR/.env && python3 -m uvicorn agent.main:app --host 0.0.0.0 --port $PORT"
fi
