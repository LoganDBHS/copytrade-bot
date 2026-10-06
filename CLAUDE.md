# CLAUDE.md — Discord alert copy-trading bot (0DTE options)

A bot that reads options trade alerts from Discord channels (captured via
Windows toast notifications — never Discord's API with a user token), parses
them, applies risk checks, places orders through a broker adapter, and
manages every exit automatically. Owner: Logan (call him that). Windows 11
desktop, Python 3.11+. Timezone everywhere: America/Los_Angeles. Market
hours 06:30–13:00 local. Contracts are 0DTE (max 1DTE per config).

## FIRST RUN — SETUP MODE
If this folder has no pyproject.toml yet, you are in setup mode. Logan
downloaded a flat pile of files here; your job is to organize everything
and walk him through setup conversationally — one short step at a time,
doing all mechanical work yourself rather than giving him instructions:
1. Inventory the files. Expected: CLAUDE.md, config.example.yaml,
   env.example, alerts_full.txt, sample_pasted.txt, entries.csv,
   alerter_onboarding.md. If something is missing, say so and continue; only
   CLAUDE.md is essential.
2. Build the structure yourself: src/ modules per Architecture below,
   tests/fixtures/bravo/, docs/, data/. Move the three bravo data
   files into tests/fixtures/bravo/, alerter_onboarding.md into docs/,
   rename env.example → .env.example. git init with a .gitignore
   covering config.yaml, .env, data/, __pycache__/, .venv/.
3. Copy config.example.yaml → config.yaml and .env.example → .env.
4. Interview Logan for values he can provide now, one at a time, with
   exact click-paths for each: Discord control-server guild/channel/user
   IDs (explain enabling Developer Mode → right-click → Copy ID), Discord
   bot token (discord.com/developers → New Application → Bot → Reset
   Token; also walk him through inviting the bot to his private server),
   Webull app key/secret (Webull → OpenAPI Management; if approval is
   pending, leave blank and note what stays blocked). Never echo secrets
   back in chat.
5. In one short paragraph, remind him: mode defaults to paper, `kill` in
   the control channel flattens everything, `status` shows live P&L.
6. Then propose the Phase 1 plan and proceed per Build phases below.

## Design principle: alerter profiles (multi-source ready)
Nothing about a specific alerter is hardcoded — Logan will add more
alerters later. Everything alerter-specific (channel hint, alert prefix,
ticker aliases, tag vocabulary, fixture data) lives under `alerters:` in
config.yaml and tests/fixtures/<name>/. Every audit row, alert, decision,
and position carries alerter_id. v1 ships one enabled profile: bravo.

**Adding a new alerter** (when Logan brings one): he drops their exported
chat history (DiscordChatExporter HTML or plain text) in
tests/fixtures/<name>/; you mine it exactly like the bravo flow — parse
the history, discover aliases/tags/format quirks, generate an
entries_expected.csv, show Logan the parse results for review, add the
config profile. New alerter = new profile + fixtures, zero code changes.

## Hard invariants — never violate, never make these toggleable-off
1. `mode` defaults to `paper`. Live requires BOTH `mode: live` in
   config.yaml AND `LIVE_TRADING_ACK=yes` in .env.
2. Fail closed. Any parse ambiguity, missing/stale quote, failed risk
   check, or contract absent from the broker's option chain = NO order +
   a post to the Discord control channel. Never guess.
3. Options orders are LIMIT only, never market (Webull rejects MARKET on
   options anyway). Entries: limit at min(ask, alert_price ×
   price_band_max). Exits: marketable limit at mid × (1 − exit_cross_pct),
   repriced every reprice_seconds until filled.
4. Audit-first: every raw message, parse, decision, quote snapshot, order,
   and fill is written to SQLite (data/bot.db) as it happens.
5. Stops are bot-managed soft stops on the quote MID each poll — no
   resting stops except one "disaster" stop-limit parked at the broker at
   −disaster_stop_pct as a parachute (if the adapter supports it; if not,
   say so at entry in the control channel).
6. Hard flatten at flatten_time_local, no exceptions.
7. Respect max_entries_per_day. One open play per contract. Never
   re-enter or trade a same-day re-alert of the same contract (log it —
   the alerter tells followers to manage re-entries themselves).
8. `kill` in the control channel: cancel all orders, flatten everything
   with marketable limits, halt new entries until restart.
9. Secrets live in .env only. Never log or commit them.

## Architecture — one module per directory, adapters everywhere
- `ingestion/` — NotificationSource interface. WindowsToastSource: winsdk
  UserNotificationListener, request access on startup, poll ~500 ms,
  filter app contains "Discord", route to an alerter profile by
  channel_hint substring match on the notification title, dedupe by
  notification id. ReplaySource reads fixture files for tests.
- `parsing/` — TOKEN-ROLE parser, not line regexes (the alerter uses six
  word orders). Take the first paragraph block, strip markdown/#/|/(),
  tokenize, assign roles: ticker (whitelist + profile aliases + fuzzy
  edit-distance ≤2 against whitelist only); side (call/calls/put/puts, or
  trailing c/p in the formal "BTO $SCHW 114c 08/21 @0.32" grammar);
  strike (number ≥50 in the ticker's plausible range); expiry
  (0dte/odte/1dte or M/D date — but a slash number adjacent to
  "risk"/"level" is a risk scale, not a date); price (decimal < 20).
  Require exactly one ticker, one side, one strike, ANY order; two
  tickers or two strike-sized numbers = ambiguous = fail closed. Status
  guards: any "%", "hit", "held", "->" is never an entry; "looking at",
  "call it a day", "call out" is commentary. Gate: 0 false trades over the
  whole mined history.
- `risk/` — gate(alert, quote, account) -> Decision, in order: staleness
  ≤ staleness_seconds; expiry resolution (missing expiry assumes TODAY,
  dated allowed ≤ max_dte, then the contract MUST exist in today's chain —
  SPY/IWM/QQQ expire daily, single names only Fridays, so a midweek
  no-expiry MSFT alert fails the chain check and is skipped with a
  notification); spread ≤ max_spread_pct_of_mid; if alert_price present:
  ask ≤ alert_price × price_band_max (below alert always fine); if absent
  (the majority of entries, in every history mined so far):
  priceless_alerts policy (enter_at_ask);
  buying power covers contracts_per_play, else single_contract_fallback
  buys 1 as a tp_only play; daily entry count; duplicate check; enough
  minutes before flatten.
- `execution/` — BrokerAdapter interface: get_option_quote, get_chain,
  buy_to_open, sell_to_close, place_disaster_stop, cancel, positions,
  buying_power. v1: WebullAdapter only (official
  webull-openapi-python-sdk; paperTrade for mode=paper). Quirks: no
  MARKET or TRAILING_STOP_LOSS on options; option SELLs are DAY
  time-in-force only; option market-data REST ≈60 req/min. Idempotent
  client_order_ids (uuid per intent). Keep the interface clean for future
  Alpaca/Schwab adapters.
- `manage/` — one PositionManager asyncio task per play, polling quotes.
  OPEN → (mid ≤ stop) EXIT; (mid ≥ fill × (1+trim_trigger_pct)) TRIM:
  sell 1, stop = breakeven → RUNNER: stop = max(breakeven, peak_mid ×
  (1 − trail_pct)), ratchets up only. tp_only plays sell all at fill ×
  (1 + single_contract_take_profit_pct). flatten_time_local overrides
  every state. Poll interval = max(poll_seconds, open_positions × 60 /
  broker_rate_limit_per_min); optionally stream the underlying stock via
  MQTT as a cheap early-warning to trigger an option re-poll.
- `control/` — discord.py bot in Logan's OWN private server
  (ToS-compliant). Posts every event: entries, fills, trims, stop moves,
  exits, daily summary, heartbeat. Commands (authorized_user_id only):
  kill, status, pause, resume, close <ticker> <strike><c/p>, alerters,
  follow/unfollow <alerter> (unfollow = new entries skipped but alerts
  still parsed+audited, open plays managed to exit; persisted in the
  audit db so restarts/Keeper can't undo it), restart
  (refuses with open positions unless `restart force`; relaunches via
  tools/start_bot.vbs). Watchdog: heartbeat stall > 3 × heartbeat_seconds
  → alert post. External "CopyTrade Keeper" scheduled task re-runs the
  launcher every 5 min during market hours — crash auto-recovery; the
  singleton port lock makes it a no-op while the bot is alive.
- `audit/` — SQLite tables: raw_messages, alerts, decisions, quotes,
  orders, fills, positions, daily_summary — all with alerter_id and
  created_at UTC.
- `dashboard/` — Streamlit reading data/bot.db: open positions with live
  stops, equity curve, per-alerter and per-risk-tag stats, claimed vs
  realized per alert. Phase 4. Seed data: tests/fixtures/bravo/
  entries.csv and docs/alerter_onboarding.md.

## Bravo profile quirks (all present in tests/fixtures/bravo/)
Six word orders for {ticker, strike, side}; company names as tickers
(tesla, apple, amazon, nvidia) and typo forms (telsa, aaple, appl, nvdia,
riksy, odte); "$" strikes; "##" prefixes; risk scales that look like
dates ("8/10 risk"). Most entries state no price at all; prices may
arrive in a LATER message — never wait. "cons" = contracts; %-updates,
"bang", walls, recaps, images are noise (log, never trade). Tags: risky,
very risky, super risky, gamble, lotto, fomo, scalp, go light, go in
with profits, not counting, re entry, last play — trade all tags v1
(except skipped re-alerts), always store them for per-tag analysis.

## Alpha profile quirks (all present in tests/fixtures/alpha/)
A multi-author channel — author_hints scope the mining to the alerter.
Canonical grammar "TICKER 〡 STRIKE Call 〡 MM/DD 〡 PRICE Fill @alert ...":
〡-separators (sometimes ASCII |), explicit MM/DD expiry (occasionally
typo'd or 1DTE), price nearly always present (bare ".69" leading-dot
decimals, fill ranges "1.00 - 1.10" → take the lower), stop clauses in the
entry message ("stop .45", "0.6 STOP" — truncated out), profit-target %s
inside entries (so the % noise-guard is soft: a fully-specified
spine+expiry+price message can still be an entry), everything after a
mid-message "@alert" is commentary, parentheticals are asides. Tags:
lotto, rentry, swing, hedge, high risk(y), size small/slight, ER plays.
Trades SPX/NOW/NDX/AVGO too (NOW/NDX are uppercase-only tickers); swings
and LEAPs parse fine but the max_dte gate skips them by design. Recaps
("Trade Recap:") are commentary-guarded.

## Build phases, in order, each with a gate
1. Parser + ReplaySource + audit db. Gate: pytest green; the parser
   reproduces both golden records byte for byte — tests/fixtures/bravo/
   alerts_full.txt -> entries.csv and tests/fixtures/alpha/export.html ->
   entries.csv — with 0 false entries, and the copy-paste sample passes.
   Fixtures are synthetic; regenerate with
   `python tests/fixtures/_generate.py`.
2. Paper end-to-end against Logan's own test Discord channel (identical
   notification path). Gate: alert typed in the test channel → paper
   fill + control post + audit rows in under ~5 seconds.
3. Shadow the real channel in paper 2+ weeks. Gate: expectancy report
   (the bot's realized results vs the alerter's own claimed numbers; treat
   claims as peak-marked ceilings, see docs/alerter_onboarding.md).
4. Streamlit dashboard; enable live only if Phase 3 is positive.

## Conventions
asyncio throughout; pytest; ruff. config.yaml and .env are gitignored.
Keep the PC awake during market hours; on startup mid-session after a
gap, warn in the control channel.
