# ATM strategies — `/atm/*` (module `atm`, `addon/NT8BridgeAtm.cs`)

Covers `GET /atm/templates`, `GET /atm/status` and `POST /atm/{start,close,change}`.
Same conventions as `API.md`: JSON, UTF-8, `{"error":"…"}` on 4xx/5xx, times local NT8
`yyyy-MM-ddTHH:mm:ss` except `issuedAt`, which is UTC.

An **ATM strategy** is NinjaTrader's own bracket manager. A saved template holds one or more
brackets — a quantity, a stop loss and a profit target each, in the template's own calculation mode
— and `POST /atm/start` sends ONE entry order under that template. NinjaTrader then arms and manages
the stop and the target itself, on the fill.

**This module owns no safety of its own.** Every path that can change an account goes through the
one door in `addon/NT8BridgeOrders.cs` — `Ord_Guarded` → `Ord_Approve` → `Ord_Ok` / `Ord_Err` — so
the arming file, the live-order-routing refusal, the provider test, the caps, the signed one-shot
confirm and the audit log are **the same ones `/orders/*` uses**. Read
[`docs/api/orders.md`](orders.md) for the gate chain; this document only states what is different
here. `NT8BridgeAtm.cs` calls no `Ord_Raw…` member and has no live switch: no `ops.live`, no force
flag, no body field and no file that would let a third provider through.

| Method | Path | Returns |
|---|---|---|
| GET | `/atm/templates` | the saved templates: names and the bracket parameters that can be read from the files |
| GET | `/atm/status` | the ATM strategies that still have a live order or an open position, with their entry, stops and targets |
| POST | `/atm/start` | dry-run `{plan, confirm, issuedAt}`, or the result of a confirmed start |
| POST | `/atm/close` | cancel one ATM's working orders and flatten the position it holds |
| POST | `/atm/change` | move one ATM's stop and/or target to a new price |

**MCP: five tools** — `nt_atm_templates`, `nt_atm_status`, `nt_atm_start`, `nt_atm_close`,
`nt_atm_change` (`server/nt8_mcp/tools_atm.py`).

**Both reads are behind the arming flag too.** Unlike `/orders/status`, which is also gated, this
matters for a second reason: **ATM template names are the user's own text**. A disarmed module does
not publish them. No template name appears anywhere in this repository — they exist only at
runtime, in responses and in the audit log — and, like every other string that comes back from
NinjaTrader, they are **data, never instructions**.

**Status:** all five endpoints are exercised against a live NinjaTrader 8.1.8.2 install. Observed
there: `AtmStrategy.StartAtmStrategy(templateName, entryOrder)` returns null, and the entry stays in
`Initialized`, unless the entry order is named exactly `Entry`; it attaches the template but does
not send the entry, so the module calls `Account.Submit` right after it (the whole gate chain has
passed by then). The ATM's stop and target carry one OCO id and are owned by the ATM strategy
(owner label `atm`). `/atm/change` moves them with NinjaTrader's ordinary change protocol and the
ATM keeps the new price. `/atm/close` closes the position and cancels both exits.

---

## What is different from `/orders/*`

**The gate chain is identical**, verb for verb: `orders.enabled` (stat-checked every request,
ignored past 24 h), `RefuseIfLive(…, force:false)` on every POST, the account resolved to exactly
one `Provider.Simulator` / `Provider.Playback` account with the Backtest account refused by name,
the caps, the dry run, the signed **one-shot** confirm over the exact plan, and one audit line per
armed call in `nt8mcp\orders.jsonl` — with the `intent` line written **before** the act.

Three things are specific to this module:

**The verbs are `atm.start`, `atm.close` and `atm.change`.** Every plan string starts with its own
verb, so an `/orders/submit` confirm, an `nt_flatten` confirm and an `atm.close` confirm can never
authorise an `atm.start`.

