# Order entry — `/orders/*` (module `orders`, `addon/NT8BridgeOrders.cs`)

Covers `GET /orders/status`, `POST /orders/submit`, `POST /orders/change`, `POST /orders/cancel`.
Same conventions as `API.md`: JSON, UTF-8, `{"error":"…"}` on 4xx/5xx, times local NT8
`yyyy-MM-ddTHH:mm:ss` except `issuedAt` and the audit log's `ts`, which are UTC.

**This is the only part of this repository that can OPEN a position.** `/ops/*` is reduce-only;
this is not. Everything it can reach is a **`Provider.Simulator` or `Provider.Playback`** account.

**There is no live switch of any kind.** The module never reads `ops.live`, never creates any flag
file, and has no code path that accepts a third provider — not behind a file, not behind a query
parameter, not behind a body field. That is the deliberate difference from `/ops/*`, where
`ops.live` widens the target set: a flatten is reduce-only and an order is not. The **Backtest
account is refused by name** as well, because there is no `Provider.Backtest` — it belongs to the
Simulator connection and would otherwise pass the provider test.

Not here, by design: brackets, ATM strategies, OCO (`oco` is always the empty string),
all-accounts forms, MIT orders, `Ioc`/`Opg`/`Gtd`. One account, one instrument, one order per call.

| Method | Path | Returns |
|---|---|---|
| GET | `/orders/status` | armed?, flag age, the caps in force, the Simulator accounts that are valid targets |
| POST | `/orders/submit` | dry-run `{plan, confirm, issuedAt}`, or the result of a confirmed submit |
| POST | `/orders/change` | the same two steps for the quantity and/or prices of one working order |
| POST | `/orders/cancel` | the same two steps for one working order |

**MCP: exactly three tools** — `nt_order_submit`, `nt_order_change`, `nt_order_cancel`
(`server/nt8_mcp/tools_orders.py`). `GET /orders/status` has no tool on purpose: the state a model
needs before it acts comes back in the dry run of the call it is about to make, and an extra read
tool is one more way to believe a stale answer.

**What a refusal looks like to a model.** `nt8_mcp.app` collapses every non-2xx answer to
`{"error": "<the AddOn's sentence>"}`. The status code and the rest of the body — including the new
plan the `409` confirm mismatch returns — do **not** survive that passthrough. Re-run the dry run;
do not retry a confirm blindly.

---

## The gate chain

Nine gates, in this order. Gate 1 answers before the body is parsed; **every refusal from gate 2
onwards is audited**.

**1. `orders.enabled`.** A file beside the AddOn in `bin\Custom\AddOns`. Stat-checked on **every
request**, never cached, and **ignored once its mtime is older than 24 h** — a flag forgotten after
one debugging session must not arm order entry for ever. The age is bounded on **both** sides: a
future mtime (a clock that runs ahead, a restore from backup, a deliberate `LastWriteTime` stamp)
is ignored too, because "age ≤ 24 h" is true of every negative age. Disarming is
`del orders.enabled`: no recompile, no NT8 restart. While it is absent or stale, **every**
`/orders/*` path — `/orders/status` included — answers:

```
403 {"error":"orders module not armed"}
```

and nothing else. **`ops.enabled` does not arm this module and `orders.enabled` does not arm ops**:
they are different file names, and `Ops_Flag()` is never called from `NT8BridgeOrders.cs`. An
unarmed call is **not** audited, because it never reached the account layer.

**2. `AnyLiveConnected()`.** All three POSTs go through the core's one guard,
`RefuseIfLive(…, force:false)`, and are refused with `409` while any Connected connection is
neither Simulator nor Playback and can manage orders. There is no `force` on any endpoint. It runs
**before the body is parsed** — it needs nothing from it. `GET /orders/status` is deliberately not
behind this guard (listing account names routes nothing); it reports `anyLive` and `postsRefused`
instead.

**3. The account.** Required, matched by name (`OrdinalIgnoreCase`) to **exactly one** `Account` —
two matches is a `409`, not a silent pick. Then `Provider.Simulator` or `Provider.Playback` only,
read from `Account.Provider` first and the account's `Connection.Options.Provider` second; **a read
that throws or returns null refuses**. The Backtest account is refused by name via
`Account.BackTestAccountName`.

