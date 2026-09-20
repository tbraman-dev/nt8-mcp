# Backtests — `/strategies`, `/templates`, `/backtest`, `/backtests`, `/backtest/{id}`

Module: `addon/NT8Bridge.Backtest.cs` (`Route_Backtest`, `Start_Backtest`, `Stop_Backtest`).
This file documents fill resolution / slippage / commission and strategy templates, plus the
date-range guard; `API.md` "Backtests (v1.1)" documents everything that was already there and
stays true.

Runs a strategy from the compiled `NinjaTrader.Custom` assembly headlessly, the way the Strategy
Analyzer does, and returns its `SystemPerformance`. Backtests run on the **Backtest** account only;
any other account name is a 400. No order ever reaches a Sim or live account through this API.

| Method | Path | Returns |
|---|---|---|
| GET | `/strategies` | every non-abstract `StrategyBase` in `NinjaTrader.Custom` with its `[NinjaScriptProperty]` inputs |
| GET | `/templates?strategy=<name>` | the strategy templates NinjaTrader has saved for that strategy |
| POST | `/backtest` | body below → `202 {"id":"b1","state":"queued"}` |
| GET | `/backtests` | rows: keys 1-12 of the status document, oldest first |
| GET | `/backtest/{id}` | the status document |
| DELETE | `/backtest/{id}` | `{"ok":true}`; terminates a running run and drops the record |

## `GET /templates?strategy=SampleMACrossOver`

```json
{"strategy": "SampleMACrossOver",
 "folder": "C:\\Users\\you\\Documents\\NinjaTrader 8\\templates\\Strategy\\SampleMACrossOver",
 "templates": ["Base", "ES-5m"]}
```

- `strategy` is required and must be a name `GET /strategies` lists (short or full type name).
  Missing → `400 {"error":"strategy is required: /templates?strategy=<name>"}`; unknown →
  `400 {"error":"unknown strategy '…' — see /strategies"}`.
- `templates` are the `*.xml` file names in the folder, without the extension, sorted
  case-insensitively. `[]` means the folder exists and holds no template (NinjaTrader creates it on
  the first save).
- `folder` comes from `StrategyTemplate.GetTemplateFolder(strategy)` — asked of NinjaTrader, never
  assembled from a naming rule, because the layout differs per strategy (`Strategy\Foo` vs
  `Strategy\Foo.Foo`). When NinjaTrader will not name it, or the folder cannot be listed, the answer
  is `"folder":null|"…", "templates":null` plus a `note`. **`templates` is never `[]` for a folder
  that could not be read** — an unreadable folder and an empty one are different claims.
- Live on 8.1.8.2: `GetTemplateFolder` returns a real path for a strategy that never had a template saved, **and
  creates that (empty) folder** as a side effect — so the common answer is `templates: []`, not `null`.
- Otherwise reads files only. A short-lived `StrategyBase` is constructed to ask for the folder and torn down
  again (`Terminated` → `Finalized` → `DbRemove`), exactly as `GET /strategies` already does.

## `POST /backtest` body

```json
{
  "strategy": "SampleMACrossOver",
  "chart": "first",
  "instrument": "ES 12-26",
  "barsPeriod": {"type": "Minute", "value": 5},
  "from": "2026-09-15",
  "to": "2026-09-17",
  "tickReplay": true,
  "inputs": {"NearTerm": 3},
  "account": "Backtest",

  "template": "ES-5m",
  "fillResolution": "Standard",
  "fillResolutionType": "Minute",
  "fillResolutionValue": 1,
  "slippageTicks": 2,
  "commissionTemplate": "ES-RT",
  "includeCommission": true,
  "fillLimitOnTouch": false,
  "includeTradeHistory": true,
  "maxTrades": 0
}
```

The first block is unchanged from `API.md`. The new fields:

