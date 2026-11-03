/* ═══════════════════════════════════════════════════════
   SHARED JS — panel switching, toast, loading, confirm
════════════════════════════════════════════════════════ */

// ── Global file-drop guard (stop the WebView opening dropped files) ──────────
// If a file is dropped anywhere OUTSIDE an explicit drop zone, the WebView's
// default action is to navigate to file://<path>, replacing the whole app with
// the browser's built-in file viewer — very visible on WebKitGTK/Linux, where a
// dropped PDF hijacks the window. Cancel the default for any drag that carries
// files, at the document level, so stray drops are harmlessly ignored. The real
// drop zones (#canvas-area, #layout-main, #compact-dropbar) still open the file
// via their own drop handlers, which run regardless of this guard (preventDefault
// cancels only the navigation, not the handlers). dragover MUST be cancelled too,
// or the browser ignores the drop's preventDefault and navigates anyway.
(function () {
  function _isFileDrag(e) {
    return !!(e.dataTransfer && e.dataTransfer.types &&
              e.dataTransfer.types.includes('Files'));
  }
  window.addEventListener('dragover', function (e) { if (_isFileDrag(e)) e.preventDefault(); });
  window.addEventListener('drop',     function (e) { if (_isFileDrag(e)) e.preventDefault(); });
})();

// ── Native-drop fallback ─────────────────────────────────────────────────────
// When the native app arms its own drop handler (window._nativeDropActive), it
// delivers the REAL OS paths to onLayoutFilesDropped/onViewerFilesDropped, which
// open by path — that is what gives the save dialog its source folder and puts
// the file in the recent-files list.  That handler is Python-side and therefore
// asynchronous, so a drop zone cannot simply hand over and forget: if the paths
// never arrive (an unexpected pywebview backend, or a drag with no file URLs on
// the pasteboard), the drop would silently do nothing.
//
// So a drop zone *defers* its byte-upload fallback instead of skipping it.  The
// native path delivery cancels the timer; if nothing cancels it, the upload runs
// and drag-and-drop still works — just without source-folder awareness.
//
// Cancelling the timer is NOT enough on its own.  The native push travels over
// pywebview's DOM-event bridge, which serialises the whole drop event — target
// element subtree included — so on a big Layout grid (hundreds of base64
// thumbnails in #layout-main) the round trip takes SECONDS.  By then the
// fallback has already fired and uploaded the bytes, and the late push added the
// same file a second time.  So the drop is a CLAIM, not a timer: whichever path
// gets there first owns that drop and the other becomes a no-op.
const NATIVE_DROP_FALLBACK_MS = 700;
let _nativeDropTimer = null;
// The drop currently in flight: {claimed: bool}.  Replaced on every new drop, so
// a fresh drop is always claimable even if the previous one is still resolving.
let _dropInFlight = null;

// Open a claim for a drop the page itself saw.  Called by dispatchFileDrop.
function _beginNativeDrop() {
  clearTimeout(_nativeDropTimer);
  _nativeDropTimer = null;
  _dropInFlight = { claimed: false };
  return _dropInFlight;
}

// Take ownership of the in-flight drop.  Returns false when some other path has
// already handled it — the caller must then do nothing at all.
function claimNativeDrop() {
  clearTimeout(_nativeDropTimer);
  _nativeDropTimer = null;
  // No page-side drop on record (macOS pull path, GTK forward, second-instance
  // open) — nothing to race with, so the caller always wins.
  if (!_dropInFlight) return true;
  if (_dropInFlight.claimed) return false;
  _dropInFlight.claimed = true;
  return true;
}
window.claimNativeDrop = claimNativeDrop;

function armNativeDropFallback(fn) {
  const drop = _dropInFlight;
  clearTimeout(_nativeDropTimer);
  _nativeDropTimer = setTimeout(function () {
    _nativeDropTimer = null;
    // Claim it for the upload before running: a native push that lands later
    // now sees the drop as spoken for and drops its paths on the floor.
    if (drop) {
      if (drop.claimed) return;
      drop.claimed = true;
    }
    fn();
  }, NATIVE_DROP_FALLBACK_MS);
}

// Route a set of dropped files to the best available opener.  Three cases:
//
//   _nativeDropPull  (macOS)   — the native side captured the real OS paths off
//                                the pasteboard; ask for them and open by path.
//   _nativeDropActive(Windows) — the native side will PUSH the real paths to
//                                onLayoutFilesDropped/onViewerFilesDropped, so
//                                defer the byte upload in case it never does.
//   neither (browser)          — upload the bytes.
//
// Opening by real path is what lets the file reach the recent-files list and
// gives the save dialog its source folder; uploading writes a temp copy, which
// is deliberately excluded from the MRU.
async function dispatchFileDrop(files, openByPath, uploadBytes) {
  _beginNativeDrop();          // this drop is now claimable exactly once
  if (window._nativeDropPull) {
    try {
      const paths = await window.pywebview.api.take_dropped_paths();
      if (paths && paths.length) {
        if (claimNativeDrop()) openByPath(paths);
        return;
      }
    } catch (e) { /* fall through — an upload is better than a lost drop */ }
    if (claimNativeDrop()) uploadBytes();
    return;
  }
  if (window._nativeDropActive) { armNativeDropFallback(uploadBytes); return; }
  if (claimNativeDrop()) uploadBytes();
}

// ── Custom titlebar (native pywebview window only) ──────────────────────────
// In a plain browser window.pywebview is undefined, so the titlebar stays hidden
// and the layout is unchanged.  Dragging / drag-to-snap / double-click-maximise
// are handled natively (win_chrome.py); the buttons call the Python bridge.
// Report the titlebar's draggable region(s) — the empty spacer — to the native
// hit-test (win_chrome), so tabs and control buttons stay clickable. Called on
// titlebar init, tab changes (_renderTabBar) and window/titlebar resize.
let _dragRegionRaf = 0;
function _reportDragRegions() {
  if (!isNativeApp() || !window.pywebview.api.set_titlebar_drag_regions) return;
  if (!document.body.classList.contains('has-native-window')) return;
  // Native-framed windows (macOS/Linux) are dragged by the OS title bar, so
  // there are no custom drag regions to report — and win_chrome is a no-op there.
  if (document.body.classList.contains('native-frame')) return;
  if (_dragRegionRaf) cancelAnimationFrame(_dragRegionRaf);
  _dragRegionRaf = requestAnimationFrame(function () {
    _dragRegionRaf = 0;
    const rects = [];
    document.querySelectorAll('#app-titlebar .tb-drag').forEach(function (el) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) rects.push([r.left, r.top, r.right, r.bottom]);
    });
    window.pywebview.api.set_titlebar_drag_regions(rects);
  });
}

