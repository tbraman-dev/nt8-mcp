# Accounts module — `/account`, `/executions`, `/performance`

AddOn file `addon/NT8Bridge.Account.cs` (`Route_Account`; no start/stop hook — nothing reflective,
nothing subscribed). MCP tools `nt_account` (`server/nt8_mcp/tools_core.py`), `nt_executions` and
`nt_performance` (`server/nt8_mcp/tools_account.py`).

**Read only.** No endpoint here submits, modifies or cancels an order, opens or fronts a window, or
writes a file. `Execution.DbGet` is a `SELECT` against NinjaTrader's own trade database.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/account?name=Sim101` | one account, or every account as a list when `name` is absent |
| GET | `/executions?account=&from=&to=&instrument=&n=200` | raw fills, oldest first |
| GET | `/performance?account=&from=&to=&instrument=&n=5000` | round-trip trades + the status-document `summary` |

`Route_Account` returns `null` for every other method and path, including `POST /account`, so the
core emits the 404.

## Common query parameters (`/executions`, `/performance`)

| Query | Default | Meaning |
|---|---|---|
| `account` | — | **required**. A name from `GET /account`, matched case-insensitively. Unknown → `404 {"error":"no account 'X' — call GET /account for the list"}` |
| `from` | today `00:00` (NT8-local) | `YYYY-MM-DD` or any invariant-culture date |
| `to` | now | same; a date-only `to` becomes that day `23:59:59` (inclusive), as `POST /backtest` treats it |
| `instrument` | all | a full name, e.g. `ES 12-26`. Pushed into the database query (the 4-arg `Execution.DbGet` overload) |
| `n` | 200 / 5000 | max rows returned, hard ceiling 5000. More were found → `capped:true` |

An inverted range, or a year outside 1990..2090, is a `400` from the core's `RangeProblem`
before any query is armed. A `from`/`to` window wider than 366 days is also a `400` — the query
window, not just the response, is bounded: `Execution.DbGet` over a whole account history is
hundreds of MB of transient allocation inside NinjaTrader's own process. Page with `from`/`to`.

## Where the data comes from, and when it is thin

Both endpoints union two sources, deduped by `ExecutionId`:

1. **NinjaTrader's trade database** (`Execution.DbGet`), over a window that starts `from - 2 days`
   (`/performance` widens that pad when the account was not flat there — see `padDays` below).
2. **`Account.Executions`**, the in-memory list, which holds only the last
   `Account.LookbackDaysExecutions` days — reported as `lookbackDays`, never hard-coded.

`source` is `"db"` when the database answered and `"memory"` when it did not. **`"memory"` means the
answer is a ~3-day window whatever range was asked for**, and `warnings` carries the reason. A thin
result with `source:"db"` is thin because there were no fills; a thin result with `source:"memory"`
may not be. Check it.

`Execution.DbGet` **throws on a bound whose `DateTime.Kind` is `Local`** — the trade database is in
the exchange time zone. Both bounds go through `DateTime.SpecifyKind(x, Unspecified)`. Without that,
every request that omitted `to` (so `to` defaulted to `DateTime.Now`, which is `Local`) fell back to
the memory window silently. The live smoke check compares `source` with and without `to` to guard
against a regression here.

## `GET /executions`

```json
{"account":"Sim101","instrument":null,"from":"2026-09-18T00:00:00","to":"2026-09-18T16:30:00",
 "source":"db","lookbackDays":3,"total":2,"capped":false,"warnings":[],
 "executions":[
   {"id":812,"executionId":"abc-1","account":"Sim101","instrument":"ES 12-26","side":"Buy",
    "qty":1,"price":5811.5,"time":"2026-09-18T09:31:04","commission":0,"fee":0,"position":1,
    "orderId":"o-1","orderName":"Entry"}]}
```

| Key | Type | Notes |
|---|---|---|
| `id` | int | `Execution.Id` |
| `executionId` | string\|null | the exchange's id; the dedup key |
| `account`, `instrument`, `orderId`, `orderName` | string\|null | a read that throws is `null`, never `""` |
| `side` | string | `"Buy"` (MarketPosition Long) \| `"Sell"` (Short) \| `""` |
| `qty` | int | |
| `price` | number\|null | |
| `time` | string\|null | NT8-local, `yyyy-MM-ddTHH:mm:ss`, no zone |
| `commission` | number\|null | **0 for every row that came out of the database** — NinjaTrader never persists per-fill commission. Use `/performance` for a labelled reconstruction |
| `fee` | number\|null | `Execution.Fee` |
| `position` | int | `Execution.Position`: the account's position in that instrument **after** this fill, as the provider reported it (signed). Partial fills share one timestamp and come back in any order, so within one timestamp it is not monotonic. It is what `/performance` uses to check the account was flat at the start of its window |

