# NT8Bridge HTTP API (contract between the AddOn and the MCP server)

The AddOn (`addon/NT8Bridge*.cs`) runs inside NinjaTrader 8 and listens on
`http://localhost:7891/`. **It is read-only by default.** No order or account-changing
endpoint exists unless you opt in. Two arming files gate every opt-in module: `ops.enabled` gates
the reduce-only ops module (flatten, a naked-position watchdog, reconnect and a restart CLI) — see
[Ops (opt-in, disarmed by default)](#ops-opt-in-disarmed-by-default-batch-3) — and `orders.enabled`
gates four Simulator/Playback-only modules that share one gate chain: order entry (submit,
bracket, change, cancel, close, reverse — see
[Orders (opt-in, disarmed by default, Simulator only)](#orders-opt-in-disarmed-by-default-simulator-only)),
ATM strategies (see [ATM strategies](#atm-strategies-opt-in-disarmed-by-default-simulator-only)),
strategies on a Simulator account (see
[Strategies on Sim](#strategies-on-sim-opt-in-disarmed-by-default)), and the Playback
seek/speed/run driver (see
[Playback control](#playback-control-opt-in-disarmed-by-default)). Neither arming file arms the
other module's gate chain. Outside these, writes are limited to `reload`, `screenshot`, chart
control (`indicator/add`, `indicator/remove`, `series`, `scroll` — no arming file, no account
touched, see [Chart control](#chart-control-module-chartcontrol-addonnt8bridgechartcontrolcs)),
`compile`/`compile?reload=1`, and the opt-in, flag-gated `data/download`.

All responses are JSON, UTF-8. Errors: HTTP 4xx/5xx with `{"error":"message"}`.
Times are ISO-8601 local NT8 time, `yyyy-MM-ddTHH:mm:ss`, no zone, unless a section says
otherwise (a few fields are US Eastern or UTC by necessity and are called out where they occur).
Every chart-touching handler runs on that chart's dispatcher thread
(`ChartControl.Dispatcher.Invoke`) and must return in well under a second: read, do not draw.

## How the AddOn is put together (for readers of the code, not just the wire)

`addon/NT8Bridge.cs` is the **core**: the HTTP listener, JSON helpers, the dispatcher-hop
helper `Ui<T>()`, the two live-account predicates (`AnyLiveConnected()` /
`AnyNonSimConnected()`), and reflection discovery. Every other `addon/NT8Bridge.*.cs` file is a
**module** — a `partial class NT8Bridge` that the core discovers by reflection at `Start()`:
a `private static string Route_<Module>(method, seg, q, body, ref status)` method (returns
`null` when a request is not its own — only the core emits the 404) and optional
`Start_<Module>()` / `Stop_<Module>()` lifecycle hooks. **Precedence: the discovered `Route_*`
table runs first, in name order, then the core's own remaining handlers, then 404.**

**The core's own handlers are exactly these five, and nothing else:**
`GET /health`, `GET /windows`, `GET /log`, `GET /compat`, `GET /ntstatus`. Every other path in
this document — including `GET /output/window`, which reads like a core concern but is
`Route_Charts`' — is a discovered module. Verified by reading `NT8Bridge.cs`'s `Route()`
method directly (`addon/NT8Bridge.cs`): it matches exactly `health`, `windows`, `log`, `compat`,
`ntstatus` on a length-1 `GET` path, after the module table has already had first refusal.

| Module file | `Route_*` | Endpoints |
|---|---|---|
| `NT8Bridge.Charts.cs` | `Route_Charts` | `/charts`, `/chart/{id}[/bars\|/indicators\|/drawings\|/reload\|/screenshot]`, `/output/window` |
| `NT8Bridge.Account.cs` | `Route_Account` | `/account`, `/executions`, `/performance` |
| `NT8Bridge.Backtest.cs` | `Route_Backtest` | `/strategies`, `/templates`, `/backtest`, `/backtests`, `/backtest/{id}` |
| `NT8Bridge.Compile.cs` | `Route_Compile` | `/compile`, `/compile?reload=1` |
| `NT8Bridge.Events.cs` | `Route_Events` | `/output`, `/nt-log` |
| `NT8Bridge.Feeds.cs` | `Route_Feeds` | `/feedhealth`, `/connections` |
| `NT8Bridge.Data.cs` | `Route_Data` | `/data/coverage`, `/data/download[/{id}]` |
| `NT8Bridge.Workspace.cs` | `Route_Workspace` | `/workspace`, `/strategies/running`, `/screenshot` |
| `NT8Bridge.Playback.cs` | `Route_Playback` | `/playback` (read), `/playback/seek`, `/playback/speed`, `/playback/run[/{id}]` (opt-in, disarmed by default) |
| `NT8Bridge.ChartControl.cs` | `Route_ChartControl` | `/chart/{id}/indicator/add`, `/chart/{id}/indicator/remove`, `/chart/{id}/series`, `/chart/{id}/scroll` |
| `NT8BridgeOps.cs` | `Route_Ops` | `/ops/status`, `/ops/flatten`, `/ops/reconnect` (opt-in, disarmed by default) |
| `NT8BridgeOrders.cs` | `Route_Orders` | `/orders/status`, `/orders/{submit,bracket,change,cancel,close,reverse}` (opt-in, disarmed by default, Simulator only) |
| `NT8BridgeAtm.cs` | `Route_Atm` | `/atm/templates`, `/atm/status`, `/atm/{start,close,change}` (opt-in, disarmed by default, Simulator only) |
| `NT8BridgeStrategyRun.cs` | `Route_StrategyRun` | `/strategy/{start,stop,running}` (opt-in, disarmed by default, Simulator only) |

`nt_optimize`, `nt_walkforward` and `nt_report` add no AddOn file and no HTTP endpoint: they
are pure Python over `POST /backtest` + `GET /backtest/{id}` (see
[Optimize / walk-forward / report](#optimize--walk-forward--report-python-only-no-addon-file)).
`nt_nrd_export` likewise adds no endpoint — it decodes `.nrd` files on disk with no NinjaTrader
involvement.

## Chart ids

`id` is stable while the chart window is open: `"c<N>"` where N is a counter the AddOn
assigns the first time it sees a window (`OnWindowCreated`, and also `State.Configure`, for
reload survival). Clients call `/charts` first. Any endpoint that takes `{id}` also
accepts `first` (the first chart found).

---

## Core endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | see below |
| GET | `/windows` | `[{"title":..,"kind":"Chart"\|"ControlCenter"\|"SuperDom"\|"Output"\|"NinjaScriptEditor"\|"Other","owned":bool,"hwnd":..,"left":..,"top":..,"width":..,"height":..,"isMinimized":..,"screen":..}]` |
| GET | `/log?n=100` | `{"lines":[..]}` the AddOn's own ring-buffer log (requests, errors, ops audit mirror) |
| GET | `/compat` | the reflection-resolution table, `{"<Member>":{"resolved":bool,"detail":"..."}, ...}` — what an NT8 upgrade broke, at a glance |
| GET | `/ntstatus` | is the code that answered current, see below |

### `GET /health`

```json
{ "ok": true, "addonVersion": "1.4.0", "nt8Version": "8.1.8.2", "startedAt": "2026-09-18T09:00:00",
  "connections": [{"name":"Sim101 feed","status":"Connected","provider":"Simulator","canManageOrders":false}],
  "charts": 1, "anyLive": false, "anyNonSim": true, "standingModal": null,
  "pid": 12345, "processStartUtc": "2026-09-18T08:59:50Z",
  "assemblyName": "NinjaTrader.Custom", "assemblyLocation": "C:\\...\\tmp\\<guid>.dll",
  "assemblyBuiltUtc": "2026-09-18T08:59:00Z", "moduleCount": 10,
  "reflection": {"...": "the same table as /compat"} }
```

| Key | Notes |
|---|---|
| `addonVersion` | the `Version` constant in `addon/NT8Bridge.cs` |
| `startedAt` | set once, in `TryBind()`, when a process binds `:7891`. **The interlock for "did a reload really happen"**: a new instance always reports a new `startedAt`; an old listener that never restarted never does. Compare this, never `assemblyBuiltUtc`, across a reload (see `docs/api/compile.md`, *Truthfulness*) |
| `assemblyBuiltUtc` | mtime of the file the running assembly loaded from. On a cold start that file **is** `bin\Custom\NinjaTrader.Custom.dll` — the exact file a reload's emit overwrites — so it is not safe as a reload interlock by itself |
| `anyLive` | `AnyLiveConnected()` (order-routing risk): any Connected connection whose `Options` is null, or whose provider is neither Simulator nor Playback and `CanManageOrders` |
| `anyNonSim` | `AnyNonSimConnected()` (data/metering awareness): the same without the `CanManageOrders` term |
| `standingModal` | title of an open `MessageBox`-type window, walking owned windows too, or `null`. Not behind `Ui()` — a standing modal is exactly when the dispatcher is blocked |
| `connections[]` | union is **not** taken here (see `GET /connections` for that); this is the original per-connection snapshot, kept for compatibility, now with `provider`/`canManageOrders` added |
| both live predicates | answer `true` on a read failure — "could not tell" is never a licence to treat the machine as safe |

### `GET /ntstatus`

```json
{ "runningAssembly": "C:\\Users\\...\\tmp\\<guid>.dll", "assemblyBuiltUtc": "...",
  "assemblyBuiltFrom": "file" | "loadTime",
  "newestSource": "...\\bin\\Custom\\Strategies\\SampleMACrossOver.cs", "newestSourceUtc": "...",
  "sourcesScanned": 349, "sourcesNewerThanRunningCode": false, "verdict": "current", "scanError": null }
```

Walks every `.cs` under `bin\Custom` — slow on a big tree, deliberately kept off `/health` and
off every hot path. **This check is self-referential**: if the old listener still owns port
7891, the *old* assembly answers `/ntstatus` and reports its own build time truthfully —
self-consistent and wrong. The out-of-band half of the interlock (comparing this against the
`.cs`/`.dll` mtimes read directly from disk, not through the AddOn) lives in the Python client,
not here.

### `GET /compat`

The reflection-resolution table, one row per reflective member this AddOn ever binds, resolved once at
`Start()` (and, for a few flag-backed rows such as the ops flags, refreshed on every request of
that module). `{"resolved": false, ...}` on any row is what an NT8 upgrade looks like from here:
the owning endpoint reports `null`/`false`/an explicit note for that feature, never a silent
empty list. Every module section below names the `/compat` rows it publishes.

---

## Chart endpoints (module `charts`, `addon/NT8Bridge.Charts.cs`)

Unchanged from v1.1 except as noted; the output-window scrape moved here from the core.

| Method | Path | Returns |
|---|---|---|
| GET | `/charts` | `[{"id":"c1","title":..,"instrument":"ES 12-26","period":"5 Minute","barsCount":N,"indicators":[..],"strategies":[]}]` |
| GET | `/chart/{id}` | chart state, see below |
| GET | `/chart/{id}/bars?n=50` | `[{"time":t,"o":..,"h":..,"l":..,"c":..,"v":..}]` last n bars of the primary series, oldest first |
| GET | `/chart/{id}/indicators?name=EMA&n=20` | see below. `name` optional (substring, case-insensitive), `n` = values per plot (default 1) |
| GET | `/chart/{id}/drawings` | `[{"tag":"..","type":"Line"\|"Ray"\|"Rectangle"\|"Text"\|..,"owner":"MyStrategy"\|"user","anchors":[{"time":t,"price":p}],"text":".."}]` |
| POST | `/chart/{id}/reload` | `{"ok":true}` — same as right-click, Reload NinjaScript, for **this chart's own NinjaScript instances only** (not the assembly — that is `/compile?reload=1`) |
| POST | `/chart/{id}/screenshot` body `{"path":"C:/x.png"}` (optional) | `{"ok":true,"path":..,"width":..,"height":..}` PNG of the chart's **canvas** via `RenderTargetBitmap`; default path `%TEMP%/nt8bridge/c1.png`. For the chart's whole window (frame, toolbar, Chart Trader included) use `POST /screenshot` in the [workspace module](#workspace-module-addonnt8bridgeworkspacecs) instead |
| GET | `/output/window?n=200` | scrape of the NinjaScript Output **window's** visual tree; `{"lines":[..]}`, or `{"lines":[],"note":"Output window not open"}`. This is the pre-1.2 `/output` behaviour, kept as the fallback for lines printed **before** the ring (`GET /output`, events module) started — see `docs/api/events.md` |

### `/chart/{id}` state

```json
{
  "id": "c1", "title": "ES 12-26 (5 Min)", "instrument": "ES 12-26", "period": "5 Minute",
  "tickSize": 0.25, "barsCount": 2000, "firstTime": t, "lastTime": t,
  "visibleFrom": t, "visibleTo": t, "lastPrice": 6500.25,
  "panels": [
    {"index": 0, "indicators": ["EMA", "VWAP"]},
    {"index": 1, "indicators": ["Bollinger"]}
  ],
  "strategies": [{"name": "MyStrat", "state": "Realtime", "position": {"qty": 0, "avg": 0}}]
}
```

### `/chart/{id}/indicators`

```json
[
  {
    "name": "EMA", "displayName": "EMA(ES 12-26, 20)", "panel": 0,
    "inputs": {"Lookback": 20, "Palette": "Twilight"},
    "plots": [
      {"name": "Delta", "values": [{"time": t, "value": 12.0}, ...]}
    ],
    "drawings": 14
  }
]
```

`inputs` = every property carrying `[NinjaScriptProperty]`, value as JSON scalar (enum as its
name). `values` = last `n` bars, oldest first, `NaN`/unset written as `null`.

**MCP:** `nt_charts()`, `nt_chart_state(chart)`, `nt_bars(chart, n)`, `nt_indicators(chart, name, n)`,
`nt_drawings(chart)`, `nt_chart_reload(chart)` (renamed from `nt_reload` in this release — the
name now distinguishes it from `nt_reload_assembly`, which swaps the assembly), `nt_screenshot(chart)`,
`nt_output_window(n)` (renamed from `nt_output`, which now names the ring tool below).

---

## Backtests (module `backtestplus`, `addon/NT8Bridge.Backtest.cs`)

Runs a strategy from the compiled `NinjaTrader.Custom` assembly headlessly, the way the Strategy
Analyzer does, and returns its `SystemPerformance`. Backtests run on the **Backtest** account
only; any other account name is a 400. No order ever reaches a Sim or live account through this
API.

| Method | Path | Returns |
|---|---|---|
| GET | `/strategies` | `[{"name":"SampleMACrossOver","fullName":"NinjaTrader.NinjaScript.Strategies.SampleMACrossOver","inputs":{"Fast":{"type":"Int32","default":10},...}}]` every non-abstract `StrategyBase` subclass, `[NinjaScriptProperty]` inputs with their `SetDefaults` value |
| GET | `/templates?strategy=<name>` | `{"strategy":..,"folder":..,"templates":[..]}` the strategy templates NinjaTrader has saved for that strategy |
| POST | `/backtest` | body below → `202 {"id":"b1","state":"queued"}` |
| GET | `/backtests` | rows: keys 1-12 of the status document, oldest first |
| GET | `/backtest/{id}` | the status document |
| DELETE | `/backtest/{id}` | `{"ok":true}` terminates a running backtest (SetState Terminated) and drops the record |

### `GET /templates?strategy=SampleMACrossOver`

```json
{"strategy": "SampleMACrossOver",
 "folder": "C:\\Users\\...\\NinjaTrader 8\\templates\\Strategy\\SampleMACrossOver",
 "templates": ["Base", "ES-5m"]}
```

`strategy` required (short or full type name); missing → 400, unknown → 400 listing `/strategies`.
`templates` = the `*.xml` names in that folder, sorted case-insensitively; `[]` means the folder
exists and holds none. `folder` comes from NinjaTrader's own `StrategyTemplate.GetTemplateFolder`,
never assembled by a naming rule. **Side effect, verified live on 8.1.8.2**: calling this makes
NinjaTrader create that (empty) template folder if it did not already exist — so the common answer
for a strategy that never had a template saved is `templates: []`, not `folder:null`.

### `POST /backtest` body

```json
{
  "strategy": "SampleMACrossOver",
  "chart": "first",
  "instrument": "ES 12-26",
  "barsPeriod": {"type": "Minute", "value": 5},
  "from": "2026-09-15",
  "to": "2026-09-17",
  "tickReplay": true,
  "inputs": {"NearTerm": 3, "StopTicks": 8},
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

- `strategy` (required): short or full type name.
- `chart` (optional): copy `Instrument`, `BarsPeriod` (incl. custom bar types) and
  `TradingHours` from that open chart. Explicit `instrument`/`barsPeriod` override the
  copied ones. Without `chart` and without `template`, both are required.
- `from`/`to` (required unless a template supplies them): dates, `to` inclusive (end of that
  day). ISO date or datetime. Refused with a 400 if inverted or a year outside
  1990..2090 — applied **after** a template merges in its own dates.
- `tickReplay` (default false): sets `IsTickReplay`. **Not** taken from a template — pass it
  explicitly.
- `inputs`: `[NinjaScriptProperty]` overrides by property name; unknown name → 400 before
  anything runs. A **fractional value sent to an integral input is a 400** naming the input and
  the value (`5.0` is accepted and runs as `5`; before this fix `5.5` silently ran as `6` and
  echoed `5.5`).
- `account`: only `"Backtest"` (default). Anything else → 400.
- `template` (string): a bare name resolves in the strategy's own template folder; a
  value containing `\`, `/` or `:` is a path; `.xml` is appended when absent. Supplies
  **defaults only** — precedence is explicit body field, then `chart`, then `template`, then the
  existing 400 if instrument/barsPeriod/from/to are still missing. The `inputs` key of the
  status document echoes the merged map. Unlike the Strategy Analyzer's own template route, the
  instrument **does** travel with a template here, because it is read off the restored instance.
- `fillResolution` (`"Standard"`\|`"High"`), `fillResolutionType`, `fillResolutionValue` (≥1),
  `slippageTicks` (≥0), `commissionTemplate`, `includeCommission`, `fillLimitOnTouch`,
  `includeTradeHistory` (default true — `false` empties `trades[]` but leaves `summary`
  unchanged), `maxTrades` (default 0 = unlimited; a client-side trim of `trades[]` — `summary`
  always counts every trade) — see `docs/api/backtest.md` for the full member table.
  **`commissionTemplate` implies `includeCommission:true`** unless the body says otherwise, and
  an unknown template name is a 400 listing the known ones (NinjaTrader itself would accept any
  string, read it back unchanged, and silently charge its default — the read-back cannot catch
  this, so the AddOn validates the name against the template folder up front).
- **Hard refusal**: `tickReplay:true` with `fillResolution:"High"` → 400 (NinjaTrader itself
  refuses the combination).

Validation happens synchronously (strategy exists, inputs exist, chart exists, dates parse,
every stripped-setter field will read back what was asked); the run itself is queued on a
background worker (one backtest at a time; further POSTs queue — this worker is exclusive
to `/backtest` and is never shared with `/data/download` or `nt_optimize`). Cap a run at 15
minutes, then terminate it and mark `"state":"timeout"`.

### `GET /backtest/{id}` status document

The 16 keys frozen in `addon/NOTES.md` "Status document v1" (id, state, strategy, instrument,
period, from, to, tickReplay, startedAt, finishedAt, seconds, error, inputs, summary, trades,
output — see that file for the exact null rules), plus, added in this release:

| # | Key | Type | Null? | Meaning |
|---|---|---|---|---|
| 17 | `settings` | object | never | the run's effective settings, 10 keys, always present, see below |
| 18 | `barsFrom` | string | **null until `done`**, or if the bars could not be read | time of the FIRST bar of the primary series the strategy really saw |
| 19 | `barsTo` | string | same | time of the LAST bar really seen |
| 20 | `warnings` | string[] | never (`[]`) | non-empty when the numbers are not for `from..to` — see below |

```json
{
  "id": "b1", "state": "queued|running|done|error|timeout|cancelled",
  "strategy": "SampleMACrossOver", "instrument": "ES 12-26", "period": "2 Renko",
  "from": "2026-09-15T00:00:00", "to": "2026-09-17T23:59:59", "tickReplay": true,
  "inputs": {"NearTerm": 3, ...},
  "startedAt": t, "finishedAt": t, "seconds": 84.2, "error": null,
  "summary": { "trades": 12, "winners": 7, "losers": 5, "winRate": 0.583,
    "netProfit": 312.5, "grossProfit": 812.5, "grossLoss": -500.0, "profitFactor": 1.625,
    "commission": 0.0, "maxDrawdown": -187.5, "avgTrade": 26.04, "avgWinner": 116.07,
    "avgLoser": -100.0, "largestWinner": 200.0, "largestLoser": -100.0, "avgMae": 45.0,
    "avgMfe": 120.0, "avgBarsInTrade": 6.5, "sharpe": 0.0, "maxConsecWinners": 3,
    "maxConsecLosers": 2 },
  "trades": [ {"n": 1, "side": "Long", "qty": 1, "entryName": "RS-L", "exitName": "Target",
     "entryTime": t, "exitTime": t, "entryPrice": 7601.25, "exitPrice": 7603.25,
     "pnl": 100.0, "pnlPoints": 2.0, "mae": 0.5, "mfe": 2.25, "bars": 4} ],
  "output": ["..."],
  "settings": { "fillResolution": "Standard", "fillResolutionType": "Minute",
    "fillResolutionValue": 1, "slippageTicks": 0, "commissionTemplate": "",
    "includeCommission": false, "fillLimitOnTouch": false, "includeTradeHistory": true,
    "maxTrades": 0, "template": null },
  "barsFrom": "2026-09-15T09:30:00", "barsTo": "2026-09-17T16:00:00", "warnings": []
}
```

`summary` comes from `SystemPerformance.AllTrades.TradesPerformance` (`Currency` figures);
`trades` from `SystemPerformance.AllTrades` in order. `output` is what the strategy printed
during the run if capturable, else `[]`. `summary`/`trades` are `null` until `state` is `done`.

**`from`/`to` are the request; `barsFrom`/`barsTo` are what really ran.** A Connected Playback
connection caps historical data at the replay clock — NinjaTrader gives no public way around
it — so a run can silently get fewer/different bars than asked. `warnings` says so and names
the cause when `barsTo < from`, `barsFrom > to`, or a Connected Playback connection's clock is
earlier than `to`.

**While `queued`,** `settings` echoes the request, with an unmentioned field `null` ("not read
back yet"). **For `running`/`done`/`timeout`/`cancelled`,** every field except `template` is
read back off the configured strategy, because four of these properties
(`IncludeTradeHistoryInBacktest`, `IncludeCommission`, `IsFillLimitOnTouch`,
`BacktestCommissionTemplate`) have stripped setters that can silently no-op — a setting that
does not take makes the job `state:"error"` naming the property, never a plausible wrong P&L.
**`error` is the one state whose `settings` is not guaranteed read back** — treat it as "what
was asked for", not "what ran".

**MCP:** `nt_strategies()`, `nt_templates(strategy)`, `nt_backtest(strategy, chart, instrument,
bars_period, from_date, to_date, tick_replay, inputs, account, template, fill_resolution,
fill_resolution_type, fill_resolution_value, slippage_ticks, commission_template,
include_commission, fill_limit_on_touch, include_trade_history, max_trades)`,
`nt_backtest_status(id)`, `nt_backtests()`, `nt_backtest_cancel(id)`. `nt_backtest` sends a
settings field only when the caller passed it, so an absent argument keeps NinjaTrader's
default instead of overwriting it with `0`/`""`.

---

## Compile (module `compile`, `addon/NT8Bridge.Compile.cs`)

Runs NinjaTrader's own compiler. Replaces the two worst properties of the old F5-only path: no
NinjaScript Editor window is needed, and a failure comes back as
`{file,line,column,code,message}` instead of a screenshot of the editor.

| Method | Path | Returns |
|---|---|---|
| POST | `/compile` | check-only: validates the tree, emits nothing, `assemblyReloaded` always `false` |
| POST | `/compile?reload=1` body `{"force":false}` | the same result, but NinjaTrader emits and **swaps** the assembly exactly as F5 does. `409` while `AnyLiveConnected()` unless `{"force":true}` |

`GET /compile` is not handled (core 404). Both forms take an optional JSON body; send
`Content-Length: 0` for an empty one (`HttpListener` answers 411 without it). The compiler
always builds the **whole** `bin\Custom` tree — install your files first.

```json
{ "ok": false, "assemblyReloaded": false, "reloadRequested": false, "checkCompileOnly": true,
  "seconds": 24.3,
  "errors": [{"file":"C:\\...\\Strategies\\SampleMACrossOver.cs","line":184,"column":13,
              "code":"CS0103","message":"The name 'foo' does not exist in the current context"}],
  "warnings": [{"file":"...","line":91,"column":25,"code":"CS0168","message":"..."}],
  "warningCount": 17, "warningsSuppressed": 113779,
  "assemblyBuiltUtc": "2026-09-18T10:00:00Z", "startedAt": "2026-09-18T12:00:00",
  "compiledAtUtc": "2026-09-18T12:41:07Z" }
```

`line`/`column` are 1-based. `warningCount` is every real warning; `warnings` lists at most the
first 200. `warningsSuppressed` counts `CS1701`/`CS1702` assembly-unification notices (113 779
on a clean 8.1.8.2 tree) without listing them — NinjaTrader's own editor never shows them
either. `Hidden`/`Info` diagnostics are dropped.

Refusals: `400` bad JSON / `force` not boolean; `409` `?reload=1` while `AnyLiveConnected()`
(body carries `"anyLive":true`), or a compile is already running (one at a time, via a
`Monitor.TryEnter` gate, never queued); `500` the compiler returned no readable result; `503`
`NinjaTrader.Code.Compiler.Compile` did not resolve (see `/compat`).

**Truthfulness.** `assemblyReloaded` is a **claim**: the process writing the response is the
pre-swap assembly, so it cannot observe its own replacement. The response also carries
`assemblyBuiltUtc`/`startedAt` of the assembly that answered; a client must compare `/health`'s
`startedAt` **only**, before and after, to verify a reload really happened —
`compile_via_addon(reload=True)` does exactly this on the Python side and reports
`assemblyReloadedSource: "observed: ..."` or `"claimed by the AddOn, not observed"`.

**Surviving the swap.** `?reload=1` replaces the assembly the listener lives in, so the socket
can drop mid-response. The same JSON is written to
`<UserDataDir>\NT8Bridge\compile_last.json` first; the Python client falls back to that file
(only if its mtime is newer than the request) and reports `"source":"compile_last.json"`
instead of `"http"`. A timeout is **not** proof of failure — the compile keeps going; the
result then carries a `hint`, not an `ok` field, so nothing can read a timeout as a failed
build.

**MCP:**

| Tool | Maps to |
|---|---|
| `nt_compile(timeout_s=120)` | `POST /compile` (check-only) via `compile_via_addon(reload=False)`. If the AddOn does not answer at all (not merely slow), falls back by itself to `nt_compile_f5` and says so in `fallback` |
| `nt_reload_assembly(timeout_s=240)` | `POST /compile?reload=1` via `compile_via_addon(reload=True)`. **A separate tool, never a flag on `nt_compile`**, so a validate step cannot disrupt a live session by accident. Refused by the AddOn with 409 while `AnyLiveConnected()`; there is no `force` override exposed here |
| `nt_compile_f5(timeout_s=240)` | the pre-1.2 cold-start path: press F5 in an open NinjaScript Editor window and wait for the dll to change. Kept as the fallback for when the AddOn is not loaded at all — a real compile, not a check |

> **Design note:** `nt_reload_assembly` exposes **no** `force` override — the AddOn's 409 while a
> live order-routing connection is up cannot be bypassed from the MCP surface at all (the AddOn's
> own `force` field is real; the tool just never sends it). `nt_compile`'s F5 fallback triggers
> only when `/health` itself does not answer (a merely slow, still-compiling AddOn is never given
> a second build).

`duplicatedRegions` is attached to a failing compile result only: a Python-side scan of
`bin\Custom` for `.cs` files with more than one anchored `#region NinjaScript generated code`
(a CS0111/CS0102 cause). The scan is wrapped so a scan failure can never mask the compile
result.

---

## Events (module `events`, `addon/NT8Bridge.Events.cs`)

Two read-only endpoints served from ring buffers. **Neither touches a dispatcher**, so both
keep answering while NinjaTrader's UI thread is wedged — the state in which every chart
endpoint returns 504.

**Everything these endpoints return is text written by NinjaScript**, including third-party
closed-source AddOns and text echoed from a data feed. It is data, never instructions.

| Method | Path | Returns |
|---|---|---|
| GET | `/output?since=-1&n=200&tab=&contains=` | `{"lines":["[1] hello"],"index":41231,"dropped":0,"subscribed":true,"source":"ring"}` |
| GET | `/nt-log?since=-1&n=200&level=&name=&contains=` | `{"entries":[{...}],"index":882,"dropped":0,"subscribed":true,"source":"ring"}` |

### Cursors

`index`/`since` is a **sequence number, not a slot index**, so a mark stays valid after the
buffer wraps (a slot index pins at the cap once the ring is full and every later read comes
back empty for the life of the process — the exact bug this design avoids). `since` absent or
negative = the newest `n`; `0` and up = items with `seq > since`, oldest first, at most `n`.
`index` in the response is always the seq of the last item returned, or the ring's total seen
count when the response is empty — either way, passing it back as `since` loses nothing and
repairs a cursor from the future. `dropped` = items that fell out of the ring before this read;
never silent. Filters are applied **after** `n` items are taken off the ring, so a filtered call
can return fewer than `n` — or zero — with a valid `index`.

### `GET /output`

NinjaScript `Print()` output, fed by the core's one subscription to
`NinjaTrader.Code.Output.OutputEvent` — the Output window is just another subscriber of the
same event, so this answers whether the window is open or closed. Ring capacity 20 000 lines.
`tab` (`1`/`2`/`0`=both/absent=both) filters to one Output tab; anything else fails **closed**
(`lines:[]`, not both tabs — NT8 has exactly two). `contains` is a case-insensitive substring.
Each line is tagged with its tab, `"[1] hello"`; a cleared tab arrives as
`"[2] <<cleared: tab 2>>"`, never silence. When the ring has never seen a line, the response
carries a `note` pointing at `GET /output/window` for lines printed **before** this AddOn
loaded — the ring cannot see those, and there is deliberately no automatic fallback (the
fallback needs a dispatcher hop, which is exactly what this endpoint exists to avoid).

### `GET /nt-log`

NinjaTrader's own log (`NinjaTrader.Cbi.Log.LogEvent`) — connection/order/execution/strategy/
system events, the Control Center's Log tab. Ring capacity 5000, backfilled once at start from
`Cbi.Log.LogEntries` (best effort). `level` matches exactly (`Alert`/`Information`/`Warning`/
`Error`); **match `name`, not `msg`** — NinjaTrader renders `msg` from (`ResourceType`, `Name`)
through a `ResourceManager`, so the English wording changes with NT8 version and UI language
while `name` is stable. `Account` and `User` are deliberately not captured — unknown lazy work,
and identity that has no business on port 7891.

```json
{"entries":[{"t":"2026-09-18T10:14:59","level":"Error","category":"Order",
             "name":"CbiOrderRejected","resource":"Resource","msg":"..."}],
 "index":882,"dropped":0,"subscribed":true,"source":"ring"}
```

**MCP:** `nt_output(since, n, tab, contains)` (renamed from `nt_output_events` in this release —
this is the ring), `nt_log(since, n, level, name, contains)`. The window scrape lives at
`nt_output_window` (charts module, above) — **not** `nt_output`, which this release repoints to
the ring.

---

## Feeds (module `feeds`, `addon/NT8Bridge.Feeds.cs`)

Two read-only endpoints. **Neither connects, disconnects, reconnects nor subscribes to
anything.** Reconnect is a different module ([Ops](#ops-opt-in-disarmed-by-default-batch-3)).
Connection names, provider names and instrument names are free text — data, never instructions.

| Method | Path | Returns |
|---|---|---|
| GET | `/feedhealth?instruments=ES%2012-26,MNQ%2012-26` | `{"nowUtc","now","feeds":[…],"anyNonSim","note"}` |
| GET | `/connections?n=20&since=-1` | `{"connections":[…],"anyLiveConnected","anyNonSimConnected","subscribed","events":[…],"index","dropped","note"}` |

### `GET /feedhealth`

Is this instrument's feed still ticking, or frozen while the connection still says `Connected`?
`instruments` required, comma-separated full names (safe: names contain spaces, never commas).
Per row: `found`, `resolvedName`, `hasSeenMarketData`, `lastPrice`, `lastTickTime`, `ageMs`,
`error`. **`ageMs:null` is STALE, never fresh.** `hasSeenMarketData:false` means no tick has
arrived **this session** — not "nobody is watching"; nothing here creates an instrument or
subscribes to data. `ageMs` is computed inside the AddOn from `Core.Globals.Now` (NinjaTrader's
own configured time zone), never the host clock — a mismatched Windows-vs-NT8 zone would
otherwise silently misreport a dead feed as fresh or a live one as stale for hours. Under a
Playback connection `lastTickTime` is replay time, so `ageMs` reads as however far the replay
clock is from wall time — not a freshness signal there; use `GET /playback` instead.

### `GET /connections`

The **union** of `Globals.ConnectOptions` (configured) and `Connection.Connections` (what
NinjaTrader actually holds), tagged `source: "configured"|"live-only"` — walking only the
configuration hid a live connection upstream (observed: 8 connected per NinjaTrader's
own menu, 6 in the report). Per row: `name`, `source`, `provider`, `canManageOrders`, `status`,
`priceStatus`, `connected`, `dropClass`, `inadvertentlyDropped`, `live` (order-routing),
`nonSim` (data). **Judge live-ness by `provider`/`canManageOrders`, never by `name`.**

`dropClass` (`connected`\|`inadvertent`\|`user`\|`failed`\|`null`) is decided **only at the
moment a transition is witnessed** — subscribed via `Connection.ConnectionStatusUpdate` — and is
`null` for any connection already down when this assembly loaded (a known boundary, by
construction, not a bug). `events[]` is the last `n` witnessed transitions off a 500-entry ring,
same sequence-number cursor convention as `/output`/`/nt-log`.

**MCP:** `nt_feedhealth(instruments)`, `nt_connections(n, since)`.

---

## Data store (module `data`, `addon/NT8Bridge.Data.cs`)

Dates on this module's wire are `YYYYMMDD`, not ISO. Everything here does file I/O on an
HttpListener thread — no dispatcher hop, no `Draw.*`, no order/position/account touch anywhere.

| Method | Path | Returns |
|---|---|---|
| GET | `/data/coverage?instrument=ES%2012-26&kind=&from=&to=` | what is on disk, per store — pre-flight only, presence not completeness |
| POST | `/data/download` | `202 {"id":"d1","state":"queued",...}` — **flag-gated**, below |
| GET | `/data/download/{id}` | the job document |
| DELETE | `/data/download/{id}` | `{"ok":true,"state":"cancelled"}` |

### `GET /data/coverage`

`instrument` required; `kind` (`tick`\|`minute`\|`day`\|`replay`, default all four); `from`/`to`
(`YYYYMMDD`) narrow the *analysis* lists, not the scan. `scanned:false` never means "empty" — a
folder that does not exist reports `scanned:false, days:null`. `granularity` differs per store:
`tick`/`minute` are per-hour/per-day `.ncd` files, **`day` is per-YEAR**, `replay` is per-day
`.nrd`. `alsoScanned` always includes the continuous contract name (`ES 12-26` → `ES ##-##`) —
Market Replay recordings can live only under the continuous name even when the front month has
every tick file. `missingWeekdays`, `daysLackingBidAsk`, `thinDays` are hints from
`analysisStore` (the first scanned, non-empty, non-year-granular store), never verdicts.

### `POST /data/download`

**The one endpoint outside the ops module that writes NinjaTrader's own data store**
(`db\replay`, `db\tick`, `db\minute`, `db\day`) and spends the data provider's bandwidth. It
touches no order, position or account.

```json
{"instrument":"ES 12-26","from":"20260901","to":"20260910",
 "kinds":["replay"], "types":["Last","Bid","Ask"], "overwrite":false,"big":false}
→ 202 {"id":"d1","state":"queued","days":10,"anyLive":false,"anyNonSim":true}
```

**No arming file.** A download moves no money, so it is not an opt-in module. Historical
tick / minute / day data is fetched with the bars request a backtest makes (merge policy
`DoNotMerge`), so it works on a broker data feed as well as on NinjaTrader's own data service;
each downloaded day names the route that served it. `GET /data/probe?instrument=&kind=` (job
model, poll `GET /data/probe/{id}`) reports how far back the connected provider serves, counting
only days it saw bars for. `GET /data/coverage` also carries a `cache` section: the series
NinjaTrader's bars cache holds for the instrument's contract chain. Full contract:
`docs/api/data.md`.

**The guards.** A download routes no order, so the order-routing predicate does not gate it.
Instead:

| Guard | Role | Effect |
|---|---|---|
| `Data_Exposure()` | **the refusal** — an open position or working order on any account but Backtest | `409`, `"exposure":true`, names the account |
| `AnyNonSimConnected()` | **the precondition** — a real data provider must be connected | `409` while false |

Both are re-checked before every date of a running job. Other guards: range > 10 days
needs `{"big":true}` (raises, does not remove, the cap — >400 days refused regardless);
a range check on `from`/`to`; its own worker thread and job map so it never starves a
backtest; Saturday is skipped; **the current and any future day (computed in
`America/New_York`) are never downloaded** and land in `skippedCurrent`; a date already on disk
is `skipped` unless `overwrite:true`; a callback reporting success with nothing on disk is
reported as `failed`, never as a silent download.

`downloaded`/`skipped`/`skippedCurrent`/`failed` are four separate buckets on the job document,
plus `anyLive`/`anyNonSim`/`flag` echoed for information. `state`:
`queued`\|`running`\|`done`\|`error`\|`cancelled`\|`refused` (a live connection or exposure
appearing mid-run stops the job at `refused`).

### `nt_nrd_export` — no NinjaTrader involved

Pure local Python (`server/nt8_mcp/nrd_offline.py`, MIT, near-verbatim from cli-nt-bridge — see
`NOTICE`). No HTTP endpoint. Decodes `.nrd` files under `db\replay` to per-day Parquet, with a
header integrity cross-check (a corrupt file writes nothing), truncation salvage, and atomic
commit (a 0-row result is never committed). `numpy`/`pyarrow` are optional, imported lazily.

**MCP:** `nt_data_coverage(instrument, kind, from_date, to_date)`, `nt_data_download(instrument,
from_date, to_date, kinds, types, overwrite, big)`, `nt_data_download_status(id)`,
`nt_data_download_cancel(id)`, `nt_nrd_export(instrument_glob, out_dir, levels, force,
replay_dir)`.

---

## Accounts (module `accounts`, `addon/NT8Bridge.Account.cs`)

**Read only.** No endpoint here submits, modifies or cancels an order, opens or fronts a
window, or writes a file.

| Method | Path | Returns |
|---|---|---|
| GET | `/account?name=Sim101` | one account, or every account as a list when `name` is absent |
| GET | `/executions?account=&from=&to=&instrument=&n=200` | raw fills, oldest first |
| GET | `/performance?account=&from=&to=&instrument=&n=5000` | round-trip trades + a `summary` shaped exactly like the backtest status document |

### `GET /account`

```json
{"name":"Sim101","cash":100000,"realized":0,"unrealized":62.5,
 "positions":[{"instrument":"ES 12-26","qty":1,"avg":5811.5,"unrealized":62.5,"hasSeenMarketData":true}],
 "orders":[{"id":"o-1","instrument":"ES 12-26","action":"Buy","type":"Limit","qty":1,"price":5800,"state":"Working"}]}
```

`positions[].unrealized` (`GetUnrealizedProfitLoss`, no market-data subscription needed) and
`positions[].hasSeenMarketData` are new in this release. `orders[]` now includes
`CancelSubmitted` (still live at the broker). A failed read is `null`, never `0`/`""`; one bad
row becomes `{"error":...}` and the rest of the list survives. Name matching is
`OrdinalIgnoreCase`. Unknown `name` → `404` (was a bare 500).

### `GET /executions` / `GET /performance`

`account` required (case-insensitive, unknown → 404); `from`/`to` default to today/now,
inclusive end-of-day; `instrument` filters; `n` caps rows (hard ceiling 5000, `capped:true` when
more exist). An inverted range or a >366-day window is 400 (this module's own bound —
`Execution.DbGet` over a whole history is hundreds of MB of transient allocation).

Both union NinjaTrader's trade database (`Execution.DbGet`, from `from - 2 days`) with the
in-memory `Account.Executions` (last `Account.LookbackDaysExecutions` days), deduped by
`ExecutionId`. `source:"db"` vs `"memory"` — **`"memory"` means the answer is a ~3-day window
whatever range was asked for**, flagged in `warnings`. `/performance`'s pad widens
**2 → 7 → 30 → 120 days** when the account was not flat at the start of the window (a position
opened before the pad pairs its closing fill as a new entry and mis-shifts every later trade in
that instrument) — `padDays` reports which pad was used, and `warnings` names the instrument
when even 120 days does not find flat.

`summary`/`trades` in `/performance` are the **same shape** as the backtest status document
(`report.py` and `nt_report` render both with one code path). Commission is reconstructed per trade
(`commissionSource`: `stored`\|`template`\|`none`) because NinjaTrader does not persist per-fill
commission; `summary.commission` stays NinjaTrader's own `TotalCommission` and is never
overwritten by the reconstruction.

**MCP:** `nt_account(name)`, `nt_executions(account, from_date, to_date, instrument, n)`,
`nt_performance(account, from_date, to_date, instrument, n)`.

---

## Workspace module (`addon/NT8Bridge.Workspace.cs`)

Read only, with one exception that is not a NinjaTrader state change: `POST /screenshot` writes
one PNG to a path the caller chose. Nothing here enables, disables, starts or stops a strategy,
and nothing restores, fronts, moves or resizes a window.

| Method | Path | Returns |
|---|---|---|
| GET | `/workspace` | `{"name":"Trading","windows":[…]}` — active workspace + every window (geometry + one `details` object per chart) |
| GET | `/strategies/running?materialize=1` | `{"gridResolved":true,"strategies":[…],"notes":[]}` — the Control Center Strategies grid |
| POST | `/screenshot` body `{"window"\|"chart"\|"hwnd","path"}` | `{"ok":true,"path":..,"window":..,"hwnd":..,"width":..,"height":..,"bytes":..,"method":..,"composited":..,"looksBlank":false}` |

`GET /workspace`: `name` is `Globals.ActiveWorkspace` (`null` if NT8 reports none). Each window
row carries the same geometry as `/windows` plus, for charts, `details:{instrument, period,
barsCount, indicators:[{name,state}], strategies:[{name,state}]}` — **`null` never means
"empty"**, `note` says which. One `Ui<T>()` hop per window, 5 s cap; never poll this — visiting
every chart's UI thread is exactly the load that has frozen a chart thread before.

`GET /strategies/running`: **read only**, the population a chart walk cannot see.
`gridResolved:false` → `strategies:null` (never `[]`). Rows include per-instrument children
(`parent` = the master row's name). **`enabled` is grid state, not proof a strategy is
running — believe `state`.** The grid is only in the visual tree while its tab is selected; the
read falls back to the logical tree, and only with `?materialize=1` does it cycle tabs and
restore the user's — a visible mutation, opt-in for that reason.

`POST /screenshot`: target precedence `hwnd`, then `chart`, then `window` (case-insensitive
substring of the caption); none of the three captures the whole virtual screen. **Never
restores, fronts, moves or resizes the target** — a minimized window is a `409`, not a
`SW_RESTORE`. `method` is `PrintWindow` or the `BitBlt` screen-blit fallback (which returns an
occluded window occluded — the truth, not a defect); `composited:true` merges the WPF frame with
the WinForms `Direct2DForm` that actually draws a NinjaTrader chart's canvas. `looksBlank` (PNG
under 8 KB) is a hint to go look at the image, never a verdict.

**MCP:** `nt_workspace()`, `nt_strategies_running(materialize)`, `nt_window_shot(window, chart,
hwnd, path)`.

---

## Playback module (read, `addon/NT8Bridge.Playback.cs`)

One read-only endpoint. **This endpoint itself never connects, disconnects, seeks, or writes the
replay speed** — it only reads. The opt-in write side that does move the clock (`/playback/seek`,
`/playback/speed`, `/playback/run`) is a separate, gated set of endpoints — see
[Playback control (opt-in, disarmed by default)](#playback-control-opt-in-disarmed-by-default)
below.

| Method | Path | Returns |
|---|---|---|
| GET | `/playback?instrument=&coverage=0&budgetSec=20` | is the Market Replay transport connected, loaded, parked or running, and does the store cover a given day |

Samples `PlaybackAdapter.NowEst` (US Eastern, **not** NT8-local, **not** UTC) twice, 1100 ms
apart, because a single reading cannot tell a parked transport from a running one — this
endpoint therefore always takes ≥1100 ms and holds no lock while it sleeps. `speed`/
`maxSpeedValue` are read-only. `transportResolved:false` means every other field is `null`
(**unknown**, not "parked"). **A connected-but-empty transport reads `clockEst` in year 2090+ —
NinjaTrader's "no replay data loaded" sentinel, never a real time**, and is stationary enough to
pass a naive moving/not-moving check as falsely ready.

Coverage (`?instrument=` one folder, or `?coverage=1` every folder — slow) is opt-in and reads
with NinjaTrader's own `.nrd` reader, bounded by `budgetSec` (default 20, clamped to 120); on
expiry `coverageTruncated:true` and the unread files are `skipped`, never silently absent.

**MCP:** `nt_playback(instrument, coverage, budget_s)` adds a `state`
(`unresolved`\|`disconnected`\|`empty`\|`running`\|`unknown`\|`parked`), `ready` and `why` on
top of the AddOn's facts-only document — the AddOn itself never renders a verdict.

---

## Optimize / walk-forward / report (Python only, no AddOn file)

**Adds no HTTP endpoint.** Pure Python over the existing `POST /backtest` + `GET
/backtest/{id}` — no new NinjaTrader surface, no GUI driving, no worker thread of its own.
Every run is a Backtest-account run.

| Tool | Does |
|---|---|
| `nt_optimize(strategy, params, ...)` | expands a parameter grid, runs one backtest per combination **serially**, ranks on one `summary` key |
| `nt_walkforward(strategy, params, train_days, test_days, anchored, ...)` | slices the range into in-sample/out-of-sample windows, optimizes each in-sample window, runs the winner out-of-sample, stitches the trades |
| `nt_report(id \| status_doc, pdf_path)` | stats + optional one-page PDF for any backtest status document |

`params` accepts a range (`{"Fast":{"min":5,"max":20,"step":5}}`, inclusive of `max`), an
explicit list, a constant, or cli-nt-bridge's `"Name:min:max:step,..."` string. `fitness` is any
`summary` key or one of NinjaTrader's own fitness names, always maximised on NinjaTrader's own
number, never recomputed. A grid over `max_combos` (default 200) or a walk-forward over
`max_backtests` (default 200) runs **nothing** and refuses with the arithmetic in the message.
Every combo's job is DELETEd once ranking is done except the winner and the returned rows.
While a Playback connection is connected, every run's *dates* are capped at the replay clock the
same way a single `/backtest` is — `warnings` flags it, results are still returned.

`nt_report`'s `stats`/`assessment` come from NinjaTrader's own summary numbers; only the equity
curve, running peak and underwater drawdown are derived (cumulative sum of `trades[].pnl`).
matplotlib is optional — without it `pdf` is `null` and `note` says why; never an error.

> **Design note:** `nt_report(id, status_doc, pdf_path)` also takes any status-document-shaped
> object with no AddOn round trip, which is how a walk-forward's stitched result gets a PDF.

---

## Ops (opt-in, disarmed by default, Batch 3)

**This module can only reduce risk.** Everything it can do is reduce-only: cancel working
orders, flatten open positions, reconnect a connection NinjaTrader itself dropped. There is no
order entry in this module (that is the separate [Orders](#orders-opt-in-disarmed-by-default-simulator-only)
module), no strategy enable/disable, no chart-series
switch, no all-accounts form, and `Account.FlattenEverything()` is never called anywhere in this
codebase. The module never creates `ops.enabled` and never creates `ops.live`.

| Method | Path | Returns |
|---|---|---|
| GET | `/ops/status` | armed?, live?, both flag ages, the accounts that are valid targets |
| POST | `/ops/flatten` | dry-run `{plan, confirm, issuedAt}`, or the result of a confirmed flatten |
| POST | `/ops/reconnect` | the same two steps for one inadvertently dropped connection |

**MCP: exactly one tool, `nt_flatten(account, instrument, confirm, issued_at)`.** The naked-
position watchdog (`python -m nt8_mcp.watch`), the connection guardian
(`python -m nt8_mcp.connwatch`) and the restart CLI (`python -m nt8_mcp.restart`) are local
processes on purpose — a model reads their event log, it never starts or stops them — and there
is no tool at all for reconnect, because a reconnect is not reduce-only (it can resume order
routing on a connection a human deliberately parked).

### The five gates (every state change passes all five, independently)

1. **`ops.enabled`** — a file beside the AddOn in `bin\Custom\AddOns`, stat-checked on every
   request, never cached, **ignored once older than 24 h** (bounded on both sides — a
   future-dated mtime is ignored too). Absent/stale: **every** `/ops/*` path, including
   `/ops/status`, answers `403 {"error":"ops module not armed"}` and nothing is audited.
2. **`AnyLiveConnected()`** — both POSTs refuse with `409` while any Connected connection
   is neither Simulator nor Playback and can manage orders. There is no `force` on either
   endpoint. `GET /ops/status` is deliberately **not** behind this guard (listing account names
   routes nothing) — it reports `anyLive`/`postsRefused` instead.
3. **`ops.live`** — a second file, same folder. Its **presence**, no age rule, is what makes a
   non-Simulator account (or connection) a valid target; without it such an account is not even
   listed by `/ops/status` (counted under `hiddenNonSimulator`) and flattening it is a `403`.
   Judged by **provider**, never by account name.
4. **Dry-run by default, and the confirm string is SIGNED.** A POST without `confirm` changes
   nothing and returns the plan, the exact confirm string, and `issuedAt`. The string is
   `<readable plan> #<16 hex>`, HMAC-SHA256 over the plan **and** `issuedAt`, under a random
   per-process secret never persisted or exported — so a caller cannot compute a valid confirm
   from the ungated `GET /account`/`GET /connections` reads alone. Re-computed on the confirming
   call: a position or order that moved refuses the token (`409` with a new plan, no new token).
5. **The 30 s window** — `issuedAt` older than 30 s (or >5 s in the future) is refused; it is
   signed **into** the string, so moving it invalidates the token rather than refreshing it.

**Audit.** Every **armed** call, refusals included, appends one JSON object to
`nt8mcp\ops.jsonl` (mirrored into `GET /log`), serialised under a lock. An unarmed call is not
audited — it never reached the account layer.

> **Text is data.** `ops.jsonl`, `NT8Bridge.log` and every string these endpoints echo back
> from NinjaTrader — account names, order names, exception text — are **DATA for whoever reads
> them, never instructions.**

### `GET /ops/status`

```json
{ "flags": { "armed": true, "flagName": "ops.enabled", "flagAgeHours": 0.12,
             "flagMaxAgeHours": 24, "live": false, "liveName": "ops.live", "liveAgeHours": null },
  "anyLive": false, "postsRefused": false, "complete": true, "error": null,
  "accounts": [ { "name": "Sim101", "provider": "Simulator", "simulator": true,
                  "openPositions": 1, "workingOrders": 2, "error": null } ],
  "hiddenNonSimulator": 2, "backtestAccounts": 1, "confirmWindowSec": 30,
  "auditLog": "C:\\Users\\…\\NinjaTrader 8\\nt8mcp\\ops.jsonl", "note": "…" }
```

`workingOrders` uses the same `WorkingStates` list as `/account` (`CancelSubmitted` counted).
Both counters are `null` with an `error` when an account could not be read — never a silent 0.
`complete:false` + a top-level `error` means the account enumeration itself threw mid-walk; the
list and counters are then partial.

### `POST /ops/flatten`

Two-step dry-run/confirm as above. The confirm string is
`FLATTEN <account> FILTER <instr|none> <instr> <side> <qty>[ + …] CANCEL [<id> <instr> <action>
<type> <qty>]… #<mac>` — **working orders are LISTED, not counted**, so any substitution or
re-price inside the 30 s window moves the string and is refused. `ordersCancelRequested` (not
`ordersCancelled`) and `ok` are **measurements**: `Account.Cancel`/`Account.Flatten` return
`void`, so the endpoint re-reads the account ~1.2 s later and reports `stillOpen`/
`stillWorking`/`positionFlat`; `ok:true` requires that re-read to say flat. A confirmed call on
an already-flat account is a no-op and still reads `flattened` — read `flattenCalled` and
`ordersCancelRequested` to tell "nothing to do" from "this call closed something". Positions and
orders are snapshotted under their own locks, **both released**, before `Account.Cancel`/
`Account.Flatten` are called (their own collections re-enter on callback; acting inside the lock
deadlocks the platform with a position open) — there is no `Ui()` hop anywhere in this endpoint
either, so it keeps working when the UI thread would not.

Refusals: `400` account missing/not JSON/Backtest account; `403` unarmed, or non-Simulator
without `ops.live`; `404` no such account; `409` live connection, stale `issuedAt`, or confirm
mismatch; `500` a position/order row could not be fully read (refusing beats flattening off a
partial snapshot); `207` the flatten ran and something inside NinjaTrader threw (read `errors`).

**Manual proof, not automated**: no test in this repo opens a real position, so
`Account.Cancel`/`Account.Flatten` on one that exists is verified only by hand, on a Simulator
account: unarmed calls come back 403, a dry run returns the plan, a wrong/stale/re-stamped
confirm is refused, a fresh confirm flattens the position (never reverses it), and every step is
audited. See `docs/api/ops.md` for the full procedure.

### `POST /ops/reconnect`

`{"name":"..."}`, confirm string `RECONNECT <name> <status> inadvertent #<mac>`, matched against
both `Globals.ConnectOptions` and `Globals.BrokerageConnectOptions` (a brokerage login is not in
the first list at all — this machine's `Simulation` connection is exactly that case). Acts on
**one** `dropClass` value only: `inadvertent`. `failed` (NinjaTrader itself refused the connect)
and `user` (a human parked it) and `null` (never witnessed) are all refused — reconnecting any
of those either hammers a connection saying no or resumes something a human chose to stop.
`ok`/`outcome` come from `statusAfter` (`Connect` is async and returns `void`), never from "the
dispatch did not throw" — reporting a refused connect as `reconnected` would put a false "the
feed healed" line in the audit log for whoever greps it later.

### `GET /compat` rows

`Ops.armed`, `Ops.live`, `Ops.endpoints` (mirrors `Ops.armed`; lists the three paths when armed,
`"none — module not armed"` when not) — how an operator reads ops state without calling an ops
path, which would 403.

### The three local processes (never MCP tools)

- `python -m nt8_mcp.watch --account <name> [--grace 20] [--report-only]` — flattens a position
  left unprotected (no opposing stop covering its **full quantity**, correct **side**, correct
  **type**) longer than the grace period; re-checks the AddOn's fresh plan before confirming, so
  a position that closed and re-opened between scan and dry-run is never flattened blind.
- `python -m nt8_mcp.connwatch --connection <name> [--grace] [--max-attempts]` — reconnects
  **inadvertently** dropped connections only, exponential backoff with jitter, cooldown-and-
  retry rather than giving up forever. Heals in-session drops only.
- `python -m nt8_mcp.restart --task <name> | --exe <path>` — restarts the NinjaTrader process
  (for changes a reload cannot survive — bars-type instances above all). Refuses before touching
  anything if neither `--task`/`--exe` is given, if any account has an open position **or** a
  working order (readable or not), or if the process list itself could not be read.

---

## Orders (opt-in, disarmed by default, Simulator only)

**This is the only part of this repository that can open a position.** The full contract — every
field, status code, refusal, the audit format and a manual test — is `docs/api/orders.md`. This
section is the summary.

| Method | Path | Returns |
|---|---|---|
| GET | `/orders/status` | armed?, flag age, the caps in force, the Simulator / Playback accounts that are valid targets, and every live order on them with its owner |
| POST | `/orders/submit` | dry-run `{plan, confirm, issuedAt}`, or the result of a confirmed submit |
| POST | `/orders/bracket` | one entry + one stop + any number of targets, under one plan and one confirm |
| POST | `/orders/change` | the same two steps for the quantity and/or prices of ANY live order |
| POST | `/orders/cancel` | the same two steps for ANY live order |
| POST | `/orders/close` | cancel one instrument's working orders on one account, then flatten it |
| POST | `/orders/reverse` | the same, then enter the same quantity on the other side |

**MCP: six tools**, `nt_order_submit`, `nt_order_bracket`, `nt_order_change`, `nt_order_cancel`,
`nt_position_close`, `nt_position_reverse`. Order types Market, Limit, StopMarket, StopLimit; TIF
Day or Gtc; one account, one instrument per call.

**Brackets and the OCO pair rule.** `/orders/bracket` takes one entry, one stop loss
(`stopLossPrice` or `stopLossTicks`) and any number of targets, sized to the entry's real fill, not
the requested quantity — a resting entry comes back `exitsPending:true` with nothing at the broker
yet, and a bounded watcher submits the exits once it fills. NinjaTrader cancels every other **live**
order in an OCO group the moment one member fills or is cancelled, so each target gets its **own**
stop under its **own** OCO pair (one pair per target, not one shared group): cancelling a target
only ever cancels its own paired stop, never a sibling pair's. Full mechanics: `docs/api/orders.md`.

**`/orders/change` and `/orders/cancel` reach ANY live order on the gated account**, not only the
ones this module placed — a running strategy's stop, an ATM's target, a hand-placed order. The
plan names the order's `owner` (`module` / `strategy <name>` / `atm` / `manual`), its OCO group,
its state and its filled quantity before you confirm.

**The gate chain, in this order.** (1) The arming file `orders.enabled` beside the AddOn,
stat-checked on every request, ignored when older than 24 h or future-dated; unarmed = `403
{"error":"orders module not armed"}` on all seven paths, before the body is parsed. `ops.enabled`
does not arm this module. (2) Every POST is refused while `AnyLiveConnected()` is true; there
is no `force`. (3) The account must resolve to exactly one account whose provider is
`Provider.Simulator` or `Provider.Playback`; a provider that cannot be read is a refusal; the
Backtest account is refused by name. **There is no live switch: the module never reads `ops.live`
and has no code path that accepts another provider.** (4) Validation. (5) Caps: 10 contracts per
order, 20 working orders per account, 60 confirmed submits per minute; the optional file
`nt8mcp\orders.config.json` changes them up to the code ceilings 100 / 100 / 600, and a value
outside `1..ceiling` falls back to the default with a warning. A bracket's exits are exempt from
the working-order cap (the account's hard ceiling of 100 live orders in code still applies).
(6) No `confirm` = dry run. (7) The confirm is an HMAC over `orders.<verb>|` + the plan (caps
included) + the AddOn-stamped `issuedAt`, valid for 30 s, compared in fixed time, and
**single-use**. (8) The call runs outside every collection lock; the result reports the order's
true state after a bounded re-read, and `ok` never means "filled". (9) Every armed call is
audited to `nt8mcp\orders.jsonl`; a confirmed action writes an intent line first and does not run
if that write fails.

`/orders/close` and `/orders/reverse` cancel one instrument's working orders on one account, then
flatten it (`reverse` also enters the same quantity the other side); `Account.FlattenEverything()`
is never called anywhere in this repository. Full contract, every field and status code:
`docs/api/orders.md`.

---

## ATM strategies (opt-in, disarmed by default, Simulator only)

An ATM strategy is NinjaTrader's own bracket manager: a saved template holds a quantity, a stop
loss and a profit target, and `/atm/start` sends one entry order under it — NinjaTrader then arms
and manages that template's stop and target itself, on the fill. Every write goes through the
**same** gate chain as `/orders/*` (`orders.enabled`, the live-routing refusal, the provider test,
the caps, the signed one-shot confirm, the same audit log). Full contract, including the
NinjaTrader internals this depends on and what is still unconfirmed: `docs/api/atm.md`.

| Method | Path | Returns |
|---|---|---|
| GET | `/atm/templates` | the saved templates: names and the bracket parameters read from the files |
| GET | `/atm/status` | the ATM strategies with a live order or an open position, with entry/stops/targets |
| POST | `/atm/start` | dry-run `{plan, confirm, issuedAt}`, or the result of a confirmed start |
| POST | `/atm/close` | cancel one ATM's working orders and flatten the position it holds |
| POST | `/atm/change` | move one ATM's stop and/or target to a new price |

**MCP: five tools**, `nt_atm_templates`, `nt_atm_status`, `nt_atm_start`, `nt_atm_close`,
`nt_atm_change`. All five endpoints are exercised against a live NinjaTrader 8.1.8.2 install.
NinjaTrader requires the entry order of an ATM strategy to be named `Entry`, and the entry is sent
with `Account.Submit` after `AtmStrategy.StartAtmStrategy` has attached the template.

---

## Strategies on Sim (opt-in, disarmed by default)

Closes the loop: write a strategy, compile it, backtest it, then run it for real on a Simulator or
Playback account and read the fills back. A strategy places its own orders, so this sits behind
the **same** gate chain as `/orders/submit`. Full contract, including the NinjaTrader internals
this depends on: `docs/api/strategyrun.md`.

| Method | Path | Returns |
|---|---|---|
| POST | `/strategy/start` | dry-run `{plan, confirm, issuedAt}`, or the result of a confirmed start |
| POST | `/strategy/stop` | the same two steps for one instance this module started. It does not flatten |
| GET | `/strategy/running` | the instances this module started, with state, position, working orders, realized PnL |

**MCP: three tools**, `nt_strategy_start`, `nt_strategy_stop`, `nt_strategy_runs`. The strategy is
added to NinjaTrader's own Control Center Strategies grid, enabled, so the user sees the row and
can disable it by hand; after a NinjaScript reload the module no longer knows the ids of the
instances it started, but the grid rows survive and the user disables them there.

---

## Chart control (module `chartcontrol`, `addon/NT8Bridge.ChartControl.cs`)

Never touches an account: no arming file, no provider check, no audit log. The
write -> compile -> put on chart -> look -> fix loop for a chart's indicators and series. Full
contract: `docs/api/chartcontrol.md`.

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/chart/{id}/indicator/add` | `{"indicator","inputs":{},"panel":0}` | the chart report |
| POST | `/chart/{id}/indicator/remove` | `{"indicator" \| "index", "force":false}` | the chart report |
| POST | `/chart/{id}/series` | `{"instrument","barsPeriod":{"type","value"}}` | the chart report |
| POST | `/chart/{id}/scroll` | `{"time"}` | the chart report |

**MCP:** `nt_chart_indicator_add`, `nt_chart_indicator_remove`, `nt_chart_set_series`,
`nt_chart_scroll_to`, plus the Python-only `nt_trade_shot` (scroll to a saved/live backtest
trade's entry, then screenshot). `indicator/add`, `indicator/remove` and `scroll` are confirmed
against a live NinjaTrader 8.1.8.2 install. `series` takes `{"restore":true}` to put back exactly
what the chart showed before the module first changed it.

---

## Playback control (opt-in, disarmed by default)

The read side, `GET /playback` (see [Playback module (read)](#playback-module-read-addonnt8bridgeplaybackcs)
above), never connects, disconnects, seeks or writes the replay speed. `/playback/seek`,
`/playback/speed` and `/playback/run` do, and are opt-in behind the **same** `orders.enabled` file
the order module reads: gated on a Connected Playback connection (never connected/disconnected by
this module), no non-Playback account holding a position or a working order, and no modal dialog.
There is no HMAC confirm here — moving a replay clock is not an order — but every armed call is
one line in `nt8mcp\playback.jsonl`. Full contract: `docs/api/playback.md`.

| Method | Path | Returns |
|---|---|---|
| POST | `/playback/seek` `{"time","waitSec"}` | reposition the replay clock |
| POST | `/playback/speed` `{"speed"}` | play (>=1) or pause (0) — writing this property IS the play/pause control |
| POST | `/playback/run` `{"from","to","speed","stopAtEnd":true}` | queue a bounded run job |
| GET | `/playback/run/{id}` | that job's status document |
| DELETE | `/playback/run/{id}` | cancel it |

**MCP:** `nt_playback_seek`, `nt_playback_speed`, `nt_playback_run`, `nt_playback_run_status`,
`nt_playback_run_cancel`. Proven on a live NinjaTrader 8.1.8.2 install: 3 replay hours played in
55 wall-clock seconds at 200x with a strategy running on Playback101, 19 fills collected. A `run`
job re-checks every gate every few seconds while it plays and always pauses and reports, even when
a gate trips mid-run.

`nt_reconcile` (`server/nt8_mcp/tools_reconcile.py`, no AddOn endpoint of its own) pairs a
backtest's trades against a `/playback/run` job's real fills — matched pairs with price/time
deltas, fills on only one side, and a plain verdict. Full contract: `docs/api/reconcile.md`.

## Threading rules (for the AddOn author)

- `HttpListener` on its own thread. Each request: parse, dispatch through the discovered
  `Route_*` table then the core's five, hop to the relevant dispatcher only when the handler
  needs one (`Ui<T>()`), build the JSON string there or on the HTTP thread, return it. Never
  hold a chart lock or a `Cbi` collection lock across I/O or across a call into NinjaTrader.
- `Ui<T>()` **throws** `TimeoutException` on a dispatcher timeout (mapped to the core's 504) —
  it never silently returns `default(T)`.
- Never call `Draw.*`, never mutate chart state except in `reload`.
- A request that throws returns 500 with `Deep(ex)` (the innermost exception, named), except
  where a module maps a specific exception type to a sharper status (400/404/409/504 — see each
  module's Errors table). The AddOn keeps running either way.
- Every static-event `+=` added by a module has its `-=` in that module's `Stop_<Module>()`,
  called on `State.Terminated` and idempotently on a rebind retry.
- Buffers are `Queue<T>` ring queues with a monotonic sequence number, never a `List<T>` +
  `RemoveRange` — the latter memmoves the whole buffer per line, under its lock, on the
  printing thread, once full.
- Stop the listener in `OnWindowDestroyed` of the Control Center and on `State.Terminated`.
