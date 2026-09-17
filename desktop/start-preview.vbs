' Launch the already-built developer preview without a terminal window.
Option Explicit
Dim shell, files, root, exe, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
exe = root & "\node_modules\electron\dist\electron.exe"
If Not files.FileExists(exe) Or Not files.FileExists(root & "\dist\main\index.js") Then
  MsgBox "Developer preview has not been built. See desktop\README.md. This is not a standalone installer.", 48, "Bucker Desktop"
  WScript.Quit 1
End If
shell.CurrentDirectory = root
command = Chr(34) & exe & Chr(34) & " " & Chr(34) & root & Chr(34)
shell.Run command, 0, False
