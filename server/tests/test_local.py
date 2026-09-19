"""Local tools (nt8_mcp.tools_local): no live NT8, no dotnet, no real keystrokes —
subprocess.run, the window lookup and NT8's paths are stubbed.
"""

import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]

from fake_addon import FakeAddon  # noqa: E402
from nt8_mcp import server as nt8, tools_local  # noqa: E402

PARTIAL_ADDON = "namespace NinjaTrader.NinjaScript.AddOns\n{\n\tpublic partial class NT8Bridge { }\n}\n"

# nt_install pauses 3-9 s for NT8's folder watcher. Swap the module reference tools_local
# looks `time` up in — never time.sleep itself, which every other thread shares.
_NO_SLEEP = types.SimpleNamespace(sleep=lambda s: None, time=time.time)


def test_nt_check_missing_file():
    # dotnet would fail on a nonexistent Compile Include; simulate that failure
    # instead of actually invoking dotnet.
    class FakeProc:
        returncode = 1
        stdout = (
            "Indicators\\DoesNotExist.cs : error CS2001: "
            "Source file 'Indicators\\DoesNotExist.cs' could not be found\n"
        )
        stderr = ""

    original_run = tools_local.subprocess.run
    tools_local.subprocess.run = lambda *a, **k: FakeProc()
    try:
        result = nt8.nt_check(["Indicators/DoesNotExist.cs"])
    finally:
        tools_local.subprocess.run = original_run

    assert result["ok"] is False
    assert any("error" in line for line in result["lines"]), result["lines"]


def test_nt_compile_f5_waits_for_dll_mtime():
    # No real NT8, no real keystrokes: stub the window lookup, the foreground/F5 calls, and
    # point NT_CUSTOM_DLL at a temp file whose mtime a background thread bumps after 0.5s.
    run = tempfile.mkdtemp(prefix="ntcompile_")
    fake_dll = os.path.join(run, "NinjaTrader.Custom.dll")
    with open(fake_dll, "w") as fh:
        fh.write("x")
    old = os.path.getmtime(fake_dll) - 10 * 86400  # older than any real .cs, so the F5 wait path runs
    os.utime(fake_dll, (old, old))

    saved = (tools_local.NT_CUSTOM_DLL, tools_local._find_windows, tools_local._send_f5)
    tools_local.NT_CUSTOM_DLL = fake_dll
    tools_local._find_windows = lambda title_substr: [(12345, "NinjaScript Editor - Foo.cs")]
    tools_local._send_f5 = lambda hwnd: None

    def _bump_mtime():
        time.sleep(0.5)
        now = time.time()
        os.utime(fake_dll, (now, now))

    threading.Thread(target=_bump_mtime, daemon=True).start()
    try:
        # nt_compile_f5 reports /health when the dll lands: answer it from the fake, never from
        # whatever is really listening on :7891 — no test in this suite may need a live NT8.
        with FakeAddon() as fake:
            fake.json("/health", {"ok": True})
            result = nt8.nt_compile_f5(timeout_s=5)
    finally:
        tools_local.NT_CUSTOM_DLL, tools_local._find_windows, tools_local._send_f5 = saved
        shutil.rmtree(run, ignore_errors=True)

    assert result["ok"] is True, result
    assert "dll_mtime" in result


