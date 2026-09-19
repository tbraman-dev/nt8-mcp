# Data store — `/data/coverage`, `/data/download` (module `data`, `addon/NT8Bridge.Data.cs`)

Covers `/data/coverage`, `nt_nrd_export` (pure Python) and `/data/download`. Same conventions as
`API.md`: JSON, UTF-8, `{"error":"…"}` on 4xx/5xx,
times local NT8 `yyyy-MM-ddTHH:mm:ss`. Dates on this module's wire are always `YYYYMMDD`.

| Method | Path | Returns |
|---|---|---|
| GET | `/data/coverage?instrument=ES%2012-26&kind=&from=&to=` | what is on disk, per store — below |
| POST | `/data/download` | `202 {"id":"d1","state":"queued","days":N,…}` — **flag-gated**, below |
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

Errors: `400` for a missing `instrument`, a bad `kind`, a non-`YYYYMMDD` date, or a range
`RangeProblem` rejects (inverted, or a placeholder year).

---

## `POST /data/download` — opt-in, disarmed by default

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
→ 202 {"id":"d1","state":"queued","days":10,"anyLive":false,"anyNonSim":true,
       "flag":{"name":"data.download.enabled","armed":true,"ageHours":0.42,"maxAgeHours":24}}
```

### The arming flag

A file named **`data.download.enabled`** beside the AddOn, in
`Documents\NinjaTrader 8\bin\Custom\AddOns`. Its content is irrelevant.

- `File.GetLastWriteTimeUtc` is **stat-checked on every request** and never cached.
- The flag is **IGNORED once its mtime is older than 24 h.** That clause is the whole point:
  it is what stops a flag forgotten after one debugging session from arming the module
  forever. Re-arming is one `touch` of the file.
- Absent or stale → **`403 {"error":"data download not enabled"}`**, for `POST` and for the
  job worker before every single date.
- Its state and age are published into `GET /compat` as the row
  `Data.downloadFlag` (`detail` = `armed, age 0.42 h of 24 h` / `STALE (ignored), age 31.20 h`
  / `absent (data.download.enabled)`). `/compat` is core-owned, so that row is **as of the
  last flag check**, not as of the `/compat` call itself.

### The guards

A download routes no order, so the order-routing predicate `AnyLiveConnected()` does **not**
refuse it: an earlier version of this guard did, which made the endpoint unreachable on a machine
whose only real data provider is a broker demo connection (it can route orders, so it counts as
live). The refusal is **exposure**.

| Guard | Role here | Effect |
|---|---|---|
| flag `data.download.enabled` | the arming, user-created, stale after 24 h | `403` without it |
| `Data_Exposure()` | **the refusal** — an open position or a working order on any account but the Backtest one, paper included | `409`, body carries `"exposure": true` and names the account |
| `AnyNonSimConnected()` | **the precondition** — any real data provider is connected | `409` while **false** |

All three are re-checked before every date of a running job; exposure mid-run ends the job with
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
- Every guard is **re-checked before each date**, not once at POST: a live connection can
  come up, and the flag can go stale, in the minutes a multi-day job runs. A live connection
  appearing mid-run stops the job at `state:"refused"`.

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
 "flag":{"name":"data.download.enabled","armed":true,"ageHours":0.42,"maxAgeHours":24},
 "freeDiskBytes":412316860416}
```

`state`: `queued` | `running` | `done` | `error` | `cancelled` | `refused`.
`refused` means a live order-routing connection came up mid-run and the job stopped itself;
`error` carries the reason in `error` (stale flag, no data provider, an exception).
A tick/minute/day entry names the types that really landed: `"20260901/tick (Bid)"`.
`404 {"error":"no data download 'dX'"}` for an unknown id.

### Engines

