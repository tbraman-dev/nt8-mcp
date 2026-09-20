# Optimize / walk-forward / report — `nt_optimize`, `nt_walkforward`, `nt_report`

Source: `server/nt8_mcp/tools_optimize.py` + `server/nt8_mcp/report.py`. Tests:
`server/tests/test_optimize.py`, `server/tests/test_report.py`.

**This module adds no HTTP endpoint and no AddOn file.** It is Python over the existing
`POST /backtest` + `GET /backtest/{id}` (see `docs/api/backtest.md`), so there is no new
NinjaTrader surface, no GUI driving, no worker thread, and cancel/queue/caps keep working
exactly as they do for one backtest. Every run is a Backtest-account run; nothing here touches a
Sim or live account, an order, a chart or the Strategy Analyzer window.

| Tool | Does |
|---|---|
| `nt_optimize(strategy, params, …)` | expands a parameter grid, runs one backtest per combination **serially**, ranks the finished runs on one `summary` key |
| `nt_walkforward(strategy, params, train_days, test_days, anchored=False, …)` | slices the range into in-sample/out-of-sample windows, optimizes each in-sample window, runs the winner once out-of-sample, stitches the out-of-sample trades |
| `nt_report(id \| status_doc, pdf_path="")` | stats (+ optional one-page PDF) for any backtest status document |

## `nt_optimize`

```python
nt_optimize(strategy, params, chart="first", instrument="", bars_period=None,
            from_date="", to_date="", tick_replay=False, inputs=None,
            fill_resolution="", fill_resolution_type="", fill_resolution_value=0,
            slippage_ticks=None, commission_template="", include_commission=None,
            fill_limit_on_touch=None,
            fitness="netProfit", top_n=10, min_trades=5,
            max_combos=200, max_runs=0, wait_s=600, include_trades=False)
```

`chart` / `instrument` / `bars_period` / `from_date` / `to_date` / `tick_replay` / `inputs` are
passed straight into each `POST /backtest` body, with the same meaning and the same defaults as
`nt_backtest` (dates default to the last 2 days) — except `tick_replay`, which defaults to
**False** here because a grid multiplies its cost. `inputs` is the fixed part of the parameter set;
the grid's values are merged over it per run. `chart` is sent whenever it is truthy — the same rule
`nt_backtest` uses — even alongside an explicit `instrument`/`bars_period`: the AddOn's explicit
instrument/bar-period already override the chart's seed values, but the chart still supplies
`TradingHours` and `ResetOnNewTradingDay`, so dropping it changed which bars a combo saw.

### Costs

`fill_resolution`, `fill_resolution_type`, `fill_resolution_value`, `slippage_ticks`,
`commission_template`, `include_commission`, `fill_limit_on_touch` — same names, same JSON keys
(`fillResolution`, `fillResolutionType`, `fillResolutionValue`, `slippageTicks`,
`commissionTemplate`, `includeCommission`, `fillLimitOnTouch`) and the same semantics as
`nt_backtest` (`docs/api/backtest.md`) — are passed straight through to **every** inner
`POST /backtest` call the grid makes. One tick of slippage can flip a strategy's sign; a fitness
computed on a gross run can rank the wrong parameter set. Nothing here recomputes or estimates a
cost — every one of these is NinjaTrader's own simulation, echoed back verbatim in the top-level
`costs` object (see "Result" below) so a consumer never has to guess whether a number is gross.

### `params`

Three accepted forms. All three expand to `{name: [value, …]}`, reported back as `grid`.

| Form | Example | Notes |
|---|---|---|
| range | `{"Fast": {"min": 5, "max": 20, "step": 5}}` | inclusive of `max`; `min`, `max` and `step` all required, `step > 0` |
| explicit list | `{"Mode": ["Long", "Short"]}` | how a bool / enum / string input is searched |
| constant | `{"Fixed": 3}` | one value; same as a one-element list |
| string | `"Fast:5:20:5,Slow:40:80:10"` | cli-nt-bridge's `--opt` syntax, with its validation wording |

