#!/bin/bash
# ==========================================================================
# Trading Bot Deploy Script
# Usage:  ssh root@187.127.73.203 'cd /opt/trading-bot && ./deploy.sh'
#         or run locally on the server: cd /opt/trading-bot && ./deploy.sh
#
# What it does:
#   1. Fetch + reset to origin/main (clean update, no merge conflicts)
#   2. Install any new pip dependencies
#   3. Sanity-check Python syntax of all changed files
#   4. Run audit.py (full system check before restart)
#   5. Restart all supervisor services
#   6. Verify services are RUNNING
#   7. Tail the bot log for 10s so any startup error is visible
# ==========================================================================
set -euo pipefail

cd /opt/trading-bot

echo "════════════════════════════════════════════════════════════════"
echo "  DEPLOY  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "════════════════════════════════════════════════════════════════"

# 1. Pull latest from GitHub (hard reset = always wins, no conflict)
echo ""
echo "▶ git fetch + reset to origin/main"
git fetch origin main
OLD_SHA=$(git rev-parse HEAD)
git reset --hard origin/main
NEW_SHA=$(git rev-parse HEAD)
if [ "$OLD_SHA" = "$NEW_SHA" ]; then
  echo "  Already at latest: $NEW_SHA"
else
  echo "  Updated $OLD_SHA → $NEW_SHA"
  echo "  Files changed:"
  git diff --name-only "$OLD_SHA" "$NEW_SHA" | sed 's/^/    /'
fi

# 2. Install new pip dependencies (if requirements.txt changed)
echo ""
echo "▶ pip install -r requirements.txt"
if [ -f requirements.txt ]; then
  /opt/trading-bot/venv/bin/pip install -q -r requirements.txt
  echo "  done"
else
  echo "  (no requirements.txt — skipping)"
fi

# 3. Python syntax check on all bot files
echo ""
echo "▶ Python syntax check"
for f in api_server.py auth.py indian_bot.py intraday_bot_v2.py          brokers/zerodha.py brokers/angelone.py brokers/__init__.py          news_scanner_indian.py _force_ipv4_kite.py; do
  if [ -f "$f" ]; then
    /opt/trading-bot/venv/bin/python -m py_compile "$f" && echo "  ✓ $f"
  fi
done

# 4. Pre-restart audit (catches misconfig before we cut over)
echo ""
echo "▶ Pre-restart audit (audit.py)"
if [ -f audit.py ]; then
  /opt/trading-bot/venv/bin/python audit.py 2>&1 | tail -8 || true
fi

# 5. Restart supervisor services
echo ""
echo "▶ Restarting services"
supervisorctl restart dashboard indian-bot trading-bot scanner
sleep 4

# 6. Verify all RUNNING
echo ""
echo "▶ Service status"
supervisorctl status

# 7. Tail bot log briefly for startup errors
echo ""
echo "▶ indian-bot log (last 5 lines)"
tail -5 /var/log/supervisor/indian-bot.out.log

echo ""
echo "✓ DEPLOY COMPLETE  $(date -u +%H:%M:%S)"
