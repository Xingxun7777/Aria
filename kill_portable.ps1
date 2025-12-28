# Kill all portable version processes
$portable = Get-Process -Name python, pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like '*dist_portable*' }

if ($portable) {
    foreach ($p in $portable) {
        Write-Host "Killing PID $($p.Id): $($p.Path)"
        Stop-Process -Id $p.Id -Force
    }
} else {
    Write-Host "No portable processes found"
}

# Wait and remove lock file
Start-Sleep -Seconds 2
