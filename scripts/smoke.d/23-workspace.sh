# workspace module: /workspace, /strategies/running, /screenshot.
# Sourced by live-smoke.sh - see its header for the helpers. Never `exit`, never `set -e`.
# Contract: docs/api/workspace.md. Every check is defensive: a missing chart or an unrealized
# Strategies tab is a fact about this desktop (WARN), a broken contract is a FAIL.

# check_result only knows OK:/ERR:. Several checks here have a legitimate third answer, so they
# go through this local wrapper instead (module-prefixed: sourced files share one shell).
ws_result() {
  case "$2" in
    OK:*)   report PASS "$1" "${2#OK:}" ;;
    WARN:*) report WARN "$1" "${2#WARN:}" ;;
    *)      report FAIL "$1" "${2#ERR:}" ;;
  esac
}

# 1. /workspace: name + windows, every row shaped, details null never faked
get_json "/workspace"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d, dict) or "name" not in d or not isinstance(d.get("windows"), list):
    print("ERR:not {name, windows:[...]}: %s" % repr(d)[:200])
else:
    wins = d["windows"]
    bad = [w for w in wins if not isinstance(w, dict) or "kind" not in w or "details" not in w or "note" not in w]
    if bad:
        print("ERR:%d window row(s) missing kind/details/note, e.g. %s" % (len(bad), repr(bad[0])[:200]))
    else:
        charts = [w for w in wins if w.get("kind") == "Chart"]
        faked = [w for w in charts if w.get("details") in ({}, [])]
        if faked:
            print("ERR:a chart reported details %r - it must be null plus a note" % (faked[0]["details"],))
        else:
            print("OK:workspace=%r %d windows, %d chart(s)" % (d["name"], len(wins), len(charts)))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/workspace" "$R"

# 2. /workspace chart details: instrument + period + per-script state
get_json "/workspace"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
charts = [w for w in d.get("windows", []) if w.get("kind") == "Chart" and w.get("details")]
if not charts:
    print("WARN:no chart row carries details (no chart open, or none answered)")
else:
    c = charts[0]["details"]
    missing = [k for k in ("instrument", "period", "barsCount", "indicators", "strategies") if k not in c]
    if missing:
        print("ERR:chart details missing %r" % (missing,))
    else:
        scripts = (c["indicators"] or []) + (c["strategies"] or [])
        bad = [s for s in scripts if "name" not in s or "state" not in s]
        if bad:
            print("ERR:a script row is not {name,state}: %r" % (bad[0],))
        else:
            print("OK:%s %s bars=%s scripts=%d" % (c["instrument"], c["period"], c["barsCount"], len(scripts)))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
ws_result "/workspace details" "$R"

# 3. /workspace geometry: a window with an hwnd must carry isMinimized and a size
get_json "/workspace"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
withgeo = [w for w in d.get("windows", []) if w.get("hwnd")]
if not withgeo:
    print("WARN:no window reported an hwnd")
else:
    bad = [w for w in withgeo if w.get("isMinimized") is None or w.get("width") is None]
    if bad:
        print("ERR:%d window(s) have an hwnd but no isMinimized/width, e.g. %r" % (len(bad), bad[0].get("title")))
    else:
        mini = [w for w in withgeo if w.get("isMinimized")]
        print("OK:%d window(s) with geometry, %d minimized" % (len(withgeo), len(mini)))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
ws_result "/workspace geometry" "$R"

# 4. /strategies/running: gridResolved bool, and null (never []) when it is false
get_json "/strategies/running"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if not isinstance(d, dict) or not isinstance(d.get("gridResolved"), bool) or not isinstance(d.get("notes"), list):
    print("ERR:not {gridResolved:bool, strategies, notes:[...]}: %s" % repr(d)[:200])