(function () {
  function initTitlebar() {
    if (!window.pywebview || !window.pywebview.api) return;
    document.body.classList.add('has-native-window');
    var api = window.pywebview.api;

    // Discover the window frame style so the titlebar lays itself out correctly:
    //   custom-chrome (Windows) — HTML draws the titlebar + min/max/close buttons
    //   native-frame  (mac/Linux) — the OS draws the frame; hide the HTML buttons
    // Default to custom-chrome if the probe is unavailable/fails (matches the
    // pre-cross-platform behaviour on older backends).
    if (api.platform) {
      Promise.resolve(api.platform()).then(function (info) {
        document.body.classList.add(
          (info && info.custom_chrome === false) ? 'native-frame' : 'custom-chrome');
        _reportDragRegions();
      }).catch(function () {
        document.body.classList.add('custom-chrome');
      });
    } else {
      document.body.classList.add('custom-chrome');
    }

    var min   = document.getElementById('tb-min');
    var max   = document.getElementById('tb-max');
    var close = document.getElementById('tb-close');
    var add   = document.getElementById('tb-newtab');
    if (min)   min.onclick   = function () { api.minimize(); };
    if (max)   max.onclick   = function () { api.toggle_maximize(); };
    if (close) close.onclick = function () { api.close(); };
    if (add)   add.onclick   = function () { if (typeof newDocument === 'function') newDocument(); };

    // Double-clicking the draggable strip toggles maximise/restore, like a real
    // titlebar.  (Both this and the drag below are handled in JS because the
    // titlebar's mouse events go to WebView2, not the native window proc.)
    var bar0 = document.getElementById('app-titlebar');
    if (bar0) bar0.addEventListener('dblclick', function (e) {
      if (e.target.closest('.tb-drag')) api.toggle_maximize();
    });

    _initTitlebarDrag(api);

    // Now that tabs belong in the titlebar, (re)render them there and report the
    // draggable region. _renderTabBar may have run before has-native-window was set.
    if (typeof _renderTabBar === 'function') _renderTabBar();
    _reportDragRegions();

    window.addEventListener('resize', _reportDragRegions);
    var bar = document.getElementById('app-titlebar');
    if (bar && typeof ResizeObserver === 'function') {
      new ResizeObserver(_reportDragRegions).observe(bar);
    }
  }
  if (window.pywebview && window.pywebview.api) initTitlebar();
  else window.addEventListener('pywebviewready', initTitlebar);
})();

// ── Titlebar drag (custom) ───────────────────────────────────────────────────
// We drive titlebar dragging ourselves instead of using pywebview's own
// .pywebview-drag-region: that one moves the window by a fixed delta and just
// slides a MAXIMISED window down without un-maximising it.  Here, once the drag
// crosses the start threshold we ask the backend to un-maximise the window under
// the cursor (begin_titlebar_drag), then follow the live cursor with
// move_window_to_cursor — the same native single-window follow the tab tear-off
// uses.  A plain click (no movement) does nothing, so double-click still toggles.
function _initTitlebarDrag(api) {
  var drag = null;                 // {pointerId, startX, startY, started, ready, grabX, grabY, el}
  var THRESH = 4;                  // px before a press becomes a drag

  function onDown(e) {
    if (e.button !== 0) return;                       // left button only
    var el = e.target && e.target.closest && e.target.closest('.tb-drag');
    if (!el) return;
    drag = { pointerId: e.pointerId, startX: e.clientX, startY: e.clientY,
             started: false, ready: false,
             grabX: e.clientX, grabY: e.clientY, el: el };
  }

  function onMove(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    if (!drag.started) {
      if (Math.abs(e.clientX - drag.startX) < THRESH &&
          Math.abs(e.clientY - drag.startY) < THRESH) return;
      drag.started = true;
      try { drag.el.setPointerCapture(drag.pointerId); } catch (_) {}
      // Un-maximise under the cursor (if maximised) and get the grab offset to
      // follow.  Nothing moves until this resolves (drag.ready), so a maximised
      // window is never slid before it restores.
      Promise.resolve(api.begin_titlebar_drag(drag.startX, drag.startY))
        .then(function (g) {
          if (!drag) return;
          if (g) { drag.grabX = g.grabX; drag.grabY = g.grabY; }
          drag.ready = true;
        })
        .catch(function () { if (drag) drag.ready = true; });
    }
    _followThrottled();
  }

  function onUp(e) {
    if (!drag) return;
    try { drag.el.releasePointerCapture(drag.pointerId); } catch (_) {}
    drag = null;
  }

  var busy = false;
  function _followThrottled() {
    if (busy) return;
    busy = true;
    setTimeout(function () {
      busy = false;
      if (!drag || !drag.started || !drag.ready) return;
      try { api.move_window_to_cursor(drag.grabX, drag.grabY); } catch (_) {}
    }, 16);                                            // ~1 SetWindowPos per frame
  }

  document.addEventListener('pointerdown', onDown);
  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerup', onUp);
  document.addEventListener('pointercancel', onUp);
}

// ── Native file dialogs (pywebview only) ────────────────────────────────────
// In the native app these return/accept real OS paths, so the app can open files
// by path and default the save dialog to the document's source folder. In a plain
// browser they return `undefined`, signalling callers to use the browser flow
// (file input / File System Access API).
function isNativeApp() {
  return !!(window.pywebview && window.pywebview.api);
}
// Put DOM focus back on a real element in THIS document.
//
// Anything that takes focus out of the page — the OS Save/Open dialog, the print
// preview's hidden iframe — leaves the document with no focused element when it
// closes.  Key events then route nowhere and every document-level shortcut looks
// broken (the classic report: "Ctrl+P does nothing after saving in Layout, but
// the print button still works", because a click revives focus and a keypress
// cannot).  window.focus() alone is window-level and does not fix this; a real
// element in the document must take focus.  The native app additionally pushes
// focus back into the WebView2 content HWND from Python — see
// win_chrome.focus_web_content — which this cannot do from JS.
function restoreWebFocus() {
  try {
    const ae = document.activeElement;
    // Something real in this document already has focus (a text field the user
    // is typing in, say) — leave it alone.
    if (ae && ae !== document.body && ae.tagName !== 'HTML') return;
    document.body.tabIndex = -1;            // body isn't focusable by default
    document.body.focus();
    window.focus();
  } catch (_) {}
  // Native app only: DOM focus above is not enough when the WebView2 CONTROL
  // itself lost focus to a modal — only Win32 can put it back.
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.restore_focus)
      window.pywebview.api.restore_focus();
  } catch (_) {}
}
window.restoreWebFocus = restoreWebFocus;

async function nativePickOpenFiles(sessionId) {
  if (!isNativeApp() || !window.pywebview.api.pick_open_files) return undefined;
  try {
    return await window.pywebview.api.pick_open_files(sessionId || null);  // [paths] | null(cancel)
  } finally {
    restoreWebFocus();
  }
}
async function nativePickSavePath(sessionId, defaultName) {
  if (!isNativeApp() || !window.pywebview.api.pick_save_path) return undefined;
  try {
    return await window.pywebview.api.pick_save_path(sessionId || null, defaultName || 'document.pdf'); // path | null(cancel)
  } finally {
    restoreWebFocus();
  }
}
// Called by the native drop handler (entry.py) with the dropped files' REAL OS
// paths, so they load by path and the save dialog can default to their folder.
// One callback per panel: the layout appends to the page grid, the viewer opens
// each drop as a new tab (see onViewerFilesDropped in viewer-panel.js).
window.onLayoutFilesDropped = function (paths) {
  if (paths && paths.length && typeof openFilesByPath === 'function') {
    // Real paths arrived — but only open them if the byte-upload fallback has
    // not already handled this same drop (slow bridge on a big grid).
    if (!claimNativeDrop()) return;
    openFilesByPath(paths);
  }
};

