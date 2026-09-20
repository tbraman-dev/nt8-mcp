# Chart control — `/chart/{id}/indicator/*`, `/series`, `/scroll` (module `chartcontrol`, `addon/NT8Bridge.ChartControl.cs`)

Covers `POST /chart/{id}/indicator/add`, `POST /chart/{id}/indicator/remove`,
`POST /chart/{id}/series`, `POST /chart/{id}/scroll`. Same conventions as `API.md`: JSON, UTF-8,
`{"error":"…"}` on 4xx/5xx, times local NT8 `yyyy-MM-ddTHH:mm:ss`.

This module never touches an account — no arming file, no provider check, no audit log. Read-only
chart endpoints (`GET /chart/{id}`, `.../bars`, `.../indicators`, `.../drawings`, `POST .../reload`,
`.../screenshot`) are `Route_Charts` (`addon/NT8Bridge.Charts.cs`) and unchanged by this module.
The point of this one is the **write -> compile -> put on chart -> look -> fix** loop: a model
changes a chart's indicator or series, then reads the result back with the existing read tools (or
`nt_trade_shot`, below) instead of guessing whether the change took.

**Status:** `/chart/{id}/indicator/add`, `/chart/{id}/indicator/remove` and `/chart/{id}/scroll`
are confirmed against a live NinjaTrader 8.1.8.2 install (see "What a live check must prove",
below). `/series` is exercised too: a chart on a third-party bar type went to 5 Minute and back to its exact original period with `{"restore":true}`.

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/chart/{id}/indicator/add` | `{"indicator","inputs":{},"panel":0}` | the chart report (below) |
| POST | `/chart/{id}/indicator/remove` | `{"indicator" \| "index", "force":false}` | the chart report |
| POST | `/chart/{id}/series` | `{"instrument","barsPeriod":{"type","value"}}` | the chart report |
| POST | `/chart/{id}/scroll` | `{"time"}` | the chart report |

`id` is a chart id from `GET /charts`, or `"first"`. An unknown id is `404`.

## Guards

- **A standing modal dialog refuses every one of these four calls**, before the body is even
  parsed — `409 {"error":"refused: a modal dialog is open (<title>)","standingModal":"<title>"}`.
  The core's `StandingModal()` is the same check `/compile` and `/backtest` use.
- **`/series` refuses a chart with an ENABLED strategy attached** — `400 "refused: this chart has
  an enabled strategy attached — stop it first"`. Changing the instrument or bar period under a
  running strategy is how a chart ends up in a state nothing here can fix; indicator add/remove and
  `/scroll` do not carry this guard, because neither changes what the strategy is trading.
- **`/indicator/remove` only removes an indicator THIS MODULE added**, so a user's own chart setup
  is never torn down by accident. Removing anything else needs `"force":true`. Tracked by object
  identity for the life of the process (an indicator's Name is not unique — a user's own indicator
  can share a display name with one this module added). Observed on NinjaTrader 8.1.8.2: removing an
  indicator this module added works; removing one it did not add is refused unless `"force":true`
  is sent, and with `"force":true` it is removed like any other.
- Observed on NinjaTrader 8.1.8.2: `/indicator/add` only succeeds when the new indicator instance is
  given the chart's primary `ChartBars`, `SetInput(bars)` is called on it, and its `Owner` is set —
  in that order, all three BEFORE `RefreshIndicators` runs. Skipping any one of them makes
  `RefreshIndicators` throw a `NullReferenceException`; this module catches that, removes the
  half-added indicator, and calls `RefreshIndicators` again so the chart is left exactly as it was
  before the call (see the add handler in `addon/NT8Bridge.ChartControl.cs`).
