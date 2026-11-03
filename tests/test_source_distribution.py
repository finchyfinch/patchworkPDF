"""Completeness guards for `build_source_distribution.py`'s payload.

The payload is a hand-maintained list of what goes into a published source
distribution.  Nothing checks it against the tree, so a file added to the repo
is simply absent from every release until somebody notices — and the failure
lands on whoever downloaded the distribution, not on the person who built it.

The one that got through: `tools/run_tests.py` was never listed.  It is the
pre-flight gate all three platform build scripts run before packaging anything
(`build_installer.bat`, `make_dmg.sh`, `build_flatpak.sh`), and a missing script
exits 2 — so every build from a distribution aborted at step 1 reporting "tests
failed", with the tests themselves perfectly fine.  `pytest.ini` was missing for
the same reason, so `pytest` in a distribution root did not collect the same set.

These check the payload against what the tree and the shipped scripts actually
need, rather than against a second hand-maintained list.

Run: pytest tests/test_source_distribution.py   (or: python tests/test_source_distribution.py)
"""
import ast
import os
import re

import _bootstrap  # noqa: F401  (path setup)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUILDER = os.path.join(_REPO, 'build_source_distribution.py')


def _need_builder():
    """These guard the payload where it is authored — in the working tree.  The
    builder is a maintainer tool and deliberately not part of what it ships, so
    in a source DISTRIBUTION it is simply absent and there is nothing to check.
    Skip there rather than fail: the distribution's build scripts run this whole
    suite as their pre-flight gate, and failing it would abort the build over a
    file the recipient was never meant to have."""
    if os.path.isfile(_BUILDER):
        return True
    try:
        import pytest
        pytest.skip('build_source_distribution.py not present — '
                    'source distribution, not the working tree')
    except ImportError:
        print('SKIP  build_source_distribution.py not present — '
              'source distribution, not the working tree')
    return False


# ── read the payload without importing the module (it pulls in wx) ───────────
def _payload():
    with open(_BUILDER, 'r', encoding='utf-8') as f:
        src = f.read()
    body = src[src.index('def get_payload'):]
    lst = body[body.index('['):]
    depth = 0
    for i, ch in enumerate(lst):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                lst = lst[:i + 1]
                break
    return ast.literal_eval(re.sub(r'#.*', '', lst))


def _ignore_globs():
    """The builder's own junk filter, read from the module rather than copied."""
    with open(_BUILDER, 'r', encoding='utf-8') as f:
        src = f.read()
    m = re.search(r'IGNORE_GLOBS\s*=\s*(\([^)]*\))', src)
    assert m, 'build_source_distribution.py must define IGNORE_GLOBS'
    return ast.literal_eval(m.group(1))


def _is_junk(rel):
    from fnmatch import fnmatch
    return any(fnmatch(part, pat)
               for part in rel.split('/') for pat in _ignore_globs())


def _shipped():
    """Every repo-relative path the payload puts in the distribution."""
    out = set()
    for name, _src, kind in _payload():
        path = os.path.join(_REPO, name)
        if kind == 'folder_all':
            for dirpath, _d, files in os.walk(path):
                for f in files:
                    rel = os.path.relpath(os.path.join(dirpath, f), _REPO).replace(os.sep, '/')
                    if not _is_junk(rel):
                        out.add(rel)
        elif kind == 'file':
            out.add(name.replace(os.sep, '/'))
    return out


# ── the payload describes real files ─────────────────────────────────────────

def test_every_payload_entry_exists():
    if not _need_builder():
        return
    absent = [name for name, _s, kind in _payload()
              if not (os.path.isdir if kind in ('folder_all', 'folder') else os.path.isfile)
              (os.path.join(_REPO, name))]
    assert not absent, (
        'the payload names files that are not in the tree, so the build logs a '
        'warning and silently ships without them: ' + ', '.join(absent))


# ── the test suite ships whole ───────────────────────────────────────────────

def test_the_whole_test_suite_ships():
    if not _need_builder():
        return
    shipped = _shipped()
    on_disk = set()
    for dirpath, _d, files in os.walk(os.path.join(_REPO, 'tests')):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), _REPO).replace(os.sep, '/')
            if not _is_junk(rel):
                on_disk.add(rel)
    missing = sorted(on_disk - shipped)
    assert not missing, (
        'a distribution that cannot run its own tests cannot be verified by '
        'whoever receives it: ' + ', '.join(missing))
    assert any(p.endswith('.test.mjs') for p in on_disk), \
        'the front-end suites are part of the tests, not an optional extra'


def test_the_build_gate_and_its_config_ship():
    if not _need_builder():
        return
    shipped = _shipped()
    for path, why in [
        ('tools/run_tests.py',
         'every platform build script runs it before packaging; a missing '
         'script exits 2 and the build aborts reporting "tests failed"'),
        ('pytest.ini',
         'without it a plain `pytest` in the distribution root does not collect '
         'the same set the gate runs'),
    ]:
        assert path in shipped, f'{path} must ship — {why}'


# ── whatever the shipped scripts reference must ship too ─────────────────────

def test_shipped_build_scripts_only_reference_files_that_ship():
    """Parse repo-relative paths out of the build scripts and setup.py, and
    check the payload carries each one.  Catches the whole class of "the script
    ships but the file it calls does not" rather than the one instance of it."""
    if not _need_builder():
        return
    shipped = _shipped()
    scripts = [p for p in shipped
               if (p.startswith('packaging/') and p.rsplit('.', 1)[-1] in ('sh', 'bat', 'iss', 'py'))
               or p == 'setup.py']
    assert scripts, 'the payload should be shipping the build scripts'

    pat = re.compile(r"""['"( ]((?:src|tests|tools|assets|locales|docs)[\w\-./\\]+)""")
    missing = set()
    for rel in scripts:
        with open(os.path.join(_REPO, rel), 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
        for hit in pat.findall(text):
            ref = hit.replace('\\', '/').rstrip('/.,;:)')
            if '*' in ref or '%' in ref or '$' in ref:
                continue                        # a glob or a variable, not a path
            if not os.path.exists(os.path.join(_REPO, ref)):
                continue                        # not a real path in this tree either
            if ref in shipped or any(s.startswith(ref.rstrip('/') + '/') for s in shipped):
                continue
            missing.add(f'{ref} (referenced by {rel})')
    assert not missing, (
        'shipped build scripts call files the payload leaves behind:\n  ' +
        '\n  '.join(sorted(missing)))


# ── and nothing that must not ship, does ─────────────────────────────────────

def test_build_residue_is_excluded():
    """A 'folder_all' entry copies a tree wholesale.  Without the ignore filter
    the working copy's __pycache__, stale .pyc files and egg-info metadata all
    reach the distribution — and a stale .pyc beside an edited .py is worse than
    noise, because Python will import it."""
    if not _need_builder():
        return
    globs = _ignore_globs()
    for pat in ('__pycache__', '*.pyc', '*.egg-info'):
        assert pat in globs, f'{pat} must be filtered out of a source distribution'

    leaked = sorted(p for p in _shipped() if _is_junk(p))
    assert not leaked, 'build residue reached the distribution: ' + ', '.join(leaked[:10])


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
