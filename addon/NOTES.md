# NT8Bridge — implementation notes

Every endpoint in `API.md` is implemented. What follows is only where the NT8 API forced a
compromise, and the facts behind the code that are not obvious from reading it.

The AddOn is ONE class, `public partial class NT8Bridge` in namespace
`NinjaTrader.NinjaScript.AddOns`, split over several files:

| File | Holds |
|---|---|
| `NT8Bridge.cs` (the **core**; the only file that names the base class, `: AddOnBase`) | life cycle incl. reload survival (`Start`/`TryBind`/`Rebind`/`AfterBind`/`Reseed`/`Stop`), listener, `Handle`, `Route`, seam discovery, `Ui`/`FindChart`/`IdOf`/`Control`/`OnChart`/`Primary`, window registry + `KnownWindows`/`StandingModal`/`Geometry`, the shared helpers `Deep`, `RangeProblem`, `Ring<T>`, `Compat`, `OutputHub`, `AnyLiveConnected`/`AnyNonSimConnected`/`RefuseIfLive`, ring log, JSON write (`Obj/P/Arr/Q/D/Tm/TmUtc/Scalar/I/Ser`), JSON read (`ParseJson`, `JGet*`, `Coerce`), `BadRequestException`, `Err`, `IsInput`, `Num`, `Tail`, the self-tests, and the handlers for `/health` `/windows` `/log` `/compat` `/ntstatus` |
| `NT8Bridge.Charts.cs` | `/charts`, `/chart/{id}`, `/bars`, `/indicators`, `/drawings`, `/reload`, `/screenshot`, `/output/window`; `NameOf`, `PanelOf` |
| `NT8Bridge.Account.cs` | `/account` |
| `NT8Bridge.Backtest.cs` | `/strategies`, `/backtest`, `/backtests`, `/backtest/{id}`; the `Job` class, the `NT8Bridge-bt` worker |

`scripts\install-addon.ps1` copies every `addon\*.cs` and deletes installed `NT8Bridge*.cs` files
that are no longer in the repo. A file with a NEW name only compiles once NT8 has added its
`<Compile Include="AddOns\<File>.cs" />` line to `NinjaTrader.Custom.csproj`. NinjaTrader does that
by itself when the file lands.

## Module seams

A module is one more `NT8Bridge.<Module>.cs` file declaring `public partial class NT8Bridge` (no base
class). It never edits the core. It plugs in through three `private static` methods that the core
finds by reflection over `typeof(NT8Bridge)` in `Discover()`, called from `Start()`. Names are sorted
ordinally; the table is logged at Start, one line per method (`seam: route Route_Charts`,
`seam: start Start_Backtest`, `seam: stop  Stop_Backtest`) — read it with `GET /log`.

```csharp
// Router. seg = the path split on '/', empty entries removed ("/chart/c1/bars" -> {"chart","c1","bars"}).
// q = the query string. body = the request body for POST/PUT, else "". status comes in as 200.
private static string Route_<Module>(string method, string[] seg,
    System.Collections.Specialized.NameValueCollection q, string body, ref int status)

// Life cycle, both optional, no parameters, no return value.
private static void Start_<Module>()
private static void Stop_<Module>()
```

Rules (binding):

- **Precedence.** The core's `Route` tries the discovered `Route_*` table FIRST, in name order, then
  its own remaining handlers (`/health`, `/windows`, `/log`, `/compat`, `/ntstatus`), then its 404. So a
  module can own an endpoint without any core edit — and a module that answers a path it does not own
  silently steals it from every module later in the alphabet and from the core.
  **`GET /output` is free for the events module.** The window scrape moved to `GET /output/window`
  (`Route_Charts`). The core still carries ONE legacy line, after the table, that answers `GET /output`
  with the same scrape — a compatibility alias for callers that still use the old path. `Route_Events`
  claiming `GET /output` shadows it automatically; delete the line (marked `LEGACY ALIAS` in `Route`)
  once it does.
- **The return-null rule.** Every Route_* MUST return null for any method/path it does not fully
  handle - only the core emits the 404. "Fully handle" includes the method: `Route_Account` returns
  null for `POST /account`. A 404 for a missing *resource* on a path the module does own is the
  module's to give (`/chart/zz/bars` -> `Route_Charts` 404 "no chart 'zz'", `/backtest/b999` ->
  `Route_Backtest` 404) — decide "is this path mine" first, look the resource up second, as
  `Route_Charts` does. The core resets `status` to 200 after every null.
- **A non-null return is the whole JSON response body.** Set `status` for anything but 200
  (`Err(ref status, 400, "...")` does both). An exception thrown out of a `Route_*` becomes a core
  `500 {"error": …}` plus a stack trace in the log; routes are bound as delegates, so the exception
  arrives unwrapped (no `TargetInvocationException`). **What goes in `error` depends on
  `InnerException`**: `ex.Message` when there is none, `Deep(ex)` when there is — and `Deep` reports the
  INNERMOST exception, i.e. `"IOException: The device is not ready.   [via Exception]   @ at System.IO…"`,
  not your wrapper's text. So `throw new Exception("template 'x' has no bars", ex)` does NOT put that
  sentence in the body. Throw a fresh exception with no inner one when a client is meant to read (or
  match on) the message; wrap only when the inner exception is the interesting part.
- **Signature matching.** A `Route_*` must have exactly the 5 parameters above (`ref int` is
  `typeof(int).MakeByRefType()`, i.e. `System.Int32&`) and return `string`; a `Start_*`/`Stop_*` must
  be `() : void`. Anything else with one of the three prefixes is logged (`seam: <name> skipped — …`)
  and skipped; discovery never throws out of `Start()`. Do not name an unrelated static method
  `Route_…`, `Start_…` or `Stop_…`.
- **`Start_*`** run in name order after the listener is bound and `running` is true, outside the
  `gate` lock, BEFORE the first request is served. They run only if the listener came up (a busy
  port starts nothing). **`Stop_*`** run in name order after the listener is closed and the listen
  thread joined. `Stop()` runs on NT8's UI thread (`OnWindowDestroyed` / `State.Terminated`, which is
  also every F5 recompile): a `Stop_*` must be bounded and must never wait on a dispatcher. Each
  hook is wrapped in its own try/catch that logs `<name>: <exception>`; one module failing does not
  stop the others. Every static-event `+=` a module makes needs its `-=` in its `Stop_<Module>()`.
  **A `Stop_*` must be idempotent**: when the port was busy and the listener came up on a rebind retry
  (pool thread), a `Stop()` that races it makes the core run the stop hooks a second time.
- **Names.** Everything a module adds to the class is prefixed with the module name
  (`Compile_…`, `Ev_…`, `Data_…`) so two modules cannot collide. The three reference modules predate
  that rule and keep their old member names.
- Request threads are HttpListener thread-pool threads. Anything that touches a window goes through
  `Ui<T>(dispatcher, f)` / `OnChart(chart, f)`.

Reference examples: `Route_Charts` (path-ownership test before the resource lookup), `Route_Account`
(the minimal form), `Route_Backtest` + `Start_Backtest` / `Stop_Backtest` (a worker thread whose
bounded `Join(3000)` lives in the stop hook).

One deliberate edge from the split: `GET /chart/<unknown id>/<unknown sub-path>` used to answer
`404 no chart '<id>'…`; it now answers the core's `404 no route for …` because `Route_Charts` does
not claim sub-paths it does not handle. Same status, same `{"error":…}` shape.

### Core helpers a module calls (all `private static` on `NT8Bridge`, never re-implemented in a module)