elif d["gridResolved"]:
    rows = d.get("strategies")
    if not isinstance(rows, list):
        print("ERR:gridResolved true but strategies is %s" % type(rows).__name__)
    else:
        bad = [r for r in rows if "error" not in r and ("state" not in r or "enabled" not in r)]
        if bad:
            print("ERR:a row has no state/enabled: %s" % repr(bad[0])[:200])
        else:
            print("OK:%d grid row(s)" % len(rows))
elif d.get("strategies") is None:
    print("WARN:gridResolved false (%s)" % ("; ".join(d["notes"]) or "no note"))
else:
    print("ERR:gridResolved false but strategies is %r - it must be null, never []" % (d.get("strategies"),))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
ws_result "/strategies/running" "$R"

# 5. /strategies (the type listing) still belongs to the backtest module - no route theft
get_json "/strategies"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d, list):
    print("OK:%d strategy type(s) - /strategies is untouched" % len(d))
else:
    print("ERR:/strategies no longer returns a list: %s" % type(d).__name__)
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/strategies not stolen" "$R"

# 6. POST /screenshot of the Control Center by title substring.
#    No `path` is sent: the AddOn's default is a Windows path, and a $WORKDIR path from Git Bash
#    (/tmp/...) is not one. The validator checks and removes the file it reports.
post_json "/screenshot" '{"window":"Control Center"}'
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, os, sys
d = json.load(open(sys.argv[1]))
path = d.get("path") or ""
if not d.get("ok") or not path:
    print("ERR:%s" % repr(d)[:200])
elif not os.path.exists(path):
    print("ERR:reported %s but no file is there" % path)
elif (d.get("width") or 0) <= 0 or (d.get("bytes") or 0) <= 0:
    print("ERR:%sx%s, %s bytes" % (d.get("width"), d.get("height"), d.get("bytes")))
elif d.get("looksBlank"):
    print("WARN:%sx%s but only %s bytes - looksBlank (a black capture looks like an answer)" % (d.get("width"), d.get("height"), d.get("bytes")))
else:
    print("OK:%sx%s %s bytes via %s" % (d.get("width"), d.get("height"), d.get("bytes"), d.get("method")))
try:
    if path and os.path.exists(path):
        os.remove(path)
except OSError:
    pass
' "$BODY_FILE")
elif [ "$HTTP_CODE" = "409" ]; then
  R="WARN:the Control Center is minimized - this endpoint never restores a window"
else
  R="ERR:http $HTTP_CODE"
fi
ws_result "/screenshot window=" "$R"

# 7. POST /screenshot of a title nothing matches -> 404
post_json "/screenshot" '{"window":"no such window zzq"}'
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/screenshot unknown title" "http 404 as expected"
else
  report FAIL "/screenshot unknown title" "expected http 404, got $HTTP_CODE"
fi

# 8. POST /screenshot of the first chart's WINDOW (200, or 409 if it is minimized)
post_json "/screenshot" '{"chart":"first"}'
case "$HTTP_CODE" in
  200)
    R=$(pycheck '
import json, os, sys
d = json.load(open(sys.argv[1]))
path = d.get("path") or ""
if d.get("ok") and d.get("method") == "PrintWindow" and d.get("composited") is not True:
    # observed: PrintWindow of the WPF chart window alone is the frame around a black hole
    print("ERR:chart printed without its render form (composited=%r) - a chart screenshot with no chart in it" % d.get("composited"))
elif d.get("ok") and path and os.path.exists(path) and (d.get("bytes") or 0) > 0:
    print("OK:%s %sx%s %s bytes composited=%s" % (d.get("window"), d.get("width"), d.get("height"), d.get("bytes"), d.get("composited")))
else:
    print("ERR:%s" % repr(d)[:200])
try:
    if path and os.path.exists(path):
        os.remove(path)
except OSError:
    pass
' "$BODY_FILE") ;;
  409) R="WARN:the chart window is minimized or has no handle yet" ;;
  404) R="WARN:no chart open" ;;
  *)   R="ERR:http $HTTP_CODE" ;;
esac
ws_result "/screenshot chart=first" "$R"