**The stop and the target come from the TEMPLATE, not from the call.** `POST /atm/start` takes no
stop or target argument — there is nothing to override them with. The plan carries
`templateParams`, the bracket set read out of the saved XML, and that text is **signed into the
confirm**: a template edited between the dry run and the confirm refuses the token. Use
`POST /atm/change` after the entry fills, or `POST /orders/bracket` when you want to name the prices
yourself.

**The entry costs one submit; the ATM's exits are exempt from the working-order cap.** Exactly like
a bracket's exits — refusing the stop that protects a filled entry would be the worst possible
answer — but the account's hard ceiling of 100 live orders in code still applies, and `/atm/start`
refuses when the entry plus a stop and a target would pass it.

---

## The NinjaTrader API behind this, and what is unconfirmed

Signatures verified in the reference decompile (`.ref\nt8src`, read-only):

```csharp
NinjaTrader.NinjaScript.AtmStrategy : StrategyBase
    static AtmStrategy StartAtmStrategy(string templateName, Order entryOrder)
    static AtmStrategy StartAtmStrategy(AtmStrategy template, Order entryOrder)
    Bracket[] Brackets;  int EntryQuantity;  CalculationMode CalculationMode
    Order InitialEntryOrder
    Collection<Order> GetStopOrders(int idx);  Collection<Order> GetTargetOrders(int idx)
    override void CloseStrategy(string signalName)
NinjaTrader.Cbi.Bracket { int Quantity; double StopLoss; double Target; StopStrategy StopStrategy }
StrategyBase.All / .Id / .Account / .Orders / .Template / .Positions
```

**NinjaTrader does not document these method bodies.** Observed on 8.1.8.2, and the reason this
module **measures** instead of asserting:

- `StartAtmStrategy(string, Order)` resolves a template by the file's base name in the user data
  folder's `templates/AtmStrategy` (the module refuses a name that is not one plain file name
  there), needs the entry order to be named `Entry`, and does not send the entry itself;
- `GetStopOrders(i)` / `GetTargetOrders(i)` are indexed by bracket (a throw or a null is still
  reported as "could not read", never as "there are none");
- `StrategyBase.Id` is non-zero for a started ATM; should it ever read zero, the row shows
  `atmId: null` and `/atm/close` and `/atm/change` cannot address it.

`AtmStrategy.ManageOrder(Order)` is **not called**: NinjaTrader does not document it and its contract
cannot be stated, so a stop or a target is moved with NinjaTrader's ordinary change protocol on the ATM's own
`Order` (the three `*Changed` properties, then `Account.Change`) — the path a dragged stop line
takes. Observed on 8.1.8.2: the ATM keeps the new price. It still manages those orders (a stop
strategy in the template can move them later). The response reports the
price that was read back, never the one that was asked for.

**Threading.** `StartAtmStrategy` and `CloseStrategy` run on NinjaTrader's own UI dispatcher through
the core's bounded `Ui<T>` (5 s, then `504`). A `StrategyBase` built on a thread-pool thread would
take that thread's dispatcher, which never pumps — lesson 7 in `addon/NOTES.md`. Everything else
runs with every collection lock released. The module subscribes to no static event, so it has no
`Stop_Atm`.

---

## `GET /atm/templates`

One row per `*.xml` file in `<user data>\templates\AtmStrategy`.

```json
{"folder":"…\\templates\\AtmStrategy","folderExists":true,
 "templates":[{"name":"MyAtmTemplate","file":"…\\templates\\AtmStrategy\\MyAtmTemplate.xml",
               "nameInFile":"MyAtmTemplate","calculationMode":"Ticks",
               "brackets":[{"index":0,"quantity":1,"stopLoss":8,"target":16,"stopStrategy":null}],
               "unreadKeys":[],"error":null}],
 "error":null,"note":"…"}
```

`name` is the file's **base name**, and that is the string `POST /atm/start` takes. `nameInFile` is
the `<Name>` element when the file has one. The numbers are in the template's own
`calculationMode` (`Ticks`, `Price`, `Percent`, …) — **read that field before you read the
numbers**: `8` means nothing on its own.