A range steps with NinjaTrader's own epsilon rule from
`bin\Custom\Optimizers\@DefaultOptimizer.cs:44,50` — a value is in while
`min + i*step <= max + step/1e6`. A plain `<= max` silently drops the last point of a float grid
(`0 + 3*0.1 == 0.30000000000000004`). Values are emitted as `int` when `min`, `max` and `step` are
all whole numbers, so an `Int32` strategy input gets an integer.

Refusals (nothing is run, `{"error": …}` comes back):
`--opt entry 'Fast:5:20' needs 4 fields Name:min:max:step.` ·
`--opt entry '9Fast:5:20:5': '9Fast' is not a property name.` ·
`--opt entry 'Fast:5:20:x': step 'x' is not a number.` ·
`step width in 'Fast:5:20:0' must be > 0.` · `needs --opt=Name:min:max:step[,...]` ·
`params['Fast'] needs min, max and step (missing: step).`

### `fitness`

One key of the status document's `summary` (`netProfit`, `profitFactor`, `sharpe`, `winRate`,
`maxDrawdown`, `avgTrade`, … — all 21 are accepted), or one of NinjaTrader's own fitness names:

| `fitness` | ranks on `summary.…` |
|---|---|
| `MaxNetProfit` | `netProfit` |
| `MaxProfitFactor` | `profitFactor` |
| `MinDrawDown` | `maxDrawdown` |
| `MaxSharpeRatio` | `sharpe` |
| `MaxPercentProfitable` | `winRate` |
| `MaxWinLossRatio` | `winLossRatio` = `avgWinner / abs(avgLoser)`, the one derived value |

Every fitness is **maximised on NinjaTrader's own number**, never recomputed here. `maxDrawdown` is
`<= 0` in status document v1, so maximising it *is* "smallest drawdown". An unknown name is a
refusal that lists the legal ones. A run whose fitness key is `null` (e.g. `profitFactor` with no
losing trade, which NinjaTrader reports as Infinity and the AddOn writes as `null`) is listed with
`skipped: "summary.profitFactor is null"` and cannot win. So is a run with fewer than `min_trades`
trades (default 5 — NinjaTrader's own fitnesses have no such guard, so a 1-trade fluke wins
without it).

### Integer inputs

Before anything runs, `GET /strategies` is read once and a **fractional value headed for an integer
input is refused** (`'Fast' has fractional values [5.5] but SampleMACrossOver.Fast is Int32 — NinjaTrader
would round them silently. Nothing was run.`). Observed: the AddOn's input coercion
(`Convert.ChangeType`) rounds `5.5` to `6` and the status document still echoes `"Fast":5.5` —
`Fast=5.5` and `Fast=6.0` returned the same `netProfit`. That is a NinjaTrader-side defect;
this check keeps a grid from ranking two copies of one run. Whole
floats (`5.0`) pass: they coerce exactly. `nt_walkforward` makes the same check.

### Playback

**While the Playback connection is connected, NinjaTrader does not backtest the dates you ask for.**
Observed with Playback parked at a fixed replay time: every run ended at the replay clock and kept
only its *length* — a requested 8-day window instead ran ending at the parked date, and two
differently-dated requests of the same length produced runs covering the same days with identical
trades — while each status document echoed the dates that were asked for.
A walk-forward is then the same two days over and over. `nt_optimize` / `nt_walkforward` compare every
run's first entry and last exit with its requested window (one day of slack for the session that opens
the evening before) and put a sentence per offending run in `warnings` (first three, then a count).
Results are still returned — they are reproducible, just not for those dates. Disconnect Playback for
a real date range.

### Cost and the cap

