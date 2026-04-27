#!/bin/bash
# ============================================================
#  Deploy trading bot to Hostinger KVM2
#  Usage:
#    SERVER_IP=1.2.3.4 bash server/deploy.sh
#    SERVER_IP=1.2.3.4 SERVER_USER=root bash server/deploy.sh
# ============================================================

SERVER_IP="${SERVER_IP:-}"
SERVER_USER="${SERVER_USER:-root}"
REMOTE_DIR="/opt/trading-bot"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_DIR="$(dirname "$SCRIPT_DIR")"

if [ -z "$SERVER_IP" ]; then
    echo ""
    echo "  ERROR: SERVER_IP not set."
    echo "  Usage: SERVER_IP=your.server.ip bash server/deploy.sh"
    echo ""
    exit 1
fi

echo "[deploy] → ${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}"

rsync -avz --progress \
  --exclude "venv/" \
  --exclude "reports/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".git/" \
  --exclude ".aider*" \
  --exclude "server/setup_hostinger.sh" \
  "$LOCAL_DIR/" \
  "${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}/"

# Install / upgrade Python deps on server
echo "[deploy] Installing Python dependencies..."
ssh "${SERVER_USER}@${SERVER_IP}" \
  "cd ${REMOTE_DIR} && venv/bin/pip install -q -r requirements_server.txt"

# Restart services
echo "[deploy] Restarting services..."
ssh "${SERVER_USER}@${SERVER_IP}" "supervisorctl restart all"

echo ""
echo "=========================================="
echo "  DEPLOY COMPLETE"
echo "=========================================="
echo "  Dashboard: http://${SERVER_IP}:5001"
echo ""
echo "  SSL setup (first time only):"
echo "    ssh ${SERVER_USER}@${SERVER_IP}"
echo "    bash ${REMOTE_DIR}/server/ssl_setup.sh"
echo ""
echo "  Logs:"
echo "    ssh ${SERVER_USER}@${SERVER_IP}"
echo "    tail -f /var/log/trading-bot.out.log"
echo "    supervisorctl status"
echo "=========================================="