```csharp
// ── threading: never do DB, file or network I/O while holding a lock ──
T Ui<T>(Dispatcher d, Func<T> f)                                   // 5 s
T Ui<T>(Dispatcher d, Func<T> f, TimeSpan timeout, string what)   // `what` names the target in the message; null = looked up
```
`Ui` **throws `TimeoutException`** when the thread does not take the call in time — it never
returns `default(T)`. Let it fly: `Handle` turns any `TimeoutException` out of a route into
**`504 {"error":"UI thread did not answer in 5s (window: Chart c1)","standingModal":<title|null>}`**.
Catch it yourself only where one dead window must not fail a whole list (`/windows`, `/charts` do; a
`/workspace` row should become `details:null` + a note). A `catch (Exception)` that turns errors into a
400/500 must let it through first: `catch (TimeoutException) { throw; }` (see `BacktestStart`).
WPF semantics, proven by the `selftest.uiTimeout` row in `/compat`: a call still *queued* at the timeout
is aborted and never runs late; a call that has *started* is waited for, so `f` must be quick. Never
call `Ui` while holding `gate`, a `Ring` lock or a `Cbi` collection lock. No DB or file I/O inside `f`.

```csharp
// ── errors ──
string Deep(Exception ex)                       // innermost "Type: message   [via Outer]   @ first stack line"
string Err(ref int status, int code, string msg)
string RangeProblem(DateTime from, DateTime to) // null = fine; else the 400 text. Call before arming ANY range
                                                //     (/backtest does; /templates defaults and /analyze must)

// ── live-account predicates: the live-connection guard ──
bool AnyLiveConnected()      // ORDER ROUTING: Connected && (Options == null || (Provider not Simulator/Playback && CanManageOrders))
bool AnyNonSimConnected()    // DATA: the same without CanManageOrders. Refuses nothing; /data/download needs it TRUE
string RefuseIfLive(ref int status, string what, bool force)
                             // null = go ahead; else the finished 409 body {"error":…,"anyLive":true}.
                             // `force` is for /compile?reload=1 ONLY; /data/download and every ops endpoint pass false.
Connection[] ConnSnapshot()  // under lock(Connection.Connections); evaluate AFTER it returns
bool IsConnected(Connection c) / IsLive(Connection c) / IsNonSim(Connection c)
```
Both `Any*` answer **true when the connections cannot be read**. Never `ConnectOptions.IsDemo`, never
`Cbi.Mode`, there is no `Provider.Backtest`, and a null `Options` is live. `Status == ConnectionLost`
does not count: the rule is `Connected`.

```csharp
// ── reflection table: resolve every reflective member ONCE, at Start() ──
object Compat.Resolve(string key, Func<object> resolver)   // runs NOW, never throws, returns the value or null
void   Compat.Set(string key, bool resolved, string detail, object value)   // for a multi-step probe / self-test
T      Compat.Get<T>(string key) where T : class
bool   Compat.IsResolved(string key)
```
Resolve everything reflective ONCE, in your `Start_<Module>()`, keep the result in a module static, and
report `resolved:false` + `null` (never `[]`) from the endpoint when it is missing. Key =
`"<Type>.<member>"` (`"ChartControl.mnuReloadNinjaScript"`); `Start_Charts` is the example.
`GET /compat` prints `{"compat":[{key,resolved,detail}],"routes":[…],"startHooks":[…],"stopHooks":[…],
"outputHub":{subscribed,seen,dropped,listeners}}`; `/health.reflection` is the `key -> bool` digest.

```csharp
// ── ring buffer: the log is bounded, never unbounded ──
new Ring<T>(int capacity)
long    Add(T item)                    // returns the item's seq; first ever = 1
long    Seen / Dropped ;  int Count    // Seen == Dropped + Count == seq of the newest item
List<T> Read(long since, int max, out long firstSeq, out long seenNow, out long dropped)
List<T> Tail(int n)
```
`Read`: `since < 0` → the newest `max`; `since >= 0` → items with `seq > since`, oldest first, at most
`max` (`max <= 0` = no cap). Item `i` of the result has seq `firstSeq + i`. A cursor older than the
buffer gets what is left, one from the future gets nothing. **The cursor is a sequence number, never a
slot index.** Queue + Dequeue only; the lock is held for queue operations and nothing else.

