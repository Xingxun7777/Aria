Get-Process -Name "python" -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "Python processes killed"