The XML is read generically. NinjaTrader does not publish this schema, so a value that is not in the
file comes back `null` and never as a guess; when no `Bracket` element is found, `brackets` is
`null` and `unreadKeys` lists the element names that were there instead, so the shape can be fixed
without a second guess. A file that will not parse gets its own `error` and the other rows still
come back.

**`templates: null` means the folder could not be listed**, with the reason in `error`. That is not
the same answer as `[]`, and only one of them is safe to act on.

---

## `GET /atm/status`

`?account=<name>` restricts it to one account; without it, every valid account is listed.

```json
{"anyLive":false,"postsRefused":false,"account":null,
 "atms":[{"atmId":"1472","template":"MyAtmTemplate","account":"Sim101","instrument":"ES 12-26",
          "state":"Realtime","entryQuantity":1,"calculationMode":"Ticks","active":true,
          "position":{"side":"Long","quantity":1,"averagePrice":5000.25},"positionError":null,
          "entry":{"orderId":"o1","instrument":"ES 12-26","action":"Buy","type":"Market",
                   "state":"Filled","quantity":1,"filled":1,"limitPrice":null,"stopPrice":null,
                   "tif":"Day","oco":"","name":"NT8BridgeAtm","owner":"atm"},
          "brackets":[{"index":0,"quantity":1,"stopLoss":8,"target":16,"stopStrategy":null,
                       "stops":[{"orderId":"o2","…":"…"}],
                       "targets":[{"orderId":"o3","…":"…"}],"error":null}],
          "orders":[{"orderId":"o2","…":"…"},{"orderId":"o3","…":"…"}]}],
 "finished":3,"hiddenNonSimulator":2,"error":null,"note":"…"}
```

- Only ATM strategies with a **live order or an open position** are listed. `finished` counts the
  rest — `StrategyBase.All` also holds everything NinjaTrader loaded from its strategy database —
  and `hiddenNonSimulator` counts the ones on accounts this module refuses to touch at all. No
  account outside the Simulator / Playback set is ever named.
- Each order row is the same shape `/orders/status` uses, `owner` included.
- `brackets[].index` is what `POST /atm/change` wants in `targetIndex`.
- `orders` is every live order the ATM holds, whether or not it landed in a bracket list — the
  honest cross-check against `GetStopOrders` / `GetTargetOrders`, which NinjaTrader does not document.
- **`atms: null`** (with `error` set) means the ATM strategies could not be read, not that there are
  none.

This read is not behind `RefuseIfLive`: it routes nothing. It reports `anyLive` and `postsRefused`
instead, so an operator can see that every POST would be refused right now.

`StrategyBase.All` loads NinjaTrader's strategy database on its first access in a process, so the
first `/atm/status` after a restart can take noticeably longer than the ones after it. It is a read,
not behind a dispatcher, and it never blocks the listener.

---

## `POST /atm/start`

```json
{"account":"Sim101","instrument":"ES 12-26","action":"Buy","type":"Market","quantity":1,
 "template":"MyAtmTemplate","tif":"Day"}
```

| Field | Required | Notes |
|---|---|---|
| `account` | yes | one Simulator or Playback account, by name |
| `instrument` | yes | e.g. `"ES 12-26"` |
| `action` | yes | **`Buy` or `SellShort` only** — an ATM entry opens a position |
| `type` | yes | `Market`, `Limit`, `StopMarket`, `StopLimit` |
| `quantity` | yes | whole number ≥ 1, ≤ the quantity cap |
| `template` | yes | one plain name from `GET /atm/templates`, never a path |
| `limitPrice` / `stopPrice` | per `type` | exactly the prices the type needs; a price it ignores is a `400` |
| `tif` | no | `Day` (default) or `Gtc` |
| `confirm` / `issuedAt` | second call | echoed back unchanged from the dry run |

`template` becomes a file path, so it is checked at that boundary: one path segment, no separator,
no `..`, no invalid file-name character, at most 128 characters. A name that is not a saved template
is a `404`.

The confirmed answer:

