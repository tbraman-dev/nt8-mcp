"""Backtests (v1.1) — run on the Backtest account only, via the AddOn's headless
Strategy Analyzer pass. See ../../API.md "Backtests (v1.1)" for the contract.
"""

import time
from datetime import datetime, timedelta
from urllib.parse import quote

from nt8_mcp import app  # POLL_S is read through the module so tests can patch it
from nt8_mcp.app import _addon_delete, _addon_get, _addon_post, mcp


@mcp.tool(name="nt_strategies")
def nt_strategies():
    """List strategies available to backtest (name, full type name, [NinjaScriptProperty] inputs with defaults)."""
    return _addon_get("/strategies")


@mcp.tool(name="nt_templates")
def nt_templates(strategy: str):
    """List the NinjaTrader strategy templates saved for one strategy, for use as nt_backtest(template=...).
    strategy is a name from nt_strategies (short or full type name); an unknown name is an error, not an
    empty list. Returns {"strategy","folder","templates":["Base","ES-5m"]} — the folder is the one
    NinjaTrader itself names for that strategy, and `templates` is [] when the folder holds no template
    yet. `folder` and `templates` come back null with a `note` when NinjaTrader will not name the folder:
    an unreadable folder and an empty one are different claims. Reads files only; changes nothing."""
    return _addon_get(f"/templates?strategy={quote(strategy)}")


@mcp.tool(name="nt_backtest")
def nt_backtest(
    strategy: str,
    chart: str = "first",
    from_date: str = "",
    to_date: str = "",
    tick_replay: bool = True,
    inputs: dict | None = None,
    instrument: str = "",
    bars_period: dict | None = None,
    wait_s: int = 600,
    template: str = "",
    fill_resolution: str = "",
    fill_resolution_type: str = "",
    fill_resolution_value: int = 0,
    slippage_ticks: float | None = None,
    commission_template: str = "",
    include_commission: bool | None = None,
    fill_limit_on_touch: bool | None = None,
    include_trade_history: bool | None = None,
    max_trades: int = 0,
):
    """Run a strategy backtest and wait for it to finish, returning the final status doc (summary + trades +
    settings). Runs on the Backtest account only — no Sim or live account is ever touched. Strategies that read
    the tape in OnMarketData need tick_replay=True (Tick Replay). chart="first" copies instrument and
    bar type from the first open chart; pass instrument/bars_period to override or skip that. from_date/
    to_date default to the last 2 days (YYYY-MM-DD) when omitted, EXCEPT with template=, where the template's
    own dates, instrument, bar period and inputs are the defaults and nothing is invented (list names with
    nt_templates; any explicit argument still wins, and chart="first" is not sent with a template unless you
    change it). Fill/cost settings: fill_resolution "Standard"|"High" (High is refused with tick_replay,
    NinjaTrader does not allow the combination), fill_resolution_type/_value for High, slippage_ticks (>=0, in
    ticks), commission_template (a NinjaTrader commission template name; an unknown name is refused, and
    passing one turns include_commission on unless you pass include_commission=False), include_commission,
    fill_limit_on_touch, include_trade_history (false empties trades[]), max_trades trims trades[] only and
    never summary. Anything unknown or out of range is refused before the job is armed. The returned document
    echoes every effective setting under "settings" — read back off the strategy, not from this call, because
    several NinjaTrader setters silently no-op. ALWAYS read "barsFrom"/"barsTo" (the bars really loaded) and
    "warnings": while a Playback connection is Connected NinjaTrader caps historical data at the replay clock,
    so the run can cover a different window than from/to — the numbers are then for barsFrom..barsTo. A
    fractional value for an integer input (Fast=5.5) is refused with 400, never rounded. Polls every POLL_S seconds; if still running after wait_s,
    returns the last status doc with a note to call nt_backtest_status(id)."""
    if not template:  # a template carries its own dates; never overwrite them with "the last 2 days"
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")
        if not from_date:
            from_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    body = {"strategy": strategy, "tickReplay": tick_replay}
    if chart and not (template and chart == "first"):
        body["chart"] = chart
    if from_date:
        body["from"] = from_date
    if to_date:
        body["to"] = to_date
    if instrument:
        body["instrument"] = instrument
    if bars_period:
        body["barsPeriod"] = bars_period
    if inputs:
        body["inputs"] = inputs
    if template:
        body["template"] = template
    if fill_resolution:
        body["fillResolution"] = fill_resolution
    if fill_resolution_type:
        body["fillResolutionType"] = fill_resolution_type
    if fill_resolution_value:
        body["fillResolutionValue"] = fill_resolution_value
    if slippage_ticks is not None:
        body["slippageTicks"] = slippage_ticks
    if commission_template:
        body["commissionTemplate"] = commission_template
    if include_commission is not None:
        body["includeCommission"] = include_commission
    if fill_limit_on_touch is not None:
        body["fillLimitOnTouch"] = fill_limit_on_touch
    if include_trade_history is not None:
        body["includeTradeHistory"] = include_trade_history
    if max_trades:
        body["maxTrades"] = max_trades

    status = _addon_post("/backtest", body)
    if "id" not in status:
        return status  # {"error": ...}
    backtest_id = status["id"]

    deadline = time.time() + wait_s
    while status.get("state") in ("queued", "running") and time.time() < deadline:
        time.sleep(app.POLL_S)
        status = _addon_get(f"/backtest/{quote(backtest_id)}")

    if status.get("state") in ("queued", "running"):
        status = dict(status)
        status["note"] = f"still running; call nt_backtest_status({backtest_id!r})"
    return status


@mcp.tool(name="nt_backtest_status")
def nt_backtest_status(id: str):
    """Status of one backtest by id: state, and once state is done, summary + trades. id from nt_backtest or nt_backtests."""
    return _addon_get(f"/backtest/{quote(id)}")


@mcp.tool(name="nt_backtests")
def nt_backtests():
    """List all backtests (id, strategy, instrument, period, from/to dates, state)."""
    return _addon_get("/backtests")


@mcp.tool(name="nt_backtest_cancel")
def nt_backtest_cancel(id: str):
    """Cancel a running backtest on the Backtest account (Terminated) and drop its record. id from nt_backtest or nt_backtests."""
    return _addon_delete(f"/backtest/{quote(id)}")