- Three of these four moves depend on a `ChartControl` member that is `internal`, not `public`
  (`RefreshIndicators`, `RemoveIndicator`, `RefreshBars` — see the .cs file's header for why). Each
  is resolved once, at AddOn start, the same `Compat.Resolve` pattern `Start_Charts` uses for the
  reload menu item (`GET /compat` shows whether it resolved). **When one does not resolve, the
  endpoint that needs it refuses cleanly and changes nothing** — it never leaves an indicator half
  added or a series half changed.

## `POST /chart/{id}/indicator/add`

```json
{"indicator": "SMA", "inputs": {"Period": 20}, "panel": 0}
```

`indicator` is a short (`"SMA"`) or full (`"NinjaTrader.NinjaScript.Indicators.SMA"`) type name —
anything `Core.Globals.AssemblyRegistry` resolves to a chart indicator type in this AddOn's own
assembly (which is every indicator NinjaTrader ships or a user compiles: both live in
`NinjaTrader.Custom`). Unknown name = `400`. `inputs` sets `[NinjaScriptProperty]` properties by
name (case-insensitive), same coercion rule as `nt_backtest`'s `inputs`: a JSON number sent to an
`int`/`enum` property must be a whole number (`5.5` is `400`, never rounded), and an unknown key
name is `400`, not silently ignored. `panel` is the chart panel index (`0` = price panel); omitted
= the indicator's own default panel.

## `POST /chart/{id}/indicator/remove`

```json
{"indicator": "SMA"}
```
or
```json
{"index": 2, "force": true}
```

`indicator` matches the indicator's display name exactly (as `GET /chart/{id}/indicators` prints
it); `index` is its position in that same list. Exactly one of the two is required. No match =
`404`-shaped `400` (`"no indicator '…' on this chart"` / `"index N is out of range"`). An indicator
this module did not add refuses with `400` unless `"force":true` is sent.

## `POST /chart/{id}/series`

```json
{"instrument": "ES 12-26", "barsPeriod": {"type": "Minute", "value": 5}}
```

Either field alone is fine — send only what changes. `instrument` resolves through
`Instrument.GetInstrument`; an unknown name is `400`. `barsPeriod.type` is a NinjaTrader
`BarsPeriodType` name (`"Minute"`, `"Day"`, `"Tick"`, `"Volume"`, `"Range"`, `"Renko"`, …),
case-insensitive; an unknown name is `400`. `barsPeriod.value` defaults to `1` and must be `>= 1`.
Refused (`400`) while the chart carries an enabled strategy — see Guards.

## `POST /chart/{id}/scroll`

```json
{"time": "2026-09-17T14:30:00"}
```

Moves the chart's visible bar window so `time` falls inside it, keeping the window's current width.
A time before the first bar or after the last clamps to the nearest end. Does not change zoom.

## The chart report

Every one of the four calls above returns the SAME shape: what the chart shows **after** the call,
read back from NinjaTrader rather than assumed from the request.

```json
{"instrument": "ES 12-26", "period": "5 Minute",
 "firstVisibleTime": "2026-09-17T09:30:00", "lastVisibleTime": "2026-09-17T16:00:00",
 "indicators": [{"name": "SMA", "displayName": "SMA(20)", "panel": 0,
                 "inputs": {"Period": 20}, "plots": [...], "drawings": 0}]}
```

`indicators` is built with the exact same per-indicator shape `GET /chart/{id}/indicators` returns
(one plot value each, since only the read-back matters here, not history) — the two can never
drift apart because this module calls the same helper Charts.cs does. An indicator not yet
`Realtime`/`Historical`, or with no bars yet, reports `{"name","error"}` instead of throwing.

## MCP tools

| Tool | Endpoint |
|---|---|
| `nt_chart_indicator_add(chart, indicator, inputs=None, panel=None)` | `POST /chart/{id}/indicator/add` |
| `nt_chart_indicator_remove(chart, indicator=None, index=None, force=False)` | `POST /chart/{id}/indicator/remove` |
| `nt_chart_set_series(chart, instrument=None, bars_period=None)` | `POST /chart/{id}/series` |
| `nt_chart_scroll_to(chart, time)` | `POST /chart/{id}/scroll` |
| `nt_trade_shot(run_id, trade_index, chart="first")` | scrolls to the trade's entry time, then screenshots (`server/nt8_mcp/tools_chartcontrol.py`, no new AddOn endpoint) |

