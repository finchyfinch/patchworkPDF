r"""Line-ending and encoding guards for the build scripts.

Both halves of this have bitten already, and both failed *far* from the edit:

  * A shell script edited on Windows can pick up CRLF endings — Python's
    ``pathlib.write_text()`` translates ``\n`` to ``\r\n`` on write, silently.
    Git Bash then runs the file fine, so a local check says "syntax OK", and the
    breakage only appears inside a Linux VM as
    ``$'\r': command not found`` followed by a syntax error.

  * A .bat file that is UTF-8 or LF-only misparses in cmd.exe.  cmd re-reads the
    file by byte offset after each command; multi-byte characters shift those
    offsets and it resumes mid-line, executing the tail of a line as a command.
    That silently skipped an ``if errorlevel`` check in the installer build —
    the build ran on with a failing test gate.

So: shell scripts must be LF and .bat files must be CRLF + ASCII.  These run on
the machine doing the editing, which is where the mistake is cheap to fix.
"""
import os

import _bootstrap  # noqa: F401  (path setup)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCAN_DIRS = ('packaging', 'tools')


def _iter_files():
    for top in _SCAN_DIRS:
        root_dir = os.path.join(_REPO, top)
        if not os.path.isdir(root_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(root_dir):
            for name in filenames:
                yield os.path.join(dirpath, name)


def _rel(path):
    return os.path.relpath(path, _REPO).replace(os.sep, '/')


def _is_shell_script(path):
    """A .sh file, or any extensionless file with a shell shebang (the Flatpak
    launcher `packaging/flatpak/patchworkpdf` is one)."""
    if path.lower().endswith('.sh'):
        return True
    if os.path.splitext(path)[1]:
        return False
    try:
        with open(path, 'rb') as f:
            first = f.readline()
    except OSError:
        return False
    return first.startswith(b'#!') and (b'sh' in first or b'bash' in first)


def shell_scripts():
    return sorted(p for p in _iter_files() if _is_shell_script(p))


def batch_files():
    return sorted(p for p in _iter_files() if p.lower().endswith('.bat'))


# ── shell scripts: LF only ───────────────────────────────────────────────────

def test_shell_scripts_are_lf_only():
    scripts = shell_scripts()
    assert scripts, 'no shell scripts found - has packaging/ moved?'
    offenders = []
    for path in scripts:
        with open(path, 'rb') as f:
            data = f.read()
        if b'\r' in data:
            offenders.append(f'{_rel(path)} ({data.count(b"\r")} CR bytes)')
    assert not offenders, (
        'CRLF in a shell script breaks it on Linux/macOS '
        '("$\'\\r\': command not found"):\n  ' + '\n  '.join(offenders))


def test_shell_scripts_start_with_a_shebang():
    for path in shell_scripts():
        with open(path, 'rb') as f:
            assert f.read(2) == b'#!', f'{_rel(path)} has no shebang'


# ── Windows batch files: CRLF, ASCII ─────────────────────────────────────────

def test_batch_files_are_crlf():
    files = batch_files()
    assert files, 'no .bat files found - has packaging/win/ moved?'
    offenders = []
    for path in files:
        with open(path, 'rb') as f:
            data = f.read()
        bare_lf = data.count(b'\n') - data.count(b'\r\n')
        if bare_lf:
            offenders.append(f'{_rel(path)} ({bare_lf} bare LF)')
    assert not offenders, (
        'batch files must use CRLF for cmd.exe:\n  ' + '\n  '.join(offenders))


def test_batch_files_are_ascii():
    offenders = []
    for path in batch_files():
        with open(path, 'rb') as f:
            data = f.read()
        bad = sorted({b for b in data if b > 127})
        if bad:
            offenders.append(f'{_rel(path)} (bytes {[hex(b) for b in bad[:6]]})')
    assert not offenders, (
        'non-ASCII in a .bat shifts cmd.exe\'s byte offsets and it resumes '
        'mid-line, skipping commands:\n  ' + '\n  '.join(offenders))


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
