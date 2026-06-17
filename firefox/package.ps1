<#
  package.ps1 — build an installable .xpi for the Grokive Prompt Studio add-on.

  Node-free: zips ONLY the runtime extension files (so the README, this script,
  .gitignore, and any dist/ never end up inside the package), with manifest.json
  at the archive root exactly as Firefox requires. The .xpi version in the name is
  read from manifest.json.

  Usage (from anywhere):
      pwsh -File firefox/package.ps1
  or, inside the firefox/ folder:
      ./package.ps1

  Output:  firefox/dist/grokive-promptstudio-<version>.xpi

  This produces an UNSIGNED package. To make it permanently installable in normal
  Firefox you must sign it (see the README "Making it permanent" section). The
  unsigned .xpi installs as-is only on Developer Edition / Nightly / ESR with
  xpinstall.signatures.required = false.
#>

$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot

# Read the version straight from the manifest so the filename always matches.
$manifest = Get-Content (Join-Path $src 'manifest.json') -Raw | ConvertFrom-Json
$version  = $manifest.version
if (-not $version) { throw "Could not read 'version' from manifest.json" }

# The exact set of runtime parts that belong in the package (explicit allow-list).
$parts = @('manifest.json', 'icons', 'lib', 'background', 'popup', 'options', 'content')
foreach ($p in $parts) {
    $full = Join-Path $src $p
    if (-not (Test-Path $full)) { throw "Missing expected extension part: $p" }
}

$distDir = Join-Path $src 'dist'
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

$zip = Join-Path $distDir "grokive-promptstudio-$version.zip"
$xpi = Join-Path $distDir "grokive-promptstudio-$version.xpi"
if (Test-Path $zip) { Remove-Item $zip -Force }
if (Test-Path $xpi) { Remove-Item $xpi -Force }

# Compress-Archive places each named file/folder at the archive ROOT, so
# manifest.json lands at the top level (required) and icons/, lib/, ... stay as
# sibling folders.
$items = $parts | ForEach-Object { Join-Path $src $_ }
Compress-Archive -Path $items -DestinationPath $zip -Force

# An .xpi is just a renamed .zip.
Move-Item $zip $xpi -Force

$size = [math]::Round((Get-Item $xpi).Length / 1KB, 1)
Write-Host "Built $xpi ($size KB, v$version)" -ForegroundColor Green
Write-Host "Install on Dev/Nightly/ESR via about:addons -> gear -> Install Add-on From File," -ForegroundColor DarkGray
Write-Host "or sign it for normal Firefox (see README -> Making it permanent)." -ForegroundColor DarkGray
