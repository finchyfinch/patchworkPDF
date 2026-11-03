# Boot timing FIRST — captures T0 as early as possible (see boot_timing.py).
from bootstrap import boot_timing as _boot
_boot.mark('process start (entry.py top)')

import os
import uuid
import tempfile
import threading
import time
import sys
import secrets
import logging
import json as _json_mod
import hashlib
import re as _re

# ── One BLAS thread — must be set before numpy is first imported ──────────────
# numpy's bundled OpenBLAS spawns a worker thread per CPU core at import and
# commits a buffer for each: measured ~745 MB of commit and 23 idle threads on a
# 24-core machine, against ~9 MB with one thread.  The app only uses numpy for
# elementwise OCR preprocessing (ocr_utils) and never calls BLAS, so the extra
# threads buy nothing.  setdefault so an explicit override still wins.  Applies
# on every platform whose numpy wheel ships OpenBLAS (Windows, Linux/Flatpak,
# Intel macOS); Apple-silicon wheels use Accelerate and simply ignore it.
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')

# ── Logging — must be set up before other app modules are imported ────────────
from bootstrap import log_config as _log_config
_log_config.setup_logging()
_boot.enable_logging()                 # flush the buffered startup marks
_boot.mark('logging configured')
logger = logging.getLogger(__name__)

from bootstrap import single_instance as _single_instance

# ── Fast second-instance exit ──────────────────────────────────────────────────
# When this process is just a double-clicked document and the app is already
# running, hand the file to the running instance and exit NOW — before paying
# for the heavy imports below (Flask, pypdf, editor, viewer) for nothing.
if __name__ == '__main__':
    if _single_instance.handle_existing_instance(_single_instance.find_initial_file()):
        sys.exit(0)

_boot.mark('single-instance check passed')

# ── Startup splash ─────────────────────────────────────────────────────────────
# Shown NOW — before the heavy imports below and the WebView2 spin-up in main()
# — because that whole stretch paints nothing on screen.  Skipped in headless
# mode (no window to bridge to).  Stdlib-only and best-effort, so it can never
# delay or break startup (see splash.py).  Dismissed on the window's `loaded`
# event in main().
_SPLASH = False                            # True only once the splash is really up
if __name__ == '__main__' and not os.environ.get('PATCHWORK_NO_BROWSER', ''):
    try:
        from bootstrap import splash as _splash
        _splash.show()
        _SPLASH = True
    except Exception as _e:                 # never let the splash block startup
        _boot.mark(f'splash unavailable: {_e}')

from flask import Flask, request, jsonify, send_file, abort, make_response
from werkzeug.exceptions import HTTPException
from routes.editor import register_editor_routes, start_heavy_init_background
from routes.viewer import register_viewer_routes
from pdf.pdf_core import (sessions, LAST_DIR, get_save_dir, _preload_file,
                      _mru_record, register_core_routes)
from routes.layout import register_layout_routes
from bootstrap import recovery as _recovery
_boot.mark('heavy imports loaded (flask, pypdf, editor, viewer, ...)')


def _pt(key, en_text, **params):
    """Translate a Python-owned UI string to the persisted language, degrading
    to the given English text if i18n is unavailable or the key is missing."""
    try:
        from i18n import i18n_py
        s = i18n_py.pt(key, **params)
        return s if s and s != key else en_text
    except Exception:
        return en_text


if __name__ == '__main__' and _SPLASH:
    _splash.set_status(_pt('splash.loading_components', 'Loading components…'))

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB

# ── Native window (pywebview) ─────────────────────────────────────────────────
# Set once the pywebview window is created in main().  The Flask thread reads it
# to push events into the live window (e.g. second-instance "open in new tab").
WV_WINDOW = None

from platform_io import platform_paths as _pp

# The custom frameless dark titlebar + resize/snap shim (win_chrome) is a
# Windows-only construction.  On macOS and Linux we use the OS-native window
# frame instead: the window is created with a normal border/title bar, the HTML
# window-control buttons are hidden (the OS draws them), and the tab strip
# renders as an ordinary top toolbar.  See docs/CROSS_PLATFORM_PLAN.md §4.
_USE_CUSTOM_CHROME = _pp.is_windows()


def _platform_info():
    """Platform descriptor handed to the page so it can lay out the titlebar for
    either the custom (Windows) or native (mac/Linux) window frame."""
    if _pp.is_windows():
        os_name = 'windows'
    elif _pp.is_macos():
        os_name = 'macos'
    else:
        os_name = 'linux'
    return {'os': os_name, 'custom_chrome': _USE_CUSTOM_CHROME}


def _webview_gui():
    """The pywebview GUI backend for this OS.  edgechromium (WebView2) is
    Windows-only; macOS uses cocoa (WKWebView) and Linux uses gtk (WebKitGTK).
    Returns None to let pywebview auto-select if the OS is unrecognised."""
    if _pp.is_windows():
        return 'edgechromium'
    if _pp.is_macos():
        return 'cocoa'
    if _pp.is_linux():
        return 'gtk'
    return None

# Extra native windows created by "pop out tab".  Kept referenced so neither the
# window nor its js_api is garbage-collected while it is open.
_EXTRA_WINDOWS = []

# Registry of all currently-open native windows (main + pop-outs), in creation
# order.  A window is added when its chrome is attached and removed on its
# `closed` event, so this list always reflects what is actually on screen.
# Routing native actions (e.g. second-instance "open in new tab") through this
# list — instead of the fixed main-window handle — means they keep working after
# the original window is closed.  evaluate_js() on a destroyed window silently
# no-ops rather than raising, so a try/except can't detect a dead window; the
# registry is the only reliable source of truth.
_LIVE_WINDOWS = []

# Most-recently-focused live window — reported by each window's JS via
# WindowApi.note_active().  Held in a one-element list so closures can rebind it
# without a `global` declaration.
_ACTIVE_WINDOW = [None]

# Registry of live windows keyed by their WindowApi id (api._id -> window), so a
# tab dragged from one window can be docked into another by id.  Maintained
# alongside _LIVE_WINDOWS in _attach_window_chrome.
_WINDOWS_BY_ID = {}


def _target_window():
    """Pick a live window to push a UI action into: the most-recently-focused
    one if it is still open, else the newest open window, else None."""
    aw = _ACTIVE_WINDOW[0]
    if aw is not None and aw in _LIVE_WINDOWS:
        return aw
    return _LIVE_WINDOWS[-1] if _LIVE_WINDOWS else None


# ── Tab dock (re-join) cross-window coordinator ──────────────────────────────
# A tab dragged out of one window can be dropped onto another window's tab bar.
# The drop decision is made HERE from live Win32 geometry (GetCursorPos +
# GetWindowRect), so it never depends on client-reported coordinates or DPI math.
_DOCK_HILITE = [None]        # window currently showing a "drop here" indicator
_CONTROLS_W  = 3 * 46        # logical px of the min/max/close cluster (excluded)


def _tabbar_band(window):
    """Physical-px rect (l, t, r, b) of a window's dockable titlebar strip, or
    None: the top TITLEBAR_H of the window, minus the control cluster."""
    from platform_io import win_chrome
    rect = win_chrome.get_window_rect(window)
    if not rect:
        return None
    l, t, r, b = rect
    scale = win_chrome.dpi_scale(window)
    return (l, t, max(l, r - int(_CONTROLS_W * scale)),
            t + int(win_chrome.TITLEBAR_H * scale))


def _dock_target_window(src_window):
    """The live window (other than src) whose dockable titlebar strip currently
    contains the cursor, or None."""
    from platform_io import win_chrome
    pt = win_chrome.cursor_pos()
    if pt is None:
        return None
    px, py = pt
    for win in list(_WINDOWS_BY_ID.values()):
        if win is src_window:
            continue
        band = _tabbar_band(win)
        if band and band[0] <= px < band[2] and band[1] <= py < band[3]:
            return win
    return None


def _set_dock_hilite(target):
    """Show the drop indicator on `target` and clear it from any other window.
    Idempotent — only touches windows whose state actually changes."""
    prev = _DOCK_HILITE[0]
    if prev is target:
        return
    for win, on in ((prev, 'false'), (target, 'true')):
        if win is not None:
            try:
                # run_js, not evaluate_js: the latter wraps the script in eval(),
                # which our CSP blocks in WKWebView (macOS).  No result needed.
                win.run_js(f'window.showTabDock && window.showTabDock({on})')
            except Exception:
                pass
    _DOCK_HILITE[0] = target

# Last directory the user opened from / saved to via a native dialog.  Used as the
# default location for files that have no known source dir (e.g. drag-dropped
# files, whose real path WebView2 does not expose).

# File-dialog filter labels are built at dialog-open time so they follow the
# active UI language. Only the label word is translated — the "(*.ext)" globs
# stay literal so pywebview parses the extensions correctly.
def _open_file_types():
    return (
        _pt('filetype.documents', 'Documents') + ' (*.pdf;*.doc;*.docx;*.odt;*.rtf;*.xls;*.xlsx;*.ods;*.csv;*.txt)',
        _pt('filetype.images', 'Images') + ' (*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.tif;*.tiff;*.webp)',
        _pt('filetype.pdf_files', 'PDF files') + ' (*.pdf)',
        _pt('filetype.all_files', 'All files') + ' (*.*)',
    )

def _save_file_types():
    return (
        _pt('filetype.pdf_file', 'PDF file') + ' (*.pdf)',
        _pt('filetype.all_files', 'All files') + ' (*.*)',
    )


