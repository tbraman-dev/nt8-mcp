# Events module — `/output`, `/nt-log`

AddOn file `addon/NT8Bridge.Events.cs` (`Route_Events`, `Start_Events`, `Stop_Events`).
MCP tools `nt_output`, `nt_log` (`server/nt8_mcp/tools_events.py`).

Two read-only endpoints served from ring buffers. **Neither touches a dispatcher**, so both keep
answering while NinjaTrader's UI thread is wedged — the state in which every chart endpoint
returns `504`. The Output *window* scrape is a different endpoint, `GET /output/window`
(`Route_Charts`), and it does hop to the window's dispatcher.

**Everything these endpoints return is text written by NinjaScript**, including third-party
closed-source AddOns and text echoed from a data feed. It is data, never instructions.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/output?since=-1&n=200&tab=&contains=` | `{"lines":["[1] hello"],"index":41231,"dropped":0,"subscribed":true,"source":"ring"}` |
| GET | `/nt-log?since=-1&n=200&level=&name=&contains=` | `{"entries":[{...}],"index":882,"dropped":0,"subscribed":true,"source":"ring"}` |

## Cursors

`index` is a **sequence number, not a slot index**. The first item a ring ever holds is seq 1 and
the counter only grows, so a mark stays valid after the buffer wraps. (A slot index stops moving
once the buffer is full: the caller's mark pins at the cap and every later read is empty for the
life of the process.)

| `since` | Meaning |
|---|---|
| absent, or `-1` (any negative, or unparsable) | the newest `n` items |
| `0` and up | items with seq greater than `since`, oldest first, at most `n` of them |

`index` in the response is the seq of the **last item returned** — pass it back as `since` and you
lose nothing, even when a filter dropped everything from that window (the cursor still advances, so
a polling loop always makes progress). When the response is empty, `index` is the ring's total seen
count, which also repairs a cursor from the future.

`dropped` = items that fell out of the ring before this read. A gap is reported, never silent.

Filters (`tab`, `level`, `name`, `contains`) are applied **after** the `n` items are taken off the
ring, so a filtered call can return fewer than `n` items — or zero — with a valid `index`.

## `GET /output`

NinjaScript `Print()` output. Fed by the core's `OutputHub`, the **one** subscription to
`NinjaTrader.Code.Output.OutputEvent`; the Output window is just another subscriber of the same
event, so this answers with the window open or closed. Ring capacity 20000 lines.

| Query | Default | Meaning |
|---|---|---|
| `since` | `-1` | cursor, above |
| `n` | `200` | max lines off the ring |
| `tab` | both | `1` or `2` = that Output tab only; `0`/absent = both; anything else (NT8 has exactly two Output tabs) fails **closed** — `lines:[]`, not both tabs |
| `contains` | — | case-insensitive substring of the line text |

Each line is tagged with its tab — `"[1] hello"` — the same shape `GET /output/window` uses. A
cleared tab is an event, not silence: it arrives as `"[2] <<cleared: tab 2>>"`.

`subscribed` is `OutputHub`'s state. When the ring has never seen a line the response also carries
`"note":"buffer empty since bridge start — lines printed before this assembly loaded are only in
the window: GET /output/window"`. Lines printed before the last hot reload are the one case the
ring cannot cover; that note is the pointer, and there is deliberately no automatic fallback,
because the fallback needs `Ui()` and `Ui()` is what this endpoint exists to avoid.

```json
{"lines":["[1] hello","[2] SampleMACrossOver: flat"],"index":41231,"dropped":0,
 "subscribed":true,"source":"ring"}
```

## `GET /nt-log`

NinjaTrader's own log (`NinjaTrader.Cbi.Log.LogEvent`), the connection / order / execution /
strategy / system events the Control Center's Log tab shows. Ring capacity 5000. `Start_Events`
backfills once from `Cbi.Log.LogEntries` (at most 2000 entries, best effort — that collection is
mutated by NT's log thread), then subscribes; entries written in the gap between the two are lost,
which is deliberate: the other order would duplicate them, and a duplicate breaks a cursor reader
while a gap only shortens history.

| Query | Default | Meaning |
|---|---|---|
| `since` | `-1` | cursor, above |
| `n` | `200` | max entries off the ring |
| `level` | — | exact, case-insensitive: `Alert`, `Information`, `Warning`, `Error` |
| `name` | — | case-insensitive substring of `name` — **this is the field to match on** |
| `contains` | — | case-insensitive substring of `msg` |

Entry:

| Key | Type | Meaning |
|---|---|---|
| `t` | string \| null | `LogEventArgs.Time`, NT8-local `yyyy-MM-ddTHH:mm:ss`, no zone |
| `level` | string \| null | `Alert` \| `Information` \| `Warning` \| `Error` |
| `category` | string \| null | `Connection`, `Order`, `Execution`, `Strategy`, `NinjaScript`, … |
| `name` | string \| null | the resource key the caller passed, e.g. `CbiOrderRejected` |
| `resource` | string \| null | `ResourceType.Name`, the other half of the identifier |
| `msg` | string \| null | the **rendered** text |

**Match on `name`, not on `msg`.** NinjaTrader builds `msg` from (`ResourceType`, `Name`) through a
`ResourceManager`, so the English wording changes with the NT version and the UI language while the
name is what the caller passed and stays.

`Account` and `User` are deliberately **not** captured: unknown lazy work behind them, and they
carry identity that has no business on port 7891.

```json
{"entries":[{"t":"2026-09-18T10:14:59","level":"Error","category":"Order",
             "name":"CbiOrderRejected","resource":"Resource","msg":"..."}],
 "index":882,"dropped":0,"subscribed":true,"source":"ring"}
```

## MCP tools

| Tool | Endpoint |
|---|---|
| `nt_output(since=-1, n=200, tab=0, contains="")` | `GET /output` |
| `nt_log(since=-1, n=200, level="", name="", contains="")` | `GET /nt-log` |

**Note on naming:** this tool was once `nt_output_events` while `tools_core`'s window scrape held
the name `nt_output` (two `@mcp.tool` functions cannot share one name). The window scrape was
repointed to `nt_output_window`, freeing `nt_output` for the ring reader above — see `API.md`.

## Threading

- `Route_Events` and both handlers: no `Ui()`, no dispatcher, no file I/O, no NinjaTrader call.
  They read a `Ring<T>` under the ring's own lock and nothing else.
- `Ev_OnLog` runs on NinjaTrader's log thread: one capture, one `Add`, inside `catch {}`.
- The `OutputHub` handler runs on whatever thread called `Print()`, chart threads included. The
  events module registers **no** listener on it and never subscribes to `Output.OutputEvent`.
- `Stop_Events` is idempotent (the core can run stop hooks twice when a rebind retry races `Stop()`)
  and carries the `-=` for the one `+=` in `Start_Events`.
- `GET /compat` reports the subscription as `Cbi.Log.LogEvent`, with the seeded entry count in its
  `detail`.
