"""Regression tests for the unsaved-work guard on closing a native window.

The report these come from: bookmarks added, never saved, app closed — no
warning, work gone.  `beforeunload` does not fire in the native host, and our
own titlebar X called WindowApi.close() straight through to destroy(), so
nothing asked.

Every close route (OS/native frame X, Alt+F4, and our titlebar X, because
destroy() calls Close()) ends in the backend's `closing` event, so the guard
lives there.  Two constraints shape it and are what these tests pin:

  * the handler runs synchronously on the UI thread and the CSP forbids eval,
    so it CANNOT ask the page anything at close time — it reads a cache the page
    pushes, and asks the page to raise the dialog via fire-and-forget run_js;
  * it must never be able to trap the user in the app.  A page that is wedged,
    or a second click on X instead of answering, closes.
"""
import threading
import time

import _bootstrap  # noqa: F401  (path setup)

import entry


class FakeWindow:
    """Just enough window: records run_js calls, and can fail like a dead one.

    run_js models the real one's cost — on WebView2 it BLOCKS until the page
    answers (edgechromium.evaluate_js waits on a semaphore), so `block` lets a
    test prove the close handler is not the thread doing the waiting.
    """

    def __init__(self, run_js_raises=False, block=0.0):
        self.scripts = []
        self.threads = []
        self._raises = run_js_raises
        self._block = block
        self.events = type('E', (), {})()
        self.events.closing = _Slot()

    def run_js(self, script):
        if self._raises:
            raise RuntimeError('window is gone')
        self.threads.append(threading.current_thread().ident)
        if self._block:
            time.sleep(self._block)
        self.scripts.append(script)

    def wait_for_prompt(self, timeout=2.0):
        """Block until the worker has delivered the prompt (or give up)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.scripts:
                return True
            time.sleep(0.005)
        return False


class _Slot:
    """Stands in for pywebview's Event: collects handlers, `fire` runs them."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self):
        """True = let the close proceed (pywebview cancels on a False return)."""
        return all(fn() is not False for fn in self.handlers)


def _guarded(run_js_raises=False, block=0.0):
    api = entry.WindowApi()
    win = FakeWindow(run_js_raises=run_js_raises, block=block)
    entry._guard_window_close(win, api)
    return win, api


# ── the cache the page pushes ────────────────────────────────────────────────

def test_window_starts_clean_and_closes_without_asking():
    win, api = _guarded()
    assert api._unsaved is False
    assert win.events.closing.fire() is True
    assert win.scripts == []            # the page was never bothered


def test_set_unsaved_is_coerced_to_a_bool():
    api = entry.WindowApi()
    api.set_unsaved('yes')
    assert api._unsaved is True
    api.set_unsaved(0)
    assert api._unsaved is False


def test_saving_re_arms_the_guard():
    # Agreeing to lose work once must not license every later close.
    win, api = _guarded()
    api.set_unsaved(True)
    api.confirm_quit()
    assert api._quit_ok is True
    api.set_unsaved(False)              # the user saved
    assert api._quit_ok is False, 'the next dirty close must ask again'

    api.set_unsaved(True)
    assert win.events.closing.fire() is False, 'and it does ask'


# ── refusing a close, and what the page is told ──────────────────────────────

def test_unsaved_work_cancels_the_close_and_asks_the_page():
    win, api = _guarded()
    api.set_unsaved(True)

    assert win.events.closing.fire() is False, 'the close is cancelled'
    assert len(win.scripts) == 1
    # run_js, never evaluate_js: the CSP forbids the eval() wrapper.
    assert 'pwPromptQuit' in win.scripts[0]
    assert api._asking is True


def test_confirming_lets_the_next_close_through():
    win, api = _guarded()
    api.set_unsaved(True)
    win.events.closing.fire()           # first X → prompt

    api.confirm_quit()                  # user: "close without saving"
    assert api._asking is False
    assert win.events.closing.fire() is True, 'the re-close is allowed'
    assert len(win.scripts) == 1, 'and the page is not asked twice'


def test_cancelling_leaves_the_guard_armed():
    win, api = _guarded()
    api.set_unsaved(True)
    win.events.closing.fire()
    api.cancel_quit()                   # user backed out
    assert api._asking is False
    assert api._quit_ok is False

    # Clicking X again asks again rather than closing silently.
    assert win.events.closing.fire() is False
    assert len(win.scripts) == 2


# ── the prompt must not be raised on the UI thread ───────────────────────────

def test_the_close_handler_does_not_block_on_the_page():
    """pywebview's run_js is NOT fire-and-forget on WebView2: it waits on a
    semaphore released by a continuation posted to the UI thread's sync context.
    The closing handler already IS the UI thread, so asking the page from it
    would block the thread that has to answer — the app would hang on close
    rather than prompt.  The ask has to go out on a worker."""
    win, api = _guarded(block=0.30)
    api.set_unsaved(True)

    started = time.perf_counter()
    assert win.events.closing.fire() is False
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15, (
        'the closing handler waited %.2fs for the page — on the real UI thread '
        'that is a deadlock, not a delay' % elapsed)
    assert win.wait_for_prompt(), 'the prompt still reaches the page'
    assert win.threads and win.threads[0] != threading.current_thread().ident,         'and it was delivered from a worker, not the closing thread'


# ── the escape hatches: never trap the user in the app ───────────────────────

def test_a_second_close_while_the_prompt_is_up_goes_through():
    # The page may be wedged, or the user may just click X again instead of
    # answering.  Either way the second attempt must close.
    win, api = _guarded()
    api.set_unsaved(True)
    assert win.events.closing.fire() is False    # prompt raised, still _asking
    assert win.events.closing.fire() is True, 'second attempt is not blocked'


def test_a_page_that_cannot_be_reached_stops_blocking_after_one_attempt():
    # The ask goes out on a worker, so the first close is already cancelled by
    # the time it fails.  What must NOT happen is re-arming: that would cancel
    # every future close too and the app could never be shut.
    win, api = _guarded(run_js_raises=True)
    api.set_unsaved(True)

    assert win.events.closing.fire() is False, 'the first attempt cancels, as always'
    deadline = time.time() + 2.0
    while api._asking and time.time() < deadline:
        time.sleep(0.005)
    assert api._asking is False, 'the failed ask released the latch'
    assert win.events.closing.fire() is True, 'and the next close is not blocked'


def test_each_window_is_judged_on_its_own_work():
    # Pop-outs get their own WindowApi; one dirty window must not gate another.
    win_a, api_a = _guarded()
    win_b, api_b = _guarded()
    api_a.set_unsaved(True)
    assert win_a.events.closing.fire() is False
    assert win_b.events.closing.fire() is True


if __name__ == '__main__':
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('  ok   ' + name)
            except AssertionError as e:
                fails += 1
                print('  FAIL ' + name + ': ' + str(e))
    print(('\n%d failed' % fails) if fails else '\nall passed')
    sys.exit(1 if fails else 0)
