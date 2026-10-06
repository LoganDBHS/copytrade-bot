# Operations guide — everything that lives OUTSIDE the code

The repo alone is not the whole system. This file lists every machine-level
piece so the bot can be rebuilt on a new computer (or repaired on this one).
Current machine: Logan's Windows 11 desktop, project at
`C:\Users\logan\Documents\CopyTrade Bot`.

## Discord control-channel commands (authorized user only)

| command | what it does |
|---|---|
| `status` | mode, open positions with stops and live marks, realized P&L today |
| `alerters` | each alerter: following or not, alerts today, open plays |
| `follow alpha` | trade this alerter's new entries again |
| `unfollow alpha` | stop NEW entries from this alerter — alerts still parsed and logged (expectancy keeps accumulating), open plays keep being managed to exit; survives restarts and the Keeper |
| `pause` | stop taking NEW entries; open positions still fully managed |
| `resume` | resume taking entries |
| `close spy 746c` | flatten just that one position (ticker + strike + c/p) |
| `kill` | cancel all orders, flatten EVERYTHING, halt entries until restart |
| `restart` | reload code+config; refuses if a position is open (`restart force` overrides) |
| `help` | post this command table |

The bot's own posts start with `» ` (so it never parses its own output).
`» 🟢 bot up — mode=live` each morning ≈6:20 means the day started clean.

## What runs, and how

- **The bot**: `run.py`, launched hidden (no window) by `tools\start_bot.vbs`,
  which also starts Discord in the tray first if it isn't running. Appears in
  Task Manager as two `python.exe` (venv launcher + real interpreter).
  Logs: `data\task_run.log` (scheduled/manual launches) and `data\restart.log`
  (instances spawned by the Discord `restart` command). A singleton lock
  (localhost port 47653) stops a second instance — it retries for ~15 s (to
  let a restarting predecessor exit), then gives up. Safe to double-run
  anything.
- **The dashboard**: `tools\dashboard.vbs` starts a hidden Streamlit server
  (own venv `.venv-dash`) and opens http://localhost:8501. Read-only over
  `data\bot.db`. Log: `data\dashboard.log`. `tools\dashboard.bat` is the
  visible-window fallback for debugging.

## Scheduled tasks (Windows Task Scheduler — NOT in the repo)

| task | schedule | action |
|---|---|---|
| `CopyTrade Bot` | weekdays 6:20 AM PT, wakes the PC | runs `tools\start_bot.vbs` |
| `CopyTrade Keeper` | weekdays every 5 min, 6:15 AM–1:15 PM PT | same launcher — revives a crashed bot; no-op while it's alive |

Recreate on a new machine (PowerShell, after fixing paths):

```
$vbs = 'C:\<project path>\tools\start_bot.vbs'
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"' + $vbs + '"')
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 6:20AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "CopyTrade Bot" -Action $action -Trigger $trigger -Settings $settings

schtasks /Create /TN "CopyTrade Keeper" /TR "wscript.exe \"C:\<project path>\tools\start_bot.vbs\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 06:15 /RI 5 /DU 07:00 /F
```

To FULLY stop the bot during market hours: disable `CopyTrade Keeper`
(Task Scheduler → right-click → Disable), then end the `python.exe` pair in
Task Manager. Re-enable the keeper afterward or nothing restarts next morning.

## Machine requirements

- **Windows session**: user must be signed in (lock screen is fine, sign-out
  is not) — the toast listener runs in the interactive session. PC set to
  never sleep on AC power (`powercfg /change standby-timeout-ac 0`).
- **Discord desktop** installed, logged in, "open on startup" enabled
  (tray mode), notifications ON for every alerter's channel (bravo's
  channel AND a multi-author alerts server #🔔alpha-alerts🦾 — channel → Notification
  Settings → All Messages). The notification title must contain the
  profile's `channel_hint` (`sun-alert`, `alpha-alert`) for routing.
  Notification access for apps must be allowed (Windows Settings →
  Notifications) — the bot requests listener access on first run.
- **Python 3.11+** and git (both were installed via winget here).

## Files that are NOT in git — copy these when migrating

| file/dir | contents |
|---|---|
| `config.yaml` | all trading settings + Discord IDs (from config.example.yaml) |
| `.env` | Discord bot token, Webull app key/secret, `LIVE_TRADING_ACK` |
| `conf/` | Webull SDK login/2FA token cache — without it the first run re-prompts |
| `data/bot.db` | the full audit history (trades, decisions, P&L) |

Never commit or paste these anywhere.

## Rebuild steps on a new machine

1. Clone the repo; copy the four private items above into place.
2. `python -m venv .venv` then `.venv\Scripts\pip install -e .[dev]`
   — **protobuf must stay <5** (webull SDK requirement). Never install
   streamlit into this venv; it force-upgrades protobuf and breaks the SDK.
3. `python -m venv .venv-dash` then
   `.venv-dash\Scripts\pip install streamlit pandas pyyaml` (dashboard only).
4. Edit the hardcoded `root =` path in `tools\start_bot.vbs` and
   `tools\dashboard.vbs`; recreate both scheduled tasks (above).
5. `.venv\Scripts\python -m pytest` — expect all green.
6. Webull side: OpenAPI keys tied to the account, and the **OPRA real-time
   non-display ($5/mo) + Nasdaq Basic** subscriptions must be active in the
   OpenAPI Advanced Quotes center (the app-side free OPRA perk does NOT
   cover OpenAPI). First run on a new machine will do Webull 2FA once.
7. Start paper first (`mode: paper`), confirm a test alert round-trips, then
   flip live (`mode: live` + `LIVE_TRADING_ACK=yes`) deliberately.
