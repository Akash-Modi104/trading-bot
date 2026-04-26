#!/bin/bash
# ============================================================
#  Deploy / update bot files to Hostinger KVM2
#  Run from your LOCAL machine (Windows Git Bash or WSL):
#    bash server/deploy.sh
# ============================================================

SERVER_IP="YOUR_SERVER_IP"       # <-- change this
SERVER_USER="root"
REMOTE_DIR="/opt/trading-bot"
LOCAL_DIR="$(dirname "$(cd "$(dirname "$0")" && pwd)")"

echo "[deploy] Uploading files to ${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}"

# Files to upload (everything except venv, reports, temp files)
rsync -avz --progress \
  --exclude "venv/" \
  --exclude "reports/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".git/" \
  --exclude "simulate_friday.py" \
  --exclude "server/setup_hostinger.sh" \
  "$LOCAL_DIR/" \
  "${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}/"

# Copy server-specific files
scp "$LOCAL_DIR/server/local_scanner.py" "${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}/local_scanner.py"

echo ""
echo "[deploy] Done. To restart services on server:"
echo "  ssh ${SERVER_USER}@${SERVER_IP}"
echo "  supervisorctl restart all"
echo "  tail -f /var/log/trading-bot.out.log"