// Called from the GTK backend (Linux) when a file is dropped on the WebView.
// WebKitGTK swallows the OS drop before the page's JS drop events fire (and would
// navigate to the file); entry.py blocks that and forwards the real path here so
// we open it into whichever panel is active — mirroring the per-panel routing the
// Windows native-drop handler does by drop target.
window.onNativeFileDrop = function (path) {
  if (!path) return;
  var panel = (typeof _activePanelId === 'function') ? _activePanelId() : 'panel-viewer';
  if (panel === 'panel-layout') {
    if (typeof window.onLayoutFilesDropped === 'function') window.onLayoutFilesDropped([path]);
  } else {   // viewer / editor / unknown → open as a new document tab
    if (typeof window.onViewerFilesDropped === 'function') window.onViewerFilesDropped([path]);
  }
};

// Save a session's PDF to an absolute path server-side; returns true on success.
// Throws with the server's reason so callers can say why a save failed rather
// than just that it did.  pageIds is optional (a subset chosen in Layout).
async function saveSessionToPath(sessionId, savePath, pageIds) {
  const res = await apiFetch('/api/save', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ session_id: sessionId, save_path: savePath, page_ids: pageIds })
  });
  const ct = res.headers.get('Content-Type') || '';
  if (ct.includes('application/json')) {
    const data = await res.json();
    if (data.success) return true;
    throw apiErr(data);
  }
  // The server could not write the path and streamed the PDF back instead; it
  // reports the underlying reason in a header.
  throw new Error(res.headers.get('X-Save-Error') || 'could not write the file');
}

// Turn an API error payload into a localized Error. The backend sends a
// language-neutral `error_key` for user-facing errors (translated here via t());
// technical guards send only English `error`, which is used as-is.
function apiErr(data) {
  var msg = (data && data.error_key && window.t) ? window.t(data.error_key)
          : (data && data.error) || 'Error';
  return new Error(msg);
}

// ── URL params ────────────────────────────────────────────────────────────
const _params    = new URLSearchParams(location.search);
let   SESSION_ID = _params.get('session_id') || '';
let   currentPageId = _params.get('page_id') || '';

// ── Shared page/zoom state (assigned by whichever panel is active) ─────────
let pageW = 0, pageH = 0, renderScale = 1.5, zoomLevel = 1.0;

// ── Light / dark theme ──────────────────────────────────────────────────────
// The server copy (ui_prefs.json, handed back as window.PW_PREFS) is what the
// next launch reads.  localStorage alone forgets the choice whenever the app
// starts on a different port — its origin changes — or the roaming profile
// that holds WebView2's data is reset, which is how it reverted to dark at work.
function _persistTheme(theme) {
  try {
    const f = window.apiFetch || window.fetch;
    f('/api/ui_prefs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme }), keepalive: true,
    }).catch(() => {});
  } catch (_) {}
}
function applyTheme(theme) {
  const light = theme === 'light';
  document.body.classList.toggle('theme-light', light);
  try { localStorage.setItem('pw_theme', light ? 'light' : 'dark'); } catch (_) {}
  _persistTheme(light ? 'light' : 'dark');
  // The page overlays are drawn with resolved colours — repaint them.
  try {
    if (typeof redrawAllRendered === 'function') redrawAllRendered();
  } catch (_) {}
}
function toggleTheme() {
  applyTheme(document.body.classList.contains('theme-light') ? 'dark' : 'light');
}
// Restore the persisted choice (scripts load at the end of <body>).  The server
// copy wins; a choice that only localStorage knows (saved by a build before
// ui_prefs.json existed) is adopted and copied to the server once.
(function () {
  const server = window.PW_PREFS && window.PW_PREFS.theme;
  let local = null;
  try { local = localStorage.getItem('pw_theme'); } catch (_) {}
  const theme = server || local;
  if (theme === 'light') document.body.classList.add('theme-light');
  if (!server && (local === 'light' || local === 'dark')) _persistTheme(local);
})();

// ── Shared confirm overlay ────────────────────────────────────────────────
var _confirmResolve = null;
function dismissConfirm(result) {
  var el = document.getElementById('custom-confirm-overlay');
  el.style.display      = 'none';
  el.style.pointerEvents = 'none';
  if (_confirmResolve) { _confirmResolve(result); _confirmResolve = null; }
}
function showConfirm(msg, title, okLabel, okClass) {
  return new Promise(function(resolve) {
    _confirmResolve = resolve;
    document.getElementById('custom-confirm-title').textContent = title || 'Confirm';
    document.getElementById('custom-confirm-msg').textContent   = msg;
    var ok = document.getElementById('custom-confirm-ok');
    ok.textContent = okLabel || 'OK';
    ok.className   = 'btn ' + (okClass || 'btn-danger');
    document.getElementById('custom-confirm-overlay').style.display      = 'flex';
    document.getElementById('custom-confirm-overlay').style.pointerEvents = 'all';
  });
}
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && _confirmResolve) dismissConfirm(false);
});

// ── Shared toast ──────────────────────────────────────────────────────────
let _toastTimer = null;
// Mirror a message into the persistent off-screen live region so screen readers
// announce it regardless of the visual element's display state. Errors are
// announced assertively (interrupt), everything else politely.
function _srAnnounce(msg, assertive) {
  const sr = document.getElementById('sr-status');
  if (!sr || !msg) return;
  sr.setAttribute('aria-live', assertive ? 'assertive' : 'polite');
  // Re-set to the same text won't re-announce; clear first so repeats are spoken.
  sr.textContent = '';
  // Strip any HTML the caller passed (loading text uses innerHTML).
  const tmp = document.createElement('div'); tmp.innerHTML = msg;
  const text = tmp.textContent || tmp.innerText || '';
  requestAnimationFrame(() => { sr.textContent = text; });
}

function showToast(msg, type) {
  const t  = document.getElementById('toast');
  const tm = document.getElementById('toast-msg');
  _srAnnounce(msg, type === 'error');
  if (!t) return;
  t.className = 'show' + (type ? ' ' + type : '');
  if (tm) tm.textContent = msg;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = ''; }, 3000);
}

// ── Shared loading overlay ────────────────────────────────────────────────
function showLoading(msg) {
  const ov = document.getElementById('loading-overlay');
  const tx = document.getElementById('loading-text');
  if (ov) ov.classList.add('show');
  if (tx && msg !== undefined) tx.innerHTML = msg;
  if (msg !== undefined) _srAnnounce(msg, false);
}
function hideLoading() {
  const ov = document.getElementById('loading-overlay');
  if (ov) ov.classList.remove('show');
}