class WindowApi:
    """JS bridge exposed to the page as window.pywebview.api.

    Each native window has its own WindowApi instance so its titlebar controls
    act on the right window.  Window-control methods (minimize/toggle_maximize/
    close) are wired up by the custom titlebar in Phase 3."""

    def __init__(self):
        self._w = None
        self._maximized = True  # we launch maximized
        # Stable per-window id so the page can address its own window and so
        # cross-window tab moves (tear-off / re-join) can target a specific one.
        self._id = uuid.uuid4().hex
        # Unsaved-work guard (see _guard_window_close).  The CSP forbids eval,
        # so evaluate_js cannot ask the page anything at close time — instead the
        # page PUSHES its state here whenever it changes, and the closing handler
        # reads this cache.  _quit_ok latches the user's "close anyway"; _asking
        # is the escape hatch, so a wedged page can never trap the app open.
        self._unsaved = False
        self._quit_ok = False
        self._asking = False
        # Background watcher that hands focus back to the page once the OS print
        # dialog closes — see begin_print_focus_guard.
        self._print_guard = None

    def set_unsaved(self, flag):
        """Page reports whether this window holds unsaved work (any tab)."""
        self._unsaved = bool(flag)
        if not self._unsaved:
            self._quit_ok = False     # saved: guard the next close again
        return True

    def confirm_quit(self):
        """Page reports the user accepted losing the unsaved work."""
        self._quit_ok = True
        self._asking = False
        return True

    def cancel_quit(self):
        """Page reports the user backed out of the close prompt."""
        self._asking = False
        return True

    def platform(self):
        """Platform descriptor for the page: {'os': 'windows'|'macos'|'linux',
        'custom_chrome': bool}.  The page uses this to render either the custom
        frameless titlebar (Windows) or an OS-native-framed layout (mac/Linux)."""
        return _platform_info()

    def _bind(self, window):
        self._w = window

    def minimize(self):
        if self._w:
            self._w.minimize()

    def toggle_maximize(self):
        if not self._w:
            return False
        # Query the real window state each call so the toggle never desyncs
        # (e.g. after a native double-click-maximise on the caption strip).
        state = None
        try:
            from platform_io import win_chrome
            state = win_chrome.is_maximized(self._w)
        except Exception:
            pass
        if state is None:
            state = self._maximized          # non-Windows fallback
        if state:
            self._w.restore()
            self._maximized = False
        else:
            self._w.maximize()
            self._maximized = True
        return self._maximized

    def close(self):
        if self._w:
            self._w.destroy()

    def popout(self, url, x=None, y=None):
        """Open a document tab in its own native window.  Replaces window.open,
        which WebView2 routes to the OS browser.  `url` is absolute
        (http://127.0.0.1:<port>/viewer?session_id=...).  Optional x/y (logical
        px == the page's CSS px) position the new window at the tear-off drop
        point.  The process stays alive until ALL windows are closed, so a
        popped-out doc keeps the app running."""
        try:
            import webview
            sub = WindowApi()
            pos = {}
            if x is not None and y is not None:
                pos = {'x': max(0, int(x)), 'y': max(0, int(y))}
            w = webview.create_window(
                'Patchwork PDF',
                url=url,
                width=1100, height=850,
                frameless=_USE_CUSTOM_CHROME,   # custom chrome on Windows; native frame elsewhere
                background_color='#0f0f11',
                js_api=sub,
                **pos,
            )
            sub._bind(w)
            sub._maximized = False          # pop-outs open windowed, not maximized
            _attach_window_chrome(w, sub)
            _EXTRA_WINDOWS.append((w, sub))
            return True
        except Exception as e:
            _log(f"popout failed: {e}")
            return False

    # ── Native file dialogs (real OS paths) ──────────────────────────────────
    def _default_dir(self, session_id):
        """Best default directory for a dialog: the document's source folder,
        else the last directory used in a dialog, else the user's home."""
        sess = sessions.get(session_id) if session_id else None
        if sess:
            d = get_save_dir(sess)
            if d:
                return d
        return LAST_DIR[0] or os.path.expanduser('~')

    def _restore_web_focus(self):
        """Hand keyboard focus back to the page after a native modal closed.

        Without this the Save/Open dialog leaves focus on the WinForms host and
        every document-level shortcut in the page goes dead until the user
        happens to click something — the "Ctrl+P does nothing after a Layout
        save, but the print button works" report.  See
        win_chrome.focus_web_content."""
        try:
            from platform_io import win_chrome
            ok = win_chrome.focus_web_content(self._w)
            _log(f"restore web focus: {'ok' if ok else 'no target'}")
        except Exception as e:
            _log(f"restore web focus failed: {e}")

    def restore_focus(self):
        """Page-callable form of _restore_web_focus, for the paths where the
        focus thief is inside the page (the print preview's hidden iframe)
        rather than a Python-opened dialog."""
        self._restore_web_focus()
        return True

    def begin_print_focus_guard(self, timeout_s=90):
        """Watch for the print dialog closing and put focus back in the page.

        The long-standing report this exists for: after a SUCCESSFUL print,
        Ctrl+P stops working — the toolbar button still prints, so the handler is
        fine; the keystroke simply never reaches the page.  Every JS-side signal
        we had for "the dialog is gone" can miss:

          * `afterprint` is unreliable in WebView2's built-in PDF viewer;
          * the window `focus` EVENT only fires if focus comes back to the web
            content — and the whole failure mode is that it comes back to the
            WinForms host instead;
          * a `mousedown` fallback needs the user to click, and a click would
            have fixed the focus anyway.

        So this watches from the OS side instead, where the answer is not in
        doubt: the dialog is modal and owned by us, so our top-level window
        becoming the foreground window again IS the dialog closing.  Once it
        does, focus is pushed back into the WebView2 content HWND — repeatedly
        for a short while, because Windows moves focus again as the dialog is
        torn down and a single push lands too early to survive it.

        Best-effort and self-limiting: one guard at a time, a hard timeout, and
        a daemon thread so it can never hold the app open.
        """
        if sys.platform != 'win32' or not self._w:
            return False
        if self._print_guard and self._print_guard.is_alive():
            return True                      # one dialog, one guard
        win = self._w

        def _watch():
            from platform_io import win_chrome
            # Floor of 1s only so tests can drive the state machine quickly;
            # every real caller uses the 90s default.
            deadline = time.time() + max(1, min(600, float(timeout_s or 90)))
            # Phase 1: wait for the dialog to actually take the foreground.  If
            # it never does (a print that silently failed, or a driver that
            # prints without a dialog) the deadline ends the guard quietly.
            left = False
            while time.time() < deadline:
                if not win_chrome.is_foreground(win):
                    left = True
                    break
                time.sleep(0.15)
            if not left:
                return
            # Phase 2: wait for it to hand the foreground back to us.
            while time.time() < deadline:
                if win_chrome.is_foreground(win):
                    break
                time.sleep(0.15)
            else:
                return
            # Phase 3: re-assert focus across the teardown.  Stop as soon as one
            # push verifiably sticks AND still holds a beat later.
            ok_run = 0
            for delay in (0.0, 0.2, 0.4, 0.8, 1.5):
                time.sleep(delay)
                if not win_chrome.is_foreground(win):
                    ok_run = 0               # something else took over — let it
                    continue
                ok_run = ok_run + 1 if win_chrome.focus_web_content(win) else 0
                if ok_run >= 2:
                    break
            _log('print focus guard: focus returned to the page')

        self._print_guard = threading.Thread(target=_watch, daemon=True,
                                             name='print-focus-guard')
        self._print_guard.start()
        return True

    def pick_open_files(self, session_id=None):
        """Open a native file-open dialog; return a list of absolute paths
        (or None if cancelled).  JS uses these to load files by real path so the
        save dialog can later default to the same folder."""
        if not self._w:
            return None
        try:
            import webview
            res = self._w.create_file_dialog(
                webview.OPEN_DIALOG,
                directory=self._default_dir(session_id),
                allow_multiple=True,
                file_types=_open_file_types(),
            )
            self._restore_web_focus()
            if not res:
                return None
            res = list(res)
            if res:
                LAST_DIR[0] = os.path.dirname(res[0])
            return res
        except Exception as e:
            self._restore_web_focus()
            _log(f"pick_open_files failed: {e}")
            return None

    def pick_save_path(self, session_id=None, default_name='document.pdf'):
        """Open a native save dialog defaulting to the document's source folder;
        return the chosen absolute path (or None if cancelled).  Once the document
        has been saved under a chosen name, default to that name so re-saving keeps
        it (overriding the caller's '_edited' suggestion)."""
        if not self._w:
            return None
        sess = sessions.get(session_id) if session_id else None
        if sess and sess.get('save_name'):
            default_name = sess['save_name']
        try:
            import webview
            res = self._w.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=self._default_dir(session_id),
                save_filename=default_name or 'document.pdf',
                file_types=_save_file_types(),
            )
            self._restore_web_focus()
            if not res:
                return None
            if isinstance(res, (list, tuple)):
                res = res[0]
            if res:
                LAST_DIR[0] = os.path.dirname(res)
            return res
        except Exception as e:
            self._restore_web_focus()
            _log(f"pick_save_path failed: {e}")
            return None

    def note_active(self):
        """Called by this window's JS when it gains focus, so the backend knows
        which window to route second-instance "open in new tab" requests into."""
        _ACTIVE_WINDOW[0] = self._w
        return True

    def set_titlebar_drag_regions(self, rects):
        """JS reports which parts of the custom titlebar are draggable (the empty
        spacer), in CSS px, so the native hit-test treats tabs/buttons as
        clickable.  Called on titlebar layout changes (tabs added/removed, resize)."""
        try:
            from platform_io import win_chrome
            win_chrome.set_drag_regions(self._w, rects or [])
        except Exception:
            pass
        return True

    def take_dropped_paths(self):
        """Pop the real OS paths the native drag handler captured for the drop
        the page is currently handling (macOS — see _arm_native_drop_pull).

        Returns [] when nothing was captured (non-file drag, or an unexpected
        pywebview internal), which tells the page to fall back to uploading the
        dropped bytes.  Always drains the buffer so one drop's paths can never
        leak into the next."""
        try:
            from webview.dom import _dnd_state
            captured = list(_dnd_state.get('paths') or [])
            _dnd_state['paths'] = []
            # Entries are (display_name, full_path); keep the paths that exist.
            paths = [p for (_n, p) in captured if p and os.path.exists(p)]
            if paths:
                _log(f'native drop (macOS): {len(paths)} real path(s) claimed')
            return paths
        except Exception as e:
            _log(f'take_dropped_paths failed: {e}')
            return []

    # ── Tab tear-off / re-join bridge ────────────────────────────────────────
    def tab_drag_hover(self):
        """Called (throttled) by the page while a tab drag is in progress.  Finds
        the window whose tab bar the cursor is over and shows the drop indicator
        there.  Returns True when a dock target is under the cursor."""
        target = _dock_target_window(self._w)
        _set_dock_hilite(target)
        return target is not None

    def tab_dock_target(self):
        """Non-mutating probe: True if the cursor is currently over another
        window's dockable tab bar.  The page calls this on release to learn
        whether the gesture is about to MOVE the tab (so it can confirm unsaved
        edits) before committing the dock."""
        return _dock_target_window(self._w) is not None

    def tab_drop(self, session_id, page_id=''):
        """Called on pointer-up.  If the cursor is over another window's tab bar,
        dock the dragged session there (open it as a new tab) and return
        {'docked': True}; else {'docked': False} so the source falls back to
        detach/switch.  Sessions are backend-global, so only the id moves."""
        _set_dock_hilite(None)
        target = _dock_target_window(self._w)
        if target is None:
            return {'docked': False}
        try:
            # run_js: evaluate_js's eval() wrapper is CSP-blocked on macOS.
            target.run_js(
                f'window.openNewTab({_json.dumps(session_id)}, {_json.dumps(page_id or "")})')
            from platform_io import win_chrome
            win_chrome.bring_to_front(target)
        except Exception as e:
            _log(f'tab_drop dock failed: {e}')
            return {'docked': False}
        _log(f'tab docked: session {session_id} -> another window')
        return {'docked': True}

    def tab_drag_end(self):
        """Called if a drag ends without docking — clear any drop indicator."""
        _set_dock_hilite(None)
        return True

    def begin_titlebar_drag(self, client_x, client_y):
        """Called by the page when a titlebar drag crosses the start threshold.

        If the window is maximised, un-maximise it under the cursor (native
        drag-to-restore, which pywebview's own drag-region can't do) and return
        the adjusted grab offset; otherwise echo the offset unchanged.  The page
        then calls move_window_to_cursor() with the returned offset to follow the
        cursor.  (client_x, client_y) is the cursor's offset within the window in
        CSS px.  Returns {'grabX', 'grabY'} in CSS px."""
        try:
            from platform_io import win_chrome
            res = win_chrome.restore_under_cursor(self._w, client_x, client_y)
            if res is not None:
                self._maximized = False
                return {'grabX': res[0], 'grabY': res[1]}
        except Exception as e:
            _log(f"begin_titlebar_drag failed: {e}")
        return {'grabX': client_x, 'grabY': client_y}

    def move_window_to_cursor(self, grab_x, grab_y):
        """Position THIS window so the grabbed point — (grab_x, grab_y) in the
        page's CSS px, the cursor's offset within the window captured at drag
        start — stays under the LIVE cursor.  Reading the cursor here (physical
        px) rather than trusting per-move client coords keeps the single-tab
        follow accurate, including across monitors with different DPI."""
        try:
            from platform_io import win_chrome
            pt = win_chrome.cursor_pos()
            if pt is None:
                return True
            scale = win_chrome.dpi_scale(self._w)
            win_chrome.move_window(self._w,
                                   round(pt[0] - grab_x * scale),
                                   round(pt[1] - grab_y * scale))
        except Exception:
            pass
        return True


