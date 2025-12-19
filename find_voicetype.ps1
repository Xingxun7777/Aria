Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    $id = $_.Id
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$id").CommandLine
    if ($cmd -match 'voicetype') {
        Write-Output "PID: $id - $cmd"
    }
}
