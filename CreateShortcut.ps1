# Create desktop shortcut for VoiceType Dev
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\VoiceType Dev.lnk")
$Shortcut.TargetPath = "F:\anaconda\pythonw.exe"
$Shortcut.Arguments = "-m voicetype.launcher"
$Shortcut.WorkingDirectory = "G:\AIBOX\voicetype-v1.1-dev"
$Shortcut.Description = "VoiceType Dev - Local AI Voice Dictation"
# Set custom icon
$IconPath = "G:\AIBOX\voicetype-v1.1-dev\assets\voicetype.ico"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
}
$Shortcut.Save()
Write-Host "Desktop shortcut 'VoiceType Dev' created with custom icon!"
