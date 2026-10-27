' Fully-detached silent launcher (survives parent shell exit)
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
script = dir & "\usage_float.py"

' Prefer pythonw
pyw = "pythonw.exe"
On Error Resume Next
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
cmd = """" & pyw & """ """ & script & """"
' Win32_Process.Create detaches from any console job
Set startup = wmi.Get("Win32_ProcessStartup").SpawnInstance_
startup.ShowWindow = 1
result = wmi.Get("Win32_Process").Create(cmd, dir, startup, pid)
If result <> 0 Then
  ' Fallback: PATH may not have pythonw — try common install path via cmd start
  sh.Run "cmd /c start """" /D """ & dir & """ pythonw """ & script & """", 0, False
End If