**4. Validation.** The instrument resolves through `Instrument.GetInstrument`; `action` is one of
`Buy`, `Sell`, `SellShort`, `BuyToCover`; `type` is one of `Market`, `Limit`, `StopMarket`,
`StopLimit`; `tif` is `Day` (default) or `Gtc`; `quantity` is a **whole** number ≥ 1 (`1.5` is a
400 — truncating it would fill a different order than the plan was signed over); and **exactly**
the prices the type needs, each finite and > 0. A price the type does not use is a `400`, not a
silent drop: a caller that sent `stopPrice` with a `Limit` order believes it set a protective level.
Fixed lists everywhere, never `Enum.Parse` — that would accept `"2"`, `"MIT"`, `"Ioc"` and anything
a future NinjaTrader adds to those enums.

**5. Caps.** See below. Evaluated before the dry run, so a capped call is refused at step one.

**6. Dry run by default.** A POST without `confirm` sends nothing to NinjaTrader and returns the
plan, the exact confirm string, and `issuedAt`.

**7. The signed confirm.** The plan is **rebuilt from fresh state** and the token re-computed on
the confirming call (`Ops_Token` / `Ops_TokenEquals` / `Ops_TokenProblem`, shared with the ops
module: HMAC-SHA256 over the plan **and** the `issuedAt` the AddOn stamped, under a random
per-process secret, fixed-time compare, 30 s window, 5 s forward skew). The readable half starts
with **`orders.<verb>|`** and carries every plan field **and the caps in force**, so all of these
are refused: an `/ops/flatten` confirm, a confirm issued for another verb, an edited plan, an order
that moved, and a config change between the two calls. A mismatch returns the new plan but **not**
a new token — run the dry run again. That is the step the token exists to make you take.

**A confirm is ONE-SHOT.** The `(confirm, issuedAt)` pair is *consumed* the moment it verifies, so
a second call carrying it is refused with `409 … this confirm was already used`. Without that, a
confirm would be a re-usable capability for its whole 30 s life: a `change` or a `cancel` plan
carries the order's own `STATE`, `FILLED` and `FROM` values and so stops matching once the act
lands, but a **submit** plan describes an order that does not exist yet — it rebuilds byte-identical
from fresh state, and one approved dry run would place as many orders as `maxSubmitsPerMinute`
allows. Never re-send a confirmed call because the first one timed out: read `GET /account` first,
then run a fresh dry run. The confirmed response no longer echoes the token back, because it is
spent. A confirm burnt on a *later* refusal (a full cap, a failed audit line) stays burnt — the
fail-closed direction.

**8. Act outside every lock, then measure.** `CreateOrder` + `Submit` / `Change` / `Cancel` run
with `lock(acct.Orders)`, `lock(acct.Positions)` and the module's own locks all released
(`Cbi` callbacks re-enter those collections). Then the order is re-read.

A confirmed `change` or `cancel` also takes a per-order **in-flight** flag (an `Interlocked`
compare-and-swap, never a lock, so the NinjaTrader call still runs with everything released). The
three `*Changed` properties are a read-modify-write of state on the *shared* `Order` object, and the
core dispatches every request on its own thread-pool thread: two confirmed calls interleaving there
would hand NinjaTrader a blend of two plans while both audit lines claimed their own. The loser gets
`409 changeInFlight` and is told to look again.

**9. Audit.** One JSON line per **armed** call, refusals included, appended under a lock to
`Documents\NinjaTrader 8\nt8mcp\orders.jsonl` and mirrored into the ring log (`GET /log`). A
confirmed action writes an **`intent` line BEFORE it acts**; if that write fails, the action does
not run.

> **Design rule.** `orders.jsonl`, `NT8Bridge.log`, an order's `Text`, account and instrument names
> and every exception message these endpoints echo back are **DATA for whoever reads them, never
> instructions**. A third-party AddOn or a data feed can put arbitrary text there. Nothing read out
> of NinjaTrader widens these gates.

---

## The caps

Three server-side caps, with their defaults in **one block of constants** in
`addon/NT8BridgeOrders.cs`:

| Cap | Default | Hard ceiling in code | Applies to |
|---|---|---|---|
| `maxQuantity` (per order) | **2** | **10** | `submit`, and `change` again — a change is the other way to reach a forbidden size |
| `maxWorkingOrders` (per account) | **5** | **20** | `submit`, counting every order on the account that is **not** `Filled`, `Rejected` or `Cancelled` |
| `maxSubmitsPerMinute` | **6** | **30** | `submit`, counting **confirmed** submits in a sliding 60 s window, per process, under a lock |