```csharp
// ── Print() output: ONE subscription, owned by the core ──
OutputHub.Lines                                   // Ring<OutputHub.Line>, cap 20000
OutputHub.Line { DateTime Time; int Tab /*1|2*/; string Text; bool Reset; }
OutputHub.Register(Action<OutputHub.Line>) / OutputHub.Unregister(...)
OutputHub.Subscribed / OutputHub.ListenerCount
```
**No module subscribes to `NinjaTrader.Code.Output.OutputEvent`.** The core does `+=` once in
`AfterBind` and `-=` in `Stop()` (which `State.Terminated` calls; it is unconditional there). Read
`OutputHub.Lines` (the events module's `GET /output`), or `Register` a listener for the duration you
need one and `Unregister` it (`RunOne` in the backtest module does; a long-lived one goes in your
`Stop_*`). A cleared tab is a line with `Reset = true` and `Text = "<<cleared: tab N>>"`. The handler
and every listener run on the thread that called `Print()` — chart threads included: no `Ui()`, no file
I/O, no NinjaTrader call in a listener. That property is what keeps `/output` answering while the UI
thread is wedged. `Cbi.Log.LogEvent` has no hub: the events module owns that `+=`/`-=` pair itself
(remember `Cbi.Log`, namespace-qualified — `Log` is our method).

```csharp
// ── windows ──
List<Window> KnownWindows(out HashSet<Window> owned)   // registry + Globals.AllWindows + the dialogs they own; NO dispatcher hop
Window[]     TopWindows()                              // Globals.AllWindows snapshot, retried if it moved
string       TitleOf(Window w)                         // ON the window's thread only (inside Ui)
string       StandingModal()                           // title / type name of a standing *MessageBox*, else null
string[]     Geometry(IntPtr hwnd)                     // the P(...) pairs hwnd,left,top,width,height,isMinimized,screen
string       Kind(Window w, string title)
bool GetWindowRect(IntPtr, out WinRect) / IsIconic(IntPtr) / MonitorFromWindow / GetMonitorInfo   // user32, thread-agnostic
string TmUtc(DateTime)                                 // "…Z"; Tm() stays NT8-local without a zone
```
Do not declare a second `GetWindowRect`/`IsIconic` P/Invoke in a module (CS0111) — use these, or give
yours a module-prefixed name with `EntryPoint=`.

## Status document v1

The frozen contract of `JobJson` / `Snapshot` in `NT8Bridge.Backtest.cs`. This is what
`GET /backtest/{id}` returns, key for key and in this order. Modules write against THIS: add keys
after the existing ones, never rename, retype or reorder these.

Conventions: times are local NT8 time, `"yyyy-MM-ddTHH:mm:ss"`, no zone; `DateTime.MinValue` is
written as `null`. A `number` is a JSON double written with `"R"`; `NaN` and ±Infinity are written
as `null`, so **every `number` below is nullable unless it says "never null"**. `int` is a JSON
integer and is never null.

| # | Key | Type | Null? | Meaning |
|---|---|---|---|---|
| 1 | `id` | string | never | `"b<N>"`, N counts up from 1 per AddOn load (ids restart after a recompile) |
| 2 | `state` | string | never | `queued` \| `running` \| `done` \| `error` \| `timeout` \| `cancelled` |
| 3 | `strategy` | string | never | the strategy's type name (`Type.Name`), not its display name |
| 4 | `instrument` | string | never | `Instrument.FullName`, e.g. `"ES 12-26"` |
| 5 | `period` | string | never | `BarsPeriod.ToString()`, e.g. `"5 Minute"`, `"2 Renko"` |
| 6 | `from` | string | no (null only for a literal `0001-01-01`) | requested start |
| 7 | `to` | string | same | requested end, inclusive; a date-only `to` is stored as that day `23:59:59` |
| 8 | `tickReplay` | bool | never | |
| 9 | `startedAt` | string | **null while `queued`** (and for a job cancelled before it ran) | when the worker picked it up |
| 10 | `finishedAt` | string | **null while `queued`/`running`** | set before `state` turns terminal |
| 11 | `seconds` | number | **null while `finishedAt` is null** | `finishedAt - startedAt`. Quirk: a job cancelled while still `queued` has no `startedAt`, so this is a huge number (~6.4e10), not null |
| 12 | `error` | string | **null unless `error`/`timeout`/`cancelled`** | exception message, `"cancelled by DELETE"`, or `"shutdown"`; `timeout` leaves it null |
| 13 | `inputs` | object | never (`{}` when none were sent) | the request's `inputs` echoed back, values as sent (number/string/bool/null) |
| 14 | `summary` | object | **null unless `state == "done"`** | below |
| 15 | `trades` | array | **null unless `state == "done"`** | below; `[]` for a run with no trades |
| 16 | `output` | array of string | never (`[]` unless `done`) | `Print()` lines captured from Output tab 2 during the run, capped at 20000 |

`GET /backtests` returns an array of rows holding keys 1-12 only, oldest first.

`summary` (all 21 keys always present when `summary` is not null, in this order):

| Key | Type | Notes |
|---|---|---|
| `trades`, `winners`, `losers` | int | `winners + losers != trades` when there are even trades |
| `winRate` | number | winners / trades, `0` when there are no trades |
| `netProfit`, `grossProfit`, `grossLoss`, `profitFactor`, `commission` | number\|null | currency; `profitFactor` is null when there are no losers (Infinity) |
| `maxDrawdown`, `avgTrade`, `avgWinner`, `avgLoser`, `largestWinner`, `largestLoser` | number\|null | currency |
| `avgMae`, `avgMfe` | number\|null | currency (the per-trade `mae`/`mfe` are points) |
| `avgBarsInTrade`, `sharpe` | number\|null | |
| `maxConsecWinners`, `maxConsecLosers` | int | |

`trades[]` item (14 keys, in this order) — or, if reading one trade threw, `{"error": string}`:

| Key | Type | Null? |
|---|---|---|
| `n` | int | never; `Trade.TradeNumber`, **0-based** |
| `side` | string | never; `"Long"` \| `"Short"`, `""` if the trade has no entry |
| `qty` | int | never |
| `entryName`, `exitName` | string | nullable |
| `entryTime`, `exitTime` | string | nullable |
| `entryPrice`, `exitPrice` | number | nullable |
| `pnl` | number | nullable; currency |
| `pnlPoints`, `mae`, `mfe` | number | nullable; points |
| `bars` | int | **nullable**: null when both bar indices are 0 (unknown), else `Exit.BarIndex - Entry.BarIndex` |

Around it: `POST /backtest` -> `202 {"id": string, "state": "queued"}` (the literal `queued`, always);
`DELETE /backtest/{id}` -> `200 {"ok": true}`; every refusal is `{"error": string}` with 400
(validation), 404 (`no backtest '<id>'`) or 500 (account lookup).

```json
{"id":"b1","state":"done","strategy":"SampleMACrossOver","instrument":"ES 12-26","period":"5 Minute",
 "from":"2026-09-10T00:00:00","to":"2026-09-17T23:59:59","tickReplay":false,
 "startedAt":"2026-09-18T17:56:53","finishedAt":"2026-09-18T17:56:53","seconds":0.6307257,"error":null,
 "inputs":{},
 "summary":{"trades":76,"winners":29,"losers":43,"winRate":0.3815789473684211,"netProfit":-212.5,"grossProfit":13862.5,
   "grossLoss":-14075,"profitFactor":0.9849023090586145,"commission":0,"maxDrawdown":-4425,"avgTrade":-2.7960526315789473,
   "avgWinner":478.01724137931035,"avgLoser":-327.3255813953488,"largestWinner":2512.5,"largestLoser":-2000,
   "avgMae":366.6118421052632,"avgMfe":548.6842105263158,"avgBarsInTrade":20.236842105263158,"sharpe":-1.021032134220192,
   "maxConsecWinners":4,"maxConsecLosers":5},
 "trades":[{"n":0,"side":"Long","qty":1,"entryName":"Buy","exitName":"Close position","entryTime":"2026-09-09T19:50:00",
   "exitTime":"2026-09-09T21:25:00","entryPrice":7648,"exitPrice":7650.25,"pnl":112.5,"pnlPoints":2.25,
   "mae":1.75,"mfe":8.5,"bars":19}],
 "output":[]}
```
(A real document — SampleMACrossOver, ES 12-26, 5 Minute — with `trades` cut to its first item of 76.
A `number` with no fraction prints without a decimal point: `-14075`, `7648`.)

## Verified against the real assemblies

Decompiled (`ilspycmd`) from `NinjaTrader.Gui.dll` / `NinjaTrader.Core.dll`, so the code is typed,
not string reflection:

- `Chart.ActiveChartControl`, `Chart.MainTabControl` (items are `ChartTab`, each with `.ChartControl`).
- `ChartControl.ChartPanels` (`Collection<ChartPanel>`), `.Indicators`
  (`ChartObjectCollection<IndicatorRenderBase>`), `.Strategies` (`…<StrategyRenderBase>`),
  `.BarsArray` (`ObservableCollection<ChartBars>`).
- `ChartPanel.PanelIndex`, `.ChartObjects` (`IList<IChartObject>`).
- `ChartBars.Bars` / `.FromIndex` / `.ToIndex`. `ChartBars` has **no** `GetTime(int)`; bar times come
  from `NinjaTrader.Data.Bars.GetTime(int)`.
- `DrawingTool.Anchors` (`IEnumerable<ChartAnchor>`; anchor has `Time`, `Price`), `.Tag`,
  `.IsUserDrawn`, `.DrawnBy` (`NinjaScriptBase` — `IsUserDrawn` is exactly `DrawnBy == null`).
  There is **no** `OwnerNinjaScript`. Text lives in `Text.DisplayText` / `TextFixed.DisplayText`,
  not `.Text`.
- `NinjaScriptBase.Plots` (`Plot[]`), `.Values` (`Series<double>[]`), `.Name`, `.Bars`, and
  `.Panel` (`int`, the **configured** panel — see below);
  `Series<double>.IsValidDataPointAt(int)` / `.GetValueAt(int)`; `IndicatorBase.DisplayName`.
  `IndicatorBase` itself declares **no** `Panel`; it comes from `NinjaScriptBase`.
  `IndicatorRenderBase : IndicatorBase, IChartObject, …` adds `ChartPanel ChartPanel` (its own panel
  back-reference) and a read-only `int PanelUI`.
- `NinjaTrader.NinjaScript.NinjaScriptPropertyAttribute` (in `NinjaTrader.Core.dll`):
  `sealed`, `: CategoryAttribute`, `[AttributeUsage(AttributeTargets.Property)]`.
- Window types: `NinjaTrader.Gui.ControlCenter`, `NinjaTrader.Gui.Chart.Chart`,
  `NinjaTrader.Gui.SuperDom.SuperDom`, `NinjaTrader.Gui.NinjaScript.NinjaScriptOutput`.
  `NTWindow` is `NinjaTrader.Gui.Tools.NTWindow` (`.Caption`).
- `Account.All` / `.Get(AccountItem, Currency)` / `.Positions` / `.Orders`;
  `Connection.Connections` / `.Status` / `.Options.Name`; `Core.Globals.ProductVersion`.

## Compromises

**`POST /chart/{id}/reload`** — NT8 has no public "reload NinjaScript" method. `ChartControl` only
has the private `MenuItem mnuReloadNinjaScript` (the right-click entry) and the public
`OnReloadNinjaScriptHotKey(object, KeyEventArgs)`. The handler raises `Click` on that private menu
item — one reflection call, clearly commented — and falls back to the public hot-key handler if the
field is missing in a future build. Both are the real menu path; nothing is reimplemented.

**`GET /output/window`** (this was `GET /output` up to 1.1; same code, same JSON, moved into
`Route_Charts` so `GET /output` is free for the events module's ring — see "Precedence" above for the
one legacy line still answering `/output`) — there is no public API for the Output window's text. The handler finds the
`NinjaScriptOutput` window in the AddOn's own window registry and walks its visual tree on its own
dispatcher, collecting `TextBox.Text` and multi-line `TextBlock.Text`. Limits:

- Only text controls that are *realized* in the visual tree are read. If NT8 virtualises the
  unselected tab, that tab's lines will be missing.
- Tabs are tagged `[1]` / `[2]` by the order the blocks appear in the tree, not by the tab's own
  name — the tree gives no reliable tab identity.
- Window closed → the `{"lines":[],"note":"Output window not open"}` form from API.md.
  Window open but no text control found → the same shape with
  `"note":"Output window is open but its text could not be read"`. Nothing is faked.

**Chart ids** come from `OnWindowCreated` and, since 1.2.0, from the `Reseed()` in `AfterBind`:
every window in `Globals.AllWindows` enters the registry and every chart gets an id the moment the
listener is up, so `/charts` is complete straight after a hot reload (the reseed logs
`reseed: 3 windows in Globals.AllWindows, 1 chart(s) registered` a few ms before `listening`).
**An id is only valid for the
NinjaTrader session that issued it, and only until it 404s — always re-read `/charts` after a
recompile.** The counter is kept in `AppDomain.CurrentDomain` (`SetData`/`GetData`, key
`NT8Bridge.chartCounter`), which the reload does not reset, so a chart registered after a recompile
gets the NEXT number (`c4`, `c5`, …), never a number a different chart already used: a cached `c2`
from before the reload gets a loud `404 no chart 'c2' — call /charts first` instead of a 200 for
someone else's chart. Ids restart at `c1` only when NinjaTrader itself restarts. The reseed only ADDS;
removal stays with
`OnWindowDestroyed` (re-adding from `AllWindows` on every request could resurrect a window NT8 is
halfway through closing). Owned windows (dialogs, message boxes) are never stored — `KnownWindows()`
reads them fresh, off `Window._ownedWindows` by reflection, because `Window.OwnedWindows` is
thread-affine and a stored dialog would never be removed (no `OnWindowDestroyed` for a plain `Window`).

**`drawings` count in `/chart/{id}/indicators`** is grouped by indicator name (`NameOf`, below). Two
instances of the same indicator on one chart therefore share one count.

**`owner` in `/chart/{id}/drawings`** is `"user"` when `IsUserDrawn`, else the `DrawnBy` NinjaScript's
name from `NameOf`, else `""` (a NinjaScript-drawn object whose `DrawnBy` NT8 did not set).

## Third-party indicators: name, panel, inputs, plots

Live-tested against `T3000_LagDetector` (vendor DLL `bin/Custom/T3000_MGI.dll`), which broke three
assumptions at once. One helper each, used by every endpoint so the answers agree:

**`NameOf(object)` — `Name`, else `GetType().Name`.** `NinjaScriptBase.Name` is *not* guaranteed to
be set: this vendor leaves it empty on the instance, which used to make `/charts` report
`"indicators":[""]`. `Name ?? GetType().Name` does not help — the value is `""`, not `null`, so the
test has to be `string.IsNullOrEmpty`. Now used by `/charts`, the `/chart/{id}` panel and strategy
lists, `/chart/{id}/indicators` (including the `name=` filter) and the `drawings` owner.

**`PanelOf(ChartControl, object)` — the panel that really holds the object.** `NinjaScriptBase.Panel`
is the *configured* property (the one in the indicator dialog) and NT8 does not set it on every
instance: on this indicator it reads `-1`, so both the `panel` field and the per-panel lists came out
empty. The authoritative relation is containment — `IndicatorRenderBase` is an `IChartObject` and
sits in `ChartPanel.ChartObjects` — so the helper scans `cc.ChartPanels` for the panel whose
`ChartObjects` hold the instance (reference identity, not `Contains`, in case a script overrides
`Equals`) and returns its `PanelIndex`. Fallbacks in order: `IndicatorRenderBase.ChartPanel
.PanelIndex`, then `NinjaScriptBase.Panel`, then `-1`. `/chart/{id}` panels and the `panel` field in
`/chart/{id}/indicators` both go through it, so they cannot disagree. Cost is O(panels × objects) per
indicator, which is nothing at chart sizes.

**`IsInput(PropertyInfo)` reads the attribute from metadata.** `GetCustomAttributesData()` and a
match on `AttributeType.Name == "NinjaScriptPropertyAttribute"`, instead of
`Attribute.IsDefined(p, typeof(NinjaScriptPropertyAttribute), true)`: metadata never has to *load*
the attribute type, so an obfuscated vendor assembly whose attribute type will not resolve cannot
throw the whole `inputs` list away, and a vendor DLL carrying its own copy of the type still matches.

**`T3000_LagDetector` really has no inputs and no plots.** Decompiled: zero `[NinjaScriptProperty]`
and zero `AddPlot(...)` calls. Its ~20 settings (`Input_LagThreshold`, `Input_AntialiasMode`, …) are
public properties with `[Display]` / `[Range]` only — NT8 shows them in the dialog, but they are not
constructor inputs, and API.md defines `inputs` as the `[NinjaScriptProperty]` set. So `"inputs":{}`,
`"plots":[]` is the correct answer for it, not a bug. If the `[Display]`-only settings are ever
wanted, that is an API.md change (a separate `settings` field), not a code fix.

**`GET /account?name=X`** with an unknown name returns 500 `{"error":…}`. API.md does not define that
case.

**JSON is hand rolled.** `NinjaTrader.Custom.csproj` references no JSON library — there is no
Newtonsoft reference in it, so adding one would mean shipping a dependency. `NaN`/infinity → `null`,
doubles with `InvariantCulture` `"R"`, `DateTime.MinValue` → `null`.

## Threading

- `HttpListener` on one background thread; each request handled on a thread-pool thread.
- Every chart read runs inside `ChartControl.Dispatcher.Invoke(…, DispatcherPriority.Send, …, 5s)`
  through `Ui<T>`. A busy or frozen chart thread yields a **504** after 5 s (`TimeoutException` →
  `Handle`) instead of hanging the listener or serializing `default(T)`.
- `/windows`, `/output/window` hop to each window's own dispatcher (NT8 gives windows separate UI
  threads, which is also why `Application.Current.Windows` is not used — the registry from
  `OnWindowCreated`/`OnWindowDestroyed`/`Reseed` is). `/windows` geometry is user32 (`GetWindowRect`,
  `IsIconic`, `MonitorFromWindow`) on the hwnd read inside that one hop: screen pixels, thread-agnostic.
