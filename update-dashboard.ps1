# ============================================================================
#  update-dashboard.ps1
#  Run AFTER Nova rebuilds the field log. Copies the newest dashboard into
#  docs/ and pushes it, so the public page tracks your latest watch.
#  Adjust $LiveDashboard if your EDMC logs folder differs.
# ============================================================================
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$LiveDashboard = "$env:LOCALAPPDATA\EDMarketConnector\logs\kestrel-field-log.html"
$LiveLogDetail = "$env:LOCALAPPDATA\EDMarketConnector\logs\log-detail.html"

if (-not (Test-Path $LiveDashboard)) {
    Write-Host "Live dashboard not found at $LiveDashboard" -ForegroundColor Red
    exit 1
}
Copy-Item $LiveDashboard "docs\index.html" -Force
if (Test-Path $LiveLogDetail) { Copy-Item $LiveLogDetail "docs\log-detail.html" -Force }

# strip the carrier markers from the published copy
(Get-Content "docs\index.html" -Raw) `
    -replace "<!-- CARRIER:BEGIN -->","" -replace "<!-- CARRIER:END -->","" `
    | Set-Content "docs\index.html"

git add docs
git commit -m ("Dashboard update - " + (Get-Date -Format "yyyy-MM-dd HH:mm"))
git push
Write-Host "Public dashboard updated." -ForegroundColor Green
