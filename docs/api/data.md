# Data store — `/data/coverage`, `/data/download` (module `data`, `addon/NT8Bridge.Data.cs`)

Covers `/data/coverage`, `nt_nrd_export` (pure Python) and `/data/download`. Same conventions as
`API.md`: JSON, UTF-8, `{"error":"…"}` on 4xx/5xx,
times local NT8 `yyyy-MM-ddTHH:mm:ss`. Dates on this module's wire are always `YYYYMMDD`.

| Method | Path | Returns |
|---|---|---|
| GET | `/data/coverage?instrument=ES%2012-26&kind=&from=&to=` | what is on disk, per store — below |
| GET | `/data/probe?instrument=ES%2012-26&kind=minute\|tick\|day` | `202 {"id":"p1","state":"queued"}` — below |
| GET | `/data/probe/{id}` | the probe result once it finishes — below |
| POST | `/data/download` | `202 {"id":"d1","state":"queued","days":N,…}` — below |
| GET | `/data/download/{id}` | the job document — below |
| DELETE | `/data/download/{id}` | `{"ok":true,"state":"cancelled"}` |

Everything in this module does file I/O on an `HttpListener` thread pool thread. No
dispatcher hop, no `Draw.*`, no order/position/account touch anywhere.

---

## `GET /data/coverage`

Pre-flight only: **presence of files, not completeness.** It answers "is there anything for
that day"; the content is decided when the series loads, and a run whose data is missing
still fails loudly there.

Query: `instrument` (required, e.g. `ES 12-26`), `kind` (`tick|minute|day|replay`, default:
all four), `from` / `to` (`YYYYMMDD`, optional, they narrow the *analysis*, not the scan).

```json
{ "instrument": "ES 12-26",
  "instrumentResolved": "ES 12-26",
  "resolved": ["ES 12-26", "ES ##-##"],
  "kind": null, "analysisStore": "tick",
  "from": "20260901", "to": "20260918",
  "stores": {
    "tick": {
      "scanned": true,
      "dir": "C:\\Users\\you\\Documents\\NinjaTrader 8\\db\\tick\\ES 12-26",
      "dirSource": "convention",
      "granularity": "hour",
      "files": 475, "bytes": 51219712,
      "firstDay": "20260910", "lastDay": "20260917",
      "days": { "20260910": {"files": 71, "bytes": 8123904, "last": 24, "bid": 24, "ask": 23} } },
    "minute": { "scanned": true, "granularity": "day",  "…": "…" },
    "day":    { "scanned": true, "granularity": "year", "days": {"2026": {"files":3, "…":"…"}} },
    "replay": { "scanned": false, "dir": "…\\db\\replay\\ES 12-26", "dirSource": "convention",
                "granularity": "day", "files": null, "bytes": null,
                "firstDay": null, "lastDay": null, "days": null } },
  "alsoScanned": { "ES ##-##": { "tick": {"scanned": false, "…": "…"},
                                 "replay": {"scanned": true, "files": 42, "days": null} } },
  "cache": { "note": "what a backtest of that series can use without a provider (NT8's own bars cache, name-based scan)",
             "scanned": true, "capReached": false,
             "series": { "ES 12-25/Minute_1_1_Last_Close_Tick_BidAsk_Minute_1.Last":
                         {"contract": "ES 12-25", "firstDay": "20250915", "lastDay": "20251212"} } },
  "missingWeekdays": ["20260916"],
  "daysLackingBidAsk": ["20260915"],
  "thinDays": [],
  "note": "pre-flight only: presence of files, not completeness" }
```

Fields that carry a decision, not just a number:

- **`scanned:false` never means "empty".** A folder that does not exist reports
  `scanned:false` with `days:null` — never `days:{}`, which reads exactly like "NinjaTrader
  has nothing for this instrument". Same rule for every count in that row (`null`, not `0`).
