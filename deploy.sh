#!/bin/bash
# AlgoTrader — Deploy to VPS (Linux/Mac)
# Usage: bash deploy.sh
# Requires: SSH key auth set up

set -e

SERVER="root@187.127.73.203"
REMOTE_PATH="/opt/trading-bot"

echo "==> Deploying AlgoTrader to $SERVER:$REMOTE_PATH"

# 1. Push to origin
echo -e "\n[1/4] Pushing to git origin..."
git push origin main

# 2. Pull + pip install on server
echo -e "\n[2/4] Pulling latest code on server..."
ssh "$SERVER" bash -s <<EOF
set -e
cd $REMOTE_PATH
git pull origin main
pip install -r requirements.txt --quiet
echo "Code updated"
EOF

# 3. Restart services
echo -e "\n[3/4] Restarting services..."
ssh "$SERVER" "supervisorctl restart dashboard trading-bot scanner"

# 4. Status
echo -e "\n[4/4] Service status:"
ssh "$SERVER" "supervisorctl status"

echo -e "\n==> Deploy complete!"