- `/health`, `/compat`, `/ntstatus`, `/log` never touch a dispatcher — except `/health`'s
  `standingModal`, which makes ONE 500 ms hop for the title and only when a `*MessageBox*` window
  exists (falls back to the type name).
- `/account` runs on `Application.Current.Dispatcher` under `lock (Account.All)`.
- No `Draw.*`, no `MarketData`/`MarketDepth` subscription, no order or account mutation anywhere.
- The listener starts at `State.Configure` (every assembly load, so also after a hot reload) and when
  the Control Center window appears — `Start()` is idempotent — and stops on the Control Center's
  destruction and on `State.Terminated` of an instance that started something (`live`). A busy port is
  logged, not thrown, and retried 5 × 2 s on a pool thread (`Rebind`); `Stop()` bumps `startEpoch`,
  which makes a pending retry give up. Observed on NinjaTrader 8.1.8.2: a normal reload never needs the
  retry, because NT8 terminates the old assembly's AddOn first (`stopped` … 29 ms … `json self-test ok`
  of the new one). Forcing the retry path — a socket grabbing `0.0.0.0:7891` in that 29 ms gap and
  holding it 5 s — gives `port 7891 busy (The process cannot access the file because it is being used by
  another process); retrying 5x every 2s off the UI thread` → `bound on retry 3` → `reseed` →
  `listening`, `/charts` complete. A raw socket on the port is what "busy" looks like to http.sys.

