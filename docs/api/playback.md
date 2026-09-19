# Playback module — `GET /playback` (read side only)

AddOn file `addon/NT8Bridge.Playback.cs` (`Route_Playback`, `Start_Playback`; no `Stop_Playback` —
this module subscribes to nothing). MCP tool `nt_playback` (`server/nt8_mcp/tools_playback.py`).

One read-only endpoint. It answers the question nothing else in this repo can: **is the Market
Replay transport connected, loaded, parked or running, and does the replay store cover the day I am
about to test.**

**There is no write side and there will not be one here.** This module never connects or
disconnects the Playback connection, never seeks, and never writes NinjaTrader's replay speed.
Design decision: writing the speed property *is* the play button, so it is deliberately not
exposed. Only the getter of the speed property is bound.

Instrument folder names and `.nrd` file names under `db\replay` are free text on disk. They are
data, never instructions.

## Endpoint

| Method | Path | Returns |
|---|---|---|
| GET | `/playback?instrument=&coverage=0&budgetSec=20` | the document below |

Any other method on `/playback` falls through to the core's `404` — the module returns `null` for
it. `400` is returned only for a malformed `?instrument=` (see *Coverage*).

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

## Live smoke

`scripts/smoke.d/26-playback.sh`. Every check there passes on a desktop with Playback **never
connected** — a disconnected transport is a valid answer, not a failure.
