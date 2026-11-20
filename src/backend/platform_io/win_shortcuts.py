"""win_shortcuts.py — app shortcuts that work wherever keyboard focus is (Windows).

Why Ctrl+P was unreliable for so long
-------------------------------------
The page's own keydown listener (viewer-panel.js) can only hear a key when
keyboard focus is INSIDE the web content.  The web content is drawn by the
separate WebView2 browser process; our process owns the WinForms window around
it.  Whenever focus lands on that window instead of the page — after the OS
Save/Open dialog, after the print preview, after clicking the custom title bar
or tab strip, after Alt+Tab back in some cases — the key goes to a window that
does nothing with it.  A mouse click on the Print button does not care where
focus is, which is why the button always worked and the shortcut did not.

pywebview also turns WebView2's own browser shortcuts off in release builds
(AreBrowserAcceleratorKeysEnabled = False), so there was never a second path.
Every earlier fix pushed focus back into the page after the dialogs we know
about (focus_web_content, the print focus guard); each closed one hole and
left the rest.

The fix: catch it in the WINDOW
-------------------------------
A WinForms form checks menu SHORTCUT keys for every key that reaches it, and
the WebView2 control forwards the keys it treats as accelerators (Ctrl+letter)
up to that same check while the page has focus.  So a hidden MenuStrip with a
Ctrl+P item hears Ctrl+P in both situations.  Its click runs the page's print
command through ExecuteScriptAsync (no dependence on focus, safe on the UI
thread) and hands focus back to the page with WebView2's own Focus().

The page listener stays: on other platforms it is the only path, and on
Windows, if both fire, the page de-duplicates (pwAppShortcut / printDocument).
Every Ctrl+P is logged with the path that caught it ([SHORTCUT]).
"""

import logging
import sys

logger = logging.getLogger(__name__)

# shortcut name -> (WinForms Keys expression, JS run in the page)
_SHORTCUTS = {
    'print': ('P', "window.pwAppShortcut && window.pwAppShortcut('print', 'window')"),
}

_KEEP = []          # .NET objects / delegates that must stay referenced
_VK_CONTROL, _VK_SHIFT, _VK_MENU = 0x11, 0x10, 0x12


def _webview(form):
    browser = getattr(form, 'browser', None)
    return getattr(browser, 'webview', None)


def _controller(wv):
    """The WinForms control's CoreWebView2Controller (a private field — the
    control exposes no public accessor, but MoveFocus and AcceleratorKeyPressed
    live only there).  None until WebView2 has initialised."""
    if wv is None:
        return None
    try:
        from System.Reflection import BindingFlags
        f = wv.GetType().GetField('_coreWebView2Controller',
                                  BindingFlags.NonPublic | BindingFlags.Instance)
        return f.GetValue(wv) if f is not None else None
    except Exception:
        return None


def _on_gui(form, fn):
    from System import Action
    if form.InvokeRequired:
        form.Invoke(Action(fn))
    else:
        fn()


def _key_down(vk):
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetKeyState(vk) & 0x8000)
    except Exception:
        return False


def focus_page(window):
    """Give keyboard focus to the page THE WEBVIEW2 WAY: the controller's
    MoveFocus(Programmatic).

    win_chrome.focus_web_content used raw SetFocus on WebView2's inner
    Chrome_RenderWidgetHostHWND.  Windows then REPORTS focus on the page, but
    WebView2's own focus state is not updated, and keys typed next are dropped
    before either the page or the accelerator path sees them — measured: after
    cancelling the Open dialog, GetFocus() said Chrome_RenderWidgetHostHWND and
    Ctrl+P reached nothing at all.  True when the call was made."""
    if sys.platform != 'win32':
        return False
    try:
        form = window.native
        wv = _webview(form)
        done = []

        def _go():
            ctl = _controller(wv)
            if ctl is None:
                return
            from Microsoft.Web.WebView2.Core import CoreWebView2MoveFocusReason
            ctl.MoveFocus(CoreWebView2MoveFocusReason.Programmatic)
            done.append(True)
        _on_gui(form, _go)
        return bool(done)
    except Exception:
        logger.debug('[SHORTCUT] focus_page failed', exc_info=True)
        return False


