"""
Windows custom-chrome shim for the frameless pywebview window.

A frameless pywebview window (FormBorderStyle.None) has no WS_THICKFRAME, so it
loses edge-resize and Aero Snap.  This module re-adds WS_THICKFRAME and subclasses
the window procedure so that:

  • WM_GETMINMAXINFO → a maximised window fills the monitor WORK AREA (taskbar
                       stays visible) and never overdraws the taskbar.
  • WM_NCCALCSIZE    → the client area covers the whole window (no visible frame),
                       so our HTML titlebar renders full-bleed with no border.
  • WM_NCHITTEST     → thin outer band = resize edges/corners; the top strip
                       (minus the window-control buttons on the right) = HTCAPTION,
                       giving native dragging, drag-to-edge Aero Snap and
                       double-click-to-maximise for free.

Everything is wrapped in try/except: if anything fails the window still works,
just without snap/resize.  Disable entirely with PATCHWORK_NATIVE_CHROME=0.

TITLEBAR_H MUST match --titlebar-h in the #app-titlebar CSS (viewer.css: 34px).
Which parts of the titlebar are draggable is reported dynamically by JS via
set_drag_regions() (the empty spacer), so tabs/buttons stay clickable.
"""

import os
import sys
import ctypes
from ctypes import wintypes

# Logical (96-DPI) titlebar height — kept in sync with viewer.css --titlebar-h.
TITLEBAR_H = 34

# ── Win32 constants ───────────────────────────────────────────────────────────
GWL_STYLE      = -16
GWLP_WNDPROC   = -4

WS_CAPTION     = 0x00C00000
WS_THICKFRAME  = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU     = 0x00080000

WM_GETMINMAXINFO = 0x0024
WM_NCCALCSIZE    = 0x0083
WM_NCHITTEST     = 0x0084
WM_DESTROY       = 0x0002

SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE       = 0x0002
SWP_NOSIZE       = 0x0001
SWP_NOZORDER     = 0x0004
SWP_NOACTIVATE   = 0x0010

SW_SHOWMAXIMIZED = 3
MONITOR_DEFAULTTONEAREST = 2

HTCLIENT      = 1
HTCAPTION     = 2
HTLEFT        = 10
HTRIGHT       = 11
HTTOP         = 12
HTTOPLEFT     = 13
HTTOPRIGHT    = 14
HTBOTTOM      = 15
HTBOTTOMLEFT  = 16
HTBOTTOMRIGHT = 17

LRESULT = ctypes.c_ssize_t
WPARAM  = ctypes.c_size_t
LPARAM  = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", _POINT), ("ptMaxSize", _POINT),
                ("ptMaxPosition", _POINT), ("ptMinTrackSize", _POINT),
                ("ptMaxTrackSize", _POINT)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_uint)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", _POINT), ("ptMaxPosition", _POINT),
                ("rcNormalPosition", _RECT)]


# Keep references alive so callbacks aren't garbage-collected while the window is
# open (one entry per hwnd).
_subclasses = {}

# Draggable ("caption") regions of the titlebar, reported by JS in CSS pixels
# relative to the window's top-left: hwnd -> [(left, top, right, bottom), ...].
# Anything in the titlebar strip NOT inside one of these is treated as client
# (so tabs and the window-control buttons receive clicks).
_drag_regions = {}


def set_drag_regions(window, rects):
    """Called from the JS bridge whenever the titlebar layout changes.  `rects`
    is a list of [left, top, right, bottom] in CSS pixels."""
    if sys.platform != 'win32':
        return
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return
    try:
        _drag_regions[hwnd] = [tuple(r) for r in (rects or []) if r and len(r) == 4]
    except Exception:
        _drag_regions[hwnd] = []


def _user32():
    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype  = ctypes.c_void_p
    u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowLongPtrW.restype  = ctypes.c_void_p
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    u.CallWindowProcW.restype    = LRESULT
    u.CallWindowProcW.argtypes   = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
    return u