Combinations run one after another. Measured (5-minute ES, 8 days, no tick replay):
NinjaTrader needs roughly 0.02-0.04 s per combo; a 3x3 grid takes about 1.2 s, a 2-window
walk-forward about 1.1 s and a 200-combo grid about 26 s (all 200 ran, every returned id still
resolved afterwards — the AddOn has no job-count cap, only `DELETE` and a recompile drop ids). The
status poll backs off from 0.1 s to `POLL_S`; a flat 2 s poll instead of the backoff can take an
order of magnitude longer for the same work. Rough figures with a custom bar type
(see `addon/NOTES.md`): roughly 0.7 s for 8 days of 5-minute bars, roughly 5 s for 2 days of
tick-replay custom bars — so 100 tick-replay combos over 20 days is on the order of an hour or more.
NinjaTrader's own headless optimizer takes comparable time per iteration (see `addon/NOTES.md`,
"Headless RunOptimization"), so an AddOn-side `POST /optimize` would buy nothing at this size. If
the grid is larger than `max_combos` (default 200; `max_runs` is an alias for the
same cap, and wins when non-zero) **nothing is run at all**:

```json
{"error": "9 combinations exceeds max_combos=2 — nothing was run. Narrow the grid or raise max_combos.",
 "combos": 9, "maxCombos": 2, "grid": {"Fast": [1,2,3,4,5,6,7,8,9]}, "ran": 0}
```

No probe run is made to estimate the wall clock: "nothing run" means no backtest was queued.

### Result

```json
{"strategy": "SampleMACrossOver", "fitness": "netProfit", "fitnessKey": "netProfit",
 "grid": {"Fast": [5, 10, 15]}, "combos": 3, "ran": 3, "ranked": 3,
 "from": "2026-09-10", "to": "2026-09-17", "minTrades": 5,
 "costs": {"slippageTicks": 1, "commissionTemplate": "Default", "includeCommission": true,
   "fillResolution": null, "fillResolutionType": null, "fillResolutionValue": null,
   "fillLimitOnTouch": null},
 "rows": [
   {"id": "b2", "state": "done", "inputs": {"Fast": 10}, "fitness": 300.0,
    "summary": {"trades": 12, "netProfit": 300.0, "...": "all 21 summary keys"},
    "skipped": null, "seconds": 0.71, "rank": 1},
   {"id": "b3", "state": "done", "inputs": {"Fast": 15}, "fitness": 200.0, "summary": {"...": "…"},
    "skipped": null, "seconds": 0.69, "rank": 2}],
 "best": {"id": "b2", "state": "done", "...": "the winner's FULL status document",
   "trades": null},
 "errors": [], "warnings": []}
```

Null rules:

| Key | Type | Null? | Meaning |
|---|---|---|---|
| `costs` | object | never | every field below is `null` when that setting was never passed; see "Costs" |
| `costs.note` | string | **present only when every cost field is null** | literally `"gross: no slippage or commission modelled"` |
| `best` | object | null only when `ran == 0` or every row was skipped | the winner's full status document |
| `best.trades` | array | **null unless `include_trades=True`** | the winner's own status-document `trades[]`, or `null` when withheld |
| `rows[].skipped` | string | null for a row that qualified and could win | see below for the reasons |
| `rows[].rank` | int | absent (not `null` — the key is left out) on a skipped row | 1-based, best first |

- `rows` is best-first, truncated to `top_n`, **followed by every skipped row** (a skipped row has
  `skipped: <reason>` and no `rank`). Sort or filter it yourself for a Pareto view — multi-objective
  is "read two columns of this table", not a second run.
