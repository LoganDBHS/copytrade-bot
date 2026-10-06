# CopyTrade Bot

A personal copy-trading bot for 0DTE options. It reads trade alerts that
arrive as Discord notifications, parses them with a token-role parser,
runs every candidate through a fail-closed risk gate, places limit orders
through a broker adapter (Webull OpenAPI), and manages every exit
automatically — stops, trims, trailing runners, and a hard end-of-day
flatten. Runs on a Windows 11 desktop, Python 3.11+, Pacific time.

> **Test fixtures are synthetic.** Everything under `tests/fixtures/` is
> generated from an invented corpus (`tests/fixtures/_corpus.py`) that covers
> each grammar feature the parser handles — no real channel content is
> committed. Real exports, broker keys, the bot token, the SDK token cache,
> and the audit database stay out of git entirely — see
> [What stays out of git](#what-stays-out-of-git).

## How it works

```
Discord toast notification
        │  (Windows UserNotificationListener — never Discord's API
        │   with a user token; ToS-compliant)
        ▼
ingestion/   route to an alerter profile, dedupe
        ▼
parsing/     token-role parser: ticker + side + strike + expiry + price,
             any word order; ambiguity = no trade
        ▼
risk/        gate: staleness, chain existence, spread, price band,
             buying power, daily caps, duplicates, time cutoff
        ▼
execution/   BrokerAdapter (Webull OpenAPI; paper or live) — LIMIT only
        ▼
manage/      one PositionManager per play: soft stops on the mid,
             trim at +25%, ratcheting runner trail, disaster stop-limit
             parked at the broker, hard flatten at 12:00 PT
        │
        ├── audit/     every message, parse, decision, quote, order, and
        │              fill written to SQLite (data/bot.db) as it happens
        ├── control/   Discord bot in a private server: posts every event,
        │              takes commands (kill, status, pause, close, …)
        └── dashboard/ read-only Streamlit over the audit db
```

Everything alerter-specific (channel hint, aliases, tag vocabulary,
fixtures) lives in a profile under `alerters:` in `config.yaml` plus
`tests/fixtures/<name>/` — adding an alerter requires no code changes.
Two profiles ship: **bravo** and **alpha**.

## Safety invariants

These are hard rules in the code, not settings:

- `mode` defaults to `paper`. Live trading requires **both** `mode: live`
  in `config.yaml` **and** `LIVE_TRADING_ACK=yes` in `.env`.
- **Fail closed.** Parse ambiguity, stale quote, failed risk check, or a
  contract missing from the chain → no order, plus a post to the control
  channel. The bot never guesses.
- Options orders are **limit only**, entries and exits both.
- Hard flatten at `flatten_time_local` (12:00 PT) — no exceptions, no
  toggle.
- One open play per contract; same-day re-alerts are logged, never traded.
- `kill` in the control channel cancels everything, flattens everything,
  and halts entries until restart.

The full exit logic — stops, trim, runner trail, disaster parachute,
worked examples — is in [docs/exit_rules.md](docs/exit_rules.md).

## Repository map

| Path | What it is |
|---|---|
| `run.py` | Entry point; singleton port lock; wires everything together |
| `src/ingestion/` | Toast-notification listener + replay source for tests |
| `src/parsing/` | Token-role parser, history mining, claim extraction |
| `src/risk/` | The entry gate: `gate(alert, quote, account) → Decision` |
| `src/execution/` | `BrokerAdapter` interface; Webull, paper, and fake adapters |
| `src/manage/` | Per-play position manager (stops, trim, runner, flatten) |
| `src/control/` | Discord control bot (commands + event posts) |
| `src/audit/` | SQLite audit schema and writers |
| `src/dashboard/` | Streamlit dashboard (separate venv) |
| `tools/` | Launchers (`start_bot.vbs`, `dashboard.vbs`), probes, history miner |
| `tests/` | pytest suite + golden fixtures per alerter |
| `docs/` | Operations, exit rules, alerter record audit |

## Setup

Full machine-level detail (scheduled tasks, Discord notification
settings, Webull subscriptions, migration steps) is in
[docs/operations.md](docs/operations.md). Short version:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .[dev]          # protobuf must stay <5 (webull SDK)
python -m venv .venv-dash
.venv-dash\Scripts\pip install streamlit pandas pyyaml   # dashboard only

copy config.example.yaml config.yaml          # fill in Discord IDs + settings
copy .env.example .env                        # fill in tokens/keys

.venv\Scripts\python -m pytest                # expect all green
.venv\Scripts\python run.py                   # starts in paper mode
```

Never install streamlit into the main venv — it force-upgrades protobuf
and breaks the Webull SDK, which is why the dashboard has its own venv.

## What stays out of git

The `.gitignore` covers these; they must never be committed or pasted
anywhere:

| Path | Contents |
|---|---|
| `.env` | Discord bot token, Webull app key/secret, live-trading ack |
| `config.yaml` | Trading settings + private Discord IDs |
| `conf/` | Webull SDK login/2FA token cache |
| `data/` | Audit database (full trade history) and runtime logs |
| `webull_*.log*` | SDK logs — they include the app key in request headers |

When migrating machines, copy those into place by hand; see
[docs/operations.md](docs/operations.md#files-that-are-not-in-git--copy-these-when-migrating).

## Documentation

- [docs/operations.md](docs/operations.md) — everything outside the code:
  control-channel commands, scheduled tasks, machine requirements,
  rebuild steps.
- [docs/exit_rules.md](docs/exit_rules.md) — every way a position leaves
  the book, with the config keys behind each number.
- [docs/alerter_onboarding.md](docs/alerter_onboarding.md) — how a chat
  history is mined into a parser contract: the format taxonomy, the status
  guards, claim mining and its bias, and how fixtures are generated.
- [CLAUDE.md](CLAUDE.md) — the full design brief: architecture,
  invariants, alerter profile quirks, build phases.

## Status

Built through Phase 4 (dashboard); live since 2026-08-18. Position
recovery on restart is in place; a Windows scheduled task ("CopyTrade
Keeper") revives the bot if it crashes during market hours.

## Disclaimer

Personal project for trading the owner's own account. 0DTE options are
extremely risky; nothing here is financial advice, and the code comes
with no warranty of any kind.