| field | type | NT8 member | notes |
|---|---|---|---|
| `template` | string | — | A bare name resolves in the strategy's own template folder; a value carrying `\`, `/` or `:` is taken as a path. `.xml` is appended when absent. |
| `fillResolution` | `"Standard"` \| `"High"` | `OrderFillResolution` | anything else → 400 |
| `fillResolutionType` | `BarsPeriodType` name or int | `OrderFillResolutionType` | same spelling rules as `barsPeriod.type` |
| `fillResolutionValue` | int ≥ 1 | `OrderFillResolutionValue` | `< 1` → 400 (`[Range(1, …)]` on the NT8 property) |
| `slippageTicks` | number ≥ 0 | `Slippage` | ticks. Negative / `NaN` → 400 |
| `commissionTemplate` | string | `BacktestCommissionTemplate` | a NinjaTrader commission-template name (Control Center → Tools → Commissions; the files in `templates\Commission`). Empty string → 400. **An unknown name → 400 listing the known names**: live on 8.1.8.2 NinjaTrader accepts any string, reads it back unchanged and silently charges its default template, so the read-back cannot catch a typo. Passing a template **implies `includeCommission:true`** (a template with `IncludeCommission` off charges 0); an explicit `includeCommission:false` still wins |
| `includeCommission` | bool | `IncludeCommission` | `true` without a `commissionTemplate` charges NinjaTrader's default template (live: the same figure as `NinjaTrader Brokerage Free`) |
| `fillLimitOnTouch` | bool | `IsFillLimitOnTouch` | |
| `includeTradeHistory` | bool (default `true`) | `IncludeTradeHistoryInBacktest` | `false` → `trades` comes back `[]`. Live on 8.1.8.2 `summary` is **unchanged** (61 trades, same net profit): NinjaTrader keeps the performance numbers and drops only the per-trade history |
| `maxTrades` | int ≥ 0 (default 0 = unlimited) | — | client-side trim of `trades[]` to the first N. **`summary` always counts every trade**, so `summary.trades > trades.length` is how truncation shows |

**Hard refusal:** `tickReplay:true` with `fillResolution:"High"` →
`400 {"error":"Order Fill Resolution is not available when Tick Replay is enabled"}`
(NinjaTrader refuses the combination; `Gui.Resource.resx:1055`).

**Everything is validated on the HTTP thread, before the job is queued** — an unknown enum name, an
out-of-range number, a missing template file, a template belonging to another strategy: all 400,
nothing armed. `RangeProblem` runs on the **merged** range last, so a template carrying the
fresh-NinjaScript placeholders `From 2099-12-01 / To 1800-01-01` is refused with the file named,
instead of making NinjaTrader load until it stops answering.

### How `template` merges

A template supplies **defaults only**. Precedence, highest first:

1. an explicit body field,
2. `chart` (when given: instrument, bar period, trading hours),
3. the template,
4. the existing 400 when instrument/barsPeriod/from/to are still missing
   (`instrument and barsPeriod are required without chart or template`).

Read off the restored instance: `From`, `To`, `BarsPeriod` (copied field by field), the instrument
named by `InstrumentOrInstrumentList`, and every writable `[NinjaScriptProperty]`. The `inputs` key
of the status document echoes the **merged** map (template values, then body values) — that is what
the run used. `tickReplay` is **not** taken from a template; pass it explicitly.

The restored instance is never used as the run object: `Configure()` builds its own, and that is the
part that works. The restored one is torn down as soon as its values are copied out. The instrument
**does** travel with a template here, unlike the Strategy Analyzer route, because we read it off the
restored instance ourselves. An instrument the template names but this machine does not have is a
missing default, not an error.

`RestoreFullStrategyTemplate` is handed the XML **root element**, not the `XDocument`: handed a
document it looks for `<StrategyType>/<Strategy>` as its own children, finds nothing and returns
null. `StrategyTemplate` is a public static class in `NinjaTrader.Gui`, which
`NinjaTrader.Custom.csproj` already references, so both calls are typed — there is no reflection in
this module and therefore nothing for `GET /compat` to report.

## Truthfulness fixes

A job whose strategy never really ran must never report `state:"done"`. Four cases, all observed
on NinjaTrader 8.1.8.2 and all fixed at the boundary this repo controls:

1. **No bars ever loaded.** If `barsFrom` would still be `null` when the run finishes, the job
   ends `state:"error"` instead of `"done"`. When NinjaTrader is showing a dialog, `error` names
   its title and says to close it (NinjaTrader raises the dialog a moment after the run returns,
   so the job waits up to 1.5 s for it); else `"the strategy never started / no bars loaded"`.
