# Order entry — `/orders/*` (module `orders`, `addon/NT8BridgeOrders.cs`)

Covers `GET /orders/status` and `POST /orders/{submit,bracket,change,cancel,close,reverse}`.
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

Everything else is open and practical, because the account is simulated: the caps are sized for
real testing, a bracket sends its own OCO legs, and `change` / `cancel` reach **every** live order
of the account, not only the ones this module placed.

Not here, by design: ATM strategies, all-accounts forms, `Account.FlattenEverything()`, MIT orders,
`Ioc`/`Opg`/`Gtd`. One account and one instrument per call.

| Method | Path | Returns |
|---|---|---|
| GET | `/orders/status` | armed?, flag age, the caps in force, the Simulator accounts that are valid targets, and **every live order on them with its owner** |
| POST | `/orders/submit` | dry-run `{plan, confirm, issuedAt}`, or the result of a confirmed submit |
| POST | `/orders/bracket` | one entry + one stop + any number of targets, under **one** plan and **one** confirm |
| POST | `/orders/change` | the same two steps for the quantity and/or prices of **any** live order |
| POST | `/orders/cancel` | the same two steps for **any** live order |
| POST | `/orders/close` | cancel one instrument's working orders on one account, then flatten it |
| POST | `/orders/reverse` | the same, then enter the same quantity on the other side |

**MCP: six tools** — `nt_order_submit`, `nt_order_bracket`, `nt_order_change`, `nt_order_cancel`,
`nt_position_close`, `nt_position_reverse` (`server/nt8_mcp/tools_orders.py`).
`GET /orders/status` has no tool on purpose: the state a model needs before it acts comes back in
the dry run of the call it is about to make, and an extra read tool is one more way to believe a
stale answer.

**What a refusal looks like to a model.** `nt8_mcp.app` collapses every non-2xx answer to
`{"error": "<the AddOn's sentence>"}`. The status code and the rest of the body — including the new
plan the `409` confirm mismatch returns — do **not** survive that passthrough. Re-run the dry run;
do not retry a confirm blindly.

---

## The one door

Every state change goes through three members of `NT8Bridge`, and nothing else:

```csharp
// 1. the gate chain + exactly one audit line per armed call
string Ord_Guarded(string endpoint, string verb, string body, ref int status,
                   Func<Ord_Gate, string> act)
// 2. dry run -> signed ONE-SHOT confirm over the plan string -> the intent line, before the act
//    null = CLEARED TO ACT (the token verified, was spent, and the intent line is on disk)
string Ord_Approve(Ord_Gate g, string plan, string planJson, string detail, string limitsJson)
// 3. the result line, after the act
string Ord_Ok(Ord_Call c, string outcome, string detail, params string[] pairs)
```

`Ord_Gate` carries the resolved, provider-checked `Account`, its canonical name, the `Ord_CapSet`
in force, the parsed body, `Confirm` / `IssuedAt`, and the `Ord_Call` audit record. A module added
later (ATM templates, strategies on a Simulator account) writes its own `Route_<Module>` and calls
`Ord_Guarded` from it:

```csharp
if (seg[0] == "atm" && seg[1] == "start" && method == "POST")
    return Ord_Guarded("/atm/start", "atm.start", body, ref status, Atm_Start);
```

It does not repeat the flag test, the live-routing test, the provider test, the caps, the token or
the audit log — and it **cannot weaken** any of them, because none of them takes a parameter that
turns it off. `verb` is the readable prefix of every plan string that call can sign, so two verbs
can never share a token.

Members named `Ord_Raw…` (`Ord_RawCreate`, `Ord_RawSubmit`, `Ord_RawCancel`, `Ord_RawFlatten`,
`Ord_RawFindAccount`) are the low-level pieces the door is built from. They talk to NinjaTrader with
no gate in front of them and **nothing outside `NT8BridgeOrders.cs` calls one**.

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

and no new request does anything. **One thing outlives the arming file, on purpose:** the exits of a
bracket whose entry was accepted while the module **was** armed. The watcher re-reads the flag before
it sends them, but it still sends them — a filled entry with no stop is worse than a send from a
disarmed module — and the audit line is named **`bracketDisarmed`** (or `bracketLiveConnection` when
a live routing connection has come up meanwhile) instead of `bracketExits`, with `armed=false` in its
detail. To leave nothing pending, cancel the resting entry before disarming; `pendingBrackets` in
`/orders/status` is the count, and `Stop_Orders` writes a `bracketAbandoned` line per bracket that
was still waiting at a recompile. **`ops.enabled` does not arm this module and `orders.enabled` does not arm ops**:
they are different file names, and `Ops_Flag()` is never called from `NT8BridgeOrders.cs`. An
unarmed call is **not** audited, because it never reached the account layer.

**2. `AnyLiveConnected()`.** Every POST goes through the core's one guard,
`RefuseIfLive(…, force:false)`, and is refused with `409` while any Connected connection is neither
Simulator nor Playback and can manage orders. There is no `force` on any endpoint. It runs **before
the body is parsed** — it needs nothing from it. `GET /orders/status` is deliberately not behind
this guard (listing account names routes nothing); it reports `anyLive` and `postsRefused` instead.

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
that moved, an order that changed owner, and a config change between the two calls. A mismatch
returns the new plan but **not** a new token — run the dry run again. That is the step the token
exists to make you take.

