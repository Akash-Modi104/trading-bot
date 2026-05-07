# AlgoTrader — Deploy to VPS
# Usage: .\deploy.ps1
# Requires: SSH key auth already set up (no password prompt)

param(
    [string]$Server = "root@187.127.73.203",
    [string]$RemotePath = "/opt/trading-bot"
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot

Write-Host "==> Deploying AlgoTrader to $Server`:$RemotePath" -ForegroundColor Cyan

# 1. Push latest commit to origin
Write-Host "`n[1/4] Pushing to git origin..." -ForegroundColor Yellow
git -C $RepoRoot push origin main
if ($LASTEXITCODE -ne 0) { Write-Host "Git push failed - aborting." -ForegroundColor Red; exit 1 }

# 2. Pull on server + install dependencies
Write-Host "`n[2/4] Pulling latest code on server..." -ForegroundColor Yellow
ssh $Server "set -e; cd $RemotePath; git pull origin main; pip install -r requirements.txt; echo `"Code updated successfully`""
if ($LASTEXITCODE -ne 0) { Write-Host "Remote pull failed." -ForegroundColor Red; exit 1 }

# 3. Restart services via supervisor
Write-Host "`n[3/4] Restarting services..." -ForegroundColor Yellow
ssh $Server "supervisorctl restart dashboard; supervisorctl restart trading-bot; supervisorctl restart scanner; echo `"Services restarted`""

# 4. Show service status
Write-Host "`n[4/4] Service status:" -ForegroundColor Yellow
ssh $Server "supervisorctl status"

Write-Host "`n==> Deploy complete!" -ForegroundColor Green