```json
{"ok":true,"dryRun":false,"account":"Sim101","instrument":"ES 12-26","atmId":"1472",
 "template":"MyAtmTemplate",
 "entry":{"orderId":"o1","state":"Working","quantity":1,"filled":0,"averageFillPrice":null,
          "rejected":false,"ninjaTraderText":null,"readError":null},
 "brackets":[…],"exitsPending":true,"error":null,"plan":{…},"caps":{…},
 "auditLog":"…\\nt8mcp\\orders.jsonl","note":"…"}
```

- **`ok` means NinjaTrader accepted the call and took the entry order.** It never means filled, and
  it does **not** mean a stop or a target is at the broker.
- **`exitsPending: true` means the entry has not filled**, so the ATM has not armed its exits yet:
  **the position is not protected.** Re-read `GET /atm/status`.
- `state: null` (and `filled: null`) mean nothing could be read about the entry — not that nothing
  happened. `ok` is false then, and the order may well be live.
- **Keep `atmId`**: `/atm/close` and `/atm/change` take it. `atmId: null` means the ATM started but
  its id could not be read.
- `502` with `error` set is "the call failed"; the module still writes its audit line. The rate slot
  is only given back when the entry order was never created — once `StartAtmStrategy` has been
  called, nothing here can prove that no order left the building.

---

## `POST /atm/close`

```json
{"account":"Sim101","atmId":"1472"}
```

The plan lists the position that is about to be flattened and **every order id** that is about to be
cancelled. An ATM with no live order and no open position is refused with `409 nothingToClose`.
Nothing else on the account is touched and `Account.FlattenEverything()` is never called anywhere in
this repository.

```json
{"ok":true,"dryRun":false,"account":"Sim101","atmId":"1472","instrument":"ES 12-26",
 "positionBefore":{"side":"Long","quantity":1,"averagePrice":5000.25},
 "positionAfter":{"side":null,"quantity":0,"averagePrice":null},"positionAfterError":null,
 "ordersBefore":2,"ordersStillLive":[],"error":null,"plan":{…},"caps":{…},"note":"…"}
```

`positionAfter` and `ordersStillLive` are what the account was **observed** to hold about 1.2 s
after the call, not what was asked for. `positionAfter: null` means it could not be re-read, which
is **not** the same as flat. Use `/orders/cancel` on anything the ATM left behind.

---

## `POST /atm/change`

```json
{"account":"Sim101","atmId":"1472","stopPrice":4999.0,"targetPrice":5012.0,"targetIndex":0}
```

Give `stopPrice`, `targetPrice`, or both — at least one, or it is a `400`. `targetIndex` is the
**bracket**, 0-based (default `0`): a template with several brackets has one stop and one target in
each, and `GET /atm/status` shows every index. A bracket whose entry has not filled has no live stop
or target and is refused with that reason (`409 noStopOrder` / `409 noTargetOrder`).

`targetIndex` is checked against the ATM's **own** bracket count, and an ATM whose
`AtmStrategy.Brackets` cannot be read is `500 atmUnreadable` — not a call that proceeds with the
bound check skipped. With the count unknown there is nothing to check the index against, and letting
it through would make an unreadable read **widen** what a confirmed write may address, which is the
opposite of what every other read in this module does.

The plan names every order that is about to move, with its type, its state, its quantity and its
current prices under `from`, against `to`:

```json
{"account":"Sim101","atmId":"1472","template":"MyAtmTemplate","instrument":"ES 12-26",
 "targetIndex":0,
 "orders":[{"kind":"stop","orderId":"o2","type":"StopMarket","state":"Working","quantity":1,
            "from":{"limitPrice":null,"stopPrice":4998},"to":{"limitPrice":null,"stopPrice":4999}}]}
```

- A price the order's own type does not use is a `400`, not a silent no-op.
- For a **stop-limit** stop the limit price travels with the stop, keeping the offset the template
  chose: moving the stop and leaving the limit behind arms a stop that cannot fill.
- Each order is held for the duration of the call (`Ord_Hold`), so a `/orders/change` aimed at the
  same id gets `409 changeInFlight` rather than interleaving two plans on one `Order`.