- `rows[].summary` is the whole `summary` object; `trades[]` is never in a row (500 combos × full
  trades is a memory bomb). Only `best` can carry trades, and only when `include_trades=True` — by
  default `best.trades` is `null` even though the winner's own status document had a real array. To
  reproduce a row's numbers, call `nt_backtest(strategy, inputs=row["inputs"], instrument=...,
  bars_period=..., chart=..., from_date=..., to_date=..., **tick_replay=False**)` — the same
  `instrument`/`bars_period`/`chart`/dates/costs the grid ran with, and **`tick_replay=False`
  explicitly**: `nt_optimize` defaults it to `False`, `nt_backtest` defaults it to `True`, and running
  the reproduction with Tick Replay on gets different fills (and a different `netProfit`) for any
  strategy that reads the tape in `OnMarketData`.
- A row's `skipped` reason, in the order it is checked: (1) the run **never really ran** — its state
  is `error`/`timeout`/`cancelled`, or it came back `"done"` with its own `barsFrom` explicitly
  `null` (NinjaTrader raised an unhandled modal, refused the fill mode, or the instrument never
  resolved) — this is added to `errors` too, and is the one case that overrides
  everything else: a job that never ran is never treated as a legitimate zero-trade, zero-profit
  result, however it reads. (2) fewer than `min_trades` trades. (3) the fitness key itself is `null`
  in that run's summary (e.g. `profitFactor` with no losing trade). A `barsFrom` key that is simply
  **absent** (an older `nt_backtest` build) is not case (1) — only an explicit `null` is.
- Every combo's backtest is DELETEd from the AddOn once ranking is done, except the winner (`best`)
  and the rows this call actually returns (`rows`, which is `top_n` plus every skipped/errored row) —
  the AddOn keeps a job until DELETEd, so a 200-combo grid does not leave 200 result documents resident
  in the live NinjaTrader process. A row you want to keep querying by id should be re-run with
  `nt_backtest` rather than assumed to still exist.
- `ranked` counts the rows that qualified before `top_n` truncation.
- A run the AddOn **refused** (no job was ever created — unknown input, bad instrument, bad date) is
  different from a never-ran row: every later combination would be refused the same way, so the grid
  stops there and the result carries `error`, `refused`, `refusedInputs` and the rows that did run.

## `nt_walkforward`

```python
nt_walkforward(strategy, params, train_days=30, test_days=10, anchored=False,
               from_date="", to_date="", chart="first", instrument="", bars_period=None,
               tick_replay=False, inputs=None,
               fill_resolution="", fill_resolution_type="", fill_resolution_value=0,
               slippage_ticks=None, commission_template="", include_commission=None,
               fill_limit_on_touch=None,
               fitness="netProfit", min_trades=5,
               max_combos=200, max_runs=0, max_backtests=200, wait_s=600,
               optimization_period_days=0, test_period_days=0, include_trades=False)
```

`optimization_period_days` / `test_period_days` are accepted as aliases for `train_days` /
`test_days` (names matching `StrategyBase.OptimizationPeriod` /
`.TestPeriod`, both in DAYS); a non-zero alias wins.

The cost settings (`fill_resolution`, `fill_resolution_type`, `fill_resolution_value`,
`slippage_ticks`, `commission_template`, `include_commission`, `fill_limit_on_touch`) are the same
as `nt_optimize`'s (see "Costs" above) and are passed to **every** inner backtest — the in-sample
grid on each window AND the out-of-sample run — so an in-sample winner is never picked on a gross
fitness and then validated with costs, or the reverse.

Window k (0-based), all dates inclusive:

| | rolling (default) | anchored |
|---|---|---|
| in-sample | `from + k*test_days` … `from + train_days + k*test_days - 1 day` | `from` … the same end |
| out-of-sample | the next `test_days` days | same |

Windows are cut while the out-of-sample end still fits inside `to_date`; a partial tail window is
never run. A range too short for one window is a refusal naming the arithmetic, and no backtest is
started. Total backtests = `windows × (combinations + 1)`, serial. The grid is checked against
`max_combos` once, before anything runs; separately, the **full** total is checked against
`max_backtests` (default 200) once the window count is known, and refused the same way if it's over:

```json
{"error": "192 windows x (9 combos + 1) = 1920 backtests exceeds max_backtests=200 — nothing was "
          "run. Narrow the date range or raise max_backtests.",
 "windows": 192, "combos": 9, "backtests": 1920, "ran": 0}