class _RunJsReplies:
    """A window proxy whose evaluate_js() is run_js() — see _csp_safe_js_bridge.

    The reply is an assignment whose completion value is the assigned OBJECT,
    and pywebview 5.4's gtk backend converts whatever a script evaluates to via
    JavaScriptCore.Value.to_object() — which the GNOME 50 runtime's JSC does not
    have, so every reply logged an AttributeError traceback (the value itself
    had already been delivered).  Ending on `void 0` makes the result undefined,
    which the converter returns early on."""

    def __init__(self, window):
        self._w = window

    def __getattr__(self, name):
        return getattr(self._w, name)

    def evaluate_js(self, script, callback=None):
        return self._w.run_js(script + ';\nvoid 0;')


# The statement pywebview 5.4's js/api.js builds every js_api method with.
_API_NEW_FUNCTION = _re.compile(
    r'nestedObject\[functionName\]\s*=\s*new Function\(\s*'
    r'sanitize_params\(params\),\s*funcBody\s*\);')

# The same method as a closure: identical promise/_checkValue/_jsApiCallback
# wiring, no string compiled into code.  Arguments go through `arguments`, so
# the declared parameter names (only ever used for their count) are not needed.
_API_CLOSURE = (
    'nestedObject[functionName] = (function (name) {'
    ' return function () {'
    '  var __id = (Math.random() + "").substring(2);'
    '  var promise = new Promise(function (resolve, reject) {'
    '   window.pywebview._checkValue(name, resolve, reject, __id);'
    '  });'
    '  window.pywebview._jsApiCallback(name, Array.prototype.slice.call(arguments), __id);'
    '  return promise;'
    ' };'
    '})(funcName);')


def _csp_safe_js_bridge():
    """Make pywebview's JS bridge work under our CSP on WebKitGTK (Linux).

    The CSP grants script-src 'self' 'unsafe-inline' but deliberately NOT
    'unsafe-eval', and WebKitGTK enforces that on pywebview's own plumbing in
    two places (WebView2 does not, which is why Windows never showed it):

    1. js/api.js builds every js_api method with `new Function(...)`.  WebKit
       throws EvalError at api.js:75, `window.pywebview.api` is never populated,
       and isNativeApp() is false — so the app silently behaved like a browser:
       Open used the HTML file input and Save fell back to a DOWNLOAD
       ("PDF downloaded") instead of the native dialog.  Up to v3.2 every Linux
       build had this.
    2. js_bridge_call sends each return value back with window.evaluate_js(),
       which wraps it in eval().  The EvalError is caught inside the wrapper, so
       nothing is logged and the page's promise simply never resolves — the
       Save dialog would open and then hang the save forever.

    Fix both at the source rather than grant 'unsafe-eval': rewrite (1) as a
    closure, and route (2) through run_js — the reply is a plain assignment
    statement, so it needs no eval() to produce a value.  Written against
    pywebview 5.4; logs rather than guesses if a future release changes shape."""
    try:
        from webview import util as _wv_util
        from webview.platforms import gtk as _wv_gtk
    except Exception as e:
        _log(f"[gtk-csp] pywebview gtk backend not importable ({e}); bridge unpatched")
        return

    orig_load = _wv_util.load_js_files

    def load_js_files(window, platform):
        js_code, finish_script = orig_load(window, platform)
        patched, n = _API_NEW_FUNCTION.subn(lambda _m: _API_CLOSURE, js_code)
        if n != 1:
            _log(f"[gtk-csp] api.js new Function() not found ({n} matches) — "
                 f"window.pywebview.api will be missing under the CSP")
            return js_code, finish_script
        return patched, finish_script

    orig_bridge = _wv_util.js_bridge_call

    def js_bridge_call(window, func_name, param, value_id):
        return orig_bridge(_RunJsReplies(window), func_name, param, value_id)

    _wv_util.load_js_files = load_js_files
    # gtk.py did `from webview.util import js_bridge_call`, so patch its copy.
    _wv_util.js_bridge_call = js_bridge_call
    _wv_gtk.js_bridge_call = js_bridge_call
    _log("[gtk-csp] pywebview JS bridge patched for the no-eval CSP")


def _file_uri_to_path(uri):
    """Convert a file:// URI (how WebKit reports a dropped file) to a local path,
    decoding %XX escapes (e.g. %20 → space).  Returns None if not a file URI."""
    try:
        from urllib.parse import urlparse, unquote
        parsed = urlparse(uri)
        if parsed.scheme != 'file':
            return None
        return unquote(parsed.path)
    except Exception:
        return None


def _block_file_navigation_gtk(window):
    """Stop the WebKitGTK drag-drop 'hijack' (Linux) — and open the dropped file.

    WebKitGTK's WebView widget treats a dropped file as a navigation and loads
    file://<path>, replacing our SPA with WebKit's built-in file viewer.  The web
    page's JS never sees a drop event, so a JS-level guard can't stop it.  Block
    it at the source: reject any attempt to navigate the webview to a file:// URL
    (the app only ever lives at http://127.0.0.1, so a file:// navigation is
    always the drop hijack).  GTK/Linux only; best-effort with logging so a failed
    hook-up is visible rather than silent."""
    if not _pp.is_linux():
        return
    try:
        # pywebview is already running the gtk backend here, so WebKit2's gi
        # version is set up — a plain import is enough.
        from gi.repository import WebKit2
    except Exception as e:
        _log(f"[gtk-nav] WebKit2 import failed; drop-hijack not blocked: {e}")
        return

    # Locate the WebKit2.WebView backing this pywebview window.
    webkit_view = None
    try:
        from webview.platforms import gtk as _gtk_be
        insts = getattr(_gtk_be.BrowserView, 'instances', {}) or {}
        bv = insts.get(getattr(window, 'uid', None))
        webkit_view = getattr(bv, 'webview', None) if bv is not None else None
    except Exception as e:
        _log(f"[gtk-nav] could not locate WebView ({e}); drop-hijack not blocked")
        return
    if webkit_view is None:
        _log(f"[gtk-nav] WebView not found for window uid={getattr(window,'uid',None)!r}; "
             f"instances={list(insts.keys()) if 'insts' in dir() else '?'}")
        return

    from gi.repository import GLib

    def _dispatch_drop(path):
        # Hand the real path to the page, which opens it into the active panel
        # (viewer → new tab, layout → grid).  run_javascript is async/non-blocking
        # and safe to call on the GTK main thread (unlike pywebview's evaluate_js,
        # which blocks).  Deferred via idle_add so it runs after the policy
        # decision returns.
        try:
            webkit_view.run_javascript(
                "window.onNativeFileDrop && window.onNativeFileDrop(%s)"
                % _json_mod.dumps(path), None, None, None)
        except Exception as e:
            _log(f"[gtk-nav] drop dispatch to page failed: {e}")
        return False   # GLib.idle_add: run once

    def _on_decide_policy(_wv, decision, decision_type):
        try:
            if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
                uri = decision.get_navigation_action().get_request().get_uri() or ''
                if uri.startswith('file://'):
                    decision.ignore()               # kill the hijack navigation
                    path = _file_uri_to_path(uri)
                    _log(f"[gtk-nav] drop intercepted: {uri} -> {path}")
                    if path:                         # ...then open it like a real drop
                        GLib.idle_add(_dispatch_drop, path)
                    return True
        except Exception as e:
            _log(f"[gtk-nav] decide-policy handler error: {e}")
        return False

    try:
        webkit_view.connect('decide-policy', _on_decide_policy)
        _log("[gtk-nav] file-drop open/guard installed")
    except Exception as e:
        _log(f"[gtk-nav] failed to connect decide-policy: {e}")


def _attach_window_chrome(window, api, start_maximized=False):
    """Apply the native custom-chrome shim (resize/snap/borderless) once the
    window is shown, then maximise (if requested) — maximising AFTER the shim is
    installed avoids a wrong-metrics first paint (thick border / taskbar overdraw).
    The maximize flag is only a non-Windows fallback; on Windows the real state is
    queried live (see WindowApi.toggle_maximize)."""
    def _on_shown():
        try:
            from platform_io import win_chrome
            win_chrome.apply(window)
        except Exception as e:
            _log(f"custom chrome not applied: {e}")
        _block_file_navigation_gtk(window)      # stop the Linux drag-drop hijack
        if start_maximized:
            try:
                window.maximize()
                api._maximized = True
            except Exception as e:
                _log(f"initial maximize failed: {e}")
    window.events.shown += _on_shown
    window.events.maximized += (lambda: setattr(api, '_maximized', True))
    window.events.restored  += (lambda: setattr(api, '_maximized', False))
    _register_native_drop(window)

    # Track this window's liveness so native actions can be routed to a window
    # that actually exists.  Newest window becomes the active one by default
    # (refined later by note_active() on focus).
    _LIVE_WINDOWS.append(window)
    _WINDOWS_BY_ID[api._id] = window
    _ACTIVE_WINDOW[0] = window

    _guard_window_close(window, api)

    def _on_closed():
        try:
            _LIVE_WINDOWS.remove(window)
        except ValueError:
            pass
        _WINDOWS_BY_ID.pop(api._id, None)
        # Drop the pop-out bookkeeping entry too, so closed pop-outs (and their
        # js_api) are no longer retained.
        _EXTRA_WINDOWS[:] = [(w, a) for (w, a) in _EXTRA_WINDOWS if w is not window]
        if _ACTIVE_WINDOW[0] is window:
            _ACTIVE_WINDOW[0] = _LIVE_WINDOWS[-1] if _LIVE_WINDOWS else None
        if not _LIVE_WINDOWS:
            _mark_closing()                 # last window gone: the process is exiting
    window.events.closed += _on_closed