## Core features 1.2.0 (observed on NinjaTrader 8.1.8.2)

- **`/health`** keeps its six 1.1 keys, in order, then adds `anyLive`, `anyNonSim`, `standingModal`,
  `pid`, `processStartUtc`, `assemblyName`, `assemblyLocation`, `assemblyBuiltUtc`, `moduleCount`
  (= discovered `Route_*`), `reflection` (`Compat` digest). Each connection gains `provider`
  (`"unknown"` when `Options` is null) and `canManageOrders` (`null` when `Options` is null).
  **`anyLive` is the real-money precondition every write path consults** — connection names are free
  text and must never be used for this. When the connections cannot be read:
  `connections:[{"error":…}]`, `anyLive:true`, `anyNonSim:true`.
- **`assemblyBuiltUtc`** = mtime of the file the assembly EXECUTES from. After a reload:
  `assemblyLocation` = `Documents\NinjaTrader 8\tmp\<guid>.dll`, `assemblyName` = that guid (not
  `NinjaTrader.Custom`). An empty `Location` (byte[] load) falls back to the moment the statics came up
  (`assemblyBuiltFrom:"loadTime"` in `/ntstatus`). `*Utc` fields end in `Z`; `startedAt` stays NT8-local.
- **`GET /ntstatus`**: `runningAssembly`, `assemblyBuiltUtc`, `assemblyBuiltFrom`, `newestSource`,
  `newestSourceUtc`, `sourcesScanned`, `sourcesNewerThanRunningCode` (null when the scan failed),
  `verdict` `current|stale|unknown`, `scanError`. Walks every `.cs` under `bin\Custom` (353 files,
  a few ms) — `EnumerateFiles("*.cs")` also matches `.csproj`, which NT8 rewrites on every compile,
  hence the explicit extension test. The answer is self-referential — the check runs inside the very
  assembly whose freshness is in question — so the staleness interlock proper lives on the Python side.
- **`/windows`** rows gain `owned`, `hwnd`, `left`, `top`, `width`, `height`, `isMinimized`, `screen`
  (all null when the window has no handle or its thread did not answer). Owned dialogs are listed too.
- **Self-tests at every Start**, visible as `selftest.*` rows in `/compat`: `json`, `core` (Ring seq
  cursor, RangeProblem, Deep), `uiTimeout` (a private dispatcher thread kept busy 700 ms; a 200 ms `Ui`
  must throw and must not run late), `outputHub` (the one line the bridge prints to Output tab 2 at
  start — `NT8Bridge v… listening on …` — must come back through the ring AND a listener).
- **Throwaway instances.** `State.Terminated` only calls `Stop()` on an instance that reached
  `Configure` or saw a window; an instance NT8 builds and drops can no longer close the live listener.

## Log

`Ring<string>(500)` (served by `/log`) plus an append to
`Documents\NinjaTrader 8\NT8Bridge.log` inside try/catch. One line per request
(`GET /charts -> 200 3ms`), plus start/stop and every 500 with its stack.

## Backtest (observed on NinjaTrader 8.1.8.2)

**Recipe A1 from `BACKTEST_RECIPE.md` works as written.** `StrategyBase.RunBacktest()` is
synchronous and loads its own bars: configure a fresh instance in `SetDefaults`, call it, read
`SystemPerformance` when it returns. No `SetState(Configure)`, no `Bars.GetBars`, no
`InitializeBars`, no optimizer, no Strategy Analyzer window. Measured: SampleMACrossOver, ES 12-26
5 Minute, 8 days → 714 ms, 79 trades. A tick-replay strategy on `chart:"first"` (a custom bar type
copied from the chart), 2 days → 5.0 s, 22 trades; 4 days → 11.1 s, 46 trades. The NT8 log printed
`Backtesting with Tick Replay was not designed to provide higher accuracy...` on the tick-replay runs,
which is the proof `IsTickReplay` took.

The exact sequence, on the `NT8Bridge-bt` worker thread (plain MTA background thread, no Dispatcher):

1. `t.Assembly.CreateInstance(t.FullName)` — the instance is already in `SetDefaults`: the ctor
   runs it, so the belt-and-braces `SetState(SetDefaults)` never fires.
2. `((StrategyRenderBase)s).IsInStrategyAnalyzer = true` (what the SA sets; never observed to matter,
   but it is free).
3. `[NinjaScriptProperty]` inputs by `PropertyInfo.SetValue`, coerced from the JSON double on the HTTP
   thread (`Coerce`: `long` for an integral input such as a volume threshold, enums by name or number).
4. `Instruments`, `BarsPeriods`, `IsTickReplays`, `IsResetOnNewTradingDays`, `TradingHoursArray` as
   length-1 arrays, then the scalar facades `Instrument`, `BarsPeriod`, `TradingHours`,
   `TradingHoursInstance`, `IsTickReplay`, `From`, `To`.
5. `Account` (resolved on `Application.Current.Dispatcher` under `lock (Account.All)` at POST time,
   `Name == Account.BackTestAccountName`), `Category = Category.Backtest`,
   `IncludeTradeHistoryInBacktest = true`, `PrintTo = OutputTab2`, `SetUniqueId()`.
6. Read back: `IncludeTradeHistoryInBacktest` and `IsTickReplay` both took in `SetDefaults`.
7. `RunBacktest()`. Returns with `State == Historical` and `SystemPerformance` populated.
   `s.Executions` is **empty** at that point even with 79 trades — trades live only in
   `SystemPerformance.AllTrades`; do not use `Executions` as a "did it run" signal.
8. Snapshot `SystemPerformance` into JSON, **then** `SetState(Terminated)`, `SetState(Finalized)`,
   `DbRemove()` if `Id != 0`. No teardown errors were logged on any run.

Recipe A2 (drive the ladder, load bars via `Data.Bars.GetBars`) is in the file as an automatic
fallback when A1 returns nothing; it has never been exercised. Recipes B and C are not implemented.

Gotchas, all observed on NinjaTrader 8.1.8.2:

- **`to` is inclusive and trading-day based.** `from 2026-09-10` on `CME US Index Futures ETH`
  yields a first trade at `2026-09-09T19:50` — the 09-10 session opens 18:00 the evening before.
  Not a bug; the `from`/`to` echo in the status document is the calendar window that was requested.
- **`winners + losers != trades`** when there are even trades (79 = 30 + 45 + 4). `API.md` has no
  `even` field; `winRate` is winners / trades.
- **`n` is `Trade.TradeNumber`, which is 0-based**, unlike the `API.md` example. Reported as NT gives it.
- `bars` = `Exit.BarIndex - Entry.BarIndex` is populated in backtests (observed 19, 14, 61);
  `null` only if both indices are 0.
- `mae` / `mfe` are `MaePoints` / `MfePoints` (points, per the `API.md` example row); the summary's
  `avgMae` / `avgMfe` are `Currency` (dollars). Same as the recipe's decision.
- `SystemPerformance.Calculate(s.Executions)` is the fallback only if `SystemPerformance` comes back
  null — never happened.
- **`output` is `[]` unless the strategy prints**: the run registers an `OutputHub` listener and keeps
  tab-2 lines. Open: the job-level capture is untested, because no strategy tried so far calls `Print`
  in a backtest. The hub itself is proven at every start by `selftest.outputHub`.
