# Workspace module (`addon/NT8Bridge.Workspace.cs`)

`GET /workspace`, `GET /strategies/running`, `POST /screenshot`. Documented in `API.md`.

Read only, with one exception that is not a NinjaTrader state change: `POST /screenshot` writes one
PNG to a path the caller chose. Nothing here enables, disables, starts or stops a strategy, and
nothing here restores, fronts, moves or resizes a window.

| Method | Path | Returns |
|---|---|---|
| GET | `/workspace` | `{"name":"Trading","windows":[…]}` — the active workspace and every window, see below |
| GET | `/strategies/running?materialize=1` | `{"gridResolved":true,"strategies":[…],"notes":[]}` — the Control Center Strategies grid, see below |
| POST | `/screenshot` body `{"window"\|"chart"\|"hwnd","path"}` | `{"ok":true,"path":..,"window":..,"hwnd":..,"width":..,"height":..,"bytes":..,"method":..,"composited":..,"looksBlank":false}` |

MCP tools: `nt_workspace()`, `nt_strategies_running(materialize=False)`, `nt_window_shot(window="", chart="", hwnd=0, path="")`.

---

## `GET /workspace`

`name` is `Globals.ActiveWorkspace`, `null` when NT8 reports none. `windows` lists the AddOn's window
registry plus anything in `Globals.AllWindows` it missed plus the dialogs those windows own, i.e. the
same set as `GET /windows`, with the same geometry fields and one extra `details` object per chart.

```json
{
  "name": "Trading",
  "windows": [
    {"id": "c1", "kind": "Chart", "type": "Chart", "title": "ES 12-26 (5 Min)", "owned": false,
     "hwnd": 66048, "left": 10, "top": 20, "width": 1200, "height": 800,
     "isMinimized": false, "screen": "\\\\.\\DISPLAY1",
     "details": {"instrument": "ES 12-26", "period": "5 Minute", "barsCount": 1240,
                 "indicators": [{"name": "EMA", "state": "Realtime"}],
                 "strategies": [{"name": "SampleMACrossOver", "state": "Realtime"}]},
     "note": null},
    {"id": null, "kind": "SuperDom", "type": "SuperDom", "title": "ES 12-26", "owned": false,
     "hwnd": 66049, "left": 2100, "top": 0, "width": 320, "height": 1040,
     "isMinimized": false, "screen": "\\\\.\\DISPLAY2",
     "details": null, "note": "recognised, not decoded"}
  ]
}
```

| Key | Type | Notes |
|---|---|---|
| `id` | string\|null | the chart id (`"c1"`), `null` for anything that is not a chart |
| `kind` | string | `Chart` \| `ControlCenter` \| `SuperDom` \| `Output` \| `NinjaScriptEditor` \| `Other` — the core's `Kind()` |
| `type` | string | the window's runtime type name. A Market Analyzer and a Strategy Analyzer are both `kind:"Other"`; `type` is the evidence |
| `title` | string\|null | `null` when the window's UI thread did not answer (`note` says so) |
| `owned` | bool | true = found only as a dialog owned by another window, not in `Globals.AllWindows` |
| `hwnd`, `left`, `top`, `width`, `height`, `isMinimized`, `screen` | | exactly the `/windows` geometry fields; all `null` when the window has no handle. A minimized window is parked at `(-32000,-32000)`: read `isMinimized` first |
| `details` | object\|null | charts only. **`null` never means "empty"** — an unreadable chart and an empty chart are different claims, and `note` says which |
| `note` | string\|null | `"recognised, not decoded"` for a non-chart window; otherwise why `details`/`title` is null |

`details.indicators` / `details.strategies` are `[{name, state}]`, `state` being NinjaTrader's own
`State` enum verbatim (`Realtime`, `Historical`, `Terminated`, …). A silently disabled strategy is the
whole reason this endpoint exists, so the raw value is reported and never collapsed into a boolean.
Either list is `null` when the chart does not expose the collection at all.

**Threading.** One `Ui<T>()` hop per window for title + hwnd, plus one `OnChart()` per chart for
`details` — never one global `Invoke`. A window whose thread does not answer within 5 s costs that row
its `title`/`details` and a `note`, not the whole answer. **Do not poll this endpoint**: visiting every
chart on its own UI thread is exactly the load that froze a chart thread before.