def _guard_window_close(window, api):
    """Warn before a close that would throw away unsaved work.

    Every way of closing a window ends in WinForms' FormClosing (the OS/native
    frame X, Alt+F4, AND our own titlebar X, because WindowApi.close() calls
    destroy() which calls Close()) — and the cocoa/gtk backends fire the same
    `closing` event.  So this one handler covers them all, which the page cannot
    do on its own: `beforeunload` never fires in the native host.

    The handler runs synchronously on the UI thread, and the CSP forbids eval so
    evaluate_js cannot ask the page anything here.  Hence the push model: the
    page keeps api._unsaved current, and when a close is refused we ask the page
    to raise its own dialog and cancel this close.  If the user accepts, the page
    latches _quit_ok and closes again.

    That ask MUST go out on a worker thread.  pywebview's run_js is not
    fire-and-forget on WebView2: it blocks on a semaphore released by a
    continuation posted to the UI thread's sync context (edgechromium.py
    evaluate_js).  Calling it from here — on the UI thread, inside FormClosing —
    would block the very thread that has to run the continuation, and the app
    would hang on close instead of asking.

    _asking is the escape hatch.  If a prompt is already outstanding — the page
    is wedged, or the user clicked X again instead of answering — the close is
    allowed through, so a broken page can never trap the app open."""

    def _ask_page():
        try:
            # run_js, NOT evaluate_js — the CSP forbids the eval() wrapper.
            window.run_js('window.pwPromptQuit && window.pwPromptQuit()')
        except Exception as e:
            # The page cannot be reached, so nothing will ever answer.  Stand the
            # guard down entirely rather than just dropping the latch: re-arming
            # would cancel the next close too, and the one after that, and the
            # user could never shut the app.  One refused close, then it opens.
            _log(f"close guard: could not prompt the page: {e}")
            api._asking = False
            api._quit_ok = True

    def _on_closing():
        if api._quit_ok or not api._unsaved:
            return True                      # nothing to lose, or already agreed
        if api._asking:
            api._asking = False
            return True                      # second attempt wins; never trap the user
        api._asking = True
        threading.Thread(target=_ask_page, name='pw-close-prompt',
                         daemon=True).start()
        return False                         # cancel this close; the page will re-close

    window.events.closing += _on_closing


def _arm_native_drop_pull(window):
    """macOS equivalent of _register_native_drop, without pywebview's DOM API.

    pywebview's cocoa backend already reads the dropped file URLs off the
    NSPasteboard in WebKitHost.performDragOperation_ and appends (name, path)
    pairs to webview.dom._dnd_state['paths'] — but only when that state says
    something is listening.  Registering a real DOM listener is what needs eval
    (blocked by our CSP), so instead we just declare the interest directly and
    then *pull* the captured paths from the page.

    AppKit runs performDragOperation_ before the page's DOM drop event, so by the
    time the page asks (WindowApi.take_dropped_paths) the paths are already
    there.  If they are not — non-file drag, or a pywebview version that changed
    this internal — the page falls back to uploading the bytes, exactly as
    before, so a drop is never lost.

    This is what makes a dropped file open by its real path on macOS, which is
    what puts it in the recent-files list and gives the save dialog its source
    folder."""
    try:
        from webview.dom import _dnd_state
    except Exception as e:
        _log(f"native drop (macOS): pywebview dnd state unavailable: {e}")
        return

    try:
        # Tell the cocoa backend that dropped paths are wanted.  Never decremented:
        # the interest lasts for the lifetime of the window.
        _dnd_state['num_listeners'] += 1
    except Exception as e:
        _log(f"native drop (macOS): could not arm dnd capture: {e}")
        return

    def _flag():
        # run_js, NOT evaluate_js — the latter wraps the script in eval() and the
        # CSP forbids that.  Fire-and-forget, so no return value is needed.
        try:
            window.run_js("window._nativeDropPull = true;")
            _log("native drop (macOS): pasteboard path capture armed")
        except Exception as ex:
            _log(f"native drop (macOS): could not set page flag: {ex}")

    window.events.loaded += _flag


def _shallow_dom_serialization(window):
    """Stop pywebview deep-copying the DOM into every event it sends to Python.

    pywebview serialises a DOM event by walking every property of the Event
    object; any value that is a Node goes through ``domJSON.toJSON`` with its
    default ``deep: true``, which recursively serialises the node's ENTIRE
    subtree — attributes and all.  A drop on ``#layout-main`` therefore drags the
    whole page grid across the bridge, and every card carries its thumbnail as an
    inline ``data:image/png;base64,...`` attribute.  At ~100 pages that is
    megabytes of JSON built on the UI thread, so the drop froze the window for
    seconds and the real paths reached the page long after the 700 ms
    byte-upload fallback had already run — the file got added twice.

    Nothing here ever reads DOM structure out of an event (the drop handler wants
    ``dataTransfer.files`` and nothing else), and pywebview's own element
    serialisation already passes ``deep: false``, so forcing that everywhere is
    both safe and what the payload actually needs.  Best-effort: a pywebview
    build without ``domJSON`` just keeps the old behaviour."""
    try:
        window.evaluate_js("""
            (function () {
              var dj = window.pywebview && window.pywebview.domJSON;
              if (!dj || dj._ppShallow) return false;
              var orig = dj.toJSON;
              dj.toJSON = function (node, opts) {
                opts = opts || {};
                opts.deep = false;          // this node only, never its children
                return orig.call(dj, node, opts);
              };
              dj._ppShallow = true;
              return true;
            })();
        """)
    except Exception as ex:
        _log(f"shallow DOM serialization patch failed: {ex}")


def _register_native_drop(window):
    """Make drag-dropped files load by their REAL OS path.

    WebView2 hides a dropped file's path from page JS, but pywebview's DOM drop
    event enriches each file with `pywebviewFullPath`.  We register a Python drop
    handler on the layout's drop targets, forward the real paths to the page via
    onNativeFilesDropped(), and set window._nativeDropActive so the JS byte-upload
    fallback stands down.  If anything fails, the flag stays unset and the JS
    upload path keeps working (just without source-dir awareness).

    The enrichment itself lives in pywebview's backend-agnostic util.py and is
    fed by each platform backend pushing (name, real_path) pairs into
    webview.dom._dnd_state.  As of pywebview 5.4 the cocoa backend does this in
    WebKitHost.performDragOperation_ (reading file URLs off the NSPasteboard),
    structurally the same as edgechromium's FilesDropped handler — so macOS gets
    real paths too, and is armed here.

    Without this, a macOS drop fell through to the JS byte-upload path, which
    writes a *copy* into the temp upload folder.  _mru_record() skips temp paths
    by design, so dropped files never reached the recent-files list (and the save
    dialog lost its source-folder default).

    Linux (gtk/WebKitGTK) is deliberately NOT armed: that backend also feeds
    _dnd_state, but WebKitGTK additionally treats a dropped file as a navigation
    (see _block_gtk_drop_hijack), so it needs separate testing before switching.
    There it keeps using the JS upload path.

    Arming is safe even if a backend turns out not to deliver paths: the page
    only *defers* its byte-upload fallback (see armNativeDropFallback in
    shared.js) and runs it anyway if no real path arrives.

    macOS uses a different mechanism — see _arm_native_drop_pull.  pywebview's
    window.dom API cannot be used there: DOM.get_elements() goes through
    window.evaluate_js(), which wraps every script in a literal eval("..."), and
    this app's CSP grants script-src 'self' 'unsafe-inline' but deliberately NOT
    'unsafe-eval', so WKWebView refuses it with an EvalError and registration
    fails.  (WebView2 is not subject to that, which is why Windows works.)"""
    if _pp.is_macos():
        _arm_native_drop_pull(window)
        return
    if not _pp.is_windows():
        return
    try:
        from webview.dom import DOMEventHandler
    except Exception:
        return

    # Each drop target routes to the JS function appropriate for its panel:
    # the viewer opens each drop as a new tab; the layout appends to the grid.
    targets = {
        '#canvas-area':     'onViewerFilesDropped',
        '#layout-main':     'onLayoutFilesDropped',  # whole layout page, not just the bar
        '#compact-dropbar': 'onLayoutFilesDropped',
    }

    def _make_handler(js_func):
        def _on_drop(e):
            try:
                files = (e or {}).get('dataTransfer', {}).get('files', []) or []
                paths = [f.get('pywebviewFullPath') for f in files]
                paths = [p for p in paths if p]
                if paths:
                    window.evaluate_js(
                        f"window.{js_func} && window.{js_func}({_json.dumps(paths)})")
            except Exception as ex:
                _log(f"native drop handler error: {ex}")
        return _on_drop

    def _wire():
        try:
            _shallow_dom_serialization(window)
            wired = 0
            for sel, js_func in targets.items():
                for el in window.dom.get_elements(sel):
                    el.on('drop', DOMEventHandler(_make_handler(js_func), prevent_default=True))
                    wired += 1
            if wired:
                window.evaluate_js("window._nativeDropActive = true;")
            _log(f"native drop registered on {wired} element(s)")
        except Exception as ex:
            _log(f"native drop registration failed: {ex}")

    window.events.loaded += _wire


# ── Security hardening ────────────────────────────────────────────────────────
# Per-launch token: generated once, injected into the HTML, required on all API
# calls (except open_in_new_tab, which a second process posts before it has a token).
APP_TOKEN = secrets.token_hex(32)

# Rate-limiter state (in-memory, per remote IP)
_rate_lock   = threading.Lock()
_rate_reqs   = {}       # ip -> [timestamps]
_RATE_WINDOW = 10       # seconds
# Max requests per window.  This is a burst guard, NOT the access control — that
# is _check_origin() + _require_token(), and the token is a per-launch secret.
#
# The ceiling has to clear what the VIEWER legitimately does, which is far more
# than a human "clicking fast": every page that scrolls into view costs up to
# three calls (render + annotations + editable_annotations), the lazy-render
# observer warms PRERENDER_MARGIN pages either side, and a fast flick through a
# long document crosses dozens of pages a second.  At the old 120 that tripped
# after ~40 pages — and the 429 came back as Flask's HTML error page, which the
# viewer then fed to res.json(), so the user saw a baffling
# "Unexpected token '<', "<!doctype"..." render error mid-scroll.
#
# Two things stop that recurring: the client now bounds its own in-flight
# renders (see _renderQueue in viewer-panel.js) so the burst is much smaller,
# and everything below still answers in JSON (see _json_http_errors) with a
# Retry-After that apiFetch honours instead of erroring.
_RATE_LIMIT  = 1200

# Fully exempt endpoints: reachable only from 127.0.0.1 and low-sensitivity.
#   /api/ping      — identity probe a launching process needs BEFORE it knows it
#                    is us (so it cannot yet hold a token); returns only app id/
#                    pid/version.
#   /api/boot_mark — diagnostic timeline marker posted by the earliest page code.
_AUTH_EXEMPT = {'/api/boot_mark', '/api/ping'}

# open_in_new_tab is posted by a SECOND process (double-clicked file) that has no
# browser origin, so the origin check is skipped for it — but it now DOES require
# the per-launch token, which the second process reads from the user-private
# rendezvous file (see single_instance).  This closes the prior gap where any
# local web page that guessed the port could drive the app to open arbitrary
# local files.
_TOKEN_ONLY = {'/api/open_in_new_tab'}


def _check_origin():
    """Reject requests that came from a page other than our own.

    A request with NO Origin and no Referer is passed through to the token check
    that always follows, which is what actually guards these routes.  It has to
    be: browsers do not send Origin on same-origin GETs, and our own
    Referrer-Policy: no-referrer (added in 2.17) strips the Referer — so every
    GET /api/ route our page called was answered 403.  That silently broke the
    first-run licence check (the dialog reappeared on every launch, because the
    page read the error body as "never accepted") and the update check.

    Rejecting on a missing header bought nothing anyway.  A foreign page cannot
    set X-App-Token on a cross-origin request without a preflight we never
    answer, and cannot read the reply even if it could send one; a non-browser
    local caller never had an Origin to begin with, which is exactly why
    _TOKEN_ONLY routes already rely on the token alone.  A header that IS
    present must still be one of ours."""
    origin = request.headers.get('Origin') or request.headers.get('Referer', '')
    if not origin:
        return                          # no header to check → the token decides
    if not (origin.startswith('http://127.0.0.1:') or
            origin.startswith('http://localhost:')):
        abort(403)


