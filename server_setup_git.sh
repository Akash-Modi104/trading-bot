#!/bin/bash
# Run this ONCE on the server to set up the repo at /opt/trading-bot
# After SSH key auth is working, run: ssh root@187.127.73.203 "bash -s" < server_setup_git.sh

set -e

REMOTE_PATH="/opt/trading-bot"
REPO_URL="https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME.git"  # ← fill in your GitHub repo URL

echo "==> Setting up trading bot on server"

# Install dependencies
apt-get update -qq
apt-get install -y -qq python3-pip git supervisor nginx certbot python3-certbot-nginx

# Clone repo
if [ ! -d "$REMOTE_PATH/.git" ]; then
    git clone "$REPO_URL" "$REMOTE_PATH"
else
    echo "Repo already cloned, skipping."
fi

cd "$REMOTE_PATH"

# Install Python packages
pip install -r requirements.txt

# Copy .env if not already there (you must create this manually)
if [ ! -f .env ]; then
    echo "WARNING: .env not found. Create $REMOTE_PATH/.env with your API keys."
fi

echo "==> Server setup complete. Create .env file and configure supervisor."
