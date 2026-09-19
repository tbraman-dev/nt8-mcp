r"""Pins the `nt_check` vs in-NT8-compile gap with three .cs fixtures.

`nt_check` is a real `dotnet build` of NinjaTrader.Custom.csproj: same compiler and the same
references as pressing F5, but NinjaTrader itself is never involved. So it catches everything the
C# compiler catches and NOTHING that only NT8's own load step sees. The three fixtures fix that
boundary in place:

  GoodStrategy.cs       compiles clean offline, loads clean in NT8            -> nt_check ok
  BadCompileStrategy.cs does not compile                                      -> nt_check fails, CS0103
  BadLoadStrategy.cs    compiles clean offline, silently mis-loads in NT8     -> nt_check ok (the gap)

BadLoadStrategy is the point of this file. It sets `BacktestCommissionTemplate = "DoesNotExist"`
in SetDefaults. The name of a commission template is data that lives inside NinjaTrader; no
compiler can check it, so `nt_check` reports ok. Whether NT8 actually throws when it instantiates
the type was once UNVERIFIED; observed on NT8 8.1.8.2: an unknown template is accepted
**silently** and the DEFAULT commission template is charged instead — there is no load rejection
at all, so the read-back cannot catch it either. The AddOn now answers `400` for an unknown
template name at `POST /backtest`, closing the gap at the boundary this repo controls. This is
the gap between nt_check and pressing F5 in NT8, which README.md does not state explicitly (the
closest existing wording is README.md:96, "nothing in NT8 is touched"). This test proves only the
OFFLINE half — that nt_check really does say ok; the live behaviour above was observed directly on
NT8, not something this test can observe (these fixtures must never reach `bin\Custom`:
BadCompileStrategy.cs alone breaks the whole tree, and NT8 compiles the whole tree).

Each nt_check is a full dotnet build (tens of seconds), so those three are marked slow:
NT8_SKIP_SLOW=1 skips them and leaves the fast header check running.
"""

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from nt8_mcp import app, tools_local  # noqa: E402

FIX = os.path.join(_HERE, "fixtures")
GOOD = os.path.join(FIX, "GoodStrategy.cs")
BAD_COMPILE = os.path.join(FIX, "BadCompileStrategy.cs")
BAD_LOAD = os.path.join(FIX, "BadLoadStrategy.cs")


def _skip_reason() -> str | None:
    """None = run it. Anything else is printed and the test returns."""
    if os.environ.get("NT8_SKIP_SLOW") == "1":
        return "NT8_SKIP_SLOW=1"
    if shutil.which("dotnet") is None:
        return "dotnet not on PATH"
    if not os.path.isfile(app.NT_CSPROJ):
        return f"no csproj at {app.NT_CSPROJ}"
    return None


def _check(path: str) -> dict:
    """nt_check on one fixture, or None when the offline compiler is unavailable."""
    reason = _skip_reason()
    if reason:
        print(f"      skip (slow): {reason}")
        return None
    return tools_local.nt_check([path])


def test_fixtures_carry_attribution_and_never_install_warning():
    """Copying a fixture without the cli-nt-bridge header is a license violation,
    and a fixture that loses its never-install line is one paste away from breaking the tree."""
    for path in (GOOD, BAD_COMPILE, BAD_LOAD):
        with open(path, encoding="utf-8") as fh:
            head = fh.read(1200)
        assert "derived from cli-nt-bridge" in head, path
        assert "MIT License" in head, path
        assert "TEST FIXTURE" in head and "NEVER install" in head, path


def test_nt8_skip_slow_env_var_skips_the_dotnet_builds():
    """The three builds below are minutes on a cold tree; NT8_SKIP_SLOW=1 must really skip them."""
    saved = os.environ.get("NT8_SKIP_SLOW")
    os.environ["NT8_SKIP_SLOW"] = "1"
    try:
        assert _skip_reason() == "NT8_SKIP_SLOW=1"
        assert _check(GOOD) is None
    finally:
        if saved is None:
            del os.environ["NT8_SKIP_SLOW"]
        else:
            os.environ["NT8_SKIP_SLOW"] = saved


def test_good_strategy_passes_nt_check():
    """slow. The control: a valid strategy must not produce errors, or the other two prove nothing.
    Asserts on the fixture itself, not on result["ok"]: nt_check builds the WHOLE installed
    NinjaTrader.Custom tree plus this one fixture, so result["ok"] is a property of the user's
    whole bin\\Custom tree (a broken third-party script fails it too) and would blame this fixture
    for someone else's compile error."""
    result = _check(GOOD)
    if result is None:
        return
    bad = [l for l in result["lines"] if "GoodStrategy.cs" in l]
    assert not bad, bad


def test_bad_compile_strategy_fails_nt_check_with_cs_error_naming_the_file():
    """slow. nt_check must fail AND point at the file: a bare ok:false is not actionable."""
    result = _check(BAD_COMPILE)
    if result is None:
        return
    assert result["ok"] is False, result
    blob = "\n".join(result["lines"])
    assert "CS0103" in blob, blob
    assert "BadCompileStrategy.cs" in blob, blob


def test_bad_load_strategy_passes_nt_check_although_nt8_rejects_it():
    """slow. THE GAP (offline half only — see module docstring: whether NT8 actually rejects this
    at load is covered by the module docstring's observed result). Asserts on the fixture itself, not on result["ok"]: see
    test_good_strategy_passes_nt_check for why. If this ever starts failing, the offline checker
    got stricter (good news) — re-read the module docstring before 'fixing' it."""
    result = _check(BAD_LOAD)
    if result is None:
        return
    bad = [l for l in result["lines"] if "BadLoadStrategy.cs" in l]
    assert not bad, (
        "BadLoadStrategy no longer compiles offline, so it no longer pins the "
        "nt_check-vs-NT8-load gap: " + "\n".join(bad)
    )