// ── Progress indicator ────────────────────────────────────────────────────
// Renders a definitional message + progress bar for the loading overlay, e.g.
//   showLoading(loadingProgressHtml(3, 100, 'Loading page 3 of 100…'))
function loadingProgressHtml(current, total, label) {
  const txt = label || `${current} of ${total}`;
  // Indeterminate phase (no total): show just the message — the overlay's
  // spinner already conveys activity.
  if (!total || total <= 0) return txt;
  const pct = Math.max(0, Math.min(100, Math.round(current / total * 100)));
  return `${txt}` +
    `<div style="margin:12px auto 0;width:220px;height:6px;background:var(--surface2);` +
    `border:1px solid var(--border);border-radius:3px;overflow:hidden">` +
    `<div style="width:${pct}%;height:100%;background:var(--accent);transition:width .15s"></div></div>`;
}

// Unique id for a long server operation so the client can poll its progress.
function newProgressId() {
  try { return crypto.randomUUID(); }
  catch (e) { return 'job-' + Date.now() + '-' + Math.random().toString(36).slice(2); }
}

// Polls /api/progress?id=<jobId> and updates the loading overlay until the
// returned stop() function is called.  Safe to call before the worker request
// has registered any progress (it just shows nothing until data appears).
function startProgressPoll(jobId) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    try {
      const r = await apiFetch('/api/progress?id=' + encodeURIComponent(jobId));
      const p = await r.json();
      if (!stopped && p && (p.total || p.label)) {
        showLoading(loadingProgressHtml(p.current, p.total, p.label));
      }
    } catch (e) { /* ignore transient poll errors */ }
    if (!stopped) setTimeout(tick, 200);
  };
  setTimeout(tick, 80);
  return () => { stopped = true; };
}

// ── Panel switching ───────────────────────────────────────────────────────
function _setActivePanel(panelId) {
  ['panel-viewer', 'panel-layout'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = (id === panelId) ? 'flex' : 'none';
  });
}


// Layout can hold a document the viewer has never seen: files dropped straight
// into the grid create a layout `sessionId` while the viewer's SESSION_ID is
// still empty.  Everything document-wide in the app is viewer-owned (print,
// search, save, tabs, annotations), so those features look at an empty
// `allPages` and report "no document loaded" while the grid is full of pages.
//
// Adopt the layout session into the viewer and load its page list.  Resolves
// true when a document is available afterwards, false when there genuinely
// isn't one.  Safe to call repeatedly — it is a no-op once the viewer is
// already on that session.
async function adoptLayoutDocument() {
  const layoutSid = (typeof sessionId !== 'undefined') ? sessionId : null;
  const alreadyOurs = SESSION_ID && (!layoutSid || layoutSid === SESSION_ID);
  if (alreadyOurs && typeof allPages !== 'undefined' && allPages.length) return true;
  if (!layoutSid || layoutSid === SESSION_ID) {
    return (typeof allPages !== 'undefined' && allPages.length > 0);
  }

  SESSION_ID    = layoutSid;
  currentPageId = '';
  if (typeof _tabs !== 'undefined' && _tabs[_activeTabIdx]) {
    _tabs[_activeTabIdx].session_id = SESSION_ID;
    _tabs[_activeTabIdx].page_id    = '';
  }
  if (typeof _resetViewerState === 'function') _resetViewerState();
  if (typeof loadPageList === 'function') {
    await loadPageList();
    if (typeof loadAllAnnotations === 'function') loadAllAnnotations();
    if (typeof loadFormFields     === 'function') loadFormFields();
    if (typeof loadAllLinks       === 'function') loadAllLinks();
  }
  return (typeof allPages !== 'undefined' && allPages.length > 0);
}

// Called by layout's Return button — layout may have rotated/reordered/deleted
// pages so the viewer must reload its page list from the server.  It may also
// be holding a document the viewer has never seen, in which case Return used to
// land on an empty viewer; adoptLayoutDocument() covers that case too.
async function switchToViewerFromLayout() {
  const layoutSid = (typeof sessionId !== 'undefined') ? sessionId : null;
  if (layoutSid && layoutSid !== SESSION_ID) {
    await adoptLayoutDocument();
  } else if (SESSION_ID && typeof _resetViewerState === 'function' && typeof loadPageList === 'function') {
    _resetViewerState();
    loadPageList().then(() => {
      if (typeof loadAllAnnotations === 'function') loadAllAnnotations();
      if (typeof loadFormFields     === 'function') loadFormFields();
      if (typeof loadAllLinks       === 'function') loadAllLinks();
    });
  }
  _setActivePanel('panel-viewer');
}

// Invalidate the viewer's cached render for one page and re-fetch it from the
// server.  Called after the editor applies changes so the viewer immediately
// shows the updated page without requiring a tab-switch.
function _invalidateViewerPage(pageId) {
  if (!pageId || typeof _pageBlocks === 'undefined') return;
  const st = _pageBlocks.get(pageId);
  if (!st) return;
  // Reset render flags and cached image so renderPageBlock re-fetches from
  // the server.  Do NOT clear st.block DOM content — removing the canvas
  // would collapse the block height and scroll the viewport to the next page.
  // The stale canvas stays visible as a placeholder until the fresh render
  // arrives and _buildPageBlockDOM replaces it in-place.
  st.rendered  = false;
  st.rendering = false;
  st.image     = null;
  // Trigger a fresh render for this page
  if (typeof allPages !== 'undefined' && typeof renderPageBlock === 'function') {
    const idx = allPages.findIndex(p => p.id === pageId);
    if (idx >= 0) renderPageBlock(idx);
  }
}

async function switchToLayout() {
  // Detect a document change before we overwrite the layout's own sessionId.
  // When switching to a different tab that was last in Layout mode, the grid
  // still holds the previous document's pages and must be reloaded.
  const sessionChanged =
    (typeof sessionId !== 'undefined') && SESSION_ID && sessionId !== SESSION_ID;
  // Always sync layout sessionId from viewer SESSION_ID
  if (typeof sessionId !== 'undefined' && SESSION_ID) {
    sessionId = SESSION_ID;
  }
  // Load the pages when the grid is empty or the active document changed.
  if (SESSION_ID && typeof pages !== 'undefined' && (!pages.length || sessionChanged)) {
    try {
      const res = await apiFetch('/api/viewer/pages', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({session_id: SESSION_ID})
      });
      const data = await res.json();
      if (data.pages && data.pages.length) {
        pages = data.pages;
        if (typeof renderGrid === 'function') renderGrid();
      }
    } catch(e) { console.warn('[switchToLayout] page sync failed:', e.message); }
  }
  _setActivePanel('panel-layout');
}

// ── Keyboard-shortcuts overlay (press ?) ─────────────────────────────────────
// A static, categorized cheat sheet. The data here mirrors the real key handlers
// in viewer-panel.js / editor.js / layout.js — keep it in sync when shortcuts
// change. fmtKeys() renders cross-platform keycaps and will also feed the
// command palette (Phase 2).
const IS_MAC = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || '');