def _hwnd_of(window):
    try:
        return window.native.Handle.ToInt32()
    except Exception:
        return None


def _dpi_scale(u, hwnd):
    try:
        dpi = u.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def _maximized(u, hwnd):
    wp = _WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
    if u.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return wp.showCmd == SW_SHOWMAXIMIZED
    return False


SW_RESTORE   = 9
SW_SHOW      = 5
SW_SHOWMINIMIZED = 2

WPF_RESTORETOMAXIMIZED = 0x0002


def bring_to_front(window):
    """Un-minimise (if needed) and foreground a window using raw Win32 calls.
    Safe to call from any thread — unlike pywebview's restore()/on_top, which
    marshal to the GUI thread and can deadlock when called from a Flask worker
    thread on a frameless, WndProc-subclassed window.

    Two things make this harder than it looks, and both were live bugs when a
    document was double-clicked while the app sat minimised:

      * `SetForegroundWindow` is denied to a process that does not own the
        foreground; Windows flashes the taskbar button instead.  The second
        instance therefore grants us its rights first
        (`single_instance._grant_foreground`), and we still keep the
        AttachThreadInput fallback for the times that grant is absent — when the
        page itself asks to be fronted, say.
      * A *denied* activation can leave `ShowWindow(SW_RESTORE)` doing nothing
        visible, so the restore is driven from GetWindowPlacement (which reports
        the real minimised state, and whether the window was maximised before it
        was minimised) rather than from IsIconic alone, and the maximised case is
        re-asserted explicitly.

    Best-effort throughout: any failure leaves the window as it was."""
    if sys.platform != 'win32':
        return
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return
    try:
        u = ctypes.windll.user32

        # ── 1. Make it visible again ─────────────────────────────────────────
        wp = _WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        minimized = bool(u.IsIconic(hwnd))
        restore_maximized = False
        if u.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            minimized = minimized or wp.showCmd == SW_SHOWMINIMIZED
            # The window remembers it was maximised before being minimised;
            # SW_RESTORE honours that, but re-assert it so a denied activation
            # can't leave us restored to the small "normal" rect instead.
            restore_maximized = bool(wp.flags & WPF_RESTORETOMAXIMIZED)
        if minimized:
            u.ShowWindow(hwnd, SW_SHOWMAXIMIZED if restore_maximized else SW_RESTORE)
        elif not u.IsWindowVisible(hwnd):
            u.ShowWindow(hwnd, SW_SHOW)

        # ── 2. Then pull it to the front ─────────────────────────────────────
        u.BringWindowToTop(hwnd)
        if u.SetForegroundWindow(hwnd):
            return
        # Denied.  Borrow the current foreground thread's input queue — while
        # attached we share its foreground rights — and try once more.
        fg = u.GetForegroundWindow()
        if not fg or fg == hwnd:
            return
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        u.GetWindowThreadProcessId.restype  = wintypes.DWORD
        fg_thread  = u.GetWindowThreadProcessId(fg, None)
        win_thread = u.GetWindowThreadProcessId(hwnd, None)
        if not fg_thread or fg_thread == win_thread:
            return
        attached = bool(u.AttachThreadInput(fg_thread, win_thread, True))
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(fg_thread, win_thread, False)
    except Exception:
        pass


# Class names of the HWNDs WebView2 nests inside its host control, innermost
# first.  Keyboard focus has to land on one of these for the PAGE to see key
# events; focus sitting on the WinForms host swallows them silently.
_WEBVIEW_CLASSES = (
    'Chrome_RenderWidgetHostHWND',
    'Chrome_WidgetWin_1',
    'Chrome_WidgetWin_0',
)


