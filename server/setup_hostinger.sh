#!/bin/bash
# ============================================================
#  Hostinger KVM2 Setup Script
#  Run as root: bash setup_hostinger.sh
#  Tested on Ubuntu 22.04 LTS
# ============================================================
set -e

echo "============================================================"
echo "  Trading Bot + Ollama Setup for Hostinger KVM2"
echo "============================================================"

# ── 1. System update ────────────────────────────────────────
apt-get update -y && apt-get upgrade -y
apt-get install -y python3 python3-pip python3-venv git curl wget \
                   nginx supervisor ufw net-tools htop screen

# ── 2. Install Ollama ────────────────────────────────────────
echo "[+] Installing Ollama..."
curl -fsSL https://ollama.com/install.sh | sh
systemctl enable ollama
systemctl start ollama
sleep 5

# ── 3. Pull the LLM model ───────────────────────────────────
# KVM2 has 8GB RAM — Qwen2.5-7B-Instruct-Q4 fits comfortably (~4.5GB)
echo "[+] Pulling Qwen2.5-7B-Instruct model (4.5GB, ~3-5 min)..."
ollama pull qwen2.5:7b-instruct-q4_K_M

# Lighter fallback if RAM is tight:
# ollama pull qwen2.5:3b

echo "[+] Model downloaded."

# ── 4. Create app directory ──────────────────────────────────
mkdir -p /opt/trading-bot
cd /opt/trading-bot

# ── 5. Python virtualenv ─────────────────────────────────────
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install requests numpy pytz flask matplotlib duckduckgo-search \
            beautifulsoup4 lxml schedule python-dotenv

echo "[+] Python dependencies installed."

# ── 6. Firewall ──────────────────────────────────────────────
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw allow 5001   # Trading dashboard (restrict later via Nginx)
ufw --force enable
echo "[+] Firewall configured."

# ── 7. Nginx reverse proxy for dashboard ────────────────────
cat > /etc/nginx/sites-available/trading << 'EOF'
server {
    listen 80;
    server_name _;          # Replace _ with your domain if you have one

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_read_timeout 120;
        # Basic auth - set username/password below
        auth_basic "Trading Dashboard";
        auth_basic_user_file /etc/nginx/.htpasswd;
    }
}
EOF

ln -sf /etc/nginx/sites-available/trading /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Create dashboard password (change USER and PASS)
DASH_USER="admin"
DASH_PASS="changeme123"
echo "${DASH_USER}:$(openssl passwd -apr1 ${DASH_PASS})" > /etc/nginx/.htpasswd
echo "[+] Dashboard will be protected with password: $DASH_USER / $DASH_PASS"
echo "    IMPORTANT: Change these in /etc/nginx/.htpasswd"

nginx -t && systemctl reload nginx

# ── 8. Supervisor config (keeps bot + scanner running 24/7) ──
cat > /etc/supervisor/conf.d/trading.conf << 'EOF'
[program:trading-dashboard]
command=/opt/trading-bot/venv/bin/python dashboard.py
directory=/opt/trading-bot
autostart=true
autorestart=true
startretries=10
stderr_logfile=/var/log/trading-dashboard.err.log
stdout_logfile=/var/log/trading-dashboard.out.log
user=root
environment=PYTHONUNBUFFERED="1"

[program:trading-bot]
command=/opt/trading-bot/venv/bin/python intraday_bot_v2.py
directory=/opt/trading-bot
autostart=true
autorestart=true
startretries=10
stderr_logfile=/var/log/trading-bot.err.log
stdout_logfile=/var/log/trading-bot.out.log
user=root
environment=PYTHONUNBUFFERED="1"

[program:local-scanner]
command=/opt/trading-bot/venv/bin/python local_scanner.py
directory=/opt/trading-bot
autostart=true
autorestart=true
startretries=10
stderr_logfile=/var/log/trading-scanner.err.log
stdout_logfile=/var/log/trading-scanner.out.log
user=root
environment=PYTHONUNBUFFERED="1"
EOF

supervisorctl reread
supervisorctl update

echo ""
echo "============================================================"
echo "  SETUP COMPLETE"
echo "============================================================"
echo "  Next steps:"
echo "  1. Upload your bot files to /opt/trading-bot/"
echo "  2. Create /opt/trading-bot/.env with your credentials"
echo "  3. Run: supervisorctl start all"
echo "  4. Dashboard: http://YOUR_SERVER_IP  (user: admin / changeme123)"
echo "  5. Monitor: tail -f /var/log/trading-bot.out.log"
echo "============================================================"