def _require_token():
    """Reject requests that don't carry the per-session app token."""
    if request.headers.get('X-App-Token') != APP_TOKEN:
        abort(403)


def _rate_limit():
    """Reject requests from an IP that exceeds the per-window limit."""
    ip  = request.remote_addr
    now = time.time()
    with _rate_lock:
        reqs = [t for t in _rate_reqs.get(ip, []) if now - t < _RATE_WINDOW]
        if len(reqs) >= _RATE_LIMIT:
            # Tell the caller how long until the oldest request ages out, so a
            # throttled client can wait exactly that long and retry rather than
            # guessing (or surfacing the throttle to the user as an error).
            retry = max(1, int(_RATE_WINDOW - (now - reqs[0])) + 1)
            abort(make_response(
                jsonify({'error': 'Too many requests — slow down and retry.',
                         'retry_after': retry}),
                429, {'Retry-After': str(retry)}))
        reqs.append(now)
        _rate_reqs[ip] = reqs


# Every /api/ failure must answer in JSON.  Flask's default error pages are
# HTML, and the front end parses API replies with res.json() — so a 429 (rate
# limit), 403 (origin/token) or 500 arrived at the viewer as
# "Unexpected token '<', \"<!doctype\"... is not valid JSON", which tells the
# user nothing about what actually went wrong.  Non-API routes keep the normal
# HTML pages.
@app.errorhandler(HTTPException)
def _json_http_errors(e):
    if not request.path.startswith('/api/'):
        return e
    # abort(make_response(...)) already carries a JSON body (the rate limiter
    # attaches Retry-After that way) — pass it through untouched.
    resp = e.get_response()
    if (resp.headers.get('Content-Type') or '').startswith('application/json'):
        return resp
    out = jsonify({'error': e.description or e.name, 'status': e.code})
    out.status_code = e.code
    for k, v in resp.headers.items():
        if k.lower() in ('retry-after', 'www-authenticate'):
            out.headers[k] = v
    return out


@app.before_request
def _security():
    """Apply rate-limit + origin + token checks to all API routes."""
    if not request.path.startswith('/api/'):
        return                          # static pages don't need these checks
    _rate_limit()
    if request.path in _AUTH_EXEMPT:
        return                          # ping / boot_mark: localhost-only, low-sensitivity
    if request.path in _TOKEN_ONLY:
        _require_token()                # cross-process caller: token, but no browser origin
        return
    _check_origin()
    _require_token()


# ── Response hardening headers ────────────────────────────────────────────────
# A Content-Security-Policy locks the page to same-origin resources so that any
# content that ever slipped through output escaping (PDF-derived text, filenames)
# cannot load or exfiltrate to an external host.  The app is a self-contained
# local SPA: scripts/styles are same-origin plus inline (inline handlers and the
# injected token bootstrap need 'unsafe-inline'); images/fonts use data: URLs
# (base64 thumbnails, embedded faces); print/download build blob: URLs; all API
# traffic is same-origin.  'unsafe-eval' is deliberately NOT granted.
_CSP = ("default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "media-src 'self' data: blob:; "
        "frame-src 'self' blob:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'")


# Response types that must never be cached by the WebView.
#
# The frontend is served by the app's own localhost server, so HTTP caching buys
# nothing — but it actively BREAKS in-place upgrades.  send_file() sends no
# Cache-Control, and with none present a WebView applies *heuristic* freshness
# (a fraction of the file's age), so it can go on serving the PREVIOUS build's
# HTML/JS/version.json for days after an upgrade.  Observed in the wild: a 2.18
# Flatpak reported "Version 2.13" in About, served from a WebKitCache record
# left behind by the 2.13 build in ~/.var/app/<id>/cache.  The same hazard
# exists on Windows (WebView2) and macOS (WKWebView), and stale *JS* against a
# new backend is far worse than a stale version string.
#
# Deliberately scoped by content type: rendered page images and PDF payloads are
# excluded, because re-fetching those would force expensive re-renders.
_NO_STORE_TYPES = ('text/html', 'text/css', 'application/javascript',
                   'application/json', 'text/plain')


@app.after_request
def _security_headers(resp):
    """Attach CSP + defensive headers to every response (harmless on API/JSON)."""
    resp.headers.setdefault('Content-Security-Policy', _CSP)
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    # Assign, don't setdefault: this must override whatever send_file() set.
    ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    if ctype in _NO_STORE_TYPES:
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
    return resp


# The page's one API entry point, injected inline so the token never has to be
# fetched over a route of its own.  Besides attaching the token it absorbs a
# rate-limit throttle: a 429 is not a failure the user should ever see, it just
# means the viewer ran ahead of the burst budget (a fast scroll through a long
# document), so wait the Retry-After the server named and go again.  Without
# this the throttled reply reached res.json() and surfaced mid-scroll as
# "Unexpected token '<'".
_API_FETCH_JS = """
window.APP_TOKEN = "%TOKEN%";
window.API_RETRY_MAX = 3;
function apiFetch(u, o) {
  o = o || {};
  o.headers = Object.assign({"X-App-Token": window.APP_TOKEN || ""}, o.headers || {});
  var attempt = 0;
  function go() {
    return fetch(u, o).then(function (res) {
      if (res.status !== 429 || attempt >= window.API_RETRY_MAX) return res;
      attempt++;
      // Retry-After is whole seconds; fall back to a short exponential backoff.
      var ra   = parseFloat(res.headers.get("Retry-After"));
      var wait = (ra > 0) ? ra * 1000 : Math.min(2000, 250 * Math.pow(2, attempt - 1));
      return new Promise(function (resolve, reject) {
        var sig = o.signal;
        function onAbort() {
          clearTimeout(timer);
          var err = new Error("Aborted");
          err.name = "AbortError";
          reject(err);
        }
        var timer = setTimeout(function () {
          if (sig) sig.removeEventListener("abort", onAbort);
          resolve(go());
        }, wait);
        // An abort during the backoff must not resurrect the request later.
        if (sig) {
          if (sig.aborted) { onAbort(); return; }
          sig.addEventListener("abort", onAbort, { once: true });
        }
      });
    });
  }
  return go();
}
"""


def _inject_token(html):
    """Inject the per-session token and apiFetch helper before </head>, and the
    saved UI preferences straight after <head> — i18n.js picks its language as
    soon as it loads in the head, so they must be defined before it."""
    prefs  = '<script>window.PW_PREFS = ' + _json_mod.dumps(_boot_prefs()) + ';</script>'
    script = '<script>' + _API_FETCH_JS.replace('%TOKEN%', APP_TOKEN) + '</script>'
    html = html.replace('<head>', '<head>\n' + prefs, 1)
    return html.replace('</head>', script + '\n</head>', 1)


# ── UI preferences that must survive a change of port ─────────────────────────
# The theme used to live only in localStorage, and the page read its language
# back only from there.  localStorage belongs to the page's ORIGIN, which
# includes the port picked at launch (49371, then 49372-49375, then any free
# one — bootstrap/single_instance.py), and it sits in WebView2's data folder
# under the roaming %APPDATA%.  A launch on another port, or a work profile
# whose roaming data is reset, found nothing: the app came back dark — the
# "always reverts to dark mode after a restart" report.  The per-user data dir
# is now the source of truth; the page gets it as window.PW_PREFS.
_THEMES = ('light', 'dark')


def _ui_prefs_file():
    from platform_io import platform_paths
    return os.path.join(platform_paths.user_data_dir(), 'ui_prefs.json')


def _read_ui_prefs():
    try:
        with open(_ui_prefs_file(), 'r', encoding='utf-8') as f:
            prefs = _json_mod.load(f)
        return prefs if isinstance(prefs, dict) else {}
    except (OSError, ValueError):
        return {}


def _boot_prefs():
    """What the page should start with: None for anything never saved, so the
    page falls back to its own defaults (localStorage, then the OS language)."""
    theme = _read_ui_prefs().get('theme')
    try:
        from i18n import i18n_py
        lang = i18n_py.saved_lang()
    except Exception:
        lang = None
    return {'theme': theme if theme in _THEMES else None, 'lang': lang}


@app.route('/api/ui_prefs', methods=['POST'])
def api_ui_prefs():
    theme = (request.get_json(silent=True) or {}).get('theme')
    if theme not in _THEMES:
        return jsonify({'error': 'theme must be light or dark'}), 400
    prefs = _read_ui_prefs()
    prefs['theme'] = theme
    path = _ui_prefs_file()
    try:
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            _json_mod.dump(prefs, f)
        os.replace(path + '.tmp', path)
    except OSError as e:
        _log(f'ui_prefs: could not persist: {e}')
        return jsonify({'error': str(e)}), 500
    return jsonify({'theme': theme})


# ── Shutdown / lifecycle ──────────────────────────────────────────────────────
# pywebview owns the window lifecycle.  Closing the native window makes
# webview.start() return in main(), which then calls os._exit(0).  The OS window
# IS the liveness signal, so the old SSE keep-alive / watchdog / heartbeat /
# shutdown-beacon machinery has been removed entirely.

import json as _json

_LOG_FILE = os.path.join(tempfile.gettempdir(), 'patchworkpdf_shutdown.log')
_lock     = threading.Lock()


# Typographic characters that read as mojibake (e.g. "â€”") when the temp log is
# opened in a non-UTF-8 editor.  Transliterated to ASCII for the diagnostic log.
_LOG_ASCII = {
    '—': '-',  '–': '-',   # em / en dash
    '→': '->', '←': '<-',  # right / left arrow
    '‘': "'",  '’': "'",   # curly single quotes
    '“': '"',  '”': '"',   # curly double quotes
    '…': '...',                 # ellipsis
    ' ': ' ',                   # non-breaking space
}


def _log(msg):
    logger.info(msg)
    # Also write to a temp log for post-mortem diagnosis.  Keep this file
    # ASCII-only so it renders correctly in any editor regardless of encoding
    # (the rotating logfile.log is written as UTF-8 and keeps the original text).
    try:
        safe = str(msg)
        for uni, asc in _LOG_ASCII.items():
            safe = safe.replace(uni, asc)
        safe = safe.encode('ascii', 'replace').decode('ascii')
        with open(_LOG_FILE, 'a', encoding='utf-8') as _f:
            _f.write(f"{time.strftime('%H:%M:%S')} {safe}\n")
    except Exception:
        pass


# ── Second-instance IPC ─────────────────────────────────────────────────────

_APP_VERSION = None


def _app_version():
    """App version from version.json (cached), or '' if unavailable."""
    global _APP_VERSION
    if _APP_VERSION is None:
        try:
            with open(os.path.join(APP_DIR, 'version.json'), 'r', encoding='utf-8') as f:
                _APP_VERSION = _json.load(f).get('version', '')
        except Exception:
            _APP_VERSION = ''
    return _APP_VERSION


@app.route('/api/ping')
def ping():
    """Identity probe.  A launching process hits this to tell *this* app apart
    from any other server that happens to hold our port — it forwards files or
    shows "already running" only when the response carries our app id.
    `closing` tells it this instance is on its way out, so it should wait for
    the lock rather than hand over a document nobody will see.
    Auth-exempt and reachable only from 127.0.0.1."""
    return jsonify({
        'app': 'patchwork-pdf',
        'pid': os.getpid(),
        'version': _app_version(),
        'closing': _APP_CLOSING,
    })