def _find_descendants_by_class(u, parent, wanted):
    """Every VISIBLE descendant HWND of `parent` whose class name is `wanted`.

    All of them, not the first: WebView2 keeps more than one render-widget HWND
    alive at a time (the print preview spins up its own, and a torn-down one can
    outlive the frame it belonged to for a moment).  Focusing the wrong one is
    indistinguishable from focusing none — keys go to a widget that is not the
    page — so the caller tries them in turn and checks which one sticks.
    Invisible ones are skipped outright; they never take focus."""
    found = []
    buf = ctypes.create_unicode_buffer(256)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, LPARAM)
    def _cb(child, _lparam):
        u.GetClassNameW(child, buf, 256)
        if buf.value == wanted and u.IsWindowVisible(child):
            found.append(child)
        return True

    u.EnumChildWindows(parent, _cb, 0)
    return found


def focus_web_content(window):
    """Put keyboard focus back into the WebView2 page content.

    A native modal (the Save/Open file dialog, the print preview) takes focus
    away from the web content.  When it closes, Windows returns focus to the
    top-level window — but NOT to the WebView2 child inside it, so key events go
    to the WinForms host and the page's own document-level shortcuts (Ctrl+P
    above all) stop firing.  Mouse clicks still work, which is why the print
    TOOLBAR BUTTON keeps working while Ctrl+P looks dead.

    SetFocus only acts within the calling thread's input queue, and this runs on
    the JS-bridge thread rather than the GUI thread, so attach to the window's
    thread input for the call.  Entirely best-effort — a failure just leaves the
    old behaviour."""
    if sys.platform != 'win32':
        return False
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return False
    try:
        u = ctypes.windll.user32
        # Explicit signatures: an HWND is pointer-sized, and ctypes' default
        # argument conversion would narrow it to a C int.
        u.SetFocus.argtypes  = [wintypes.HWND]
        u.SetFocus.restype   = wintypes.HWND
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        u.GetWindowThreadProcessId.restype  = wintypes.DWORD
        u.GetFocus.restype = wintypes.HWND
        # Candidates innermost-first, then the top-level window as a last resort.
        targets = []
        for cls in _WEBVIEW_CLASSES:
            targets.extend(_find_descendants_by_class(u, hwnd, cls))
        targets.append(hwnd)

        cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        win_thread = u.GetWindowThreadProcessId(hwnd, None)
        attached = False
        if win_thread and win_thread != cur_thread:
            attached = bool(u.AttachThreadInput(cur_thread, win_thread, True))
        try:
            # SetFocus can silently do nothing (a hidden or already-destroyed
            # widget, a window on another input queue), and a failure here is
            # invisible to the user right up until they press a key.  While
            # attached we share the window's input queue, so GetFocus() reports
            # ITS focus and we can check rather than hope.
            for target in targets:
                u.SetFocus(target)
                if u.GetFocus() == target:
                    return True
            return False
        finally:
            if attached:
                u.AttachThreadInput(cur_thread, win_thread, False)
    except Exception:
        return False


def is_foreground(window):
    """True when this app's window is the foreground window.

    The print dialog is modal and owned by our process, so "our window is
    foreground again" is the one signal that it has closed which does not depend
    on the web content having focus — which is exactly what is broken at that
    moment.  See entry.py's print focus guard."""
    if sys.platform != 'win32':
        return True
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return False
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = wintypes.HWND
        fg = u.GetForegroundWindow()
        if not fg:
            return False
        if fg == hwnd:
            return True
        # A child/owned window of ours counts as us.
        u.GetAncestor.restype = wintypes.HWND
        GA_ROOTOWNER = 3
        return u.GetAncestor(fg, GA_ROOTOWNER) == hwnd
    except Exception:
        return False


def get_window_rect(window):
    """(left, top, right, bottom) of a window in PHYSICAL px, or None.  Used by
    the tab-dock hit-test to find which window's tab bar the cursor is over."""
    if sys.platform != 'win32':
        return None
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return None
    try:
        r = _RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def dpi_scale(window):
    """DPI scale (1.0 == 96 DPI) of a window, for converting the page's CSS px to
    physical px.  Falls back to 1.0."""
    if sys.platform != 'win32':
        return 1.0
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return 1.0
    try:
        return _dpi_scale(ctypes.windll.user32, hwnd)
    except Exception:
        return 1.0


