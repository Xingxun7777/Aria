# Cleanup all Aria processes and lock files

# Kill all Aria related processes
$procs = Get-Process -Name python, pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like '*aria*' -or $_.Path -like '*AIBOX*' }

if ($procs) {
    foreach ($p in $procs) {
        Write-Host "Killing PID $($p.Id): $($p.Path)"
        Stop-Process -Id $p.Id -Force
    }
} else {
    Write-Host "No Aria processes found"
}

# Clean up lock files
Start-Sleep -Seconds 2
$locks = Get-ChildItem "C:\Users\84238\AppData\Local\Temp\aria*.lock" -ErrorAction SilentlyContinue
if ($locks) {
    foreach ($l in $locks) {
        Remove-Item $l.FullName -Force -ErrorAction SilentlyContinue
        Write-Host "Removed: $($l.Name)"
    }
} else {
    Write-Host "No lock files found"
}

Write-Host "Cleanup complete"
