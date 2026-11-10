$ErrorActionPreference = "Stop"

# The pin lives in mpv-pin.json, shared with mpv_provision.py (which usage-float runs itself into
# ~/.usage-float/runtime/mpv when mpv is missing). Sources are tried in order; each has its own SHA-256.
$pin = Get-Content -LiteralPath (Join-Path $PSScriptRoot "mpv-pin.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$targetDir = Join-Path $PSScriptRoot "vendor\mpv"
$licenseDir = Join-Path $PSScriptRoot "licenses\mpv"
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempArchive = Join-Path $tempRoot ("usage-float-mpv-" + [guid]::NewGuid().ToString("N") + ".7z")
$tempExtract = Join-Path $tempRoot ("usage-float-mpv-" + [guid]::NewGuid().ToString("N"))

try {
    $downloaded = $null
    foreach ($source in $pin.sources) {
        $expectedSha256 = $source.sha256.ToLowerInvariant()
        foreach ($url in $source.urls) {
            try {
                Write-Host "Downloading pinned mpv build $($pin.build) ($($source.archive)) from $url ..."
                Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tempArchive -UserAgent "usage-float-mpv-provision"
                $actualSha256 = (Get-FileHash -LiteralPath $tempArchive -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($actualSha256 -ne $expectedSha256) {
                    Write-Warning "mpv archive checksum mismatch from ${url}: $actualSha256"
                    continue
                }
                $downloaded = $source
                break
            }
            catch {
                Write-Warning "Download failed from ${url}: $_"
            }
        }
        if ($downloaded) {
            break
        }
    }
    if (-not $downloaded) {
        throw "Could not download a verified mpv archive (build $($pin.build))"
    }

    New-Item -ItemType Directory -Path $tempExtract -Force | Out-Null
    # System32's bsdtar reads 7z; a Git-for-Windows GNU tar earlier on PATH does not.
    & (Join-Path $env:SystemRoot "System32\tar.exe") -xf $tempArchive -C $tempExtract @($pin.files)
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to extract mpv archive (tar exit $LASTEXITCODE)"
    }

    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    foreach ($name in $pin.files) {
        Copy-Item -LiteralPath (Join-Path $tempExtract $name) -Destination (Join-Path $targetDir $name) -Force
    }
    foreach ($name in @("LICENSE.GPL", "Copyright")) {
        Copy-Item -LiteralPath (Join-Path $licenseDir $name) -Destination (Join-Path $targetDir $name) -Force
    }

    Write-Host "mpv installed to $targetDir (from $($downloaded.archive))"
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
