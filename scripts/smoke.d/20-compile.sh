# needs: -b  -- a real tree compile takes ~20-30 s, so this is not part of the fast pass.
# compile checks - sourced by live-smoke.sh, see its header for the helpers.
#
# CHECK-ONLY, ALWAYS. Nothing here ever sends reload=1: that swaps the NinjaScript assembly,
# restarts every indicator and orphans bars-type instances. POST /compile with checkCompileOnly
# disturbs nothing - it emits no assembly at all. The reload path is proven once, by hand,
# never by a script that runs at every gate.

# 1. POST /compile -> 200 with the documented shape, and assemblyReloaded MUST be false.
post_json "/compile" "{}"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
missing = [k for k in ("ok", "assemblyReloaded", "errors", "warnings", "warningCount", "warningsSuppressed", "seconds", "checkCompileOnly") if k not in d]
if missing:
    print("ERR:missing keys %r" % (missing,))
elif not isinstance(d["ok"], bool):
    print("ERR:ok is not a bool: %r" % (d["ok"],))
elif d["assemblyReloaded"] is not False or d["checkCompileOnly"] is not True:
    print("ERR:a check-only compile claimed assemblyReloaded=%r checkCompileOnly=%r"
          % (d["assemblyReloaded"], d["checkCompileOnly"]))
elif not isinstance(d["errors"], list) or not isinstance(d["warnings"], list):
    print("ERR:errors/warnings are not lists: %r" % (d,))
elif len(d["warnings"]) > 200 or d["warningCount"] < len(d["warnings"]):
    print("ERR:warnings[] must be capped at 200 and counted in full: %d listed, warningCount=%r" % (len(d["warnings"]), d["warningCount"]))
elif any(w.get("code") in ("CS1701", "CS1702") for w in d["warnings"]):
    print("ERR:CS1701/CS1702 assembly-unification noise is listed (113k of them on a clean tree)")
elif not isinstance(d["seconds"], (int, float)):
    print("ERR:seconds is not a number: %r" % (d["seconds"],))
else:
    bad = [e for e in (d["errors"] + d["warnings"])
           if sorted(e) != ["code", "column", "file", "line", "message"]]
    if bad:
        print("ERR:diagnostic not {file,line,column,code,message}: %r" % (bad[0],))
    else:
        print("OK:ok=%s %d error(s) %d warning(s) in %.1fs"
              % (d["ok"], len(d["errors"]), len(d["warnings"]), d["seconds"]))
' "$BODY_FILE")
elif [ "$HTTP_CODE" = "000" ]; then
  # A timeout is NOT proof of a failed compile - NinjaTrader is probably still compiling.
  R="WARN:no response within the smoke helper timeout; NT8 may still be compiling"
else
  R="ERR:http $HTTP_CODE"
fi
case "$R" in
  OK:*)   report PASS "POST /compile" "${R#OK:}" ;;
  WARN:*) report WARN "POST /compile" "${R#WARN:}" ;;
  *)      report FAIL "POST /compile" "${R#ERR:}" ;;
esac

# 2. GET /compile -> 404. Route_Compile must return null for a method it does not handle,
#    so the core - not the module - gives the 404.
get_json "/compile"
if [ "$HTTP_CODE" = "404" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d.get("error"), str) and d["error"]:
    print("OK:error=%r" % (d["error"],))
else:
    print("ERR:no error field: %r" % (d,))
' "$BODY_FILE")
else
  R="ERR:expected http 404 for GET /compile, got $HTTP_CODE"
fi
check_result "GET /compile" "$R"

# 3. compile_last.json - the copy that survives a reload tearing the socket down.
COMPILE_LAST="${NT8_HOME:-$HOME/Documents/NinjaTrader 8}/NT8Bridge/compile_last.json"
if [ ! -f "$COMPILE_LAST" ]; then
  report WARN "compile_last.json" "not found at $COMPILE_LAST (UserDataDir may not be the default)"
else
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
if "ok" in d and isinstance(d.get("errors"), list):
    print("OK:%s ok=%s errors=%d" % (d.get("compiledAtUtc"), d.get("ok"), len(d["errors"])))
else:
    print("ERR:not a compile result: %r" % (d,))
' "$COMPILE_LAST")
  check_result "compile_last.json" "$R"
fi
