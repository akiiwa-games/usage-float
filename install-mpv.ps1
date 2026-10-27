$ErrorActionPreference = "Stop"

$releaseTag = "20260814"
$archiveName = "mpv-x86_64-20260814-git-7b8915bc1d.7z"
$archiveUrl = "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/$releaseTag/$archiveName"
$expectedSha256 = "1bf3b029da2c98e605e00e85f21ee3142f22a1dcc4ceb5c827b5c51e36e390f9"
$targetDir = Join-Path $PSScriptRoot "vendor\mpv"
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempArchive = Join-Path $tempRoot ("usage-float-mpv-" + [guid]::NewGuid().ToString("N") + ".7z")
$tempExtract = Join-Path $tempRoot ("usage-float-mpv-" + [guid]::NewGuid().ToString("N"))

try {
    Write-Host "Downloading pinned mpv build $releaseTag..."
    Invoke-WebRequest -Uri $archiveUrl -OutFile $tempArchive

    $actualSha256 = (Get-FileHash -LiteralPath $tempArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $expectedSha256) {
        throw "mpv archive checksum mismatch: $actualSha256"
    }

    New-Item -ItemType Directory -Path $tempExtract -Force | Out-Null
    & tar.exe -xf $tempArchive -C $tempExtract mpv.exe d3dcompiler_43.dll
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to extract mpv archive (tar exit $LASTEXITCODE)"
    }

    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $tempExtract "mpv.exe") -Destination (Join-Path $targetDir "mpv.exe") -Force
    Copy-Item -LiteralPath (Join-Path $tempExtract "d3dcompiler_43.dll") -Destination (Join-Path $targetDir "d3dcompiler_43.dll") -Force
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/mpv-player/mpv/master/LICENSE.GPL" -OutFile (Join-Path $targetDir "LICENSE.GPL")
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/mpv-player/mpv/master/Copyright" -OutFile (Join-Path $targetDir "Copyright")

    Write-Host "mpv installed to $targetDir"
    & (Join-Path $targetDir "mpv.exe") --version | Select-Object -First 2
}
finally {
    if (Test-Path -LiteralPath $tempArchive) {
        Remove-Item -LiteralPath $tempArchive -Force
    }
    $resolvedExtract = [IO.Path]::GetFullPath($tempExtract)
    if ((Test-Path -LiteralPath $resolvedExtract) -and $resolvedExtract.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedExtract -Recurse -Force
    }
}
