# Strategies on a simulated account — `/strategy/*` (module `strategyrun`, `addon/NT8BridgeStrategyRun.cs`)

Covers `POST /strategy/start`, `POST /strategy/stop` and `GET /strategy/running`.
Same conventions as `API.md`: JSON, UTF-8, `{"error":"…"}` on 4xx/5xx, times local NT8
`yyyy-MM-ddTHH:mm:ss` except `issuedAt` and the audit log's `ts`, which are UTC.

This closes the loop the rest of the server builds up to: **write a strategy → compile it →
backtest it → run it on a Simulator or Playback account → read the fills back**.

**A strategy places its own orders.** That is the point of the verb, and it is why these endpoints
sit behind exactly the same gate chain as `/orders/submit` — the same arming file, the same
live-routing refusal, the same provider check, the same dry run, the same single-use confirm, the
same audit log. **There is no live switch of any kind**: this file never reads `ops.live`, never
creates a flag file, and has no code path that accepts a third provider.

**Nothing started here is hidden.** The strategy is added to NinjaTrader's own Control Center
**Strategies** grid through that grid's own add / enable / disable path, so the user sees the row
and can disable it by hand at any moment. When that path is not available on the running
NinjaTrader build, `POST /strategy/start` answers `501` and **starts nothing** — a runner that
could not show a row, or that could start a strategy it cannot stop, is deliberately not shipped.

| Method | Path | Returns |
|---|---|---|
| POST | `/strategy/start` | dry-run `{plan, confirm, issuedAt}`, or the result of a confirmed start |
| POST | `/strategy/stop` | the same two steps for one instance this module started. **It does not flatten** |
| GET | `/strategy/running` | the instances **this module** started, with state, position, working orders and realized PnL |

**MCP: three tools** — `nt_strategy_start`, `nt_strategy_stop`, `nt_strategy_runs`
(`server/nt8_mcp/tools_strategyrun.py`).

**Not the same as `GET /strategies/running`.** That one (module `workspace`) is read-only and lists
**every** row in the Control Center grid, whoever created it. `GET /strategy/running` lists only
what this module started, because only those can be stopped through it.

**What a refusal looks like to a model.** `nt8_mcp.app` collapses every non-2xx answer to
`{"error": "<the AddOn's sentence>"}`. The status code and the rest of the body — including the new
plan a `409` confirm mismatch returns — do **not** survive that passthrough. Re-run the dry run; do
not retry a confirm blindly.

---

## The gate chain

Every state change goes through **the one door** in `addon/NT8BridgeOrders.cs` and nothing lower:

```csharp
Ord_Guarded("/strategy/start", "strategy.start", body, ref status, Sr_Start)
Ord_Approve(gate, plan, planJson, detail, null)
```

so gates 1, 2, 3, 6, 7 and 9 are the order module's, unchanged and not re-implemented here:

1. **`orders.enabled`** beside the AddOn in `bin\Custom\AddOns`, stat-checked on every request,
   ignored when older than 24 h. Unarmed → `403 {"error":"orders module not armed"}` on all three
   paths, before the body is parsed. It is the **same** file the order tools use, on purpose: a
   strategy on a Simulator account places real simulated orders.
2. **`RefuseIfLive(..., force:false)`** on `start` and `stop` → `409` while any connection that can
   route orders is Connected. No override. `GET /strategy/running` is a read and reports `anyLive`
   instead of refusing.
3. **The account**: required, matched by name to **exactly one** `Account`, `Provider.Simulator` or
   `Provider.Playback` only, and the Backtest account refused by name.
4. **This module's own availability**: `StrategiesGrid.StrategyAdd`, `StrategyEnable` and
   `StrategyDisable` must all have resolved at `Start_StrategyRun`. Any one missing → `501` on
   `start` **and** on `stop`, and nothing is started. `GET /compat` carries the row
   `StrategyRun.canStart`.
5. **Validation**: the strategy type exists in this AddOn's assembly and resolves to exactly one;
   the instrument resolves; `barsPeriod` is present and sane; `daysToLoad` is 1…3650; every
   `inputs` key is a real `[NinjaScriptProperty]` input and its value coerces (a fractional number
   for an integral input is a `400`, never a silent rounding). NinjaTrader's own
   `StrategiesGrid.IsStrategyConfigurationValid` runs on the configured instance at **dry-run**
   time, so a configuration it rejects is refused before any token is issued.
