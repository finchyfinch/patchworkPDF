"""Shared path setup + a tiny standalone runner for the regression tests.

Importing this puts ``src/backend`` on sys.path.  ``run_module_tests(globals())``
lets each test file double as a script: ``python tests/test_x.py`` runs every
``test_*`` function and reports pass/fail, so the suite is verifiable even in an
environment without pytest.  Under pytest the ``test_*`` functions are collected
normally and this runner is never invoked.
"""
import os
import sys
import traceback

_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'backend')
if os.path.abspath(_BACKEND) not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))


def _isolate_user_state():
    """Point the per-user state dir at a throwaway folder for the whole run.

    `platform_paths.user_data_dir()` is the REAL app's folder
    (%LOCALAPPDATA%\\PatchworkPDF and equivalents).  Tests that mark pages dirty
    left crash-recovery snapshots of their fixtures there ("a.pdf, page 1"), so
    the next real launch opened on a Recover dialog full of test junk; the log,
    recent files and What's New state were exposed the same way.  Every test
    module imports this file first, before any backend module, and the state
    dir is resolved at call time — so redirecting the environment here is
    enough, under pytest and standalone alike.
    """
    if os.environ.get('PATCHWORK_TEST_STATE_DIR'):
        return                                   # already isolated (nested import)
    import atexit
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix='pw_test_state_')
    os.environ['PATCHWORK_TEST_STATE_DIR'] = d
    os.environ['LOCALAPPDATA'] = d               # Windows
    os.environ['XDG_DATA_HOME'] = d              # Linux / Flatpak
    if sys.platform == 'darwin':                 # macOS: ~/Library/Application Support
        os.environ['HOME'] = d
    atexit.register(shutil.rmtree, d, True)


_isolate_user_state()


def _skipped_exc():
    """pytest's Skipped, when pytest is importable.  It derives from
    BaseException, not Exception, so a bare `except Exception` does NOT catch it
    — a test that calls pytest.skip() for a missing optional dependency would
    crash this runner instead of reporting a skip."""
    try:
        from _pytest.outcomes import Skipped
        return Skipped
    except ImportError:
        return ()


def run_module_tests(ns):
    """Run every test_* callable in namespace *ns*; return process exit code.

    A test may skip itself (pytest.skip, or a plain `return` after printing
    SKIP) — that is reported, never counted as a failure.  tools/run_tests.py
    looks for "SKIP" in this output to flag the suite as skipped, which
    --strict then turns into a build failure."""
    skipped_exc = _skipped_exc()
    tests = sorted((n, f) for n, f in ns.items()
                   if n.startswith('test_') and callable(f))
    failed = skipped = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except skipped_exc as e:                # pytest.skip() called outside pytest
            skipped += 1
            print(f"SKIP  {name}: {e}")
        except Exception:                       # noqa: BLE001 — report and continue
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    tail = f" ({skipped} skipped)" if skipped else ""
    print(f"\n{len(tests) - failed - skipped}/{len(tests)} passed{tail}")
    return 1 if failed else 0