- **`granularity`** is how the store names its files, verified live on 8.1.8.2:
  `tick` = `YYYYMMDDHHmm.<Last|Bid|Ask>.ncd` (hourly, day = first 8 characters),
  `minute` = `YYYYMMDD.<Type>.ncd`, `replay` = `YYYYMMDD.nrd`, and
  **`day` = `YYYY.<Type>.ncd` — one file per YEAR, not per day.** So the `day` store's
  `days` map is keyed by year and is never used for the day-level analysis below.
- **`dirSource`** is `"BarsBytes.GetDataDir"` when NT8's own resolver answered with a folder
  that exists and is named for this instrument, else `"convention"`
  (`db\<kind>\<Instrument.FullName>`). A caller is never told a guess is authoritative.
  Observed on NinjaTrader 8.1.8.2: the resolver returns the per-instrument folder
  (`…\db\tick\ES 12-26`), so `tick`/`minute`/`day` read `"BarsBytes.GetDataDir"`. `replay`
  is always `"convention"` — `GetDataDir` has no replay period type.
- **`alsoScanned`** holds the continuous contract name (`ES 12-26` → `ES ##-##`) as a summary
  with no `days` map. It exists because of the trap in cli-nt-bridge's own changelog: a front
  month can hold every NCD file and still have **no `.nrd` at all**, because market-replay
  recordings live under the continuous name. Read this before concluding `replay` is empty.
- **`analysisStore`** is the store the three lists below were computed from: the first
  scanned, non-empty, non-year-granular store in `tick, minute, day, replay` order (or the
  one `kind` names). `null` when nothing was scannable.
- **`missingWeekdays`** — Mon–Fri days inside `[firstDay..lastDay] ∩ [from..to]` with no file.
  Saturday has no session; **Sunday is not flagged either** — a Sunday-evening open is filed
  under the Monday session date.
- **`daysLackingBidAsk`** — days in the window whose `bid` or `ask` file count is 0.
  `null` for the `replay` store, which has no per-type concept.
- **`thinDays`** — days holding less than a fifth of the median day's bytes. A blunt
  heuristic on purpose and a hint, never a verdict; `null` when fewer than 5 days are in
  the window to take a median from.
- **`cache`** answers a different question than `stores`: not "what is in `db\<kind>`" but
  "what can a **backtest** use right now without touching the provider at all". A backtest's
  own bars loader (`NT8Bridge.Backtest.cs` `RunA2`) does not read `db\minute` — it calls
  `Bars.GetBars` and NinjaTrader persists the result into its own bars cache instead:
  `db\cache\<TradingHours>.<TimeZone>\<MINUTE|TICK>\<contract>\<seriesKey>.<from>.<to>.<Type>.ncd`.
  `cache.series` is keyed `"<contract>/<seriesKey>"`, one entry per series that already exists
  there, with `firstDay`/`lastDay` taken straight from the filename (never parsed). It is scanned
  across **every contract of the instrument's chain** (root-symbol match, e.g. `NQ 12-26` also
  finds `NQ 09-26`, `NQ 06-26`, …) because that is exactly what `MergePolicy` walks for a backtest.
  `day` and `replay` have no cache folder, so `cache.series` is empty for those kinds.
  `capReached:true` means the bounded scan (`Data_CacheScanCap` files) stopped before finishing —
  a partial answer, never a false "nothing more".

Errors: `400` for a missing `instrument`, a bad `kind`, a non-`YYYYMMDD` date, or a range
`RangeProblem` rejects (inverted, or a placeholder year).

---

## `GET /data/probe` — how far back the connected provider serves

`nt_data_coverage` answers what NinjaTrader's own store already has on disk; it cannot say what
the CONNECTED provider would serve if asked. `/data/probe` answers that with a bounded binary
search of small `Bars.GetBars` requests — never an unbounded search, never a guess of
"3 months / 1 year / 2 years" by trial.

**Queued, not synchronous**: the search can legitimately run up to `Data_ProbeCapWall`
(90s) of wall time, worse with a slow or half-dead provider (each in-flight request can add up
to `Data_ProbeRequestTimeout`, 20s, on top). Running that inline on the `HttpListener` thread
pool — the same pool that serves every other endpoint — could pin one of its workers for up to
~110s. `GET /data/probe` therefore validates on the request thread and answers **202
`{"id":"p1","state":"queued"}`** at once; the actual search runs on its own one-shot background
thread. Poll `GET /data/probe/{id}`, same shape as `/data/download`.