# ── Closing: refuse hand-offs once the last window has gone ───────────────────
# Closing the window does not end the process at once: WebView2 tears down and
# the clean-exit purge runs first — ~3 s on a fast PC, much longer on a slow
# laptop — and the single-instance lock is held until the process is gone.  A
# document double-clicked in that gap used to either be handed to this dying
# process (loaded into a session no window would ever show, then lost with the
# process) or, once Flask stopped answering, meet "already running".  From the
# moment the last window closes this instance says so: /api/ping reports it and
# /api/open_in_new_tab answers 503, and the launching process waits for our lock
# to free and starts fresh (single_instance._defer_to_running_instance).
_APP_CLOSING = False


def _mark_closing():
    global _APP_CLOSING
    if not _APP_CLOSING:
        _APP_CLOSING = True
        _log('closing: no windows left — refusing hand-offs')


def _refuse_closing():
    return jsonify({'error': 'closing', 'closing': True}), 503


@app.route('/api/open_in_new_tab', methods=['POST'])
def open_in_new_tab():
    """Called by a second process (double-clicked file) to open a file as a new
    viewer tab in the already-running window, without starting a second backend.
    Runs on the Flask request thread — evaluate_js must be called from here, not
    a freshly-spawned daemon thread (which deadlocks on the COM/GUI marshal).
    Auth is exempt — this endpoint is reachable only from 127.0.0.1."""
    if _APP_CLOSING:
        return _refuse_closing()
    data      = request.json or {}
    file_path = data.get('file_path', '')
    # _pp.fs_path: a document on a deep share is past Windows' 260-char limit,
    # where a plain isfile() answers False and the hand-off rejects a file that
    # is plainly there.
    if not file_path or not os.path.isfile(_pp.fs_path(file_path)):
        return jsonify({'error': 'Invalid file path'}), 400

    # Come forward FIRST.  The user double-clicked a document and expects the app
    # to appear; _preload_file below can take many seconds on a large or scanned
    # PDF, and leaving the window minimised for all of it reads as "nothing
    # happened".  It also spends the forwarding process's foreground grant
    # (single_instance._grant_foreground) while that process is still blocked on
    # the POST, which is when the grant is valid.
    target = _target_window()
    if target is not None:
        try:
            from platform_io import win_chrome
            win_chrome.bring_to_front(target)
        except Exception as e:
            _log(f"open_in_new_tab: could not foreground window: {e}")
    else:
        _log("open_in_new_tab: no live window to push to")

    new_session_id = str(uuid.uuid4())
    sessions[new_session_id] = {'pages': [], 'first_file_path': file_path, 'comments': {}}
    _preload_file(file_path, new_session_id)

    if _APP_CLOSING:
        # The window closed while a long document was loading: nothing will
        # show it.  Hand it back so the launcher opens it in a fresh instance.
        sessions.pop(new_session_id, None)
        return _refuse_closing()

    if not sessions[new_session_id]['pages']:
        return jsonify({'error': 'Could not load file', 'error_key': 'err.could_not_load_file'}), 500

    _mru_record(file_path)
    new_page_id = sessions[new_session_id]['pages'][0]['id']

    # Open the tab in the window we fronted above (the focused one if possible).
    # Routing via the registry — not the fixed main-window handle — means this
    # still works after the original window has been closed while a popped-out
    # window stays open.
    if target is not None:
        try:
            # run_js: evaluate_js's eval() wrapper is CSP-blocked on macOS.
            target.run_js(
                f"window.openNewTab({_json.dumps(new_session_id)}, {_json.dumps(new_page_id)})")
        except Exception as e:
            _log(f"open_in_new_tab: could not push to window: {e}")
        # Restoring a window puts keyboard focus on the WinForms host, not the
        # WebView2 child, so the page's document-level shortcuts would be dead in
        # the tab we just opened (see CLAUDE.md, "Native-window traps").
        try:
            from platform_io import win_chrome
            win_chrome.focus_web_content(target)
        except Exception as e:
            _log(f"open_in_new_tab: could not restore web focus: {e}")

    _log(f"open_in_new_tab: {file_path} → session {new_session_id}")
    return jsonify({'session_id': new_session_id, 'page_id': new_page_id})


# In-memory session storage


# ── File-association startup state ────────────────────────────────────────────
# Set by main() when launched with a file path argument (e.g. Windows "open with").
INITIAL_SESSION_ID = None
INITIAL_PAGE_ID    = None


@app.route('/favicon.ico')
def favicon():
    ico_path = os.path.join(APP_DIR, 'assets', 'icon.ico')
    if os.path.isfile(ico_path):
        return send_file(ico_path, mimetype='image/x-icon')
    return '', 404


@app.route('/viewer.css')
def viewer_css():
    path = os.path.join(FRONTEND_CSS_DIR, 'viewer.css')
    if os.path.isfile(path):
        return send_file(path, mimetype='text/css')
    return '', 404

@app.route('/shared.js')
def shared_js():
    path = os.path.join(FRONTEND_JS_DIR, 'shared.js')
    if os.path.isfile(path):
        return send_file(path, mimetype='application/javascript')
    return '', 404

@app.route('/layout.js')
def layout_js():
    path = os.path.join(FRONTEND_JS_DIR, 'layout.js')
    if os.path.isfile(path):
        return send_file(path, mimetype='application/javascript')
    return '', 404

@app.route('/viewer-panel.js')
def viewer_panel_js():
    path = os.path.join(FRONTEND_JS_DIR, 'viewer-panel.js')
    if os.path.isfile(path):
        return send_file(path, mimetype='application/javascript')
    return '', 404

@app.route('/i18n.js')
def i18n_js():
    path = os.path.join(FRONTEND_JS_DIR, 'i18n.js')
    if os.path.isfile(path):
        return send_file(path, mimetype='application/javascript')
    return '', 404

# Per-language UI dictionaries. The <lang> is whitelisted to the supported set
# so this route can never be coaxed into serving arbitrary files.
_SUPPORTED_LANGS = ('en', 'es', 'fr', 'de', 'it')

@app.route('/locales/<lang>.json')
def locale_json(lang):
    if lang not in _SUPPORTED_LANGS:
        return '', 404
    path = os.path.join(APP_DIR, 'locales', lang + '.json')
    if os.path.isfile(path):
        return send_file(path, mimetype='application/json')
    return '', 404

@app.route('/api/set_lang', methods=['POST'])
def api_set_lang():
    """Persist the UI language so pre-browser code (splash, single-instance
    dialog) and native file dialogs can pick it up."""
    lang = (request.get_json(silent=True) or {}).get('lang', '')
    from i18n import i18n_py
    i18n_py.set_lang(lang)
    return jsonify({'ok': True, 'lang': i18n_py.get_lang()})

# The name stamped as the author of everything the user adds (comments,
# replies, status changes).  Kept with the other per-user settings rather than
# in localStorage: the page's origin includes a port that can change between
# launches (bootstrap/single_instance.py), and localStorage would forget it.
def _reviewer_file():
    from platform_io import platform_paths
    return os.path.join(platform_paths.user_data_dir(), 'reviewer.json')


def _clean_reviewer_name(value):
    name = ''.join(ch for ch in str(value or '') if ch.isprintable())
    return ' '.join(name.split())[:100]


@app.route('/api/reviewer_name', methods=['GET', 'POST'])
def api_reviewer_name():
    if request.method == 'POST':
        name = _clean_reviewer_name((request.get_json(silent=True) or {}).get('name', ''))
        try:
            with open(_reviewer_file(), 'w', encoding='utf-8') as f:
                _json.dump({'name': name}, f)
        except OSError as e:
            _log(f'reviewer_name: could not persist: {e}')
            return jsonify({'error': str(e)}), 500
        return jsonify({'name': name})
    try:
        with open(_reviewer_file(), 'r', encoding='utf-8') as f:
            name = _clean_reviewer_name((_json.load(f) or {}).get('name', ''))
    except (OSError, ValueError, AttributeError):
        name = ''
    return jsonify({'name': name})


@app.route('/version.json')
def version_json():
    path = os.path.join(APP_DIR, 'version.json')
    if os.path.isfile(path):
        return send_file(path, mimetype='application/json')
    return '', 404


# ── Usage license ─────────────────────────────────────────────────────────────
# LICENSE.txt is shown from the About box and, on first run, in a blocking
# acceptance dialog.  Acceptance is persisted next to recent_files.json in
# %LOCALAPPDATA%\PatchworkPDF so it survives upgrades but is per-user.

@app.route('/license.txt')
def license_txt():
    path = os.path.join(APP_DIR, 'LICENSE.txt')
    if os.path.isfile(path):
        return send_file(path, mimetype='text/plain')
    return '', 404


def _license_state_file():
    from platform_io import platform_paths
    return os.path.join(platform_paths.user_data_dir(), 'license_accepted.json')


@app.route('/api/license_status')
def license_status():
    try:
        with open(_license_state_file(), 'r', encoding='utf-8') as f:
            data = _json.load(f)
        return jsonify({'accepted': bool(data.get('accepted'))})
    except Exception:
        return jsonify({'accepted': False})


@app.route('/api/accept_license', methods=['POST'])
def accept_license():
    version = ''
    try:
        with open(os.path.join(APP_DIR, 'version.json'), 'r', encoding='utf-8') as f:
            version = _json.load(f).get('version', '')
    except Exception:
        pass
    try:
        with open(_license_state_file(), 'w', encoding='utf-8') as f:
            _json.dump({'accepted': True, 'version': version,
                        'when': time.time()}, f, indent=1)
        _log('license accepted (first run)')
        return jsonify({'ok': True})
    except Exception as e:
        _log(f'license: could not persist acceptance: {e}')
        return jsonify({'error': str(e)}), 500


# ── "What's New" (run once per version) ───────────────────────────────────────
# v3.1 moved the editor's tools into the viewer and removed the Editor button.
# Something people used every day is no longer where it was, so they are told
# once rather than left to conclude the button is broken.
#
# Shown at most once per version, and NEVER to a brand-new user: someone who has
# never seen the old layout has nothing to be told.  "Has this user been here
# before?" is answered by the license acceptance file, which every returning
# user has and no first-run user does.

def _whats_new_state_file():
    from platform_io import platform_paths
    return os.path.join(platform_paths.user_data_dir(), 'whats_new.json')


def _app_version():
    try:
        with open(os.path.join(APP_DIR, 'version.json'), 'r', encoding='utf-8') as f:
            return str(_json.load(f).get('version', ''))
    except Exception:
        return ''


@app.route('/api/whats_new_status')
def whats_new_status():
    """Answer whether this user should see the What's New note for this version.

    FAILS CLOSED, like /api/license_status: only an explicit, successfully-read
    "not yet shown" opens the dialog.  A state file we cannot read is not
    evidence the user has never seen it — treating it as such is exactly how the
    first-run licence wall once appeared on every single launch.
    """
    version = _app_version()
    try:
        # A user with no license record has never run an older build, so there
        # is no change for them to be surprised by.
        if not os.path.isfile(_license_state_file()):
            return jsonify({'show': False, 'version': version})
        try:
            with open(_whats_new_state_file(), 'r', encoding='utf-8') as f:
                seen = str((_json.load(f) or {}).get('shown_for', ''))
        except FileNotFoundError:
            seen = ''
        return jsonify({'show': seen != version, 'version': version})
    except Exception as e:
        _log(f"whats_new: status unreadable, not showing: {e}")
        return jsonify({'show': False, 'version': version})


