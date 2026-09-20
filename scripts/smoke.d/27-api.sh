# api checks - sourced by live-smoke.sh, see its header for the helpers.
#
# READ-ONLY, ALWAYS. Pure reflection over already-loaded assemblies: nothing is instantiated,
# nothing is invoked, nothing installed or compiled changes.

# 1. GET /api/search?q=SMA -> the SMA indicator type is in there somewhere.
get_json "/api/search?q=SMA"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
hits = [r for r in d.get("results", []) if r.get("resultKind") == "Type" and r.get("name", "").endswith(".SMA")]
if not hits:
    print("ERR:no Type result endswith \".SMA\" in %r" % (d.get("results"),))
elif not isinstance(d.get("matched"), int) or not isinstance(d.get("truncated"), bool):
    print("ERR:matched/truncated missing or wrong type: %r" % (d,))
else:
    print("OK:found %s (matched=%d)" % (hits[0]["name"], d["matched"]))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "GET /api/search?q=SMA" "$R"

# 2. GET /api/type?name=...StrategyBase&member=EnterLong -> the overload(s), never collapsed.
get_json "/api/type?name=NinjaTrader.NinjaScript.StrategyBase&member=EnterLong"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
members = d.get("members", [])
bad_name = [m for m in members if "enterlong" not in (m.get("name") or "").lower()]
exact = [m for m in members if m.get("name") == "EnterLong"]
if not members:
    print("ERR:member=EnterLong filter returned no members: %r" % (d,))
elif bad_name:
    print("ERR:member filter (contains, case-insensitive) let through a non-match: %r" % (bad_name[0],))
elif len(exact) < 2:
    print("ERR:expected several EnterLong overloads, got %d" % (len(exact),))
elif len({m.get("signature") for m in members}) != len(members):
    print("ERR:two overloads share one signature (collapsed): %r" % (members,))
else:
    print("OK:%d member(s) match, %d EnterLong overload(s)" % (len(members), len(exact)))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "GET /api/type EnterLong overloads" "$R"

# 3. GET /api/type?name=<not real> -> a clean 404, not a 200 with an empty/guessed body.
get_json "/api/type?name=Definitely.Not.A.Real.NinjaTrader.Type"
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
  R="ERR:expected http 404 for an unknown type, got $HTTP_CODE"
fi
check_result "GET /api/type unknown type is a clean 404" "$R"