Query: `instrument` (required), `kind` (`minute|tick|day`, required — `replay` is not probed).

```json
GET /data/probe?instrument=ES%2012-26&kind=minute
→ 202 { "id": "p1", "state": "queued" }

GET /data/probe/p1
→ { "id": "p1", "state": "done",
    "instrument": "ES 12-26", "instrumentResolved": "ES 12-26", "kind": "minute",
    "requestsUsed": 9, "capRequests": 14, "capReached": false,
    "earliestDate": "20240115", "depthDays": 613,
    "note": "binary search over Bars.GetBars; NinjaTrader resolves the connection itself" }
```

`state`: `queued` | `running` | `done` | `error`. While `queued`/`running` the body is just
`{"id","state"}` — poll again. `404 {"error":"no data probe '<id>'"}` for an unknown id.

- **`capReached: true`** means the search found data all the way back to its floor
  (`Data_ProbeCapDays` = 3650 days) without ever finding a day with none: `earliestDate` and
  `depthDays` are `null` and `note` reads `"at least 3650 days back (search cap reached) — the
  connected provider may serve more"`. This is a floor the search proved, never the provider's
  real limit.
- Each probed day uses `MergePolicy.DoNotMerge`, the same policy a backtest's own bars loader
  uses (`NT8Bridge.Backtest.cs` `RunA2`) — this matters for `/data/download`'s fallback,
  see "Which connection serves it" below.
- **No answer within the per-request timeout** (a stalled provider) stops the whole probe and
  reports `earliestDate: null`, `depthDays: null` with a note naming the date that did not
  answer — never a guessed depth.
- The boundary-is-monotonic assumption (once a day has data, every later day does too) is a
  heuristic: a provider with a genuine mid-history gap can report a boundary later than its true
  earliest day. `requestsUsed`/`capRequests` are always echoed so a caller can tell a clean
  answer from one that hit its cap.
- Same guards as `/data/download` below (`Data_Exposure`, `AnyNonSimConnected`) and no arming
  file — a probe moves no money. It does the small amount of local caching any `Bars.GetBars`
  call does as a side effect; it is not a bulk download.
- Errors: `400` for a missing `instrument`, a `kind` other than `minute|tick|day`, or an unknown
  instrument — all from `GET /data/probe` itself, before a job is even queued. `409` for the same
  two guards `/data/download` uses, also from the queueing call. `GET /data/probe/{id}` itself
  only ever 404s (unknown id); a failure inside the search lands in the job as `state:"error"`.

---

## `POST /data/download`

**The one endpoint in batch 1 that writes NinjaTrader's own data store** (`db\replay`,
`db\tick`, `db\minute`, `db\day`) and spends the data provider's bandwidth. It touches no
order, position or account. (Honest note: the backtest loader already passes
`LookupPolicies.Provider | Repository` to `Bars.GetBars`, so a backtest *already* quietly
pulls missing history. This makes that explicit rather than introducing it.)

```json
{"instrument":"ES 12-26","from":"20260901","to":"20260910",
 "kinds":["replay"],                      // or any subset of ["tick","minute","day"]
 "types":["Last","Bid","Ask"],            // tick/minute/day only; ignored for replay
 "overwrite":false,"big":false}
→ 202 {"id":"d1","state":"queued","days":10,"anyLive":false,"anyNonSim":true}
```

### No arming file

A download moves no money, so there is no flag to create or keep fresh — `POST /data/download`
and `GET /data/probe` are reachable as soon as the guards below allow it. (An earlier version of
this module gated both behind `data.download.enabled`; that file is gone, and nothing here still
asks anyone to create it.)

### Which connection serves it (tick/minute/day)

