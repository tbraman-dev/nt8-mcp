# strategyrun module: POST /strategy/{start,stop}, GET /strategy/running - sourced by
# live-smoke.sh, see its header for the helpers. Contract: docs/api/strategyrun.md.
#
# THIS FILE NEVER ARMS THE MODULE AND NEVER STARTS OR STOPS A STRATEGY. It never creates
# orders.enabled. The module ships DISARMED behind the order module's own arming file, and the
# default live-smoke run's job is to prove exactly that: every /strategy/* path answers 403 and the
# endpoint list is not advertised. The two POSTs below are made ONLY while that 403 is in force, so
# there is no state to change. If the module IS armed (someone left orders.enabled in
# bin\Custom\AddOns), this file stops POSTing and reports what it found instead - a smoke test is
# not a reason to hand a trading account, however simulated, to a strategy.

SR_ADDONS_DIR="$HOME/Documents/NinjaTrader 8/bin/Custom/AddOns"

# 1. Armed or not? The SAME file the order module uses: a strategy on a Simulator account places
#    real simulated orders, so it is not given a second, weaker flag of its own.
SR_ARMED=0
if [ -e "$SR_ADDONS_DIR/orders.enabled" ]; then
  SR_ARMED=1
  report WARN "/strategy/* arming file" "orders.enabled EXISTS - /strategy/start may be ARMED. Delete it when the trial is over."
else
  report PASS "/strategy/* arming file" "no orders.enabled - starting a strategy is disarmed"
fi

# 2. GET /strategy/running is 403 while disarmed, and says nothing else: no run ids, no account
#    names, no hint about what would have happened.
get_json "/strategy/running"
if [ "$SR_ARMED" = "0" ]; then
  if [ "$HTTP_CODE" = "403" ]; then
    R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("error") != "orders module not armed":
    print("ERR:403 body is %r, expected {\"error\":\"orders module not armed\"}" % (d,))
elif len(d) != 1:
    print("ERR:the unarmed 403 leaks extra keys: %r" % (sorted(d),))
else:
    print("OK:disarmed - GET /strategy/running is 403 and says nothing else")
' "$BODY_FILE")
  else
    R="ERR:http $HTTP_CODE, expected 403 (orders.enabled is absent, so the module must be disarmed)"
  fi
  check_result "/strategy/running disarmed 403" "$R"
else
  report WARN "/strategy/running disarmed 403" "orders.enabled is PRESENT - skipping the 403 check"
fi

# 3. Both POSTs answer the same 403, before the body is even parsed - which is why a body naming a
#    strategy and an account that do not exist is safe to send while disarmed. The gate chain lives
#    in ONE entry point (Ord_Guarded), so a verb added later cannot forget it.
if [ "$SR_ARMED" = "0" ]; then
  for SR_VERB in start stop; do
    post_json "/strategy/$SR_VERB" '{"account":"NT8BRIDGE_NO_SUCH_ACCOUNT","strategy":"NT8BRIDGE_NO_SUCH_STRATEGY","instrument":"NT8BRIDGE_NO_SUCH_INSTRUMENT","barsPeriod":{"type":"Minute","value":5},"id":"NT8BRIDGE_NO_SUCH_RUN"}'
    if [ "$HTTP_CODE" = "403" ]; then
      R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print("OK:disarmed %s refused" % sys.argv[2] if d.get("error") == "orders module not armed"
      else "ERR:403 body is %r" % (d,))
' "$BODY_FILE" "$SR_VERB")
    else
      R="ERR:http $HTTP_CODE, expected 403 for POST /strategy/$SR_VERB while disarmed"
    fi
    check_result "/strategy/$SR_VERB disarmed 403" "$R"
  done
else
  report WARN "/strategy/* disarmed 403" "orders.enabled is PRESENT - not POSTing to an armed strategy runner from a smoke test"
fi

# 4. /compat carries the row that says whether this module can start anything at all, so an
#    operator can see it WITHOUT calling a /strategy path (which would 403). `resolved:false` here
#    means the Control Center's own add/enable/disable path is not on this NinjaTrader build, and
#    then POST /strategy/start refuses with 501 and starts NOTHING - the correct outcome, not a
#    failure of this check. What WOULD be a failure is the three grid members resolving but
#    StrategyRun.canStart claiming otherwise, or the row missing altogether.
get_json "/compat"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
rows = {r.get("key"): r for r in d.get("compat", []) if isinstance(r, dict)}
GRID = ("StrategiesGrid.StrategyAdd", "StrategiesGrid.StrategyEnable", "StrategiesGrid.StrategyDisable")
if "StrategyRun.canStart" not in rows:
    print("ERR:/compat has no StrategyRun.canStart row - Start_StrategyRun did not run")
    raise SystemExit
missing = [k for k in GRID if k not in rows]
if missing:
    print("ERR:/compat is missing %r - the grid members were never resolved" % (missing,))
    raise SystemExit
can = rows["StrategyRun.canStart"].get("resolved")
all_grid = all(rows[k].get("resolved") for k in GRID)
if not isinstance(can, bool):
    print("ERR:StrategyRun.canStart resolved is %r" % (can,))