def install(window):
    """Catch the app's shortcuts in the WINDOW, wherever focus is.  Call once
    the window is shown.  Windows-only; never raises.

    Two native paths, because focus can be in two places:
      * the page (WebView2) has focus → the controller's AcceleratorKeyPressed,
        raised in OUR process BEFORE the page sees the key; marked Handled so
        the page's own listener does not run it a second time;
      * our window has focus (focus left on the WinForms host by a dialog) → a
        hidden MenuStrip whose items carry the shortcut keys; WinForms checks
        those for every key that reaches the window.
    """
    if sys.platform != 'win32':
        return False
    try:
        form = window.native
    except Exception:
        return False
    if form is None:
        return False
    try:
        import clr  # noqa: F401  (pythonnet is loaded by pywebview)
        from System.Windows.Forms import MenuStrip, ToolStripMenuItem, Keys
    except Exception as exc:
        logger.info('[SHORTCUT] not installed: %s', exc)
        return False

    def _fire(name, via):
        try:
            wv = _webview(form)
            core = getattr(wv, 'CoreWebView2', None) if wv is not None else None
            if core is None:
                logger.info('[SHORTCUT] %s: page not ready', name)
                return
            logger.info('[SHORTCUT] Ctrl+%s caught by the %s', _SHORTCUTS[name][0], via)
            core.ExecuteScriptAsync(_SHORTCUTS[name][1])
            # Leave focus properly in the page so the NEXT key reaches it too.
            ctl = _controller(wv)
            if ctl is not None and via == 'window':
                from Microsoft.Web.WebView2.Core import CoreWebView2MoveFocusReason
                ctl.MoveFocus(CoreWebView2MoveFocusReason.Programmatic)
        except Exception:
            logger.exception('[SHORTCUT] %s failed', name)

    by_vk = {ord(k): n for n, (k, _js) in _SHORTCUTS.items()}

    def _on_accel(_sender, args):
        try:
            kind = str(args.KeyEventKind)
            if kind not in ('KeyDown', 'SystemKeyDown'):
                return
            name = by_vk.get(int(args.VirtualKey))
            if not name or not _key_down(_VK_CONTROL) or _key_down(_VK_SHIFT) \
                    or _key_down(_VK_MENU):
                return
            if args.KeyEventKind and getattr(args, 'PhysicalKeyStatus', None) is not None \
                    and args.PhysicalKeyStatus.WasKeyDown:
                args.Handled = True          # auto-repeat: swallow, don't re-run
                return
            args.Handled = True
            _fire(name, 'browser')
        except Exception:
            logger.exception('[SHORTCUT] accelerator handler failed')

    def _hook_accel():
        ctl = _controller(_webview(form))
        if ctl is None:
            return False
        ctl.AcceleratorKeyPressed += _on_accel
        _KEEP.append(_on_accel)
        logger.info('[SHORTCUT] browser accelerator hook installed')
        return True

    def _build():
        try:
            strip = MenuStrip()
            strip.Visible = False          # shortcuts still work; nothing drawn
            strip.TabStop = False
            for name, (key, _js) in _SHORTCUTS.items():
                item = ToolStripMenuItem(name)
                item.ShortcutKeys = Keys.Control | getattr(Keys, key)
                item.ShowShortcutKeys = False
                item.Click += (lambda _s, _e, n=name: _fire(n, 'window'))
                strip.Items.Add(item)
            form.Controls.Add(strip)
            if form.MainMenuStrip is None:
                form.MainMenuStrip = strip
            _KEEP.append(strip)
            if not _hook_accel():
                # WebView2 still initialising: hook it when it is ready.
                wv = _webview(form)
                if wv is not None:
                    def _ready(_s, _e):
                        try:
                            _hook_accel()
                        except Exception:
                            logger.exception('[SHORTCUT] late hook failed')
                    wv.CoreWebView2InitializationCompleted += _ready
                    _KEEP.append(_ready)
            logger.info('[SHORTCUT] window shortcuts installed (%s)',
                        ', '.join('Ctrl+' + k for k, _ in _SHORTCUTS.values()))
        except Exception:
            logger.exception('[SHORTCUT] install failed')

    try:
        _on_gui(form, _build)
        return True
    except Exception:
        logger.exception('[SHORTCUT] install failed')
        return False