const _KEY_GLYPH = {
  Mod:   IS_MAC ? '⌘' : 'Ctrl',   // ⌘ on macOS, Ctrl elsewhere
  Shift: IS_MAC ? '⇧' : 'Shift',
  Alt:   IS_MAC ? '⌥' : 'Alt',
  Enter: '↵', Esc: 'Esc', Up: '↑', Down: '↓',
  PageUp: 'PgUp', PageDown: 'PgDn',
  Delete: 'Del', Backspace: '⌫',
  Plus: '+', Minus: '−', Scroll: 'Scroll',
};
function _glyph(tok) { return _KEY_GLYPH[tok] || tok; }

// One chord like "Mod+Shift+Z" → "<kbd>⌘</kbd><kbd>⇧</kbd><kbd>Z</kbd>"
function fmtChord(chord) {
  return chord.split('+').map(t => `<kbd>${_glyph(t)}</kbd>`).join('');
}
// Alternatives → chords joined by a muted "or"
function fmtKeys(keys) {
  return keys.map(fmtChord).join('<span class="kbd-or">or</span>');
}

const SHORTCUTS = [
  { group: 'File', items: [
    { desc: 'New document',  keys: ['Mod+N'], scope: 'Viewer' },
    { desc: 'Open…',    keys: ['Mod+O'] },
    { desc: 'Save',          keys: ['Mod+S'] },
    { desc: 'Close tab',     keys: ['Mod+W'] },
    { desc: 'Print',         keys: ['Mod+P'] },
  ]},
  { group: 'Search', items: [
    { desc: 'Find in document',      keys: ['Mod+F'], scope: 'Viewer' },
    { desc: 'Next / previous match', keys: ['Enter', 'Shift+Enter'], scope: 'Viewer' },
    { desc: 'Clear search',          keys: ['Esc'], scope: 'Viewer' },
  ]},
  { group: 'Navigate & view', items: [
    { desc: 'Command palette',    keys: ['Mod+K'] },
    { desc: 'Previous page',      keys: ['Up', 'PageUp'], scope: 'Viewer' },
    { desc: 'Next page',          keys: ['Down', 'PageDown'], scope: 'Viewer' },
    { desc: 'Exit fullscreen',    keys: ['Esc'], scope: 'Viewer' },
    { desc: 'Keyboard shortcuts', keys: ['?'] },
  ]},
  { group: 'Zoom', items: [
    { desc: 'Zoom in',        keys: ['Plus'], scope: 'Viewer' },
    { desc: 'Zoom out',       keys: ['Minus'], scope: 'Viewer' },
    { desc: 'Zoom in / out',  keys: ['Mod+Plus', 'Mod+Minus'], scope: 'Layout' },
    { desc: 'Reset zoom',     keys: ['Mod+0'], scope: 'Layout' },
    { desc: 'Zoom to cursor', keys: ['Mod+Scroll'] },
  ]},
  // Layout's gestures are not keyboard shortcuts, but this overlay is the app's
  // only discovery surface for them — they used to be a prose hint sitting in
  // the Layout toolbar, which is where the crowding started.
  { group: 'Layout', items: [
    { desc: 'Reorder pages',            keys: ['Drag'] },
    { desc: 'More actions on a page',   keys: ['Right-click'] },
    { desc: 'Include / exclude a page', keys: ['Checkbox'] },
  ]},
  { group: 'Editing', items: [
    { desc: 'Undo',               keys: ['Mod+Z'], scope: 'Viewer' },
    { desc: 'Redo',               keys: ['Mod+Y', 'Mod+Shift+Z'], scope: 'Viewer' },
    { desc: 'Commit / accept',    keys: ['Enter'] },
    { desc: 'Cancel',             keys: ['Esc'] },
    { desc: 'Delete annotation / selected text', keys: ['Delete', 'Backspace'], scope: 'Viewer' },
    { desc: 'Copy / paste annotations', keys: ['Mod+C', 'Mod+V'], scope: 'Viewer' },
    { desc: 'Paste an image from the clipboard', keys: ['Mod+V'], scope: 'Viewer' },
  ]},
];

function _renderShortcuts() {
  const grid = document.getElementById('sc-grid');
  if (!grid) return;
  grid.innerHTML = SHORTCUTS.map(sec => `
    <section class="sc-sec">
      <h4 class="sc-sec-title">${sec.group}</h4>
      ${sec.items.map(it => `
        <div class="sc-row">
          <span class="sc-desc">${it.desc}${it.scope ? `<span class="sc-scope">${it.scope}</span>` : ''}</span>
          <span class="sc-keys">${fmtKeys(it.keys)}</span>
        </div>`).join('')}
    </section>`).join('');
}

let _scPrevFocus = null;
function _shortcutsOpen() {
  const m = document.getElementById('shortcuts-modal');
  return !!m && m.style.display === 'flex';
}
function openShortcutsOverlay() {
  const m = document.getElementById('shortcuts-modal');
  if (!m) return;
  _renderShortcuts();
  _scPrevFocus = document.activeElement;
  m.style.display = 'flex';
  const closeBtn = document.getElementById('sc-close');
  if (closeBtn) closeBtn.focus();
}
function closeShortcutsOverlay() {
  const m = document.getElementById('shortcuts-modal');
  if (!m) return;
  m.style.display = 'none';
  if (_scPrevFocus && _scPrevFocus.focus) _scPrevFocus.focus();
  _scPrevFocus = null;
}

// Global hooks: "?" toggles the cheat sheet; Esc closes it. Capture phase so it
// works from any panel and isn't swallowed by other listeners.
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && _shortcutsOpen()) {
    e.preventDefault(); e.stopPropagation(); closeShortcutsOverlay(); return;
  }
  if (e.key !== '?') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const ae = document.activeElement;
  const tag = (ae && ae.tagName) || '';
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (ae && ae.isContentEditable)) return;
  e.preventDefault(); e.stopPropagation();
  if (_shortcutsOpen()) closeShortcutsOverlay(); else openShortcutsOverlay();
}, true);

// ── Command palette (Ctrl/Cmd+K) ─────────────────────────────────────────────
// Fuzzy-searchable list of every action, scoped to the active panel plus global
// commands. Each command's run() calls a function that already exists; enabled()
// reflects live state so unavailable actions render greyed. Shares fmtKeys() and
// the modal pattern with the shortcuts overlay above.

// Safe reads of viewer-panel globals (declared in another script with `let`).
function _hasDoc()   {
  try {
    if (typeof allPages !== 'undefined' && allPages.length > 0) return true;
    // Layout can hold a document the viewer has never loaded.
    return typeof pages !== 'undefined' && Array.isArray(pages) && pages.length > 0;
  } catch (_) { return false; }
}
function _multiTab() { try { return typeof _tabs !== 'undefined' && _tabs.length > 1; } catch (_) { return false; } }
function _curPanel() {
  if (typeof _activePanelId === 'function') { const p = _activePanelId(); if (p) return p; }
  for (const id of ['panel-viewer', 'panel-layout']) {
    const el = document.getElementById(id);
    if (el && el.style.display !== 'none') return id;
  }
  return 'panel-viewer';
}
const _wViewer = (p) => p === 'panel-viewer';
const _wLayout = (p) => p === 'panel-layout';
// Call a global handler by name.  These panels load as plain (non-module)
// scripts, so every top-level `function foo(){}` is a property of window — no
// eval fallback is needed (and avoiding eval keeps the page CSP-clean).
const _call = (name, ...args) => {
  if (typeof window[name] === 'function') return window[name](...args);
};

