#!/bin/bash
# Compile-check = the real NinjaTrader.Custom.csproj (everything in Documents\NinjaTrader 8\bin\Custom) built into a temp dir.
# Same compiler, same references, same sources as pressing F5 in NT8. Nothing in NT8 is touched.
# Usage: ninjascript/build/check.sh Indicators/*.cs
#   Files you pass REPLACE the installed copies with the same file name (Indicators or AddOns) (so stale NT8-generated regions never matter).
#   No args = build the install exactly as it is.
# Prints errors (and warnings from your files), then CHECK_OK on success. Safe to run concurrently.
NTPROJ="${USERPROFILE:-$HOME}\\Documents\\NinjaTrader 8\\bin\\Custom\\NinjaTrader.Custom.csproj"
INST="${USERPROFILE:-$HOME}\\Documents\\NinjaTrader 8\\bin\\Custom\\Indicators"
RUN="$(cygpath -w "${TEMP:-/tmp}")\\ntcheck_$$"; RUNU="$(cygpath -u "$RUN")"; mkdir -p "$RUNU"
{
  echo '<Project><ItemGroup>'
  for f in "$@"; do
    b="$(basename "$f")"
    echo "<Compile Remove=\"$INST\\$b\" /><Compile Remove=\"AddOns\\$b\" /><Compile Remove=\"Strategies\\$b\" />"
    echo "<Compile Include=\"$(cygpath -w "$(realpath "$f")")\" />"
  done
  echo '</ItemGroup></Project>'
} > "$RUNU/files.props"
MINE="$(for f in "$@"; do basename "$f"; done | paste -sd'|' -)"
[ -z "$MINE" ] && MINE="__none__"
# DocumentationFile is set explicitly in the csproj (bin\Release\NinjaTrader.Custom.XML) and is NOT moved by -o: without the
# override every run writes that one file inside the real NT8 folder, so two concurrent checks collide on it.
dotnet build "$NTPROJ" -nologo -v q -clp:NoSummary -p:CustomAfterMicrosoftCommonTargets="$RUN\\files.props" -p:BaseIntermediateOutputPath="$RUN\\obj\\" -p:DocumentationFile="$RUN\\out\\NinjaTrader.Custom.XML" -o "$RUN\\out" 2>&1 \
  | grep -E "error|warning CS" | grep -vE "warning CS(0436|3002|3003|3009|1591)" | sed 's/ \[.*csproj\]//' | grep -E "error|($MINE)" | sort -u
rc=${PIPESTATUS[0]}
rm -rf "$RUNU"
test $rc -eq 0 && echo "CHECK_OK"
