Option Explicit
Dim shell, files, root, command, result, quote
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
quote = Chr(34)
command = quote & shell.ExpandEnvironmentStrings("%ComSpec%") & quote & " /d /s /c " & quote & quote & root & "\tools\start_gui.cmd" & quote & quote
result = shell.Run(command, 0, True)
If result <> 0 Then
    MsgBox "AutoDL Watcher could not start. Check the Python environment and .ui\startup.log.", 16, "AutoDL GPU Watcher"
End If
