# Opens a shell already sitting in the voice-claude folder.
# $PSScriptRoot is this file's own directory, so it works from anywhere
# and copes with spaces in the path.
Set-Location -LiteralPath $PSScriptRoot

$activate = Join-Path $PSScriptRoot '.venv\Scripts\Activate.ps1'
if (Test-Path -LiteralPath $activate) {
    & $activate
} else {
    Write-Host 'No .venv here yet - run run.bat once to build it.' -ForegroundColor Yellow
}

Write-Host ''
Write-Host '  voice-claude' -ForegroundColor Cyan
Write-Host ''
Write-Host '    python voice_claude.py --exo      exobiology report: hold value, rank, projection'
Write-Host '    python voice_claude.py --check    verify keys, folders, mic, speakers'
Write-Host '    python voice_claude.py --tools    which tools and sites she can use'
Write-Host '    python voice_claude.py --auth     which Claude credential, and test it'
Write-Host '    python voice_claude.py --mic      5s mic test with a level meter'
Write-Host '    python voice_claude.py            start talking'
Write-Host ''
Write-Host '    python live.py --selftest         what the game is doing this second'
Write-Host '    python live.py --tail             follow live reactions, printed not spoken'
Write-Host ''