// Context-aware Open / Save mirror the keyboard shortcuts.
function _cmdOpen() {
  if (_curPanel() === 'panel-layout') _call('addFiles');
  else _call('viewerOpenViaDialog');
}
function _cmdSave() {
  const p = _curPanel();
  if      (p === 'panel-layout') _call('saveDocument');
  else                           _call('viewerSaveToFile');
}

// Annotation tools expand into one command each.
const _ANNOT_TOOLS = [
  ['text', 'Text'], ['highlight', 'Highlight'], ['rect', 'Rectangle'], ['ellipse', 'Ellipse'],
  ['line', 'Line'], ['arrow', 'Arrow'], ['darrow', 'Double-ended arrow'],
  ['cloud', 'Revision cloud'], ['sticky', 'Sticky note'],
  ['image', 'Image'], ['signature', 'Signature'],
];

const COMMANDS = [
  // ── File ──
  { title: 'New document',       section: 'File', keys: ['Mod+N'], keywords: ['blank', 'create'], run: () => _call('newDocument') },
  { title: 'Open…',         section: 'File', keys: ['Mod+O'], keywords: ['file', 'load', 'add'], run: _cmdOpen },
  { title: 'Save / Apply',       section: 'File', keys: ['Mod+S'], keywords: ['export', 'write', 'download'], enabled: _hasDoc, run: _cmdSave },
  { title: 'Close tab',          section: 'File', keys: ['Mod+W'], keywords: ['shut'], enabled: _multiTab, run: () => _call('_closeTab', (typeof _activeTabIdx !== 'undefined' ? _activeTabIdx : 0)) },
  { title: 'Print',              section: 'File', keys: ['Mod+P'], keywords: ['paper'], enabled: _hasDoc, run: () => _call('printDocument') },
  { title: 'Document properties',section: 'File', keywords: ['metadata', 'title', 'author', 'info'], when: _wViewer, enabled: _hasDoc, run: () => _call('showDocProperties') },
  { title: 'Password protect…',  section: 'File', keywords: ['encrypt', 'lock', 'security'], when: _wViewer, enabled: _hasDoc, run: () => _call('showPasswordProtectModal') },

  // ── Go to ──
  { title: 'Go to Layout',       section: 'Go to', keywords: ['pages', 'manage', 'reorder', 'merge'], when: (p) => !_wLayout(p), enabled: _hasDoc, run: () => _call('goLayout') },
  { title: 'Back to Viewer',     section: 'Go to', keywords: ['return', 'read'], when: _wLayout, run: () => _call('switchToViewerFromLayout') },

  // ── View (viewer) ──
  { title: 'Find in document',   section: 'View', keys: ['Mod+F'], keywords: ['search', 'text'], when: _wViewer, enabled: _hasDoc, run: () => { const si = document.getElementById('search-input'); if (si) { si.focus(); si.select(); } } },
  { title: 'Next page',          section: 'View', keys: ['Down'], keywords: ['forward'], when: _wViewer, enabled: _hasDoc, run: () => _call('nextPage') },
  { title: 'Previous page',      section: 'View', keys: ['Up'], keywords: ['back'], when: _wViewer, enabled: _hasDoc, run: () => _call('prevPage') },
  { title: 'Zoom in',            section: 'View', when: _wViewer, enabled: _hasDoc, run: () => _call('zoomIn') },
  { title: 'Zoom out',           section: 'View', when: _wViewer, enabled: _hasDoc, run: () => _call('zoomOut') },
  { title: 'Reset zoom',         section: 'View', keywords: ['100%'], when: _wViewer, enabled: _hasDoc, run: () => _call('zoomReset') },
  { title: 'Fit width',          section: 'View', keywords: ['fit', 'page width'], when: _wViewer, enabled: _hasDoc, run: () => _call('zoomFitWidth') },
  { title: 'Fullscreen',         section: 'View', keywords: ['presentation', 'full screen'], when: _wViewer, enabled: _hasDoc, run: () => _call('enterFullscreen') },
  { title: 'Toggle comments panel', section: 'View', keywords: ['notes', 'annotations', 'sidebar'], when: _wViewer, run: () => _call('toggleRightPanel') },
  { title: 'Toggle bookmarks / pages', section: 'View', keywords: ['outline', 'thumbnails'], when: _wViewer, run: () => _call('toggleLeftMode') },

  // ── Annotate (viewer) ──
  ..._ANNOT_TOOLS.map(([tool, label]) => ({
    title: 'Annotate: ' + label, section: 'Annotate',
    keywords: ['markup', 'draw', 'annotation', label],
    when: _wViewer, enabled: _hasDoc, run: () => _call('selectAnnotTool', tool),
  })),
  { title: 'Add bookmark (current page)', section: 'Annotate', keywords: ['outline'], when: _wViewer, enabled: _hasDoc, run: () => _call('addBookmarkCurrentPage') },
  { title: 'Auto-detect bookmarks',       section: 'Annotate', keywords: ['outline', 'contents'], when: _wViewer, enabled: _hasDoc, run: () => _call('autoBookmarks') },

  // ── Pages (layout) ──
  { title: 'Add files',          section: 'Pages', keywords: ['open', 'import', 'merge'], when: _wLayout, run: () => _call('addFiles') },
  { title: 'Select all pages',   section: 'Pages', when: _wLayout, run: () => _call('selectAllPages') },
  { title: 'Deselect all pages', section: 'Pages', when: _wLayout, run: () => _call('deselectAllPages') },
  { title: 'Rotate selected clockwise',         section: 'Pages', keywords: ['turn'], when: _wLayout, run: () => _call('rotateSelected', 90) },
  { title: 'Rotate selected counter-clockwise', section: 'Pages', keywords: ['turn'], when: _wLayout, run: () => _call('rotateSelected', -90) },
  { title: 'Compress',           section: 'Pages', keywords: ['shrink', 'size'], when: _wLayout, run: () => _call('showCompress') },
  { title: 'Delete selected pages', section: 'Pages', keywords: ['remove'], when: _wLayout, run: () => _call('deleteSelectedPages') },
  { title: 'Export as…',         section: 'Pages', keywords: ['save', 'convert', 'word', 'image'], when: _wLayout, run: () => _call('showExportModal') },
  { title: 'Header / Footer…',   section: 'Pages', keywords: ['page numbers', 'stamp'], when: _wLayout, run: () => _call('showHFModal') },
  { title: 'Larger page thumbnails',  section: 'Pages', keywords: ['zoom in', 'bigger', 'grid'], when: _wLayout, run: () => _call('layZoomIn') },
  { title: 'Smaller page thumbnails', section: 'Pages', keywords: ['zoom out', 'smaller', 'grid'], when: _wLayout, run: () => _call('layZoomOut') },

  // ── Help (global) ──
  { title: 'Keyboard shortcuts', section: 'Help', keys: ['?'], keywords: ['cheat sheet', 'keys', 'hotkeys'], run: openShortcutsOverlay },
  { title: 'User manual',        section: 'Help', keywords: ['docs', 'guide', 'help'], run: () => _call('openBundledFile', 'PatchworkPDF_Manual.pdf') },
  { title: 'Changelog',          section: 'Help', keywords: ['whats new', 'version', 'release'], run: () => _call('openBundledFile', 'changelog.docx') },
  { title: 'View license',       section: 'Help', keywords: ['legal', 'terms'], run: () => _call('showLicense') },
  { title: 'About Patchwork PDF',section: 'Help', keywords: ['version', 'credits'], run: () => _call('showAbout') },
  { title: 'Toggle light / dark theme', section: 'Help', keywords: ['dark mode', 'light mode', 'appearance'], run: () => _call('toggleTheme') },
];