def cursor_pos():
    """Current cursor position in PHYSICAL px, or None.  Read on the backend so
    the dock hit-test never has to trust client-reported coordinates."""
    if sys.platform != 'win32':
        return None
    try:
        p = _POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return (p.x, p.y)
    except Exception:
        return None


def move_window(window, x, y):
    """Move a window's top-left to (x, y) in PHYSICAL px, without resizing,
    re-z-ordering, or activating it — for single-tab window-follow during a
    re-join drag."""
    if sys.platform != 'win32':
        return
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return
    try:
        ctypes.windll.user32.SetWindowPos(
            hwnd, None, int(x), int(y), 0, 0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    except Exception:
        pass


def is_maximized(window):
    """Public: real maximised state of a pywebview window, or None if unknown."""
    if sys.platform != 'win32':
        return None
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return None
    try:
        return _maximized(_user32(), hwnd)
    except Exception:
        return None


def restore_under_cursor(window, grab_css_x, grab_css_y):
    """Native-style drag-to-restore, driven from the JS titlebar drag.

    If *window* is maximised, un-maximise it to its stored normal size (or 80% of
    the work area if it never had one — e.g. the app launched maximised) and
    reposition it so the cursor stays on the titlebar at the same relative point.
    (grab_css_x, grab_css_y) is the cursor's offset within the window at drag
    start, in CSS px.

    Returns the NEW grab offset (x, y) in CSS px so the JS follow keeps that point
    under the cursor, or None when the window wasn't maximised (the caller then
    keeps dragging with the original offset).  The frameless window can't get this
    from DefWindowProc, and pywebview's own drag-region just slides the maximised
    window, so we do it here."""
    if sys.platform != 'win32':
        return None
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return None
    try:
        u = _user32()
        if not _maximized(u, hwnd):
            return None
        scale = _dpi_scale(u, hwnd) or 1.0

        # Maximised rect (fills the work area) + this monitor's work area.
        mr = _RECT(); u.GetWindowRect(hwnd, ctypes.byref(mr))
        max_left, max_w = mr.left, max(1, mr.right - mr.left)

        mi = _MONITORINFO(); mi.cbSize = ctypes.sizeof(_MONITORINFO)
        hmon = u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        work = mi.rcWork if (hmon and u.GetMonitorInfoW(hmon, ctypes.byref(mi))) else mr
        work_w, work_h = work.right - work.left, work.bottom - work.top

        # Restore size: the window's stored normal size, unless it's missing or
        # almost as wide as maximised (launched maximised) — then 80% of work.
        rw = rh = 0
        wp = _WINDOWPLACEMENT(); wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        if u.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            rw = wp.rcNormalPosition.right - wp.rcNormalPosition.left
            rh = wp.rcNormalPosition.bottom - wp.rcNormalPosition.top
        if rw < 400 or rh < 300 or rw >= max_w * 0.98:
            rw, rh = int(work_w * 0.8), int(work_h * 0.8)

        # Cursor physical X, and its fraction across the maximised titlebar.
        grab_phys_x = grab_css_x * scale
        cursor_x    = max_left + grab_phys_x
        fx          = grab_phys_x / max_w

        new_left = int(cursor_x - rw * fx)
        new_top  = mr.top
        new_left = max(work.left, min(new_left, work.right - rw))
        new_top  = max(work.top,  min(new_top,  work.bottom - rh))

        u.ShowWindow(hwnd, SW_RESTORE)                    # clear the maximised state
        u.SetWindowPos(hwnd, None, new_left, new_top, rw, rh,
                       SWP_NOZORDER | SWP_NOACTIVATE)

        # New grab offset within the restored window (CSS px) for the JS follow.
        new_grab_x = (rw * fx) / scale
        new_grab_y = min(max(float(grab_css_y), 0.0), float(TITLEBAR_H - 1))
        return (new_grab_x, new_grab_y)
    except Exception:
        return None


def apply(window):
    """Apply custom chrome to a shown pywebview window.  Returns True on success.
    Must be called from the window's `shown` event (so window.native exists)."""
    if sys.platform != 'win32' or os.environ.get('PATCHWORK_NATIVE_CHROME', '1') == '0':
        return False
    hwnd = _hwnd_of(window)
    if hwnd is None:
        return False

    try:
        u = _user32()

        # 1. Add the styles that enable resize / snap / min / max (caption off).
        style = u.GetWindowLongPtrW(hwnd, GWL_STYLE)
        style = (style | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU) & ~WS_CAPTION
        u.SetWindowLongPtrW(hwnd, GWL_STYLE, ctypes.c_void_p(style))

        # 2. Subclass the window proc (chain unhandled messages to WinForms').
        old_ptr = u.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)

        def _wndproc(h, msg, wparam, lparam):
            try:
                if msg == WM_GETMINMAXINFO:
                    # Maximised window == monitor work area (taskbar stays visible).
                    mi = _MONITORINFO()
                    mi.cbSize = ctypes.sizeof(_MONITORINFO)
                    hmon = u.MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST)
                    if hmon and u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                        work = mi.rcWork
                        mon  = mi.rcMonitor
                        mmi = ctypes.cast(lparam, ctypes.POINTER(_MINMAXINFO)).contents
                        mmi.ptMaxPosition.x  = work.left - mon.left
                        mmi.ptMaxPosition.y  = work.top - mon.top
                        mmi.ptMaxSize.x      = work.right - work.left
                        mmi.ptMaxSize.y      = work.bottom - work.top
                        mmi.ptMaxTrackSize.x = work.right - work.left
                        mmi.ptMaxTrackSize.y = work.bottom - work.top
                        return 0

                if msg == WM_NCCALCSIZE and wparam:
                    # Client area == whole window: no visible non-client frame.
                    return 0

                if msg == WM_NCHITTEST:
                    scale  = _dpi_scale(u, h)
                    border = max(1, int(8 * scale))
                    title  = int(TITLEBAR_H * scale)

                    x = ctypes.c_short(lparam & 0xFFFF).value
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    r = _RECT()
                    u.GetWindowRect(h, ctypes.byref(r))
                    w_ = r.right - r.left
                    h_ = r.bottom - r.top
                    rx = x - r.left
                    ry = y - r.top

                    if not _maximized(u, h):
                        on_l = rx < border
                        on_r = rx >= w_ - border
                        on_t = ry < border
                        on_b = ry >= h_ - border
                        if on_t and on_l: return HTTOPLEFT
                        if on_t and on_r: return HTTOPRIGHT
                        if on_b and on_l: return HTBOTTOMLEFT
                        if on_b and on_r: return HTBOTTOMRIGHT
                        if on_l: return HTLEFT
                        if on_r: return HTRIGHT
                        if on_t: return HTTOP
                        if on_b: return HTBOTTOM

                    if ry < title:
                        # The titlebar is draggable only inside the regions JS
                        # reported (the empty spacer); tabs and control buttons
                        # sit outside them and stay client (clickable).  Compare
                        # in CSS px (regions are reported in CSS px).
                        cx = rx / scale
                        cy = ry / scale
                        for (l, t, rr, b) in _drag_regions.get(h, ()):
                            if l <= cx < rr and t <= cy < b:
                                return HTCAPTION
                        return HTCLIENT
                    return HTCLIENT

                if msg == WM_DESTROY:
                    _subclasses.pop(h, None)
                    _drag_regions.pop(h, None)
            except Exception:
                pass
            return u.CallWindowProcW(old_ptr, h, msg, wparam, lparam)

        cb = WNDPROC(_wndproc)
        _subclasses[hwnd] = (cb, old_ptr)
        u.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p))

        # 3. Force a non-client recalculation so the new frame takes effect.
        u.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                       SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE |
                       SWP_NOZORDER | SWP_NOACTIVATE)
        return True
    except Exception:
        return False
