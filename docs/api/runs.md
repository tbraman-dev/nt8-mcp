# Run registry — `nt_runs`, `nt_run`, `nt_run_compare`

Module: `server/nt8_mcp/runs.py` (write side) + `server/nt8_mcp/tools_runs.py` (read side). Pure
Python — no AddOn endpoint, no HTTP call.

Every `nt_backtest` call that reaches a terminal state (`done`, `error`, `timeout`, `cancelled`) and
was not told `save_run=False` (see `docs/api/backtest.md`) is written to
`<NinjaTrader user data dir>\nt8mcp\runs\<utc-stamp>-<id>.json` — one file per run, named
`YYYYMMDDTHHMMSSZ-<backtest id>.json`. A result can always be traced back to the exact code and
settings that produced it, days later, without re-running anything.

## What is saved

| Field | Source |
|---|---|
| `savedAt` | the UTC stamp also in the filename |
| `id` | the backtest's own id (`"b1"`, …) — restarts per AddOn load, so the filename's timestamp is what makes the saved run unique, not this alone |
| `strategy`, `instrument`, `period` | off the finished result |
| `request` | the exact JSON body `nt_backtest` sent to `POST /backtest` |
| `inputs`, `settings` | the result's own `inputs` / `settings` — what actually ran, read back off the strategy, not merely what was asked for |
| `from`, `to`, `barsFrom`, `barsTo`, `warnings` | the requested vs. really-loaded window |
| `state`, `error` | the terminal state and, for `error`/`timeout`/`cancelled`, the reason |
| `summary`, `equity`, `trades` | the full summary object, the per-trade equity curve, and the trade list (see `docs/api/backtest.md`) — `nt_analyze(run_id=...)` needs `trades` for its per-trade breakdowns (by month/weekday/hour/side, MAE/MFE, streaks); `summary`/`equity` alone cannot answer those |
| `sourceHash` | sha256 of the strategy's `.cs` file under `bin\Custom\Strategies`, or `null` when this machine cannot find it (a base class in another folder, a file name that does not match the type name) — a missing fact, not an error |

A save failure (an unwritable directory, a full disk) never fails the backtest — `nt_backtest`'s
`warnings` gets `"run not saved: <reason>"` instead, and nothing is written.

## `nt_runs(limit=20, strategy="")`

Newest-first (by the filename's UTC stamp), one compact row per run: `id` (pass to `nt_run` /
`nt_run_compare` — the file's stem, `"<utc-stamp>-<backtest id>"`, not the bare backtest id, since
that resets every AddOn load and would collide across sessions), `savedAt`, `strategy`,
`instrument`, `period`, `from`, `to`, `state`, `trades`, `netProfit`. `strategy` filters by exact
name, case-insensitive. A file that fails to parse is skipped silently rather than raising — one
corrupt run must not break the whole list.

## `nt_run(id)`

The full saved record (the table above). `{"error": "no run '<id>'"}` for an unknown id.

## `nt_run_compare(a, b)`

```json
{
  "a": "20260919T220301Z-b12", "b": "20260919T221007Z-b13",
  "sourceHashMatches": true,
  "inputs": {"Fast": {"a": 10, "b": 15}},
  "settings": {},
  "window": {},
  "summary": {"netProfit": {"a": -212.5, "b": 340.0}, "trades": {"a": 76, "b": 81}}
}
```

Each of `inputs`, `settings`, `window` (`from`/`to`/`barsFrom`/`barsTo`), `summary` lists only the
keys that actually differ between the two runs, as `{"a": <value>, "b": <value>}` — an unchanged key
is simply absent, so an empty object means "identical on this dimension". `sourceHashMatches` is
`true` only when both runs have a non-null `sourceHash` and they are equal: it answers "was this the
same strategy code" without a consumer having to compare two 64-character hex strings by eye.
Either `a` or `b` not found is `{"error": "no run '<id>'"}` naming which one, never a partial diff.
