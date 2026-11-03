"""
splash.py — instant startup splash screen (pure Win32 / ctypes).

Patchwork PDF's window can't paint anything until the heavy Python imports
(Flask, pypdf, editor, viewer) AND the WebView2/edgechromium runtime are up —
on a slow machine that is several seconds of blank screen with no sign the app
is launching.  This module covers that gap with a borderless splash that appears
almost immediately, because it is shown BEFORE those heavy imports and drawn
with raw GDI rather than the WebView2 stack it is trying to hide.

Why ctypes and not tkinter: the target Python installs here ship without Tcl/Tk,
so `import tkinter` fails and a Tk splash would silently never appear.  ctypes is
part of core CPython (always present) and bundles into the cx_Freeze build with
no extra DLLs or library trees — same reason single_instance.py stays stdlib-only.

Usage (see entry.py):
    from bootstrap import splash
    splash.show()                       # as early as possible
    splash.set_status('Loading components…')
    ...
    splash.close()                      # on the main window's `loaded` event

Win32 windows are thread-affine: a window's messages are delivered only to the
thread that created it.  So the whole window lifecycle (create, message loop,
GDI objects, teardown) lives on ONE background thread.  Other threads never call
USER32/GDI directly — they post messages through a Queue that the splash thread
drains on a WM_TIMER tick.  pywebview owns the real main thread (its
webview.start() blocks it), which is exactly why the splash gets its own.

Non-Windows / any failure: every entry point is wrapped so the splash simply
does not appear and never blocks or breaks startup.
"""

import os
import sys
import queue
import logging
import threading

logger = logging.getLogger(__name__)

# Auto-dismiss safety net: if the `loaded` event never arrives (load error,
# crash, etc.) the splash must not hang on screen forever.
_MAX_LIFETIME_MS = 30_000

# Palette as COLORREF (0x00BBGGRR) — mirrors the app's CSS tokens.
_BG     = 0x00110F0F     # --bg #0f0f11
_FG     = 0x00F4F2F2     # primary text #f2f2f4
_MUTED  = 0x00938A8A     # version / status #8a8a93
_ACCENT = 0x003B82F6     # brand accent — orange #f6823b (matches the app icon)
_TRACK  = 0x002E2A2A     # progress groove #2a2a2e

# Design size, in layout units at 96 DPI.  Every geometry constant below is in
# these units and gets multiplied by the display scale at window-creation time
# (see _dpi_scale), so the splash is the same PHYSICAL size on a 100% and a 200%
# display instead of shrinking to half on the latter.
_W, _H   = 440, 300
_RADIUS  = 28            # diameter of the window's rounded corners

_queue   = queue.Queue()        # (kind, payload) messages → splash thread
_thread  = None
_started = False

# Live state shared with the GDI paint handler (string assignment is atomic
# under the GIL, which is all the cross-thread safety this needs).
_status_text = 'Starting…'

# Kept alive for the window's whole lifetime so the GC can't collect the WndProc
# trampoline or the GDI objects out from under USER32.
_keepalive = []