```

A grid small enough to clear `max_combos` can still produce far more total runs once multiplied by
a wide date range's window count — this second cap is what refuses that case up front instead of
queuing thousands of backtests one at a time with no way to stop short of restarting NinjaTrader.

```json
{"strategy": "SampleMACrossOver", "fitness": "netProfit", "grid": {"Fast": [5, 10]}, "combos": 2,
 "from": "2026-01-01", "to": "2026-01-20", "trainDays": 10, "testDays": 5, "anchored": false,
 "minTrades": 5,
 "costs": {"slippageTicks": null, "commissionTemplate": null, "includeCommission": null,
   "fillResolution": null, "fillResolutionType": null, "fillResolutionValue": null,
   "fillLimitOnTouch": null, "note": "gross: no slippage or commission modelled"},
 "table": [
   {"window": 1, "inSample": {"from": "2026-01-01", "to": "2026-01-10"},
    "outOfSample": {"from": "2026-01-11", "to": "2026-01-15"}, "inputs": {"Fast": 10},
    "inSampleFitness": 300.0, "outOfSampleNetProfit": 320.0, "outOfSampleTrades": 8,
    "state": "done", "error": null}],
 "windows": [
   {"window": 1,
    "inSample":    {"from": "2026-01-01", "to": "2026-01-10", "inputs": {"Fast": 10},
                    "fitness": 300.0, "ranked": 2},
    "outOfSample": {"from": "2026-01-11", "to": "2026-01-15", "id": "b3", "state": "done",
                    "inputs": {"Fast": 10}, "summary": {"...": "NinjaTrader's own summary"},
                    "error": null}}],
 "outOfSample": {"strategy": "SampleMACrossOver", "state": "done", "windows": 2,
                 "summary": {"trades": 8, "winners": 5, "losers": 3, "winRate": 0.625,
                             "netProfit": 600.0, "grossProfit": 900.0, "grossLoss": -300.0,
                             "profitFactor": 3.0, "commission": 0, "maxDrawdown": -120.0,
                             "avgTrade": 75.0},
                 "trades": [],
                 "note": "summed in Python across the out-of-sample windows; …"}}