6. **No `confirm` = dry run.** Nothing is sent, nothing is added to the grid.
7. **With `confirm`**: the plan is rebuilt from fresh state and the token re-computed over it. The
   signed string starts with `strategy.start|` / `strategy.stop|`, so an `/orders/*` or `nt_flatten`
   confirm can never authorise a strategy. It is **one-shot**: a start plan describes something
   that does not exist yet and reads the same before and after the act, so the consumed-token set
   is the only thing that stops a retry after a timeout from running the same strategy twice.
8. **The act** runs on the Control Center's own dispatcher, with no Cbi collection lock held, and
   the account is **read back off the instance** — see below.
9. **Audit**: one JSON line per armed call to `<UserDataDir>\nt8mcp\orders.jsonl` — the same log
   the order module writes, with `endpoint` `/strategy/start` or `/strategy/stop`. A confirmed call
   writes its `intent` line **before** it acts.

## How a strategy is started, and why it is done that way

The Control Center's grid owns this. `NinjaTrader.Gui.NinjaScript.StrategiesGrid` is a public type,
but the three members that matter are not public:

| Member | Visibility | Used for |
|---|---|---|
| `StrategyAdd(StrategyBase)` | `private static` | putting the row in the grid (and in NinjaTrader's strategy DB, which `StrategyBase.All` reads) |
| `StrategyEnable(StrategyBase, Window, StrategiesGridEntry)` | `private static` | starting it, exactly as the Enabled checkbox does |
| `StrategyDisable(StrategyBase)` | `private static` | stopping it |
| `StrategyRemove(StrategyBase)` | **`public static`** | taking the row out — a typed call, no reflection |
| `IsStrategyConfigurationValid(IEnumerable<StrategyBase>)` | `internal static` | the pre-flight at dry-run time |

The three non-public ones are resolved **once**, in `Start_StrategyRun`, through `Compat.Resolve`,
and reported in `GET /compat`. This is the same compromise the core makes for
`POST /chart/{id}/reload` and for the same reason: it is the path the Control Center itself uses,
and nothing is re-implemented beside it. **If any of them is missing, the feature is off** — there
is no second path that would start a strategy without a grid row.

Three facts shape the implementation, each paid for before:

- **The instance is constructed on the Control Center's dispatcher.** A `StrategyBase` built on a
  thread-pool thread carries a `Dispatcher` for a thread that never pumps messages, and
  `StrategyEnable` does `strategy.Dispatcher.Invoke` against it — which froze the Control Center
  permanently, the user's own checkbox included (`addon/NOTES.md` "Lessons from cli-nt-bridge" #7).
- **The enable is dispatched with `BeginInvoke`, not `Invoke`.** `StrategyEnable` can raise a modal
  dialog, and a modal blocks that dispatcher until a human dismisses it. `Ui<T>` waits for a call
  that has already *started*, so a bounded `Invoke` there would not be bounded at all. The request
  hands the work over, then polls the instance's own `State`, so a wedged Control Center costs the
  answer its `state` — never the caller's handle on the strategy. `GET /health.standingModal` names
  the box when there is one.
- **`enabled` is not running** (`addon/NOTES.md` lesson 20). The evidence is the instance's own
  `State` reaching `Realtime`, which is what every answer here reports and what `ok` is judged on.
- **Observed on NinjaTrader 8.1.8.2: `StrategyEnable` runs a clone, not the instance handed to
  `StrategyAdd`.** The specific object passed to `StrategyAdd` is discarded — it moves to
  `State.Finalized` — and a different instance, sharing the same strategy `Id`, is what actually
  runs in the grid. `Sr_Live(StrategyBase)` is the lookup this forces: it scans
  `StrategyBase.All` for another instance with the same `Id` whose `State` is not `Finalized`, and
  every read after the enable, plus the stop, follows that live instance instead of the one this
  module built. Skipping this lookup makes a running strategy read back as `Finalized`.
- **Observed on NinjaTrader 8.1.8.2: the instrument must be set before `StrategyEnable`, or the
  Control Center's own validation refuses with "Please select an instrument".** `Sr_Build` sets
  both `Instrument` and `InstrumentOrInstrumentList` (the property that validation actually reads)
  before the instance is ever added to the grid, so this endpoint never hits that refusal.

## The account is READ BACK, never echoed

`StrategyBase.Account` is a plain settable property
(`.ref\nt8src\core\NinjaTrader.NinjaScript\StrategyBase.cs:780`) with no guard on it. A strategy
that assigns it in `OnStateChange(State.Configure)` — user code, in exactly the population
`GET /strategies` lists — moves itself to any account in `Account.All`, and from that moment every
order it places lands there. The provider gate in front of this call would still have passed, and an
answer that reported the account it was **asked for** would be an assertion nothing measured.

So the account is read off the instance **twice**: after `StrategyAdd` and before the enable, and
again after the state poll settles. Both readings must equal the gated account **by reference and by
name**. A mismatch — or an account that cannot be read at all, which refuses exactly as an unreadable
provider does — dispatches `StrategyDisable`, answers `502` with `outcome: "accountMoved"`, and names
**both** accounts in the sentence and in the audit line. The first read-back refuses before the
strategy was ever enabled.

`accountObserved` carries that reading beside the requested `account`:

| Where | Fields |
|---|---|
| `POST /strategy/start` (confirmed) | `account` (requested), `accountObserved` (read back after the enable) |
| `POST /strategy/stop` plan and answer | `account`, `accountObserved`, and `ACCOUNTOBSERVED=` in the signed plan string |
| every row of `GET /strategy/running` | `account`, `accountObserved`, `accountMatches`, `accountError` |

On `/strategy/stop` this matters twice over: `positionLeftBehind` and `workingOrdersLeftBehind` are
read off the **requested** account, so when the two names differ, "flat, no working orders" is true
of that account and says nothing about what the strategy left on the other one.

## `POST /strategy/start`

```json
{"strategy":"SampleMACrossOver","account":"Sim101","instrument":"ES 12-26",
 "barsPeriod":{"type":"Minute","value":5},"inputs":{"Fast":10,"Slow":25},"daysToLoad":5}
```

| Field | Required | Meaning |
|---|---|---|
| `strategy` | yes | a type name from `GET /strategies`; it must live in this AddOn's assembly |
| `account` | yes | one Simulator or Playback account, resolved to exactly one |
| `instrument` | yes | e.g. `"ES 12-26"` |
| `barsPeriod` | yes | `{type, value, value2?, baseType?, baseValue?}`. `type` is a `BarsPeriodType` name or, for a custom bar type with no enum name, its number. `MarketDataType` is always `Last` |
| `inputs` | no | `[NinjaScriptProperty]` inputs by name; an unknown name is a `400` that lists the real ones |
| `daysToLoad` | no | 1…3650; absent keeps the strategy's own value |
| `confirm` / `issuedAt` | no | absent = dry run |

Dry run → `{dryRun:true, plan, confirm, issuedAt, expiresInSec, caps, flags, note}`. The `plan`
carries the account, the strategy, the instrument, the bars period, `daysToLoad` and **every**
input with the value the configured instance really reads back — not only the keys that were sent —
because the plan is built off a real instance that is then torn down.

Confirmed →

```json
{"ok":true,"dryRun":false,"id":"s1","account":"Sim101","accountObserved":"Sim101",
 "strategy":"SampleMACrossOver",
 "instrument":"ES 12-26","barsPeriod":{"type":"Minute","value":5,"...":"..."},"daysToLoad":5,
 "inputs":{"Fast":10,"Slow":25},"state":"Realtime","running":true,"inStrategiesGrid":true,
 "startedAt":"2026-09-19T21:14:07","plan":{...},"caps":{...},"auditLog":"...","note":"..."}
```

- `ok` = the row is in the Strategies grid **and** the enable was dispatched **and** the instance
  is readable and not terminated. It is `502` otherwise. **`ok` never means the strategy is
  trading.**
- `running` = `state == "Realtime"`. Enabling walks `Configure → DataLoaded → Historical →
  Realtime` asynchronously, so a strategy loading days of bars answers `"Historical"`; that is not
  a failure, it is still starting. Re-read `GET /strategy/running`.
- `state: null` means the instance could not be read, **not** that nothing happened. The `id` is
  handed back either way, so the strategy can still be stopped.
- `accountObserved` is the account the **instance** carries, read back after the enable. It is what
  the orders route to. A start whose instance moved itself is `502 accountMoved`, not a result.
- Keep the `id`. It is what `POST /strategy/stop` takes.

## `POST /strategy/stop`

```json
{"id":"s1","account":"Sim101"}
```

`account` is required as well as `id`: the one door resolves and provider-checks the account before
anything else runs, so every call names it, and an id belonging to another account is refused
(`409 accountMismatch`) rather than redirected.

**One stop at a time per run.** A confirmed stop takes a hold on the run id — the same `Ord_Hold`
table `/orders/change` and `/orders/cancel` use, keyed `strategy:<id>` — before its token is spent,
and releases it in a `finally`. `StoppedUtc` is only written at the very **end** of a stop, after the
disable and the remove have been dispatched, so without the hold two confirmed stops for one id
(a client retry, two callers) would both pass the "already stopped" test and both dispatch
`StrategyDisable` and `StrategiesGrid.StrategyRemove` against the same live instance. The second one
gets `409 stopInFlight` and keeps its confirm. A dry run is never held: it changes nothing.

**IT DOES NOT FLATTEN.** Disabling a strategy stops it **managing** its position; the position and
the working orders stay on the account with nobody minding them. Both are read before the act (into
the plan) and again after it (into the answer), for that account and instrument. Closing them is a
separate, separately approved `POST /orders/close`.

The row leaves the grid **only once the instance reports `Terminated`**. If it did not, the row is
deliberately left in the Control Center — a running strategy with no grid row would be invisible to
its own user — and `removeNote` says so, with `ok:false` and `502`.

```json
{"ok":true,"dryRun":false,"id":"s1","account":"Sim101","accountObserved":"Sim101",
 "strategy":"SampleMACrossOver",
 "instrument":"ES 12-26","stateBefore":"Realtime","state":"Terminated","removedFromGrid":true,
 "removeNote":"the row was removed from the Strategies grid","flattened":false,
 "positionLeftBehind":{"side":"Long","quantity":2,"averagePrice":5000.25},"positionError":null,
 "workingOrdersLeftBehind":["o7"],"workingOrdersError":null,"plan":{...},"note":"..."}
```

## `GET /strategy/running`

A read: gate 1 (armed) only. It reports `anyLive` and `canStart` instead of refusing, so an
operator can see that a start would be refused right now.

Each row: `id`, `strategy`, `account`, `accountObserved`, `accountMatches`, `accountError`,
`instrument`, `barsPeriod`, `inputs`, `startedAt`,
`stoppedAt`, `state`, `running`, `inStrategiesGrid`, `position` + `positionError`, `workingOrders`
(each with its `owner`: `module` / `strategy <name>` / `atm` / `manual`) + `workingOrdersError`,
`realizedPnL`, `realtimeTrades`, `performanceError`, `stopNote`.

- `state` is the evidence, never a grid checkbox. `null` means the instance could not be read.
- `account` is what the run was **started on**; `accountObserved` is what the instance carries
  **now**, read off it on every call. `accountMatches:false` means the strategy moved itself and its
  orders are going somewhere else — a state a start refuses, but one a running strategy can still
  reach later, which is why this is a live read and not a cached field.
- `position` and `workingOrders` are the **account's**, for that instrument — an order placed by
  hand on the same instrument is listed too, with its own `owner`.
- `realizedPnL` / `realtimeTrades` come off the instance's own
  `SystemPerformance.RealTimeTrades.TradesPerformance.Currency.CumProfit`; both are `null`, with
  `performanceError` set, until the strategy has one.

## Status codes

| Code | When |
|---|---|
| 200 | dry run; a start whose row is in the grid; a stop whose instance reported `Terminated` |
| 400 | unknown strategy, unknown instrument, missing or malformed `barsPeriod`, `daysToLoad` out of range, unknown input name, a value that will not coerce, a configuration NinjaTrader rejects |
| 403 | unarmed; a non-Simulator account; the Backtest account |
| 404 | `no strategy run '<id>'` |
| 409 | a live order-routing connection is up; ambiguous account or strategy name; stale, mismatched, already-used or wrong-verb confirm; the id belongs to another account; the run was already stopped; another stop for that id is in flight (`stopInFlight`) |
| 500 | the add or the dispatch failed; the audit line could not be written |
| 501 | this NinjaTrader build does not expose the Control Center's add / enable / disable path |
| 502 | the act ran but could not be verified (`state` unreadable or terminated on a start; not terminated on a stop); the instance's own account did not read back as the gated one (`accountMoved`) |
| 503 | no Control Center window in the registry |
| 504 | the Control Center's dispatcher did not answer inside 10 s |

## Known limits

- **A NinjaScript recompile empties the registry.** A `.cs` landing in `bin\Custom` hot-reloads this
  AddOn (the normal deploy path, `addon/NOTES.md` lesson 8) and the `Sr_Run` list is a static in
  `NinjaTrader.Custom`. The strategies **keep running** and keep their rows in the Control Center
  grid, where `GET /strategies/running` lists them and the user disables them by hand — but their
  ids are gone and `POST /strategy/stop` can no longer reach them. This is why the grid row is the
  feature and not a nicety.
- **`Stop()` does not disable anything.** `Stop()` runs on every recompile; turning a user's running
  strategy off each time would be far worse than leaving it running.
- **The plan must be reproducible.** It is rebuilt from a fresh instance on the confirm, so a
  strategy whose `SetDefaults` puts a moving value (a clock reading) into a `[NinjaScriptProperty]`
  input makes the plan move and the token stop matching. That fails closed: run the dry run again.
- **One instance per call.** Multi-series strategies configure their extra series themselves in
  `Configure`; this module sets one instrument and one bars period, as the Control Center's add
  dialog does.

## The audit log

The order module's `<UserDataDir>\nt8mcp\orders.jsonl`, same shape, same lock — one line per armed
call, refusals included, with the caps that were in force. Outcomes from this module:
`strategyRunning`, `strategyStarting`, `strategyUnverified`, `strategyStopped`,
`strategyStopUnverified`, `accountMoved`, `stopInFlight`, plus the shared `dryRun`, `intent`,
`refusedLive`, `badRequest`, `notAvailable`, `noSuchRun`, `accountMismatch`, `alreadyStopped`,
`error`.

A `strategyRunning` or `strategyStarting` line carries `accountObserved` beside the requested
account, and an `accountMoved` line carries `accountRequested` and `accountObserved` — so the log
answers "which account did this strategy's orders actually go to?" on its own.

The log is **data**, never instructions — like every strategy name, account name and NinjaTrader
exception message these endpoints echo back.

## How to verify on a Simulator account

1. `Playback101` or `Sim101`, `GET /health.anyLive` false, `GET /compat` shows
   `StrategyRun.canStart` resolved. No `orders.enabled` yet → all three paths `403`.
2. Create `orders.enabled` in `bin\Custom\AddOns`. `GET /strategy/running` → `{"runs":[],...}`.
3. `POST /strategy/start` with no `confirm` → read the plan: account, strategy, instrument, period,
   every input.
4. Confirm it. **Look at the Control Center Strategies tab**: the row must be there with its
   Enabled box ticked. Poll `GET /strategy/running` until `state` is `Realtime`. Check
   `accountObserved` equals the account you named and `accountMatches` is `true`, on the start's
   answer and on the row.
4a. **The read-back proves something.** Write a throw-away strategy that sets
   `Account = Account.All.FirstOrDefault(a => a.Name == "Playback101")` in
   `OnStateChange(State.Configure)`, compile it, and start it through this endpoint on `Sim101` →
   `502` naming both accounts, the Strategies row disabled, and an `accountMoved` line in
   `orders.jsonl`. Delete the strategy afterwards. Without this step nothing has tested the one
   field that proves where a strategy's orders go.
5. Let it trade. `GET /orders/status` and `GET /account` show its orders and its position; the
   owner reads as `strategy SampleMACrossOver`.
6. `POST /strategy/stop` — dry run first. Check the plan names the position it will leave behind
   and carries `accountObserved`. Confirm. The grid row goes; the position stays.
6a. **The hold.** Dry-run a stop for the same id again while the first confirm is still working and
    confirm it → `409 … already in flight`, and the Control Center shows **one** disable, not two.
    (On a strategy that terminates instantly this window is short; a strategy loading days of bars
    is the easy case to catch it on.)
7. `POST /orders/close` to flatten, then delete `orders.enabled`.