@app.route('/api/whats_new_ack', methods=['POST'])
def whats_new_ack():
    """Remember that this version's note has been shown."""
    version = _app_version()
    try:
        with open(_whats_new_state_file(), 'w', encoding='utf-8') as f:
            _json.dump({'shown_for': version, 'when': time.time()}, f, indent=1)
        return jsonify({'ok': True})
    except Exception as e:
        _log(f'whats_new: could not persist acknowledgement: {e}')
        return jsonify({'error': str(e)}), 500


# ── Update notifications ──────────────────────────────────────────────────────
# Once the page has rendered it asks /api/update_check (off the UI thread)
# whether a newer release is published on SourceForge.  The check itself
# (changelog download + installer existence) lives in check_for_updates.py and
# fails soft.  The user can opt out of future prompts; that preference is stored
# next to the license/recent-files state in %LOCALAPPDATA%\PatchworkPDF.

# Run the (network) check at most once per process: every window loads the page
# and would otherwise re-prompt, and a pop-out opened later would prompt again.
_update_check_lock = threading.Lock()
_update_checked    = [False]

# Across process restarts we also throttle to at most one network check per day.
# The last check's timestamp and result are cached in update_prefs.json, so a
# user who launches the app several times a day only reaches SourceForge once —
# important when the user base is large.
_UPDATE_CHECK_INTERVAL = 24 * 60 * 60      # seconds


def _update_prefs_file():
    from platform_io import platform_paths
    return os.path.join(platform_paths.user_data_dir(), 'update_prefs.json')


def _read_update_prefs():
    """Return the update-prefs dict ({} if unreadable/absent)."""
    try:
        with open(_update_prefs_file(), 'r', encoding='utf-8') as f:
            data = _json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_update_prefs(prefs):
    """Persist the update-prefs dict, swallowing I/O errors (fail soft)."""
    try:
        with open(_update_prefs_file(), 'w', encoding='utf-8') as f:
            _json.dump(prefs, f, indent=1)
    except Exception as e:
        _log(f'update prefs: could not persist: {e}')


def _update_notifications_enabled():
    return bool(_read_update_prefs().get('notify', True))  # default: notify


@app.route('/api/update_check')
def update_check():
    """Report whether a newer release is published online.  Honours the user's
    opt-out preference (returns immediately, no network, when disabled) and runs
    the actual check only once per process so a second window never re-prompts.

    A manual "Check for Updates" from the About box passes ``?force=1``: an
    explicit click bypasses both the opt-out short-circuit and the once-per-
    process / 24h throttles, so it always performs a live check.  Every response
    carries the current ``notify`` preference so the update dialog can show — and
    let the user flip — whichever way they currently stand.

    Executes on a Flask worker thread, so the round-trip never blocks the UI."""
    force     = request.args.get('force') in ('1', 'true', 'yes')
    notify_on = _update_notifications_enabled()

    if not force and not notify_on:
        return jsonify({'new_release': False, 'opted_out': True, 'notify': notify_on})

    with _update_check_lock:
        if not force and _update_checked[0]:
            return jsonify({'new_release': False, 'already_checked': True, 'notify': notify_on})
        _update_checked[0] = True

    # Throttle across restarts: if the last *successful* online check was under
    # 24h ago, reuse its cached result and touch the network not at all.  A forced
    # (manual) check skips the cache — an explicit click should always be live.
    # The cache is only valid for the version that wrote it: right after an
    # upgrade the app version changes, and replaying the pre-upgrade result
    # would falsely announce the update the user just installed.
    prefs = _read_update_prefs()
    last  = prefs.get('last_check_ts')
    cached = prefs.get('last_result')
    if (not force and isinstance(last, (int, float)) and isinstance(cached, dict)
            and 0 <= (time.time() - last) < _UPDATE_CHECK_INTERVAL
            and cached.get('current_version') == (_app_version() or None)):
        out = dict(cached)
        out['cached'] = True
        out['notify'] = notify_on
        return jsonify(out)

    try:
        from services.check_for_updates import check_for_updates as _cfu
        result = _cfu(current_version=_app_version() or None)
        if result.new_release:
            _log(f"Update available: {result.release_version} "
                 f"(running {result.current_version})")
        data = result.to_dict()
        # Only cache a clean online result — a transient network/parse error must
        # not pin us for 24h; the next launch should retry.
        if result.checked_online and not result.error:
            prefs['last_check_ts'] = time.time()
            prefs['last_result']   = data
            _write_update_prefs(prefs)
        data['notify'] = notify_on
        return jsonify(data)
    except Exception as e:                   # never let an update check break the app
        _log(f"update_check failed: {e}")
        return jsonify({'new_release': False, 'error': str(e), 'notify': notify_on})


@app.route('/api/update_set_notify', methods=['POST'])
def update_set_notify():
    """Set whether the user receives update notifications — in *either*
    direction.  Body: ``{"notify": true|false}``.

    This backs the update dialog's "Don't notify me about future updates"
    checkbox, which is now two-way: a user who once opted out can turn
    notifications back on (typically after a manual "Check for Updates"), not
    just off.  Cached check state in the prefs file is preserved."""
    data   = request.json or {}
    notify = bool(data.get('notify', True))
    try:
        prefs = _read_update_prefs()          # keep any cached check state
        prefs['notify'] = notify
        prefs['when']   = time.time()
        _write_update_prefs(prefs)
        _log(f'update notifications set: notify={notify}')
        return jsonify({'ok': True, 'notify': notify})
    except Exception as e:
        _log(f'update_set_notify: could not persist: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/open_external', methods=['POST'])
def open_external():
    """Open an external URL in the user's default browser (the update prompt's
    Download button).  Restricted to SourceForge https links so the page cannot
    drive the OS browser to an arbitrary destination."""
    data = request.json or {}
    url  = str(data.get('url', ''))
    if not (url.startswith('https://') and 'sourceforge.net/' in url):
        return jsonify({'error': 'URL not allowed'}), 400
    try:
        import webbrowser
        webbrowser.open(url)
        _log(f'opened download page: {url}')
        return jsonify({'ok': True})
    except Exception as e:
        _log(f'open_external failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/fonts/<path:filename>')
def bundled_font(filename):
    """Serve the bundled .ttf faces (Option B font matching) for the editor's
    @font-face preview. Restricted to .ttf files inside assets/fonts."""
    if not filename.lower().endswith('.ttf') or '/' in filename or '\\' in filename:
        return '', 404
    path = os.path.join(APP_DIR, 'assets', 'fonts', filename)
    if os.path.isfile(path):
        return send_file(path, mimetype='font/ttf')
    return '', 404


@app.route('/')
def index():
    html_path = os.path.join(FRONTEND_DIR, 'viewer.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        return _inject_token(f.read()), 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/api/open_bundled_file', methods=['POST'])
def open_bundled_file():
    """Load a bundled app file (manual, changelog) into a new session so the
    viewer can open it in a new tab.  Only explicitly whitelisted filenames
    are accepted to prevent directory traversal."""
    data     = request.json or {}
    filename = data.get('filename', '')
    ALLOWED  = {'changelog.docx', 'PatchworkPDF_Manual.pdf'}
    if filename not in ALLOWED:
        return jsonify({'error': 'File not allowed'}), 400
    file_path = os.path.join(APP_DIR, 'docs', filename)
    if not os.path.isfile(file_path):
        return jsonify({'error': f'{filename} not found'}), 404

    new_session_id = str(uuid.uuid4())
    sessions[new_session_id] = {'pages': [], 'first_file_path': file_path, 'comments': {}}
    _preload_file(file_path, new_session_id)

    if not sessions[new_session_id]['pages']:
        return jsonify({'error': f'Could not load {filename}'}), 500

    first_page_id = sessions[new_session_id]['pages'][0]['id']
    _log(f'open_bundled_file: {filename} → session {new_session_id}')
    return jsonify({'session_id': new_session_id, 'page_id': first_page_id})


# ── Leftover editor drafts ────────────────────────────────────────────────────
# Builds before v3.1 kept the Editor's un-applied edits here as JSON deltas.
# Nothing writes them any more — every viewer edit commits immediately — so the
# only thing left to do with them is dispose of any an older build left behind:
# /api/drafts/clear (called at launch) and the clean-exit purge.  The save/get/
# delete/list routes went with the Editor.
def _drafts_dir():
    try:
        d = os.path.join(_single_instance._state_dir(), 'drafts')
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None

def _draft_path(doc_key):
    """Hash the (arbitrary, possibly unsafe) doc key into a stable filename."""
    h = hashlib.sha1((doc_key or '').encode('utf-8')).hexdigest()
    d = _drafts_dir()
    return os.path.join(d, h + '.json') if d else None


def _purge_all_drafts():
    """Remove every stored editor draft.  Shared by the Discard-all route and the
    clean-shutdown cleanup."""
    d = _drafts_dir()
    if not d or not os.path.isdir(d):
        return
    for name in os.listdir(d):
        if name.endswith('.json'):
            try: os.remove(os.path.join(d, name))
            except OSError: pass


@app.route('/api/drafts/clear', methods=['POST'])
def drafts_clear():
    """Discard drafts: a specific list of doc keys, or all when none supplied."""
    data = request.json or {}
    keys = data.get('docKeys')
    if keys:
        for k in keys:
            p = _draft_path(k)
            if p and os.path.isfile(p):
                try: os.remove(p)
                except OSError: pass
    else:
        _purge_all_drafts()
    return jsonify({'ok': True})


# ── Applied/committed-but-unsaved recovery (working-bytes snapshots) ──────────
# Complements the un-applied JSON draft store above: recovery.py snapshots the
# actual per-page working PDF bytes (viewer annotations + editor Apply) via a
# background daemon so they survive a crash.  These routes drive the launch-time
# recovery prompt and restore.  Encrypted docs are never snapshotted.
@app.route('/api/recovery/list', methods=['POST'])
def recovery_list():
    """Metadata for every stored working-bytes snapshot (no page bytes)."""
    return jsonify({'snapshots': _recovery.list_manifests()})


@app.route('/api/recovery/restore', methods=['POST'])
def recovery_restore():
    """Rebuild a session from a stored snapshot and return its page list. The
    full bytes are reloaded, so no original file is needed."""
    data   = request.json or {}
    doc_id = data.get('docId')
    snap   = _recovery.load(doc_id) if doc_id else None
    if not snap:
        return jsonify({'error': 'Snapshot not found'}), 404

    new_session_id = str(uuid.uuid4())
    pages = []
    for p in snap['pages']:
        pages.append({
            'id':          p['id'],
            'filename':    p['filename'],
            'page_num':    p['page_num'],
            'total_pages': p['total_pages'],
            'rotation':    p.get('rotation', 0),
            'thumbnail':   p.get('thumbnail'),
            'pdf_bytes':   p['pdf_bytes'],
            # Filled form values ride in the snapshot (they are unsaved work like
            # any other); an older snapshot that predates that simply has none.
            'form_fields': p.get('form_fields') or [],
            'width_pts':   p.get('width_pts', 595.28),
            'height_pts':  p.get('height_pts', 841.89),
        })
    comments  = snap.get('comments')  if isinstance(snap.get('comments'),  dict) else {}
    bookmarks = snap.get('bookmarks') if isinstance(snap.get('bookmarks'), list) else []
    metadata  = snap.get('metadata')  if isinstance(snap.get('metadata'),  dict) else {}
    sessions[new_session_id] = {
        'pages':           pages,
        'first_file_path': snap.get('source_path'),
        'comments':        comments,
        'bookmarks':       bookmarks,
        'metadata':        metadata,
        '_rec_id':         doc_id,          # keep updating the same snapshot dir
    }
    _log(f'recovery_restore: {doc_id} → session {new_session_id} ({len(pages)} pages)')
    return jsonify({
        'session_id':   new_session_id,
        'is_protected': False,
        'pages': [{k: v for k, v in p.items() if k != 'pdf_bytes'} for p in pages],
    })