```

### `table` — read this one first

Always present, one row per window, in window order, **never** the trade lists (that's what
`windows[]`/`outOfSample.trades` are for) — this is the compact summary in place of
a 126 KB response with every out-of-sample trade inlined twice. Null rules:

| Key | Type | Null? | Meaning |
|---|---|---|---|
| `window` | int | never | 1-based |
| `inSample` / `outOfSample` | object `{from, to}` | never | the window's own date range, always present even when the window was skipped or errored |
| `inputs` | object | **null** when no in-sample combination qualified | the winning in-sample combo, merged over the fixed `inputs` |
| `inSampleFitness` | number | null, same condition as `inputs` | the winner's in-sample fitness value |
| `outOfSampleNetProfit` | number | **null** unless `state == "done"` | never `0` for a run that did not really happen — see `state` |
| `outOfSampleTrades` | int | same as `outOfSampleNetProfit` | |
| `state` | string | never | `"done"` (a real out-of-sample result), `"error"` (the AddOn refused, the run ended in error/timeout/cancelled, or it came back `"done"` with `barsFrom` explicitly `null` — never really started), or `"skipped"` (no in-sample combination qualified, so no out-of-sample run was even attempted) |
| `error` | string | null only when `state == "done"` | the reason, verbatim where NinjaTrader gave one |

A window that reads `state: "error"` with `outOfSampleNetProfit: null` catches, for walk-forward,
the same job that would have looked like a legitimate zero-trade
out-of-sample result in `windows[].outOfSample.summary` (trades 0, netProfit 0, error null) is never
folded into the top-level `outOfSample` stitched summary either — a never-ran window contributes
nothing to `outOfSample.windows` or its summed `netProfit`.

### `windows[]` and the stitched `outOfSample` — the detailed record

- A window whose in-sample grid produced **no** qualifying combination gets
  `inSample.inputs: null`, `outOfSample: null` and a `note`; it is skipped, not guessed at (and its
  `table` row reads `state: "skipped"`).
- The top-level `outOfSample` object is **status-document-shaped, but not from the AddOn**: its
  `summary` is summed in Python across the windows and its `maxDrawdown` comes from the stitched
  equity curve. It is the only place in this repo where a performance number is computed outside
  NinjaTrader, because no single NinjaTrader run spans the windows. The per-window
  `windows[].outOfSample.summary` objects are NinjaTrader's own and are the authoritative ones.
  Each stitched trade keeps its original keys plus `window`, and `n` is renumbered from 0.
- `outOfSample.trades` is `[]` unless `include_trades=True` — the default keeps the response small.
  With `include_trades=True` every out-of-sample trade appears there exactly
  ONCE, in date order; it is never also duplicated per window (`windows[].outOfSample` never carries
  a `trades` key at all).
- It is shaped that way on purpose: hand it straight to `nt_report(status_doc=…)`.
- A top-level `warnings` list carries the same "different window" sentences as `nt_optimize`'s (see
  *Playback*). Under normal conditions the window dates POSTed match the window dates reported and
  the job documents' `from`/`to`, and the windows' out-of-sample `netProfit` values sum to the
  stitched total — but under a connected Playback, windows can collapse onto the same days, and
  `warnings` says so.

## `nt_report`

```python
nt_report(id="", status_doc=None, pdf_path="")
```

**Design note.** An earlier spec named this surface `nt_backtest_report(id: str, out_path: str) ->
dict`. This module ships `nt_report(id, status_doc, pdf_path)` instead — same job,
plus `status_doc` (a superset: it takes any status-document-shaped object with no AddOn round trip,
which is how `nt_walkforward`'s stitched `outOfSample` gets a PDF) and `pdf_path` instead of
`out_path`. Call `nt_report`, not `nt_backtest_report` — the latter name does not exist.

`id` is one backtest id (`"b3"`) or several (`"b1,b2,b3"` → one batch page); the documents are
fetched with `GET /backtest/{id}`. `status_doc` skips the AddOn entirely and takes any status
document v1 shaped object — including `nt_walkforward`'s stitched `outOfSample`, which is how a
walk-forward gets a PDF.

```json
{"stats": {"strategy": "SampleMACrossOver", "id": "b1", "net": -212.5, "trades": 76,
           "profit_factor": 0.985, "max_dd": -4425, "win_rate": 38.2, "avg_trade": -2.8,
           "avg_win": 478.0, "avg_loss": -327.3, "sharpe": -1.02, "n_wins": 29, "n_losses": 43},
 "assessment": "unprofitable (net -212.50); profit factor 0.98 (no edge); 76 trades; max drawdown -4,425.00",
 "table": "trades   76\nwinners  29\n…",
 "pdf": "C:/tmp/b1.pdf",
 "note": null}
```

- With several ids, `stats` and `assessment` are lists in the same order and `table` is the first
  document's.
- **NinjaTrader's numbers are used as they are.** `report.compute_stats` only derives what the
  status document does not carry: the equity curve, the running peak and the underwater drawdown,
  cumulated from `trades[].pnl`. A summary key that is missing or `null` falls back to a value
  derived from the trades — never to an exception — so a partial document still reports.
  `win_rate` is a **percentage**; `summary.winRate` is a fraction.
- The equity / drawdown / pnl arrays are in `report.compute_stats(doc)` for the PDF; `nt_report`
  leaves them out of its answer.
- `pdf_path` writes a one-page PDF: header, six KPI tiles, equity curve with running peak,
  underwater drawdown, trade-P&L histogram (batch page: a summary table + net-P&L-by-run bars).
  **matplotlib is an optional import.** Without it — or if the path cannot be written — `pdf` is
  `null`, `note` says exactly why, and the stats are still there. It is never an error.

## Not ported from cli-nt-bridge

`RunAnalyzerRun`, Strategy Analyzer window driving, `StrategyAnalyzerGridEntry` reading, template
file restore for optimization, CSV `LogFilePrefix` collection, `GeneticOptimizer`. Their own
measurement is why: the Strategy Analyzer path and their headless runner produced identical
windows, parameters, trade counts and byte-identical CSVs, so the
expensive path buys no accuracy. `report.py` is ported (attribution header in the file; `metrics` →
`summary`, `totalTrades` → `trades`).
