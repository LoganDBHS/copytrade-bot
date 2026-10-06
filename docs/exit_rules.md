# Exit Rulebook

Every way a position leaves the book — stop losses, the trim, the trailing
runner, the disaster parachute, the hard flatten, and the manual overrides.
Numbers shown are the current values in `config.yaml` (`exits:` block); the
logic lives in `src/manage/position.py`. Change values in config, not code.

**Key numbers at a glance:** initial stop **−25%** · trim trigger **+25%**
(sell 1, stop → breakeven) · runner trail **15%** off peak · disaster stop
**−50%** at the broker · hard flatten **12:00 PT**.

## How every exit actually fills

The bot never sends a market order on options (Webull rejects them anyway).
Every exit — stop, trim, take-profit, flatten, kill — is the same mechanism:
a **marketable limit** priced to cross the spread:

```
limit = mid × (1 − exit_cross_pct)        # mid × 0.95
```

If it doesn't fill, the bot cancels, re-quotes, and re-prices every
`reprice_seconds` (2 s) until the position is flat. Option sells are DAY
time-in-force only on Webull — fine, since nothing is ever left working
overnight.

All stop decisions are made on the **quote mid** (not bid or last), checked
every poll. The poll interval is 1 s per open play, stretched automatically to
stay under Webull's ~60 req/min option-quote limit. If a quote fetch fails
mid-flight, the bot holds the current stop, posts a warning to the control
channel, and retries next poll — it never exits on a missing quote.

## Check order, every poll

Earlier rules always win — a stop that fires at 11:59 doesn't wait to see if
the trim would have hit.

1. **Hard flatten.** At or after 12:00 PT, sell everything, whatever the
   state or P&L.
2. **Update the peak.** `peak_mid` = highest mid ever seen; it only goes up.
3. **Ratchet the runner stop** (RUNNER state only), then **check the stop**:
   mid ≤ stop → sell everything remaining.
4. **Check the trim** (OPEN state only): mid ≥ fill × 1.25 → sell 1, promote
   to RUNNER.

## Lifecycle of a normal play

A normal play is 2 contracts (`contracts_per_play`). States move one
direction only: **OPEN → RUNNER → CLOSED**.

### Initial stop — soft, −25%

```
stop = fill × (1 − initial_stop_pct)      # fill × 0.75
```

Set the moment the entry fills. It's a *soft* stop: the bot watches the mid
each poll and fires the sell itself. Nothing rests at the broker at this
level, so a wide or flickering bid can't shake the position out — only the
mid crossing the line does.

### Trim — sell 1 at +25%, stop to breakeven

```
trigger: mid ≥ fill × (1 + trim_trigger_pct)   # fill × 1.25
```

Sell exactly one contract, banking roughly +25% on it. The stop on what
remains immediately jumps to the entry fill price — from this point the play
can no longer lose money (before commissions/slippage). State becomes RUNNER.

### Runner trail — 15% below peak, ratchets up only

```
stop = max(previous stop, fill, peak_mid × (1 − trail_pct))
```

Recomputed every poll. The three-way max means the stop can never drop:
never below breakeven, never below where it already ratcheted, and it climbs
as the peak climbs. There is no upper take-profit on a runner — the trail is
the exit.

### Disaster stop — the −50% parachute at the broker

```
stop  = fill × (1 − disaster_stop_pct)    # fill × 0.50
limit = stop × (1 − exit_cross_pct)
```

The one resting order: a stop-limit parked at Webull at entry. It exists for
the case where the bot itself dies (PC crash, network drop) so a position is
never truly naked; in normal operation the soft −25% stop fires long before
it. The bot cancels it before any sell it makes itself, and re-arms it on the
remaining contract after a trim (still keyed to the original fill). If the
broker can't hold one, the bot says so in the control channel at entry — then
the soft stop is the only stop.

## TP-only plays (the 1-contract fallback)