**A confirm is ONE-SHOT.** The `(confirm, issuedAt)` pair is *consumed* the moment it verifies, so
a second call carrying it is refused with `409 … this confirm was already used`. Without that, a
confirm would be a re-usable capability for its whole 30 s life: a `change`, a `cancel`, a `close`
or a `reverse` plan carries state that stops matching once the act lands, but a **submit** and a
**bracket** plan describe orders that do not exist yet — they rebuild byte-identical from fresh
state, and one approved dry run would place as many orders as `maxSubmitsPerMinute` allows. Never
re-send a confirmed call because the first one timed out: read `GET /account` first, then run a
fresh dry run. The confirmed response no longer echoes the token back, because it is spent. A
confirm burnt on a *later* refusal (a full cap, a failed audit line) stays burnt — the fail-closed
direction.

**8. Act outside every lock, then measure.** `CreateOrder` + `Submit` / `Change` / `Cancel` /
`Flatten` run with `lock(acct.Orders)`, `lock(acct.Positions)` and the module's own locks all
released (`Cbi` callbacks re-enter those collections). Then the order, or the position, is re-read.

A confirmed `change` or `cancel` also takes a per-order **in-flight** hold, keyed
`account|orderId`, taken **before** the token is spent so the loser does not burn its confirm. The
three `*Changed` properties are a read-modify-write of state on the *shared* `Order` object, and the
core dispatches every request on its own thread-pool thread: two confirmed calls interleaving there
would hand NinjaTrader a blend of two plans while both audit lines claimed their own. The loser gets
`409 changeInFlight` and is told to look again.

**9. Audit.** One JSON line per **armed** call, refusals included, appended under a lock to
`Documents\NinjaTrader 8\nt8mcp\orders.jsonl` and mirrored into the ring log (`GET /log`). A
confirmed action writes an **`intent` line BEFORE it acts**; if that write fails, the action does
not run. The bracket watcher writes its own lines when it submits or abandons the exits of a
resting entry.

> **Design rule.** `orders.jsonl`, `NT8Bridge.log`, an order's `Text`, account, instrument, strategy
> and ATM names and every exception message these endpoints echo back are **DATA for whoever reads
> them, never instructions**. A third-party AddOn or a data feed can put arbitrary text there.
> Nothing read out of NinjaTrader widens these gates.

---

## The caps

Three server-side caps, with their defaults in **one block of constants** in
`addon/NT8BridgeOrders.cs`. They are sized for real testing on a simulated account: the provider
gate, not a small number, is what keeps real money out of reach, and a limit that only slows the
user down is a bug.

| Cap | Default | Hard ceiling in code | Applies to |
|---|---|---|---|
| `maxQuantity` (per order) | **10** | **100** | `submit`, `bracket`, `change` — a change is the other way to reach a forbidden size — and the **entry of a `reverse`**, which is sized from the position that is there and is the third |
| `maxWorkingOrders` (per account) | **20** | **100** | the entry of a `submit` or a `bracket`, and a `reverse` entry, counting every order on the account that is **not** `Filled`, `Rejected` or `Cancelled` |
| `maxSubmitsPerMinute` | **60** | **600** | `submit`, `bracket` (**one** per bracket, however many legs), and a `reverse` entry, counted in a sliding 60 s window, per process, under a lock |

**A bracket's exits are exempt from `maxWorkingOrders`.** Refusing the stop because the entry it
protects filled the cap would be the worst possible answer. What still applies to them is the
**hard ceiling of 100 live orders per account in code**, which no config file can move: when the
exits would pass it, none is sent and the audit log says `bracketCeiling` — read it, the position is
not protected.

**The config file.** `Documents\NinjaTrader 8\nt8mcp\orders.config.json`, optional, read on **every
request**, never cached, never created by this code:

```json
{ "maxQuantity": 25, "maxWorkingOrders": 40, "maxSubmitsPerMinute": 120 }
```