@app.route('/api/recovery/clear', methods=['POST'])
def recovery_clear():
    """Drop a snapshot — by live session id (after Save) or by doc id (Discard)."""
    data = request.json or {}
    sid  = data.get('session_id')
    if sid and sid in sessions:
        _recovery.clear(sessions[sid])
    elif data.get('docId'):
        _recovery.purge(data['docId'])
    return jsonify({'ok': True})


@app.route('/api/boot_mark', methods=['POST'])
def boot_mark():
    """Diagnostic-only: record a browser-side bootstrap milestone on the server
    timeline (see boot_timing.py).  Auth-exempt so the earliest page scripts can
    report before the app token is in scope; localhost-bound and logs only a
    label + the client's performance.now()."""
    data  = request.get_json(silent=True) or {}
    label = str(data.get('label', ''))[:80]
    cms   = data.get('t')
    try:
        cms = float(cms) if cms is not None else None
    except (TypeError, ValueError):
        cms = None
    _boot.mark('client: ' + label, client_ms=cms)
    return ('', 204)


# Register editor and viewer routes
register_core_routes(app, sessions, _inject_token)
register_layout_routes(app, sessions, _inject_token)
register_editor_routes(app, sessions, _inject_token)
register_viewer_routes(app, sessions, _inject_token)
_recovery.start()          # background working-bytes snapshotter
_boot.mark('flask routes registered')


# ── Frozen executable (cx_Freeze) path resolution ────────────────────────────
# When running as a built executable, sys.frozen is True and the HTML files
# live alongside the executable rather than relative to this source file.
def get_app_dir():
    """Return the directory containing the HTML files and assets (project root)."""
    if getattr(sys, 'frozen', False):
        # cx_Freeze: executable directory (assets/, locales/, src/frontend/ sit here)
        return os.path.dirname(sys.executable)
    # Development: this file is <root>/src/backend/entry.py — climb to <root>.
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_DIR = get_app_dir()

# Frontend (web UI) assets live under src/frontend/ — kept as the same relative
# layout in both dev and frozen builds so these constants resolve identically.
FRONTEND_DIR = os.path.join(APP_DIR, 'src', 'frontend')
FRONTEND_JS_DIR = os.path.join(FRONTEND_DIR, 'js')
FRONTEND_CSS_DIR = os.path.join(FRONTEND_DIR, 'css')

# Route HTML files from app directory (supports both dev and frozen builds)
# In v2.00 the app is a unified SPA served from viewer.html; individual panel
# HTML files are no longer served as separate pages.


def main():
    """Main entry point — used by both direct execution and cx_Freeze build."""
    global INITIAL_SESSION_ID, INITIAL_PAGE_ID

    no_browser = bool(os.environ.get('PATCHWORK_NO_BROWSER', ''))
    debug      = bool(os.environ.get('PATCHWORK_DEBUG', ''))

    # ── File-association: accept a file path as the first argument ────────────
    initial_file = _single_instance.find_initial_file()

    # ── Single-instance arbitration + port selection ──────────────────────────
    # Acquire the machine-wide single-instance lock and a free port to serve on.
    # If another instance already owns the lock, this hands the file off to it
    # (or shows the "already running" message) and returns None so we exit.  The
    # import-time fast path already handled the common already-running case
    # before the heavy imports; this is the authoritative arbiter and also
    # covers callers that import entry and invoke main() directly.
    port = _single_instance.become_primary_or_handoff(initial_file, ipc_token=APP_TOKEN)
    if port is None:
        sys.exit(0)

    _boot.mark('main() begin')
    if _SPLASH:
        _splash.set_status(_pt('splash.preparing', 'Preparing…'))
    _log("Patchwork PDF starting, PID=" + str(os.getpid()) + ", port=" + str(port))

    # ── Pre-load the file before Flask starts so viewer URL is ready ──────────
    # On THIS thread, before the window exists — never on a worker.  v3.3 moved
    # it to a background thread to overlap the WebView start-up, and the
    # double-clicked document then crashed the app about half the time:
    # pdfium/pikepdf ran while this thread loaded the .NET runtime and WinForms,
    # the shared CRT heap was corrupted, and pdfium aborted (0x80000003 — the
    # crash described in routes/viewer.py).  _NATIVE_LOCK cannot help there: the
    # CLR start-up is not ours to serialise.  The overlap saved 0.1–0.5 s.
    start_url = f'http://127.0.0.1:{port}'
    if initial_file:
        _log(f"File-association: pre-loading {initial_file}")
        INITIAL_SESSION_ID = str(uuid.uuid4())
        sessions[INITIAL_SESSION_ID] = {'pages': [], 'first_file_path': initial_file, 'comments': {}}
        _preload_file(initial_file, INITIAL_SESSION_ID)
        _boot.mark('initial file pre-loaded')
        if sessions[INITIAL_SESSION_ID]['pages']:
            # Opening a file by double-click / "Open With" is a real open of a
            # real path, so it belongs in the MRU.  The handoff route used when
            # the app is ALREADY running (/api/open_in_new_tab) records it; this
            # cold-start branch did not, so the same action produced a recent-
            # files entry or not depending on whether the app happened to be
            # open.  All platforms.
            _mru_record(initial_file)
            INITIAL_PAGE_ID = sessions[INITIAL_SESSION_ID]['pages'][0]['id']
            start_url = (f'http://127.0.0.1:{port}/viewer'
                         f'?session_id={INITIAL_SESSION_ID}&page_id={INITIAL_PAGE_ID}')
            _log(f"Viewer URL: {start_url}")
        elif sessions[INITIAL_SESSION_ID].get('needs_password'):
            # Encrypted file — open viewer with session so it can show the password prompt
            start_url = f'http://127.0.0.1:{port}/viewer?session_id={INITIAL_SESSION_ID}'
            _log(f"Encrypted file — waiting for password, URL: {start_url}")

    # ── Window strategy ───────────────────────────────────────────────────────
    # pywebview owns the main thread and wraps Flask in a native window; closing
    # the window ends webview.start() and exits the process.  PATCHWORK_NO_BROWSER
    # runs Flask headless (no window) for dev/testing — open a browser manually.
    webview = None
    if not no_browser:
        try:
            import webview as _webview
            webview = _webview
            if _pp.is_linux():
                _csp_safe_js_bridge()      # before any window injects api.js
        except Exception as e:
            _log(f"pywebview unavailable ({e}); running headless — browse to {start_url}")

    if webview is None:
        # Headless: blocking Flask, no native window. Drop the splash if one was
        # shown (e.g. pywebview import failed after we'd already started it).
        if _SPLASH:
            _splash.close()
        # host is always 127.0.0.1 — never expose to the network.
        # No WebView2 startup to compete with, so load heavy deps right away.
        start_heavy_init_background()
        _log(f"Headless mode — serving on {start_url}")
        app.run(host='127.0.0.1', port=port, debug=debug, threaded=True, use_reloader=False)
        return

    global WV_WINDOW

    # Bind the socket HERE, on the main thread, then serve from a worker.  This
    # used to start app.run() on the thread and poll the port until it answered,
    # which cost ~300 ms on every launch: the first poll lands before the bind,
    # and on Windows a connect to a closed loopback port is not refused promptly
    # (measured ~2 s), so it sat out the full probe timeout.  make_server returns
    # with the socket already listening, so the window can be pointed at it at
    # once.  threaded=True so concurrent API calls (tab switches) don't block.
    from werkzeug.serving import make_server
    _server = make_server('127.0.0.1', port, app, threaded=True)
    threading.Thread(target=_server.serve_forever, name='pw-flask',
                     daemon=True).start()
    _boot.mark('flask server listening')
    if _SPLASH:
        _splash.set_status(_pt('splash.starting_interface', 'Starting interface…'))

    _log("Launching pywebview window")
    api = WindowApi()
    WV_WINDOW = webview.create_window(
        'Patchwork PDF',
        url=start_url,
        width=1280, height=860,                # opened, then maximised after chrome
        frameless=_USE_CUSTOM_CHROME,          # custom dark titlebar on Windows; native frame elsewhere
        background_color='#0f0f11',            # --bg, avoids white flash
        js_api=api,
    )
    api._bind(WV_WINDOW)
    _attach_window_chrome(WV_WINDOW, api, start_maximized=True)
    _boot.mark('webview window created + chrome attached')
    if _SPLASH:
        _splash.set_status(_pt('splash.opening_window', 'Opening window…'))

    # Mark when WebView2 reports the page shown / loaded (handlers fire in order
    # they were added, so these run alongside the heavy-init kickoff below).
    WV_WINDOW.events.shown  += lambda: _boot.mark('window shown (WebView2)')
    WV_WINDOW.events.loaded += lambda: _boot.mark('window loaded (WebView2 page ready)')

    # Dismiss the splash once the real page has rendered — this is the hand-off
    # point, so the splash bridges right up to the live UI appearing.
    if _SPLASH:
        WV_WINDOW.events.loaded += lambda: _splash.close()

    # Load the editor's heavy deps (numpy, pypdfium2, PIL, Tesseract probe)
    # only once the page is up, so their disk/CPU load doesn't compete with
    # WebView2 startup.  Idempotent — `loaded` fires again on navigations.
    WV_WINDOW.events.loaded += start_heavy_init_background

    _boot.mark('webview.start() — handing the main thread to the WebView backend')
    # Blocks the main thread until the window is closed, then returns.  The GUI
    # backend is per-OS: edgechromium (WebView2) on Windows, cocoa (WKWebView) on
    # macOS, gtk (WebKitGTK) on Linux.  gui=None lets pywebview auto-select.
    _gui = _webview_gui()
    if _gui:
        webview.start(gui=_gui, private_mode=False)
    else:
        webview.start(private_mode=False)

    # Window closed → tear everything down. The daemon Flask thread dies with the
    # process; os._exit avoids waiting on any lingering non-daemon threads.
    #
    # This line is reached ONLY on a clean, commanded shutdown — every window was
    # closed and webview.start() returned normally. A crash, kill, or power loss
    # never gets here. The crash guard exists purely to recover from those
    # abnormal exits, so any draft / working-bytes snapshot left over from this run
    # is now stale: clear it so the next launch starts clean instead of re-offering
    # recovery for work the user already walked away from. (os._exit below skips
    # atexit, so this is the only reliable clean-exit hook.)
    _mark_closing()          # normally already set by the last window's `closed`
    try:
        _purge_all_drafts()
    except Exception as e:
        _log(f"draft cleanup on exit failed: {e}")
    try:
        _recovery.purge_all()
    except Exception as e:
        _log(f"recovery cleanup on exit failed: {e}")

    _log("Window closed — exiting.")
    os._exit(0)


if __name__ == '__main__':
    main()
