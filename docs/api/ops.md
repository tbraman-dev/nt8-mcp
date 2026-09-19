# Live ops — `/ops/*` (module `ops`, `addon/NT8BridgeOps.cs`)

Covers `/ops/flatten`, the naked-position watchdog, and `/ops/reconnect` plus the connection
guardian and the restart CLI. Same conventions as `API.md`: JSON, UTF-8,
`{"error":"…"}` on 4xx/5xx, times local NT8 `yyyy-MM-ddTHH:mm:ss` except `issuedAt` and the audit
log's `ts`, which are UTC.

**This module can only reduce risk on a trading account** (order entry is the separate,
Simulator-only module in `docs/api/orders.md`), and everything it
can do is reduce-only: cancel working orders, flatten open positions, reconnect a connection
NinjaTrader itself dropped. There is no order entry, no strategy enable/disable, no chart-series
switch, no all-accounts form, and `Account.FlattenEverything()` is never called. The module never
creates `ops.enabled` and never creates `ops.live`.

| Method | Path | Returns |
|---|---|---|
| GET | `/ops/status` | armed?, live?, both flag ages, the accounts that are valid targets |
| POST | `/ops/flatten` | dry-run `{plan, confirm, issuedAt}`, or the result of a confirmed flatten |
| POST | `/ops/reconnect` | the same two steps for one inadvertently dropped connection |

**MCP: exactly one tool, `nt_flatten`.** The watchdog (`python -m nt8_mcp.watch`), the connection
guardian (`python -m nt8_mcp.connwatch`) and the restart CLI (`python -m nt8_mcp.restart`) are local
processes on purpose — a model reads their event log, it does not start or stop them — and there is
no tool that can reconnect anything.

**What a refusal looks like to a model.** `nt8_mcp.app` collapses every non-2xx answer to
`{"error": "<the AddOn's sentence>"}`. The status code and the rest of the body — including the new
plan the `409` confirm mismatch returns — do **not** survive that passthrough, so a caller cannot
tell a `403` from a `409` except by reading the sentence, and cannot see what moved. Re-run the
dry-run; do not retry a confirm blindly.

---

## The five gates

Every state change passes all five. They are independent: none of them is a fallback for another.

**1. `ops.enabled`.** A file beside the AddOn in `bin\Custom\AddOns`. Stat-checked on **every
request**, never cached, and **ignored once its mtime is older than 24 h** — a flag forgotten after
one debugging session must not arm the module for ever. The age is bounded on **both** sides: a
future mtime (a clock that runs ahead, a restore from backup, a deliberate `LastWriteTime` stamp)
is ignored too, because "age ≤ 24 h" is true of every negative age and would arm the module
permanently. Disarming is `del ops.enabled`: no
recompile, no NT8 restart. While it is absent or stale, **every** `/ops/*` path — `/ops/status`
included — answers:

```
403 {"error":"ops module not armed"}
```

and nothing else: no flag ages, no account names, no hint about what would have happened. The
endpoint list is not advertised either (see `/compat` below). An unarmed call is **not** audited,
because it never reached the account layer.

**2. `AnyLiveConnected()`.** Both POSTs go through the core's one guard,
`RefuseIfLive(…, force:false)`, and are refused with `409 {"error":"ops … refused: a live
order-routing connection is up …","anyLive":true}` while any Connected connection is neither
Simulator nor Playback and can manage orders. There is no `force` on either endpoint.
`GET /ops/status` is deliberately **not** behind this guard — the order-routing guard restricts
only what can route or disturb orders, and listing account names routes nothing. It reports
`anyLive` and `postsRefused` instead, so the state is visible rather than guessed at from a 409.

**3. `ops.live`.** A second file, same folder. Its **presence** — whoever wrote it, no age rule — is
what makes a non-Simulator account a valid target. Without it a non-Simulator account is not even
**listed** by `/ops/status` (it is counted under `hiddenNonSimulator`), and flattening one answers
`403 … is not a Simulator account and ops.live is absent`. Simulator/Playback is judged by
**provider**, never by the account name: a name is free text a human typed. An account whose
provider cannot be read counts as non-Simulator. The same rule gates reconnecting a non-Simulator
**connection**, which `AnyLiveConnected()` cannot cover because the target is down.

