# Changelog

## 1.3.0 — 2026-09-19

### Added

- **Order entry on Simulator and Playback accounts only**, as its own opt-in module
  (`addon/NT8BridgeOrders.cs`), disarmed by default: `GET /orders/status`, `POST /orders/submit`,
  `POST /orders/change`, `POST /orders/cancel`, and the tools `nt_order_submit`, `nt_order_change`,
  `nt_order_cancel`. Market, Limit, StopMarket and StopLimit; one account, one instrument, one
  order per call. Full contract: `docs/api/orders.md`.
- Its gates: the arming file `orders.enabled` (24 h, separate from `ops.enabled` in both
  directions); accounts judged by provider, never by name, with the Backtest account refused;
  refused while any order-routing connection to a real broker is up; dry run, then a signed,
  single-use, 30-second confirm over the exact plan; caps of 2 contracts per order, 5 working
  orders per account and 6 orders per minute, adjustable through `nt8mcp\orders.config.json` up to
  the code ceilings 10 / 20 / 30; change and cancel only for orders the module placed; an audit
  line for every armed call in `nt8mcp\orders.jsonl`, written before a confirmed action runs.
- **There is no live-account switch** in the order module: it never reads `ops.live`.
- `scripts/smoke.d/91-orders.sh`: disarmed-state checks only; it never arms and never submits.

### Changed

- README: "Why there is no order entry (yet)" is now "Order entry: Simulator only, off by
  default". 50 tools (47 + 3).

### Known limitations

- A NinjaScript reload clears the orders-per-minute count. The other two caps read live state.
- No brackets, ATM strategies or OCO orders.

## 1.2.0 — 2026-09-19