Top-level keys alongside `executions`: `source`, `lookbackDays`, `capped`, `warnings`, and `total`
(int — rows in range before `n` was applied; not a key on the row objects above).

The 2-day pad exists to pair round trips and is filtered back out here: `/executions` reports only
fills at or after `from`. A row that could not be read at all becomes `{"error": "..."}` in place of
that one execution; it never removes the others.

## `GET /performance`

```json
{"account":"Sim101","instrument":null,"from":"...","to":"...","source":"db","lookbackDays":3,
 "executions":4,"padTrimmed":1,"padDays":2,"capped":false,"warnings":[],
 "summary":{ ... the 21 keys of Status document v1 ... },
 "trades":[{ ... the 14 keys of Status document v1, then commission, commissionSource, fee ... }],
 "commissionInfo":{"template":"Default","source":"template","total":8.36,
                   "tradesFromStored":0,"tradesFromTemplate":2,"tradesNoCommission":0,
                   "tradeCommissionTotal":0,"tradeFeeTotal":0,
                   "serverCommissionTotal":8.36,"serverFeeTotal":0}}
```

`summary` and `trades` are **the backtest status document's shapes, key for key and in order**
(`addon/NOTES.md`, "Status document v1"), produced by the same `SystemPerformance` /
`TradesPerformance` engine. That is deliberate: `nt_report` and `server/nt8_mcp/report.py` render a
live account and a backtest with the same code. `summary` is `null` only when NinjaTrader produced
no trade collection at all; an individual metric it could not produce is `null`, not `0`.

This object is named `summary` here (not `metrics`) so `report.py` reads both documents unchanged.

### `padTrimmed` and why metrics are computed twice

Pairing needs executions from before `from`, so the database query starts 2 days early. The trades
that pair out of that pad are then dropped by `Exit.Time`, and `padTrimmed` counts them. When
`padTrimmed > 0` the **legs of the surviving trades are re-fed to `SystemPerformance.Calculate`** and
`summary` + `trades` both come from that second result — so the pad never reaches the metrics and
`sum(trades[].pnl) == summary.netProfit` holds by construction **while `capped` is false**. When
`capped` is true, `trades` is truncated to `n` and `summary` still covers every trade in range, so
the sum is a subtotal, not the total. If the re-pair fails, `warnings` says the padded set was used
instead; it is never silent.

`executions` is how many fills went into the first pairing pass (padded), not how many trades came
back.

### `padDays` and the "not flat" warning — read `warnings` before the numbers

Pairing is only right when the account was **flat at the start of the padded window**. A position
opened before the pad is invisible, so its closing fills pair as *new entries* and every later trade
in that instrument shifts. A multi-day hold spanning the fixed 2-day pad can materially change both
the trade count and the net P&L this way, while `sum(trades[].pnl) == summary.netProfit` still holds
— the internal consistency check does not catch a mis-paired window.

So the AddOn checks it. For each instrument, position-before-first-fill =
`Execution.Position − signed quantity` (judged over the first *timestamp*, because partial fills
come back in any order). When it is not 0 the pad widens **2 → 7 → 30 → 120 days** and the query
runs again; `padDays` is the pad that was used. When it is still not flat at 120 days — or no fill
carries a `Position` at all — `warnings` says so and names the instrument:

```
not flat at the start of the 120-day pad (MES 09-26 was -3 before its first fill): a position opened
before it is invisible here, so its closing fills pair as new entries and later trades in that
instrument can be mis-paired
```

That warning is normal for an account whose first fill in NinjaTrader's database closed a position
opened elsewhere (another platform, or before this install). NinjaTrader's own Trade Performance
window has the same blind spot and says nothing. With the widening, every window tried reproduces
the pairing of the full-history query (`scripts/smoke.d/25-accounts.sh` check 8b).

`trades[].bars` is `null` for real fills: only a backtest fill carries a bar index (a real one has
`BarIndex -1`), and `summary.avgBarsInTrade` is NinjaTrader's own `0` for them.

### Observed behavior (NinjaTrader 8.1.8.2)

- `source:"db"` with **and** without `to`, on every account — the `DateTimeKind` regression is absent.
- The `from`/`to` bounds are NT8-local wall clock end to end (the database stores UTC; a `to` one
  minute after a known fill returns that fill).
- 4-arg `Execution.DbGet(Account, Instrument, …)` returns exactly the rows of the 3-arg call
  filtered by hand.