Hardcoding `Connection.ClientConnection` (NinjaTrader's
own hosted data service) into `DownloadFromProvider` fails in ~4 ms on any machine whose real
market data comes from a broker adapter with no entitlement there — while a backtest pulls the
exact same bars from that adapter without complaint. There is no public NinjaTrader API that
names which connection serves an instrument's historical data (confirmed against the decompile:
`Bars.GetBars`/`BarsRequest` take no connection argument at all — NinjaTrader resolves it
internally), so this module tries connections itself, in order, per day:

1. Every **connected** connection whose provider is not Simulator/Playback (a broker adapter),
   in the order `Connection.Connections` lists them.
2. Then connected Simulator/Playback connections.
3. **`Connection.ClientConnection` last** — found unentitled when the real data comes from a
   broker adapter.

Each candidate is tried with `DownloadFromProvider` on a fresh `Bars` collection; a day's entry
in `downloaded` names which one worked, e.g. `"20260901/tick (Bid) via connection 1 (Provider31,
My Data Feed)"` — `connection N` plus the provider enum text are what a repo file may ever show;
the connection's own **name** (here, "My Data Feed") is a live value from `/health.connections` on
the user's own machine, never written into source or docs.

**On a broker-adapter data connection (`anyNonSim:true`), `DownloadFromProvider` can fail for
every candidate, every day** — the callback returns `false`
("download reported failure") even through the one connected, non-Simulator adapter. It is
demoted to a first attempt only, never trusted alone.

If every candidate answers "failure", the module falls back to asking for the bars **the same way
the headless backtest loader does** (`NT8Bridge.Backtest.cs` `RunA2`, `Data.Bars.GetBars(...,
LookupPolicies.Provider | Repository, MergePolicy.DoNotMerge, ...)`, no connection argument —
NinjaTrader picks it). **This is the fix**: the fallback originally passed `MergePolicy.UseDefault`,
which answered with `ErrorCode.NoError` and zero bars, so it looked like a silent no-op even
though the provider had the data — a backtest asking for the identical range with
`MergePolicy.DoNotMerge` returned bars and ran trades. Matching the backtest's own merge policy
made the fallback work: a 3-day range absent from `db\minute` (`NQ 12-26`,
2026-08-03..05) came back `downloaded` via `"… via fallback GetBars (connection chosen by
NinjaTrader)"`, landed as real `.ncd` files under `db\minute\NQ 12-26`, and a `SampleMACrossOver`
backtest over that same range then ran 194 trades against it. Market Replay (`kinds:["replay"]`)
is unaffected by any of this — it stays on `Connection.HistoricalDataClient`/`ClientConnection`
per the existing `Data_Requester` chain, which is where NinjaTrader serves replay recordings.

### The guards

A download routes no order, so the order-routing predicate `AnyLiveConnected()` does **not**
refuse it: an earlier version of this guard did, which made the endpoint unreachable on a machine
whose only real data provider is a broker demo connection (it can route orders, so it counts as
live). The refusal is **exposure**.

| Guard | Role here | Effect |
|---|---|---|
| `Data_Exposure()` | **the refusal** — an open position or a working order on any account but the Backtest one, paper included | `409`, body carries `"exposure": true` and names the account |
| `AnyNonSimConnected()` | **the precondition** — any real data provider is connected | `409` while **false** |

Both are re-checked before every date of a running job; exposure mid-run ends the job with
`state:"refused"`. `anyLive` / `anyNonSim` are still echoed in the `202` and in every
`GET /data/download/{id}`, for information only.

### Other guards

- Range wider than **10 days** → `400` unless `{"big":true}` (a heavy replay day is ~500 MB
  and 300–460 s). `big` **raises** the cap, it does not remove it: more than **400 days**
  (about a season) is refused even with `{"big":true}` — split the request.
- `RangeProblem` on `from`/`to`.
- Unknown instrument, bad `kinds`/`types`, a key present with the wrong JSON type → `400`.
- **Its own worker thread (`NT8Bridge-dl`) and its own job map.** It never touches the
  backtest worker, so a 460 s replay day cannot starve a backtest.
