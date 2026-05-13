# Deployment Workflow

## Source of Truth
- **GitHub:** `https://github.com/Akash-Modi104/trading-bot`
- **Production server:** `root@187.127.73.203:/opt/trading-bot`
- **Live URL:** `https://dilipcentralacademy.tech`

## Daily Developer Loop
```bash
# 1. Edit locally
git pull origin main          # always start fresh
# ... edit files ...

# 2. Verify locally
python -m py_compile yourfile.py

# 3. Commit + push to GitHub
git add <files>
git commit -m "Brief description of change"
git push origin main

# 4. Deploy to production (one command)
ssh root@187.127.73.203 'cd /opt/trading-bot && ./deploy.sh'
```

## What `deploy.sh` does
1. `git fetch + reset --hard origin/main` (server always matches GitHub)
2. `pip install -r requirements.txt` (if changed)
3. Python syntax check on every bot file
4. Pre-restart audit (audit.py) — catches misconfig
5. `supervisorctl restart` all services
6. Confirms each service is RUNNING
7. Tails bot log to surface startup errors

## Services (managed by supervisor)
| Service        | Command                          | Restart           |
|----------------|----------------------------------|-------------------|
| `dashboard`    | `api_server.py`                  | Always-on (Flask) |
| `indian-bot`   | `indian_bot.py` (Zerodha live)   | Always-on         |
| `trading-bot`  | `intraday_bot_v2.py` (Alpaca)    | Always-on         |
| `scanner`      | `local_scanner.py`               | Always-on         |
| `open-webui`   | external chat UI                 | Independent       |

## What NOT to commit (in `.gitignore`)
- `users.db` (encrypted user creds + audit log)
- `.env` (secrets, encryption keys)
- `*_state.json`, `*_log.json`, `negative_news_in.json` (runtime state)
- `*.bak.*` (working backups during edits)

## Rollback
```bash
# On server:
cd /opt/trading-bot
git log --oneline -10                 # find commit before bad change
git reset --hard <COMMIT_SHA>
supervisorctl restart dashboard indian-bot trading-bot
```

## CI Hook (optional future)
GitHub Actions can SSH-deploy on every push to main. Workflow file would call
the same `deploy.sh` after running tests + syntax check.

## Emergency Stops
```bash
# Kill all trading immediately (positions stay open, GTT brackets remain)
supervisorctl stop indian-bot trading-bot

# Square off all Zerodha MIS positions NOW (uses dashboard panic-flat button)
curl -k -X POST https://dilipcentralacademy.tech/api/zerodha/squareoff \
     -b cookies.txt
```
