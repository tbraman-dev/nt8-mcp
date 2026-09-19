#!/usr/bin/env bash
# check-module.sh - the module writer's compile gate. Runs from ANY directory.
#
# Usage:  bash <repo>/scripts/check-module.sh <your .cs files...>
#
# Builds the git HEAD version of every addon/*.cs EXCEPT the ones you name, plus YOUR
# working-tree copies of the ones you do name, and hands that set to check.sh. So another
# writer's half-finished file in the working tree can never make your gate red, and your
# own file is checked against the core exactly as it was last committed.
#
# It also neutralises (replaces with an empty file) any NT8Bridge*.cs already installed in
# bin\Custom\AddOns whose name is in neither set: an install that is ahead of HEAD - an
# AddOn partial split out after the last commit - would otherwise duplicate members of the
# HEAD core and report CS0111 against code you never touched.
#   Override the install folder with NT_ADDONS=<dir> (default: the real NT8 one).
#
# Prints the file set it built (stderr), then check.sh's output. CHECK_OK = green.
# Nothing in NinjaTrader is touched or written; several concurrent runs are safe (just slow).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
NT_ADDONS="${NT_ADDONS:-$HOME/Documents/NinjaTrader 8/bin/Custom/AddOns}"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <your .cs files...>" >&2
  exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Absolute paths for the caller's files (they may be relative to any cwd).
MINE=()
for f in "$@"; do
  d="$(dirname "$f")"
  if [ ! -f "$f" ]; then
    echo "no such file: $f" >&2
    exit 2
  fi
  MINE+=("$(cd "$d" && pwd)/$(basename "$f")")
done

# " NT8Bridge.Charts.cs NT8Bridge.Compile.cs " - the caller's basenames, for substring tests.
SKIP=" $(for f in "${MINE[@]}"; do basename "$f"; done | tr '\n' ' ')"

# 1. every addon/*.cs at HEAD that the caller did not pass
HEAD_N=0
while IFS= read -r tracked; do
  case "$tracked" in *.cs) ;; *) continue ;; esac
  b="$(basename "$tracked")"
  case "$SKIP" in *" $b "*) continue ;; esac
  if ! git -C "$REPO" show "HEAD:$tracked" > "$TMP/$b"; then
    echo "cannot read HEAD:$tracked" >&2
    exit 2
  fi
  HEAD_N=$((HEAD_N+1))
done < <(git -C "$REPO" ls-tree --name-only HEAD addon/)

# 2. installed NT8Bridge*.cs that neither set covers -> empty stub, so check.sh replaces it
STUB_N=0
if [ -d "$NT_ADDONS" ]; then
  for inst in "$NT_ADDONS"/NT8Bridge*.cs; do
    [ -f "$inst" ] || continue
    b="$(basename "$inst")"
    case "$SKIP" in *" $b "*) continue ;; esac
    [ -e "$TMP/$b" ] && continue
    echo "// neutralised by check-module.sh: installed but not in addon/ at HEAD" > "$TMP/$b"
    STUB_N=$((STUB_N+1))
  done
fi

SUBSTITUTES=()
[ "$((HEAD_N + STUB_N))" -gt 0 ] && SUBSTITUTES=("$TMP"/*.cs)

echo "check-module: ${#MINE[@]} file(s) from your tree, $HEAD_N from HEAD, $STUB_N neutralised" >&2
for f in "${MINE[@]}"; do echo "  yours  $f" >&2; done
for f in "${SUBSTITUTES[@]+"${SUBSTITUTES[@]}"}"; do echo "  subst  $(basename "$f")" >&2; done

bash "$SCRIPT_DIR/check.sh" "${SUBSTITUTES[@]+"${SUBSTITUTES[@]}"}" "${MINE[@]}"