2. **Multi-series + `fillResolution:"High"`.** NinjaTrader itself refuses this combination — "'High'
   Order Fill Resolution is only available for single-series strategies. For multi-series
   strategies, please program directly into your strategy the more granular resolution you would
   like to simulate order fills with." — but only shows a modal and returns a fast, empty
   `SystemPerformance` instead of raising. It is refused BEFORE the run: a throwaway instance is
   driven to `State.Configure` on Standard fill resolution and only the periods the STRATEGY adds
   are counted, so NinjaTrader never shows its dialog and the job ends `state:"error"` with
   NinjaTrader's own sentence. The count is never taken after a run: on a High request NinjaTrader
   adds a fill series of its own, so `BarsArray` holds 2 after a good single-series run (observed on
   8.1.8.2). Single-series + High runs normally.
   **Rollover.** `warnings` carries a `rollover:` entry when the instrument is a dated futures
   contract and the window starts before the date that contract became the front month
   (`MasterInstrument.RolloverCollection`). Observed on 8.1.8.2 with a broker data feed: for a day
   before the rollover, the stored minute history of the December contract held the same prices as
   the September contract's, while a Market Replay recording of that day held the December
   contract's own prices, a calendar spread away. A backtest there measures the contract that was
   the front month on that day. The tool cannot tell which series a provider served; it says when
   the window crosses the line, and `nt_reconcile` against a replay shows the size of the gap.
3. **An unresolvable or continuous-contract instrument.** `instrument` is resolved before the job is
   queued. Unknown name → `400 {"error":"unknown instrument '<name>'"}`. A name that resolves but is
   a continuous-contract reference (no dated expiry — `"ES"`, `"ES ##-##"`) → `400` naming a dated
   contract to use instead. `nt_data_coverage` can still list a continuous name as locally
   "resolved" — that answers a different question (what data exists on disk), not whether a
   backtest can use it.
4. **`output` was `[]` even when Print() output existed** (it does exist, in the events module's
   ring — see below).

## `output` / `outputNote` (key 16 nullability, plus one new key)

`output` is captured by **cursor**, off the same ring `GET /output` reads (`addon/NOTES.md` "Module
seams" — `OutputHub.Lines`, a `Ring<OutputHub.Line>` capped at 20000, owned by the core): the
sequence number is taken when the job starts running and read to "now" when it finishes, filtered to
tab 2 (`PrintTo = OutputTab2`, set in `Configure()`). This replaced an earlier per-job listener that
worked but duplicated what the ring already does.

`output` is `null` — never a false `[]` — when it could not be captured: the hub was never
subscribed, or the run printed enough that the ring's cap evicted some of it before this read. A
genuinely quiet run still reports `output:[]`; that is a true claim there. The new key `outputNote`
(string, nullable, always present) explains a `null` `output`; it is `null` whenever `output` is a
real array.

## `equity` (new key)

An array of `{time, cumulativeNetProfit}`, one entry per **closed** trade, ordered by **exit time**
(not `n`/`TradeNumber` — those can differ once positions overlap). Always present as an array
(`[]` when there are no closed trades), and **not** trimmed by `maxTrades` — trimming would leave
the curve ending on the wrong total. When `includeTradeHistory:false` empties `trades[]`, `equity`
is `[]` too: NinjaTrader keeps the summary numbers but drops the per-trade objects this is computed
from, so there is nothing to build a curve from.

## `summary.sharpe` / `summary.profitFactor` are `null` below 2 trades

`sharpe` reads `1` on a zero-trade run and `0` on a 348-trade run — not a meaningful
number either way with fewer than 2 trades to define a ratio over. Both `sharpe` and `profitFactor`
are now forced `null` when `summary.trades < 2`, regardless of what NinjaTrader itself returns for
that case.

## `warnings`: "connect a data provider"

When the loaded window is shorter than the request (`barsFrom > from` or `barsTo < to`) for a
reason **other** than a Connected Playback connection capping the clock, and no data provider is
connected (`AnyNonSimConnected()` false), `warnings` includes: `"connect a data provider: a
backtest fetches missing bars from the connected provider on demand"`. A backtest fetches missing
bars from whichever adapter is connected; `/data/download` cannot (see `docs/api/data.md`), so this
is the actionable fix, not "wait" or "download".

## Status document — the new `settings` key

`GET /backtest/{id}` returns the 16 keys frozen in `addon/NOTES.md` "Status document v1", unchanged,
then one added key:

| # | Key | Type | Null? | Meaning |
|---|---|---|---|---|
| 17 | `settings` | object | never | the run's effective settings; 10 keys, always present, in the order below |
| 18 | `barsFrom` | string | **null until `done`**, or when the bars could not be read | time of the FIRST bar of the primary series the strategy really saw (NT8-local, `Tm()`) |
| 19 | `barsTo` | string | same | time of the LAST bar really seen |
| 20 | `warnings` | string[] | never (`[]`) | non-empty when the numbers are not for `from..to` — see below |

**`from`/`to` are the request; `barsFrom`/`barsTo` are what ran.** Observed on NinjaTrader 8.1.8.2: while a
Playback connection is Connected, NinjaTrader caps historical data at the replay clock, so two different
requested ranges that both fall after that clock can silently load the same, earlier window and produce
identical trades. The engine cannot be steered (no public end-time control), so the document warns instead. One
warning is written when `barsTo < from`, or `barsFrom > to`, or a Playback connection is Connected and `to` is
later than the replay clock. It says the numbers are for `barsFrom..barsTo`, not `from..to`, and names the cause:
`a Connected Playback connection caps historical data at the replay clock <time>`. Disconnect Playback (the
bridge never does it for you) to backtest dates after the clock. The clock is `PlaybackAdapter.NowEst`
(US Eastern) compared with NT8-local `to`: exact on an Eastern machine, up to the zone offset elsewhere.

**Integer inputs are strict.** A JSON number that is not whole (`5.5`), or NaN/Infinity/out of range, sent to an
integral `[NinjaScriptProperty]` (int, long, short, byte, unsigned forms, enum-as-number) is a `400` naming the
input and the value. `5.0` is accepted and runs as `5`. Before this, `5.5` ran as `6` and echoed `5.5`.

```json
"settings": {
  "fillResolution": "Standard",
  "fillResolutionType": "Minute",
  "fillResolutionValue": 1,
  "slippageTicks": 0,
  "commissionTemplate": "",
  "includeCommission": false,
  "fillLimitOnTouch": false,
  "includeTradeHistory": true,
  "maxTrades": 0,
  "template": null
}
```

- **While `queued`,** `settings` echoes what the request asked for, and a field the request did not
  mention is `null` — "NinjaTrader's own default, not read back yet".
- **For `running`, `done`, `timeout` and `cancelled`,** every field except `template` is the value
  read **back off the configured strategy**, requested or not. `template` is the template file the
  defaults came from, or `null`.
- **`error` is the one state that is not guaranteed to be read back.** The read-back happens only
  after `Configure()`'s own checks pass (`Backtest_VerifySettings` and the earlier
  `IncludeTradeHistoryInBacktest`/`IsTickReplay`/`Account` checks); a job that errors out of one of
  those checks — a rejected stripped setter is exactly what makes `Backtest_VerifySettings` throw —
  still carries the POST's requested echo in `settings`, not what the instance actually had. Treat an
  `error` document's `settings` as "what was asked for", not "what ran".
