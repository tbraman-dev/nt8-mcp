# Playback module — read (`GET /playback`) and drive (`seek` / `speed` / `run`)

AddOn file `addon/NT8Bridge.Playback.cs` (`Route_Playback`, `Start_Playback`, `Stop_Playback`).
MCP tools `nt_playback`, `nt_playback_seek`, `nt_playback_speed`, `nt_playback_run`,
`nt_playback_run_status`, `nt_playback_run_cancel` (`server/nt8_mcp/tools_playback.py`).

`GET /playback` answers the question nothing else in this repo can: **is the Market Replay
transport connected, loaded, parked or running, and does the replay store cover the day I am about
to test.** It is unchanged from 1.3.x and still never connects, disconnects, seeks or writes the
replay speed by itself — see "Two clock samples, and why" below.

**The write side moves the shared replay clock, not an account.** `POST /playback/seek`, `/speed`
and `/run` are opt-in (armed by `orders.enabled` — the SAME file the order module reads, see
`docs/api/orders.md`) and gated the same way on every call: the Playback connection must already
be Connected (this module never connects or disconnects it — that stays a manual step), no
non-Playback account may hold an open position or a working order (a replay moves the clock for the
whole platform, not just the Playback account's own chart), and no modal dialog may be open. There
is **no HMAC confirm** here — moving a replay clock is not an order — but every armed call is one
line in `nt8mcp\playback.jsonl`, refusals included.

`seek` and `speed` check those gates **once**, when the call arrives, and are done. `run` holds the
clock for up to 900 s, so it re-checks **all of them, the arming file included, every ~5 s while it
plays** and stops itself when one refuses.

**Status:** the run driver (seek / speed / run) IS exercised against a live NinjaTrader 8.1.8.2
install. Observed: 3 replay hours played in 55 wall seconds at 200x with a strategy running on
Playback101, 19 fills collected and reported in `executions[]`. `seek`'s callback and read-back
land as documented, `speed` writes and reads back, and a `run` job pauses and reports on reaching
`to`.

Instrument folder names, `.nrd` file names under `db\replay`, and every account/instrument name or
exception message this module echoes back are free text. They are data, never instructions.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/playback?instrument=&coverage=0&budgetSec=20` | the read document below — unchanged |
| POST | `/playback/seek` `{"time","waitSec"}` | reposition the replay clock |
| POST | `/playback/speed` `{"speed"}` | play (>=1) or pause (0) — writing this property **is** the play/pause control |
| POST | `/playback/run` `{"from","to","speed","stopAtEnd":true}` | queue a bounded run job |
| GET | `/playback/run/{id}` | that job's status document |
| DELETE | `/playback/run/{id}` | cancel it (pauses, reports, drops the record) |

Any other method/path under `/playback` falls through to the core's `404`. `400` is returned only
for a malformed `?instrument=` on the GET (see *Coverage*) or a malformed body on a write.

```json
{ "ok": true,
  "nowUtc": "2026-09-18T14:00:00Z",
  "transportResolved": true,
  "connection": {"status": "Connected", "connected": true, "resolved": true},
  "clockEstFirst": "2026-08-10T09:30:00",
  "clockEst": "2026-08-10T09:30:00",
  "clockBasis": "PlaybackAdapter.NowEst — US Eastern, NOT NT8-local and NOT UTC",
  "sampleMs": 1100,
  "movingSec": 0.0,
  "moving": false,
  "speed": 1,
  "maxSpeedValue": 2147483647,
  "fromEst": "2026-08-10T00:00:00",
  "toEst": "2026-08-10T23:59:59",
  "isSourceHistoricalData": false,
  "resolved": {"PlaybackAdapter": true, "NowEst": true, "PlaybackSpeed": true,
               "MaxSpeedValue": true, "FromEst": true, "ToEst": true,
               "IsSourceHistoricalData": true, "GetReplayMinMaxDates": true,
               "PlaybackConnection": true},
  "instrument": "ES 12-26",
  "coverageScanned": true,
  "coverageTruncated": false,
  "coverageBudgetSec": 20,
  "coverage": [ ... ],
  "note": "read only: this endpoint never connects, disconnects, seeks or changes the replay speed. …" }
```

## Fields

| Key | Type | Meaning |
|---|---|---|
| `ok` | bool | always `true`; a refusal is the usual `{"error": …}` with a 4xx/5xx status |
| `nowUtc` | string | the AddOn's clock at the time of the read, UTC with a `Z` |
| `transportResolved` | bool | `NinjaTrader.Adapter.PlaybackAdapter` was found by reflection. `false` ⇒ every field below is `null` and that means **unknown**, not "parked" |
| `connection.status` | string\|null | `Connection.PlaybackConnection.Status`. `null` = NinjaTrader holds no Playback connection object at all (never connected this session) — *not* the same claim as `"Disconnected"`, which NinjaTrader itself reported |
| `connection.connected` | bool | `Status == Connected` |
| `connection.resolved` | bool | the `PlaybackConnection` property resolved |
| `clockEstFirst`, `clockEst` | string\|null | the replay clock, sampled twice `sampleMs` apart. **US Eastern** (`NowEst`), carrying no zone — neither NT8-local nor UTC. `null` when Playback has never connected this session (`NowEst`'s un-assigned default) — reported as unknown, never as a parked `moving:false` reading |
| `sampleMs` | int | 1100, the gap between the samples. `NowEst` ticks in whole seconds (observed on NinjaTrader 8.1.8.2), so a shorter gap read a 1x replay as parked about one call of two |
| `movingSec` | number\|null | `clockEst − clockEstFirst` in seconds. `null` when the clock could not be read |
| `moving` | bool\|null | `movingSec > 0.05` (a jitter guard, not a speed threshold — NT8's Playback speed floors at 1x, which alone moves the clock one whole second over one `sampleMs` sample). `null` when the clock could not be read |
| `speed`, `maxSpeedValue` | int\|null | replay speed multiplier and its ceiling, **read only** |
| `fromEst`, `toEst` | string\|null | the loaded replay range (US Eastern); `null` for `DateTime.MinValue` |
| `isSourceHistoricalData` | bool\|null | `true` = the Historical store (`db\tick`, `.ncd`), `false` = Market Replay (`db\replay`, `.nrd`) |
| `resolved` | object | reflection-resolution table, member → bool. This is what separates "NT8 moved this member" from "the value really is null" |
| `instrument` | string\|null | the `?instrument=` that was asked for, echoed |
| `coverageScanned` | bool | **whether the store was looked at**. `false` + `coverage: []` = *not looked*, never *nothing there* |
| `coverageTruncated` | bool | the scan hit `coverageBudgetSec` and stopped early; `skipped` per instrument says how many files were not read |
| `coverageBudgetSec` | int | the wall-clock budget used for the scan |
| `coverage` | array | one row per instrument folder, below; `[]` when `coverageScanned` is false |

### `coverage[]`

```json
{ "instrument": "ES 12-26", "files": 2, "readable": 2, "unreadable": 0, "skipped": 0,
  "from": "2026-08-10T00:00:00", "to": "2026-08-10T23:59:59",
  "days": [{"file": "20260810", "readable": true,
            "from": "2026-08-10T00:00:00", "to": "2026-08-10T23:59:59", "bytes": 41234567}] }
```

`from`/`to` on the row span every **readable** file and are `null` when `readable` is 0. A file the
reader could not parse is counted in `unreadable` and still gets a `days[]` row with
`readable: false` — a corrupt `.nrd` and a missing one are different problems, so neither is
silently dropped. `bytes` is `-1` when the file length could not be read (never `0`, which would be
a claim about its size).

## Live observations (NT8 8.1.8.2)

- All nine `resolved` entries `true`, `transportResolved:true`; `POST /playback` is the core's
  `404 no route for POST /playback`.
- **A parked transport reads `speed: 0`, not `>= 1`.** A connection with a range typed in but never
  played reads `clockEst` = `fromEst` (the start of the range), `movingSec` 0, `moving:false`,
  `speed:0`. So `speed:0` alone does not mean "nothing loaded"; the 2099 sentinel does.
  Not observed: the never-connected `connection.status`, the connected-but-empty 2099
  sentinel, and `movingSec` while playing at 1x.
- **Time basis.** `GetReplayMinMaxDates` fills both out-parameters. `20260913.nrd` reads
  `10:18:16 .. 23:59:59`; the same file decoded offline (`nt_nrd_export`, UTC) runs
  `14:18:16Z .. 03:59:59Z` next day, and every full-day file runs `04:00Z .. 03:59:59Z`. So the reader
  answers in UTC-4 = US Eastern (daylight time) and the `.nrd` day files are cut at Eastern midnight —
  **not UTC**. When the local NT8 time zone is also Eastern, Eastern-vs-NT8-local cannot be told
  apart from observation alone; the wording stays "Eastern" on the strength of the member names
  (`NowEst`, `FromEst`, `ToEst`).
- **Scan cost.** `?instrument=ES 12-26&budgetSec=5`: several `.nrd` files (hundreds of MB) in
  single-digit milliseconds on top of the 400 ms sample; `coverageTruncated` never fired. A
  continuous-contract name with no folder present: `coverageScanned:true`,
  `coverage:[]`. Upstream's reported 17 s for one instrument does not reproduce here; the 20 s
  default has room.
- **The slider lesson, reproduced.** The range starts 09-10 but the first recorded day is 09-11: the
  clock sat on a day with no `.nrd`. `nt_playback` keeps `ready:true` (a seek can be trusted) and
  appends `no readable .nrd covers that clock time` to `why`.
- **A connected Playback changes what a backtest loads** — see `docs/api/optimize.md`, "Playback".

## Two clock samples, and why

A **single** clock reading cannot tell a parked transport from a running one. That distinction
blocked a replay-equivalence gate upstream for a day: the same seek landed on one box's parked clock
and silently no-opped on the other's moving one. `RealtimeTickCount` cannot separate them either —
it advances ~1 per wall-second in both states. So the handler samples `NowEst`, sleeps 1100 ms and
samples again. **The endpoint therefore always takes at least 1100 ms**; it holds no lock and makes
no NinjaTrader call while it sleeps.

## Coverage is opt-in, and bounded

Coverage is read with NinjaTrader's **own** `.nrd` reader (`GetReplayMinMaxDates`). It is **not**
the Playback slider: the slider's bounds are the connection range a human typed, not the indexed
data, and reading it as proof that data was loaded cost hours upstream.

| Query | Scans |
|---|---|
| neither | nothing. `coverageScanned: false` |
| `?instrument=ES 12-26` | that one folder under `db\replay` (upstream: ~17 s) |
| `?coverage=1` | **every** instrument folder — slow (upstream: 3-7 min over 35 instruments) |

`?instrument=` names **one folder**, not a path: a value containing `/`, `\`, `..` or any character
invalid in a file name is refused with `400`. Scan the continuous name (`ES ##-##`) as well as the
front month (`ES 12-26`) — Market Replay stores under both.

`?budgetSec=` (default 20, **clamped to 120**) caps the scan's wall clock. On expiry it stops, sets
`coverageTruncated: true` and reports the unread files as `skipped`. The 120 s ceiling exists so a
caller cannot pin a ThreadPool thread scanning `db\replay` for hours inside the live NT8 process —
without it, a wide scan outlives any HTTP client and produces an answer nobody is still listening
for, and keeps running anyway.

## MCP tool `nt_playback(instrument="", coverage=False, budget_s=20)`

Passes the endpoint's document through unchanged and adds the **verdict**, which lives in exactly
one place — the AddOn deliberately reports facts only:

| Key | Meaning |
|---|---|
| `state` | `unresolved` \| `disconnected` \| `empty` \| `running` \| `unknown` \| `parked` |
| `ready` | a seek from this transport can be trusted |
| `why` | one sentence, printable |

**`empty` is the rule worth knowing.** A connected Playback with nothing loaded reads
`clockEst` **2099-12-01** with `speed` 0 — which is *stationary*, and therefore passes a naive
moving/not-moving test as READY. Upstream shipped that false green and a live run found it. Any
`clockEst` whose year is 2090 or later is NT's "no replay data loaded" sentinel, never a time.

`ready` is also `false` for a `parked` transport whose store was not scanned, for one whose scan
hit `coverageBudgetSec` and was cut short (`coverageTruncated: true` — a truncated scan is not
proof the store is empty either), and for one whose store holds no readable `.nrd`.

The tool widens its HTTP deadline to `budget_s + 10` for a coverage scan only, and puts it back.

## The write side: `seek`, `speed`, `run`

Built as a Playback RUN DRIVER, not only seek and speed. Studied first: `cli-nt-bridge`'s
`playbackrun` (`.ref/cli-nt-bridge/addon/
NT8BridgeServerPlayback.cs`, `nt8bridge/playback_run.py`) — its mechanism is UI-tree automation of
the Playback window (fragile, build-specific) and is **not** what this module does. What carried
over is the *findings*, bisected there on NinjaTrader 8.1.8.2 and unchanged by our own reflection
here:

- Writing `PlaybackAdapter.PlaybackSpeed` **is** the play/pause control — no button, no window, no
  dispatcher. Speed `0` parks the transport; any positive value starts it moving. This was already
  NOTES.md's stated reason the old read-only module never bound the setter; the write side exists
  now specifically to use it.
- `PlaybackAdapter.FromEst`/`ToEst` only take effect **after** the connection is Connected — before
  that the adapter discards them. Gate 2 (Connected) makes this moot: this module never writes them
  before that gate has already passed.
- `PlaybackAdapter.Reset(DateTime targetTimeEst, Action<bool> callback)` (real signature, `.ref\
  nt8src\core\NinjaTrader.Adapter\PlaybackAdapter.cs`) repositions the clock and is **asynchronous**;
  its callback is used directly (a `ManualResetEventSlim`, bounded), and the clock is also read back
  independently afterward, because a callback with no matching clock move would be exactly the kind
  of lie the read-back-every-write discipline exists to catch.
- **Not yet confirmed on a live platform**: NinjaTrader does not document the `FromEst`/`ToEst`/
  `PlaybackSpeed` property **setters** (empty bodies in source), so their effect cannot be read from
  source. What backs the claim
  above that they work is cli-nt-bridge's own bisected, live-tested finding, plus this module's own
  read-back on every write (`Playback_WriteDate`, `Playback_WriteSpeed`) — never a bare "the call
  did not throw". Nothing here needed the Playback **window** or any UI control: every write is a
  reflective call on the `PlaybackAdapter` statics, exactly like the existing read side, so there is
  no ceiling here to report in the "not done" sense the task anticipated.

### `POST /playback/seek {"time": "2026-08-10T09:30:00", "waitSec": 30}`

`time` is required, US Eastern, no zone (same basis as `clockEst`). `waitSec` (default 30, clamped
1..120) bounds how long the call waits for `Reset`'s own completion callback.

```json
{ "ok": true, "requestedTime": "2026-08-10T09:30:00",
  "before": {"clockEst": "2026-08-10T09:00:00", "speed": 0},
  "clockEst": "2026-08-10T09:30:00", "callbackOk": true, "timedOut": false, "waitedSec": 30,
  "note": "…" }
```

`ok` mirrors `callbackOk` — NinjaTrader's own success signal. `clockEst` is read back
independently; a large jump can still be settling when `timedOut:true`, so re-read `GET /playback`
if precision matters.

### `POST /playback/speed {"speed": 0}`

Whole number, `>=0`; refused above this build's `maxSpeedValue`. `0` pauses.

```json
{ "ok": true, "requestedSpeed": 0,
  "before": {"clockEst": "2026-08-10T09:30:00", "speed": 4},
  "speed": 0, "problem": null, "note": "…" }
```

`ok` is `false` when the read-back does not match what was written (`problem` says what); it is
never inferred from the write call alone.

**The gates are checked once, when the call arrives.** A positive speed then leaves the clock
running with nothing watching it and no bound: this verb is the manual control, and whoever sends it
owns the pause. `POST /playback/run` is the bounded form — it re-checks the gates while it plays and
always pauses at the end.

### `POST /playback/run {"from": "...", "to": "2026-08-10T16:00:00", "speed": 8, "stopAtEnd": true}`

A **job**: `to` is required (both the new `ToEst` and the watch target); `from` is optional (omit
it to keep playing from wherever the clock already sits — a resume). `speed` is required, a whole
number `>=1` (use `/playback/speed` with `0` to just pause). `stopAtEnd` must be `true` if sent at
all — **`false` is refused with 400**: there is no free-running mode in this module (see "Known
ceiling" below). Only one run may be queued/running at a time — a second `POST` while one is active
is refused `409 runInProgress`, not queued behind it, because both would be driving the same shared
clock. **The check and the enqueue happen under one lock**, the way `Ord_ReserveSubmit` reserves the
submit caps: the core dispatches every request on its own thread-pool thread, so a check in one lock
and an enqueue in another would let two callers (a retry after a slow answer, two clients) both see
"nothing in progress" and both get a `202`, with the second one's `before` snapshot describing a
clock the first had already moved.

`202 {"id":"pr1","state":"queued"}`. The worker (`NT8Bridge-pbrun`, one thread) then:

1. re-checks the gate chain — the arming file included (the world can change between the POST and
   the worker picking the job up: a run dequeued after `orders.enabled` was deleted never starts);
2. records `before` (the clock and speed exactly as they were, untouched);
3. sets `ToEst` (always) and `FromEst` (only if `from` was given), each read back;
4. if `from` was given, calls `Reset` to position the clock there (same mechanism as `/seek`);
5. writes `PlaybackAdapter.PlaybackSpeed = speed` — this starts the replay;
6. polls the clock every ~1 s until it reaches `to`, or a **900 s wall-clock cap** (same ceiling as
   the backtest job's 15-minute cap) — whichever comes first — **re-running the whole gate chain
   every ~5 s while it plays**. A run holds the platform's one replay clock for up to 15 minutes, so
   a check taken once at the start cannot see `orders.enabled` being deleted, another account taking
   on a position or a working order, or a modal opening, ten seconds in. Any of those breaks the
   watch loop with `error` set to `stopped mid-run (<gate>): <the gate's own sentence>`, and step 7
   pauses the clock where it stood;
7. **always** pauses (`speed = 0`) and reads the clock one more time as `clockEndEst` — this is the
   entire "restore": pause and report, never reconnect, never touch any account;
8. reads executions on every **Playback-provider** account for the window the run covered (see
   below), the same way `GET /executions` does.

`GET /playback/run/{id}`:

```json
{ "id": "pr1", "state": "done",
  "request": {"from": "2026-08-10T09:30:00", "to": "2026-08-10T16:00:00", "speed": 8, "stopAtEnd": true},
  "queuedAt": "...", "startedAt": "...", "finishedAt": "...",
  "before": {"clockEst": "2026-08-10T09:00:00", "speed": 0},
  "clockStartEst": "2026-08-10T09:30:00", "clockEndEst": "2026-08-10T16:00:00",
  "reachedTo": true, "wallSeconds": 12.3,
  "executions": [{"account": "Playback101", "windowFromEst": "...", "windowToEst": "...",
                  "source": "db", "count": 3, "executions": [ ... ], "warnings": []}],
  "warnings": [], "error": null }
```

`state` is `queued | running | done | error | cancelled`. `error` is set (and `state` becomes
`error`) when the wall-clock cap fires before `to`, when a gate refuses the run at the worker's
start-up re-check, or when one of the repeated mid-run checks refuses it (`stopped mid-run (…)`).
`reachedTo` is `false` in all three cases: a run that was cut short never reports `done`. `warnings` collects non-fatal notes (a range/seek write that did not read back as
written, `Reset` timing out, executions that could not be read for one account) without failing the
whole run.

`DELETE /playback/run/{id}` sets a cancel flag the worker notices on its next poll tick (at most
~1 s), which finishes the job the same way a normal end does (pause, report) with `state:
"cancelled"` — then the record is removed, same convention as `DELETE /backtest/{id}`: a later GET
404s.

### Executions: read the way `GET /executions` reads them

`Playback_ExecutionsJson` calls straight into `NT8Bridge.Account.cs`'s own `Acct_Fetch`/
`Acct_ExecutionOne` (the trade-DB-plus-in-memory union, deduped by `ExecutionId`) — that file is
never edited by this module, only reused, so there is exactly one copy of that logic.

**Known ceiling, disclosed rather than silently approximated:** the query window is the run's
`clockStartEst`..`clockEndEst` **padded ±3 hours**, because `Execution.Time` is stamped in the
**exchange's own local time** (e.g. CME Central for ES) while the replay clock (`NowEst`) is fixed
to US Eastern, and this module has no per-exchange timezone table. The pad is wide enough to absorb
any US exchange's offset from Eastern; it can also pull in a fill from just outside the run for an
account that traded right before/after it — each row still carries its own `time`, so a caller
needing an exact boundary filters on that. Upgrade path: resolve each instrument's real exchange
timezone if a boundary miss is ever observed live, and narrow the pad.

## Known ceiling: no free-running mode

`stopAtEnd:false` is refused outright rather than silently ignored. A free-running mode (queue the
range/seek/play steps, return immediately, and keep the replay going unattended) needs its own
design for who pauses it and on what trigger if nobody ever polls `GET /playback/run/{id}` again —
this module needs a bounded run driver, not an unattended one, so that mode was not built.
`seek`, `speed`, and `run` over an already-loaded range are shipped; nothing here was blocked by
needing a UI control PlaybackAdapter could not reach.

## Live smoke

`scripts/smoke.d/26-playback.sh`. Every check there passes on a desktop with Playback **never
connected** — a disconnected transport is a valid answer, not a failure. The write-side checks in
that script are read-only preconditions (armed flag, `GET /playback/run/{id}` 404 for an unknown
id) — placing an actual order-free but clock-moving call needs a live Playback session with a
loaded range and is exercised by hand (see the by-hand verification steps below), not by the smoke
script.

Two of those by-hand checks are worth naming, because nothing offline can reach them:

- **Disarm mid-run.** Start a run over a long window (`speed` 1, a `to` an hour ahead), and delete
  `bin\Custom\AddOns\orders.enabled` about ten seconds in. Within ~5 s the run must finish with
  `state:"error"`, `reachedTo:false` and an `error` beginning `stopped mid-run (notArmed)`, and
  `GET /playback` must show `speed: 0` — the clock is paused, not left running. Do the same again
  with a working order placed by hand on `Sim101` instead of the delete: the outcome is
  `stopped mid-run (exposure)` and names that account.
- **Two runs at once.** Fire two `POST /playback/run` calls back to back (two shells, no wait).
  Exactly one gets a `202` with an id; the other gets `409 runInProgress` naming that id.
  `GET /playback/run/<the other id>` must 404 — a refused run is never queued.