`nt_trade_shot` reads `run_id` from `nt_runs`/`nt_run`, or a live (unsaved) backtest id such as
`nt_backtest` returns; either way it needs that run's `trades[trade_index].entryTime`. A missing
run/backtest, an out-of-range `trade_index`, or a trade with no `entryTime` (no entry — e.g. a
short-lived reversal) is `{"error": "…"}`, never a screenshot of the wrong place.

## Threading

- Every handler validates and parses its body BEFORE touching the chart, so a `400` never runs
  `OnChart`/`Ui()` at all.
- The chart mutation itself is one `OnChart(chart, cc => {...})` call per endpoint — one bounded
  dispatcher round trip (`Ui()`, 5 s), matching `Route_Charts`. No `BeginInvoke`, no unbounded wait,
  never a NinjaTrader call while holding a lock (this module holds none).
- Indicator construction and `[NinjaScriptProperty]` coercion happen OFF the chart's dispatcher
  (plain object construction, same as `Backtest.cs`'s strategy instantiation) — only
  `Indicators.Add`/`Remove` and the two reflective refresh calls run ON it.
- No event subscription anywhere in this module, so there is no `Stop_ChartControl`.

## What a live check must prove

The offline gate (`check-module.sh` + `server/tests/test_chartcontrol.py`) proves this file
compiles and the Python side calls the right paths with the right bodies. It **cannot** prove that
`RefreshIndicators`/`RefreshBars`/`RemoveIndicator` do what their signatures suggest, because
NinjaTrader does not document their bodies — behavior for add/remove/scroll is observed on
8.1.8.2, see the .cs file's header; `/series` is exercised as well.

`scripts/smoke.d/28-chartcontrol.sh` is deliberately READ-ONLY: every request in it is one that is
expected to be REFUSED (an unknown indicator name, removing a not-mine indicator without `force`,
an unknown instrument, a modal-dialog note), so an automated smoke run never adds, removes, or
otherwise mutates anything on a real chart. Add and remove can only be verified by hand, on
Sim101 / Playback101:

1. `nt_chart_indicator_add(chart, "SMA", {"Period": 20})` — SMA(20) appears on the chart, on the
   requested panel, and the response's `indicators[]` entry for it reads back `Period: 20`.
2. `nt_chart_indicator_remove` on that same indicator by name — it disappears from the chart and
   from the response's `indicators[]`; removing a DIFFERENT, pre-existing chart indicator without
   `force` is refused, and with `force` also disappears.
3. `nt_chart_set_series(chart, bars_period={"type":"Minute","value":15})` on a chart with NO
   strategy attached — the chart reloads at 15-minute bars; `GET /chart/{id}` and this response
   agree on `period`. The same call on a chart with an ENABLED strategy is refused. Exercised on a live platform.
4. `nt_chart_scroll_to(chart, "<some time inside the loaded bars>")` — the chart's visible window
   moves to include that time; `firstVisibleTime`/`lastVisibleTime` bracket it. Exercised on a
   live platform (see Status, above).
5. `nt_trade_shot(run_id, 0)` after a backtest with at least one trade — the screenshot is centered
   on that trade's entry.

## `/series`: what was observed on NinjaTrader 8.1.8.2

- `ChartControl.RefreshBars` enumerates its `added` and `removed` arguments: they must be EMPTY
  collections, never null.
- The chart reloads its bars AFTER `RefreshBars` returns. The endpoint re-reads the chart (up to
  about 8 s) and answers with the report taken after the reload, so `period` in the response is
  the new one.
- `{"restore": true}` puts back the chart's OWN original `BarsPeriod` object and instrument, saved
  in memory the first time the module changes that chart. A third-party bar type can carry
  settings that `barsPeriod` in a request cannot express; only the same object restores them all.
  The saved original does not survive a NinjaScript reload. With nothing saved the answer is a 400.
- A failed change rolls the chart's series properties back, so nothing is left half-set.
- `barsPeriod` takes `type` (name or number), `value`, `value2`, `baseType`, `baseValue`.
