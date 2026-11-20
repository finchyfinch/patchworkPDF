"""Ctrl+P works wherever keyboard focus is (v3.4).

The page's keydown listener only hears keys while WebView2 has focus, and the
old focus fix-up (raw SetFocus on Chrome_RenderWidgetHostHWND) made Windows
REPORT focus on the page while WebView2 dropped the next key — measured: after
cancelling the Open dialog, Ctrl+P reached nothing.  Now:

* focus is handed back with WebView2's own MoveFocus (win_shortcuts.focus_page),
  tried FIRST by win_chrome.focus_web_content;
* the window catches Ctrl+P itself — WebView2's AcceleratorKeyPressed when the
  page has focus, a hidden MenuStrip shortcut when the window does;
* every Ctrl+P is logged with its path and outcome ([SHORTCUT]).

The real-window behaviour needs a desktop and real keypresses:
tools/verify_ctrl_p.py (15/15 across five focus situations when written).
"""
import sys

import _bootstrap  # noqa: F401

import pytest

import entry
# win_shortcuts is safe to import anywhere (it does nothing off Windows);
# win_chrome is Windows-only at import time (ctypes.WINFUNCTYPE), so it is
# imported inside the Windows-only tests — importing it here broke collection
# of the whole suite on macOS and Linux (v3.4 release builds).
from platform_io import win_shortcuts


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows focus path')
def test_focus_uses_webview2_movefocus_before_raw_setfocus(monkeypatch):
    from platform_io import win_chrome
    calls = []
    monkeypatch.setattr(win_shortcuts, 'focus_page', lambda w: calls.append(w) or True)
    monkeypatch.setattr(win_chrome, '_hwnd_of',
                        lambda w: (_ for _ in ()).throw(AssertionError('raw path used')))
    assert win_chrome.focus_web_content('W') is True
    assert calls == ['W']


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows focus path')
def test_raw_setfocus_remains_the_fallback(monkeypatch):
    from platform_io import win_chrome
    monkeypatch.setattr(win_shortcuts, 'focus_page', lambda w: False)
    monkeypatch.setattr(win_chrome, '_hwnd_of', lambda w: None)
    assert win_chrome.focus_web_content('W') is False      # reached the old path


def test_install_is_harmless_without_a_native_window():
    class W:
        native = None
    assert win_shortcuts.install(W()) is False
    assert win_shortcuts.focus_page(W()) is False


def test_window_shortcuts_are_installed_when_the_window_is_shown(monkeypatch):
    class Ev(list):
        def __iadd__(self, fn):
            self.append(fn); return self
    class Events:
        def __init__(self):
            self.shown, self.maximized, self.restored = Ev(), Ev(), Ev()
            self.loaded, self.closed, self.closing = Ev(), Ev(), Ev()
    class W:
        events = Events()
        native = None
    seen = []
    monkeypatch.setattr(win_shortcuts, 'install', lambda w: seen.append(w) or True)
    try:
        entry._attach_window_chrome(W, type('A', (), {'_maximized': False})())
    except Exception:
        pass                       # other shown-time wiring may need a real window
    for fn in W.events.shown:
        try:
            fn()
        except Exception:
            pass
    assert seen == [W]


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def test_ui_event_logs_only_plain_labels(client, caplog):
    import logging
    caplog.set_level(logging.INFO)
    h = {'X-App-Token': entry.APP_TOKEN}
    client.post('/api/ui_event', json={'name': 'print', 'via': 'window', 'outcome': 'run'}, headers=h)
    client.post('/api/ui_event', json={'name': 'print\nINJECT', 'via': 'page'}, headers=h)
    lines = [r.getMessage() for r in caplog.records if '[SHORTCUT]' in r.getMessage()]
    assert lines == ['[SHORTCUT] print via window: run']