def test_strip_generated_region_is_line_anchored():
    # A comment that merely MENTIONS the region must not truncate the file (it used to, silently).
    run = tempfile.mkdtemp(prefix="ntregion_")
    path = os.path.join(run, "Foo.cs")
    body = (
        "// never write #region NinjaScript generated code twice\n"
        "public class Foo { int keepMe = 1; }\n"
        "\t#region NinjaScript generated code. Neither change nor remove.\n"
        "namespace Generated { }\n"
        "#endregion\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        assert nt8._strip_generated_region(path) is True
        with open(path, encoding="utf-8") as fh:
            kept = fh.read()
        assert "keepMe" in kept and "namespace Generated" not in kept, kept
        assert kept.startswith("// never write"), kept
        assert os.path.exists(path + ".bak")
        assert nt8._strip_generated_region(path) is False  # nothing left to cut
    finally:
        shutil.rmtree(run, ignore_errors=True)


def test_folder_for_partial_addon_file():
    # A multi-file AddOn's partial files name no base class; they must still route to AddOns.
    # If this regresses, nt_install puts a partial in Custom\Indicators while install-addon.ps1
    # puts it in Custom\AddOns: duplicate members, CS0111, and every custom indicator unloads.
    assert nt8._folder_for(PARTIAL_ADDON) == "AddOns"
    assert nt8._folder_for("public class X : AddOnBase { }") == "AddOns"
    assert nt8._folder_for("namespace NinjaTrader.NinjaScript.Strategies { public class S : Strategy { } }") == "Strategies"
    assert nt8._folder_for("namespace NinjaTrader.NinjaScript.Indicators { public class I : Indicator { } }") == "Indicators"


def test_nt_install_multi_file_addon_all_land_in_addons():
    # nt_install takes the whole multi-file AddOn in one call; every partial goes to AddOns.
    run = tempfile.mkdtemp(prefix="ntinstall_")
    custom = os.path.join(run, "Custom")
    for folder in nt8._SCRIPT_FOLDERS:
        os.makedirs(os.path.join(custom, folder))
    srcs = []
    for name in ("NT8Bridge.cs", "NT8Bridge.Charts.cs", "NT8Bridge.Backtest.cs"):
        path = os.path.join(run, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(PARTIAL_ADDON)
        srcs.append(path)

    saved = (tools_local.NT_CUSTOM, tools_local.time)
    tools_local.NT_CUSTOM, tools_local.time = custom, _NO_SLEEP
    try:
        results = nt8.nt_install(srcs)
    finally:
        tools_local.NT_CUSTOM, tools_local.time = saved
        shutil.rmtree(run, ignore_errors=True)

    assert {r["folder"] for r in results} == {"AddOns"}, results
    assert all(r["action"] == "copied" for r in results), results


def test_nt_install_addon_sweeps_orphans():
    # An NT8Bridge*.cs left over from an older split must be deleted, or its duplicate members
    # keep NinjaTrader.Custom red forever. Files that are not ours are never touched.
    run = tempfile.mkdtemp(prefix="ntsweep_")
    custom = os.path.join(run, "Custom")
    addons = os.path.join(custom, "AddOns")
    for folder in nt8._SCRIPT_FOLDERS:
        os.makedirs(os.path.join(custom, folder))
    source = os.path.join(run, "addon")
    os.makedirs(source)
    for name in ("NT8Bridge.cs", "NT8Bridge.Charts.cs"):
        with open(os.path.join(source, name), "w", encoding="utf-8") as fh:
            fh.write(PARTIAL_ADDON)
    for name in ("NT8Bridge.Stale.cs", "NT8Bridge_Probe.cs", "MyIndicator.cs", "NT8Bridge.cs.bak"):
        with open(os.path.join(addons, name), "w", encoding="utf-8") as fh:
            fh.write("// not ours\n")

    saved = (tools_local.NT_CUSTOM, tools_local.ADDON_DIR, tools_local.time)
    tools_local.NT_CUSTOM = custom
    tools_local.ADDON_DIR = pathlib.Path(source)
    tools_local.time = _NO_SLEEP
    try:
        result = nt8.nt_install_addon()
        landed = sorted(os.listdir(addons))
    finally:
        tools_local.NT_CUSTOM, tools_local.ADDON_DIR, tools_local.time = saved
        shutil.rmtree(run, ignore_errors=True)

    assert result["ok"] is True, result
    assert result["removedOrphans"] == ["NT8Bridge.Stale.cs", "NT8Bridge_Probe.cs"], result
    assert landed == ["MyIndicator.cs", "NT8Bridge.Charts.cs", "NT8Bridge.cs", "NT8Bridge.cs.bak"], landed


def test_nt_install_addon_sweeps_before_it_copies():
    # install-addon.ps1 deletes orphans and only then copies. Sweeping afterwards leaves the new core
    # AND a stale partial in the folder for the seconds nt_install sleeps — long enough for NT8's
    # folder watcher to compile both: CS0111, NinjaTrader.Custom red, every user indicator unloaded.
    run = tempfile.mkdtemp(prefix="ntorder_")
    addons = os.path.join(run, "Custom", "AddOns")
    source = os.path.join(run, "addon")
    os.makedirs(addons)
    os.makedirs(source)
    with open(os.path.join(source, "NT8Bridge.cs"), "w", encoding="utf-8") as fh:
        fh.write(PARTIAL_ADDON)
    with open(os.path.join(addons, "NT8Bridge.Stale.cs"), "w", encoding="utf-8") as fh:
        fh.write("// orphan\n")

    seen = []
    saved = (tools_local.NT_CUSTOM, tools_local.ADDON_DIR, tools_local.nt_install)
    tools_local.NT_CUSTOM = os.path.join(run, "Custom")
    tools_local.ADDON_DIR = pathlib.Path(source)
    tools_local.nt_install = lambda files: seen.append(sorted(os.listdir(addons))) or []
    try:
        result = nt8.nt_install_addon()
    finally:
        tools_local.NT_CUSTOM, tools_local.ADDON_DIR, tools_local.nt_install = saved
        shutil.rmtree(run, ignore_errors=True)

    assert result["removedOrphans"] == ["NT8Bridge.Stale.cs"], result
    assert seen == [[]], f"the orphan was still there when nt_install ran: {seen}"


def test_nt_install_addon_refuses_a_misrouted_file():
    # A file that would land outside Custom\AddOns is refused before anything is copied.
    run = tempfile.mkdtemp(prefix="ntmisroute_")
    source = os.path.join(run, "addon")
    os.makedirs(source)
    with open(os.path.join(source, "NT8Bridge.cs"), "w", encoding="utf-8") as fh:
        fh.write("public class NT8Bridge { }\n")  # no AddOns namespace, no : AddOnBase

    saved = tools_local.ADDON_DIR
    tools_local.ADDON_DIR = pathlib.Path(source)
    try:
        result = nt8.nt_install_addon()
    finally:
        tools_local.ADDON_DIR = saved
        shutil.rmtree(run, ignore_errors=True)

    assert result["ok"] is False and "NT8Bridge.cs" in result["error"], result


def _png(name: str) -> str:
    path = os.path.join(tempfile.gettempdir(), name)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n")
    return path


def test_nt_shot_asks_the_addon_first():
    seen = {}
    path = _png("nt8_shot_addon.png")

    def handler(req):
        seen["body"] = req.body
        return {"ok": True, "path": path}

    saved = tools_local._capture_shot
    tools_local._capture_shot = lambda *a: (_ for _ in ()).throw(AssertionError("shot.ps1 must not run"))
    try:
        with FakeAddon() as fake:
            fake.register("/screenshot", "POST", handler)
            result = nt8.nt_shot(title="Control Center")
    finally:
        tools_local._capture_shot = saved
        os.remove(path)
    assert '"window": "Control Center"' in seen["body"]
    assert isinstance(result, nt8.Image) and str(result.path) == path


def test_nt_shot_falls_back_to_the_script_when_the_addon_refuses():
    # The AddOn never restores a minimized window; shot.ps1 does, so a refusal is not the end.
    out = os.path.join(tempfile.gettempdir(), "nt8_shot_fallback.png")
    called = []

    def fake_capture(title, out_path):
        called.append(title)
        with open(out_path, "wb") as fh:
            fh.write(b"\x89PNG\r\n")
        return types.SimpleNamespace(stdout="", stderr="")

    saved = tools_local._capture_shot
    tools_local._capture_shot = fake_capture
    try:
        with FakeAddon() as fake:
            fake.json("/screenshot", {"error": "window is minimized"}, method="POST", status=400)
            result = nt8.nt_shot(title="Control Center", out=out)
    finally:
        tools_local._capture_shot = saved
        os.remove(out)
    assert called == ["Control Center"]
    assert isinstance(result, nt8.Image) and str(result.path) == out
