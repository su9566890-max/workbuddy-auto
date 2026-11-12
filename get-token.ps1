# get-token.ps1
# Copy the WorkBuddy desktop accessToken to the clipboard, for pasting into
# a GitHub repository Secret (name: WB_TOKEN).
# The full token is never printed to the console.

$ErrorActionPreference = 'Stop'

$infoPath = Join-Path $env:LOCALAPPDATA 'CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info'

if (-not (Test-Path -LiteralPath $infoPath)) {
    Write-Host "ERROR: login state file not found:" -ForegroundColor Red
    Write-Host "  $infoPath"
    Write-Host "Please sign in to the WorkBuddy desktop app first."
    exit 1
}

$raw  = Get-Content -LiteralPath $infoPath -Raw -Encoding UTF8
$auth = ($raw | ConvertFrom-Json).auth

$token  = $auth.accessToken
$domain = $auth.domain
if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Host "ERROR: accessToken is empty in the login state file." -ForegroundColor Red
    exit 1
}

Set-Clipboard -Value $token

$expiresAt = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$auth.expiresAt).ToLocalTime()
$preview   = $token.Substring(0, 6) + '...' + $token.Substring($token.Length - 4)

Write-Host "OK - accessToken copied to clipboard." -ForegroundColor Green
Write-Host ("  preview    : {0}" -f $preview)
Write-Host ("  length     : {0}" -f $token.Length)
Write-Host ("  domain     : {0}" -f $domain)
Write-Host ("  expires at : {0}" -f $expiresAt.ToString('yyyy-MM-dd HH:mm:ss zzz'))
Write-Host ""
Write-Host "Paste it into: GitHub repo -> Settings -> Secrets and variables -> Actions -> New repository secret"
Write-Host "Secret name must be exactly: WB_TOKEN"