def _app_dir():
    """Directory holding the bundled assets (mirrors entry.get_app_dir)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # dev: this file is <root>/src/backend/bootstrap/splash.py — climb to <root>.
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))


def _read_version():
    """Best-effort 'v<version>' from version.json; '' if unavailable."""
    try:
        import json
        with open(os.path.join(_app_dir(), 'version.json'), 'r', encoding='utf-8') as f:
            v = json.load(f).get('version', '')
        return f'v{v}' if v else ''
    except Exception:
        return ''


def _make_process_dpi_aware(ctypes, user32):
    """Declare this process SYSTEM DPI-aware, before any window is created.

    Why this must happen here: pywebview's winforms backend calls
    SetProcessDPIAware() itself, but only from inside webview.start() — long
    after the splash window exists.  Until then the process is DPI-UNAWARE, so
    Windows virtualises our window: on a 150% display the 440x300 splash is
    stretched to 660x450 physical pixels.  The moment pywebview flips awareness,
    the stretching stops and the still-visible splash visibly SHRINKS (to 2/3 at
    150%, to half at 200%) a beat before it closes.  Machines running at 100%
    never see it, which is why the glitch looked display-dependent.

    Claiming awareness up-front removes the mid-flight transition entirely: the
    splash is created in the same DPI regime it dies in.  pywebview's later
    SetProcessDPIAware() then simply no-ops (it ignores the return value).

    SYSTEM_AWARE — not PER_MONITOR — is deliberate: it is exactly the state
    pywebview would have reached, so the main window's behaviour is unchanged.
    Per-monitor would additionally require WinForms per-monitor support that
    this app does not configure.
    """
    DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = ctypes.c_void_p(-2)
    PROCESS_SYSTEM_DPI_AWARE = 1
    try:                                  # Win10 1607+
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_SYSTEM_AWARE):
            return
    except Exception:
        pass
    try:                                  # Win8.1+
        ctypes.WinDLL('shcore').SetProcessDpiAwareness(PROCESS_SYSTEM_DPI_AWARE)
        return
    except Exception:
        pass
    try:                                  # Vista+
        user32.SetProcessDPIAware()
    except Exception as e:
        logger.info(f'splash: could not set DPI awareness ({e}); '
                    f'splash may resize when the main window starts')


def _dpi_scale(ctypes, user32, gdi32):
    """Display scale factor (1.0 at 96 DPI, 1.5 at 150%, 2.0 at 200%).

    Read AFTER _make_process_dpi_aware, otherwise it always reports 96 — an
    unaware process is shown a virtualised 96-DPI view of the world.
    """
    LOGPIXELSX = 88
    try:
        hdc = user32.GetDC(None)
        if hdc:
            try:
                dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
            finally:
                user32.ReleaseDC(None, hdc)
            if dpi:
                return dpi / 96.0
    except Exception as e:
        logger.info(f'splash: DPI query failed ({e}); assuming 100%')
    return 1.0


def _run(initial_status):
    """Splash thread body: register a class, create a layered popup, then pump
    its message loop until a 'close' arrives or the lifetime cap is hit.  Every
    USER32/GDI call stays on this thread."""
    global _status_text
    _status_text = initial_status

    try:
        import ctypes
        from ctypes import wintypes
    except Exception as e:
        logger.info(f'splash: ctypes unavailable ({e}); skipping splash')
        return
    if sys.platform != 'win32':
        logger.info('splash: non-Windows platform; skipping splash')
        return

    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        gdi32  = ctypes.WinDLL('gdi32',  use_last_error=True)
    except Exception as e:
        logger.info(f'splash: could not load USER32/GDI32 ({e}); skipping splash')
        return

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    # ── win32 constants ──────────────────────────────────────────────────────
    WM_DESTROY, WM_PAINT, WM_TIMER, WM_ERASEBKGND = 0x0002, 0x000F, 0x0113, 0x0014
    WS_POPUP, WS_VISIBLE = 0x80000000, 0x10000000
    WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x00080000, 0x00000008, 0x00000080
    SW_SHOWNOACTIVATE = 4
    LWA_ALPHA = 0x00000002
    DT_CENTER, DT_SINGLELINE, DT_VCENTER, DT_NOPREFIX = 0x1, 0x20, 0x4, 0x800
    TRANSPARENT = 1
    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x00000010
    DI_NORMAL = 0x0003
    SM_CXSCREEN, SM_CYSCREEN = 0, 1
    DEFAULT_CHARSET, CLEARTYPE_QUALITY = 1, 5
    FW_NORMAL, FW_SEMIBOLD = 400, 600
    IDC_ARROW = 32512

    class RECT(ctypes.Structure):
        _fields_ = [('left', wintypes.LONG), ('top', wintypes.LONG),
                    ('right', wintypes.LONG), ('bottom', wintypes.LONG)]

    class POINT(ctypes.Structure):
        _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', RECT),
                    ('rcWork', RECT), ('dwFlags', wintypes.DWORD)]

    class PAINTSTRUCT(ctypes.Structure):
        _fields_ = [('hdc', wintypes.HDC), ('fErase', wintypes.BOOL),
                    ('rcPaint', RECT), ('fRestore', wintypes.BOOL),
                    ('fIncUpdate', wintypes.BOOL), ('rgbReserved', wintypes.BYTE * 32)]

    class WNDCLASS(ctypes.Structure):
        _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', WNDPROC),
                    ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                    ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                    ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                    ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]

    # ── prototypes ───────────────────────────────────────────────────────────
    # ctypes defaults every untyped argument to 32-bit C int, which truncates
    # 64-bit handles/pointers (large HBRUSH/HDC/HWND values overflow).  So every
    # call that takes or returns a handle, pointer, or wide style flag is typed.
    (HWND, HDC, HBRUSH, HICON, HFONT, HINSTANCE, HMODULE, HMENU, HGDIOBJ,
     UINT, DWORD, BOOL, LPCWSTR, LPVOID, COLORREF, BYTE, VOIDP, INT, UINT_PTR) = (
        wintypes.HWND, wintypes.HDC, wintypes.HBRUSH, wintypes.HICON,
        wintypes.HFONT, wintypes.HINSTANCE, wintypes.HMODULE, wintypes.HMENU,
        wintypes.HGDIOBJ, wintypes.UINT, wintypes.DWORD, wintypes.BOOL,
        wintypes.LPCWSTR, wintypes.LPVOID, wintypes.COLORREF, wintypes.BYTE,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t)

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetModuleHandleW.restype  = HMODULE
    kernel32.GetModuleHandleW.argtypes = [LPCWSTR]

    def _proto(fn, restype, argtypes):
        fn.restype, fn.argtypes = restype, argtypes

    _proto(user32.DefWindowProcW, LRESULT, [HWND, UINT, wintypes.WPARAM, wintypes.LPARAM])
    _proto(user32.RegisterClassW, wintypes.ATOM, [VOIDP])
    _proto(user32.CreateWindowExW, HWND,
           [DWORD, LPCWSTR, LPCWSTR, DWORD, INT, INT, INT, INT, HWND, HMENU, HINSTANCE, LPVOID])
    _proto(user32.DestroyWindow, BOOL, [HWND])
    _proto(user32.ShowWindow, BOOL, [HWND, INT])
    _proto(user32.UpdateWindow, BOOL, [HWND])
    _proto(user32.InvalidateRect, BOOL, [HWND, VOIDP, BOOL])
    _proto(user32.SetLayeredWindowAttributes, BOOL, [HWND, COLORREF, BYTE, DWORD])
    _proto(user32.GetSystemMetrics, INT, [INT])
    _proto(user32.GetCursorPos, BOOL, [VOIDP])
    _proto(user32.MonitorFromPoint, VOIDP, [POINT, DWORD])
    _proto(user32.GetMonitorInfoW, BOOL, [VOIDP, VOIDP])
    _proto(user32.LoadCursorW, HGDIOBJ, [HINSTANCE, VOIDP])
    _proto(user32.LoadImageW, wintypes.HANDLE, [HINSTANCE, LPCWSTR, UINT, INT, INT, UINT])
    _proto(user32.SetTimer, UINT_PTR, [HWND, UINT_PTR, UINT, VOIDP])
    _proto(user32.KillTimer, BOOL, [HWND, UINT_PTR])
    _proto(user32.PostQuitMessage, None, [INT])
    _proto(user32.BeginPaint, HDC, [HWND, VOIDP])
    _proto(user32.EndPaint, BOOL, [HWND, VOIDP])
    _proto(user32.FillRect, INT, [HDC, VOIDP, HBRUSH])
    _proto(user32.FrameRect, INT, [HDC, VOIDP, HBRUSH])
    _proto(user32.DrawTextW, INT, [HDC, LPCWSTR, INT, VOIDP, UINT])
    _proto(user32.DrawIconEx, BOOL, [HDC, INT, INT, HICON, INT, INT, UINT, HBRUSH, UINT])
    _proto(user32.GetMessageW, INT, [VOIDP, HWND, UINT, UINT])
    _proto(user32.TranslateMessage, BOOL, [VOIDP])
    _proto(user32.DispatchMessageW, LRESULT, [VOIDP])
    _proto(gdi32.CreateSolidBrush, HBRUSH, [COLORREF])
    _proto(gdi32.CreateFontW, HFONT,
           [INT, INT, INT, INT, INT, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, LPCWSTR])
    _proto(gdi32.SelectObject, HGDIOBJ, [HDC, HGDIOBJ])
    _proto(gdi32.SetTextColor, COLORREF, [HDC, COLORREF])
    _proto(gdi32.SetBkMode, INT, [HDC, INT])
    _proto(gdi32.CreateRoundRectRgn, wintypes.HRGN, [INT, INT, INT, INT, INT, INT])
    _proto(user32.SetWindowRgn, INT, [HWND, wintypes.HRGN, BOOL])
    _proto(user32.GetDC, HDC, [HWND])
    _proto(user32.ReleaseDC, INT, [HWND, HDC])
    _proto(gdi32.GetDeviceCaps, INT, [HDC, INT])
    try:
        _proto(user32.SetProcessDpiAwarenessContext, BOOL, [VOIDP])
    except AttributeError:
        pass            # pre-Win10 1607 — _make_process_dpi_aware falls back

    # Claim DPI awareness BEFORE the window is created, then scale every
    # geometry constant by the display scale.  Order matters both ways: awareness
    # first or the window resizes under us later; scale second or the splash is
    # created at half physical size on a HiDPI display.
    _make_process_dpi_aware(ctypes, user32)
    scale = _dpi_scale(ctypes, user32, gdi32)

    def S(v):
        """Layout units (96 DPI) → physical pixels."""
        return int(round(v * scale))

    W, H = S(_W), S(_H)

    hInstance = kernel32.GetModuleHandleW(None)
    bg_brush  = gdi32.CreateSolidBrush(_BG)

    def _font(height, weight):
        return gdi32.CreateFontW(S(height), 0, 0, 0, weight, 0, 0, 0,
                                 DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, 'Segoe UI')

    f_title  = _font(-30, FW_SEMIBOLD)
    f_ver    = _font(-15, FW_NORMAL)
    f_status = _font(-15, FW_NORMAL)
    accent_brush = gdi32.CreateSolidBrush(_ACCENT)
    track_brush  = gdi32.CreateSolidBrush(_TRACK)

    # Logo from the bundled .ico (DrawIconEx scales it); PNG would need GDI+.
    ICON = S(96)
    hicon = None
    try:
        ico = os.path.join(_app_dir(), 'assets', 'icon.ico')
        if os.path.isfile(ico):
            hicon = user32.LoadImageW(None, ico, IMAGE_ICON, ICON, ICON, LR_LOADFROMFILE)
    except Exception as e:
        logger.info(f'splash: logo not loaded ({e})')

    version = _read_version()
    elapsed = [0]
    TICK = 33                       # ~30 fps — smooth enough for the progress bar

    # Indeterminate progress-bar geometry: a static groove near the bottom with
    # an accent chunk that sweeps across it once every PROG_PERIOD ms.
    PROG_X, PROG_H = S(70), S(4)
    PROG_Y = H - S(40)
    PROG_W = W - 2 * PROG_X
    CHUNK_W = S(96)
    PROG_PERIOD = 1400          # milliseconds — a duration, never scaled

    def _draw(hdc):
        gdi32.SetBkMode(hdc, TRANSPARENT)
        if hicon:
            user32.DrawIconEx(hdc, (W - ICON) // 2, S(34), hicon, ICON, ICON, 0, None, DI_NORMAL)

        def text(s, color, font, top, bottom):
            r = RECT(0, S(top), W, S(bottom))
            gdi32.SetTextColor(hdc, color)
            old = gdi32.SelectObject(hdc, font)
            user32.DrawTextW(hdc, ctypes.c_wchar_p(s), -1, ctypes.byref(r),
                             DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX)
            gdi32.SelectObject(hdc, old)

        text('Patchwork PDF', _FG, f_title, 140, 176)

        # Short brand-accent rule under the title.
        rule_w = S(40)
        rule = RECT((W - rule_w) // 2, S(184), (W + rule_w) // 2, S(187))
        user32.FillRect(hdc, ctypes.byref(rule), accent_brush)

        if version:
            text(version, _MUTED, f_ver, 194, 216)
        text(_status_text, _MUTED, f_status, _H - 74, _H - 50)

        # Indeterminate progress bar: a groove with an accent chunk that sweeps
        # left→right on a loop.  elapsed[0] is advanced by the WM_TIMER tick.
        track = RECT(PROG_X, PROG_Y, W - PROG_X, PROG_Y + PROG_H)
        user32.FillRect(hdc, ctypes.byref(track), track_brush)

        t = (elapsed[0] % PROG_PERIOD) / PROG_PERIOD
        start = (PROG_X - CHUNK_W) + t * (PROG_W + CHUNK_W)
        c_left  = max(PROG_X, int(start))
        c_right = min(W - PROG_X, int(start + CHUNK_W))
        if c_right > c_left:
            chunk = RECT(c_left, PROG_Y, c_right, PROG_Y + PROG_H)
            user32.FillRect(hdc, ctypes.byref(chunk), accent_brush)

    def _wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_ERASEBKGND:
            return 1                                    # bg brush already fills; avoid flicker
        if msg == WM_PAINT:
            ps  = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            full = RECT(0, 0, W, H)
            user32.FillRect(hdc, ctypes.byref(full), bg_brush)
            _draw(hdc)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_TIMER:
            try:
                while True:
                    kind, payload = _queue.get_nowait()
                    if kind == 'status':
                        globals()['_status_text'] = payload
                        user32.InvalidateRect(hwnd, None, True)
                    elif kind == 'close':
                        user32.DestroyWindow(hwnd)
                        return 0
            except queue.Empty:
                pass
            elapsed[0] += TICK
            # Repaint just the progress strip each tick so the indeterminate
            # bar animates without redrawing (or flickering) the rest of the card.
            strip = RECT(PROG_X, PROG_Y, W - PROG_X, PROG_Y + PROG_H)
            user32.InvalidateRect(hwnd, ctypes.byref(strip), False)
            if elapsed[0] >= _MAX_LIFETIME_MS:
                logger.info('splash: lifetime cap reached — auto-closing')
                user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.KillTimer(hwnd, 1)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(_wndproc)
    _keepalive.extend([proc, bg_brush, accent_brush, track_brush,
                       f_title, f_ver, f_status, hicon])

    cls = WNDCLASS()
    cls.style         = 0
    cls.lpfnWndProc   = proc
    cls.hInstance     = hInstance
    cls.hCursor       = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
    cls.hbrBackground = bg_brush
    cls.lpszClassName = 'PatchworkPdfSplash'
    if not user32.RegisterClassW(ctypes.byref(cls)):
        logger.info(f'splash: RegisterClassW failed ({ctypes.get_last_error()})')
        return
    _keepalive.append(cls)

    # Centre the splash on the monitor the app will open on.  pywebview creates
    # the main window without an explicit position, so Windows places it on the
    # monitor the app was launched from — i.e. the one under the cursor.  Match
    # that here (MonitorFromPoint on the cursor) so the splash and the app appear
    # together on multi-monitor setups, falling back to the primary monitor.
    MONITOR_DEFAULTTONEAREST = 2
    mon_left, mon_top = 0, 0
    mon_w = user32.GetSystemMetrics(SM_CXSCREEN)
    mon_h = user32.GetSystemMetrics(SM_CYSCREEN)
    try:
        pt = POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcMonitor
                mon_left, mon_top = r.left, r.top
                mon_w, mon_h = r.right - r.left, r.bottom - r.top
    except Exception as e:
        logger.info(f'splash: monitor detection failed ({e}); using primary')

    x = mon_left + (mon_w - W) // 2
    y = mon_top  + (mon_h - H) // 3

    hwnd = user32.CreateWindowExW(
        WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
        'PatchworkPdfSplash', 'Patchwork PDF',
        WS_POPUP | WS_VISIBLE, x, y, W, H,
        None, None, hInstance, None)
    if not hwnd:
        logger.info(f'splash: CreateWindowExW failed ({ctypes.get_last_error()})')
        return

    # Rounded corners: clip the window to a round-rect region.  Windows takes
    # ownership of the region after SetWindowRgn, so we must not free it ourselves.
    hrgn = gdi32.CreateRoundRectRgn(0, 0, W + 1, H + 1, S(_RADIUS), S(_RADIUS))
    if hrgn:
        user32.SetWindowRgn(hwnd, hrgn, True)

    # A layered window stays invisible until composited; opaque alpha shows it.
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)      # don't steal focus from launch
    user32.UpdateWindow(hwnd)
    user32.SetTimer(hwnd, 1, TICK, None)

    # Standard GetMessage pump; ends when WM_DESTROY → PostQuitMessage(0).
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def show(initial_status='Starting…'):
    """Show the splash on its own thread.  Idempotent and never fatal: any
    failure is logged and swallowed so it can't block app startup."""
    global _thread, _started
    if _started:
        return
    _started = True
    try:
        _thread = threading.Thread(target=_run, args=(initial_status,),
                                   daemon=True, name='splash')
        _thread.start()
    except Exception as e:
        logger.info(f'splash: could not start ({e})')


def set_status(text):
    """Update the splash status line (thread-safe; no-op if not showing)."""
    if _started:
        _queue.put(('status', text))


def close():
    """Dismiss the splash (thread-safe; no-op if not showing)."""
    if _started:
        _queue.put(('close', None))


if __name__ == '__main__':
    # Local test harness: run `python splash.py` to see the splash in isolation,
    # without booting the whole app.  Shows two status messages 5 seconds apart,
    # then closes.  Windows-only (the splash is a no-op elsewhere).
    import time

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s: %(message)s')

    show('Starting…')
    time.sleep(2)                       # let the window paint before first update

    set_status('Loading components…')
    time.sleep(5)

    set_status('Almost ready…')
    time.sleep(5)

    close()
    if _thread is not None:
        _thread.join(timeout=5)         # wait for the splash thread to tear down
