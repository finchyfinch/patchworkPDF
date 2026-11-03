"""Regression tests for "double-clicked file opens, but the window stays minimised".

The report: with Patchwork minimised to the taskbar, double-clicking a PDF added
the tab — and the taskbar button flashed instead of the window coming back.

That flash is Windows refusing the foreground change.  Only the process that
owns the foreground may call SetForegroundWindow; Explorer grants that right to
the process it launches, which is the short-lived SECOND instance, while the
window belongs to the primary.  So the primary's own attempt is denied and
Windows flashes the taskbar button in its place.

Two halves, one per side of the hand-off, and both are pinned here:

  * the second instance must pass its foreground rights on to the primary's PID
    (AllowSetForegroundWindow) *before* it POSTs, while it is still alive to
    own them;
  * the primary must front the window BEFORE it loads the document, not after —
    _preload_file takes seconds on a large or scanned PDF, and a load that fails
    outright used to leave the window minimised with nothing said at all.
"""
import sys

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry
from bootstrap import single_instance


# ── second instance: hand our foreground rights to the primary ───────────────

def test_forward_file_grants_foreground_to_the_primary_before_posting(monkeypatch):
    """The grant is what stops Windows answering with a taskbar flash."""
    calls = []
    monkeypatch.setattr(single_instance, '_read_rendezvous',
                        lambda: {'pid': 4321, 'port': 49371, 'token': 'tok'})
    monkeypatch.setattr(single_instance, '_grant_foreground',
                        lambda pid: calls.append(('grant', pid)))

    sent = {}

    def _fake_urlopen(req, timeout=None):
        calls.append(('post', req.full_url))
        sent['headers'] = dict(req.headers)
        raise OSError('no server in a test')      # _forward_file swallows this

    monkeypatch.setattr(single_instance.urllib.request, 'urlopen', _fake_urlopen)
    single_instance._forward_file(49371, 'C:/docs/a.pdf')

    assert ('grant', 4321) in calls, (
        'the primary must be blessed with our foreground rights, or its '
        'SetForegroundWindow is denied and the window never un-minimises')
    assert calls.index(('grant', 4321)) < [c[0] for c in calls].index('post'), (
        'the grant must precede the POST — the primary fronts itself while '
        'handling it')
    # The hand-off is still authenticated (the token moved to the same read).
    assert sent['headers'].get('X-app-token') == 'tok'


def test_grant_foreground_is_a_noop_off_windows(monkeypatch):
    """Called unconditionally; only Windows has the concept."""
    monkeypatch.setattr(single_instance.sys, 'platform', 'linux')
    single_instance._grant_foreground(4321)       # must not raise


@pytest.mark.skipif(sys.platform != 'win32', reason='Win32-only API')
def test_grant_foreground_survives_a_bogus_pid():
    """Best-effort: a dead PID must not take the hand-off down with it."""
    single_instance._grant_foreground(0x7FFFFFFF)  # must not raise


# ── primary: front the window before the (slow, fallible) load ───────────────

class _FakeWindow:
    def __init__(self):
        self.scripts = []

    def run_js(self, script):
        self.scripts.append(script)


def _patch_window(monkeypatch, order):
    """Route /api/open_in_new_tab at a fake window and record what it is told."""
    win = _FakeWindow()
    monkeypatch.setattr(entry, '_target_window', lambda: win)

    class _Chrome:
        @staticmethod
        def bring_to_front(w):
            order.append('front')

        @staticmethod
        def focus_web_content(w):
            order.append('focus')

    # Both: `from platform_io import win_chrome` reads the package attribute when
    # the real submodule has already been imported (by an earlier test in the
    # same run) and only falls back to sys.modules when it has not.
    import platform_io
    monkeypatch.setitem(sys.modules, 'platform_io.win_chrome', _Chrome)
    monkeypatch.setattr(platform_io, 'win_chrome', _Chrome, raising=False)
    return win


def test_window_is_fronted_before_the_file_is_loaded(monkeypatch, tmp_path):
    order = []
    win = _patch_window(monkeypatch, order)

    pdf = tmp_path / 'a.pdf'
    pdf.write_bytes(b'%PDF-1.4\n')

    def _fake_preload(path, session_id):
        order.append('preload')
        entry.sessions[session_id]['pages'] = [{'id': 'p1'}]

    monkeypatch.setattr(entry, '_preload_file', _fake_preload)
    monkeypatch.setattr(entry, '_mru_record', lambda p: None)

    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        r = c.post('/api/open_in_new_tab', json={'file_path': str(pdf)},
                   headers={'X-App-Token': entry.APP_TOKEN})

    assert r.status_code == 200
    assert order[0] == 'front', (
        'a large or scanned PDF spends seconds in _preload_file; fronting after '
        'it means the user sees nothing happen for all of that time — and spends '
        'the foreground grant too late to matter')
    assert order == ['front', 'preload', 'focus']
    assert any('openNewTab' in s for s in win.scripts)


def test_window_is_fronted_even_when_the_file_fails_to_load(monkeypatch, tmp_path):
    """The old order returned early on a failed load, leaving the app minimised
    and the user with no sign it had refused the document at all."""
    order = []
    _patch_window(monkeypatch, order)

    pdf = tmp_path / 'broken.pdf'
    pdf.write_bytes(b'not a pdf')
    monkeypatch.setattr(entry, '_preload_file', lambda p, s: order.append('preload'))

    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        r = c.post('/api/open_in_new_tab', json={'file_path': str(pdf)},
                   headers={'X-App-Token': entry.APP_TOKEN})

    assert r.status_code == 500
    assert order[0] == 'front'


def test_invalid_path_does_not_front_the_window(monkeypatch, tmp_path):
    """Nothing is opening, so nothing should steal the user's foreground."""
    order = []
    _patch_window(monkeypatch, order)

    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        r = c.post('/api/open_in_new_tab',
                   json={'file_path': str(tmp_path / 'nope.pdf')},
                   headers={'X-App-Token': entry.APP_TOKEN})

    assert r.status_code == 400
    assert order == []


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