- `barsPeriod.type` accepts a `BarsPeriodType` name or an integer (a third-party custom bar type has
  no enum name, only its integer); `baseValue` (→ `BaseBarsPeriodValue`) is accepted on top of
  `API.md`'s fields. The chart's period is copied **field by field** (`CopyPeriod`), not `Clone()`d.
  An explicitly built period gets `MarketDataType.Last` — a fresh `BarsPeriod` defaults to `Ask`,
  which tick replay refuses.
- The strategy list keys on `Type.Name` (`SampleMACrossOver`), not `instance.Name` (a localized
  resource string). Only types in this AddOn's own assembly (`NinjaTrader.Custom.dll`) are listed,
  so a concrete base class a user derives from shows up too. Inputs are read off a fresh instance and
  include inherited ones, so a derived strategy lists its base class's inputs as well.
- **The 15-minute cap and `DELETE` on a running job both fire `SetState(Terminated)` from a foreign
  thread** (a `System.Threading.Timer` or the HTTP thread) because the worker is inside the
  synchronous `RunBacktest()`. `Terminate()` therefore dispatches that `SetState` through
  `ThreadPool.QueueUserWorkItem` rather than running it on the caller's thread — blocking NT8's UI
  thread on NinjaScript's state machine while the worker drives the same instance deadlocks shutdown.
  The record flips to `timeout` / `cancelled` at once; the worker only picks the next job when
  `RunBacktest()` returns. `DELETE` on a finished id just drops the record.
- `POST` answers in ~10 ms and the listener stayed responsive during runs: 15 `/health` probes
  during the 11 s run, worst case 25 ms. The single worker means further `POST`s queue.
- `Stop()` (Control Center closed, or `State.Terminated` on F5) cancels queued jobs, terminates a
  running one and joins the worker for at most 3 s. An F5 while a backtest runs abandons that run.
- The `jobs` dictionary only shrinks on `DELETE` (known slow leak, deliberately not capped).
- Open: whether a headless run leaves a row in the NT8 Strategies grid. The `DbRemove()` guard stays in.
- `POST /chart/{id}/screenshot` parses its body with the same `ParseJson` (no regex).

## Headless RunOptimization

**It works.** `StrategyBase.RunOptimization(Action<StrategyBase>)` runs headless and `Optimizer.Strategies`
IS populated — by the `StrategyBase.Optimizer` **setter**, before `RunOptimization` is even called. The
route does not die at `Strategies[0]`; `BACKTEST_RECIPE.md` §2.3 records that as an *expected* failure,
but it is not an observed one. Observed on NinjaTrader 8.1.8.2, from a throwaway probe AddOn, with
Playback connected, `anyLive:false`, the Backtest account, no window, on a plain background thread:

1. `Configure(job)` exactly as `/backtest` does (SampleMACrossOver, ES 12-26, 5 Minute, 09-10..09-17).
   `s.OptimizationParameters.Count == 2` (`Fast`, `Slow`, both `Int32`) on the fresh instance — the
   decompiled getter's `return null` is a stripped body, not the behaviour.
2. `s.OptimizationFitness = new MaxNetProfit()` and `s.Optimizer = new DefaultOptimizer()` both read back
   (same object). Straight after the setter: `opt.Strategies.Count == 1`, `opt.State == SetDefaults`,
   `KeepBestResults == 10`.
3. `Fast` set to `Min 5 / Max 15 / Increment 5`, `Slow` left at `25/25/1`.
4. `RunOptimization(cb)` returned after 0.36 s with `opt.State == Active`, `NumberOfIterations == 3` — it
   is asynchronous. The callback fired 1.05 s later: `Optimizer.Results` = 3 slots, all with trades:
   `61 trades / 3437.5`, `62 / 350`, `87 / 87.5`.
5. Those are **exactly** `nt_optimize`'s three rows for the same grid (`Fast` 10, 15, 5). The template
   instance itself stays at `State.Configure` with an empty `SystemPerformance`: results live on
   `Optimizer.Results` only. `TearDown` was clean, nothing appeared in the NT8 log or the Strategies grid,
   the Backtest account stayed flat.

Cost: 1.4 s for 3 iterations, against ~0.4 s for the same 3 combos through the Python loop over
`POST /backtest` (0.13 s per combo once the status poll backs off). So there is still no reason to build
an AddOn-side `POST /optimize`: same numbers, slower at this size, more NT8 surface. Revisit only for a
grid where NinjaTrader's per-iteration worker threads would beat one serial worker (tick replay, hundreds
of combos). Not tried: a custom `Optimizer` subclass, `GeneticOptimizer`, tick replay, walk-forward
(`OptimizationPeriod` / `TestPeriod`), and what happens when `Stop()` lands mid-optimization.

## Backtest job lifecycle races and input validation

How the job lifecycle in `NT8Bridge.Backtest.cs` is made race-free, and why each guard exists:
- `Configure()` tears itself down on any exception, so a throw after `SetUniqueId()` (a stripped
  input setter, `IncludeTradeHistoryInBacktest`/`IsTickReplay`/`Account` rejected) cannot leak a
  live instance + strategy-DB row. Covers both call sites (A1 and the A2 retry) with one guard.
- `RunOne` re-checks `job.Cancel` right after each `Configure()` call, before `RunBacktest()`/`RunA2`.
  Closes the window where a `DELETE` lands while the worker is still inside `Configure()` (Terminate
  sees `job.Strat == null` and can touch nothing): without it the job runs to completion anyway.
- `job.Strat` is cleared (under `jobGate`) *before* `TearDown()` runs, both at the A1→A2 switch and in
  RunOne's finally. Terminate() reads `job.Strat`, so this stops a DELETE/cap-timeout from calling
  `SetState` on an instance that's mid-`Finalize`/`DbRemove` on the worker thread.
- Terminal state transitions (`running` → `done`/`error`/`timeout`/`cancelled`) go through a
  `TryFinish` compare-and-set under `jobGate`, so the cap timer, the worker and DELETE cannot
  relabel a result that has already settled — otherwise a `done` is overwritten by a timeout that
  fired a moment later.
- `job.FinishedAt` is set *before* the state flip in the worker, so a GET can never observe
  `state:"done"` with `finishedAt:null`. `CancelAll` and `BacktestCancel` set it too.
- **`Terminate()` never calls `s.SetState(State.Terminated)` synchronously on the caller's
  thread** — it is dispatched via `ThreadPool.QueueUserWorkItem`. `Stop()` runs on NT8's UI thread
  (`OnWindowDestroyed`/`OnStateChange`), and blocking that thread on NinjaScript's state machine
  while the worker thread drives the same instance wedges shutdown. The bounded `Join`s in `Stop()`
  are what keeps shutdown safe when a run does not unwind in time.
- `JGetStr`/`JGetBool`/`JGetInt` throw (`BadRequestException`, caught by `BacktestStart` → 400)
  when a key is *present with the wrong JSON type*, instead of silently returning the default.
  Without that, a stringified `"tickReplay":"true"` runs as `tickReplay:false` and reports `done`
  with a plausible-but-wrong summary, and a stringified `barsPeriod.value` silently runs a 1-minute
  backtest. An empty string counts as a present value (`"account":""` is a 400, not the default
  backtest account).
- Range guard: `barsPeriod.value < 1` or `value2 < 0` is a 400, not a queued job that only surfaces
  as `state:"error"` later.
- `POST /backtest`'s 202 body always echoes the literal `"state":"queued"` instead of re-reading
  `job.State`, which could already have moved to `running`/`done` by the time the HTTP thread read it.
- `GET /strategies` tears down each probe instance (`NewStrategy` → `TearDown` per type) instead
  of leaking one un-finalized `StrategyBase` per strategy per call.

## Lessons from cli-nt-bridge

Every one is an **observation on NinjaTrader 8.1.8.2**, not a claim about future builds; most were
paid for in hours by `eman007/cli-nt-bridge` (MIT) or by this repo. Other documents cite these by
number, so do not renumber. File and line references point into the cli-nt-bridge source. Where the
core already answers a lesson, the pointer says where; the lesson stays, because the next module
writer needs the reason, not just the helper.