// Subsequence fuzzy score; -1 if `query` isn't a subsequence of `text`.
function _fuzzyScore(query, text) {
  query = query.toLowerCase(); text = text.toLowerCase();
  if (!query) return 0;
  let qi = 0, score = 0, prevIdx = -1, consec = 0;
  for (let ti = 0; ti < text.length && qi < query.length; ti++) {
    if (text[ti] === query[qi]) {
      let bonus = 1;
      if (ti === prevIdx + 1) { consec++; bonus += consec * 3; } else consec = 0;
      if (ti === 0 || /[\s\-_/]/.test(text[ti - 1])) bonus += 6;   // word-boundary bonus
      score += bonus; prevIdx = ti; qi++;
    }
  }
  if (qi < query.length) return -1;
  return score - text.length * 0.04;                                // prefer shorter matches
}
function _cmdScore(query, cmd) {
  if (!query) return 0;
  const q = query.toLowerCase();
  const title = cmd.title.toLowerCase();
  let best = _fuzzyScore(q, title);
  if (title.includes(q)) best = Math.max(best, 60 + (title.startsWith(q) ? 30 : 0) - title.length * 0.04);
  const extra = ((cmd.keywords || []).join(' ') + ' ' + (cmd.section || '')).toLowerCase();
  const es = _fuzzyScore(q, extra);
  if (es >= 0) best = Math.max(best, es * 0.5);                      // keyword match is weaker
  return best;
}

let _cpRows = [];   // [{el, cmd, disabled}]
let _cpSel  = -1;
let _cpPrevFocus = null;

function _cpItemHTML(c, disabled, idx) {
  return `<div class="cp-item${disabled ? ' disabled' : ''}" data-idx="${idx}" role="option"
       ${disabled ? '' : `onmousemove="_cpHover(${idx})" onclick="_cpRun(${idx})"`}>
    <span class="cp-title">${c.title}</span>
    <span class="cp-sec">${c.section || ''}</span>
    ${c.keys ? `<span class="cp-keys">${fmtKeys(c.keys)}</span>` : ''}
  </div>`;
}
function _cpRender(query) {
  const list = document.getElementById('cp-list');
  if (!list) return;
  const panel = _curPanel();
  const cmds  = COMMANDS.filter(c => (typeof c.when === 'function') ? c.when(panel) : true);
  const dis   = (c) => (typeof c.enabled === 'function') ? !c.enabled() : false;
  const rows  = [];
  let html = '';
  if (query) {
    cmds
      .map(c => ({ c, s: _cmdScore(query, c) }))
      .filter(x => x.s >= 0)
      .sort((a, b) => b.s - a.s)
      .forEach(({ c }) => { const i = rows.length; rows.push(c); html += _cpItemHTML(c, dis(c), i); });
  } else {
    let lastSec = null;
    cmds.forEach(c => {
      if (c.section !== lastSec) { html += `<div class="cp-sec-label">${c.section || 'Other'}</div>`; lastSec = c.section; }
      const i = rows.length; rows.push(c); html += _cpItemHTML(c, dis(c), i);
    });
  }
  list.innerHTML = rows.length ? html : `<div class="cp-empty">No matching actions</div>`;
  const els = [...list.querySelectorAll('.cp-item')];
  _cpRows = els.map((el, i) => ({ el, cmd: rows[i], disabled: dis(rows[i]) }));
  _cpSel  = _cpRows.findIndex(r => !r.disabled);
  _cpHighlight();
}
function _cpHighlight() {
  _cpRows.forEach((r, i) => r.el.classList.toggle('sel', i === _cpSel));
  if (_cpSel >= 0 && _cpRows[_cpSel]) _cpRows[_cpSel].el.scrollIntoView({ block: 'nearest' });
}
function _cpHover(i) { if (_cpRows[i] && !_cpRows[i].disabled) { _cpSel = i; _cpHighlight(); } }
function _cpMove(dir) {
  if (!_cpRows.length) return;
  let i = _cpSel;
  for (let n = 0; n < _cpRows.length; n++) {
    i = (i + dir + _cpRows.length) % _cpRows.length;
    if (!_cpRows[i].disabled) { _cpSel = i; break; }
  }
  _cpHighlight();
}
function _cpRun(i) {
  const r = _cpRows[(i != null) ? i : _cpSel];
  if (!r || r.disabled) return;
  closeCommandPalette();
  try { r.cmd.run(); }
  catch (e) { if (typeof showToast === 'function') showToast(window.t('toast.action_failed') + e.message, 'error'); }
}
function _cpOnInput() { _cpRender(document.getElementById('cp-input').value.trim()); }
function _cpOnKey(e) {
  if      (e.key === 'ArrowDown') { e.preventDefault(); _cpMove(1); }
  else if (e.key === 'ArrowUp')   { e.preventDefault(); _cpMove(-1); }
  else if (e.key === 'Enter')     { e.preventDefault(); _cpRun(); }
  else if (e.key === 'Escape')    { e.preventDefault(); closeCommandPalette(); }
}
function _cpIsOpen() { const m = document.getElementById('command-palette'); return !!m && m.style.display === 'flex'; }
function closeCommandPalette() {
  const m = document.getElementById('command-palette');
  if (!m) return;
  m.style.display = 'none';
  if (_cpPrevFocus && _cpPrevFocus.focus) _cpPrevFocus.focus();
  _cpPrevFocus = null;
}
function openCommandPalette() {
  if (_cpIsOpen()) { closeCommandPalette(); return; }
  const m = document.getElementById('command-palette');
  if (!m) return;
  if (_shortcutsOpen()) closeShortcutsOverlay();
  _cpPrevFocus = document.activeElement;
  m.style.display = 'flex';
  const inp = document.getElementById('cp-input');
  if (inp) { inp.value = ''; }
  _cpRender('');
  if (inp) inp.focus();
}