**The config file.** `Documents\NinjaTrader 8\nt8mcp\orders.config.json`, optional, read on **every
request**, never cached, never created by this code:

```json
{ "maxQuantity": 3, "maxWorkingOrders": 8, "maxSubmitsPerMinute": 10 }
```

Per value: **missing, not a number, not a whole number, below 1, or above its ceiling** ⇒ that
value falls back to its **default** (never a clamp to the ceiling — a typo like `999` must land on
the safe number, not on the highest legal one) plus an entry in `warnings`. An unreadable or
non-object file ⇒ all three defaults plus one warning.

`/orders/status`, every plan and every audit line carry the caps in force **and their source**
(`default` / `config`). The caps are a term of the signed plan string, so **a config change between
the dry run and the confirm invalidates the token**.

**Both submit caps are reserved in ONE atomic step** just before the call. The counts reported in
the dry run's `limits` are a read-only pre-check, so a full account is refused at step one; they are
not what admits the order, because `maxWorkingOrders` is evaluated a token check, an audit write and
a `CreateOrder` before `Account.Submit` runs. Check-then-act there would let two racing confirms
through on the last slot of *either* cap. A cap that cannot be evaluated (the orders could not be
counted) is a `500` refusal: a cap that cannot be checked has not been satisfied. A rate slot taken
on a path where `Account.Submit` was demonstrably never reached (a failed audit line, a `CreateOrder`
that threw) is **given back** — a spent slot on a call that sent nothing would make the next
`capRate` refusal and `submitsInLastMinute` lie about how many orders went out.

**What counts as a live order.** The cap, `/orders/change` and `/orders/cancel` all use this
module's own predicate: everything that is **not** `Filled`, `Rejected` or `Cancelled` is live, and
a state that cannot be read counts as live too. It deliberately does *not* use the core's
`WorkingStates` whitelist, which is built for `GET /account` and names neither
`OrderState.Suspended` (a `Gtc` order between sessions), nor `Initialized` (between `CreateOrder`
and `Submit`), nor `AcceptedByRisk`. Judged by that whitelist those orders would be invisible to the
cap **and** refused for cancellation while live at the broker — leaving the one module that can open
a position with an order nothing here can take back. Both numbers appear per account in
`/orders/status`: `workingOrders` (the core's whitelist, so the document agrees with `GET /account`)
and `liveOrders` (what the cap counts).

---

## `GET /orders/status`

```json
{ "flags": { "armed": true, "flagName": "orders.enabled", "flagAgeHours": 0.12,
             "flagMaxAgeHours": 24 },
  "anyLive": false, "postsRefused": false,
  "complete": true, "error": null,
  "accounts": [ { "name": "Sim101", "provider": "Simulator",
                  "openPositions": 0, "workingOrders": 1, "liveOrders": 1, "error": null } ],
  "hiddenNonSimulator": 2,
  "backtestAccounts": 1,
  "caps": { "maxQuantity": 2, "maxQuantitySource": "default",
            "maxWorkingOrders": 5, "maxWorkingOrdersSource": "default",
            "maxSubmitsPerMinute": 6, "maxSubmitsPerMinuteSource": "default",
            "ceilings": { "maxQuantity": 10, "maxWorkingOrders": 20, "maxSubmitsPerMinute": 30 },
            "defaults": { "maxQuantity": 2, "maxWorkingOrders": 5, "maxSubmitsPerMinute": 6 },
            "configFile": "C:\\Users\\…\\NinjaTrader 8\\nt8mcp\\orders.config.json",
            "configPresent": false, "warnings": [] },
  "submitsInLastMinute": 0, "rateWindowSec": 60, "ownedOrders": 0,
  "confirmWindowSec": 30,
  "orderTypes": ["Market","Limit","StopMarket","StopLimit"],
  "actions": ["Buy","Sell","SellShort","BuyToCover"],
  "tif": ["Day","Gtc"],
  "auditLog": "C:\\Users\\…\\NinjaTrader 8\\nt8mcp\\orders.jsonl",
  "note": "…" }
```

A non-Simulator account is **never listed** — only counted under `hiddenNonSimulator` — and there
is no file that would make it appear. `openPositions` / `workingOrders` / `liveOrders` are `null`
with an `error` string when that account could not be read; they are never silently 0.
`liveOrders` ≥ `workingOrders`: the second is the core's `WorkingStates` whitelist (so this document
agrees with `GET /account`), the first is what `maxWorkingOrders` counts. `complete` is `false` with a
top-level `error` when the account enumeration itself threw part way (`Account.All` can mutate
while it is walked). `ownedOrders` is how many submitted orders are still addressable by
`/orders/change` and `/orders/cancel`.

