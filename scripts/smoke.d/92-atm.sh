# atm module: GET /atm/{templates,status}, POST /atm/{start,close,change} - sourced by
# live-smoke.sh, see its header for the helpers. Contract: docs/api/atm.md.
#
# THIS FILE NEVER ARMS THE MODULE AND NEVER STARTS, CLOSES OR CHANGES AN ATM STRATEGY. It never
# creates orders.enabled (the ATM module is armed by that same file - it has no flag of its own).
# The module ships DISARMED and the default live-smoke run's job is to prove exactly that: every
# /atm/* path answers 403, the two READS included, so no ATM template name - the user's own text -
# is published to an unarmed caller. The POSTs below are made ONLY while the 403 is in force, so
# there is no state to change. If the module IS armed, this file stops POSTing and reports what it
# found instead: a smoke test is not a reason to start an ATM strategy on a trading account,
# however simulated.

ATM_ADDONS_DIR="$HOME/Documents/NinjaTrader 8/bin/Custom/AddOns"
ATM_GETS="templates status"
ATM_POSTS="start close change"

# 1. The ATM module has no arming file of its own: orders.enabled arms it, and 91-orders.sh already
#    reports a leftover one as a FAIL. Here it only decides what this file is allowed to do.
ATM_ARMED=0
[ -e "$ATM_ADDONS_DIR/orders.enabled" ] && ATM_ARMED=1

# 2. Both READS are 403 while disarmed, and say nothing else. This is the one that matters most in
#    this module: a template name is free text the user typed, and an unarmed caller gets none of it.
if [ "$ATM_ARMED" = "0" ]; then
  for P in $ATM_GETS; do
    get_json "/atm/$P"
    if [ "$HTTP_CODE" = "403" ]; then
      R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("error") != "orders module not armed":
    print("ERR:403 body is %r, expected {\"error\":\"orders module not armed\"}" % (d,))
elif len(d) != 1:
    print("ERR:the unarmed 403 leaks extra keys: %r" % (sorted(d),))
else:
    print("OK:disarmed - GET /atm/%s is 403 and says nothing else" % sys.argv[2])
' "$BODY_FILE" "$P")
    else
      R="ERR:http $HTTP_CODE, expected 403 (orders.enabled is absent, so the module must be disarmed)"
    fi
    check_result "/atm/$P disarmed 403" "$R"
  done
else
  report WARN "/atm/* disarmed 403" "orders.enabled is PRESENT - the ATM module may be armed; skipping the 403 checks and every POST below"
fi

# 3. All three POSTs answer the same 403, before the body is even parsed - which is why a body
#    naming an account, a template and an ATM id that do not exist is safe to send while disarmed.
#    The gate chain lives in ONE entry point (Ord_Guarded), so a verb added later cannot forget it;
#    this loop is what keeps that honest.
if [ "$ATM_ARMED" = "0" ]; then
  for VERB in $ATM_POSTS; do
    post_json "/atm/$VERB" '{"account":"NT8BRIDGE_NO_SUCH_ACCOUNT","atmId":"NT8BRIDGE_NO_SUCH_ATM","instrument":"NT8BRIDGE_NO_SUCH_INSTRUMENT","action":"Buy","type":"Market","quantity":1,"template":"NT8BRIDGE_NO_SUCH_TEMPLATE","stopPrice":1}'
    if [ "$HTTP_CODE" = "403" ]; then
      R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
print("OK:disarmed %s refused" % sys.argv[2] if d.get("error") == "orders module not armed"
      else "ERR:403 body is %r" % (d,))
' "$BODY_FILE" "$VERB")
    else
      R="ERR:http $HTTP_CODE, expected 403 for POST /atm/$VERB while disarmed"
    fi
    check_result "/atm/$VERB disarmed 403" "$R"
  done
fi

# 4. The return-null rule: a path Route_Atm does not own must reach the CORE's 404, not an atm 403.
#    A module that answers paths it does not own steals them from every module later in the
#    alphabet and from the core. GET on a POST-only path is the same rule, and POST on a GET-only
#    path is its mirror - both must be the core's 404, never a 403 and never a 405.
get_json "/atm/bogus"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "/atm/<unowned> is a 404" "the core answered, not Route_Atm"
else
  report FAIL "/atm/<unowned> is a 404" "http $HTTP_CODE for GET /atm/bogus, expected the core's 404"
fi

get_json "/atm/start"
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "GET /atm/start is a 404" "the module owns POST there and returns null for GET"
else
  report FAIL "GET /atm/start is a 404" "http $HTTP_CODE, expected the core's 404 (start is POST-only)"
fi

post_json "/atm/templates" '{}'
if [ "$HTTP_CODE" = "404" ]; then
  report PASS "POST /atm/templates is a 404" "the module owns GET there and returns null for POST"
else
  report FAIL "POST /atm/templates is a 404" "http $HTTP_CODE, expected the core's 404 (templates is GET-only)"
fi

# 5. The ATM module is discovered by reflection like every other seam: /compat must list Route_Atm.
#    A missing row means the file did not compile into the running assembly, whatever /health says.
get_json "/compat"
if [ "$HTTP_CODE" = "200" ]; then
  R=$(pycheck '
import json, sys
d = json.load(open(sys.argv[1]))
routes = d.get("routes") or []
if "Route_Atm" not in routes:
    print("ERR:/compat does not list Route_Atm - the ATM module is not in the running assembly: %r" % (routes,))
else:
    print("OK:Route_Atm discovered (%d route seam(s) in this build)" % len(routes))
' "$BODY_FILE")
else
  R="ERR:http $HTTP_CODE"
fi
check_result "/compat lists Route_Atm" "$R"

# 6. The saved ATM templates, read from disk and NOT through the AddOn: this file must work while
#    the module is disarmed, and it must never print a template NAME - those are the user's own.
#    Their count is enough to tell "no templates saved" from "the module cannot see them".
ATM_TEMPLATE_DIR="$HOME/Documents/NinjaTrader 8/templates/AtmStrategy"
if [ -d "$ATM_TEMPLATE_DIR" ]; then
  ATM_N=$(find "$ATM_TEMPLATE_DIR" -maxdepth 1 -type f -name '*.xml' 2>/dev/null | wc -l | tr -d ' ')
  if [ "$ATM_N" = "0" ]; then
    report WARN "ATM templates on disk" "the templates folder exists but holds no *.xml - save one from the ATM dialog before trialling POST /atm/start"
  else
    report PASS "ATM templates on disk" "$ATM_N template file(s) in templates/AtmStrategy (names not printed - they are the user's own)"
  fi
else
  report WARN "ATM templates on disk" "no templates/AtmStrategy folder - GET /atm/templates will report folderExists:false and templates:null"
fi

# 7. The ATM module writes to the ORDERS audit log; it has no file of its own. 91-orders.sh checks
#    that file's shape. Here we only confirm no separate atm.jsonl was invented, because a second
#    log would be a second place a confirmed act could hide.
if [ -f "$HOME/Documents/NinjaTrader 8/nt8mcp/atm.jsonl" ]; then
  report FAIL "one audit log only" "an atm.jsonl exists - the ATM module must write to nt8mcp/orders.jsonl, not to a log of its own"
else
  report PASS "one audit log only" "no atm.jsonl - ATM calls are audited in nt8mcp/orders.jsonl with the orders"
fi
