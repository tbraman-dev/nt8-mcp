# Analysis — `nt_analyze`

Source: `server/nt8_mcp/analysis.py` (pure functions, no AddOn call) +
`server/nt8_mcp/tools_analysis.py` (the tool + the run-file read). Tests:
`server/tests/test_analysis.py`.

**This module adds no HTTP endpoint and no AddOn file.** It is plain Python over a trade list —
and, when there is one, the equity array a saved run carries — so there is no NinjaTrader surface,
no account, no order, no chart. Every breakdown function tolerates a trade missing the field it
needs (no `mae`, no `entryTime`, …) by leaving that trade out of the one breakdown that needed it,
never by raising.

```python
nt_analyze(run_id="", trades=None)
```

Pass **one** of:

- `run_id` — a saved run's id. Read from `<NinjaTrader user data dir>\nt8mcp\runs\<run_id>.json`
  (the run registry another module writes) through a loader that only needs the file's `trades`,
  `equity` and `summary` keys — nothing else in that file is this module's business. `run_id` is
  reduced to its basename before the path is built, so it can never walk out of the runs folder.
  `run_id` wins if both `run_id` and `trades` are given.
- `trades` — a status document v1 `trades[]` list (`addon/NOTES.md` "Status document v1") handed in
  directly, e.g. from `nt_backtest`'s or `nt_optimize`'s own result. No `equity` array is available
  this way, so every equity-based breakdown derives one from `trades[].pnl` instead.

Neither given: `{"error": "pass run_id or trades."}`. An unreadable/missing run, or a `trades` value
that isn't a list, is `{"error": "..."}` too — never an empty set of breakdowns standing in for a
result that could not be computed.

## Result

```json
{"runId": "b17", "trades": 76,
 "byMonth": {"2026-09": {"trades": 40, "netProfit": 1200.0, "winners": 22, "losers": 18}},
 "byWeekday": {"Monday": {"trades": 15, "netProfit": -80.0, "winners": 6, "losers": 9}},
 "byEntryHour": {"9": {"trades": 30, "netProfit": 900.0, "winners": 18, "losers": 12}},
 "bySide": {"Long": {"trades": 50, "netProfit": 400.0, "winners": 28, "losers": 22},
            "Short": {"trades": 26, "netProfit": -612.5, "winners": 10, "losers": 16}},
 "maeMfe": {"trades": 76, "avgMae": 366.6, "maxMae": 2100.0, "avgMfe": 548.7, "maxMfe": 3050.0},
 "streaks": {"longestWinStreak": 4, "longestLossStreak": 5, "currentStreak": 2, "currentStreakKind": "loss"},
 "drawdown": {"amount": -4425.0, "start": "2026-09-12T14:30:00", "trough": "2026-09-14T09:15:00",
              "recovered": "2026-09-16T11:00:00", "durationSeconds": 331200.0},
 "timeUnderWater": {"totalSeconds": 950400.0, "longestSeconds": 331200.0},
 "longestFlatPeriod": {"start": "2026-09-12T14:30:00", "end": "2026-09-16T11:00:00", "seconds": 331200.0}}
```

Null rules and shapes:

| Key | Type | Null? | Meaning |
|---|---|---|---|
| `runId` | string | null when called with `trades` directly | the `run_id` argument, echoed back |
| `trades` | int | never | `len(trade_list)` |
| `byMonth` | object | never (`{}` when nothing could be bucketed) | keyed `"YYYY-MM"` by **exit** time — never entry time, which misattributes a trade spanning a month boundary |
| `byWeekday` | object | never (`{}`) | keyed by exit weekday name (`"Monday"`…`"Sunday"`), only the days that actually have a trade |
| `byEntryHour` | object | never (`{}`) | keyed by entry hour `0`..`23`, NT8-local (no timezone conversion, same convention as the status document) |
| `bySide` | object | never (`{}`) | keyed `"Long"` / `"Short"`; a trade with no entry (`side == ""`) is left out |
| `maeMfe` | object \| null | **null when no trade in the list carries `mae`/`mfe`** | avg/max, in points (the status document's own unit) |
| `streaks` | object | never | `longestWinStreak`, `longestLossStreak` (by the list's own order — NinjaTrader's chronological `n`), `currentStreak`/`currentStreakKind` (the streak still open at the end, `currentStreakKind` null if it ended flat or the list is empty) |
| `drawdown` | object \| null | **null with fewer than 2 usable equity points, or if the curve never dips below its own start** | the single WORST drawdown period — see below |
| `timeUnderWater` | object \| null | same condition as `drawdown` | `totalSeconds` (all time spent below the running peak), `longestSeconds` (the longest one stretch of it) |
| `longestFlatPeriod` | object \| null | same condition as `drawdown` | the longest stretch between two consecutive NEW equity highs — see below |

Every `{trades, netProfit, winners, losers}` bucket (`byMonth`/`byWeekday`/`byEntryHour`/`bySide`)
counts only the trades whose `pnl` is present for `netProfit`/`winners`/`losers`; `trades` counts
every trade placed in that bucket regardless.

### `drawdown`

The equity curve used is the caller's own `equity` array (`[{"time", "cumulativeNetProfit"}]`, by
exit time) when there is one, else one derived from `trades[].pnl` in the list's own
order. Only the single worst drawdown is reported (by `amount`, most negative):

| Key | Meaning |
|---|---|
| `amount` | `<= 0`; the drop from the prior peak to the trough |
| `start` | the prior peak's own time |
| `trough` | the lowest point's time |
| `recovered` | the first time the curve is back at or above that peak, or `null` if the series ends still underwater |
| `durationSeconds` | `recovered - start`, or `null` when `recovered` is `null` |

### `longestFlatPeriod`

Distinct from `timeUnderWater`: a curve that stalls exactly at its old peak — no new drawdown, just
no new high — still counts as flat here, which `timeUnderWater` would not catch. `start`/`end` are
the two equity-high times bounding the longest such gap (or the last high and the series' final
point, if nothing tops it afterward); `seconds` is the gap.

## Not computed here

Anything NinjaTrader's own `summary` already gives (`netProfit`, `profitFactor`, `maxConsecWinners`,
…) — see `docs/api/backtest.md`'s Status document v1 table, or call `nt_report` for a PDF and the
overall stats. `nt_analyze` is the trade-list breakdowns `nt_report` does not do, not a replacement
for it.