// Ctrl/Cmd+K toggles the palette (capture phase to pre-empt any browser default).
document.addEventListener('keydown', function(e) {
  if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
  if ((e.key || '').toLowerCase() !== 'k') return;
  e.preventDefault(); e.stopPropagation();
  openCommandPalette();
}, true);

// ── Crash-recovery draft store (disk-backed via the backend) ────────────────
// Lightweight, fail-safe persistence for in-progress editor work so an accidental
// tab close, renderer crash, or app/server restart doesn't silently lose markup.
// Only small JSON edit-deltas are stored (coordinates, text, style) — never page
// image bytes — so writes are cheap and the store stays tiny.
//
// Persistence lives on DISK under the app's state dir (handled by the Flask
// backend), NOT browser IndexedDB. IndexedDB is scoped to the WebView2 origin
// (127.0.0.1:<port>), and the app's port can shift between launches, which would
// orphan the drafts after exactly the kind of crash we're trying to recover from.
// Records are keyed by DOCUMENT identity (filename + page count + page number),
// independent of the volatile session UUID, so a draft survives into a fresh
// session when the same file/page is reopened.

function _pwPost(url, body) {
  return apiFetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

// All draft helpers swallow errors and resolve to a benign value: persistence is a
// safety net, never a hard dependency — the editor must keep working if a write
// fails (no writable dir, server hiccup, etc.).



// Metadata for every stored draft (no edit state) — drives the launch-time prompt.

// Discard a specific set of drafts, or all when no keys are given.
async function pwDraftClear(docKeys) {
  try { await _pwPost('/api/drafts/clear', docKeys ? { docKeys } : {}); } catch (_) {}
}

// ── Unsaved-work guard (beforeunload) ───────────────────────────────────────
// Views register a predicate that returns true when they hold unsaved work. If
// any returns true on unload, the browser shows its native "leave site?" prompt.
// The registry is deliberately generic so later phases (e.g. a document-modified
// -since-last-save flag spanning all three views) can opt in without new wiring.
const _pwDirtyChecks = [];
function pwRegisterDirtyCheck(fn) {
  if (typeof fn === 'function') _pwDirtyChecks.push(fn);
}
function pwHasUnsavedWork() {
  for (const fn of _pwDirtyChecks) {
    try { if (fn()) return true; } catch (_) {}
  }
  return false;
}
window.addEventListener('beforeunload', (e) => {
  if (!pwHasUnsavedWork()) return;
  e.preventDefault();
  e.returnValue = '';   // required for the prompt to appear in most browsers
  return '';
});

// ── Applied/committed-but-unsaved recovery (server working-bytes snapshots) ──
// Complements the un-applied draft store above.  The server snapshots the actual
// working page bytes (viewer annotations + editor Apply) on its own; these helpers
// just drive the launch prompt, restore, and clear-on-save.  A document-level
// dirty flag (set on any applied/committed change, cleared on Save) also feeds the
// beforeunload guard so the user is warned about recoverable-but-unsaved work.
async function pwRecoveryList() {
  try {
    const r = await _pwPost('/api/recovery/list', {});
    const d = await r.json();
    return (d && d.snapshots) || [];
  } catch (_) { return []; }
}
async function pwRecoveryRestore(docId) {
  if (!docId) return null;
  try {
    const r = await _pwPost('/api/recovery/restore', { docId });
    const d = await r.json();
    return (d && !d.error) ? d : null;
  } catch (_) { return null; }
}
// opts: { session_id } after a successful Save, or { docId } to discard a snapshot.
async function pwRecoveryClear(opts) {
  try { await _pwPost('/api/recovery/clear', opts || {}); } catch (_) {}
}

let _pwDocDirty = false;
function pwSetDocDirty(v) { _pwDocDirty = (v !== false); pwPublishUnsaved(); }
// The flag tracks the document on screen, so anything juggling several of them
// (the viewer's tabs) has to park it per document — see _saveActiveTabMeta.
function pwIsDocDirty() { return _pwDocDirty; }
pwRegisterDirtyCheck(() => _pwDocDirty);

// The ONE place a successful full-document save is recorded.  Every panel has
// its own Save — viewer-panel's viewerSaveToFile, editor's saveToFile, layout's
// saveDocument — and each of those has three write paths (native OS dialog,
// showSaveFilePicker, browser download).  Nine endings, all meaning the same
// thing: the working state is on disk now.  When only the viewer's three called
// this, applying an edit and then hitting Save in the *editor* left the document
// flagged dirty forever, so closing the app claimed unsaved changes that had
// already been written — and the tab carried that phantom flag across every
// later tab switch (_saveActiveTabMeta parks it), so it warned about background
// tabs too.  Route every save ending here rather than repeating the pair.
//
// Only for a save of the WHOLE document: exporting a subset of pages to a new
// file (Layout's checked-pages save) leaves the document itself unsaved.
function pwMarkSaved(sessionId) {
  if (typeof pwSetDocDirty   === 'function') pwSetDocDirty(false);
  if (typeof pwRecoveryClear === 'function' && sessionId) {
    pwRecoveryClear({ session_id: sessionId });
  }
}

// ── Quitting the app with unsaved work ──────────────────────────────────────
// beforeunload does not fire in the native host, and the close guard there runs
// on the UI thread where the CSP forbids eval — so it cannot ask us anything at
// close time.  Instead we PUSH the answer whenever it changes; entry.py's
// _guard_window_close reads that cache and calls pwPromptQuit() below when a
// close would lose work.  Callers that change per-tab state (viewer-panel's
// _renderTabBar) publish too, since the flag alone does not see other tabs.
// Starts false to match WindowApi._unsaved's default, so a clean session
// never spends a bridge call saying so.
let _pwLastPublished = false;
function pwPublishUnsaved() {
  if (!isNativeApp() || !window.pywebview.api.set_unsaved) return;
  const v = pwHasUnsavedWork();
  if (v === _pwLastPublished) return;      // only on a real change
  // Record it only once it is actually across the bridge, so a change made
  // before pywebview is ready is not mistaken for one already reported.
  try { window.pywebview.api.set_unsaved(v); _pwLastPublished = v; }
  catch (_) { /* window going away */ }
}

// Raised by the native close guard, which has already cancelled that close.
// Answering yes latches the decision and closes again; no just backs out.
let _pwQuitPromptOpen = false;
async function pwPromptQuit() {
  if (_pwQuitPromptOpen) return;
  _pwQuitPromptOpen = true;
  let ok = false;
  try {
    ok = await showConfirm(window.t('confirm.quit_unsaved_msg'),
                           window.t('confirm.quit_unsaved_title'),
                           window.t('confirm.close_without_saving'), 'btn-danger');
  } finally {
    _pwQuitPromptOpen = false;
  }
  const api = (window.pywebview && window.pywebview.api) || null;
  if (!api) return;
  try {
    if (ok) { await api.confirm_quit(); api.close(); }
    else    { await api.cancel_quit(); }
  } catch (_) { /* window already gone */ }
}
window.pwPromptQuit = pwPromptQuit;

