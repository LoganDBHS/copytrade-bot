' Opens the CopyTrade dashboard with no terminal window: starts the
' Streamlit server hidden (if it isn't running already) and opens the
' browser to it. Server log: data\dashboard.log. The server keeps running
' in the background; it is read-only over bot.db and harmless to leave up.
Dim sh, wmi, procs, root, cmd
root = "C:\Users\logan\Documents\CopyTrade Bot"
Set sh = CreateObject("WScript.Shell")
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery( _
    "SELECT ProcessId FROM Win32_Process WHERE Name LIKE 'python%' " & _
    "AND CommandLine LIKE '%streamlit%'")
If procs.Count = 0 Then
    sh.CurrentDirectory = root
    cmd = "cmd /c """"" & root & "\.venv-dash\Scripts\python.exe"" -m streamlit run " & _
          "src\dashboard\app.py --server.headless true >> """ & _
          root & "\data\dashboard.log"" 2>&1"""
    sh.Run cmd, 0, False
    WScript.Sleep 6000
End If
sh.Run "http://localhost:8501"