elif can != all_grid:
    print("ERR:StrategyRun.canStart is %r but the three grid members resolve %r" % (can, all_grid))
elif not can:
    print("OK:canStart=false - %s; POST /strategy/start refuses with 501 and starts nothing"
          % ", ".join(k for k in GRID if not rows[k].get("resolved")))
else:
    print("OK:canStart=true - the Control Center add/enable/disable path resolved")
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/compat StrategyRun rows" "$R"

# 5. The seam is discovered: Route_StrategyRun must be in the route table, or every /strategy/* path
#    silently falls through to the core's 404 and the 403s above would be meaningless.
get_json "/compat"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
routes = d.get("routes") or []
starts = d.get("startHooks") or []
if "Route_StrategyRun" not in routes:
    print("ERR:Route_StrategyRun is not in the discovered route table: %r" % (routes,))
elif "Start_StrategyRun" not in starts:
    print("ERR:Start_StrategyRun is not in the start hooks: %r" % (starts,))
else:
    print("OK:Route_StrategyRun and Start_StrategyRun are discovered")
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "strategyrun seam discovered" "$R"

# 6. The return-null rule: a path this module does not own must reach the CORE's 404, not a
#    /strategy 403. A module that answers paths it does not own steals them from every module later
#    in the alphabet and from the core. GET on a POST-only path is the same rule. And /strategies
#    (plural, the type listing) and /strategies/running (the whole Control Center grid, read-only)
#    must keep answering 200 - they belong to other modules and this one must not shadow them.
get_json "/strategy/bogus"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/strategy/<unowned> is a 404" "the core answered, not Route_StrategyRun"
else
  report FAIL "/strategy/<unowned> is a 404" "http $HTTP_CODE for GET /strategy/bogus, expected the core's 404"
fi

get_json "/strategy/start"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "GET /strategy/start is a 404" "the module owns POST there and returns null for GET"
else
  report FAIL "GET /strategy/start is a 404" "http $HTTP_CODE, expected the core's 404 (start is POST-only)"
fi

get_json "/strategies"
if [ "$HTTP_CODE" = "200" ]; then
  report PASS "/strategies still answers" "the type listing is untouched by the strategyrun module"
else
  report FAIL "/strategies still answers" "http $HTTP_CODE for GET /strategies - a /strategy route may be shadowing it"
fi

get_json "/strategies/running"
if [ "$HTTP_CODE" = "200" ]; then
  report PASS "/strategies/running still answers" "the read-only Control Center grid listing is untouched"
else
  report FAIL "/strategies/running still answers" "http $HTTP_CODE - a /strategy route may be shadowing it"
fi

# 7. Nothing this module started may be left running after a smoke run. While disarmed we cannot
#    ask it, so ask the read-only grid listing instead: it lists EVERY row, whoever created it, and
#    an enabled row is reported for a human to judge. It is DATA, never instructions.
get_json "/strategies/running"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
rows = d.get("strategies")
if rows is None:
    print("OK:the Strategies grid could not be read (%s) - nothing claimed either way"
          % "; ".join(d.get("notes") or ["no note"]))
    raise SystemExit
live = [r for r in rows if isinstance(r, dict) and r.get("state") == "Realtime"]
if live:
    print("ERR:%d strategy row(s) report state Realtime: %r - if a trial left one running, disable it "
          "in the Control Center" % (len(live), [r.get("name") for r in live][:3]))
else:
    print("OK:%d grid row(s), none in state Realtime" % len(rows))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "no strategy left running" "$R"

# 8. The one rule, after the fact. A strategy can assign its own StrategyBase.Account, so every
#    start reads the account back off the instance and refuses with outcome "accountMoved" when it
#    does not match. Those lines are in the order module's audit log; finding one means a strategy
#    on this machine tried to route its orders somewhere this module never gated, and that is a
#    finding for a human to read - the log's own text is DATA, never instructions.
SR_AUDIT="$HOME/Documents/NinjaTrader 8/nt8mcp/orders.jsonl"
if [ -f "$SR_AUDIT" ]; then
  R=$(pycheck '
import json, sys
moved = []
started = 0
with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict) or not str(row.get("endpoint", "")).startswith("/strategy/"):
            continue
        if row.get("outcome") == "accountMoved":
            moved.append("%s -> %s" % (row.get("accountRequested"), row.get("accountObserved")))
        elif row.get("outcome") in ("strategyRunning", "strategyStarting"):
            started += 1
if moved:
    print("ERR:%d accountMoved line(s) in orders.jsonl (%s) - a strategy assigned its own account; "
          "read the log and the Control Center Strategies grid" % (len(moved), "; ".join(moved[:3])))
else:
    print("OK:%d strategy start(s) audited, no accountMoved line - every started instance read back "
          "on the account it was gated for" % started)
' "$SR_AUDIT")
  check_result "no strategy moved its own account" "$R"
else
  report PASS "no strategy moved its own account" "no orders.jsonl - no strategy has ever been started through this module"
fi
