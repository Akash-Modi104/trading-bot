#!/bin/bash
# ============================================================
#  AlgoTrader — SSL + Nginx setup for Hostinger KVM2
#
#  Run ON the server after deploying:
#    ssh root@YOUR_SERVER_IP
#    bash /opt/trading-bot/server/ssl_setup.sh
#
#  Option A (recommended): provide a domain → Let's Encrypt cert
#  Option B (fallback):    leave domain blank → self-signed cert
# ============================================================
set -e

APP_PORT=5001
NGINX_CONF_SRC="/opt/trading-bot/server/nginx.conf"
NGINX_CONF_DST="/etc/nginx/sites-available/algotrader"
SSL_DIR="/etc/ssl/algotrader"

echo ""
echo "========================================"
echo "  AlgoTrader — SSL + Nginx Setup"
echo "========================================"
echo ""

# ── Gather inputs ────────────────────────────────────────────
read -p "Domain name (e.g. trading.example.com) — leave blank for self-signed: " DOMAIN
if [ -n "$DOMAIN" ]; then
    read -p "Email for Let's Encrypt renewal alerts: " EMAIL
fi

DASHBOARD_USER="${DASHBOARD_USER:-admin}"
echo ""
read -p "Dashboard username [admin]: " INPUT_USER
[ -n "$INPUT_USER" ] && DASHBOARD_USER="$INPUT_USER"

read -sp "Dashboard password (leave blank = no auth): " DASHBOARD_PASS
echo ""

# ── Install packages ─────────────────────────────────────────
echo "[1/5] Installing nginx…"
apt-get update -q
apt-get install -y nginx

if [ -n "$DOMAIN" ]; then
    echo "[1/5] Installing certbot…"
    apt-get install -y certbot python3-certbot-nginx
fi

# ── SSL directory ────────────────────────────────────────────
echo "[2/5] Setting up SSL certificates…"
mkdir -p "$SSL_DIR"

if [ -n "$DOMAIN" ]; then
    # ── Let's Encrypt (real cert) ────────────────────────────
    echo "  Obtaining Let's Encrypt cert for $DOMAIN…"

    # Temporarily serve ACME challenge via port 80 before nginx is configured
    # Use standalone mode first, then switch to nginx
    certbot certonly \
        --standalone \
        --agree-tos \
        --non-interactive \
        --email "$EMAIL" \
        -d "$DOMAIN" || {
            echo "  certbot standalone failed — trying webroot after nginx is up"
            USE_CERTBOT_NGINX=1
        }

    if [ -z "$USE_CERTBOT_NGINX" ]; then
        ln -sf "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" "$SSL_DIR/fullchain.pem"
        ln -sf "/etc/letsencrypt/live/$DOMAIN/privkey.pem"   "$SSL_DIR/privkey.pem"
    fi

    # Auto-renewal cron
    (crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet --post-hook 'systemctl reload nginx'") | sort -u | crontab -
    echo "  Auto-renewal cron installed."

else
    # ── Self-signed cert (fallback) ──────────────────────────
    echo "  Generating self-signed certificate (valid 10 years)…"
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "$SSL_DIR/privkey.pem" \
        -out    "$SSL_DIR/fullchain.pem" \
        -subj "/CN=$(hostname -I | awk '{print $1}')" \
        2>/dev/null
    echo "  Self-signed cert created. Browser will show a warning — click 'Advanced → Proceed'."
fi

# ── Write .env DASHBOARD credentials ────────────────────────
echo "[3/5] Writing dashboard credentials to .env…"
ENV_FILE="/opt/trading-bot/.env"
# Remove existing entries
sed -i '/^DASHBOARD_USER=/d' "$ENV_FILE" 2>/dev/null || true
sed -i '/^DASHBOARD_PASS=/d' "$ENV_FILE" 2>/dev/null || true

if [ -n "$DASHBOARD_PASS" ]; then
    echo "DASHBOARD_USER=$DASHBOARD_USER" >> "$ENV_FILE"
    echo "DASHBOARD_PASS=$DASHBOARD_PASS" >> "$ENV_FILE"
    echo "  Dashboard auth enabled (user: $DASHBOARD_USER)"
else
    echo "  No password set — dashboard is publicly accessible"
fi

# ── Write Nginx config ───────────────────────────────────────
echo "[4/5] Configuring Nginx…"
cp "$NGINX_CONF_SRC" "$NGINX_CONF_DST"

# Set domain (or _ wildcard if none)
SERVER_NAME="${DOMAIN:-_}"
sed -i "s|server_name _;|server_name $SERVER_NAME;|g" "$NGINX_CONF_DST"

# Move the limit_req_zone directive to http block (nginx requires it there)
# Append to /etc/nginx/nginx.conf http block if not already present
if ! grep -q "limit_req_zone" /etc/nginx/nginx.conf; then
    sed -i '/http {/a\    limit_req_zone $binary_remote_addr zone=api:10m rate=30r\/s;' /etc/nginx/nginx.conf
fi
# Remove it from sites-available (it must be in http block only)
sed -i '/^limit_req_zone/d' "$NGINX_CONF_DST"

# Enable site
ln -sf "$NGINX_CONF_DST" /etc/nginx/sites-enabled/algotrader
rm -f /etc/nginx/sites-enabled/default

# If Let's Encrypt via nginx plugin (fallback)
if [ -n "$USE_CERTBOT_NGINX" ] && [ -n "$DOMAIN" ]; then
    nginx -t && systemctl start nginx
    certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive --redirect
    ln -sf "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" "$SSL_DIR/fullchain.pem"
    ln -sf "/etc/letsencrypt/live/$DOMAIN/privkey.pem"   "$SSL_DIR/privkey.pem"
fi

# Test and reload
nginx -t && systemctl enable nginx && systemctl restart nginx

# Restart dashboard so it picks up new .env (DASHBOARD_PASS)
echo "[5/5] Restarting dashboard service…"
supervisorctl restart dashboard || true

echo ""
echo "=========================================="
echo "  SSL SETUP COMPLETE"
echo "=========================================="
if [ -n "$DOMAIN" ]; then
    echo "  URL:  https://$DOMAIN"
else
    SERVER_IP=$(hostname -I | awk '{print $1}')
    echo "  URL:  https://$SERVER_IP  (self-signed — browser warning is normal)"
fi
[ -n "$DASHBOARD_PASS" ] && echo "  Auth: $DASHBOARD_USER / $DASHBOARD_PASS"
echo ""
echo "  Logs: tail -f /var/log/nginx/error.log"
echo "=========================================="
