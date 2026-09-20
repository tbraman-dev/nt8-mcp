# Reconciliation — `nt_reconcile`

Module: `server/nt8_mcp/reconcile.py` (pure functions) + `server/nt8_mcp/tools_reconcile.py` (the
one MCP tool, `nt_reconcile`). No AddOn endpoint, no HTTP call of its own, no write of any kind.
The tool reuses `nt_backtest_status`, `nt_run` and `nt_playback_run_status` to load its inputs —
see `docs/api/backtest.md`, `docs/api/runs.md`, `docs/api/playback.md` — and starts nothing: no
backtest, no playback run, no order.

The question this answers: **did the strategy fill the same trades on a real Market Replay session
that it filled in a headless backtest over the same window?** A backtest fills at the bar's own
price with no book to move; a replay session drives NinjaTrader's real order-matching engine. A gap
between the two is the backtest lying about what would really have happened.

## `nt_reconcile(...)`

```
nt_reconcile(backtest_id="", backtest_run_id="", backtest_trades=None,
             playback_run_id="", playback_executions=None,
             tolerance_ticks=1, tolerance_seconds=60, tick_size=None)
```

Give the backtest side **exactly one** of:

| Argument | Loads |
|---|---|
| `backtest_id` | a live/finished backtest by id, through `nt_backtest_status` (`docs/api/backtest.md`) |
| `backtest_run_id` | a saved run by id, through `nt_run` (`docs/api/runs.md`) |
| `backtest_trades` | a bare `trades[]` list, or the whole status/run document, handed in directly |

Give the replay side **exactly one** of:

| Argument | Loads |
|---|---|
| `playback_run_id` | a playback run by id, through `nt_playback_run_status` (`docs/api/playback.md`) |
| `playback_executions` | a bare execution list (`GET /executions` shape, `docs/api/accounts.md`), or a playback run's own `executions` field/document, handed in directly |

Giving zero or two of either side's arguments is a `400`-style `{"error": "..."}`, never a guess at
which one was meant. A `backtest_id`/`playback_run_id` still `queued` or `running` is also refused —
reconciling a result before it exists would silently compare a real backtest against nothing.

## What it does

1. **Normalizes the backtest's trades** (`normalize_backtest_trades`): each already-paired
   `trades[]` entry (`side`, `qty`, `entryTime`, `exitTime`, `entryPrice`, `exitPrice`, `pnl`,
   `pnlPoints` — Status document v1, `addon/NOTES.md`). A trade recorded as `{"error": ...}` or with
   no entry (`side == ""`) is dropped.
2. **Pairs the replay's raw fills into round-trip trades** (`build_trades_from_executions`): FIFO by
   side, per `(account, instrument)`, in time order. A fill on a flat position opens a trade; more
   fills the same side scale it in (quantity-weighted average price); an opposite-side fill closes
   it, until the entry quantity is used up. A closing fill bigger than what was open finishes that
   trade and opens a new one the other way with the leftover quantity — a position reversal, same as
   the broker's own book. A lot still open when the fill list ends (never closed) is dropped, not
   reported as a trade.