Merge of read-only and dev-loop capability from
[`eman007/cli-nt-bridge`](https://github.com/eman007/cli-nt-bridge) (MIT). See `NOTICE` for
attribution and `docs/api/*.md` for the full contract of each item below.

### Added

- `nt8` CLI entry point (`nt8 <tool> --k v ...`) over the existing MCP tool functions; bare `nt8`
  prints the tool list. Exit codes: `0` did it, `1` could not reach the AddOn, `2` refused/unknown
  tool.
- `GET /data/coverage` (`nt_data_coverage`): which days the local `tick`/`minute`/`day`/`replay`
  stores actually hold for an instrument, before running a backtest or a download against it.
- `POST /backtest` gains `fillResolution`, `fillResolutionType`/`Value`, `slippageTicks`,
  `commissionTemplate`, `includeCommission`, `fillLimitOnTouch`, `maxTrades`, each read back and
  echoed so a job document says what it actually ran with.
- `/health` hardening (`pid`, `assemblyBuiltUtc`, `standingModal`, …), `GET /ntstatus`
  (`nt_status`, is the running assembly newer than the newest `.cs` on disk), `GET /compat`
  (`nt_compat`, the reflection-resolution table for every member this repo binds by name).
- `GET /feedhealth` (`nt_feedhealth`): last-tick age per instrument, using
  `Instrument.HasSeenMarketData` to tell "never ticked" from "feed went dark" — no subscription
  is created.
- `GET /connections` (`nt_connections`): the union of configured and actually-running
  connections (including brokerage logins, which a configured-only read misses), each with a
  `dropClass` (`connected`/`user`/`inadvertent`/`failed`) computed from
  `ConnectionStatusUpdate`, not guessed from the account name.
- `nt_nrd_export`: offline `.nrd` (Market Replay) decoder to Parquet, with its integrity
  cross-check, truncated-file salvage and atomic commit intact. Needs the `parquet` extra
  (`numpy`, `pyarrow`).
- `POST /compile` (`nt_compile`) and `POST /compile?reload=1` (`nt_reload_assembly`, a separate
  tool on purpose, never a flag): compiles the whole `bin\Custom` tree through NinjaTrader's own
  Roslyn compiler with no NinjaScript Editor window open, returning structured
  `{file,line,column,code,message}` instead of a screenshot. The reload form refuses while a
  live order-routing connection is up; the tool exposes no way to override that. The F5 +
  Editor-window path (`nt_compile_f5`) is kept as the cold-start fallback.
- `GET /output` (event ring, `nt_output`) and `GET /nt-log` (`nt_log`): both read from an
  in-process ring fed by `Output.OutputEvent`/`Cbi.Log.LogEvent`, so they answer in milliseconds
  with the Output window closed or the UI thread wedged. The old window-scrape behaviour is kept
  as `nt_output_window`, a fallback for when the ring is empty.
- `nt_report`: equity curve, underwater drawdown and a trade-P&L histogram as a one-page PDF (or
  stats alone), computed from a finished backtest's own trade list. Needs the `report` extra
  (`matplotlib`).
- `GET /workspace` (`nt_workspace`): every window the AddOn can see, with each chart's
  indicators/strategies and their `State`, so a silently-disabled strategy is visible without
  opening the chart.
- `GET /strategies/running` (`nt_strategies_running`): reads the Control Center's Strategies grid
  (population a chart walk cannot see) by reflection, `?materialize=1` to force the tab into the
  visual tree and restore the user's tab afterwards.
- `GET /templates` and `nt_backtest(..., template=...)`: NinjaTrader's own saved strategy
  templates as defaults for a backtest, so instrument/dates/inputs cannot drift from what the GUI
  runs.
- `nt_windows` gains geometry (`hwnd`, position, size, minimized/maximized); an in-AddOn
  `POST /screenshot` (`nt_shot`, `nt_window_shot`) captures a window with `PrintWindow` without
  fronting or restoring it. `scripts/shot.ps1` is kept as the fallback for the rare window
  `PrintWindow` cannot read.
- `GET /executions` and `GET /performance` (`nt_executions`, `nt_performance`): real fills and
  round-trip performance for a live/Sim account, with a labelled commission reconstruction when
  NinjaTrader's own per-fill commission reads back 0.
- `nt_optimize` / `nt_walkforward`: a Python grid loop and date-sliced walk-forward over the
  existing, proven `/backtest` — zero new NinjaTrader surface, same numbers as a Strategy-Analyzer
  run.
- `POST /data/download` (opt-in): downloads Market Replay and historical tick/minute/day data on
  its own worker thread. Gated by an arming flag plus a real-data-connection precondition plus an
  exposure check (refuses while any account but Backtest has an open position or a working
  order).
- `GET /playback` (`nt_playback`, read side only): whether the Market Replay transport is
  connected, loaded, parked or running, and optional bounded coverage scanning.
- Opt-in, disarmed-by-default ops module (see "Ops module" in `README.md` and
  `docs/api/ops.md`): `nt_flatten` (the only order-touching MCP tool in this repository), plus
  two local, non-MCP processes (`python -m nt8_mcp.watch`, a naked-position watchdog with a
  quantity/side protective-stop test; `python -m nt8_mcp.connwatch` / `python -m nt8_mcp.restart`,
  a connection guardian and a restart CLI).
- Test suite: `fake_addon.py` fixture, `.cs` load-vs-compile fixtures (never installed into
  `bin\Custom`), a scripted-clock poll-loop test, and a CLI exit-path test per outcome.

### Changed

- `Ui<T>()` throws on a dispatcher timeout instead of silently returning `default(T)`, which used
  to serialize as an indistinguishable document of zeros and nulls.
- `nt_install`'s generated-region strip now anchors on the real `#region` marker with a regex and
  writes a `.bak` first, instead of truncating a file at the first substring match of a header
  comment.
- `/health.standingModal`: reports a blocking `MessageBox` by title instead of leaving every
  caller to time out guessing why the UI thread will not answer.
- Account/feed read quick wins: per-position `unrealized` P&L, `CancelSubmitted` counted as a
  working order, and a null-on-failure discipline (`SafeStr`/`SafeInt`/`SafeNum`) so a failed read
  degrades to `null`, never a misleading `0`.
- Bridge reload survival: the AddOn restarts itself on `State.Configure`, not only from a Control
  Center window event, with a bounded rebind retry and a reseeded window/chart registry, so a hot
  reload after installing a `.cs` file no longer leaves the bridge dead or serving stale statics.

### Fixed

- `GET /account?name=X` read failures now degrade to `null`, never a misleading `0`; a `404` is
  returned for an unknown account name instead of a bare `500`.
- `/backtest`'s `DateTimeKind` handling no longer silently collapses every intraday
  `/executions`/`/performance` pull to a ~3-day in-memory window.
- `POST /backtest`'s 202 body always echoes the literal `"state":"queued"` instead of re-reading
  job state, which could already have moved to `running`/`done` by the time the HTTP thread read
  it.
- `JGetStr`/`JGetBool`/`JGetInt` now throw a validation error (400) when a key is present with the
  wrong JSON type, instead of silently returning a default that could run a backtest with the
  wrong settings and report a plausible but wrong summary.
- Backtest job lifecycle races: terminal state transitions are compare-and-set so a cancel or
  timeout firing at the same moment as a normal finish can no longer relabel an already-settled
  result; `finishedAt` is now set before the state flip so a reader can never observe `state:done`
  with `finishedAt:null`.
- `GET /strategies` now tears down each probe instance instead of leaking one un-finalized
  strategy per call.

### Security

- Opt-in, disarmed-by-default ops module: every `/ops/*` endpoint answers `403` until an arming
  file is created, acts on Simulator accounts only unless a second file is present, runs as a dry
  run unless the caller returns a signed, time-boxed confirm string, and writes an audit log. See
  `docs/api/ops.md`.

### Known limitations / not included

- **No order entry.** A deliberate choice for 1.2: this tool feeds an assistant a lot of unvetted
  text (output, logs, third-party add-ons), and it runs inside the NinjaTrader desktop with access to
  every account. Planned: Simulator-only order entry as its own opt-in module, off by default, behind
  the same gates as the ops module. See "Why there is no order entry (yet)" in `README.md`.

- **No Strategy Analyzer automation (`/analyze`).** A real Optimize/WalkForward run driven
  through the Strategy Analyzer window is not implemented — it would overwrite the user's own
  Strategy Analyzer tab for numbers `nt_optimize`/`nt_walkforward` already produce over the
  `/backtest` path.
- **Playback is read-only.** There is no seek or speed control, and there will not be one:
  writing NinjaTrader's replay speed property *is* the play button, so a tool that could read the
  speed could also, by the same call, start the replay. `nt_playback` remains read-only.
- **No out-of-band staleness check in the Python client yet.** `nt_status` (`GET /ntstatus`) is
  self-referential — the code answering "is the assembly stale" is the code being asked about —
  so it cannot be its own interlock. The only out-of-band check today is the `startedAt`
  comparison inside the reload path; nothing yet independently reads `NinjaTrader.Custom.dll`'s
  mtime or the newest `.cs` on disk from outside the AddOn.
- `nt_reload_assembly` has no `force` argument to override its 409-while-live refusal.
- `nt_shot` has no `hwnd` argument (use `nt_window_shot(hwnd=...)` instead).
