# ============================================================================
#  publish-to-github.ps1
#  FIRST-TIME publish of this project to GitHub, from PowerShell.
#
#  Prerequisites (one time):
#    - Git installed            https://git-scm.com/download/win
#    - GitHub CLI installed     https://cli.github.com   (recommended)
#    - Run:  gh auth login      (paste your token once; see the README/chat)
#
#  Then just run this script from inside the project folder:
#    powershell -ExecutionPolicy Bypass -File .\publish-to-github.ps1
# ============================================================================

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$RepoUrl = "https://github.com/Jduchock/Mister-Johns-ED-Dashboard.git"

# --- safety: refuse to run if a .env is about to be exposed -----------------
if (Test-Path ".env") {
    $ignored = (git check-ignore .env 2>$null)
    if (-not $ignored) {
        Write-Host "STOP: .env is not being ignored. Aborting to protect your keys." -ForegroundColor Red
        exit 1
    }
}

if (-not (Test-Path ".git")) {
    git init
    git branch -M main
    git remote add origin $RepoUrl
} elseif (-not (git remote 2>$null | Select-String origin)) {
    git remote add origin $RepoUrl
}

git add -A
git status --short

$msg = Read-Host "Commit message (Enter for a default)"
if ([string]::IsNullOrWhiteSpace($msg)) {
    $msg = "Update dashboard - " + (Get-Date -Format "yyyy-MM-dd HH:mm")
}
git commit -m $msg

Write-Host "`nPushing to $RepoUrl ..." -ForegroundColor Cyan
git push -u origin main

Write-Host "`nDone. If this was the first push, enable GitHub Pages:" -ForegroundColor Green
Write-Host "  repo -> Settings -> Pages -> Source: 'Deploy from a branch'" -ForegroundColor Green
Write-Host "  Branch: main   Folder: /docs   -> Save" -ForegroundColor Green
Write-Host "  Live at: https://jduchock.github.io/Mister-Johns-ED-Dashboard/" -ForegroundColor Green