3. **Matches backtest trades to replay trades** (`match_trades`): same `side`, `entryTime` within
   `tolerance_seconds` (default 60 — the two runs' clocks are not the same clock), nearest-time
   first so two backtest trades competing for one replay trade cannot both claim it.
4. **Reports.**

## Result shape

```json
{
  "matched": [
    { "side": "Long", "entryTimeDeltaSec": 20.0,
      "backtestEntryTime": "2026-09-10T09:30:00", "playbackEntryTime": "2026-09-10T09:30:20",
      "backtestExitTime": "2026-09-10T10:00:00", "playbackExitTime": "2026-09-10T10:00:10",
      "backtestEntryPrice": 5000.0, "playbackEntryPrice": 5001.0,
      "entryPriceDelta": 1.0, "entryTicksDelta": 4.0,
      "backtestExitPrice": 5010.0, "playbackExitPrice": 5009.5,
      "exitPriceDelta": -0.5, "exitTicksDelta": -2.0,
      "backtestQty": 1, "playbackQty": 1, "qtyDelta": 0,
      "withinTolerance": false }
  ],
  "backtestOnly": [ ],
  "playbackOnly": [ ],
  "counts": {"backtestTrades": 1, "playbackTrades": 1, "matched": 1, "backtestOnly": 0, "playbackOnly": 0},
  "slippage": {"ticksAvailable": true, "tickSize": 0.25, "pointValue": 50.0,
               "totalTicks": 6.0, "totalCurrency": 75.0},
  "toleranceTicks": 1, "toleranceSeconds": 60,
  "verdict": "all 1 trade matched between backtest and replay; 1 matched trade outside the 1-tick tolerance."
}
```

`backtestOnly` / `playbackOnly` are trades in the normalized/paired shape above that found no match
— the backtest filled something the replay never did, or the other way round. `entryPriceDelta` /
`exitPriceDelta` are `playback − backtest`, in raw price units, always present. `entryTicksDelta` /
`exitTicksDelta` / `withinTolerance` are `null` unless `tick_size` is given (see below) — a matched
pair with usable prices but no `tick_size` still reports its price deltas, just not in ticks.

### `tick_size` and currency — there is no instrument table here

`nt_reconcile` never talks to NinjaTrader, so it has no tick size or point value for the instrument.
Pass `tick_size` (e.g. `0.25` for ES) to get `entryTicksDelta` / `exitTicksDelta` / `withinTolerance`
and the `slippage` totals; without it those are `null` and `slippage.ticksAvailable` is `false` — a
missing fact, not a false `0`.

Currency is **derived from the backtest's own trades**: `pointValue = pnl / (pnlPoints * qty)`,
median over every trade where that is computable (`docs/api/backtest.md`, Status document v1). One
trade with a rounding artefact or a commission difference cannot skew it. `slippage.totalTicks` is
the sum of `|entryTicksDelta| + |exitTicksDelta|` over every matched pair; `slippage.totalCurrency`
is that times `tick_size * pointValue * backtestQty`, summed the same way. Both are `null` when
`pointValue` could not be computed from any backtest trade, even with `tick_size` given.

### `verdict`

One sentence, always present, built from whichever of these apply:

- `"the backtest filled N trade(s) the replay did not"` — `backtestOnly` is non-empty.
- `"the replay filled N trade(s) the backtest did not"` — `playbackOnly` is non-empty.
- `"all N trade(s) matched between backtest and replay"` — neither of the above, and at least one match.
- `"neither the backtest nor the replay had a trade to compare"` — nothing on either side.
- `"; N matched trade(s) outside the <tolerance_ticks>-tick tolerance"` — appended when `tick_size`
  was given and at least one matched pair's `withinTolerance` is `false`.

## Reading the result

A backtest stamps a trade at the bar's own **close** time; a replay fill is stamped at the real
time the broker's matching engine filled it. So up to one bar of time difference between
`backtestEntryTime` and `playbackEntryTime` is normal, not a bug — `tolerance_seconds` (default
60) is there to absorb exactly that gap, not just clock drift.

**A price difference is a different kind of finding.** If `entryPriceDelta` / `exitPriceDelta`
are small (within a tick or two), that is ordinary fill-model slippage. If they are many points
apart, the historical store the backtest read and the Market Replay store the replay session read
hold **different data** for that day — a data-store finding, not a fill-model finding. Chasing
that as a bug in the strategy or in `nt_reconcile` wastes time; check `nt_data_coverage` for both
stores over that day instead.

## Known ceilings

- **Clocks are compared as naive timestamps.** A backtest's historical timestamps and a replay
  session's real execution timestamps are not guaranteed to share one clock or time zone —
  `docs/api/playback.md` admits the same gap with its own ±3 hour pad on execution reads.
  `tolerance_seconds` absorbs small skew; a large, systematic one needs a bigger tolerance. Upgrade
  path: an explicit clock-offset argument if a real mismatch is ever observed live.
- **One open lot per `(account, instrument)`.** `build_trades_from_executions` assumes no
  independent, concurrent entries in the same instrument (`EntriesPerDirection == 1`). Upgrade path:
  per-entry lot ids if a strategy under test ever scales in with independent stops.
- **Matching is greedy nearest-time, not globally optimal.** For the common case (one strategy, one
  instrument, trades well separated in time) this is exact; a pathological cluster of many same-side
  trades within one `tolerance_seconds` window could match a locally-better pair over a globally
  better one. Upgrade path: an assignment solver, if that is ever observed to matter on real data.
