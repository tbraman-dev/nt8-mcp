# nt8-mcp

**Let Claude Code (or any MCP client) write, compile, chart-check and backtest your NinjaTrader 8
NinjaScript — by itself.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](server/pyproject.toml)
[![Latest release](https://img.shields.io/github/v/release/tbraman-dev/nt8-mcp)](https://github.com/tbraman-dev/nt8-mcp/releases)
[![MCP server](https://img.shields.io/badge/MCP-server-8A2BE2.svg)](https://modelcontextprotocol.io)

`nt8-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server plus a small
NinjaTrader 8 AddOn. Together they let an AI coding assistant do what you do when you develop
NinjaScript: write the code, compile it, put it on a chart, look at what it drew, read what it
printed, backtest it, and fix what is wrong — without you pasting screenshots and compiler errors
back and forth.

It is built for **developing indicators and strategies**, not for placing trades. It is read-only
by default. Not affiliated with NinjaTrader, LLC.

## Quick start

```
powershell -ExecutionPolicy Bypass -File scripts\install-addon.ps1   # then press F5 in the NinjaScript Editor once
pip install -e server
claude mcp add --scope user nt8 -- nt8-mcp
nt8 health                                                         # NT8 running -> AddOn + NT8 version, connections
```

Needs Windows, NinjaTrader 8 (a free Simulator install is enough) and Python 3.10+. Details:
[Requirements](#requirements) and [Install](#install).

<!-- demo gif: docs/demo.gif -->

## What can I ask it?

- *"Add a 20-period volume-weighted band to my indicator, compile it, and check the plot values on
  the ES chart match the math."*
- *"The NinjaScript build is red. Find the errors and fix them."*
- *"Backtest SampleMACrossOver on the first chart for last week with Tick Replay, 1 tick of
  slippage, and show me the trades by hour."*
- *"Run a walk-forward of my strategy's stop and target grid, 30 days in-sample, 10 days out."*
- *"Which days of NQ tick and minute data do I have locally? Download the missing minute days for
  last month."*
- *"What is the real signature of `Draw.Line`? Check before you write the call."*

## How it works

```text
+----------------------------------------------------------+
|  AI assistant  (Claude Code, or any MCP client)          |
+----------------------------------------------------------+
                 |  MCP
                 v
+----------------------------------------------------------+
|  nt8-mcp  (Python MCP server: 79 tools + the nt8 CLI)    |
+----------------------------------------------------------+
                 |  HTTP on localhost:7891
                 v
+----------------------------------------------------------+
|  NT8Bridge AddOn  (runs inside NinjaTrader 8)            |
|  charts, indicators, drawings, output and log,           |
|  compiler, backtests, data, accounts                     |
+----------------------------------------------------------+
```

## What you get

- **A real compile loop.** `nt_check` compiles your `.cs` files against your actual NinjaTrader
  Custom project in a scratch folder — nothing in NinjaTrader is touched, so a broken draft can never
  unload your indicators. `nt_compile` then compiles through NinjaTrader's own compiler, with **no
  NinjaScript Editor window open**, and returns structured errors: file, line, column, code,
  message. `nt_reload_assembly` swaps the new code in without waiting for NinjaTrader's file watcher.
- **Eyes on the chart.** Every open chart with instrument and bar type; every indicator with its
  inputs and the last *n* values of every plot; every drawing object with tag, type, owner and
  anchors; the last *n* bars. The assistant can check that the level your indicator drew is the
  level the math says it should be.
- **Everything your code printed.** `Print()` output and NinjaTrader's own log are captured as
  event rings inside the AddOn. They answer with the Output window closed, with a cursor so nothing
  is read twice, and they still answer when NinjaTrader's UI thread is stuck behind a dialog.
- **Headless backtests.** `nt_backtest` runs a strategy with no Strategy Analyzer window: summary
  metrics plus the full trade list, Tick Replay, custom bar types, slippage, commission templates,
  fill resolution, saved strategy templates. It runs on the Backtest account only. Each result
  reports the bars that were **really** loaded (`barsFrom`, `barsTo`, `warnings`), so a silently
  shortened data window cannot pass as a result.
- **Optimize, walk-forward, report.** A grid search and an anchored or rolling walk-forward over
  the same backtest engine, ranked by the fitness you pick, with a hard cap on combinations, and a
  one-page PDF report with equity curve and drawdown.
- **Screenshots that do not steal focus.** Any NinjaTrader window, captured in-process, even when
  another window covers it. Charts include the Direct2D render surface, not a black rectangle.
- **Know your data before you trust a result.** Which days your local tick, minute, day and
  Market Replay stores really hold; feed health per instrument (a feed that says Connected but has
  gone quiet); every connection with the reason it last dropped; an offline decoder that turns
  `.nrd` replay files into Parquet.
- **Real fills and performance.** Executions and round-trip performance for any account, with
  correct pairing across positions held for days.
- **A simulation bench.** Write a strategy, compile it, backtest it headlessly, then run it for
  real on a Simulator account or drive a Market Replay session through it, and check the replay's
  real fills against the backtest's with `nt_reconcile`. All opt-in, all off by default — see
  [Order module](#order-module-opt-in-simulator-only),
  [Strategies on Sim](#strategies-on-sim-opt-in) and
  [Playback control and the replay bench](#playback-control-and-the-replay-bench-opt-in).
- **A truth check on the tool itself.** `nt_health`, `nt_status` and `nt_compat` tell the assistant
  which build is running, if it is older than your source, and which NinjaTrader internals an
  upgrade broke — so it does not debug code that is not the code that is running.
- **A shell CLI.** Every tool is also a command: `nt8 health`, `nt8 bars --chart first --n 5`.

## Why this one, for development

The other NinjaTrader MCP servers are about trading: accounts, positions, orders. This one is
about the edit-compile-look-fix loop.

1. **The assistant can verify its own work.** It reads plot values, drawing anchors, printed
   output and compiler diagnostics as data. No "please send me a screenshot".
2. **It cannot break your platform while it drafts.** NinjaTrader compiles all custom code as one
   assembly, so one bad file unloads every indicator you have. `nt_check` catches the error
   offline first.
3. **It needs no open windows.** No Editor window to compile, no Output window to read prints, no
   Strategy Analyzer to backtest. It works on a second monitor, minimized, or while you trade.
4. **It tells the truth about state.** Reloads are proven by a new process start stamp, not by a
   file date. Backtests report the data they really used. A stale or half-loaded build says so.
5. **It is safe to leave connected.** Most of what an assistant reads here is text from charts,
   logs and third-party add-ons — and text an assistant reads must never be able to move a funded
   account. So no order can go to a live, funded or broker-demo account, and the two
   account-changing features are off by default, on disk, in every clone (see
   [Order entry: Simulator only, off by default](#order-entry-simulator-only-off-by-default) and the [Safety model](#safety-model)).
6. **It is MCP-native.** 79 typed tools with docstrings written for a model, grouped by module. No
   bespoke IPC layer, no prompt glue.
7. **It closes the loop.** Build, compile, backtest, run on Sim or in a replay, and compare the
   fills (the simulation bench above).

## How it compares

As of September 2026, from each project's own documentation. Check the projects themselves before
you decide; they move fast.

| | nt8-mcp | [eman007/cli-nt-bridge](https://github.com/eman007/cli-nt-bridge) | [ozmnf4/ninjatrader-mcp](https://github.com/ozmnf4/ninjatrader-mcp) | [anfs-pain/ninjatrader-mcp](https://github.com/anfs-pain/ninjatrader-mcp) | [Official NinjaTrader MCP](https://github.com/NT-NinjaTrader/mcp-skills) |
|---|---|---|---|---|---|
| Runs against | NT8 desktop (AddOn) | NT8 desktop (AddOn) | NT8 desktop (AddOn) or cloud | NT8 desktop (AddOn) | Tradovate cloud API |
| Orders / positions / account | read only by default; opt-in, disarmed flatten; opt-in, disarmed order entry (submit, bracket, change, cancel, close, reverse), ATM strategies and strategies-on-Sim on Simulator/Playback accounts only, with dry run + signed confirm ([why](#order-entry-simulator-only-off-by-default)) | yes (no confirm/dry-run gate) | yes | yes | yes |
| Chart list, symbol, period | yes | no (headless only) | symbol + period | yes | no |
| Indicator inputs + plot values (last n bars) | yes | no | current value only | current value only | no |
| Drawing objects (tag, type, owner, anchors) | yes | no | no | no | no |
| NinjaScript Output window / log text | yes, as event rings (no window needed) | yes (window scrape) | no | no | no |
| Compile check without touching NT8 | yes (`nt_check`) | no | no | no | no |
| Compile through NT8 with no Editor window open | yes (`nt_compile`) | yes | no | no | no |
| Reload NinjaScript on a chart | yes | no | no | yes | no |
| Chart / any-window screenshot | yes, in-process `PrintWindow` (no fronting) | window scrape | no | no | chart PNG |
| NT8 trace/log tail | yes | no | no | no | no |
| Headless backtest (summary + trade list, Tick Replay, custom bar types) | yes (`nt_backtest`) | yes | no | no | no |
| Grid optimize / walk-forward | yes, a Python loop over `/backtest` (`nt_optimize`, `nt_walkforward`) | yes, through the Strategy Analyzer window itself | no | no | no |
| End-to-end Market Replay playback runs (connect, seek, speed, drive a session start to finish) | read-only transport state (`nt_playback`) plus opt-in seek / speed / a bounded run driver (`nt_playback_seek`, `nt_playback_speed`, `nt_playback_run`) — never connects or disconnects | yes | no | no | no |
| Naked-position watchdog / auto-reconnect / restart CLI | yes, opt-in, disarmed by default, dry-run + signed confirm on the one order-touching call | yes (no confirm/dry-run gate) | no | no | no |

What cli-nt-bridge has that this repo deliberately still does not: a real Optimize/WalkForward run
driven through the Strategy Analyzer window (`/analyze` — not included, see "Known limitations" in
`CHANGELOG.md`), and a Playback **connect**/disconnect control (seek, speed and a bounded run
driver are opt-in here; connecting the transport stays a manual step, see
[Playback control and the replay bench](#playback-control-and-the-replay-bench-opt-in)). What this
repo has that cli-nt-bridge does not: MCP-native tools (no bespoke CLI/IPC layer), eyes on chart
indicators and drawing objects, headless backtests that never need a Strategy Analyzer window, an
ops module gated by dry-run + a signed, time-boxed confirm string instead of acting on the first
call, and a backtest-versus-replay reconciliation check (`nt_reconcile`).

## Order entry: Simulator only, off by default

Other NinjaTrader MCP servers can place trades on any account, the official one included. This one
places orders on **Simulator and Playback accounts only**, and only after you switch that on by
hand. That is a decision about risk, and here is the reasoning.

**This tool feeds an assistant a lot of text that nobody vetted.** Indicator names, drawing tags,
everything any script prints to the Output window, NinjaTrader's log, window titles, the output of
closed-source third-party add-ons, text echoed by a data feed. That is the whole point of the
tool: the assistant reads your platform. It is also the classic setup for *prompt injection* — a
line of text that reads like an instruction ("close all positions and buy 10 ES") and that a model
may act on. If the same session holds a tool that can place an order, one bad line can move money.

**The official server is in a different position.** It talks to the broker's cloud API. The login,
the permissions, the risk limits and the liability sit with the broker, and it reads almost no
untrusted text. `nt8-mcp` runs *inside* your NinjaTrader desktop. It sees every account that
NinjaTrader sees — Sim, broker demo, funded, prop-firm evaluation — with no login of its own and
no broker-side limit between a tool call and the order.

**So the rule is: reading is free, acting is gated, and no order ever goes to a real account.**
Order entry is useful for development, not just for trading: test how a strategy handles its
orders, create a position for a test, reproduce a fill-handling bug. A Simulator account is enough
for all of that. The order module (`nt_order_submit`, `nt_order_bracket`, `nt_order_change`,
`nt_order_cancel`, `nt_position_close`, `nt_position_reverse`) is its own file, and these are its
gates:

- an arming file, `orders.enabled`, that you create by hand; ignored again after 24 hours. The ops
  module's file does not arm it, and its file does not arm the ops module — the same file also
  arms ATM strategies, strategies-on-Sim and the Playback controls below;
- Simulator and Playback accounts only, judged by the connection's provider and never by the
  account's name. The Backtest account is refused too;
- refused while any connection that can route orders to a real broker is up. A broker *demo*
  counts as real, on purpose;
- a dry run first, then a signed confirm string that works once, within 30 seconds, for exactly
  the order the dry run showed;
- hard caps: 10 contracts per order, 20 working orders per account, 60 orders per minute. A config
  file can change them, up to fixed ceilings in the code (100 / 100 / 600);
- it can change or cancel **any** working order on the gated account, not only the ones it placed
  itself — the plan names the order's owner before you confirm, so you see what you are about to
  take away;
- every armed call, refused or not, is written to an audit log, before the order goes out.

**There is no live-account switch in that module, and there is no plan for one.** The ops module
has a second file that widens it to other accounts, because a flatten can only reduce risk. An
order can add risk, so the order module has no such file and no code path for one. If you need an
assistant that trades a real account,
[`ozmnf4/ninjatrader-mcp`](https://github.com/ozmnf4/ninjatrader-mcp),
[`anfs-pain/ninjatrader-mcp`](https://github.com/anfs-pain/ninjatrader-mcp) and the
[official server](https://github.com/NT-NinjaTrader/mcp-skills) do that. Think about what text that
assistant can read in the same session before you connect it to funded money.

## Requirements

- Windows with **NinjaTrader 8** desktop (developed and tested on 8.1.8.2). A free Simulator
  install is enough; no broker account or data subscription is needed for the development tools.
- **Python 3.10+**.
- The **.NET SDK** (`dotnet` on the path) for the offline compile check `nt_check`. Everything
  else works without it.
- An MCP client. The examples use Claude Code; any client that runs a stdio MCP server works.

## Install

The [Quick start](#quick-start) commands, step by step:

1. Install the AddOn with `scripts\install-addon.ps1`, then press F5 in the
   NinjaScript Editor (or let `nt_compile` do it once the MCP server is running). This copies every
   `addon/NT8Bridge*.cs` into `%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\`.
2. Install the MCP server from the `server` folder (`pip install -e .`).
   Optional extras add two tools' dependencies — `pip install -e ".[parquet,report]"`: `parquet`
   (`numpy`, `pyarrow`) for `nt_nrd_export`, `report` (`matplotlib`) for `nt_report`'s PDF. Both
   tools work without them and degrade with a clear error naming the missing package.
3. Start NT8. The AddOn listens on `http://localhost:7891` once it's compiled in.
4. Register the server with Claude Code. Project scope, in a `.mcp.json` next to your code:

   ```json
   {"mcpServers":{"nt8":{"command":"nt8-mcp"}}}
   ```

   or for every project, the `claude mcp add --scope user` line from the Quick start.
   Claude Code asks once to enable a project `.mcp.json` server; answer yes. The `nt_*` tools then load in the next session.

Override the AddOn's address with the `NT8BRIDGE_URL` environment variable if it's not on the default port.

A cloud-sync tool that mirrors `bin\Custom` (Google Drive, OneDrive, Dropbox, …) can drop a
duplicate file there — Explorer's `<name> (1).cs` pattern — the moment two devices touch the same
file. NinjaTrader compiles the **whole** tree as one assembly, so one such duplicate turns the
entire NinjaScript build red, unrelated to anything this repo does. If a compile that was passing
suddenly fails with hundreds of errors in files you did not touch, look for a stray `(1).cs` first.

Check that it works:

```
nt8 health
```

It prints the AddOn version, the NinjaTrader version, your connections and the number of open
charts. `connection refused` means NinjaTrader is not running or the AddOn is not compiled in yet.

## A typical session

You ask: *"Add a 20-period volume-weighted band to my indicator and make sure it plots where it
should."* The assistant can then run the whole loop by itself:

| Step | Tool | What it learns |
|---|---|---|
| 1. Draft the change | (its own editor) | |
| 2. Compile offline | `nt_check(files)` | `CS0103` at line 88, fix, repeat — NinjaTrader never saw the broken draft |
| 3. Install and build | `nt_install(files)`, `nt_compile()` | zero errors from NinjaTrader's own compiler |
| 4. Load the new code | `nt_reload_assembly()`, `nt_chart_reload(chart)` | `nt_status()` confirms the running build is current |
| 5. Look at the result | `nt_indicators(chart, name, n=50)`, `nt_bars(chart, n=50)` | the plot values, next to the bars they were computed from |
| 6. Read the debug prints | `nt_output(contains="band")` | what the code printed, Output window open or not |
| 7. See it | `nt_screenshot(chart)` | the chart as you would see it |
| 8. For a strategy: test it | `nt_backtest(...)`, `nt_optimize(...)`, `nt_report(id)` | trades, metrics, the bars really used, a PDF |

Every step returns data the assistant can reason about, so it can find its own mistake and go
around the loop again before it tells you it is done.

## Tools

AddOn passthroughs (need NT8 open with the AddOn compiled in), grouped by the module that owns them:

### Chart, window and account reads (core)

| Tool | Returns |
|---|---|
| `nt_health` | AddOn version, NT8 version, connections, chart count, `standingModal`, `anyLive` |
| `nt_windows` | Open NT8 windows: title, kind, and geometry (hwnd, position, size, minimized/maximized) |
| `nt_charts` | Open charts (id, instrument, period, indicators) |
| `nt_chart_state(chart="first")` | Full state of one chart |
| `nt_bars(chart, n=50)` | Last n OHLCV bars |
| `nt_indicators(chart, name="", n=1)` | Indicators, their inputs, last n plot values |
| `nt_drawings(chart)` | Drawing objects on the chart |
| `nt_chart_reload(chart)` | Reload NinjaScript on the chart (not the assembly — that's `nt_reload_assembly`) |
| `nt_account(name="")` | Read-only account state (cash, P&L, positions, orders); no name = all accounts |
| `nt_bridge_log(n=100)` | Tail of the AddOn's own request/error log |
| `nt_screenshot(chart="first")` | PNG of the chart window |
| `nt_status()` | Is the running assembly newer than the newest `.cs` on disk (`GET /ntstatus`), plus an out-of-band check from Python: the newest `.cs` date on disk against the build time, and the installed AddOn files against the repo's. It reports a `disagreement` when the AddOn says it is fresh and the disk says it is not, so a stale AddOn cannot vouch for itself |
| `nt_compat()` | The reflection-resolution table for every NT8 member this repo binds by name (`GET /compat`) — what an NT8 upgrade broke |

### Chart control

| Tool | Returns |
|---|---|
| `nt_chart_indicator_add(indicator, chart="first", inputs=None, panel=None)` | Add an indicator to a chart; returns the chart report (instrument, period, visible range, indicators read back off the chart) |
| `nt_chart_indicator_remove(chart="first", indicator=None, index=None, force=False)` | Remove an indicator by name or index; only one this tool added, unless `force=True` |
| `nt_chart_set_series(chart="first", instrument=None, bars_period=None)` | Change a chart's instrument and/or bar period; refused with an enabled strategy attached |
| `nt_chart_scroll_to(time, chart="first")` | Scroll a chart's visible window to a time, keeping its width |
| `nt_trade_shot(run_id, trade_index, chart="first")` | Scroll to one trade's entry time, then screenshot it |

None of these touch an account or need an arming file. `nt_chart_indicator_add` and
`nt_chart_indicator_remove` are proven on a live NinjaTrader 8.1.8.2 install; so is
`nt_chart_scroll_to`. `nt_chart_set_series(restore=True)` puts back exactly what the chart showed before. Full contract:
`docs/api/chartcontrol.md`.

### Compile / reload

| Tool | Returns |
|---|---|
| `nt_compile(timeout_s=120)` | Compiles the whole `bin\Custom` tree through NinjaTrader's own compiler with no Editor window open; structured `{file,line,column,code,message}` |
| `nt_reload_assembly(timeout_s=240)` | Compiles for real **and** swaps the running assembly (`POST /compile?reload=1`) so new/changed types are available immediately instead of waiting for NT8's own 20-150 s folder watcher. Disruptive; separate tool on purpose, never a flag on `nt_compile`. Refuses while a live order-routing connection is up; the tool cannot override that (no `force` argument is exposed) |
| `nt_compile_f5(timeout_s=240)` | Cold-start fallback: press F5 in the NinjaScript Editor and wait for `NinjaTrader.Custom.dll` to be rebuilt. Kept as a fallback even after `nt_compile`/`nt_reload_assembly` landed |

### Output / log

Event rings: they answer even with the Output window closed or the UI thread wedged.

| Tool | Returns |
|---|---|
| `nt_output(since=-1, n=200, tab=0, contains="")` | NinjaScript `Print()` output from the AddOn's ring buffer, whether or not the Output window is open |
| `nt_output_window(n=200)` | Fallback: lines scraped from the NinjaScript Output window itself. Needs the window open; prefer `nt_output` |
| `nt_log(since=-1, n=200, level="", name="", contains="")` | NinjaTrader's own log (connection/order/execution/strategy/system events) from the AddOn's ring |

### Backtests

| Tool | Returns |
|---|---|
| `nt_strategies()` | Strategies available to backtest (name, full type name, inputs with defaults) |
| `nt_templates(strategy)` | Saved NinjaTrader strategy templates for one strategy, for `nt_backtest(template=...)` |
| `nt_backtest(strategy, chart="first", from_date="", to_date="", tick_replay=True, inputs=None, instrument="", bars_period=None, template="", fill_resolution="", slippage_ticks=0, commission_template="", max_trades=0, wait_s=600)` | Runs a backtest and waits for it, returning the final status doc (summary + trades) |
| `nt_backtest_status(id)` | Status of one backtest (state, and once done, summary + trades) |
| `nt_backtests()` | List all backtests (id, strategy, instrument, period, state) |
| `nt_backtest_cancel(id)` | Cancel a running backtest |

`nt_backtest` runs a strategy through the AddOn's headless Strategy Analyzer pass, on the
**Backtest account only** — no Sim or live account is ever touched. Strategies that read the tape in
`OnMarketData` need `tick_replay=True` (Tick Replay). `chart="first"` copies instrument and bar type from
the first open chart; pass `instrument`/`bars_period` to override or skip that. It polls until the backtest
finishes or `wait_s` elapses (use `nt_backtest_status`/`nt_backtests`/`nt_backtest_cancel` for a run still
going).

**A run that did not happen cannot report `done`.** A strategy that never started, an instrument
NinjaTrader cannot backtest, or a multi-series strategy asked for High fill resolution ends as
`state:"error"` (or a `400`) with the reason, NinjaTrader's own dialog text included when there is
one. `barsFrom` / `barsTo` are the window that really loaded. `output` holds the lines the run
printed, or `null` plus `outputNote` when they could not be captured, never a false `[]`. `equity`
is the cumulative net profit per closed trade, by exit time. Ratios that mean nothing on fewer
than two trades are `null`. If a zero-trade result surprises you, run `SampleMACrossOver` on the
same instrument and window first: it tells a data problem from a strategy problem in one call.

Example:

```python
nt_backtest("SampleMACrossOver", chart="first", from_date="2026-09-15", to_date="2026-09-17")
```

### Optimize / walk-forward / report

A Python grid loop and date-sliced walk-forward over the same proven `/backtest`, adding zero new
NinjaTrader surface — measured against a real Strategy Analyzer run, this produces the same
numbers.

| Tool | Returns |
|---|---|
| `nt_optimize(strategy, ..., params, fitness="MaxNetProfit", top_n=10, min_trades=5, max_combos=200)` | Grid search over `/backtest`; refuses above `max_combos` naming the count instead of running |
| `nt_walkforward(strategy, ..., optimization_period_days, test_period_days, anchored=False, include_trades=False)` | In-sample optimize + one out-of-sample run per window; always a compact per-window `table` |
| `nt_analyze(run_id="", trades=None)` | Breakdowns of a saved run or a trade list: by month (exit time), weekday, hour, long vs short, MAE / MFE, streaks, drawdown with start / trough / recovery, time under water |
| `nt_runs(limit=20, strategy="")`, `nt_run(id)`, `nt_run_compare(a, b)` | The run registry: every finished `nt_backtest` is saved with its request, costs, data window and a hash of the strategy source, so a result can be traced to the code that made it |
| `nt_report(id="", status_doc=None, pdf_path="")` | Stats (equity curve, drawdown) and optionally a one-page PDF for a finished backtest |

**Costs go all the way down.** `nt_optimize` and `nt_walkforward` take `slippage_ticks`,
`commission_template`, `include_commission`, `fill_resolution*` and `fill_limit_on_touch`, pass them to
every inner backtest, and echo them in `costs`. A run with none set says
`gross: no slippage or commission modelled`. An inner run that failed is an error row; it is never
ranked as a zero-profit result.

### NinjaScript API lookup

| Tool | Returns |
|---|---|
| `nt_api_search(query, limit=30)` | Types and members in the loaded NinjaTrader assemblies that match a keyword |
| `nt_api(type_name, member="")` | The real signatures of one type: overloads, parameter names and types, properties, enum values |

Read-only reflection on what NinjaTrader has loaded: nothing is created, nothing is called. Use it
before you write NinjaScript against a member you are not sure of; it is cheaper than a failed
compile.

### Data store

| Tool | Returns |
|---|---|
| `nt_data_coverage(instrument, kind="", from_date="", to_date="")` | Which days the local tick/minute/day/replay stores hold for an instrument, plus what NinjaTrader's bars cache holds for its contract chain (the data a backtest can use with no provider connected) |
| `nt_data_probe(instrument, kind="minute")` | How far back the connected data provider serves this instrument, found with a few small bounded requests; it reports only days it saw bars for |
| `nt_nrd_export(instrument_glob, out_dir, levels=["L1","L2"], force=False)` | Offline decode of `.nrd` Market Replay files to Parquet — no NinjaTrader involvement |
| `nt_data_download(instrument, from_date, to_date, kinds=["replay"], types=["Last","Bid","Ask"], overwrite=False, big=False)` | Fills missing historical data (tick / minute / day, or Market Replay) from the connected data provider into NT8's own store. Ranges wider than 10 days need `big=True`. See below |
| `nt_data_download_status(id)` | Status of one download job |
| `nt_data_download_cancel(id)` | Cancel a queued or running download |

`nt_data_download` writes into the same store every open chart, SuperDOM and running strategy
reads, and spends the data provider's bandwidth. It needs no arming file: a download moves no
money. It is refused unless a real (non-Simulator/Playback) data connection is up, and while any
account other than the Backtest account has an open position or a working order. It works with
a broker data feed, not only with NinjaTrader's own data service: the fetch uses the same bars
request a backtest makes. A backtest also fetches bars it lacks from the connected provider on
demand, so for a one-off test you may not need a download at all; `nt_data_probe` tells you how
far back the provider goes.

To use the downloaded tick and minute data in Python, export it offline to Parquet or CSV with
[`ninjatrader-to-parquet`](https://github.com/tbraman-dev/ninjatrader-to-parquet).

### Feeds and connections

| Tool | Returns |
|---|---|
| `nt_feedhealth(instruments)` | Last-tick age per instrument, without subscribing to anything — catches a feed that reports Connected but has gone quiet |
| `nt_connections(n=20, since=-1)` | Every connection NinjaTrader has configured or running (including brokerage logins), each with why it last dropped (`connected`/`user`/`inadvertent`/`failed`) |

### Real-account executions and performance

| Tool | Returns |
|---|---|
| `nt_executions(account, from_date="", to_date="", instrument="", n=200)` | Real order fills on one account, oldest first |
| `nt_performance(account, from_date="", to_date="", instrument="", n=5000)` | Round-trip trades and performance metrics for real fills, with commission labelled by source (per-fill vs. reconstructed from the account's template) |

### Workspace and Control Center

| Tool | Returns |
|---|---|
| `nt_workspace()` | Every window the AddOn can see, with each chart's indicators/strategies and their `State` |
| `nt_strategies_running(materialize=False)` | The Control Center's Strategies grid — a strategy population no chart walk can see. `materialize=True` forces the tab into view and restores the user's tab afterwards |
| `nt_window_shot(window="", chart="", hwnd=0, path="")` | PNG of any NT8 window, captured in-process with `PrintWindow` — never fronts or restores it |

### Playback

| Tool | Returns |
|---|---|
| `nt_playback(instrument="", coverage=False, budget_s=20)` | Is the Market Replay transport connected, loaded, parked or running; optional bounded coverage scan of the replay store |

The read side above never connects, disconnects, seeks or changes the replay speed. Seek, speed
and a bounded run driver exist too, opt-in behind the order module's arming file — see
[Playback control and the replay bench](#playback-control-and-the-replay-bench-opt-in). The
Playback connection itself is still a manual step: none of these tools ever connect or disconnect
it.

### Local tools (no AddOn needed)

These touch the filesystem and NT8's own windows directly.

| Tool | Returns |
|---|---|
| `nt_check(files)` | Compile-check files against the real Custom project in a scratch dir; nothing in NT8 is touched |
| `nt_install(files)` | Copy `.cs` files into the right Custom subfolder, handling NT8's stale-generated-region quirk |
| `nt_install_addon()` | Install the whole NT8Bridge AddOn: delete orphan `NT8Bridge*.cs`, copy every current one in |
| `nt_shot(title="", out="")` | Screenshot any top-level window by title substring (or the whole screen), via `PrintWindow` |
| `nt_trace(n=100)` | Tail of NT8's newest trace file and newest log file |

`nt_shot` and `nt_window_shot` both capture with `PrintWindow` and never front or restore the
target. `scripts/shot.ps1` (front-and-restore) is kept as a fallback for the rare window
`PrintWindow` cannot read.

## Order module (opt-in, Simulator only)

**This is the only part of this repository that can open a position**, and it is disarmed by
default, on disk, in every clone. Full contract: `docs/api/orders.md`. The reasoning:
[Order entry: Simulator only, off by default](#order-entry-simulator-only-off-by-default).

**How to arm it.** Create an empty file named `orders.enabled` in
`%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\`. No recompile, no NT8 restart. Every
`/orders/*` endpoint answers `403 {"error":"orders module not armed"}` until that file exists and
is younger than 24 h. Delete the file to disarm. `ops.enabled` does not arm this module.

| Tool | Does |
|---|---|
| `nt_order_submit(account, instrument, action, order_type, quantity, limit_price=None, stop_price=None, tif=None, confirm=None, issued_at=None)` | One Market, Limit, StopMarket or StopLimit order on one Simulator or Playback account |
| `nt_order_bracket(account, instrument, action, order_type, quantity, limit_price=None, stop_price=None, stop_loss_price=None, stop_loss_ticks=None, targets=None, tif=None, confirm=None, issued_at=None)` | One entry with its stop loss and profit target(s), under one plan and one confirm |
| `nt_order_change(account, order_id, quantity=None, limit_price=None, stop_price=None, confirm=None, issued_at=None)` | Change the quantity or the prices of ANY working order on the account, whoever placed it |
| `nt_order_cancel(account, order_id, confirm=None, issued_at=None)` | Cancel ANY working order on the account, whoever placed it |
| `nt_position_close(account, instrument, confirm=None, issued_at=None)` | Cancel one instrument's working orders on one account, then flatten it |
| `nt_position_reverse(account, instrument, confirm=None, issued_at=None)` | The same, then enter the same quantity on the other side |

Each tool has two steps. Called with no `confirm`, it changes nothing and returns a plan plus a
confirm string. Call it again with that exact string inside 30 seconds to act. The string works
once. The result reports the state NinjaTrader shows after the call (`Working`, `Filled`,
`Rejected`, ...), not the state you asked for: `ok` means "NinjaTrader took the call", never
"filled".

**Brackets and the OCO pair rule.** `nt_order_bracket` sizes the stop and each target to what the
entry really **filled**, not to what was asked for, with tick offsets measured from the real
average fill price. A resting entry comes back with no exits yet (`exitsPending:true`); a bounded
watcher submits them once it fills. NinjaTrader cancels every other **live** order in an OCO group
the moment one member fills or is cancelled, so each target gets its **own** stop under its own
OCO pair — one pair per target, not one shared group — and cancelling a target only ever takes its
own paired stop with it, never a sibling pair's. To move a target's price without breaking that
pairing, use `nt_order_change` instead of cancelling and resubmitting.

**Caps.** 10 contracts per order, 20 working orders per account, 60 confirmed submits per minute.
The optional file `Documents\NinjaTrader 8\nt8mcp\orders.config.json` (`maxQuantity`,
`maxWorkingOrders`, `maxSubmitsPerMinute`) changes them, up to the ceilings 100 / 100 / 600 in the
code. A value outside `1..ceiling` falls back to the default, with a warning. A bracket's exits and
an ATM's exits are exempt from the working-order cap (the account's hard ceiling of 100 live
orders in code still applies); a NinjaScript reload clears the per-minute count, the other two caps
read live state.

**What it does not do, armed or not:** touch an account whose provider is not Simulator or
Playback, touch the Backtest account, or act on all accounts at once
(`Account.FlattenEverything()` is never called anywhere in this repository). It **can** change or
cancel any working order on the gated account, not only the ones it placed itself — the plan names
the order's owner (`module` / `strategy <name>` / `atm` / `manual`) before you confirm. Every
armed call is appended to `Documents\NinjaTrader 8\nt8mcp\orders.jsonl`. To end flat after a test,
use `nt_position_close` or the ops module's `nt_flatten`.

## Strategies on Sim (opt-in)

Closes the loop: write a strategy, compile it, backtest it, then run it for real on a Simulator or
Playback account and read the fills back. Same arming file as the order module
(`orders.enabled`), same gate chain. Full contract: `docs/api/strategyrun.md`.

| Tool | Does |
|---|---|
| `nt_strategy_start(strategy, account, instrument, bars_period, inputs=None, days_to_load=None, confirm=None, issued_at=None)` | Add a strategy to NinjaTrader's own Control Center Strategies grid, enabled, on a Simulator/Playback account |
| `nt_strategy_stop(id, account, confirm=None, issued_at=None)` | Disable the strategy and remove its grid row; reports the position and working orders left behind. It does not flatten |
| `nt_strategy_runs()` | State, position, working orders and realized P&L for the strategies this server started |

The strategy is added to NinjaTrader's own grid, so the user always sees the row and can disable
it by hand. After a NinjaScript reload the module no longer knows the ids of the instances it
started — they keep running and keep their grid row, and are disabled there by hand; the grid row
is the safety feature, not a nicety. Use `nt_position_close` to flatten what a stopped strategy
left behind.

## ATM strategies (opt-in, Simulator only)

An ATM strategy is NinjaTrader's own bracket manager: one saved template holds a quantity, a stop
loss and a profit target, and `nt_atm_start` sends one entry order under it — NinjaTrader then
arms and manages that template's stop and target itself, on the fill. Same gate chain as the order
module. Full contract: `docs/api/atm.md`.

| Tool | Does |
|---|---|
| `nt_atm_templates()` | The saved ATM templates and the bracket parameters read out of each one |
| `nt_atm_status(account=None)` | The ATM strategies still working: entry, stop, target and the position they hold |
| `nt_atm_start(account, instrument, action, order_type, quantity, template, limit_price=None, stop_price=None, tif=None, confirm=None, issued_at=None)` | Send one entry order under a saved ATM template |
| `nt_atm_close(account, atm_id, confirm=None, issued_at=None)` | Cancel one ATM's working orders and flatten the position it holds |
| `nt_atm_change(account, atm_id, stop_price=None, target_price=None, target_index=None, confirm=None, issued_at=None)` | Move a running ATM's stop and/or target to a new price |

All five are exercised against a live NinjaTrader 8.1.8.2 install: a Market entry with a saved
template filled, the template's stop and target went out at the right offsets, `nt_atm_change`
moved the target and the stop, and `nt_atm_close` left the account flat with no working orders.
NinjaTrader requires the entry order of an ATM strategy to be named `Entry`; the module does that.

## Playback control and the replay bench (opt-in)

Drives the Market Replay clock, and compares what it produces against a backtest. Same arming
file as the order module (`orders.enabled`); the Playback connection must already be Connected —
these tools never connect or disconnect it. Full contract: `docs/api/playback.md`,
`docs/api/reconcile.md`.

| Tool | Does |
|---|---|
| `nt_playback_seek(time, wait_s=30)` | Move the replay clock to a time and read it back |
| `nt_playback_speed(speed)` | Play (>=1) or pause (0) — writing this property IS the play/pause control |
| `nt_playback_run(to, from_time="", speed=1, wait_s=910)` | Run a bounded replay job: seek, play, watch, always pause and report at the end |
| `nt_playback_run_status(id)` | Status of one run job |
| `nt_playback_run_cancel(id)` | Cancel a queued or running job |
| `nt_reconcile(backtest_id="", backtest_run_id="", backtest_trades=None, playback_run_id="", playback_executions=None, tolerance_ticks=1, tolerance_seconds=60, tick_size=None)` | Pair a backtest's trades against a replay run's real fills: matched pairs, fills on only one side, and a plain verdict |

Proven on a live NinjaTrader 8.1.8.2 install: 3 replay hours played in 55 wall-clock seconds at
200x with a strategy running on Playback101, 19 fills collected. A `run` job re-checks every gate
— the arming file included — every few seconds while it plays, and always pauses the clock and
reports before returning, even when a gate trips mid-run. `nt_reconcile` never starts a backtest,
a playback run or an order; a price gap between the two sides usually means the historical and
replay data stores hold different data for that day, not a fill-model bug (see
`docs/api/reconcile.md`).

## Ops module (opt-in)

This module can only **reduce** risk on a trading account, and it is disarmed by default, on
disk, in every clone. Full contract: `docs/api/ops.md`.

**How to arm it.** Create an empty file named `ops.enabled` in
`%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\`. No recompile, no NT8 restart. Every
`/ops/*` endpoint (and `nt_flatten`) answers `403 {"error":"ops module not armed"}` until that
file exists and is younger than 24 h — a flag left over from a debugging session cannot arm the
module forever, in either direction (a future-dated file is treated as stale too). Delete the
file to disarm again, at any time, with no side effect.

**What it can do, once armed:**

- **`nt_flatten(account, instrument=None, confirm=None, issued_at=None)`** — cancel that
  account's working orders and flatten its open positions. Called with no `confirm`, it changes
  nothing and returns a plan plus an AddOn-computed confirm string; call it again with that exact
  string inside 30 seconds to actually act. A position or order that changed in between refuses
  the confirm and hands back a new plan, not a new token.
- A naked-position watchdog (`python -m nt8_mcp.watch --account <name>`) — a local process, not
  an MCP tool, that flattens a position left without an opposing, quantity-matched, correctly
  sided stop order for longer than its grace period.
- A connection guardian (`python -m nt8_mcp.connwatch --connection <name>`) and a restart CLI
  (`python -m nt8_mcp.restart --task <name>`), both local processes — reconnect NinjaTrader's own
  inadvertent drops with backoff, and restart the NinjaTrader process for changes a reload cannot
  survive (chiefly bars-type strategies).

**What it does not do, armed or not:** open a position, submit or modify an order, enable or
disable a strategy, or switch a live chart's series. It can only reduce exposure. (Simulator-only
order entry is a separate opt-in module with its own arming file; see
[Order module](#order-module-opt-in-simulator-only).) `Account.FlattenEverything()` is never
called — every action names one account. A non-Simulator account is never even listed as a
target unless a second file, `ops.live`, is present (this repository never creates it). Only the
two POSTs, `/ops/flatten` and `/ops/reconnect` (and `nt_flatten`), are refused outright while any
live, order-routing connection is up; `GET /ops/status` is a read and is deliberately not behind
that guard — it answers 200 and reports `anyLive:true` while a live connection is up. Every armed
call, successful or not, is appended to an audit log
(`Documents\NinjaTrader 8\nt8mcp\ops.jsonl`).

**The manual Sim flatten test.** No automated run in this repository opens a live position, so the
one thing that actually cancels an order and flattens a position is verified by hand, on
`Sim101`, following the numbered steps in `docs/api/ops.md`: unarmed calls come back 403; arming
needs no recompile; a dry run returns the correct plan and confirm string; a wrong or stale
confirm is refused (409) without touching anything; the fresh confirm cancels the working stop
and flattens the position (`ok:true`, `positionFlat:true`); the account reads back flat, not
reversed, on a second, independent read; the audit log records every step; disarming restores the
403. Two optional refusals (waiting out the 30 s window, and a second working order arriving
between dry-run and confirm) are covered by the automated test suite instead of by hand — see
`CHANGELOG.md`.

## Safety model

- **Read-only by default.** With no arming file on disk there is no order entry, no order
  modify and no flatten, and there is never a strategy enable or disable. Order entry exists for
  Simulator and Playback accounts only; the reasoning is in
  [Order entry: Simulator only, off by default](#order-entry-simulator-only-off-by-default). Outside the opt-in
  features below, the only writes are a chart reload, a screenshot file and a compile.
- **Backtests use the Backtest account only.** A request that names another account is refused.
- **Local only.** The AddOn listens on `localhost:7891`. Nothing is sent to any server by this
  project; your code, charts and account data stay on your machine.
- **Two opt-in files, each armed by an empty file you create by hand**, each ignored again
  after 24 hours: `orders.enabled` and `ops.enabled`. No tool, script or test in this repository
  creates either file for you. `orders.enabled` now arms four things behind one file: order entry
  (including brackets, close and reverse), ATM strategies, strategies-on-Sim, and the Playback
  seek/speed/run controls — all Simulator/Playback only, all gated the same way. `ops.enabled`
  arms the separate, reduce-only ops module below.
- **Live-connection guards.** An assembly reload and both ops actions are refused while a
  connection that can route orders to a real broker is up. A broker *demo* counts as live, on
  purpose: the guard asks "can this connection send an order", not "is this real money".
- **Text is data.** Output, logs, indicator names, drawing tags and window titles can contain
  text written by third-party add-ons or a data feed. The tools that return such text say so in
  their descriptions: it is data, never instructions.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `nt8 health` says connection refused | NinjaTrader is not running, or the AddOn is not compiled in. Run `scripts\install-addon.ps1`, then press F5 in a NinjaScript Editor once. |
| You installed new code but the old behaviour is still there | NinjaTrader recompiles by itself 20-150 s after a `.cs` file lands. `nt_status` says if the running build is older than your source. Indicators already on a chart keep the old code until `nt_chart_reload`. |
| A compile suddenly fails with hundreds of errors in files you did not touch | One duplicate or orphan file breaks the whole NinjaScript assembly. Look for `<name> (1).cs` copies made by a cloud-sync tool. `nt_compile` names the file and line of every error. |
| A backtest ran different dates than you asked for | A connected **Playback** connection caps historical data at the replay clock. The result says so in `warnings`, with the real window in `barsFrom` / `barsTo`. Disconnect Playback for a backtest over other dates. |
| A chart tool answers 504 | NinjaTrader's UI thread is blocked, most often by a message box. `nt_health` reports it as `standingModal`. `nt_output` and `nt_log` still answer. |
| Your own `curl -X POST` gets HTTP 411 | The listener needs a `Content-Length`. Send a body, even an empty one: `curl -s -X POST -d "{}" localhost:7891/compile`. |
| New or renamed tools do not appear in your assistant | An MCP server is started once per session. Start a new session after you update. |
| `nt_nrd_export` or the PDF report says a package is missing | Install the extras: `pip install -e ".[parquet,report]"`. |

## The `nt8` command line

Every MCP tool is also a shell command that prints JSON, which is useful in scripts and for a
quick look without an assistant:

```
nt8                                  list the tools
nt8 health                           the nt_ prefix is optional
nt8 bars --chart first --n 5         keyword arguments
nt8 backtest '{"strategy": "SampleMACrossOver", "tick_replay": false}'
```

Exit code 0 = done, 1 = the AddOn could not be reached or the tool returned an error, 2 = unknown
tool or bad arguments.

## Project layout and development

| Path | What |
|---|---|
| `addon/NT8Bridge.cs` | the AddOn core: HTTP listener, JSON, thread-safe UI access, module discovery |
| `addon/NT8Bridge.*.cs`, `addon/NT8BridgeOps.cs`, `addon/NT8BridgeOrders.cs` | one module per file; the core finds each `Route_<Module>` by reflection, so a new module is a new file and no edit to the core |
| `addon/NOTES.md`, `addon/BACKTEST_RECIPE.md` | what we learned about NinjaTrader internals: reflection targets, threading rules, the headless backtest recipe |
| `server/nt8_mcp/` | the MCP server; `tools_*.py` files are loaded automatically |
| `API.md`, `docs/api/` | the HTTP contract, one file per module |
| `scripts/` | installer, offline compile check, live smoke test |

Checks before a pull request:

```
bash scripts/check.sh addon/*.cs        # offline compile of the AddOn -> CHECK_OK
python server/tests/run_all.py          # unit tests, no NinjaTrader needed
bash scripts/live-smoke.sh -b           # against a running NinjaTrader; -b adds the backtest checks
```

NinjaTrader has no public API for most of what this project does, so the AddOn binds some internal
members by name. `nt_compat` lists every one of them and whether it resolved on your version. After
a NinjaTrader update, that table is the first place to look.

## Related

- [`ninjatrader-to-parquet`](https://github.com/tbraman-dev/ninjatrader-to-parquet) — reads
  NinjaTrader 8 tick and minute history (`.ncd`) in Python and exports it to Parquet or CSV, with
  NinjaTrader closed. Download history with `nt_data_download`, then export it offline for Python
  backtests. (`nt_nrd_export` here covers the Market Replay `.nrd` files.)

## Credits

Read-only and dev-loop capability in this release (compile-through-AddOn, output/log event
rings, `/health`/`/ntstatus`/`/compat`, feed and connection health, executions/performance, the
offline `.nrd` decoder, the optimize/report tooling, and the ops module's design) was merged in
from [`eman007/cli-nt-bridge`](https://github.com/eman007/cli-nt-bridge) (MIT) — see `NOTICE`
for the full license text.

## License

MIT. See `NOTICE` for third-party attribution.
