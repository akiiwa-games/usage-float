# Enable UsageFloat on Windows logon (HKCU Run)
$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "usage_float.py"

$pywCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pywCmd) {
  $pyw = $pywCmd.Source
} else {
  $py = (Get-Command python.exe -ErrorAction Stop).Source
  $candidate = Join-Path (Split-Path $py -Parent) "pythonw.exe"
  if (Test-Path $candidate) { $pyw = $candidate } else { $pyw = $py }
}

$cmd = "`"$pyw`" `"$script`""
$reg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
New-ItemProperty -Path $reg -Name "UsageFloat" -Value $cmd -PropertyType String -Force | Out-Null
Write-Host "Autostart ON:"
Write-Host "  $cmd"