When buying power covers only one contract (`single_contract_fallback`), the
play enters as **TP_ONLY** — nothing to trim, so the bracket is fixed:

- **Take profit:** sell all at `fill × (1 + single_contract_take_profit_pct)`
  = fill × 1.25. First to trigger wins between this and the stop.
- **Stop:** same −25% soft stop, never moves, no trailing. Disaster parachute
  at −50% still armed. Hard flatten still overrides everything.

## Time rules

- **Hard flatten at 12:00 PT** (`flatten_time_local`), one hour before the
  13:00 close. Every open position is sold via the standard marketable-limit
  loop regardless of state, P&L, or how good the runner looks. Hard
  invariant — there is no toggle to hold through it.
- **Entry cutoff:** no new entries within 45 minutes of flatten
  (`min_minutes_before_flatten`), so nothing opens at 11:20 just to be
  force-closed 40 minutes later.

## Manual and safety exits

| Trigger | What happens |
| --- | --- |
| `kill` in the control channel | Cancel every working order, flatten every position with marketable limits, halt all new entries until restart. |
| `close SPY 640c` | Manually close one play through the same sell loop. A lock makes this safe even if the poll loop fires a stop at the same instant — the second seller becomes a no-op. |
| Quote failure mid-flight | Hold the current stop, post a warning, retry next poll. Fail closed on entries, fail *patient* on exits. |
| Bot restart with open positions | Recovered plays come back with persisted state, stop, and peak intact — a ratcheted runner stop is never reset back down to −25%. Every stop move is written to `data/bot.db` as it happens. |
| `unfollow <alerter>` | Blocks *new* entries only. Open plays from that alerter keep being managed to their normal exit. |

## Worked example — 2 contracts filled at $1.00

| Mid | State | Stop | Action |
| --- | --- | --- | --- |
| 1.00 | OPEN | 0.75 | Entry fills. Soft stop at −25%; disaster stop-limit parked at 0.50. |
| 1.25 | OPEN → RUNNER | 0.75 → 1.00 | Trim: sell 1 (+$25 banked). Stop jumps to breakeven — play is now risk-free. |
| 1.60 | RUNNER | 1.36 | Peak = 1.60, stop ratchets to 1.60 × 0.85. It will not come back down. |
| 1.36 | RUNNER → CLOSED | 1.36 | Mid touches the trail. Sell the runner (≈ +$36). Total ≈ +$61 on $200 risked. |

Same play, bad day: mid never reaches 1.25 and slides to 0.75 → both
contracts sold at the soft stop for ≈ −$50. Worst realistic case with the bot
alive is the −25% stop; −50% only matters if the bot itself is down.

## Where each number lives

All under `exits:` in `config.yaml` (last two are entry-side keys that shape
exits):

| Key | Value | Governs |
| --- | --- | --- |
| `initial_stop_pct` | 0.25 | Soft stop at fill × 0.75 |
| `trim_trigger_pct` | 0.25 | Sell 1 at fill × 1.25, stop → breakeven |
| `trail_pct` | 0.15 | Runner stop = peak × 0.85, ratchet-only |
| `disaster_stop_pct` | 0.50 | Resting stop-limit parachute at the broker |
| `single_contract_take_profit_pct` | 0.25 | TP_ONLY plays sell all at fill × 1.25 |
| `flatten_time_local` | 12:00 | Hard flatten (market closes 13:00 PT) |
| `exit_cross_pct` | 0.05 | Sell limit = mid × 0.95 (crosses the spread) |
| `reprice_seconds` | 2 | Cancel/re-price cadence until filled |
| `min_minutes_before_flatten` | 45 | Entry cutoff before flatten (entry-side) |
| `single_contract_fallback` | true | Creates TP_ONLY plays (entry-side) |

**Changing a value:** edit `config.yaml` and restart the bot (`restart`
refuses with open positions unless forced — better to wait until flat).
Positions already open keep the stop they've earned; new values apply from
the next entry.