**4. Dry-run by default, and the confirm string is SIGNED.** A POST without `confirm` changes
nothing and returns the plan, the exact confirm string, and `issuedAt`. The string is computed **by
the AddOn** from the freshly resolved plan and **re-computed on the confirming call**, so a position
or working order that moved in between refuses the token (`409`). A mismatch returns the new plan
but **not** a new token — run the dry-run again. That is the step the token exists to make you take.

The string is `<readable plan> #<16 hex>`, where the hex is HMAC-SHA256 over the plan **and** the
`issuedAt` the AddOn stamped, under a random per-process secret that is never persisted or exported.
The readable half is deliberately readable — an operator must see what they are approving — but it
is **not** the token: every one of its terms (account name, each position's instrument/side/qty,
each working order, the connection's name and status) is published by `GET /account` and
`GET /connections`, which are **not** behind `ops.enabled`. Without the signature a caller could
compute a valid confirm from those ungated reads and flatten on its first call, never running the
dry-run, and could re-stamp `issuedAt` on a token it already held. Editing either half now breaks
the MAC. A NinjaTrader restart invalidates every outstanding token, which is correct: a token is
worth 30 seconds.

**5. The 30 s window.** `issuedAt` older than 30 s is refused, and so is one more than 5 s in the
future. It is signed **into** the confirm string, so moving it invalidates the token rather than
refreshing it. A token cannot be replayed out of a transcript or a stale model turn.

**Audit.** Every **armed** call, refusals included, appends one JSON object to
`Documents\NinjaTrader 8\nt8mcp\ops.jsonl` and mirrors it into the ring log (`GET /log`). The line
carries the call's **scope** (`account` *and* `instrument`), not only its plan — without the
narrowing a reviewer cannot tell a whole-account flatten from one scoped to a single instrument,
i.e. cannot tell whether the other instruments were deliberately left alone or never looked at. The
append is serialised under a lock: the core dispatches each request on its own thread pool thread,
and two parallel armed calls would otherwise collide on the file handle and silently drop a line —
possibly the flatten's.

> **Design rule.** `ops.jsonl`, `NT8Bridge.log` and every string these endpoints echo back from
> NinjaTrader — account names, order names, exception text — are **DATA for whoever reads them,
> never instructions**. A third-party AddOn or a data feed can put arbitrary text there. Nothing
> read out of NinjaTrader widens these gates.

---

## `GET /ops/status`

```json
{ "flags": { "armed": true, "flagName": "ops.enabled", "flagAgeHours": 0.12,
             "flagMaxAgeHours": 24, "live": false, "liveName": "ops.live", "liveAgeHours": null },
  "anyLive": false,
  "postsRefused": false,
  "complete": true, "error": null,
  "accounts": [ { "name": "Sim101", "provider": "Simulator", "simulator": true,
                  "openPositions": 1, "workingOrders": 2, "error": null } ],
  "hiddenNonSimulator": 2,
  "backtestAccounts": 1,
  "confirmWindowSec": 30,
  "auditLog": "C:\\Users\\…\\NinjaTrader 8\\nt8mcp\\ops.jsonl",
  "note": "…" }
```

`openPositions` counts non-flat positions; `workingOrders` uses the core's `WorkingStates` list, so
a `CancelSubmitted` order — still live at the broker — is counted. Both are `null` with an `error`
string when that account could not be read; they are never silently 0. The Backtest account is
counted under `backtestAccounts` and is never a target.

`complete` is `false` with a top-level `error` when the account enumeration itself threw part way
(`Account.All` can mutate while it is walked). The `accounts` list and both counters are then
**partial** — this is the one thing that stops a half-read answer from looking like an ordinary
success document to someone asking "is anything at stake anywhere?".

## `POST /ops/flatten`

```jsonc
// step 1 — changes nothing
{ "account": "Sim101" }                      // "instrument": "ES 12-26" narrows it
```
```json
{ "dryRun": true, "account": "Sim101", "instrument": null,
  "plan": { "positions": [ {"instrument":"ES 12-26","side":"Long","qty":2} ],
            "orders":    [ {"id":"o1","instrument":"ES 12-26","action":"Sell",
                            "type":"StopMarket","qty":1,"state":"Working"} ] },
  "confirm": "FLATTEN Sim101 FILTER none ES 12-26 Long 2 CANCEL [o1 ES 12-26 Sell StopMarket 1] #3f9a1c77b2e40d58",
  "issuedAt": 1790000000.0, "expiresInSec": 30,
  "flags": { "…": "…" }, "note": "…" }
```
```jsonc
// step 2 — the only call that changes anything. Echo BOTH halves back byte for byte.
{ "account": "Sim101",
  "confirm": "FLATTEN Sim101 FILTER none ES 12-26 Long 2 CANCEL [o1 ES 12-26 Sell StopMarket 1] #3f9a1c77b2e40d58",
  "issuedAt": 1790000000.0 }
```
```json
{ "ok": true, "dryRun": false, "account": "Sim101", "instrument": null,
  "flattenCalled": true, "ordersCancelRequested": 1,
  "stillOpen": 0, "stillWorking": 0, "positionFlat": true, "observeError": null,
  "plan": { "…": "…" }, "confirm": "…", "errors": [],
  "auditLog": "…\\nt8mcp\\ops.jsonl", "note": "…" }
```

The confirm string is
`FLATTEN <account> FILTER <instr|none> <instr> <side> <qty>[ + …] CANCEL [<id> <instr> <action> <type> <qty>]… #<mac>`,
both lists sorted ordinally (the order of a `Cbi` collection is not a contract, so the string is a
function of the **state**, not of the enumeration), `nothing` in place of either list when it is
empty, and the `#<mac>` described under gate 4.

**Working orders are LISTED, not counted, and the `instrument` narrowing is a term of its own.** A
bare count was blind to a swap: the dry-run's ES stop fills and an NQ bracket goes working inside
the 30 s window, the count is still 1, the string is byte-identical, and the confirming call cancels
an order that appeared in no plan the operator ever read. Listing ids means any substitution,
re-price that issues a new id, or filter change moves the string and is refused.

**`ordersCancelRequested`, not `ordersCancelled`, and `ok` is a MEASUREMENT.** `Account.Cancel` and
`Account.Flatten` are asynchronous and return `void`: a broker that rejects a cancel, or a position
that cannot be closed, throws nothing. So the counter is what was handed to NinjaTrader, and after a
~1.2 s settle the endpoint **re-reads the account** and reports `stillOpen` / `stillWorking` /
`positionFlat` (all `null`/false with `observeError` when that re-read itself failed). `ok:true` and
the audited outcome `flattened` require that re-read to say flat; otherwise the outcome is
`flattenRequested`. The one durable record of a kill switch firing must not assert an outcome
nothing measured.

**A confirmed call on an already-flat account is a no-op, and it still reads `flattened`.** The plan
is `FLATTEN <account> FILTER none nothing CANCEL nothing`; neither `Account.Cancel` nor
`Account.Flatten` is called; the answer is `ok:true, flattenCalled:false, ordersCancelRequested:0`
and the audit line says `outcome:"flattened"` with `flattenCalled:false`.
`flattened` means "the re-read found the account flat", not "this call closed something":
read `flattenCalled` and `ordersCancelRequested` to tell which.

Refusals: `400` account missing or the body is not JSON; `400` the Backtest account; `403` unarmed,
or non-Simulator without `ops.live`; `404` no such account; `409` live connection, stale `issuedAt`,
or confirm mismatch; `500` the positions/orders could not be fully read — **including a single
position row whose instrument or market position could not be read**, because dropping it silently
would let the call answer `ok:true` over a position that is still open. A plan that could not be
read is not a plan, and refusing beats flattening off a partial snapshot. `207` means the flatten
ran and something inside NinjaTrader threw; read `errors`.

**Threading.** Positions and orders are snapshotted under `lock(acct.Positions)` /
`lock(acct.Orders)`, **both locks are released**, and only then are `Account.Cancel` and
`Account.Flatten` called: `Cbi` callbacks re-enter those collections, and acting
inside the lock deadlocks the whole platform with a live position open. There is no `Ui()` hop
anywhere in this endpoint either: these are `Cbi` reads, not WPF, and a kill switch that needs a
healthy UI thread is useless exactly when it is needed.

**What it does not do.** It does **not** disable the strategy. A still-enabled strategy sees itself
flat on the next tick and can re-enter immediately — flattening is not stopping. NinjaTrader closes
the position asynchronously, so re-read `GET /account` to see it flat.

## `POST /ops/reconnect`

Same two steps; `{"name":"Sim101 feed"}`, confirm string
`RECONNECT <name> <status> inadvertent #<mac>` (signed exactly like the flatten token, and for the
same reason: every term of the readable half comes from the ungated `GET /connections`). The name is
matched against **both** configured lists — `Globals.ConnectOptions` **and**
`Globals.BrokerageConnectOptions` — because a brokerage login is not in the first one at all
(a broker demo connection is a common example), and a 404 "not configured" for a
connection `/connections` lists would leave `connwatch` retrying something it can never heal. A
reconnect is **not** reduce-only: it can resume order
routing and re-arm ATMs on a connection a human deliberately parked, so the policy lives in the
AddOn and not only in the Python loop that calls it.

It acts on **one** `dropClass` value, read from the feeds module's classifier
(`addon/NT8Bridge.Feeds.cs`), never re-derived:

| `dropClass` | Meaning | Reconnect |
|---|---|---|
| `inadvertent` | `ConnectionLost`, or `Disconnected` with a real error | **the only one acted on** |
| `failed` | a connect NinjaTrader itself **refused** (`Connecting → Disconnected` with an error) | refused — nothing dropped, and a retry hammers a connection that is saying no |
| `user` | `Disconnected` with `NoError`/`UserAbort` — a human parked it | refused |
| `connected` | already up | `409`, nothing to reconnect |
| `null` | never witnessed: down before this assembly loaded | refused — "not known" is not "inadvertent" |

`Connection.Connect` is marshalled to `Globals.MainThreadDispatcher` through the core's `Ui()` with
a **10 s** bound, so a wedged UI thread cannot hold an HttpListener thread open for ever; a timeout
is audited as `uiTimeout` and answers `504`. The connect is asynchronous, so the endpoint pauses
~1.5 s and reports `statusAfter` as NinjaTrader then had it — `"Connecting"` means it is still under
way. Note that NinjaTrader's own `Disconnect` silently disables every running strategy and restores
none of them on reconnect.

**`ok` and `outcome` come from `statusAfter`, never from the dispatch.** `Connection.Connect`
returns `void` and is asynchronous, so a connect NinjaTrader refuses (expired credentials, broker
down) throws nothing here — reporting "it did not throw" as `outcome:"reconnected"` wrote
`{"outcome":"reconnected","statusAfter":"Disconnected"}` into `ops.jsonl`, i.e. the field an
operator greps saying the feed healed while the feed was down. `Connected` → `ok:true` /
`reconnected`; `Connecting` → `ok:false` / `connecting` (still under way); anything else or unknown
→ `ok:false` / `notConnected`. `connectError` and `connectTimedOut` stay separate fields, so a
failed **dispatch** is still distinguishable from a connect that was dispatched and refused.

## `GET /compat`

Three rows, published at `Start_Ops()` and refreshed on every ops request:

| key | `resolved` | `detail` |
|---|---|---|
| `Ops.armed` | is `ops.enabled` present and fresh | `absent (ops.enabled)` / `armed, age 0.12 h of 24 h` / `STALE (ignored), age 31.40 h of 24 h` |
| `Ops.live` | is `ops.live` present | `absent (ops.live) — Simulator accounts only` / `PRESENT — …` |
| `Ops.endpoints` | = `Ops.armed` | the three paths when armed, `none — module not armed; every /ops/* path answers 403` when not |

That is how an operator reads the state **without** calling an ops path, which would 403. (The
`routes` array in `/compat` lists discovered seam *method* names, `Route_Ops` among them; a module
never edits the core, and a method name advertises no path.)

---

## How to verify flatten on a Simulator account

This exercises the one path no automated test in this repository can cover: `Account.Cancel` +
`Account.Flatten` against a position that actually exists. Everything else (unarmed 403, arming
without a recompile, dry-run, a wrong/stale/re-stamped confirm being refused, a fresh confirm being
a no-op on a flat account, a non-Simulator account being refused, 24 h staleness, audit lines,
delete = disarmed) is covered by `server/tests/test_ops.py`; this procedure is for confirming the
live flatten call itself against a real broken-out position. It takes about three minutes. Use a
Simulator account (e.g. `Sim101`) only.

0. **Pre-flight.** No `ops.live` file in `bin\Custom\AddOns`. `curl -s localhost:7891/health` shows
   `anyLive:false` (only a Simulator or Playback connection Connected — with any broker demo
   connection up, both POSTs answer `409`). No strategy enabled on the target account: a running
   strategy can re-enter on the next tick and the test then reads as a failed flatten.
1. **Open a position by hand** in NinjaTrader (Chart Trader or SuperDOM, on your Simulator account,
   any instrument): Buy Market 1. Then place a **Sell Stop Market 1** far below the market, so the
   cancel path has one working order. No ATM strategy.
2. **Arm** (PowerShell):
   `New-Item -ItemType File '%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\ops.enabled'`
3. `curl -s localhost:7891/ops/status` → `armed:true`, `live:false`, the account's row shows
   `openPositions:1`, `workingOrders:1`. `/health.startedAt` should not change (no recompile needed).
4. **Dry-run** (Git Bash):
   ```bash
   D=$(curl -s -X POST -H "Content-Type: application/json" -d '{"account":"Sim101"}' localhost:7891/ops/flatten); echo "$D"
   ```
   Expect `plan.positions` to list the position, `plan.orders` to list the stop, and `confirm` to be
   a string of the form `FLATTEN <account> FILTER none <instrument> Long 1 CANCEL [<id> <instrument>
   Sell StopMarket 1] #<16 hex>`. Look at NinjaTrader: the position is still open and the stop is
   still working — the dry-run changes nothing.
5. **Confirm, inside 30 s** — both halves echoed back unchanged:
   ```bash
   echo "$D" | python -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({'account':'Sim101','confirm':d['confirm'],'issuedAt':d['issuedAt']}))" | curl -s -X POST -H "Content-Type: application/json" -d @- localhost:7891/ops/flatten
   ```
   Expect `ok:true, flattenCalled:true, ordersCancelRequested:1, stillOpen:0, stillWorking:0,
   positionFlat:true`. `ok:false` with `stillOpen:1` means the ~1.2 s re-read came before NinjaTrader
   finished settling; re-read `/account` — it is only a real failure if the position is still there
   several seconds later.
6. **Verify in two places.** `curl -s "localhost:7891/account?name=Sim101"` → `positions: []`,
   `orders: []`. NinjaTrader's Orders tab: one `Close` order Filled (Sell 1), the stop `Cancelled`.
   The position must be **flat, not reversed** — a `Short 1` afterwards is a fail.
7. **Optional refusals, worth exercising once with a position open** (repeat steps 1 and 4 first):
   change one character of `confirm` → `409`; wait 31 s before step 5 → `409 issuedAt is … s old`;
   place a second working order between steps 4 and 5 → `409` with the new plan and no new token.
   After each, the position stays open.
8. **Audit.** The last lines of `%USERPROFILE%\Documents\NinjaTrader 8\nt8mcp\ops.jsonl` should read
   one `dryRun` entry, then an `outcome:"flattened"` entry with `confirmMatch:true,
   flattenCalled:true, positionFlat:true, opsLive:false`.
9. **Disarm and check.** `Remove-Item '%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\ops.enabled'`,
   then `curl -s localhost:7891/ops/status` → `403`. List the folder: no `ops.enabled`, no `ops.live`.
   Never create `ops.live` for this test.

The `nt_flatten` tool runs the same two steps (`nt_flatten("Sim101")`, then again with `confirm` and
`issued_at`), but only in a session started **after** the module was installed: a running MCP server
is pinned to the code it started with.

---

## The three local processes

None of them is an MCP tool, and each calls the HTTP endpoints above through the **same** two-step
dry-run/confirm path a human would — never an internal shortcut — so every gate applies to them too.

### `python -m nt8_mcp.watch --account Sim101`

Naked-position watchdog. Every scan reads `GET /account?name=…` for each watched account and checks
that a protective stop really covers each open position. A position unprotected for longer than
`--grace` (default 20 s) is flattened, and why is appended to `nt8mcp\watch.jsonl`.

**The protective-stop test is the corrected one.** cli-nt-bridge's accepted any working order on the
instrument whose type contained `Stop`, regardless of quantity and regardless of side. Here a
position is protected only when **opposing stop orders cover its full quantity**:

- quantity: a 1-lot stop under a 5-lot position is **not** protection; several stops are summed, so
  a legitimate 3 + 2 scaled bracket does cover 5;
- side: a long needs `Sell`/`SellShort` stops, a short needs `Buy`/`BuyToCover`. A **buy** stop above
  a long position is an entry stop and protects nothing;
- type: a lone `Limit` is a profit target, not protection.

An account that could not be read is logged and skipped, never treated as flat — and so is an
account **one of whose position or order rows** degraded to `{"error": …}`: `AccountOne` degrades a
row whose `Instrument` read throws, and skipping such a row silently gave both of the worst answers
this loop can give (an unreadable protective stop made a covered position read naked and got it
flattened; an unreadable position row made a naked position read as nothing at all).

Between the scan and the dry-run the position can close and a fresh **entry** order take its place.
The loop therefore checks that the AddOn's fresh plan still holds that instrument on that side
before confirming; if it does not, it logs `planMoved`, confirms nothing, keeps the timer, and lets
the next scan re-decide. The grace period is
not a nicety: a freshly entered position has a moment before its bracket lands, and firing inside
that window kills good trades mid-bracket-placement. `--report-only` logs what it would flatten and
touches nothing. There is no all-accounts form.

### `python -m nt8_mcp.connwatch --connection "Sim101 feed"`

Reconnects watched connections that NinjaTrader dropped inadvertently, once they stay down past
`--grace`. Exponential backoff `15 · 2^(n-1)` capped at 300 s with ±10 % jitter; after
`--max-attempts` failures it cools down for 30 minutes and retries rather than giving up for ever
(an expired token gets refreshed and a session-critical feed must still heal). `Connecting` means a
connect is under way and does not count as a failed attempt, but one that never settles is escalated
so a stuck connect still surfaces. Events go to `nt8mcp\connwatch.jsonl`. An unreadable
`/connections` raises rather than reading as "nothing flagged". It heals **in-session** drops only:
a connection already down when the AddOn loaded has `dropClass: null` for ever, by construction.

### `python -m nt8_mcp.restart --task "<name>" | --exe "<path>"`

Restarts the NinjaTrader process, for the changes a compile, a reload and a chart reload all survive
— bars types above all, which keep executing the pre-reload assembly and publishing into its statics
while everything else reads the new one (a custom bar type is exactly that case).

Two refusals before anything is stopped, then a third at the stop step — and it too refuses before
a single `taskkill` is dispatched:

1. **Neither `--task` nor `--exe`** → refuse. There is no default of either. A scheduled-task name is
   site-specific; a bare `NinjaTrader.exe` produces a *process*, not a usable *platform* — on a box
   that starts NT through a credential wrapper it stops at the Welcome screen while a running-check
   reports healthy. Prefer a task created with `/IT`: it runs in the interactive session, which is
   the only way the UI comes up for a session-0 or SSH caller.
2. **An open position *or a working order* on any account** → refuse. Terminating NinjaTrader with a
   position open leaves it live at the broker with nothing managing it — strictly worse than the
   naked position the watchdog exists to prevent. A resting order is the same hazard one step
   earlier: the account reads flat, the platform goes down, the order fills at the broker anyway,
   and the position it opens has nothing managing it. Exposure that could not be **read** refuses
   too. Deliberately not filtered to a subset of accounts: a spurious refusal costs a sentence,
   `--force-with-open-position` clears it, and a missed one costs an unmanaged live position.
3. **`tasklist` could not be read** → refuse, before any `taskkill` is dispatched. The read is
   **three-valued** (listed / not listed / could not be read), decoded with `errors="replace"`
   because it writes in the console codepage, and *nothing* in the module collapses the third value
   to `false`: that collapse made `stop()` answer "not running" without killing anything, after
   which the start step launched a **second** NinjaTrader against the same user data dir. The same
   rule governs the waits — a window in which the list could not be read never satisfies one, so
   there is no unearned "closed gracefully".

Then: stop (graceful `taskkill`, escalating to `/F` after 25 s, because a graceful close can block on
NinjaTrader's own save-workspace prompt that nothing here can answer) → confirm stopped → start →
confirm the process is back. `ok:true` means the **process** is up; the platform may still be at a
login or workspace screen, so confirm with `GET /health` before running anything headless. It does
not save your workspace and it does not ask.