- **replay** → NT8's own `RequestMarketReplay(Instrument, DateTime dateEst, Action<ErrorCode,
  string,object>, IProgress, object state)`. `Connection.HistoricalDataClient` is `internal`,
  so the property and the method resolve **once** in `Start_Data()` through `Compat`
  (keys `Connection.HistoricalDataClient` / `HdsClient.RequestMarketReplay`), with the
  public `Connection.Adapter` as a fallback. When the API is not on this build the job says
  so loudly — `"MarketReplay download API not found on this NT8 build …"` — it is never
  swallowed into "nothing to do".
- **tick / minute / day** → `BarsSeries.DownloadFromProvider(coll, overwrite, false, null,
  Connection.ClientConnection, showErrors:false, isRescheduled:false, cb)`, one `Bars` per
  requested `MarketDataType`. That is exactly the Historical Data window's Download button.
  `showErrors:false` matters: `true` plausibly raises a modal on the UI thread from a
  background job.
- Per-date wait: 900 s, then the date is reported as a timeout and the loop continues.

### Observed behavior, and what is not covered

Observed on NinjaTrader 8.1.8.2 with only `Playback` connected (`anyLive:false`, `anyNonSim:false`):

| Case | Answer |
|---|---|
| no flag file | `403 {"error":"data download not enabled"}`; `/compat` `Data.downloadFlag` = `absent (data.download.enabled)` |
| fresh flag, no real data provider | `409 {"error":"data download needs a real data provider connected …","anyLive":false,"anyNonSim":false}`; `/compat` = `armed, age 0.00 h of 24 h` |
| flag `LastWriteTime` set 25 h back | `403` again; `/compat` = `STALE (ignored), age 25.00 h of 24 h` |
| flag set 23.9 h back | armed again (`409`, not `403`) |
| flag deleted | `403` |
| `/compat` | `Connection.HistoricalDataClient` and `HdsClient.RequestMarketReplay` both `resolved:true` |

**Manual check (about 10 minutes).** Any real data provider works, including a broker demo
connection (it can route orders, so it counts as live). Every account must be flat with no working
order.

1. `curl -s localhost:7891/health` must say `"anyNonSim":true`.
2. PowerShell: `New-Item -ItemType File 'C:\Users\you\Documents\NinjaTrader 8\bin\Custom\AddOns\data.download.enabled'`
3. `curl -s -X POST -H "Content-Type: application/json" -d "{\"instrument\":\"ES 12-26\",\"from\":\"20260909\",\"to\":\"20260909\",\"kinds\":[\"tick\"],\"types\":[\"Bid\"]}" localhost:7891/data/download`
   → `202 {"id":"d1",…}`. (`20260909` is absent today: `GET /data/coverage?instrument=ES%2012-26` starts at `20260910`.)
4. `curl -s localhost:7891/data/download/d1` until `state` is `done`. Expect `downloaded:["20260909/tick (Bid)"]`,
   no modal on screen, `/health.standingModal` null. Then `GET /data/coverage?instrument=ES%2012-26&kind=tick`
   must list `20260909` with `bid` > 0.
5. Today's date: repeat step 3 with `from`/`to` = today. Expect it in `skippedCurrent`, never in `downloaded`.
6. Mid-run re-check: POST a 5-day range, delete the flag file while `current` is the first date.
   Expect `state:"error"` with `arming flag data.download.enabled is absent or stale — stopped`.
7. PowerShell: `Remove-Item 'C:\Users\you\Documents\NinjaTrader 8\bin\Custom\AddOns\data.download.enabled' -Force`
   (do this whatever happened), then disconnect the data connection.

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
| `nt_data_download(instrument, from_date, to_date, kinds=None, types=None, overwrite=False, big=False)` | `POST /data/download` |
| `nt_data_download_status(id)` | `GET /data/download/{id}` |
| `nt_data_download_cancel(id)` | `DELETE /data/download/{id}` |
| `nt_nrd_export(instrument_glob, out_dir, levels=None, force=False, replay_dir="")` | local only |
