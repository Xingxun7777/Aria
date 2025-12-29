' Aria GUI Launcher (No Console Window)
' Double-click to run Aria with floating ball UI

Set FSO = CreateObject("Scripting.FileSystemObject")
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = ScriptDir
WshShell.Run """" & ScriptDir & "\.venv\Scripts\pythonw.exe"" """ & ScriptDir & "\launcher.py""", 0, False