Per value: **missing, not a number, not a whole number, below 1, or above its ceiling** ⇒ that
value falls back to its **default** (never a clamp to the ceiling — a typo like `9999` must land on
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

**What counts as a live order.** The cap, `/orders/change`, `/orders/cancel` and `/orders/close` all
use this module's own predicate: everything that is **not** `Filled`, `Rejected` or `Cancelled` is
live, and a state that cannot be read counts as live too. It deliberately does *not* use the core's
`WorkingStates` whitelist, which is built for `GET /account` and names neither
`OrderState.Suspended` (a `Gtc` order between sessions), nor `Initialized` (between `CreateOrder`
and `Submit`), nor `AcceptedByRisk`. Judged by that whitelist those orders would be invisible to the
cap **and** refused for cancellation while live at the broker — leaving the one module that can open
a position with an order nothing here can take back. Both numbers appear per account in
`/orders/status`: `workingOrders` (the core's whitelist, so the document agrees with `GET /account`)
and `liveOrders` (what the cap counts).

---

## Who owns an order

Every `change` and `cancel` plan, and every row in `/orders/status`, names the order's **owner**:

| `owner` | Read from |
|---|---|
| `module` | `Order.Name == "NT8Bridge"` — this module placed it |
| `strategy <name>` | `Order.GetOwnerStrategy()`, by the strategy's `Name` (its type name when `Name` is blank) |
| `atm` | `Order.GetOwnerServerStrategy()` |
| `strategy (name unreadable)` | `Order.OrderEntry == OrderEntry.Automated` — a NinjaScript sent it, but which one could not be read |
| `manual` | nothing above identified an owner |

`manual` is the **fall-through**, not a claim that a human typed the order: `GetOwnerStrategy` and
`GetOwnerServerStrategy` are real members of `NinjaTrader.Cbi.Order` that NinjaTrader does not
document, so what they answer for a given order was only observable against a running NinjaTrader —
observed on 8.1.8.2, see *Who owns an order* above. Every read is guarded on its own and a null
falls through to the next test.

Moving or cancelling a `strategy` or `atm` order changes what that strategy or ATM is managing, and
NinjaTrader may put the order straight back. `OWNER=` is a **signed term of the plan**, so an order
that changed hands between the dry run and the confirm refuses the token.

---

## `GET /orders/status`

```json
{ "flags": { "armed": true, "flagName": "orders.enabled", "flagAgeHours": 0.12,
             "flagMaxAgeHours": 24 },
  "anyLive": false, "postsRefused": false,
  "complete": true, "error": null,
  "accounts": [ { "name": "Sim101", "provider": "Simulator",
                  "openPositions": 1, "workingOrders": 2, "liveOrders": 2,
                  "positions": [ { "instrument": "ES 12-26", "side": "Long", "quantity": 2,
                                   "averagePrice": 5000.25 } ],
                  "orders": [ { "orderId": "o1", "instrument": "ES 12-26", "action": "Sell",
                                "type": "StopMarket", "state": "Working", "quantity": 2,
                                "filled": 0, "limitPrice": null, "stopPrice": 4995,
                                "tif": "Day", "oco": "NT8Bridge-7f3c…", "name": "NT8Bridge",
                                "owner": "module" } ],
                  "ordersTruncated": false, "error": null } ],
  "hiddenNonSimulator": 2,
  "backtestAccounts": 1,
  "caps": { "maxQuantity": 10, "maxQuantitySource": "default",
            "maxWorkingOrders": 20, "maxWorkingOrdersSource": "default",
            "maxSubmitsPerMinute": 60, "maxSubmitsPerMinuteSource": "default",
            "ceilings": { "maxQuantity": 100, "maxWorkingOrders": 100, "maxSubmitsPerMinute": 600 },
            "defaults": { "maxQuantity": 10, "maxWorkingOrders": 20, "maxSubmitsPerMinute": 60 },
            "configFile": "C:\\Users\\…\\NinjaTrader 8\\nt8mcp\\orders.config.json",
            "configPresent": false, "warnings": [] },
  "submitsInLastMinute": 0, "rateWindowSec": 60, "ownedOrders": 1, "pendingBrackets": 0,
  "confirmWindowSec": 30,
  "orderTypes": ["Market","Limit","StopMarket","StopLimit"],
  "actions": ["Buy","Sell","SellShort","BuyToCover"],
  "tif": ["Day","Gtc"],
  "auditLog": "C:\\Users\\…\\NinjaTrader 8\\nt8mcp\\orders.jsonl",
  "note": "…" }
```

A non-Simulator account is **never listed** — only counted under `hiddenNonSimulator` — and there
is no file that would make it appear. `orders[]` lists **every** live order of the account, whoever
placed it: that is where the ids for `/orders/change` and `/orders/cancel` come from.
`openPositions` / `workingOrders` / `liveOrders` / `positions` / `orders` are `null` with an `error`
string when that account could not be read; they are never silently 0 or `[]`. `ordersTruncated` is
`true` when an account holds more than 500 live orders. `liveOrders` ≥ `workingOrders`: the second
is the core's `WorkingStates` whitelist (so this document agrees with `GET /account`), the first is
what `maxWorkingOrders` counts. `complete` is `false` with a top-level `error` when the account
enumeration itself threw part way (`Account.All` can mutate while it is walked). `ownedOrders` is
how many orders this module has placed and still tracks for the cap; `pendingBrackets` is how many
resting entries the watcher is still waiting on.

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
  "limits": { "workingOrders": 1, "maxWorkingOrders": 20,
              "submitsInLastMinute": 0, "maxSubmitsPerMinute": 60 },
  "plan": { "account": "Sim101", "instrument": "ES 12-26", "action": "Buy", "type": "Limit",
            "quantity": 1, "limitPrice": 5000.25, "stopPrice": null, "tif": "Day" },
  "confirm": "orders.submit|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|ACTION=Buy|TYPE=Limit|QTY=1|LIMIT=5000.25|STOP=none|TIF=Day|CAPS qty=10/default working=20/default rate=60/default #3f9a1c77b2e40d58",
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
strategy order), `oco` = the empty string and `gtd` = `Globals.MaxDate`, so **no OCO pair or bracket
can form out of this call**. For an entry with a stop and a target, use `/orders/bracket`.

## `POST /orders/bracket`

One entry, one stop loss and any number of targets, under **ONE plan and ONE confirm**.

```jsonc
// step 1 — a 3-lot Market entry, stop 20 ticks away, scaled out 1 + 2
{ "account": "Sim101", "instrument": "ES 12-26", "action": "Buy", "type": "Market",
  "quantity": 3, "stopLossTicks": 20,
  "targets": [ { "ticks": 40, "quantity": 1 }, { "ticks": 80, "quantity": 2 } ],
  "tif": "Day" }
```
```jsonc
{ "dryRun": true,
  "limits": { "workingOrders": 0, "maxWorkingOrders": 20,
              "submitsInLastMinute": 0, "maxSubmitsPerMinute": 60,
              "bracketCountsAsSubmits": 1, "exitsExemptFromWorkingOrderCap": true,
              "hardWorkingOrderCeiling": 100 },
  "plan": { "account": "Sim101", "instrument": "ES 12-26", "action": "Buy", "type": "Market",
            "quantity": 3, "limitPrice": null, "stopPrice": null, "tif": "Day",
            "exitAction": "Sell", "stopLossPrice": null, "stopLossTicks": 20, "tickSize": 0.25,
            "targets": [ { "price": null, "ticks": 40, "quantity": 1 },
                         { "price": null, "ticks": 80, "quantity": 2 } ] },
  "confirm": "orders.bracket|ACCOUNT=Sim101|INSTRUMENT=ES 12-26|ACTION=Buy|TYPE=Market|QTY=3|LIMIT=none|STOP=none|TIF=Day|EXIT=Sell|SL=ticks 20|TARGETS=T1=ticks 40 qty 1 T2=ticks 80 qty 2|CAPS qty=10/default working=20/default rate=60/default #a1b2c3d4e5f60718",
  "issuedAt": 1790000000.0, "expiresInSec": 30, "caps": { "…": "…" }, "flags": { "…": "…" },
  "note": "…" }
```
```json
{ "ok": true, "dryRun": false, "bracketId": "br1",
  "account": "Sim101", "instrument": "ES 12-26",
  "entry": { "kind": "entry", "orderId": "o1", "state": "Filled", "quantity": 3,
             "requestedPrice": null, "filled": 3, "averageFillPrice": 5000.25,
             "limitPrice": null, "stopPrice": null, "rejected": false,
             "ninjaTraderText": null, "error": null },
  "exits": [ { "kind": "stop", "orderId": "o2", "state": "Working", "quantity": 1,
               "requestedPrice": 4995.25, "filled": 0, "averageFillPrice": 0,
               "limitPrice": null, "stopPrice": 4995.25, "rejected": false,
               "ninjaTraderText": null, "error": null },
             { "kind": "target", "orderId": "o3", "state": "Working", "quantity": 1,
               "requestedPrice": 5010.25, "…": "…" },
             { "kind": "stop", "orderId": "o4", "state": "Working", "quantity": 2,
               "requestedPrice": 4995.25, "…": "…" },
             { "kind": "target", "orderId": "o5", "state": "Working", "quantity": 2,
               "requestedPrice": 5020.25, "…": "…" } ],
  "exitsPending": false, "oco": "NT8Bridge-7f3c…", "error": null,
  "plan": { "…": "…" }, "caps": { "…": "…" },
  "auditLog": "…\\nt8mcp\\orders.jsonl", "note": "…" }
```

`exits[]` lists **one pair per target**: `o2`/`o3` are the first pair (`oco` =
`NT8Bridge-7f3c…-1`), `o4`/`o5` the second (`oco` = `NT8Bridge-7f3c…-2`) — see *The OCO pairs*
below. `Ord_LegJson` does not print `oco` per leg; read it per order from `GET /orders/status`
instead, or from the `orders.jsonl` `bracketExits` line.

**Arguments.** `action` must be `Buy` or `SellShort` — a bracket entry **opens** a position, so
`Sell` and `BuyToCover` are a `400` (use `/orders/submit`, `/orders/close` or `/orders/reverse` for
those). `type` and its prices work exactly as in `/orders/submit`. The stop loss is
`stopLossPrice` (absolute) **or** `stopLossTicks` (an offset), never both and never neither.
`targets` is optional — omit it for a stop only — and each item carries exactly one of `price` or
`ticks`, plus a whole `quantity` ≥ 1. With several targets the quantities **must add up to the entry
quantity**: a scale-out covers the whole entry. Each target gets **its own** stop of matching
quantity — see *The OCO pairs* below for why.

**Tick offsets are resolved from the REAL average fill price** of the entry and rounded with
`MasterInstrument.RoundToTickSize`, not from the price that was asked for. For a long entry the stop
is `fill − ticks × tickSize` and a target is `fill + ticks × tickSize`; for a short entry both
reverse. An instrument whose tick size cannot be read is a `400` — give absolute prices instead.
If, when the exits are about to go out, the average fill price is unreadable or ≤ 0 and any leg was
given as ticks, **no exit is sent** and the audit line says `bracketUnverified`: the position is not
protected, and a guessed price would be worse than none.

**The exits are sized to what the entry really FILLED**, never to what was asked for. A 3-lot entry
that filled 2 gets a stop of 2 and targets of 1 and 1 — the requested quantities are taken in order
and truncated to what exists.

**When the exits go out.** A `Market` entry is waited for inside the request (up to 4 s), so the
answer carries the exits and `exitsPending` is `false`. A **resting** entry (`Limit`, `StopMarket`,
`StopLimit`) comes back with `exitsPending: true` and an empty `exits` array — **no stop and no
target are at the broker yet, so the position is not protected**. A single bounded watcher thread
(`NT8Bridge-brackets`, one poll every 250 ms, started lazily at the first resting bracket and joined
in `Stop_Orders`) submits them when the entry reaches `Filled`, `Cancelled` or `Rejected`:

* entry `Filled` ⇒ the exits go out, sized to the fill, and the audit line is `bracketExits`
  (`bracketExitsPartial` when a leg failed, with that leg's error);
* entry `Cancelled` or `Rejected` with nothing filled ⇒ **no exit at all**, audit `bracketNoFill`;
* entry part-filled and then cancelled ⇒ exits sized to the part that filled;
* nothing after **240 minutes** ⇒ the bracket is dropped, audit `bracketAbandoned`.

*Known ceiling:* the exits are submitted when the entry reaches a **terminal** state. A resting
entry that part-fills and keeps resting is not protected until it finishes. `pendingBrackets` in
`/orders/status` is how many entries are in that state.

**The OCO pairs.** Observed on NinjaTrader 8.1.8.2: cancelling or filling one order of an OCO group
cancels every other **live** order that carries the same `oco` id. Because of that, the exits are
built as **one OCO pair per target**, not one shared group: the base id returned as `oco` on the
bracket response (`NT8Bridge-<guid>`) gets a numeric suffix per pair — the first target's stop and
that target share `<base>-1`, the second pair shares `<base>-2`, and so on. Each pair's stop is
sized to match its target's quantity, so filling one target only ever cancels the stop protecting
*that slice*, never the stop covering a target that has not filled yet. Targets are matched in the
order given and truncated to what the entry really filled, so a part-filled entry can run out of
fill before the targets are used up; whatever quantity that leaves uncovered gets a stop of its own
with **no** `oco` partner (`oco: ""`). Step 6b of the live check below is a regression check for
this.

`ok` means the **entry** was accepted **and every exit that was due to be sent actually went out**.
For a resting entry that has not filled yet, `ok` is `true` with `exitsPending:true` — nothing has
failed, the exits simply are not due yet. `ok` is `false` when the entry itself was rejected, or
when the entry filled and one of its stop/target legs failed to submit. It never means filled — read
`exitsPending` and `exits[]` for that. A leg that was not sent carries its own `error`; the others
still went.

## `POST /orders/change`

```jsonc
{ "account": "Sim101", "orderId": "o1", "limitPrice": 5001.00 }   // and/or quantity, stopPrice
```

It acts on **every live order of the account**, not only the ones this module placed: a running
strategy's stop, an ATM's target and a hand-placed limit are all reachable by their order id, which
`GET /orders/status` lists. An id that matches no live order on that account is a `404`. What
protects the caller is not a shorter list but **the plan**, which names the order's `owner`, its
`oco` group, its `state` and how much has `filled` before anything is signed — see *Who owns an
order* above.

The account's own `Account.Orders` collection is snapshotted under its lock and every member is read
after that lock is released. The module's own bookkeeping list is searched too, because NinjaTrader
assigns `OrderId` asynchronously and an order submitted a millisecond ago may not be in the
collection yet.

A **hot reload** of `NinjaTrader.Custom` — the normal deploy path, and every `F5` — re-initialises
every static in the module while the `Order` objects live on in `NinjaTrader.Core`.
`Start_Orders()` therefore **re-adopts** on every load: it walks the accounts that pass the same
provider gate a submit would and takes back every live order whose `Account.CreateOrder` name is
`NT8Bridge`, so the working-order count and the `module` owner label are right again. Addressability
no longer depends on it — every live order is addressable — but the count and the label do.

Give at least one of `quantity`, `limitPrice`, `stopPrice`; the ones you leave out keep their
current value. The order's **own type** decides which prices exist — a `stopPrice` on a `Limit`
order is a `400`, not a no-op. Only an order that is still **live** can be changed (see *What counts
as a live order* above): `Filled`, `Rejected` and `Cancelled` are a `409` naming the real state, and
everything else — `Working`, `PartFilled`, `Suspended`, `AcceptedByRisk` — is accepted.

The plan names the order's **current** quantity, how much of it has already **filled**, its owner
and its OCO group, beside the requested values
(`OWNER=module|…|OCO=none|STATE=Working|FILLED=0|FROM qty=1 limit=5000.25 stop=none|TO qty=2 limit=5001 stop=none`),
so an order that moved, part-filled, filled, changed owner or was cancelled inside the 30 s window
refuses the token.

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

Changing a `strategy` or `atm` order changes what that strategy or ATM is managing, and NinjaTrader
may put it straight back. That is a real outcome on a simulated account, not a reason to refuse it.

## `POST /orders/cancel`

```jsonc
{ "account": "Sim101", "orderId": "o1" }
```

Same reach, same two steps, same live-order rule. The plan carries the order's owner, its OCO group,
its current state **and its filled quantity** (`OWNER=atm|OCO=…|QTY=2|FILLED=1|STATE=PartFilled`),
so an order that filled or part-filled in between refuses the token — and `plan.quantity` alone
would have suggested two lots were cancellable when only one was. When the order carries a non-empty
`oco`, the plan also carries `plan.ocoWarning`, spelling out in plain language that NinjaTrader
cancels every other **live** order sharing that `oco` id the moment this one is cancelled —
cancelling a bracket's target also cancels **its own paired** stop (see *The OCO pairs* above) and
leaves that slice of the position unprotected. `ocoWarning` is `null` when `oco` is empty (nothing
to warn about). To move a target's price instead of cancelling it, use `/orders/change`, which does
not touch the OCO pairing. The poll waits for `Cancelled`, `Filled` or `Rejected`: **only
`state: "Cancelled"` means cancelled** — an order can fill instead of cancelling.

## `POST /orders/close` and `POST /orders/reverse`

```jsonc
{ "account": "Sim101", "instrument": "ES 12-26" }
```

**ONE account, ONE instrument.** `close` cancels that instrument's working orders on that account —
whoever placed them, listed by id in the plan — and then flattens that instrument's position.
`reverse` does the same and then enters the **same quantity on the other side** with a `Market`
order. `Account.FlattenEverything()` is never called, here or anywhere in this repository: the
flatten takes a one-instrument list, so nothing else on the account is touched.

```jsonc
// the dry run's plan
{ "account": "Sim101", "instrument": "ES 12-26",
  "position": { "side": "Long", "quantity": 2, "averagePrice": 5000.25 },
  "cancelOrders": ["o3", "o4"],
  "enterAfterFlat": { "side": "Short", "quantity": 2, "type": "Market" } }  // null for close
```
```json
{ "ok": true, "dryRun": false, "account": "Sim101", "instrument": "ES 12-26",
  "cancelledOrders": 2, "cancelError": null, "flattenError": null,
  "positionBefore": { "side": "Long", "quantity": 2, "averagePrice": 5000.25 },
  "positionAfter": { "side": "Short", "quantity": 2, "averagePrice": 5000.5 },
  "positionAfterError": null,
  "entry": { "kind": "entry", "orderId": "o5", "state": "Filled", "quantity": 2, "…": "…" },
  "plan": { "…": "…" }, "caps": { "…": "…" }, "auditLog": "…", "note": "…" }
```

`close` on an account with **no position and no working order** in that instrument is a `409`
(`nothingToDo`); `reverse` with **no position** is a `409` (`noPosition`) — there is nothing to
reverse.

**`maxQuantity` applies to the reverse entry**, and the refusal lands on the **dry run**, so no
token is ever issued for a plan the caps forbid. The entry is sized from the observed position, not
from the body, so a position built by something this module does not cap — a strategy started
through `/strategy/start`, or a hand-placed order — is where an oversized reverse would come from:
`Long 40` under a cap of 10 is `403 capQuantity`, naming the position, the cap and its source. Use
`/orders/close`, which only reduces and is deliberately **not** capped: capping the way out of an
oversized position would be the worse failure.

**The reverse entry goes out ONLY once the account is observed flat.** Cancel and flatten are
asynchronous, so the module waits ~1.2 s and re-reads the position: if the instrument is still
showing a side, or the position could not be re-read at all, **nothing is entered** and
`entry.error` says which. Sending a market order on top of a position that has not finished closing
would double the size instead of turning it round. The reverse entry takes one rate slot; `close`
takes none.

`positionAfter` is what was **observed**, not what was asked for. `null` there means the position
could not be re-read, which is **not** the same as flat.

## Status codes

| Code | When |
|---|---|
| `400` | bad JSON; missing/invalid `account`, `instrument`, `orderId`, `action`, `type`, `tif`, `quantity`; a price the type does not use; a bracket entry that is not `Buy`/`SellShort`; no stop loss or both forms of it; targets that do not add up; an unreadable tick size; `confirm` without `issuedAt`; nothing to change |
| `403` | unarmed; non-Simulator account; the Backtest account; any cap, the position size of a `reverse` included |
| `404` | no such account; no such live order on that account |
| `409` | a live order-routing connection is up; the name matches more than one account; the order is no longer live; nothing to close; no position to reverse; a stale `issuedAt`; a confirm mismatch; a confirm that was **already used**; another change or cancel for that order is **in flight** |
| `500` | the order, the position or the working-order count could not be read; the audit line could not be written (**nothing was sent**) |
| `502` | `CreateOrder` / `Submit` / `Change` / `Cancel` / `Flatten` threw, or a close/reverse could not be verified |
| `504` | the UI thread did not answer (this module makes no dispatcher call of its own) |

## The audit log

One JSON object per line in `Documents\NinjaTrader 8\nt8mcp\orders.jsonl`:

```json
{"ts":"2026-09-19T12:00:00.0000000Z","endpoint":"/orders/submit","status":0,"outcome":"intent",
 "account":"Sim101","instrument":"ES 12-26","orderId":null,"dryRun":false,
 "plan":"orders.submit|ACCOUNT=Sim101|…|CAPS qty=10/default working=20/default rate=60/default",
 "confirmGiven":"…","confirmExpected":"…","confirmMatch":true,
 "caps":{"maxQuantity":10,"maxQuantitySource":"default","…":"…"},
 "anyLive":false,"detail":"about to call NinjaTrader — written BEFORE the action"}
{"ts":"…","endpoint":"/orders/submit","status":200,"outcome":"submitAccepted","account":"Sim101",
 "instrument":"ES 12-26","orderId":"o1","dryRun":false,"plan":"…","confirmGiven":"…",
 "confirmExpected":"…","confirmMatch":true,"caps":{"…":"…"},"anyLive":false,
 "detail":"state=Working, filled=0","state":"Working","filled":0,"rejected":false,
 "submitsInLastMinute":1}
```

`outcome` is one of `read`, `readIncomplete`, `dryRun`, one of the refusal names (`refusedLive`,
`refusedNonSimulator`, `backtestAccount`, `noSuchAccount`, `ambiguousAccount`, `noSuchOrder`,
`notWorking`, `noPosition`, `nothingToDo`, `badRequest`, `noIssuedAt`, `staleToken`,
`confirmMismatch`, `confirmReplayed`, `changeInFlight`, `capQuantity`, `capWorkingOrders`,
`capRate`, `capUnreadable`, `orderUnreadable`, `positionUnreadable`, `auditFailed`), `intent`,
`submitAccepted` / `changeAccepted` / `cancelAccepted` / `closeAccepted` / `reverseAccepted`,
`submitUnverified` / `changeUnverified` / `cancelUnverified` / `closeUnverified` /
`reverseUnverified` (the call did not throw but **no** state could be read — acceptance is not
claimed), `rejected`, `submitFailed` / `changeFailed` / `cancelFailed` / `bracketFailed`,
`bracketAccepted`, `bracketResting`, and the watcher's own lines — `bracketExits`,
`bracketExitsPartial`, `bracketDisarmed`, `bracketLiveConnection`, `bracketNoFill`, `bracketCeiling`,
`bracketUnverified`, `bracketAbandoned` — plus `uiTimeout` and `error`.

`bracketDisarmed` and `bracketLiveConnection` are `bracketExits` with the gate state that was true
when the exits went out: the watcher re-reads the arming file and `AnyLiveConnected()` before every
send and names the line after what it found, so "this module sent orders while it was disarmed" is
one grep, not an inference. `bracketAbandoned` is written by the watcher at the 240-minute bound
**and** by `Stop_Orders` — one line per bracket that was still waiting for its entry when the AddOn
stopped, naming the account, the instrument and the plan, because a NinjaScript recompile is the
routine deploy path and it leaves those entries live at the broker with no exits coming.

The list is exhaustive: filtering the log by it is the review workflow it exists for, and a name
missing from here silently drops a whole class of call — `noIssuedAt`, for instance, is the
signature of a client fabricating tokens. The `caps` object records the caps **in force at the
time**: without it a reviewer cannot tell whether a 25-lot order was legal then or whether the
config file moved afterwards. The watcher's lines carry `caps: null`, because they are written long
after the request that approved them and the caps that applied are on that request's own lines.

The append is serialised under a lock — the core dispatches each request on its own thread-pool
thread, the watcher has a thread of its own, and two parallel armed calls would otherwise collide on
the file handle and silently drop a line, possibly the submit's.

## `GET /compat`

Three rows, published at `Start_Orders()` and refreshed on every request:

| key | `resolved` | `detail` |
|---|---|---|
| `Orders.armed` | is `orders.enabled` present and fresh | `absent (orders.enabled)` / `armed, age 0.12 h of 24 h` / `STALE (ignored), age 31.40 h of 24 h` |
| `Orders.endpoints` | = `Orders.armed` | the seven paths when armed, `none — module not armed; every /orders/* path answers 403` when not |
| `Orders.caps` | always `true` | `CAPS qty=10/default working=20/default rate=60/default` |

That is how an operator reads the state **without** calling an `/orders` path, which would 403.

`Start_Orders()` also logs one `orders re-adopt: N live order(s) named NT8Bridge …` line into the
ring log (`GET /log`), beside the `seam: start Start_Orders` line. `Stop_Orders()` joins the bracket
watcher for at most 1.5 s and then writes one **`bracketAbandoned` audit line per bracket** that was
still waiting for its entry — their exits were **not** submitted, and that belongs in
`orders.jsonl`, not only in the ring log a restart throws away.

---

## How to verify order entry on a Simulator account

This exercises the one path no automated test in this repository can cover:
`Account.CreateOrder` + `Submit` / `Change` / `Cancel` / `Flatten` against a real account.
Everything else (unarmed 403, the dry run, a forged / stale / re-stamped / wrong-verb confirm being
refused, the owner labels, the caps, the argument mapping) is covered by
`server/tests/test_orders.py`. It takes about fifteen minutes. Use a Simulator account (`Sim101`)
only.

0. **Pre-flight.** `curl -s localhost:7891/health` shows `anyLive:false`. No `ops.live` in
   `bin\Custom\AddOns`. `curl -s "localhost:7891/account?name=Sim101"` shows `positions: []` and
   `orders: []`. No strategy enabled on the account.
1. **Unarmed**: all seven paths answer 403.
   ```bash
   for p in status submit bracket change cancel close reverse; do curl -s -o /dev/null -w "$p %{http_code}\n" localhost:7891/orders/$p; done
   ```
   Create `ops.enabled` alone and repeat: still 403 on every one. Then delete it.
2. **Arm** (PowerShell), no recompile needed:
   `New-Item -ItemType File '%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\orders.enabled'`
3. `curl -s localhost:7891/orders/status` → `armed:true`, `Sim101` listed with `orders: []`, caps
   `10 / 20 / 60` all `default`. `/health.startedAt` must not change.
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
5a. **Close it.** Two steps on `/orders/close` with `{"account":"Sim101","instrument":"ES 12-26"}`
   → `cancelledOrders:0`, `positionAfter.side:null`, and `/account` shows `positions: []`.
6. **Limit, change, cancel.** Submit a `Limit` Buy 1 far below the market (two steps again) →
   `state:"Working"`. Change its `limitPrice` (two steps) → the answer shows the **new** price.
   Cancel it (two steps) → `state:"Cancelled"`, and `/account` shows no working order.
6a. **A Market bracket.** Two steps on `/orders/bracket` with
   `{"account":"Sim101","instrument":"ES 12-26","action":"Buy","type":"Market","quantity":1,`
   `"stopLossTicks":20,"targets":[{"ticks":40,"quantity":1}]}` → `exitsPending:false`, two exits,
   both `Working`, sharing one pair's `oco` (`<base>-1`, since there is only one target). Check the
   stop's `stopPrice` really is `entry.averageFillPrice − 20 × 0.25` and the target's `limitPrice`
   is `+ 40 × 0.25`. Then `/orders/close` the instrument (two steps) and confirm both exits are
   gone.
6b. **Scale-out OCO pairs — regression check.** A 2-lot Market bracket with two targets of 1
   (`"ticks":4` and `"ticks":8`, close enough to fill) and a 40-tick stop produces two pairs: stop 1
   / target 1 under `<base>-1`, stop 1 / target 2 under `<base>-2`. When the FIRST target fills,
   read `/orders/status`: confirm the **paired** stop (same `oco`) is gone and the **other** pair's
   stop and target are untouched — that is the proven NinjaTrader 8.1.8.2 behaviour this design
   exists to work around. If either survives, or the other pair is disturbed, that is a regression.
6c. **A resting bracket.** Same call with `"type":"Limit"` and a `limitPrice` far below the market
   → `exitsPending:true`, `exits:[]`, and `/orders/status` shows `pendingBrackets:1`. Move the
   entry's `limitPrice` up through `/orders/change` until it fills, then check `/orders/status`:
   the stop and the target are there, sized to the fill, and `orders.jsonl`'s last line is
   `bracketExits`. Cancel the entry instead, on a second run, and the last line is `bracketNoFill`
   with no exit at the broker.
7. **Refusals, each worth doing once**: change one character of `confirm` → 409; wait 31 s → 409
   `issuedAt is … s old`; send the previous step's cancel token to `/orders/submit` → 409; send an
   `/ops/flatten` confirm → 409; `"quantity":11` → 403 `above the cap of 10`; a bracket with
   `"action":"Sell"` → 400; a bracket whose targets add up to the wrong number → 400; a
   non-Simulator account name **without** a confirm → 403 `no other provider`. After each, nothing
   moved.
7a. **The replay.** Re-send step 5's confirmed body *unchanged*, within 30 s of `issuedAt` →
   `409 … this confirm was already used`, and `/account?name=Sim101` shows **one** order, not two.
   Repeat it on a bracket: **one** entry and **one** set of exits.
7b. **Re-adopt across a reload.** Submit a `Gtc` Limit far from the market (two steps) →
   `state:"Working"`. Touch any `.cs` in `bin\Custom` so NinjaTrader recompiles and hot-reloads,
   wait for `seam: start Start_Orders` in `GET /log` and the `orders re-adopt: N live order(s)`
   line beside it, then cancel that order by its id (two steps) → `state:"Cancelled"`, and
   `/orders/status` shows its `owner` as `module`, not `manual`. A resting bracket does **not**
   survive the reload: its watcher is gone. Send one (step 6c), touch a `.cs` to force the
   recompile, and read `orders.jsonl`: there is one `bracketAbandoned` line for that bracket,
   naming its account, its instrument and its plan. Cancel the naked entry by hand afterwards.
7c. **Disarm while a bracket is resting.** Send a resting bracket (step 6c), delete
   `orders.enabled`, then move the market to the entry (or lower the entry through the chart, since
   `/orders/change` now answers 403). The exits still arrive, and `orders.jsonl`'s line is
   `bracketDisarmed` with `armed=false` in its detail — not `bracketExits`. This is the documented
   exception to "disarmed means nothing goes out"; confirm it reads that way in the log.
8. **Someone else's order.** Enable `SampleMACrossOver` on `Sim101` on a chart until it has a
   working order, or place one by hand in the DOM. `GET /orders/status` lists it with
   `owner:"strategy SampleMACrossOver"` (or `"manual"`). Cancel it through `/orders/cancel` (two
   steps): the plan must name that owner, and the cancel must land. Do the same with an ATM
   bracket's stop (`owner:"atm"`) and note in *Who owns an order* above what the label really read.
   Disable the strategy afterwards.
8a. **A reverse bigger than the cap.** With `maxQuantity` at its default 10, build a `Long 12` on
   `Sim101` — two 6-lot submits, or one hand-placed 12 in the DOM — then dry-run `/orders/reverse`
   on that instrument → `403 capQuantity` naming `Long 12`, the cap and `default`, and **no**
   confirm in the body. `/orders/close` on the same position dry-runs normally and closes it (two
   steps): the way out of an oversized position is never capped.
9. **Caps from the config file.** Write
   `%USERPROFILE%\Documents\NinjaTrader 8\nt8mcp\orders.config.json` as
   `{"maxQuantity":25}` → a dry run with `"quantity":25` is accepted and `caps.maxQuantitySource` is
   `config`, with a warning for each missing key. Change it to `{"maxQuantity":9999}` → back to
   `10`, `source:"default"`, with a warning naming the ceiling. Delete the file when done.
10. **Audit.** The last lines of `%USERPROFILE%\Documents\NinjaTrader 8\nt8mcp\orders.jsonl` read
    one `dryRun`, then one `intent`, then one `submitAccepted` — every armed call, refusals
    included, has a line, and each request line carries the caps in force.
11. **Disarm and check.**
    `Remove-Item '%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\orders.enabled'`, then all
    seven paths answer 403 again. List the folder: no `orders.enabled`, no `ops.live`. Check
    `/orders/status` first, while still armed: `pendingBrackets` must be `0`, or the watcher will
    keep sending those exits (step 7c) after the file is gone.
12. **End flat**: `/orders/close` every instrument that still shows a position (two steps each), then
    `/account` shows `positions: []` and `orders: []`.

The six tools run the same two steps, but only in a session started **after** the module was
installed: a running MCP server is pinned to the code it started with.
