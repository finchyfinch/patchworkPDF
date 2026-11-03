"""Regression tests for "Ctrl+P stops working after a successful print".

The handler itself is a document-level capture listener registered once at load,
so it cannot be unregistered — and the toolbar Print button keeps working
throughout.  Both facts say the same thing: the keystroke never reaches the page.
After a native modal closes, Windows returns focus to the top-level WinForms
host rather than the WebView2 child inside it, and key events go to the host.
A mouse click fixes it (which is why the button still works and the shortcut
does not), a keypress cannot.

Every JS-side signal for "the dialog has gone" can miss:

  * `afterprint` is unreliable in WebView2's built-in PDF viewer;
  * the window `focus` EVENT only fires if focus comes back to the web content —
    and the whole failure is that it comes back to the host instead;
  * the `mousedown` fallback needs the click that would have fixed it anyway.

So the guard watches from the OS side, where the answer is not in doubt: the
print dialog is modal and owned by this process, so our window becoming the
foreground window again IS the dialog closing.  These tests drive that state
machine with a fake win_chrome — the real Win32 calls and a real print dialog
are not reachable from a test, but the decision logic is where the bug lived.

What must hold:
  * focus is re-asserted only after the dialog has come AND gone;
  * it is re-asserted more than once, because Windows moves focus again while the
    dialog is torn down and a single push lands too early to survive it;
  * a print that never raised a dialog, or one the user leaves open, must not
    leave a thread running or steal focus from whatever they moved on to.
"""
import sys
import time

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry


class FakeChrome:
    """Stands in for platform_io.win_chrome: scripts the foreground sequence and
    records every focus push."""

    def __init__(self, foreground_script, focus_ok=True):
        # One entry consumed per poll; the last value repeats forever after.
        self._script = list(foreground_script)
        self.focus_calls = 0
        self.focus_ok = focus_ok
        self.polls = 0

    def is_foreground(self, _window):
        self.polls += 1
        if len(self._script) > 1:
            return self._script.pop(0)
        return self._script[0] if self._script else True

    def focus_web_content(self, _window):
        self.focus_calls += 1
        return self.focus_ok


@pytest.fixture()
def api(monkeypatch):
    """A WindowApi bound to a stand-in window, with win_chrome faked out."""
    import platform_io

    def install(chrome):
        monkeypatch.setitem(sys.modules, 'platform_io.win_chrome', chrome)
        monkeypatch.setattr(platform_io, 'win_chrome', chrome, raising=False)

    a = entry.WindowApi()
    a._bind(object())                      # any non-None window will do
    a.install_chrome = install
    return a


def _wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _join(api, timeout=8.0):
    if api._print_guard:
        api._print_guard.join(timeout=timeout)


# ── the fix ──────────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only guard')
def test_focus_returns_to_the_page_once_the_dialog_closes(api):
    # foreground: ours → dialog takes it → dialog gives it back.
    chrome = FakeChrome([True, False, False, True])
    api.install_chrome(chrome)

    assert api.begin_print_focus_guard(timeout_s=1.5) is True
    _join(api)

    assert chrome.focus_calls >= 1, (
        'the page never got focus back, which is exactly the reported bug: the '
        'Print button still works, Ctrl+P does not')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only guard')
def test_focus_is_re_asserted_more_than_once(api):
    """A single push is undone: Windows moves focus again as the modal is
    destroyed, so the push has to be repeated across the teardown."""
    chrome = FakeChrome([True, False, False, True], focus_ok=False)
    api.install_chrome(chrome)
    api.begin_print_focus_guard(timeout_s=1.5)
    _join(api)
    assert chrome.focus_calls >= 3, (
        f'only {chrome.focus_calls} attempt(s) — one push at the wrong instant '
        'is silently undone and the shortcut stays dead')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only guard')
def test_it_stops_pushing_once_focus_verifiably_sticks(api):
    """Re-asserting forever would fight the user for focus."""
    chrome = FakeChrome([True, False, False, True], focus_ok=True)
    api.install_chrome(chrome)
    api.begin_print_focus_guard(timeout_s=1.5)
    _join(api)
    assert chrome.focus_calls <= 5, (
        f'{chrome.focus_calls} pushes — it must settle once focus holds')


# ── it must never make things worse ──────────────────────────────────────────

@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only guard')
def test_a_print_that_never_raises_a_dialog_touches_nothing(api):
    """Some print paths spool without a visible dialog.  Nothing left the
    foreground, so nothing should be grabbed back."""
    chrome = FakeChrome([True])            # foreground never leaves us
    api.install_chrome(chrome)
    api.begin_print_focus_guard(timeout_s=1.5)
    _join(api, timeout=12)
    assert chrome.focus_calls == 0, 'no dialog appeared; focus was never ours to reclaim'
    assert not api._print_guard.is_alive(), 'and the watcher stopped on its own'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only guard')
def test_a_dialog_that_never_closes_does_not_strand_a_thread(api):
    """The user leaves the print dialog open and walks away."""
    chrome = FakeChrome([True, False])     # gone, and never comes back
    api.install_chrome(chrome)
    api.begin_print_focus_guard(timeout_s=1.5)
    _join(api, timeout=12)
    assert not api._print_guard.is_alive(), 'the deadline has to end it'
    assert chrome.focus_calls == 0, 'and it must not grab focus off another app'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only guard')
def test_the_guard_is_a_daemon_so_it_can_never_hold_the_app_open(api):
    chrome = FakeChrome([True, False])
    api.install_chrome(chrome)
    api.begin_print_focus_guard(timeout_s=1.5)
    assert api._print_guard.daemon is True
    _join(api, timeout=12)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only guard')
def test_one_dialog_gets_one_guard(api):
    """Re-arming while a guard is live must not pile up watcher threads."""
    chrome = FakeChrome([True, False])
    api.install_chrome(chrome)
    api.begin_print_focus_guard(timeout_s=1.5)
    first = api._print_guard
    assert _wait_for(lambda: chrome.polls > 0)
    assert api.begin_print_focus_guard(timeout_s=1.5) is True
    assert api._print_guard is first, 'a second call must reuse the live guard'
    _join(api, timeout=12)


def test_it_does_nothing_without_a_window():
    a = entry.WindowApi()                  # never _bind()ed
    assert a.begin_print_focus_guard() is False
    assert a._print_guard is None


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