## `POST /orders/submit`

```jsonc
// step 1 — sends nothing to NinjaTrader
{ "account": "Sim101", "instrument": "ES 12-26", "action": "Buy",
  "type": "Limit", "quantity": 1, "limitPrice": 5000.25, "tif": "Day" }
```
```jsonc
{ "dryRun": true,
  // `workingOrders` here is the LIVE count, as maxWorkingOrders counts it. It is a read-only
  // pre-check: both caps are re-evaluated atomically just before the order goes out.
  "limits": { "workingOrders": 1, "maxWorkingOrders": 5,
              "submitsInLastMinute": 0, "maxSubmitsPerMinute": 6 },
  "plan": { "account": "Sim101", "instrument": "ES 12-26", "action": "Buy", "type": "Limit",
            "quantity": 1, "limitPrice": 5000.25, "stopPrice": null, "tif": "Day" },
  "confirm": "orders.submit|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|ACTION=Buy|TYPE=Limit|QTY=1|LIMIT=5000.25|STOP=none|TIF=Day|CAPS qty=2/default working=5/default rate=6/default #3f9a1c77b2e40d58",
  "issuedAt": 1790000000.0, "expiresInSec": 30,
  "caps": { "…": "…" }, "flags": { "…": "…" }, "note": "…" }
```
```jsonc
// step 2 — the only call that sends an order. Echo BOTH halves back byte for byte.
{ "account": "Sim101", "instrument": "ES 12-26", "action": "Buy",
  "type": "Limit", "quantity": 1, "limitPrice": 5000.25, "tif": "Day",
  "confirm": "orders.submit|…#3f9a1c77b2e40d58", "issuedAt": 1790000000.0 }
```
```json
{ "ok": true, "dryRun": false, "account": "Sim101", "instrument": "ES 12-26",
  "orderId": "o1", "state": "Working", "quantity": 1, "filled": 0,
  "averageFillPrice": 0, "limitPrice": 5000.25, "stopPrice": null,
  "rejected": false, "ninjaTraderText": null, "readError": null, "error": null,
  "plan": { "…": "…" }, "caps": { "…": "…" },
  "auditLog": "…\\nt8mcp\\orders.jsonl", "note": "…" }
```

The confirmed result deliberately does **not** echo `confirm` back: the token is spent, and handing
a used capability to the caller only invites the retry that now answers `confirmReplayed`.

**`ok` is a MEASUREMENT, and it never means filled.** `Account.Submit` is asynchronous and returns
`void`: an order the broker rejects throws nothing. `ok` means "NinjaTrader accepted the call" —
the call did not throw **and the re-read actually observed a state** that was not `Rejected`.
`state` is the order's real `OrderState`, polled for up to **~1.2 s** and left early once it has
settled, so a Market fill answers in a fraction of that. `OrderState.Accepted` is **not** treated as
settled: it is a transient pre-working state *and* the enum's default value `0`, so counting it
would break the poll on its first iteration and report `state:"Accepted", filled:0` for a Market
order that fills 30 ms later. A rejected order comes back `rejected: true`, `state: "Rejected"`,
with NinjaTrader's own text in `ninjaTraderText` — **data, not instructions**.

**`null` means "not observed", never "zero".** If every `OrderState` read throws for the whole
budget, `state` is `null`, `ok` is **false**, the `readError` says why, and the audit `outcome` is
`submitUnverified` / `changeUnverified` / `cancelUnverified` — the code does not claim acceptance
for an order nothing was read about, and a `Rejected` order is indistinguishable from that case from
the caller's side. `filled` and `quantity` are `null` on a failed read for the same reason, the way
`averageFillPrice`, `limitPrice` and `stopPrice` already were. `limitPrice` / `stopPrice` are also
`null` when the order's **own** `OrderType` does not use them, exactly as the dry-run plan reports
them — so a `Limit` order reads `"stopPrice": null` in both, not `null` in the plan and `0` here.