- `Account.LookbackDaysExecutions` is `3` at runtime.
- Over real executions `TradesPerformance` fills all 21 `summary` keys (sharpe, avgMae, avgMfe and
  both streaks included). An account with **no** trades reads `profitFactor:1`, `sharpe:1` — those
  are NinjaTrader's own values for an empty set, not ours.
- `commissionInfo.source` is a roll-up **of the trades**: with no trades it is `"none"` whatever
  `template` says, even when the account's commission template is set.
- **Still open:** whether `Trade.Commission` / `Trade.Fee` prorate per matched pair. This needs an
  account with both a commission template and closed round-trip trades to decide: give an account a
  commission template, make one round trip, then compare `commissionInfo.tradeCommissionTotal` with
  `commissionInfo.total`.

### Commission is often reconstructed, and always labelled

NinjaTrader does not persist per-fill commission; its own Trade Performance window recomputes it at
display time from the account's commission template. Per trade, in order of preference:

| `commissionSource` | Meaning |
|---|---|
| `stored` | the server stamped commission on the fills; `Entry.Commission + Exit.Commission` |
| `template` | reconstructed as `acct.Commission.GetWithMinimum(instrument, qty)` on both legs |
| `none` | no template and no stored value — **0 is the true answer**, normal on a funded/prop account |

`commissionInfo.source` is the roll-up (`mixed` when both appear). `commissionInfo.total` is the
reconstruction. **`summary.commission` stays NinjaTrader's own `TradesPerformance.TotalCommission`**
and is never overwritten with the reconstruction.

`tradeCommissionTotal` / `tradeFeeTotal` are the sums of `Trade.Commission` / `Trade.Fee` — public
on 8.1.8.2 and possibly already prorated per matched pair. They are reported next to the
reconstruction so one comparison on a real account decides whether the hand-rolled proration
cli-nt-bridge carried is needed at all (currently left open). `serverCommissionTotal`
/ `serverFeeTotal` are `Account.Get(AccountItem.Commission|Fee, denomination)` — the account's
running totals, a cross-check, not a per-range number.

`trades[].pnl` is `Trade.ProfitCurrency`, which is **net** of commission and fee when the provider
stamped them on the fills.

## `GET /account`

Unchanged keys, plus two per position:

```json
{"name":"Sim101","cash":100000,"realized":0,"unrealized":62.5,
 "positions":[{"instrument":"ES 12-26","qty":1,"avg":5811.5,"unrealized":62.5,"hasSeenMarketData":true}],
 "orders":[{"id":"o-1","instrument":"ES 12-26","action":"Buy","type":"Limit","qty":1,"price":5800,"state":"Working"}]}
```

| Change | Why |
|---|---|
| `positions[].unrealized` | `Position.GetUnrealizedProfitLoss(PerformanceUnit.Currency)`. The price argument defaults to "current market", so no market-data subscription is needed |
| `positions[].hasSeenMarketData` | `Instrument.HasSeenMarketData`. `unrealized` computed with no tick yet is not an error, but it is not a mark-to-market either. `null` if the read fails |
| `orders[]` now includes `CancelSubmitted` | a cancel-submitted order is still live at the broker and used to be invisible here |
| a failed read is `null` | never `0` and never `""`; one bad position or order becomes `{"error":"..."}` in its own row and the rest of the list survives |
| `name` matching | `OrdinalIgnoreCase`, deliberately: NinjaTrader account names are user-typed and case is not meaningful in them |
| unknown `name` | now **404** `{"error":"no account 'X' — call GET /account for the list"}`. It used to be a 500, which `API.md` never defined |

`Account.All` is snapshotted under its lock and the JSON is built outside it.

## Errors

| Status | When |
|---|---|
| 400 | `account` missing, a bad date, an unknown instrument, a range `RangeProblem` refuses |
| 404 | unknown account name |
| 500 | anything else, body `{"error": "<Deep(ex)>"}` — the innermost exception, named |
| 504 | NinjaTrader's UI thread did not answer while the account was being resolved (core-generated) |

## Threading

The **only** dispatcher hop is resolving the `Account` object (and, for `/account`, reading the
positions and orders). `Execution.DbGet` is a synchronous SQLite query and runs on the HTTP thread
after that hop returns — a wide range inside `Ui()` would block NinjaTrader's dispatcher for the
whole query. `Account.Executions` is snapshotted under `lock(acct.Executions)` and every JSON string
is built outside the lock.

## Live smoke

`scripts/smoke.d/25-accounts.sh`. It discovers an account name from `GET /account` and skips
(WARN, not FAIL) when there is none, when the account has no fills in range, or when the trade
database is unavailable — an empty account is a valid state, a broken contract is not.
