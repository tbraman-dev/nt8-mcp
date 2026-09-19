#!/usr/bin/env bash
# live-smoke.sh - smoke test the LIVE NT8Bridge AddOn HTTP API.
# Contract: <repo>\API.md (read that first if a
# check here looks wrong - this script must follow the contract, not the other way round).
#
# Usage (run from any directory):
#   ./live-smoke.sh              # core (non-backtest) checks only - seconds
#   ./live-smoke.sh -b           # also run backtest-class checks - can take ~5 min per run
#   NT8_BASE=http://localhost:7891 ./live-smoke.sh   # override base URL (default shown)
#
# Deps: curl, python3 (or python) on PATH. Bash only (Git Bash on Windows is fine).
# Exit code: 0 if no FAILs (WARNs are ok), 1 if any FAIL.
#
# ---------------------------------------------------------------------------
# The checks themselves live in scripts/smoke.d/*.sh, sourced here in name order.
# One file per module, named <NN>-<module>.sh (00-core, 10-backtest, 20-compile, ...).
# A file whose FIRST LINE is the comment `# needs: -b` is backtest-class: it is skipped
# unless -b was passed. Everything below is in scope for a sourced file:
#
#   $BASE $PY $WORKDIR $RUN_BACKTESTS
#   get_json <path>            -> $HTTP_CODE, $BODY_FILE   (GET)
#   post_json <path> <body>    -> same. An EMPTY body sends `Content-Length: 0`,
#                                 because HttpListener answers 411 to a POST without it.
#   delete_json <path>         -> same
#   pycheck <python src> <file>-> prints "OK:<detail>" or "ERR:<detail>"
#   check_result <name> <line> -> turns that line into a PASS/FAIL row
#   report PASS|FAIL|WARN <name> <detail>   -> one row, counted in the summary
#
# A sourced file must not `exit` (it would kill the whole run) and must not `set -e`.
# ---------------------------------------------------------------------------

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${NT8_BASE:-http://localhost:7891}"
RUN_BACKTESTS=0
while getopts "b" opt; do
  case "$opt" in
    b) RUN_BACKTESTS=1 ;;
    *) echo "usage: $0 [-b]" >&2; exit 2 ;;
  esac
done

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "1" >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  # Windows ships a "python3"/"python" App Execution Alias stub that prints a
  # Store-install nag and exits nonzero instead of running - command -v alone
  # can't tell it apart from a real interpreter, so we probe execution above.
  echo "FAIL  no working python3/python on PATH" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PASS_N=0
FAIL_N=0
WARN_N=0

report() {
  # $1=PASS|FAIL|WARN  $2=check name  $3=detail
  case "$1" in
    PASS) PASS_N=$((PASS_N+1)) ;;
    WARN) WARN_N=$((WARN_N+1)) ;;
    *)    FAIL_N=$((FAIL_N+1)) ;;
  esac
  printf '%-4s  %-32s  %s\n' "$1" "$2" "$3"
}

# GET/POST/DELETE helpers: write body to $WORKDIR/resp.json, set HTTP_CODE, BODY_FILE.
get_json() {
  BODY_FILE="$WORKDIR/resp.json"
  HTTP_CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 10 "$BASE$1")"
}
post_json() {
  BODY_FILE="$WORKDIR/resp.json"
  if [ -n "${2:-}" ]; then
    HTTP_CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 60 -X POST -H 'Content-Type: application/json' -d "$2" "$BASE$1")"
  else
    # HttpListener answers 411 to a POST without Content-Length, so send an empty body explicitly
    HTTP_CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 60 -X POST -H 'Content-Length: 0' "$BASE$1")"
  fi
}
delete_json() {
  BODY_FILE="$WORKDIR/resp.json"
  HTTP_CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 10 -X DELETE "$BASE$1")"
}

# Run a python validator against $BODY_FILE. Validator must print "OK:<detail>" or
# "ERR:<detail>" on stdout. Returns that line via stdout.
pycheck() {
  "$PY" -c "$1" "${@:2}" 2>&1
}

check_result() {
  # $1=name  $2=OK:/ERR: line from pycheck
  local name="$1" res="$2"
  case "$res" in
    OK:*)  report PASS "$name" "${res#OK:}" ;;
    *)     report FAIL "$name" "${res#ERR:}" ;;
  esac
}

echo "== NT8Bridge live smoke test against $BASE =="
echo

SOURCED_N=0
for SMOKE_FILE in "$SCRIPT_DIR"/smoke.d/*.sh; do
  [ -f "$SMOKE_FILE" ] || continue        # no match: the glob stays literal
  NEEDS_B=0
  case "$(head -n 1 "$SMOKE_FILE")" in
    *"needs: -b"*) NEEDS_B=1 ;;
  esac
  if [ "$NEEDS_B" -eq 1 ] && [ "$RUN_BACKTESTS" -eq 0 ]; then
    echo "(skipping $(basename "$SMOKE_FILE") - pass -b to run it)"
    continue
  fi
  SOURCED_N=$((SOURCED_N+1))
  # shellcheck disable=SC1090
  . "$SMOKE_FILE"
  echo
done

if [ "$SOURCED_N" -eq 0 ]; then
  echo "FAIL  no check files ran from $SCRIPT_DIR/smoke.d" >&2
  exit 1
fi

echo "== Summary: $PASS_N pass, $WARN_N warn, $FAIL_N fail =="
if [ "$FAIL_N" -gt 0 ]; then
  exit 1
fi
exit 0
