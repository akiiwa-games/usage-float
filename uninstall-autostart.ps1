# Disable UsageFloat autostart
$reg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Remove-ItemProperty -Path $reg -Name "UsageFloat" -ErrorAction SilentlyContinue
Write-Host "Autostart OFF"
