# Create desktop shortcut for Aria Dev
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Aria Dev.lnk")
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$RuntimeExe = Join-Path $ProjectRoot ".venv\Scripts\AriaDevRuntime.exe"
$Rcedit = Join-Path $ProjectRoot "tools\rcedit.exe"
# Prefer a renamed runtime so Task Manager shows a friendly process name.
$Pythonw = "F:\anaconda\pythonw.exe"
if (Test-Path $VenvPythonw) {
    try {
        Copy-Item -Path $VenvPythonw -Destination $RuntimeExe -Force
    } catch {
        # Keep going; runtime may already exist but be locked by a running process.
    }
    if (Test-Path $RuntimeExe) {
        $Pythonw = $RuntimeExe
    } else {
        $Pythonw = $VenvPythonw
    }
}
if ((Test-Path $Rcedit) -and (Test-Path $RuntimeExe)) {
    $patched = $true
    & $Rcedit $RuntimeExe --set-version-string "FileDescription" "Aria Dev" 2>$null
    if ($LASTEXITCODE -ne 0) { $patched = $false }
    & $Rcedit $RuntimeExe --set-version-string "ProductName" "Aria Dev" 2>$null
    if ($LASTEXITCODE -ne 0) { $patched = $false }
    & $Rcedit $RuntimeExe --set-version-string "OriginalFilename" "AriaDevRuntime.exe" 2>$null
    if ($LASTEXITCODE -ne 0) { $patched = $false }
    if (-not $patched) {
        Write-Host "Runtime is in use. Close Aria, then re-run CreateShortcut.ps1 to apply friendly process name."
    }
}
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = "-m aria.launcher"
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description = "Aria Dev - Local AI Voice Dictation"
# Set custom icon
$IconPath = Join-Path $ProjectRoot "assets\aria.ico"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
}
$Shortcut.Save()
Write-Host "Desktop shortcut 'Aria Dev' created with custom icon!"