**Threading and the dispatcher**

1. NT8 is multi-UI-threaded, worse than it sounds: 179 windows across 26 distinct UI threads
   measured. A central loop reading `w.Left`/`.IsVisible`/`.WindowState` throws on every window,
   and `new WindowInteropHelper(w).Handle` throws too. `EnumWindows` filtered by process id is
   the only thread-agnostic inventory. (cli-nt-bridge `CHANGELOG.md:641-651`)
2. `Globals.AllWindows` is safe to snapshot off-thread; every member read off a window in it is
   not. Marshal **per window**, bounded, never one global `Invoke`. → `TopWindows()` + one `Ui<T>`
   hop per window.
3. `Globals.AllWindows` does **not** contain owned/modal windows. Use an "including owned" walk.
   (`Playback.cs:802`) → `KnownWindows(out owned)`.
4. A `Dispatcher.Invoke` with a timeout **aborts and returns** — it does not throw. Unguarded,
   that is a document of zeros that looks like data. → fixed in `Ui<T>`, proven every start
   by `selftest.uiTimeout`.
5. A standing WPF modal blocks that dispatcher; NT opens order-rejection boxes with **no owner**,
   so they are in neither `AllWindows` nor any window's `OwnedWindows` — walk
   `PresentationSource.CurrentSources`. Match the trailer `affected Order:`, not one wording:
   matching on one phrase dismissed 1 of 7 boxes. (cli-nt-bridge `CHANGELOG.md:254-262`) Our
   `StandingModal()` walks the window registry only, so an ownerless box is a known blind spot.
6. Never call `Account.Cancel`/`Flatten` (or any NT call) while holding a `Cbi` collection lock —
   snapshot under the lock, act after releasing it. ("Lesson #159",
   cli-nt-bridge `addon/NT8BridgeServer.cs:2718-2720`)
7. Create a strategy instance on the **Control Center window's** dispatcher. Created on a
   thread-pool thread, `StrategyEnable` does `strategy.Dispatcher.Invoke` against a thread that
   never pumps messages and the Control Center freezes **forever**, including the user's own
   checkbox. Proven with a dotnet-dump. (`Playback.cs:6124-6141`)

**Reload and staleness**

8. A `.cs` landing in `bin\Custom` makes NT recompile and hot-reload by itself (~19 s;
   temp-write + `os.replace` triggers it too). The reload path is our **normal** deploy, not a
   rare event.
9. A reload does **not** close already-open AddOn windows or re-create the Control Center. The old
   assembly keeps executing while the new statics start empty, and neither statics nor `Type`
   identity can detect the split — both reset. The interlock must live in an artifact that
   crosses the reload boundary (a file, a timestamped log line), and must be **verified out of
   band**: an old listener still holding port 7891 answers `/health` truthfully about itself.
   (cli-nt-bridge `README.md:153`) → `Start()` from `State.Configure` + `Reseed()`.
10. After a reload the running code is `Documents\NinjaTrader 8\tmp\<guid>.dll`, not
    `bin\Custom\NinjaTrader.Custom.dll`. The DLL's mtime **and** its ModuleVersionId both lie.
    The only correct staleness test is **executing-assembly build time vs newest `.cs` under
    `bin\Custom`**. A rule of thumb such as "restart after every third successful reload" is not a
    substitute. → `/health.assemblyBuiltUtc`, `/ntstatus`.
11. A reload restarts indicators, can interrupt a running strategy, and **orphans bars-type
    instances** — they keep executing the pre-reload assembly and publishing into its statics.
    Only an NT process restart fixes it. This matters for any custom bar type in use.
12. After a reload the same class is loaded twice (old + new `NinjaTrader.Custom`), so `Type`
    identity and `IsInstanceOfType` report a mismatch between two types with the **same
    FullName**. Compare by FullName string. (`Playback.cs:6175-6188`)
13. Static events live in `NinjaTrader.Core`, which is **not** reloaded. Every `+=` needs its
    `-=`, or each reload stacks another handler running dead code and pins every dead
    `NinjaTrader.Custom` assembly. → the `Stop_<Module>()` rule in "Module seams"; `OutputEvent`
    is the core's single subscription (`OutputHub`).

**Compiling**

14. NT's compiler always builds the **whole tree**. A `--type` argument is meaningless, and any
    broken `.cs` in `bin\Custom` fails the build for everything. (This is why
    `server/tests/fixtures/*.cs` must never be installed.)
15. Duplicated `#region NinjaScript generated code` blocks (8 copies in one file observed) break
    the whole tree with CS0111/CS0102 errors that name the symbol but never the cause. Detect with
    an anchored regex `^[ \t]*#region ...`, **never a substring count** — the files most likely to
    mention the marker are the ones whose header comment explains this rule, and a count-based
    strip truncated 21 % of a healthy file. Write a `.bak` first.
16. Roslyn's `Diagnostic.GetMessage` has no zero-arg overload; line positions are 0-based.
17. `checkCompileOnly` true vs false produce identical diagnostics. Report `assemblyReloaded`
    truthfully, and keep reload a **separate command**, never a flag on compile.
18. A compile timeout is not proof of failure. 30 s is too short; 120 s compile / 240 s reload,
    with a hint separating "still compiling" from "AddOn not loaded".

**Reading state without being lied to**

19. Never read state from the UI element that merely displays it: the Playback slider's bounds
    are the range you typed, not indexed data; the Playback source radios
    are display-only; the Strategies checkbox starts nothing and proves nothing.
20. `enabled` is not running. Believe the strategy's own `State` reaching `Realtime`. Enabling is
    asynchronous, so an immediate re-read reports a transitional state — re-read, never re-click;
    a second click toggles it back off.
21. Read a value back off a **freshly resolved** chain, never off the object you just wrote to —
    a detached object echoes its own value happily. And report it as a **value**, never a verdict:
    setters legitimately transform (`BarsPeriod "77077:120:1"` reads back `"Wave 120"`), so an
    equality check manufactures false failures.
22. Writing `Strategy` on an SA tab installs a **fresh** `StrategyTemplate`; every key written
    afterwards lands on a detached object, `SetValue` does not throw, and each key reports "set"
    with its value echoed. Fix = re-resolve targets **per key** **and** apply swap keys **first**
    (Dictionary order is not a contract). `From`/`To`/`BarsPeriod`/`IsTickReplay` live only on the
    template; `InstrumentOrInstrumentList` also exists on the tab and survives the swap — which is
    why the instrument looked applied while the bar type silently did not.
23. Detect a finished SA run by comparing `SystemPerformance` **by reference**, not by
    `Results.Count` — a re-run **replaces** the entry, so the count never changes. Require
    progress-gone **and** every new row carrying a `SystemPerformance`, stable for two ticks,
    and allow settled rows to decide alone because a fast run can finish between ticks.
24. A walk-forward writes the **same Guid** onto two different entry objects (one with 1 child,
    one with 3). Dedupe by Guid keeping the fuller object; `OptimizationResults` was **empty** on
    every entry, so read the in-sample ranking off the child `Optimize` rows.
25. `null` is not `[]`. A member that did not resolve serializes as `null`; an unreadable chart and
    an empty chart are different claims, and collapsing them is how "the run produced nothing" gets
    mistaken for "the run was fine". Same for a scan that could not run: report **unavailable**,
    never **empty**. → the reflection rule in "Core helpers": `resolved:false` + `null`.
26. `ok` on a step means "this step did its job", never "the answer is yes". Passing the answer as
    the ok flag made a healthy running strategy report FAIL and killed two good runs.
27. A reflection **miss must not degrade to 0**. If the trust flag resolves and the value does not,
    flip the trust flag off and attach a note.
