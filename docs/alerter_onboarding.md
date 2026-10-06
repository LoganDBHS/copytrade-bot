# Alerter onboarding — mining a chat history into a parser contract

Adding an alerter is a data exercise, not a coding one. You export their
channel history, mine it with `tools/mine_history.py`, read every disagreement
between what the parser found and what you can see in the export, and only
then write the config profile. No parser code changes: everything
alerter-specific lives in `alerters:` in `config.yaml` and in
`tests/fixtures/<name>/`.

This document is the method, plus the format taxonomy that shaped the parser.

> **Fixtures are synthetic.** Everything in `tests/fixtures/` is generated
> from `tests/fixtures/_corpus.py` — invented messages written to cover each
> grammar feature below. No real channel content is committed to this repo.
> See [Keeping real exports out of git](#keeping-real-exports-out-of-git).

## The flow

1. Export the channel (DiscordChatExporter HTML, or a plain-text/copy-paste
   dump — `ingestion/replay.py` reads all three shapes).
2. Drop it in `tests/fixtures/<name>/`. **Local only** — `data/` and any real
   export are gitignored.
3. Run `python tools/mine_history.py <name> --write`. It prints entries,
   same-day repeats, ambiguous messages, and a noise count, and writes
   `entries.csv`.
4. Read every ambiguous message by hand. Each one is either a parser gap or a
   correct fail-closed call; the split tells you whether to adjust the profile
   or leave it alone.
5. Add the profile to `config.yaml`: `channel_hint`, `alert_prefix`,
   `ticker_aliases`, `known_tags`, `author_hints`.
6. Freeze `entries.csv` as a golden test (see `tests/test_golden_bravo.py`).

## Why token-role parsing, not line regexes

The spine of an alert — `{ticker, strike, side}` — always appears, but the
word order does not hold still. A single alerter will write all six:

| Order | Example |
|---|---|
| ticker strike side | `iwm 289 put 0dte RISKY` |
| ticker side strike | `iwm put 296 0dte risky` |
| strike ticker side | `280 appl call 0dte RISKY` |
| strike side ticker | `245 call amazon 7/1` |
| strike side dte ticker | `387.5 call 0dte msft` |
| ticker strike dte side | `qqq 720 0dte call GAMBLE` |

So the parser tokenizes the first paragraph block and assigns each token a
role, requiring exactly one ticker, one side, and one strike in any order.
Two tickers or two strike-sized numbers is ambiguous, and ambiguous means no
trade.

### Variance the taxonomy has to absorb

- **Company names as tickers** — `tesla`, `apple`, `amazon`, `nvidia` — plus
  typo forms: `telsa`, `aaple`, `appl`, `nvdia`, `odte`. Profile aliases cover
  the known ones; fuzzy edit-distance matching against the whitelist (never
  against free text) catches the rest.
- **Tag typos** — `riksy`, `riskyy`, `sclap`, `re ntry`. Recorded in the
  alerter's own spelling so per-tag statistics stay honest.
- **Decoration** — `$` prefixes on strikes, markdown `##`, vertical-bar
  lookalikes (`〡`, `│`, `｜`) used as field separators and often glued to the
  neighboring token.
- **Risk scales that look like dates** — `8/10 risk`, `level 9/10`. A slash
  number adjacent to `risk`/`level` is a scale, not an expiry.
- **Leading-dot prices** — `.69` must normalize to `0.69`, or it gets stripped
  into a fake strike.
- **Fill ranges** — `1.00 - 1.10 FILL`. Take the lower bound; it is the
  conservative read.
- **Stop clauses inside the entry message** — `stop .45`, `0.6 STOP`.
  Everything from the stop marker onward is exit talk, and a bare number
  immediately before the marker belongs to it.
- **Parenthetical asides** — their numbers look exactly like prices and must
  be stripped before tokenizing.
- **Profit targets inside entries** — `Only looking for 20-30%`. This is why
  the `%` guard is soft: a `%` message can still be an entry, but only a fully
  specified one (spine + expiry + price).

### Status guards that held up

Never an entry, regardless of what else is in the message: a `->` arrow, the
words `hit` or `held`, or the phrases `looking at`, `call it a day`,
`call out`, `recap`. `STO` (sell-to-open) is not a follow-able entry either.

### Expiry and price are frequently absent

Across the histories mined so far, a large minority of entries state no
expiry, and most state no price at all. Both gaps are load-bearing policy,
not edge cases:

- **No expiry** → assume today, then require the contract to actually exist in
  the broker's chain. SPY/IWM/QQQ expire daily; single names generally only on
  Fridays, so a midweek no-expiry MSFT alert fails the chain check and is
  skipped with a notification.
- **No price** → the `priceless_alerts` policy decides (`enter_at_ask` by
  default). Prices sometimes arrive in a *later* message; the bot never waits
  for one.

Final authority is always the option chain: whatever parses must exist as a
real contract today at that broker, or no order goes out.

## Multi-author channels

Most alert channels carry more than one voice: the alerter, server staff, and
members echoing or guessing plays. `author_hints` scopes mining to the
alerter's own handles, so nobody else's message can enter the record.

Two cases worth knowing:

- **An alerter who renames mid-history** keeps *both* handles in
  `author_hints`, so one continuous record spans the rename.
- **An alerter's personal/lookalike account is deliberately left out.** Claim
  mining in particular must be scoped this way — percentage claims posted from
  a side account would otherwise be credited to the record.

## Claim mining and its known bias

`parsing/claims.py` attaches the alerter's own peak-marked percentages to each
play, so the dashboard can report *claimed vs. realized*. A claim counts for a
play only when the message names that play's ticker (in any alias or typo
form) or its strike — unnamed hype numbers are ignored.

The remaining bias is deliberate: when a claim names only the ticker and the
alerter has two open strikes on it, both get credited. Over-crediting the
alerter is the conservative direction for an audit whose whole purpose is to
compare their claims against what the bot actually realizes.

**Read any claimed figure as a ceiling, not a return.** Peak marking scores a
win at the highest print rather than at an exit a follower could systematically
take; plays labeled "not counting" are pre-excused from the record before they
resolve; and summing percentages across trades produces a number that means
nothing for a real account. What a copier with realistic fills and systematic
exits actually earns appears in no alerter's history — measuring it is exactly
what Phase 3 paper mode is for.

## Keeping real exports out of git

A real channel export is third-party personal data: real usernames, real chat,
and in a paid server, material the operator has not licensed for
redistribution. None of it belongs in a public repo.

- `data/` and `config.yaml` are gitignored; keep real exports there while you
  mine them.
- Committed fixtures are synthetic. To extend coverage, add a message to
  `tests/fixtures/_corpus.py` with a `note` saying which grammar feature it
  pins down, then regenerate:

      python tests/fixtures/_generate.py

  That rewrites both export files and re-mines the golden CSVs. Update the
  counts in the golden tests to match, and confirm each change is one you
  intended.
