# Create desktop shortcut for Aria Dev
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Aria Dev.lnk")
$Shortcut.TargetPath = "F:\anaconda\pythonw.exe"
$Shortcut.Arguments = "-m aria.launcher"
$Shortcut.WorkingDirectory = "G:\AIBOX\aria-v1.1-dev"
$Shortcut.Description = "Aria Dev - Local AI Voice Dictation"
# Set custom icon
$IconPath = "G:\AIBOX\aria-v1.1-dev\assets\aria.ico"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
}
$Shortcut.Save()
Write-Host "Desktop shortcut 'Aria Dev' created with custom icon!"