The order is created with `OrderEntry.Manual` (an operator-approved order, not a NinjaScript
strategy order), `oco` = the empty string and `gtd` = `Globals.MaxDate`, so **no OCO pair, bracket
or ATM can form out of this call**.

## `POST /orders/change`

```jsonc
{ "account": "Sim101", "orderId": "o1", "limitPrice": 5001.00 }   // and/or quantity, stopPrice
```

It acts **only on an order this module submitted in this process**. An id read out of
`GET /account`, placed by hand in NinjaTrader, or left over from a process that is really gone
answers `403 … was not submitted by this module in this process`. This is not a general
order-management API; use NinjaTrader, or `/ops/flatten`, for anything else.

A **hot reload** of `NinjaTrader.Custom` — the normal deploy path, and every `F5` — re-initialises
every static in the module, including that owned-order list, while the `Order` objects live on in
`NinjaTrader.Core` and keep working at the broker. `Start_Orders()` therefore **re-adopts** on every
load: it walks the accounts that pass the same provider gate a submit would, and takes back every
live order whose `Account.CreateOrder` name is `NT8Bridge` — this module's own. Without it a `Gtc`
order placed before a deploy would be live at the broker with nothing here able to cancel it.

Give at least one of `quantity`, `limitPrice`, `stopPrice`; the ones you leave out keep their
current value. The order's **own type** decides which prices exist — a `stopPrice` on a `Limit`
order is a `400`, not a no-op. Only an order that is still **live** can be changed (see *What counts
as a live order* above): `Filled`, `Rejected` and `Cancelled` are a `409` naming the real state, and
everything else — `Working`, `PartFilled`, `Suspended`, `AcceptedByRisk` — is accepted.

The plan names the order's **current** quantity, how much of it has already **filled**, and its
prices, beside the requested ones
(`STATE=Working|FILLED=0|FROM qty=1 limit=5000.25 stop=none|TO qty=2 limit=5001 stop=none`), so an
order that moved, part-filled, filled or was cancelled inside the 30 s window refuses the token.

**Read `plan.filled` before confirming.** `Order.Quantity` is the order's **original** size and
never shrinks as it fills, so a `PartFilled` order would otherwise read as if its whole size were
still working: on `quantity 2, filled 1`, asking for `quantity 1` leaves **nothing** working, which
is the opposite of what "cut it to one" means. Only `quantity - filled` is still live.

**How "did it land?" is measured.** After `Account.Change` the order passes through
`ChangePending`/`ChangeSubmitted` and **back to `Working`**, so "the state is Working" is already
true a millisecond after the call — of the *old* order, at the *old* price. The poll therefore
waits until the order's **values** match the request (or it reaches a finished state, or ~1.2 s
passes) and reports `quantity`, `limitPrice` and `stopPrice` as the order really shows them. If
NinjaTrader rounds a price to the tick size the comparison never matches, the poll runs its full
budget and the **true** values are returned: slower, still honest.

## `POST /orders/cancel`

```jsonc
{ "account": "Sim101", "orderId": "o1" }
```

Same ownership rule, same two steps, same live-order rule. The plan carries the order's current
state **and its filled quantity** (`QTY=2|FILLED=1|STATE=PartFilled`), so an order that filled or
part-filled in between refuses the token — and `plan.quantity` alone would have suggested two lots
were cancellable when only one was. The poll waits for `Cancelled`, `Filled` or `Rejected`: **only
`state: "Cancelled"` means cancelled** — an order can fill instead of cancelling.

**The `note` in the body is per verb.** A model does not see the tool docstring again on the tool
result, so the sentence shipped with the answer is the last place the rule reaches it: `submit` gets
"`ok` never means filled", `cancel` gets "only `state:\"Cancelled\"` means cancelled — an order can
fill instead", and `change` gets "`ok` does not mean the change landed — compare `quantity` /
`limitPrice` / `stopPrice` against `plan.to`".

## Status codes

| Code | When |
|---|---|
| `400` | bad JSON; missing/invalid `account`, `instrument`, `orderId`, `action`, `type`, `tif`, `quantity`; a price the type does not use; `confirm` without `issuedAt`; nothing to change |
| `403` | unarmed; non-Simulator account; the Backtest account; an order this module did not submit; any cap |
| `404` | no such account |
| `409` | a live order-routing connection is up; the name matches more than one account; the order is no longer live; a stale `issuedAt`; a confirm mismatch; a confirm that was **already used**; another change or cancel for that order is **in flight** |
| `500` | the order or the working-order count could not be read; the audit line could not be written (**nothing was sent**) |
| `502` | `CreateOrder` / `Submit` / `Change` / `Cancel` threw |
| `504` | the UI thread did not answer (this module makes no dispatcher call of its own) |

