# Build a standalone Windows zip: UsageFloat.exe + bundled mpv.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m unittest test_usage_float.py
if ($LASTEXITCODE -ne 0) {
    throw "unit tests failed"
}

python -m PyInstaller --noconfirm --clean usage_float.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed"
}

$stage = Join-Path $PSScriptRoot "dist\UsageFloat"
if (-not (Test-Path -LiteralPath (Join-Path $stage "UsageFloat.exe"))) {
    throw "UsageFloat.exe was not produced"
}

$mpvSrc = Join-Path $PSScriptRoot "vendor\mpv"
$mpvDst = Join-Path $stage "vendor\mpv"
New-Item -ItemType Directory -Path $mpvDst -Force | Out-Null
foreach ($name in @("mpv.exe", "d3dcompiler_43.dll", "LICENSE.GPL", "Copyright")) {
    $src = Join-Path $mpvSrc $name
    if (-not (Test-Path -LiteralPath $src)) {
        throw "missing $src — run .\install-mpv.ps1 first"
    }
    Copy-Item -LiteralPath $src -Destination (Join-Path $mpvDst $name) -Force
}

foreach ($name in @("wallpaper-start.lua", "LICENSE", "THIRD_PARTY_NOTICES.md", "README.md")) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $stage $name) -Force
}

$versionMatch = Select-String -Path (Join-Path $PSScriptRoot "usage_float.py") -Pattern '^VERSION = "([^"]+)"' | Select-Object -First 1
if (-not $versionMatch) {
    throw "VERSION not found in usage_float.py"
}
$version = $versionMatch.Matches[0].Groups[1].Value
$zipName = "usage-float-$version-windows-x64.zip"
$zipPath = Join-Path $PSScriptRoot "dist\$zipName"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Compress-Archive -Path $stage -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host "Built $zipPath"
