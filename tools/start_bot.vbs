' Launches CopyTrade Bot hidden (no console window). Called by the
' "CopyTrade Bot" scheduled task each market morning; safe to double-run —
' run.py's singleton port lock makes a second instance exit immediately.
' Output goes to data\task_run.log.
Dim sh, root, py, log, cmd
root = "C:\Users\logan\Documents\CopyTrade Bot"
py = root & "\.venv\Scripts\python.exe"
log = root & "\data\task_run.log"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root

' Discord desktop is the bot's input feed (toast notifications). If it isn't
' running, start it in the tray — same command Discord's own login auto-start
' uses — and give it a head start before the bot comes up.
Dim wmi, discordProcs, appData
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set discordProcs = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='Discord.exe'")
If discordProcs.Count = 0 Then
    appData = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%")
    sh.Run """" & appData & "\Discord\Update.exe"" --processStart Discord.exe --process-start-args ""--start-inactive""", 0, False
    WScript.Sleep 20000
End If
cmd = "cmd /c """"" & py & """ -u run.py >> """ & log & """ 2>&1"""
sh.Run cmd, 0, False
