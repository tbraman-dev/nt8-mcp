"""Chart control (v1.4) — add/remove an indicator, change the primary series, scroll to a
time. See ../../docs/api/chartcontrol.md for the contract. `nt_trade_shot` is Python-only: it reads
a saved run or a live backtest's trade list, then composes the two AddOn calls
(`POST /chart/{id}/scroll` then `POST /chart/{id}/screenshot`) a model would otherwise make by hand.
"""

from urllib.parse import quote

from nt8_mcp.app import Image, _addon_get, _addon_post, mcp
from nt8_mcp.tools_runs import _find as _find_run


@mcp.tool(name="nt_chart_indicator_add")
def nt_chart_indicator_add(indicator: str, chart: str = "first", inputs: dict | None = None, panel: int | None = None):
    """Add an indicator to a chart. indicator is a short ("SMA") or full type name; unknown = error.
    inputs sets its [NinjaScriptProperty] properties by name (a fractional value for an int/enum
    property is refused, never rounded); an unknown input name is an error. panel is the chart
    panel index (0 = price panel), omitted = the indicator's own default. Refused while a modal
    dialog is open. Returns the chart report: instrument, period, visible range, and the full
    indicator list with inputs read back off the chart — the read-back is the source of truth,
    not this call's own arguments."""
    body: dict = {"indicator": indicator}
    if inputs:
        body["inputs"] = inputs
    if panel is not None:
        body["panel"] = panel
    return _addon_post(f"/chart/{quote(chart)}/indicator/add", body)


@mcp.tool(name="nt_chart_indicator_remove")
def nt_chart_indicator_remove(chart: str = "first", indicator: str | None = None, index: int | None = None, force: bool = False):
    """Remove an indicator from a chart, by its display name (indicator, exact match, from
    nt_indicators) or its position (index, 0-based, from nt_indicators). Exactly one of the two is
    required. Only removes an indicator nt_chart_indicator_add itself put there, unless force=True —
    a user's own chart setup is not torn down by accident. Returns the chart report (see
    nt_chart_indicator_add)."""
    if indicator is None and index is None:
        return {"error": "indicator or index is required"}
    body: dict = {"force": force}
    if indicator is not None:
        body["indicator"] = indicator
    else:
        body["index"] = index
    return _addon_post(f"/chart/{quote(chart)}/indicator/remove", body)


@mcp.tool(name="nt_chart_set_series")
def nt_chart_set_series(chart: str = "first", instrument: str | None = None, bars_period: dict | None = None,
                        restore: bool = False):
    """Change a chart's primary series: instrument (a name nt_data_coverage/nt_backtest would
    accept, e.g. "ES 12-26") and/or bars_period ({"type": "Minute", "value": 5}; also value2,
    baseType, baseValue; type is a NinjaTrader BarsPeriodType name: Minute, Day, Tick, Volume,
    Range, Renko, ... or the NUMBER of a third-party bar type). Either alone is fine.

    restore=True puts back EXACTLY what the chart showed before this tool first changed it (the
    chart's own original period object, so a third-party bar type comes back with every setting).
    Use it to undo a change: do not try to re-type an exotic period by hand. The memory of the
    original does not survive a NinjaScript reload.

    Refused while the chart has an ENABLED strategy attached, or a modal dialog is open. The chart
    reloads its bars after the change; the call waits (up to about 8 s) and returns the chart
    report read AFTER that reload (see nt_chart_indicator_add)."""
    if restore:
        return _addon_post(f"/chart/{quote(chart)}/series", {"restore": True})
    if not instrument and not bars_period:
        return {"error": "instrument and/or bars_period is required, or restore=True"}
    body: dict = {}
    if instrument:
        body["instrument"] = instrument
    if bars_period:
        body["barsPeriod"] = bars_period
    return _addon_post(f"/chart/{quote(chart)}/series", body)


@mcp.tool(name="nt_chart_scroll_to")
def nt_chart_scroll_to(time: str, chart: str = "first"):
    """Scroll a chart so `time` (local NT8 time, "yyyy-MM-ddTHH:mm:ss") is visible, keeping the
    current window width. A time outside the loaded bars clamps to the nearest end. Returns the
    chart report (see nt_chart_indicator_add)."""
    return _addon_post(f"/chart/{quote(chart)}/scroll", {"time": time})


def _trade_of(run_id: str, trade_index: int):
    """The one trade a screenshot needs, from a saved run (nt_runs id) or a live/unsaved backtest
    id (nt_backtest/nt_backtests). Returns (trade_dict, None) or (None, error_dict)."""
    rec = _find_run(run_id)
    if rec is not None:
        trades = rec.get("trades")
    else:
        doc = _addon_get(f"/backtest/{quote(run_id)}")
        if not isinstance(doc, dict) or "trades" not in doc:
            return None, {"error": f"no saved run or backtest '{run_id}' (see nt_runs / nt_backtests)"}
        trades = doc.get("trades")
    if not isinstance(trades, list) or not trades:
        return None, {"error": f"'{run_id}' has no trades"}
    if trade_index < 0 or trade_index >= len(trades):
        return None, {"error": f"trade_index {trade_index} is out of range (0..{len(trades) - 1})"}
    return trades[trade_index], None


@mcp.tool(name="nt_trade_shot")
def nt_trade_shot(run_id: str, trade_index: int, chart: str = "first"):
    """Screenshot of `chart` scrolled to one trade's entry time. run_id is a saved run id (nt_runs)
    or a live backtest id (nt_backtest/nt_backtests); trade_index is 0-based into that run's
    trades[] (n in nt_backtest's trade rows). A trade with no entry (entryTime null) is an error,
    not a screenshot of wherever the chart already was."""
    trade, err = _trade_of(run_id, trade_index)
    if err is not None:
        return err
    entry_time = trade.get("entryTime")
    if not entry_time:
        return {"error": f"trade {trade_index} of '{run_id}' has no entryTime"}

    scrolled = nt_chart_scroll_to(entry_time, chart)
    if not isinstance(scrolled, dict) or "error" in scrolled:
        return scrolled  # {"error": ...}

    shot = _addon_post(f"/chart/{quote(chart)}/screenshot")
    if not isinstance(shot, dict) or "path" not in shot:
        return shot  # {"error": ...}
    return Image(path=shot["path"])