- `maxTrades` is ours and is echoed as given.
- Why read-back: `IncludeTradeHistoryInBacktest`, `IncludeCommission`, `IsFillLimitOnTouch` and
  `BacktestCommissionTemplate` have **stripped setters** that silently no-op depending on `State`. A
  requested setting that does not take makes the job `state:"error"` with a message naming the
  property, the asked-for value and the value it reads — never a plausible, wrong P&L.
- `/backtests` rows are unchanged (keys 1-12); `settings` is on the full document only.

## MCP tools

| Tool | Maps to |
|---|---|
| `nt_templates(strategy)` | `GET /templates?strategy=` |
| `nt_backtest(strategy, …, template, fill_resolution, fill_resolution_type, fill_resolution_value, slippage_ticks, commission_template, include_commission, fill_limit_on_touch, include_trade_history, max_trades, save_run)` | `POST /backtest` + poll, then the run registry |
| `nt_backtest_status(id)` / `nt_backtests()` / `nt_backtest_cancel(id)` / `nt_strategies()` | unchanged |

`nt_backtest` sends a settings field **only when the caller passed it**, so an absent argument keeps
NinjaTrader's default instead of overwriting it with `0` / `""`. `slippage_ticks`,
`include_commission`, `fill_limit_on_touch` and `include_trade_history` use a `None` sentinel, which
is what keeps "not asked for" apart from an explicit `0.0` / `False`. With `template=` the tool does
not default the dates to the last two days and does not send `chart="first"` — the template's own
dates, instrument and bar period are the point of passing one — but any argument the caller states
explicitly is still sent and still wins.

## Run registry (`save_run`)

`nt_backtest(..., save_run=True)` (the default) saves every result that reaches a terminal state
(`done`, `error`, `timeout` or `cancelled`) to the run registry — see `docs/api/runs.md` for
`nt_runs` / `nt_run` / `nt_run_compare`, and `server/nt8_mcp/runs.py` for the writer. A save failure
never fails the backtest: it appends `"run not saved: <reason>"` to the result's `warnings` instead.
`nt_optimize` / `nt_walkforward` call their inner `nt_backtest`s with `save_run=False` so a sweep of
disposable combos does not fill the registry — only the swept, ranked result belongs there, and that
is each module's own choice, not this one's.