28. Log the **step** that failed in every reflection chain — a silent miss looks like "the bot
    printed nothing". And flatten `InnerException` up to three levels, or the log names a symptom
    with no cause. → `Compat.Set(key, resolved, detail, value)` and `Deep(ex)`.
29. `NinjaScriptBase.Name` is legitimately blank on scripts that hide their on-chart label (the
    label **is** `Name`). The snapshot and the lookup must fall back to the **same** thing, or rows
    list fine and then fail to match. → `NameOf(object)`, used by every endpoint.
30. `LogEventArgs.Name` is the identifier; `Message` is only its `ResourceManager` rendering from
    (`ResourceType`, `Name`) and changes with NT version and UI language. **Match on `Name`.**
31. The Control Center owns a UI thread different from `Globals.MainThreadDispatcher`, NT8's real
    windows are not in `Application.Current.Windows`, and the Strategies tab is **virtualized**
    while inactive — three independent reasons a naive grid read returns a convincing empty.
    The tab index is not stable across machines.
32. `Globals.ActiveWorkspace` is the workspace name. The obvious probe list
    (`CurrentWorkspace`/`Workspace`/`WorkspaceName`) matches nothing, so a field fed from it is
    always null — and a degrade-to-null discipline hides that it never resolved.
33. A feed can go dark while NT keeps reporting **Connected**. The only tell is last-tick age,
    computed inside NT from one clock. **`ageMs:null` is stale, not fresh** — and
    `HasSeenMarketData` is what separates "never had data" from "went dark".
34. Inadvertent vs user disconnect is classifiable **only at the event**, from
    `ConnectionStatusEventArgs.Error`: `ConnectionLost` → inadvertent; `Disconnected` with
    `NoError`/`UserAbort` → user parked it. `Connecting`/`Disconnecting` are transient — leave the
    prior classification alone. A connection already down when the bridge loaded is unclassifiable.
35. A connections report must **union** configured options and live connections. Walking
    configuration alone made a connected Live connection vanish from a report and a reader
    concluded nothing was connected.
36. An explicit `Disconnect()` silently **disables every running strategy**, and NT restores none —
    not on reconnect, not on an app restart. Toggling Playback does the same.

**Backtests, data and accounts**

37. `Execution.DbGet` throws on a `DateTime` whose `Kind` is `Local` (NT's trade DB is in exchange
    time). Defaulting `to` to `DateTime.Now` made **every** intraday pull fall back to the 3-day
    in-memory window, and the failure looked like thin data. Use `DateTimeKind.Unspecified`.
38. Pad the DB query start by 2 days, then filter the resulting **trades** by `Exit.Time` inside
    the real range so the pad never reaches the metrics. In-memory `Account.Executions` is capped
    at `Account.LookbackDaysExecutions` (= 3), so the union of DB + memory is the only complete
    source; dedupe by `ExecutionId`.
39. NT **never persists per-fill commission** — `DbGet` returns 0. The Trade Performance window
    recomputes it from the account's Commission template at display time. Reconstruct it and say
    which source each trade used. On a funded/prop account there is no local template, so the real
    costs exist only in that window's in-memory cash history.
40. `Trade.ProfitCurrency` is **net** of commission and fee when the provider stamps them on fills,
    so "gross" and "net" silently swap meaning between a Sim account and a prop account.
41. Market Replay reads `db\replay` (`.nrd`); Playback/Historical reads `db\tick` (`.ncd`).
    Answering from the wrong store is a silent wrong answer, and front-month recordings are often
    filed under the **continuous** name (`MNQ ##-##`, not `MNQ 09-26`).
42. `.ncd` is one file per **hour** per data type (`YYYYMMDDHH.<Last|Bid|Ask>.ncd`), so a per-day
    coverage answer is a **pre-flight** — "is there anything for that day", not "is it complete".
43. Never download the current or future day: replay data is partial until the session closes.
    Compute the cutoff in America/New_York.
44. A corrupt `.nrd` decodes to **in-format garbage**, not an error. Cross-check the decode against
    the header's per-slot volume sum and price range and write nothing on mismatch. Truncated files
    are normal — salvage the clean prefix, drop the incomplete final record, skip the integrity
    check for that file. An empty 0-row export that did not throw is worse than a failure: discovery
    records it as done and skips that date forever.
45. Never string-compare `DD.MM.YYYY` times: `"14.01.2026" >= "13.03.2026"` is true because the day
    field sorts first. Two runs reported ok at 5.7 % and 13.2 % coverage before this was caught.
    Related: `date.fromisoformat` is not a format check — on 3.11 it also accepts `20260607` and
    `2026-W23-1`. Pin the shape with a pattern.
46. An optimize/walk-forward with an **empty** `OptimizationParameters` is refused outright ("The
    strategy must have at least one parameter to optimize.") and runs nothing while every other
    setting looks right.
47. `@DefaultOptimizer.cs`'s step loop stops at `min + i*inc > max + inc/1000000` — copy that
    epsilon or a double grid silently drops its last point.
48. Refuse `From`/`To` outside 1990..2090 or inverted: a fresh NinjaScript's placeholder range
    (`From 2099-12-01 / To 1800-01-01`) restored from a template made **NinjaTrader load until it
    stopped answering**. → `RangeProblem(from, to)`; call it before arming ANY range.
49. `High` order fill resolution is not available with Tick Replay. Four of the six backtest
    cost/fill setters have stripped setters and can no-op depending on State — an unread-back
    write is a lie, and a result document that does not echo what it ran with is a silently wrong
    number later.
50. Decode subprocess output with `errors="replace"`: `tasklist`/`schtasks`/`taskkill` write in the
    console codepage and the first non-ASCII byte raised `UnicodeDecodeError`.
51. `Print()` output is an **output** channel, never an **evidence** channel. No control-flow step
    may wait on or conclude from a bot's line — the bot may print nothing at all.

**52. An offline compile is not a load check, and an unknown commission template is accepted
silently.** `nt_check` is a real `dotnet build` of `NinjaTrader.Custom.csproj` — the same compiler
and references as F5 — so it catches every compiler error and **nothing** NinjaTrader's own load
step sees. Observed on NinjaTrader 8.1.8.2: a strategy that sets
`BacktestCommissionTemplate = "DoesNotExist"` compiles clean offline, is **not** rejected at load,
reads the value back as set, and is charged the **DEFAULT** commission template instead (observed
351.36, identical to naming no template at all). The read-back cannot catch it, so the guard has to
sit at the boundary this repo controls: `POST /backtest` checks the template name against
`Cbi.Commission.All` and answers `400` listing the known names before NinjaTrader ever sees the bad
value. `server/tests/fixtures/BadLoadStrategy.cs` documents that silent-default trap, and
`server/tests/test_fixtures_compile.py` pins the offline half. The fixtures must never be installed
into `bin\Custom` — see lesson 14.

## The loaded window (`barsFrom`/`barsTo`/`warnings`) and strict integer inputs

- **Playback caps a backtest's bars at the replay clock.** `RunBacktest()` with a Connected
  Playback connection loads a window that ENDS at the clock and keeps only its length; `from`/`to` in the
  document are only the request. The reference decompile has stripped bodies for `RunBacktest`, `Bars.GetBars`
  and `Globals.Now`, and no public member selects the end time, so **there is no clean way to force the requested
  window**. The fix is honesty: `Backtest_Window()` reads `BarsArray[0]` first/last bar time before teardown into
  the new keys `barsFrom`/`barsTo` (18, 19; null until done) and writes `warnings` (20; `[]` normally) when
  `barsTo < from` || `barsFrom > to` || (Playback Connected && `to` > `Playback_Clock()`). Keys 1-17 unchanged.
  To backtest dates after the clock, disconnect Playback first; the bridge never does it for you.
- **`Coerce` refuses a non-whole number for an integral input**: `Convert.ChangeType(5.5, int)`
  is 6. Now a 400 naming input and value; 5.0 still runs as 5. Covered by `selftest.core`.
