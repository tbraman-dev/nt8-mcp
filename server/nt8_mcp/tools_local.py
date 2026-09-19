"""Local tools: compile-check, install, F5-compile, screenshot-any-window, trace/log tail.

These touch the filesystem, dotnet and NT8's own windows directly, without going through
the AddOn's HTTP API.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from nt8_mcp.app import (
    NT_CSPROJ,
    NT_CUSTOM,
    NT_CUSTOM_DLL,
    NT_LOG_DIR,
    NT_TRACE_DIR,
    Image,
    _addon_get,
    _addon_post,
    _SCRIPT_FOLDERS,
    mcp,
)

# The repo's own addon/ folder — the source of truth nt_install_addon installs from.
ADDON_DIR = Path(__file__).resolve().parents[2] / "addon"

# dotnet build warnings that are noise on every build of the real Custom project,
# not something introduced by the files being checked (see check.sh this ports).
_IGNORED_WARNINGS = re.compile(r"warning CS(0436|3002|3003|3009|1591)")
_ERROR_OR_WARNING = re.compile(r"error|warning CS")
_CSPROJ_SUFFIX = re.compile(r" \[.*csproj\]")


def _filter_build_output(text: str, mine: str) -> list[str]:
    lines = [l for l in text.splitlines() if _ERROR_OR_WARNING.search(l)]
    lines = [l for l in lines if not _IGNORED_WARNINGS.search(l)]
    lines = [_CSPROJ_SUFFIX.sub("", l) for l in lines]
    mine_re = re.compile(mine)
    lines = [l for l in lines if "error" in l or mine_re.search(l)]
    return sorted(set(lines))


@mcp.tool(name="nt_check")
def nt_check(files: list[str] | None = None) -> dict:
    """Compile-check files against the real NinjaTrader.Custom.csproj in a scratch build dir — same compiler and
    references as pressing F5, nothing in NT8 touched. files=[] builds the install as-is. Returns {ok, lines}."""
    files = files or []
    run = tempfile.mkdtemp(prefix="ntcheck_")
    try:
        props = ['<Project><ItemGroup>']
        names = []
        for f in files:
            b = os.path.basename(f)
            names.append(b)
            for folder in _SCRIPT_FOLDERS:
                props.append(f'<Compile Remove="{os.path.join(NT_CUSTOM, folder, b)}" />')
            props.append(f'<Compile Include="{os.path.abspath(f)}" />')
        props.append('</ItemGroup></Project>')
        props_path = os.path.join(run, "files.props")
        with open(props_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(props))
        mine = "|".join(names) if names else "__none__"
        cmd = [
            "dotnet", "build", NT_CSPROJ, "-nologo", "-v", "q", "-clp:NoSummary",
            f"-p:CustomAfterMicrosoftCommonTargets={props_path}",
            f"-p:BaseIntermediateOutputPath={os.path.join(run, 'obj')}\\",
            # the csproj pins DocumentationFile inside the real NT8 folder and -o does not move it:
            # without this, concurrent checks collide on that one file
            f"-p:DocumentationFile={os.path.join(run, 'out', 'NinjaTrader.Custom.XML')}",
            "-o", os.path.join(run, "out"),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            return {"ok": False, "lines": ["dotnet not found on PATH"]}
        out = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "lines": _filter_build_output(out, mine)}
    finally:
        shutil.rmtree(run, ignore_errors=True)


def _folder_for(src_text: str) -> str:
    # The namespace test matters: a 'partial class' file of a multi-file AddOn names no base class,
    # and landing it in Indicators as well as AddOns duplicates every member and breaks the whole build.
    if ": AddOnBase" in src_text or "namespace NinjaTrader.NinjaScript.AddOns" in src_text:
        return "AddOns"
    if re.search(r"class\s+\w+\s*:\s*Strategy\b", src_text):
        return "Strategies"
    if ": SuperDomColumn" in src_text:
        return "SuperDomColumns"
    return "Indicators"


@mcp.tool(name="nt_install")
def nt_install(files: list[str]) -> list[dict]:
    """Copy .cs files into NT8's Custom/<AddOns|Strategies|SuperDomColumns|Indicators> (folder decided by class
    content). If [NinjaScriptProperty]/AddPlot() counts changed vs the installed copy, deletes it and recopies
    after a pause (NT8's folder watcher otherwise glues a stale generated region onto it), then strips that
    region so F5 regenerates it clean. Does not compile — call nt_compile after. Precondition: NT8 not fighting
    you for the file (close it in the NinjaScript Editor first)."""
    results = []
    pending: list[tuple[str, str, str]] = []  # (src, dst, name)
    for f in files:
        name = os.path.basename(f)
        with open(f, encoding="utf-8", errors="ignore") as fh:
            src_text = fh.read()
        folder = _folder_for(src_text)
        dst = os.path.join(NT_CUSTOM, folder, name)
        entry = {"file": name, "folder": folder}
        if os.path.exists(dst):
            with open(dst, encoding="utf-8", errors="ignore") as fh:
                old_text = fh.read()
            if (old_text.count("[NinjaScriptProperty]") != src_text.count("[NinjaScriptProperty]")
                    or old_text.count("AddPlot(") != src_text.count("AddPlot(")):
                os.remove(dst)
                pending.append((f, dst, name))
                entry["action"] = "removed (inputs changed), recopy pending"
                results.append(entry)
                continue
        shutil.copy2(f, dst)
        entry["action"] = "copied"
        results.append(entry)

    if pending:
        time.sleep(6)
        for f, dst, name in pending:
            shutil.copy2(f, dst)
            next(r for r in results if r["file"] == name)["action"] = (
                "replaced (inputs changed, remove/re-add on chart)"
            )

    time.sleep(3)
    for r in results:
        dst = os.path.join(NT_CUSTOM, r["folder"], r["file"])
        if os.path.exists(dst) and _strip_generated_region(dst):
            r["note"] = "stripped stale generated region (backup: .bak)"
    return results


@mcp.tool(name="nt_install_addon")
def nt_install_addon() -> dict:
    """Install the whole NT8Bridge AddOn: delete orphan NT8Bridge*.cs files in NT8's Custom/AddOns (a partial
    left behind from an older split keeps duplicate members in the build and takes NinjaTrader.Custom down
    with it), then copy every addon/*.cs of this repo there. Same job as scripts/install-addon.ps1.
    Does not compile — NT8 recompiles by itself 20-150 s after the files land. Returns
    {ok, installed, removedOrphans, dir}."""
    files = sorted(str(p) for p in ADDON_DIR.glob("*.cs"))
    if not files:
        return {"ok": False, "error": f"no .cs files in {ADDON_DIR}"}

    # Refuse rather than scatter: a partial that does not classify as an AddOn would land in
    # Custom\Indicators while install-addon.ps1 puts it in Custom\AddOns -> CS0111 -> dead build.
    misrouted = []
    for f in files:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            if _folder_for(fh.read()) != "AddOns":
                misrouted.append(os.path.basename(f))
    if misrouted:
        return {"ok": False, "error": f"would not land in Custom\\AddOns: {misrouted} — "
                                      "each addon/*.cs needs 'namespace NinjaTrader.NinjaScript.AddOns' or ': AddOnBase'"}

    # Delete THEN copy, like install-addon.ps1: nt_install pauses for seconds afterwards, and
    # NT8's folder watcher can fire in that window. Sweeping last would let it compile the new
    # core together with a stale partial -> CS0111 -> NinjaTrader.Custom red.
    keep = {os.path.basename(f) for f in files}
    dest = os.path.join(NT_CUSTOM, "AddOns")
    removed = []
    for name in sorted(os.listdir(dest)) if os.path.isdir(dest) else []:
        if name.startswith("NT8Bridge") and name.endswith(".cs") and name not in keep:
            os.remove(os.path.join(dest, name))
            removed.append(name)

    installed = nt_install(files)
    return {"ok": True, "dir": dest, "installed": installed, "removedOrphans": removed}


# Anchored to a line start: a plain substring match truncates a file at any COMMENT that merely
# mentions the region, silently deleting the code after it (found by cli-nt-bridge, regions.py).
_GENERATED_REGION = re.compile(r"^[ \t]*#region NinjaScript generated code", re.M)


def _strip_generated_region(path: str) -> bool:
    """Cut NT8's generated-code region off the end of a .cs file, keeping a .bak. True if it cut."""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        txt = fh.read()
    m = _GENERATED_REGION.search(txt)
    if not m:
        return False
    shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(txt[: m.start()].rstrip() + "\n")
    return True


def _find_windows(title_substr: str):
    """Visible top-level windows whose title contains title_substr (case-insensitive): [(hwnd, title), ...]."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_substr.lower() in buf.value.lower():
            found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(enum_proc, 0)
    return found


def _shot_script_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "scripts" / "shot.ps1")


def _capture_shot(title: str, out_path: str):
    """Run scripts/shot.ps1 to screenshot a window by title substring (restores it first if
    minimized) into out_path. Returns the completed process; check os.path.exists(out_path)
    for success."""
    cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", _shot_script_path(), "-Title", title, "-Out", out_path]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _send_f5(hwnd):
    """Post WM_KEYDOWN/WM_KEYUP for F5 straight to the editor window. The WPF editor handles
    it without focus, so no restore/foreground dance and other NT8 windows may sit on top."""
    import ctypes

    user32 = ctypes.windll.user32
    WM_KEYDOWN, WM_KEYUP, VK_F5 = 0x100, 0x101, 0x74
    scan = user32.MapVirtualKeyW(VK_F5, 0)
    user32.PostMessageW(hwnd, WM_KEYDOWN, VK_F5, (scan << 16) | 1)
    user32.PostMessageW(hwnd, WM_KEYUP, VK_F5, (scan << 16) | 1 | (1 << 30) | (1 << 31))


@mcp.tool(name="nt_compile_f5")
def nt_compile_f5(timeout_s: int = 240) -> dict:
    """Cold-start compile: press F5 in NT8's NinjaScript Editor and wait for NinjaTrader.Custom.dll to be
    rebuilt (proof of a successful compile — the AddOn's own startedAt is not used, since the AddOn may not
    be running yet or may have just been restarted by hand). Prefer nt_compile, which asks the AddOn and
    falls back to this automatically; use this one directly only when the AddOn is not loaded. Unlike
    nt_compile this is a REAL compile, so NinjaTrader swaps the assembly and restarts indicators when it
    succeeds. Precondition: a NinjaScript Editor window
    is open (Control Center > New > NinjaScript Editor); it may be minimized or covered. Returns
    {ok, seconds, dll_mtime, health} on success, where health is GET /health (or the not-reachable
    error if the AddOn isn't back up yet). On timeout returns {ok, seconds, error, editor_shot},
    where editor_shot is a screenshot of the editor window (likely showing a compile error)."""
    windows = _find_windows("NinjaScript Editor")
    if not windows:
        return {"ok": False, "error": "No NinjaScript Editor window found. Open Control Center > New > NinjaScript Editor."}
    hwnd = windows[0][0]

    before_mtime = os.path.getmtime(NT_CUSTOM_DLL) if os.path.exists(NT_CUSTOM_DLL) else None

    # NT8 recompiles on its own ~30-60 s after a .cs file lands in bin\Custom while the editor is
    # open. If the dll is already newer than every source file, that compile has happened: done.
    newest_src = max((os.path.getmtime(os.path.join(r, f)) for folder in _SCRIPT_FOLDERS
                      for r, _, fs in os.walk(os.path.join(NT_CUSTOM, folder)) for f in fs if f.endswith(".cs")), default=0)
    if before_mtime and before_mtime > newest_src:
        return {"ok": True, "seconds": 0, "dll_mtime": datetime.fromtimestamp(before_mtime).isoformat(),
                "note": "dll already newer than every .cs (NT8 auto-compiled); no F5 sent", "health": _addon_get("/health")}

    _send_f5(hwnd)

    start = time.time()
    while time.time() - start < timeout_s:
        time.sleep(1)
        if os.path.exists(NT_CUSTOM_DLL):
            mtime = os.path.getmtime(NT_CUSTOM_DLL)
            if mtime != before_mtime:
                time.sleep(2)  # the AddOn restarts a moment after the dll lands
                return {
                    "ok": True,
                    "seconds": round(time.time() - start, 1),
                    "dll_mtime": datetime.fromtimestamp(mtime).isoformat(),
                    "health": _addon_get("/health"),
                }

    editor_shot = os.path.join(tempfile.gettempdir(), "nt_compile_f5_editor.png")
    _capture_shot("NinjaScript Editor", editor_shot)
    return {
        "ok": False,
        "seconds": round(time.time() - start, 1),
        "error": "NinjaTrader.Custom.dll was not rebuilt; the compile probably failed",
        "editor_shot": editor_shot if os.path.exists(editor_shot) else None,
    }


@mcp.tool(name="nt_shot")
def nt_shot(title: str = "", out: str = ""):
    """Screenshot a top-level window by title substring (empty = whole virtual screen), returned as a PNG
    image. Captured in-process by the AddOn (PrintWindow, so a covered window still comes back whole);
    if the AddOn is not running, or refuses — it never restores a minimized window — this falls back to
    scripts/shot.ps1, which does restore a minimized window first. nt_window_shot is the AddOn-only
    version with chart/hwnd targeting."""
    out_path = out or os.path.join(tempfile.gettempdir(), "ntshot.png")
    body = {"path": out_path}
    if title:
        body["window"] = title
    result = _addon_post("/screenshot", body)
    if isinstance(result, dict) and result.get("path"):
        return Image(path=result["path"])

    proc = _capture_shot(title, out_path)
    if not os.path.exists(out_path):
        reason = result.get("error") if isinstance(result, dict) else result
        raise RuntimeError(f"screenshot failed: AddOn said {reason!r}; "
                           f"shot.ps1 said {(proc.stdout or '') + (proc.stderr or '')}")
    return Image(path=out_path)


def _newest_file(directory: str):
    if not os.path.isdir(directory):
        return None
    files = [os.path.join(directory, f) for f in os.listdir(directory)]
    files = [f for f in files if os.path.isfile(f)]
    return max(files, key=os.path.getmtime) if files else None


@mcp.tool(name="nt_trace")
def nt_trace(n: int = 100) -> dict:
    """Last n lines of the newest NT8 trace file and the newest NT8 log file, tagged by source."""
    out = {}
    for tag, directory in (("trace", NT_TRACE_DIR), ("log", NT_LOG_DIR)):
        f = _newest_file(directory)
        if f is None:
            out[tag] = {"file": None, "lines": []}
            continue
        with open(f, encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()[-n:]
        out[tag] = {"file": f, "lines": [l.rstrip("\n") for l in lines]}
    return out