## The audit log

One JSON object per line in `Documents\NinjaTrader 8\nt8mcp\orders.jsonl`:

```json
{"ts":"2026-09-19T12:00:00.0000000Z","endpoint":"/orders/submit","status":0,"outcome":"intent",
 "account":"Sim101","instrument":"ES 12-26","orderId":null,"dryRun":false,
 "plan":"orders.submit|ACCOUNT=Sim101|…|CAPS qty=2/default working=5/default rate=6/default",
 "confirmGiven":"…","confirmExpected":"…","confirmMatch":true,
 "caps":{"maxQuantity":2,"maxQuantitySource":"default","…":"…"},
 "anyLive":false,"detail":"about to call NinjaTrader — written BEFORE the action"}
{"ts":"…","endpoint":"/orders/submit","status":200,"outcome":"submitAccepted","account":"Sim101",
 "instrument":"ES 12-26","orderId":"o1","dryRun":false,"plan":"…","confirmGiven":"…",
 "confirmExpected":"…","confirmMatch":true,"caps":{"…":"…"},"anyLive":false,
 "detail":"state=Working, filled=0","state":"Working","filled":0,"rejected":false,
 "submitsInLastMinute":1}
```

`outcome` is one of `read`, `readIncomplete`, `dryRun`, one of the refusal names (`refusedLive`,
`refusedNonSimulator`, `backtestAccount`, `noSuchAccount`, `ambiguousAccount`, `notOwned`,
`notWorking`, `badRequest`, `noIssuedAt`, `staleToken`, `confirmMismatch`, `confirmReplayed`,
`changeInFlight`, `capQuantity`, `capWorkingOrders`, `capRate`, `capUnreadable`, `orderUnreadable`,
`auditFailed`), `intent`, `submitAccepted` / `changeAccepted` / `cancelAccepted`, `submitUnverified`
/ `changeUnverified` / `cancelUnverified` (the call did not throw but **no** `OrderState` could be
read — acceptance is not claimed), `rejected`, `submitFailed` / `changeFailed` / `cancelFailed`,
`uiTimeout`, `error`. The list is exhaustive: filtering the log by it is the review workflow it
exists for, and a name missing from here silently drops a whole class of call — `noIssuedAt`, for
instance, is the signature of a client fabricating tokens. The `caps` object records the caps
**in force at the time**: without it a
reviewer cannot tell whether a 3-lot order was legal then or whether the config file moved
afterwards.

The append is serialised under a lock — the core dispatches each request on its own thread-pool
thread, and two parallel armed calls would otherwise collide on the file handle and silently drop a
line, possibly the submit's.

## `GET /compat`

Three rows, published at `Start_Orders()` and refreshed on every request:

| key | `resolved` | `detail` |
|---|---|---|
| `Orders.armed` | is `orders.enabled` present and fresh | `absent (orders.enabled)` / `armed, age 0.12 h of 24 h` / `STALE (ignored), age 31.40 h of 24 h` |
| `Orders.endpoints` | = `Orders.armed` | the four paths when armed, `none — module not armed; every /orders/* path answers 403` when not |
| `Orders.caps` | always `true` | `CAPS qty=2/default working=5/default rate=6/default` |

That is how an operator reads the state **without** calling an `/orders` path, which would 403.

`Start_Orders()` also logs one `orders re-adopt: N live order(s) named NT8Bridge …` line into the
ring log (`GET /log`), beside the `seam: start Start_Orders` line. `N` is how many orders survived
the reload and are addressable by `/orders/change` and `/orders/cancel` again.

---

## How to verify order entry on a Simulator account

This exercises the one path no automated test in this repository can cover:
`Account.CreateOrder` + `Submit` / `Change` / `Cancel` against a real account. Everything else
(unarmed 403, the dry run, a forged / stale / re-stamped / wrong-verb confirm being refused, the
ownership rule, the caps, the argument mapping) is covered by `server/tests/test_orders.py`. It
takes about five minutes. Use a Simulator account (`Sim101`) only.