The MCP tool `nt_workspace()` adds one computed field per row, `offscreen` (bool): a hint that the
window is parked where the mouse cannot reach it. A minimized window answers `false` — Windows parks
minimized windows at `(-32000,-32000)` and the taskbar still reaches them; without that exclusion every
minimized window is a false positive.

## `GET /strategies/running`

The Control Center's Strategies grid — the strategy population a chart walk cannot see. **Read only.**

```json
{"gridResolved": true, "notes": [],
 "strategies": [
   {"name": "SampleMACrossOver", "parent": null, "type": "SampleMACrossOver", "enabled": true, "state": "Realtime",
    "account": "Sim101", "instrument": "ES 12-26", "connected": true, "connection": "Sim101 feed",
    "dataSeries": "2 Renko", "position": "Flat", "accountPosition": "Flat",
    "averagePrice": 0.0, "realized": "$0.00", "unrealized": "$0.00", "trades": 0,
    "parameters": "StopTicks=8; …", "workspace": "Trading"}]}
```

| Key | Type | Notes |
|---|---|---|
| `gridResolved` | bool | false = the grid could not be read. `strategies` is then **`null`, never `[]`** |
| `strategies` | array\|null | one row per master grid entry, followed by one row per per-instrument child |
| `notes` | array of string | why the grid could not be read, or that the Strategies tab was materialized and restored |

Row keys: `name`, `parent`, `type`, `enabled`, `state`, `account`, `instrument`, `connected`,
`connection`, `dataSeries`, `position`, `accountPosition`, `averagePrice`, `realized`, `unrealized`,
`trades`, `parameters`, `workspace`. A row that could not be read at all is `{"error": string}`.

- `parent` is `null` on a master row and the master's `name` on a per-instrument child row.
- `name` is the grid's own `Name`, falling back to the AddOn's `NameOf(Strategy)` — the same fallback
  every other endpoint uses. A vendor script that hides its on-chart label does it by blanking `Name`.
