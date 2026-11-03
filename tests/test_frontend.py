"""Pytest entry point for the front-end regression suites in tests/js/.

Those behaviours live in JavaScript, so the assertions do too: each
``tests/js/*.test.mjs`` loads the real ``src/frontend/js`` code over a headless
DOM and drives it (see tests/README.md).  This runs them with Node, which is a
dev-only dependency — the suites skip rather than fail when it isn't installed.
"""
import glob
import os
import shutil
import subprocess

import _bootstrap  # noqa: F401  (path setup, and lets this file run standalone)

_JS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'js')


def _suites():
    return sorted(glob.glob(os.path.join(_JS_DIR, '*.test.mjs')))


def test_frontend_suites():
    node = shutil.which('node')
    if node is None:
        try:
            import pytest
            pytest.skip('Node.js not on PATH — front-end suites skipped')
        except ImportError:
            print('SKIP  Node.js not on PATH — front-end suites skipped')
            return

    suites = _suites()
    assert suites, 'no front-end suites found in tests/js/'
    failures = []
    for path in suites:
        proc = subprocess.run([node, path], capture_output=True, text=True)
        print(f'--- {os.path.basename(path)}')
        print(proc.stdout.rstrip())
        if proc.returncode != 0:
            failures.append(f'{os.path.basename(path)}:\n{proc.stdout}{proc.stderr}')
    assert not failures, 'front-end suite(s) failed:\n' + '\n'.join(failures)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
