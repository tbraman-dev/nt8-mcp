"""Run the nt8_mcp test suite. No pytest.

    python server/tests/run_all.py                 every tests/test_*.py
    python server/tests/run_all.py test_cli.py     just those files (name or path)

Discovers tests/test_*.py, calls every module-level `test_*` function in source order,
prints one line per test and a summary. Exit code 0 only if nothing failed.
"""

import glob
import importlib.util
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(_HERE, "..")]  # tests/ (fake_addon), server/ (nt8_mcp)


def _resolve(arg: str) -> str:
    for candidate in (arg, arg + ".py", os.path.join(_HERE, arg), os.path.join(_HERE, arg + ".py")):
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    raise SystemExit(f"no such test file: {arg}")


def _load(path: str):
    name = "nt8tests_" + os.path.basename(path)[:-3]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tests(module):
    fns = [v for k, v in vars(module).items() if k.startswith("test_") and callable(v)]
    return sorted(fns, key=lambda f: getattr(f, "__code__", None) and f.__code__.co_firstlineno or 0)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    paths = [_resolve(a) for a in argv] if argv else sorted(glob.glob(os.path.join(_HERE, "test_*.py")))
    if not paths:
        print("no test files found")
        return 1

    passed, failed = 0, []
    started = time.time()
    for path in paths:
        name = os.path.basename(path)
        try:
            module = _load(path)
        except Exception:
            failed.append((name, "<import>", traceback.format_exc()))
            print(f"FAIL  {name} <import>")
            continue
        for fn in _tests(module):
            try:
                fn()
            except Exception:
                failed.append((name, fn.__name__, traceback.format_exc()))
                print(f"FAIL  {name}::{fn.__name__}")
            else:
                passed += 1
                print(f"ok    {name}::{fn.__name__}")

    for name, test, tb in failed:
        print(f"\n=== {name}::{test} ===\n{tb}", end="")
    print(f"\n== {passed} passed, {len(failed)} failed in {time.time() - started:.1f}s ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
