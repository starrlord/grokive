<#
  sign.ps1 — sign the Grokive Prompt Studio add-on via Mozilla AMO and download
  the signed .xpi. Thin, fail-fast wrapper around `npx web-ext sign`.

  Credentials come from the environment (so secrets stay out of your shell
  history and out of this file). Get them once at
  https://addons.mozilla.org/developers/addon/api/key/ -> Generate new credentials:

      $env:WEB_EXT_API_KEY    = "user:1234567:89"    # JWT issuer
      $env:WEB_EXT_API_SECRET = "your-long-secret"   # JWT secret (shown once)

  Then, from anywhere:

      pwsh -File firefox/sign.ps1            # unlisted (self-distributed) build
      pwsh -File firefox/sign.ps1 -Channel listed   # submit a listed build (public, reviewed)

  On success the signed .xpi lands in firefox/dist/. Install it via
  about:addons -> gear -> Install Add-on From File. It then survives restarts.

  AMO rejects re-signing a version that already exists, so bump "version" in
  manifest.json before each new sign. Requires Node.js (uses npx; nothing to
  pre-install). MV2 deprecation warnings in the output are expected.
#>

[CmdletBinding()]
param(
    [ValidateSet('unlisted', 'listed')]
    [string]$Channel = 'unlisted'
)

$ErrorActionPreference = 'Stop'
$src  = $PSScriptRoot
$dist = Join-Path $src 'dist'

# --- Preconditions -----------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($env:WEB_EXT_API_KEY) -or
    [string]::IsNullOrWhiteSpace($env:WEB_EXT_API_SECRET)) {
    Write-Host "Missing AMO API credentials." -ForegroundColor Red
    Write-Host "Get them at https://addons.mozilla.org/developers/addon/api/key/ then set:" -ForegroundColor DarkGray
    Write-Host '    $env:WEB_EXT_API_KEY    = "user:1234567:89"'  -ForegroundColor DarkGray
    Write-Host '    $env:WEB_EXT_API_SECRET = "your-long-secret"' -ForegroundColor DarkGray
    exit 1
}

if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
    Write-Host "npx (Node.js) was not found on PATH. Install Node.js, then re-run." -ForegroundColor Red
    exit 1
}

$version = (Get-Content (Join-Path $src 'manifest.json') -Raw | ConvertFrom-Json).version
if ([string]::IsNullOrWhiteSpace($version)) {
    Write-Host "Could not read 'version' from manifest.json." -ForegroundColor Red
    exit 1
}
Write-Host "Signing Grokive Prompt Studio v$version  (channel: $Channel)" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# Snapshot the .xpi files already in dist/ (it's shared with package.ps1's UNSIGNED
# output). After signing, the freshly-downloaded signed .xpi is whatever is NEW, so
# we never misreport a stale/unsigned artifact as Signed.
$beforeXpi = @(Get-ChildItem $dist -Filter '*.xpi' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)

# --- Sign --------------------------------------------------------------------
# web-ext reads WEB_EXT_API_KEY / WEB_EXT_API_SECRET from the environment, so the
# secret is never placed on the command line. Non-runtime files (and dist/, which
# lives inside the source dir) are excluded so the signed package holds only the
# extension itself — web-ext does NOT special-case a custom --artifacts-dir during
# sign, so dist/ is ignored explicitly to keep the build deterministic.
& npx --yes web-ext sign `
    --channel=$Channel `
    --source-dir="$src" `
    --artifacts-dir="$dist" `
    --ignore-files=README.md `
    --ignore-files=package.ps1 `
    --ignore-files=sign.ps1 `
    --ignore-files=dist/**

if ($LASTEXITCODE -ne 0) {
    Write-Host "`nweb-ext sign failed (exit $LASTEXITCODE). See the output above." -ForegroundColor Red
    exit $LASTEXITCODE
}

# web-ext writes the signed file as <uuid>-<version>.xpi. It's the newest .xpi whose
# name wasn't already in dist/ before signing, so a stale/unsigned artifact can never
# be misreported as Signed.
$signed = $null
foreach ($xpi in (Get-ChildItem $dist -Filter '*.xpi' -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending)) {
    if ($xpi.Name -notin $beforeXpi) { $signed = $xpi; break }
}

if ($signed) {
    Write-Host "`nSigned: $($signed.FullName)" -ForegroundColor Green
    Write-Host "Install via about:addons -> gear -> Install Add-on From File." -ForegroundColor DarkGray
}
elseif ($Channel -eq 'listed') {
    Write-Host "`nSubmitted to AMO for review (listed channel) — no local .xpi is produced now." -ForegroundColor Yellow
    Write-Host "Listed builds are reviewed asynchronously; download it from your AMO dashboard once approved." -ForegroundColor DarkGray
}
else {
    Write-Host "`nweb-ext reported success but no freshly-signed .xpi appeared in $dist." -ForegroundColor Yellow
    Write-Host "Check the output above (and any web-ext-artifacts/ folder) — the signed file may have landed elsewhere." -ForegroundColor DarkGray
    exit 1
}