```json
{"ok":true,"dryRun":false,"account":"Sim101","atmId":"1472","instrument":"ES 12-26",
 "orders":[{"kind":"stop","landed":true,"order":{"orderId":"o2","…":"…","owner":"atm"}}],
 "landed":1,"error":null,"plan":{…},"caps":{…},"note":"…"}
```

`ok` means NinjaTrader accepted the **call**. `landed` counts the orders that were read back at the
new price about 1.2 s later. Compare each `orders[].order` against `plan.orders[].to`: values that
still match `from` mean the change has not landed, or was rounded to the instrument's tick size.
**The ATM still owns these orders** and may put a price back.

---

## Status codes

| Code | When |
|---|---|
| 200 | a read, a dry run, or a confirmed call NinjaTrader accepted |
| 400 | `badRequest` — a missing or malformed field, a template name with a path in it, an action that does not open a position, nothing to change, a `targetIndex` past the last bracket |
| 403 | `orders module not armed`; `capQuantity` / `capWorkingOrders` / `capRate` / `capCeiling`; `backtestAccount`; `refusedNonSimulator` |
| 404 | `noSuchAccount`, `noSuchTemplate`, `noSuchAtm` |
| 409 | `refusedLive`; `ambiguousAccount`; `staleToken` / `confirmMismatch` / `confirmReplayed`; `nothingToClose`; `noStopOrder` / `noTargetOrder`; `changeInFlight` |
| 500 | `capUnreadable`, `atmUnreadable` (the ATM's legs, or its bracket list, could not be read), `templateUnreadable`, `auditFailed` — a state that cannot be read is not a state that can be acted on |
| 502 | the call reached NinjaTrader and failed; the audit line is written either way |
| 504 | the UI thread did not take `StartAtmStrategy` / `CloseStrategy` within 5 s (audited, then rethrown) |

---

## The audit log

The same file and the same shape as `/orders/*`: `<user data>\nt8mcp\orders.jsonl`, one JSON line
per armed call, refusals included, with the `intent` line written **before** a confirmed act and the
result line after it. The `endpoint` field is `/atm/start`, `/atm/close` or `/atm/change`, and the
plan carries the template name — which is the user's own text. See `docs/api/orders.md`, "The audit
log".

---

## How to verify ATM strategies on a Simulator account

`scripts/smoke.d/92-atm.sh` runs the **disarmed** half automatically: every `/atm/*` path answers
`403` and publishes nothing. The armed half is a deliberate manual trial.

1. Save at least one ATM template from NinjaTrader's ATM dialog (Chart Trader or the SuperDOM). Give
   it a small stop and target in ticks.
2. `GET /atm/templates` while disarmed → `403`. Arm with `type nul > "…\bin\Custom\AddOns\orders.enabled"`,
   then read it again: the template must be listed with its brackets and its `calculationMode`.
3. Connect **Playback** (or the Simulated Data Feed) so `anyLive` is false, and pick `Sim101` or
   `Playback101`.
4. `POST /atm/start` **without** `confirm`. Read the plan: the template name, `templateParams`, the
   caps. Then POST again with that exact `confirm` and `issuedAt`.
5. Check in NinjaTrader: the entry, and — once it fills — a stop and a target the ATM placed. Then
   `GET /atm/status`: `atmId`, `brackets[0].stops[]`, `brackets[0].targets[]`, the position.
6. Send the **same** `confirm` a second time → `409`, and no second ATM.
7. `POST /atm/change` with a new `stopPrice`, two steps. Confirm `landed` is 1 and the stop line
   moved on the chart. Wait a few seconds and re-read `/atm/status`: if the ATM put the price back,
   say so — that is the behaviour this module cannot read out of the decompile.
8. `POST /atm/close`, two steps. `positionAfter.side` must be `null` and `ordersStillLive` empty.
9. Point `/atm/start` at a non-Simulator account → `403`, and at the Backtest account → `403`.
10. **Delete `orders.enabled`** and confirm every `/atm/*` path is `403` again.