0. **Pre-flight.** `curl -s localhost:7891/health` shows `anyLive:false`. No `ops.live` in
   `bin\Custom\AddOns`. `curl -s "localhost:7891/account?name=Sim101"` shows `positions: []` and
   `orders: []`. No strategy enabled on the account.
1. **Unarmed**: all four paths answer 403.
   ```bash
   for p in status submit change cancel; do curl -s -o /dev/null -w "$p %{http_code}\n" localhost:7891/orders/$p; done
   ```
   Create `ops.enabled` alone and repeat: still 403 on every one. Then delete it.
2. **Arm** (PowerShell), no recompile needed:
   `New-Item -ItemType File '%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\orders.enabled'`
3. `curl -s localhost:7891/orders/status` → `armed:true`, `Sim101` listed, caps `2 / 5 / 6` all
   `default`. `/health.startedAt` must not change.
4. **Dry run** (Git Bash):
   ```bash
   D=$(curl -s -X POST -H "Content-Type: application/json" -d '{"account":"Sim101","instrument":"ES 12-26","action":"Buy","type":"Market","quantity":1}' localhost:7891/orders/submit); echo "$D"
   ```
   `/account?name=Sim101` is unchanged — the dry run sent nothing.
5. **Confirm, inside 30 s** — both halves echoed back unchanged:
   ```bash
   echo "$D" | python -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({'account':'Sim101','instrument':'ES 12-26','action':'Buy','type':'Market','quantity':1,'confirm':d['confirm'],'issuedAt':d['issuedAt']}))" | curl -s -X POST -H "Content-Type: application/json" -d @- localhost:7891/orders/submit
   ```
   Expect `state:"Filled"`, `filled:1`. `/account?name=Sim101` shows the position.
6. **Limit, change, cancel.** Submit a `Limit` Buy 1 far below the market (two steps again) →
   `state:"Working"`. Change its `limitPrice` (two steps) → the answer shows the **new** price.
   Cancel it (two steps) → `state:"Cancelled"`, and `/account` shows no working order.
7. **Refusals, each worth doing once**: change one character of `confirm` → 409; wait 31 s → 409
   `issuedAt is … s old`; send the previous step's cancel token to `/orders/submit` → 409; send an
   `/ops/flatten` confirm → 409; `"quantity":3` → 403 `above the cap of 2`; a non-Simulator account
   name **without** a confirm → 403 `no other provider`. After each, nothing moved.
7a. **The replay.** Re-send step 5's confirmed body *unchanged*, within 30 s of `issuedAt` →
   `409 … this confirm was already used`, and `/account?name=Sim101` shows **one** order, not two.
   This is the check that matters most on `submit`: its plan is the only one in the module that
   rebuilds identically after the act.
7b. **Re-adopt across a reload.** Submit a `Gtc` Limit far from the market (two steps) →
   `state:"Working"`. Touch any `.cs` in `bin\Custom` so NinjaTrader recompiles and hot-reloads,
   wait for `seam: start Start_Orders` in `GET /log` and the `orders re-adopt: N live order(s)`
   line beside it, then cancel that order by its id (two steps) → `state:"Cancelled"`, **not**
   `403 notOwned`.
8. **Caps from the config file.** Write
   `%USERPROFILE%\Documents\NinjaTrader 8\nt8mcp\orders.config.json` as
   `{"maxQuantity":3}` → a dry run with `"quantity":3` is accepted and `caps.maxQuantitySource` is
   `config`, with a warning for each missing key. Change it to `{"maxQuantity":999}` → back to `2`,
   `source:"default"`, with a warning naming the ceiling. Delete the file when done.
9. **Audit.** The last lines of `%USERPROFILE%\Documents\NinjaTrader 8\nt8mcp\orders.jsonl` read
   one `dryRun`, then one `intent`, then one `submitAccepted` — every armed call, refusals included,
   has a line, and each carries the caps in force.
10. **Disarm and check.**
    `Remove-Item '%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\orders.enabled'`, then all
    four paths answer 403 again. List the folder: no `orders.enabled`, no `ops.live`.
11. **End flat**: arm ops, `POST /ops/flatten` on `Sim101` (two steps), disarm ops. `/account` shows
    `positions: []` and `orders: []`.

The three tools run the same two steps, but only in a session started **after** the module was
installed: a running MCP server is pinned to the code it started with.