- Every guard is **re-checked before each date**, not once at POST: a position can open in the
  minutes a multi-day job runs. Exposure appearing mid-run stops the job at `state:"refused"`.

### Date-loop rules

- **Saturday is skipped** (no session) and is not reported.
- **The current and any future day, computed in `America/New_York`, are never downloaded** —
  replay data is partial until the session closes — and land in `skippedCurrent`, never in
  `downloaded`. (Falls back to the machine's local date if the ET zone cannot be resolved.)
- A date already on disk is `skipped` unless `overwrite:true`. For the `day` store that test
  is necessarily "is there a file for that YEAR", which is why `overwrite` is the honest
  switch there.
- `downloaded` / `skipped` / `skippedCurrent` / `failed` stay **four separate buckets**: a
  planner must be able to tell "we did not ask" from "it failed".
- A callback that reports success with nothing on disk is reported as a failure, not a
  download — a silent gap is worse than a loud one.

### `GET /data/download/{id}`

```json
{"id":"d1","state":"running","instrument":"ES 12-26","from":"20260901","to":"20260910",
 "kinds":["replay"],"types":["Last","Bid","Ask"],"overwrite":false,"big":false,
 "days":10,"current":"20260903",
 "queuedAt":"2026-09-18T10:00:00","startedAt":"2026-09-18T10:00:00","finishedAt":null,"seconds":null,
 "error":null,
 "downloaded":["20260901/replay"],
 "skipped":["20260902/replay"],
 "skippedCurrent":["20260918"],
 "failed":[{"date":"20260903","kind":"replay","error":"replay download ConnectionLost"}],
 "anyLive":false,"anyNonSim":true,
 "freeDiskBytes":412316860416}
```

`state`: `queued` | `running` | `done` | `error` | `cancelled` | `refused`.
`refused` means exposure (a position or working order) appeared mid-run and the job stopped
itself; `error` carries the reason (no data provider, an exception).
A tick/minute/day entry names the types that really landed AND which connection served it, e.g.
`"20260901/tick (Bid) via connection 1 (Provider31, Simulation)"` or `"… via fallback GetBars
(connection chosen by NinjaTrader)"`. A `failed` entry names every connection tried and
NinjaTrader's own reason, plus the hint that a backtest fetches missing bars on demand from the
connected provider. `404 {"error":"no data download 'dX'"}` for an unknown id.

### Engines

- **replay** → NT8's own `RequestMarketReplay(Instrument, DateTime dateEst, Action<ErrorCode,
  string,object>, IProgress, object state)`. `Connection.HistoricalDataClient` is `internal`,
  so the property and the method resolve **once** in `Start_Data()` through `Compat`
  (keys `Connection.HistoricalDataClient` / `HdsClient.RequestMarketReplay`), with the
  public `Connection.Adapter` as a fallback. When the API is not on this build the job says
  so loudly — `"MarketReplay download API not found on this NT8 build …"` — it is never
  swallowed into "nothing to do". Always `Connection.ClientConnection` first (see "Which
  connection serves it" above) — Market Replay recordings are NinjaTrader's own, not a broker's.
- **tick / minute / day** → `BarsSeries.DownloadFromProvider(coll, overwrite, false, null,
  <candidate connection>, showErrors:false, isRescheduled:false, cb)` per connected candidate
  (see above), one `Bars` per requested `MarketDataType`, falling back to `Bars.GetBars` when no
  candidate answers. `showErrors:false` matters: `true` plausibly raises a modal on the UI
  thread from a background job.
- Per-date wait: 900 s per connection attempt, then that attempt is reported as a timeout and
  the next candidate (or the fallback) is tried.

### Observed behavior — proven live on NinjaTrader 8.1.8.2

With a broker-adapter connection connected (`anyLive:true`, `anyNonSim:true`) and every account flat:

| Case | Answer |
|---|---|
| no real data provider | `409 {"error":"data download needs a real data provider connected …","anyLive":false,"anyNonSim":false}` |
| exposure on any non-Backtest account | `409 {"error":"data download refused: account '…' has an open position — flatten and cancel first","exposure":true}` |
| `/compat` | `Connection.HistoricalDataClient` and `HdsClient.RequestMarketReplay` both `resolved:true` |
| `POST /data/download` for `NQ 12-26` `minute`/`Last`, a 3-day range absent from `db\minute` | every day `downloaded` via `"… via fallback GetBars (connection chosen by NinjaTrader)"` in well under a second; `GET /data/coverage` then lists all three days with `last:1` |
| `SampleMACrossOver` backtest over that same range, same instrument, same period, right after | `state:"done"`, 194 trades — the downloaded bars are usable by a backtest, not just present as files |
| `GET /data/probe?instrument=ES%2012-26&kind=minute` | `earliestDate:"20260612"`, `depthDays:99`, `requestsUsed:13` — a believable listing-date floor for a Dec-26 contract, not the old "≥3650 days" bug (that bug counted bars stamped outside the asked day; fixed by only counting bars where `day.Date <= t < day.Date.AddDays(2)`) |
| `GET /data/coverage?instrument=NQ%2012-26&kind=minute` `cache` section | 8 series across 3 chain contracts (`NQ 03-26`, `NQ 06-26`, `NQ 09-26`), each with real `firstDay`/`lastDay` |

`DownloadFromProvider` itself: tried first on every connected candidate, still fails on this
machine for every candidate and every day (`"download reported failure"`) — kept as the first
attempt in case another machine's provider answers it, but never the only path.

---

## `nt_nrd_export` — no NinjaTrader involved

Pure local Python (`server/nt8_mcp/nrd_offline.py`, copied near-verbatim from cli-nt-bridge,
MIT — see `NOTICE`). No HTTP endpoint: NinjaTrader need not even be running.

`nt_nrd_export(instrument_glob, out_dir, levels=["L1","L2"], force=False, replay_dir="")`
decodes `.nrd` files under `db\replay` to
`<out_dir>/<SEASON>/<SYM>-<SEASON>_<L1|L2>/<YYYYMMDD>.parquet` and returns
`{engine, exported, failed, truncated, corrupt, empty, count}`.

Rules that must not be "simplified away":

- **Header integrity cross-check.** Every decode is verified against the `.nrd` header's own
  per-slot volume sums and price ranges. Byte damage stays *in format* and would otherwise
  decode to silently-wrong data. A mismatch writes **nothing** and lands in `corrupt[]`.
- **Truncation salvage.** An end-of-stream truncation emits the clean prefix and drops the
  incomplete final record (NT8's own engine keeps one garbage trailing row), and the
  integrity check is skipped for it — a legitimate prefix falls short of the header totals.
- **Atomic commit.** Temp sibling + `os.replace`, and a **0-row level is never committed**: a
  silent empty parquet would make every later run skip that date forever.
- `CONTRACT_DIR_RE` accepts both `<SYM> MM-YY` and `<SYM> ##-##`; a half-renamed folder
  (`NQ ##-26`) is skipped rather than guessed at.

`numpy` and `pyarrow` are required and are **not** declared dependencies of `nt8-mcp`;
`nrd_offline` is therefore imported lazily inside the tool, and a missing wheel returns
`{"error": "nt_nrd_export needs numpy and pyarrow: …"}` instead of taking the MCP server down.

## MCP tools

| Tool | Wraps |
|---|---|
| `nt_data_coverage(instrument, kind="", from_date="", to_date="")` | `GET /data/coverage` |
| `nt_data_probe(instrument, kind="minute", wait_s=120)` | `GET /data/probe` + poll `GET /data/probe/{id}` |
| `nt_data_download(instrument, from_date, to_date, kinds=None, types=None, overwrite=False, big=False)` | `POST /data/download` |
| `nt_data_download_status(id)` | `GET /data/download/{id}` |
| `nt_data_download_cancel(id)` | `DELETE /data/download/{id}` |
| `nt_nrd_export(instrument_glob, out_dir, levels=None, force=False, replay_dir="")` | local only |
