# Changelog

## 1.4.0 — 2026-09-20

### Added

- **Order module: brackets, close, reverse, and manage-any-order.** `nt_order_bracket` sends an
  entry plus a stop and any number of targets under one plan and one confirm; exits are sized to
  the entry's real fill, not the requested quantity. `nt_position_close` and `nt_position_reverse`
  cancel one instrument's working orders on one account, then flatten it (`reverse` also enters
  the same quantity the other way). `nt_order_change` and `nt_order_cancel` now reach **any**
  working order on a Simulator/Playback account — a running strategy's stop, an ATM's target, a
  hand-placed order — and the plan names its owner (`module` / `strategy <name>` / `atm` /
  `manual`). **One OCO pair per target**, because NinjaTrader cancels every other live order in an
  OCO group when one fills or is cancelled: cancelling a target also cancels its own paired stop,
  never a sibling pair's. Caps raised to 10 contracts per order / 20 working orders per account /
  60 submits per minute, with config-file ceilings of 100 / 100 / 600. Full contract:
  `docs/api/orders.md`.
- **ATM strategies on Sim (opt-in, Simulator only)**: `nt_atm_templates`, `nt_atm_status`,
  `nt_atm_start`, `nt_atm_close`, `nt_atm_change` — one entry order under a saved ATM template,
  with NinjaTrader arming and managing that template's stop and target. Same gate chain as the
  order module. Full contract: `docs/api/atm.md`.
- **Strategies on Sim (opt-in)**: `nt_strategy_start` adds a strategy to NinjaTrader's own Control
  Center Strategies grid, enabled, on a Simulator/Playback account, so the user sees the row and
  can disable it by hand; `nt_strategy_stop` disables it and reports the position and working
  orders left behind (it does not flatten); `nt_strategy_runs` reads state, position, working
  orders and realized P&L for what this server started. Full contract: `docs/api/strategyrun.md`.
- **Playback control and the replay bench (opt-in)**: `nt_playback_seek`, `nt_playback_speed` and
  `nt_playback_run` (a bounded run job: from/to/speed, always pauses at the end) drive the Market
  Replay clock, gated by the same `orders.enabled` file, a Connected Playback connection, no
  exposure on any non-Playback account, and no modal dialog. Proven: 3 replay hours in 55 s at
  200x with a strategy running on Playback101, 19 fills collected. Full contract:
  `docs/api/playback.md`.
- **`nt_reconcile`**: pairs a backtest's trades with a Market Replay run's real fills and reports
  matched pairs (with price/time deltas), fills only on one side, and a plain verdict. A backtest
  stamps a trade at the bar's close time and a replay fill at the real time, so up to one bar of
  time difference is normal (`tolerance_seconds`); a large price difference points at the
  historical and replay stores holding different data for that day, not a fill-model bug. Full
  contract: `docs/api/reconcile.md`.
- **Chart control**: `nt_chart_indicator_add` / `nt_chart_indicator_remove` add or remove an
  indicator on a chart (remove of one this module did not add needs `force`); `nt_chart_set_series`
  changes a chart's instrument and/or bar period (refused with an enabled strategy attached);
  `nt_chart_scroll_to` moves the visible window to a time; `nt_trade_shot` scrolls to a trade's
  entry and screenshots it. None of these touch an account or need an arming file. Full contract:
  `docs/api/chartcontrol.md`.

### Changed

- 79 tools (57 + 22).
- README/API.md: the order module's summary no longer says "it changes and cancels only the
  orders it placed itself" — it can now manage any working order on a Simulator/Playback account,
  and the caps/ceilings numbers are updated to 10/20/60 and 100/100/600.
- `docs/api/playback.md` "Status": seek, speed and the run driver are exercised on NinjaTrader
  8.1.8.2 (see Added, above).
- `docs/api/chartcontrol.md` "Status": indicator add/remove and scroll are exercised on NinjaTrader
  8.1.8.2. A series change can be undone exactly with `restore`.

### Fixed