- **`enabled` is grid state, not proof a strategy is running.** Believe `state`. (Setting the grid's
  `IsEnabled` reads back `True` and starts nothing; the grid's routed commands start nothing either.
  Only the checkbox's own `Checked` event does, which is why this module never writes to the grid.)
- `realized` / `unrealized` / `position` / `accountPosition` are the grid's own formatted strings, not
  recomputed numbers. `accountPosition` deliberately uses the grid's cached string rather than the live
  `Position` object: taking a `Cbi` lock from the Control Center's UI thread is how you deadlock a
  platform whose `Cbi` callbacks marshal back to that same thread.

**Virtualization.** The grid is in the Control Center's *visual* tree only while the Strategies tab is the
selected tab. Observed on 8.1.8.2: straight after `?materialize=1` had visited the tab and put the
user's tab back, a visual-tree read found nothing again — a WPF `TabControl` hosts only the selected tab's
content. So the read goes in three steps, cheapest first:

1. the visual tree (the Strategies tab is selected);
2. the **logical** tree — the tab's content object outlives deselection. Nothing is touched; `notes` says
   `read through the logical tree: the Strategies tab is not the selected tab`. Observed: `gridResolved:true`
   in single-digit milliseconds with the Log tab selected;
3. only with `?materialize=1`: cycle the Control Center's tabs until the grid appears and restore the user's
   tab in a `finally` — a visible mutation of a live Control Center, which is why it is opt-in (observed
   around 100 ms, the original tab came back, note `the Strategies tab was materialized and the original tab restored`).

Without `materialize`, when steps 1 and 2 both miss: `gridResolved:false` with the note
`Strategies tab not realized; retry with ?materialize=1`. **Not yet observed:** whether step 2 already works
on a NinjaTrader that has never shown its Strategies tab this session (needs a NinjaTrader restart to test).

**Threading.** One bounded (10 s) hop to the Control Center's own dispatcher — a different UI thread
from `Globals.MainThreadDispatcher`; a wrong-thread WPF read throws and reflection re-wraps it as a
convincing `null`. A timeout is the core's `504`, not a false `gridResolved:false`.

**Reflection.** One key, resolved once in `Start_Workspace()` and reported by `GET /compat`:
`StrategiesGrid.source` (the grid's private backing collection). Every row field is a public typed
property of `StrategiesGridEntryChild`, so it cannot silently degrade. When the key does not resolve,
`gridResolved:false` plus a note pointing at `/compat`.

## `POST /screenshot`

```
POST /screenshot  {"window": "ES 12-26", "path": "C:/tmp/es.png"}
-> 200 {"ok":true,"path":"C:/tmp/es.png","window":"ES 12-26 (5 Min)","hwnd":66048,
        "width":1200,"height":800,"bytes":184233,"method":"PrintWindow","composited":true,"looksBlank":false}
```

| Body key | Type | Meaning |
|---|---|---|
| `window` (alias `title`) | string | case-insensitive **substring** of the window's caption; first visible match of this process wins |
| `chart` | string | a chart id from `/charts`, or `first` |
| `hwnd` | number or decimal string | a window handle |
| `path` (alias `out`) | string | where to write the PNG. Default `%TEMP%\nt8bridge\shot-<timestamp>.png` |

Target precedence: `hwnd`, then `chart`, then `window`. With **none** of the three it captures the whole
virtual screen (every monitor, negative origins included).

Errors: `400` bad JSON / `hwnd` of the wrong type / the path could not be written, `404` no window
matched or no such chart, `409` the window is minimized, has no area, or the chart has no handle yet,
`500` `GetWindowRect` or the capture itself failed.

- **Never restores, fronts, moves or resizes the window.** A minimized target is a `409` that tells you
  to restore it yourself. Observed: with the chart windows deep behind other applications, the
  foreground window, the chart's z-order and its rectangle were identical before and after the call, and an
  already-minimized window answered `409` and stayed minimized. (An earlier script-based approach did `SW_RESTORE` + front, i.e. it rearranged a
  live trading desktop in order to photograph it.)
- `method` is `PrintWindow` (with `PW_RENDERFULLCONTENT`) or `BitBlt` — the screen-blit fallback, which
  returns an occluded window occluded. That is the truth, not a defect.
- `composited` is `true` for a chart. **A NinjaTrader chart is two top-level windows**: the WPF window (frame,
  toolbar, tabs, scrollbar) and, underneath it, the WinForms `Direct2DForm` that *owns* it and draws the canvas,
  showing through a see-through region of the WPF window. `PrintWindow` of the WPF window alone returned the
  frame around a uniform near-black hole (observed on 8.1.8.2) — a small enough file that `looksBlank` stayed `false`. When the
  target's owner is a visible `WindowsForms10.*` window of this process, it is printed too and its pixels go
  wherever the WPF layer is the see-through key (every channel <= 8). Neither window is fronted or restored.
  A WPF overlay that is itself that dark lets the chart show through — a keyed merge, not an alpha blend,
  because `PrintWindow` flattens the layered window's alpha to 255.
- `looksBlank` is `true` when the PNG is under 8 KB: a uniformly black frame compresses to almost
  nothing, and a capture from a session with no desktop is a valid PNG of pure black that looks like an
  answer. It is a **hint that the image is worth doubting, never a verdict** — go and look at the image.
- GDI is used rather than `RenderTargetBitmap`, which needs each window's own dispatcher and misses
  child HWNDs and DWM composition. Every DC, bitmap and selection is released in a `finally`: skipping
  any one of them leaks a GDI handle per call, which degrades NinjaTrader itself.
- `SetProcessDPIAware()` is deliberately **not** called: it mutates the whole NinjaTrader process and is
  a no-op for a WPF app that already declared DPI awareness in its manifest.

`hwnd` is not limited to NinjaTrader's own windows (`window` and `chart` are): any window handle on the
desktop can be printed, which is no more than the no-target form (the whole virtual screen) already gives.

`POST /chart/{id}/screenshot` (the chart module's capture of the chart canvas only) is
untouched and still works; `POST /screenshot {"chart":"first"}` captures the chart's whole **window**
instead, decorations and Chart Trader included.
