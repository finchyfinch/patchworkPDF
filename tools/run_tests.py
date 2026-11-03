#!/usr/bin/env python3
"""
run_tests.py — run the whole regression suite and report a single verdict.

This is the pre-flight gate every installer build runs before it packages
anything (Windows, macOS and Linux all call it), so a release can never be cut
from a tree whose tests are failing.  It is also the one command to reach for
when you just want to know whether the tree is healthy:

    python tools/run_tests.py             # run everything
    python tools/run_tests.py --strict    # skipped suites also fail the run
    python tools/run_tests.py --quiet     # summary + failures only

It runs the same tests either way pytest is present or not: with pytest it
delegates to it, otherwise it executes each ``tests/test_*.py`` as the
standalone script that file is also written to be.  Build machines rarely have
pytest, and a build must not silently skip its own gate for that.

Stdlib only, so it runs on a bare interpreter.  Exit code is 0 when everything
passed and 1 otherwise — that is what the build scripts branch on.
"""

import argparse
import os
import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

# A suite that reports itself skipped (rather than passed) — a missing optional
# tool, not a failure.  Node, for the front-end suites, is the only one today.
_SKIP_MARKERS = ("SKIP ", "skipped")


# Third-party packages the suites (and the backend modules they import) need.
# Checked up front so a build machine missing them gets one clear line instead of
# a wall of import tracebacks — this bites on a fresh Linux/macOS build VM, where
# the Flatpak/DMG tooling is installed but the app's Python deps are not.
_REQUIRED = {
    "pikepdf":    "pikepdf",
    "pypdf":      "pypdf",
    "PIL":        "Pillow",
    "numpy":      "numpy",
    "pypdfium2":  "pypdfium2",
    "flask":      "flask",
    "docx":       "python-docx",
}


def _missing_dependencies():
    missing = []
    for module, package in _REQUIRED.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def _have_pytest():
    try:
        import pytest  # noqa: F401
        return True
    except ImportError:
        return False


def _run(cmd, quiet):
    """Run *cmd* from the repo root; return (returncode, combined output)."""
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if not quiet:
        sys.stdout.write(proc.stdout)
        sys.stdout.flush()
    return proc.returncode, proc.stdout


def _test_modules():
    return sorted(TESTS_DIR.glob("test_*.py"))


def run_with_pytest(quiet):
    print("Runner: pytest")
    code, out = _run([sys.executable, "-m", "pytest", "-q" if quiet else "-v"], quiet)
    skipped = "skipped" in out
    return [("pytest (whole suite)", code == 0, skipped, out)]


def run_standalone(quiet):
    """Run each test module as a script — the no-pytest path."""
    print("Runner: standalone (pytest not installed)")
    results = []
    for path in _test_modules():
        name = path.name
        if not quiet:
            print(f"\n--- {name}")
        code, out = _run([sys.executable, str(path)], quiet)
        skipped = any(m in out for m in _SKIP_MARKERS)
        results.append((name, code == 0, skipped, out))
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--strict", action="store_true",
                    help="treat a skipped suite (e.g. the front-end tests when "
                         "Node is missing) as a failure")
    ap.add_argument("--quiet", action="store_true",
                    help="only print the summary and any failures")
    ap.add_argument("--no-pytest", action="store_true",
                    help="use the standalone runner even if pytest is installed")
    args = ap.parse_args(argv)

    modules = _test_modules()
    if not modules:
        print(f"ERROR: no test modules found in {TESTS_DIR}")
        return 1

    print("=" * 62)
    print("  PatchworkPDF - regression tests")
    print("=" * 62)
    print(f"repo   : {REPO_ROOT}")
    print(f"python : {sys.version.split()[0]} ({sys.executable})")
    print(f"suites : {len(modules)}")
    print()

    missing = _missing_dependencies()
    if missing:
        print("ERROR: the tests cannot run - these Python packages are missing:")
        print("       " + ", ".join(missing))
        print()
        print("       Install just these:")
        print("         python3 -m pip install " + " ".join(missing))
        print()
        print("       On a distro that manages its own site-packages (Ubuntu 24.04+,")
        print("       Fedora 39+) add:  --break-system-packages")
        print()
        print("       Do NOT install -r requirements.txt on a Linux build box: it")
        print("       pulls pywebview, whose GTK/WebKit bindings must come from the")
        print("       system, not pip.  The tests need only the packages above.")
        print()
        print("       (A build machine needs them even when the app itself is")
        print("        packaged in a sandbox - the tests run on THIS interpreter.)")
        return 1

    t0 = time.time()
    if _have_pytest() and not args.no_pytest:
        results = run_with_pytest(args.quiet)
    else:
        results = run_standalone(args.quiet)
    elapsed = time.time() - t0

    failed  = [name for name, ok, _sk, _o in results if not ok]
    skipped = [name for name, ok, sk, _o in results if ok and sk]

    print()
    print("-" * 62)
    for name, ok, sk, _out in results:
        state = "FAIL" if not ok else ("PASS (with skips)" if sk else "PASS")
        print(f"  {state:<18} {name}")
    print("-" * 62)

    if failed:
        print(f"\nFAILED: {len(failed)} of {len(results)} - {', '.join(failed)}")
        if args.quiet:
            print("\nOutput from the failing suite(s):")
            for name, ok, _sk, out in results:
                if not ok:
                    print(f"\n--- {name}\n{out}")
        print("\nThe build must not proceed until these pass.")
        return 1

    if skipped and args.strict:
        print(f"\nFAILED (--strict): {len(skipped)} suite(s) skipped - "
              f"{', '.join(skipped)}")
        print("Install the missing tool (Node.js for the front-end suites) "
              "and re-run.")
        return 1

    if skipped:
        print(f"\nNOTE: {len(skipped)} suite(s) reported skips - {', '.join(skipped)}")
        print("      The front-end tests need Node.js on PATH; without it they")
        print("      do not run.  Install Node to cover them, or use --strict")
        print("      to make a skip fail the build.")

    print(f"\nAll tests passed in {elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