- **A single-series backtest with High fill resolution ended as `error` in 1.3.1** although the run
  had happened: the check after the run counted the fill series NinjaTrader adds for High as a
  second data series. Multi-series + High is now refused BEFORE the run, on a throwaway instance
  that counts only the series the strategy itself adds, so NinjaTrader never shows its dialog;
  single-series + High runs.
- Every dry run on a Playback account says when the replay is paused (`replayWarning`): nothing
  fills there until the replay clock moves.

- **Rollover warning on backtests.** A backtest of a dated futures contract whose window starts
  before that contract became the front month now carries a `rollover:` warning with the date.
  Observed with a broker data feed: the minute history stored for the December contract on a day
  before the rollover held the same prices as the September contract's, a calendar spread away
  from the December prices a Market Replay recording of that day held.
- The "connect a data provider" warning compared times and not dates, so it fired on every full
  day (a session ends before midnight). It now fires only when whole days are missing.

### Known limitations

- An order that was created but never sent (for example after a failed ATM start on a build
  before this one) stays in NinjaTrader's account as `Initialized` / `CancelPending` until
  NinjaTrader restarts. The exposure guards count it as a working order on purpose; restart
  NinjaTrader to clear it.
- `nt_chart_set_series(restore=True)` forgets the original series after a NinjaScript reload.
- A resting bracket's exits do not survive a NinjaScript reload: the bracket watcher is a static
  that a hot reload discards, so a resting entry submitted before a reload gets no exits.
- A part-filled resting entry is protected only once it reaches a terminal state (`Filled`,
  `Cancelled` or `Rejected`); one that part-fills and keeps resting is not protected until then.
- Strategies started by `nt_strategy_start` are not re-adopted by id after a NinjaScript reload:
  they keep running and keep their Control Center grid row, but `nt_strategy_stop` can no longer
  reach them by the id this server returned — disable them by hand in the grid instead.

## 1.3.1 — 2026-09-19

### Fixed

- **A backtest that never ran can no longer report `done`.** A strategy that never started, no
  bars loaded, or a multi-series strategy asked for High fill resolution ends as `state:"error"`
  with the reason (NinjaTrader's own dialog text when there is one). A continuous-contract name
  is refused with a `400` before the job is queued.
- Backtest `output` now holds the lines the run printed (read from the output ring by cursor);
  `null` plus `outputNote` when they could not be captured, never a false `[]`.
- `sharpe` and `profitFactor` are `null` on fewer than two trades.
- **`nt_optimize` and `nt_walkforward` model costs**: `slippage_ticks`, `commission_template`,
  `include_commission`, `fill_resolution*`, `fill_limit_on_touch` go to every inner backtest and
  are echoed in `costs`. A failed inner run is an error row, never a zero-profit result.
- **`nt_data_download` works on a broker data feed.** The historical fetch now uses the bars
  request a backtest makes (merge policy `DoNotMerge`); the earlier request returned no bars.
  A failure names the route it used.

### Added

- `equity` on every backtest: cumulative net profit per closed trade, by exit time.
- `nt_walkforward` / `nt_optimize`: `include_trades=False` by default and a compact per-window
  `table`; the response schema is in `docs/api/optimize.md`.
- `nt_analyze`: breakdowns by month (exit time), weekday, hour, side, MAE / MFE, streaks,
  drawdown with start / trough / recovery, time under water.
- Run registry: every finished `nt_backtest` is saved with its request, costs, data window and a
  hash of the strategy source. `nt_runs`, `nt_run`, `nt_run_compare`.
- `nt_api_search` / `nt_api`: real NinjaScript signatures by reflection on the loaded assemblies.
- `nt_data_probe`: how far back the connected provider serves an instrument.
- `nt_data_coverage` reports NinjaTrader's bars cache for the instrument's contract chain.
- `nt_status` adds an out-of-band check from Python (disk dates and installed files against the
  repo), so a stale AddOn cannot vouch for itself.

### Changed

- `/data/download` needs no arming file any more (a download moves no money). It is still
  refused without a real data provider connected and while any account has a position or a
  working order. 57 tools (50 + 7).

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
