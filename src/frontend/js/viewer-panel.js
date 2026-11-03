/* ═══════════════════════════════════════════════════════
   VIEWER JS (viewer.html)
════════════════════════════════════════════════════════ */

// ── URL params ──────────────────────────────────────────────────────────────
// ── Per-page block state ─────────────────────────────────────────────────────
// page_id → { block, canvas, hlOv, annotOv, selOv,
//             image, pageW, pageH, renderScale, rendered, rendering }
const _pageBlocks = new Map();
let _renderObserver = null;   // IntersectionObserver for lazy rendering
let _scrollRafId    = null;   // rAF handle for scroll-tracking debounce

// ── Tab management ──────────────────────────────────────────────────────────
// Each entry: {session_id, page_id, label, zoomLevel, scroll}
// The active tab's full state lives in the regular globals below.
// Inactive tabs only keep this lightweight descriptor; they reload on switch.
let _tabs         = [];
let _activeTabIdx = 0;

// Scroll position is remembered per tab as an *anchor* — {pageId, dy, dx}: the
// page block the viewport starts in, plus the offset into it — not a raw
// scrollTop.  Switching back rebuilds the column from placeholder heights that
// only settle as pages render, so a raw scrollTop would land somewhere else;
// an anchor is expressed in coordinates that survive the rebuild, and re-pins
// itself as the true heights arrive (see _pendingPin).
// Returns null when there is nothing meaningful to capture (no document, or the
// viewer panel is hidden because the tab is in the editor/layout) — callers keep
// the previously stored anchor in that case.
function _captureScrollAnchor() {
  const area = document.getElementById('canvas-area');
  if (!area || !area.clientHeight || !allPages.length) return null;
  // The anchor is the last block starting at or above the viewport top; if the
  // viewport sits above every block (column padding), anchor to the first one.
  let pageId = '', top = 0;
  for (const p of allPages) {
    const block = document.getElementById('page-block-' + p.id);
    if (!block) continue;
    const bt = block.offsetTop;
    if (pageId && bt > area.scrollTop + 1) break;
    pageId = p.id; top = bt;
  }
  if (!pageId) return null;
  return { pageId, dy: area.scrollTop - top, dx: area.scrollLeft };
}

// Anchor to restore on the next loadPageList(), set when switching to a tab.
let _pendingScrollAnchor = null;

// The thumbnail pane carries its own scroll position, remembered per tab as a
// raw scrollTop.  A raw value is enough here (unlike the page column): the
// thumb boxes are pre-sized from the page dimensions, so the list's geometry
// does not move under the value while the images decode.
// null = nothing remembered for the pane — it keeps whatever position it has,
// which after _resetViewerState is the top.
let _pendingThumbScroll = null;

// Returns null when there is no live position worth reading: the pane is hidden
// (the tab is in the editor/layout, or the left panel is showing bookmarks),
// where scrollTop reads 0 and would erase what the tab had.
function _captureThumbScroll() {
  const list = document.getElementById('thumb-list');
  if (!list || !list.clientHeight || !allPages.length) return null;
  return list.scrollTop;
}

// Adopt a tab's parked unsaved-changes state as we make it the active one.
function _restoreTabDirty(tab) {
  if (typeof pwSetDocDirty === 'function') pwSetDocDirty(tab && tab.dirty === true);
}

// The shared guard only knows the document on screen.  Closing the window with
// edits sitting in a BACKGROUND tab has to warn as well, so report those too.
if (typeof pwRegisterDirtyCheck === 'function') {
  pwRegisterDirtyCheck(() => _tabs.some((t, i) => i !== _activeTabIdx && t.dirty === true));
}

function _saveActiveTabMeta() {
  if (!_tabs[_activeTabIdx]) return;
  // Unsaved-changes state belongs to the document, but the flag it lives in is
  // a single global describing whatever is on screen.  Park it with the tab we
  // are leaving, or the next document inherits this one's dirtiness — and the
  // next Save clears a flag that was standing for a different file.
  if (typeof pwIsDocDirty === 'function') _tabs[_activeTabIdx].dirty = pwIsDocDirty();
  _tabs[_activeTabIdx].page_id   = currentPageId;
  _tabs[_activeTabIdx].label     = allPages.length ? allPages[0].filename : 'Document';
  _tabs[_activeTabIdx].zoomLevel = zoomLevel;
  const anchor = _captureScrollAnchor();
  if (anchor) _tabs[_activeTabIdx].scroll = anchor;
  const thumbTop = _captureThumbScroll();
  if (thumbTop !== null) _tabs[_activeTabIdx].thumbScroll = thumbTop;
  // Remember which panel this tab is currently showing so switching back to it
  // restores the same mode (Viewer / Layout) rather than always reverting to
  // the viewer. Read the live DOM — it is the source of truth for the active tab.
  // An EMPTY inline display is the Layout panel's hidden default (the CSS
  // hides it; only _setActivePanel ever writes 'flex'/'none').  Testing
  // `!== 'none'` read that default as "visible", so every tab left before
  // Layout had ever been opened was parked as a Layout tab — and coming back
  // to it (clicking it, or closing the tab in front of it) dropped the user
  // into Layout for no reason.
  const layPanel = document.getElementById('panel-layout');
  _tabs[_activeTabIdx].mode = _panelShown(layPanel) ? 'layout' : 'viewer';
}

// Restore the panel (Viewer / Layout) the given tab was last showing.
function _restoreTabPanel(tab) {
  if (tab && tab.mode === 'layout' && typeof switchToLayout === 'function') {
    switchToLayout();
  } else if (typeof _setActivePanel === 'function') {
    _setActivePanel('panel-viewer');
  }
}

function _escHtml(s) {
  return String(s || 'Document')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _tabMarkup() {
  // No onclick / pop-out button: tab switching and tear-off-to-new-window are
  // both driven by the pointer-drag controller below (a click — press without
  // drag — switches; dragging a tab off the window detaches it).
  return _tabs.map((t, i) => `
    <div class="v-tab${i === _activeTabIdx ? ' active' : ''}"
         data-idx="${i}" title="${_escHtml(t.label)}">
      <span class="v-tab-label">${_escHtml(t.label)}</span>
      ${_tabs.length > 1 ? `<button class="v-tab-close" onclick="event.stopPropagation();_closeTab(${i})" title="Close tab">&#x2715;</button>` : ''}
    </div>`).join('');
}

function _renderTabBar() {
  // Runs after every tab mutation (open / close / switch / reorder), which is
  // the only time _tabs[].dirty can change — and the doc-dirty flag alone does
  // not see other tabs.  Cheap: pwPublishUnsaved only crosses the bridge when
  // the answer actually changed.
  if (typeof pwPublishUnsaved === 'function') pwPublishUnsaved();
  const native  = document.body.classList.contains('has-native-window');
  const inPanel = document.getElementById('tab-bar');
  const tbTabs  = document.getElementById('tb-tabs');

  if (native && tbTabs) {
    // Native app: tabs live in the custom titlebar, always shown (>=1 tab).
    if (inPanel) { inPanel.classList.remove('show'); inPanel.innerHTML = ''; }
    tbTabs.innerHTML = _tabMarkup();
    if (typeof _reportDragRegions === 'function') _reportDragRegions();
    return;
  }

  // Browser/dev: keep the in-panel bar, shown only when there's more than one tab.
  if (!inPanel) return;
  inPanel.classList.toggle('show', _tabs.length > 1);
  inPanel.innerHTML = (_tabs.length <= 1) ? '' : _tabMarkup();
}

// AbortController used to cancel in-flight viewer API requests when switching tabs.
// Replaced in _resetViewerState() so old requests abort immediately.
let _viewerAbortCtrl = new AbortController();

// Wrapper around apiFetch that injects the current viewer AbortController signal.
// Use this for all viewer-side data fetches so they are cancelled on tab switch.
function _vFetch(url, options) {
  options = options || {};
  options.signal = _viewerAbortCtrl.signal;
  return apiFetch(url, options);
}

function _resetViewerState() {
  // Cancel any in-flight requests from the previous tab
  _viewerAbortCtrl.abort();
  _viewerAbortCtrl = new AbortController();

  allPages        = [];
  currentIdx      = 0;
  _pendingPin     = null; _selfScrollAt = null;
  _pendingScrollAnchor = null;   // callers re-arm it after the reset
  _pendingThumbScroll  = null;   // ditto
  _fitWidestPts = 0; _autoFitZoom = null;   // the fit belongs to the old document
  pageW = 0; pageH = 0; renderScale = 1.5;
  searchMatches   = []; searchFlatList = []; searchCursor = -1; searchQuery = '';
  allAnnotations  = {}; pageAnnotations = []; _annotHighlight = null;
  // Bookmarks belong to the document — clear them and revert the left panel to
  // Pages view so a newly opened/switched document never shows stale bookmarks.
  _bookmarks = [];
  _bmSelected.clear(); _bmAnchorId = null;
  _editableAnnots = {};
  _allLinks = {};
  // The Pointer's runs belong to the pages being thrown away.
  if (typeof _etInvalidate === 'function') _etInvalidate(null, false);
  if (typeof setLeftMode === 'function') setLeftMode('pages');
  _pageBlocks.clear();
  // Queued indices point into the OLD allPages — drop them with the blocks they
  // belonged to, or they would render pages of the previous document.
  _clearRenderQueue();
  _clearRenderPrefetch();   // and any opening-page render started for that document
  const col = document.getElementById('canvas-column');
  if (col) col.innerHTML = '';
  // Wipe the thumbnail pane too, its scroll position included.  Replacing the
  // list's innerHTML does NOT move scrollTop — the emptied list is never laid
  // out, so nothing clamps it — which means a new document would otherwise
  // open with the previous document's pane position: the thumbnails sitting on
  // some page of the file you were reading before, while the viewer shows
  // page 1.
  const tl = document.getElementById('thumb-list');
  if (tl) { tl.innerHTML = ''; tl.scrollTop = 0; }
  _clearFormOverlays();
  _updateLockButton(false);
  if (_renderObserver) { _renderObserver.disconnect(); _renderObserver = null; }
  const si = document.getElementById('search-input');
  if (si) si.value = '';
  const sinfo = document.getElementById('search-info');
  if (sinfo) { sinfo.textContent = ''; sinfo.classList.remove('searching'); }
  const csb = document.getElementById('clear-search-btn');
  if (csb) csb.style.display = 'none';
}

/** Put the armed tool away when the active document changes.  A tool belongs
 *  to the page it was armed on: carrying it into another tab left, e.g., Edit
 *  text armed over a document whose runs it had never loaded.  A shape still
 *  being drawn is committed first — to the document it was drawn on, which is
 *  why every caller runs this BEFORE SESSION_ID moves — or dropped when that
 *  document is being closed anyway. */
function _putToolAway(keepDraft = true) {
  if (typeof _draft !== 'undefined' && _draft) {
    if (keepDraft) commitDraft(); else _removeDraft();
  }
  if (typeof _activeAnnotTool !== 'undefined' && _activeAnnotTool) _disarmTool();
}

async function _loadActiveTab() {
  showLoading('Loading…');
  await loadPageList().then(() => Promise.all([loadAllAnnotations(), loadFormFields(),
                                               loadAllLinks()]));
}

async function _switchTab(idx) {
  if (idx < 0 || idx >= _tabs.length) return;
  // Re-clicking the already-active tab is a no-op — stay in whatever panel
  // (Viewer / Layout) it is currently showing.
  if (idx === _activeTabIdx) return;
  _putToolAway();
  _saveActiveTabMeta();
  _activeTabIdx = idx;
  const tab     = _tabs[idx];
  SESSION_ID    = tab.session_id;
  currentPageId = tab.page_id || '';
  zoomLevel     = tab.zoomLevel || 1.0;
  _fitWidthPending = !tab.zoomLevel;   // no stored zoom yet → open fitted
  _restoreTabDirty(tab);
  const zl = document.getElementById('zoom-label');
  if (zl) zl.textContent = Math.round(zoomLevel * 100) + '%';
  _resetViewerState();
  _pendingScrollAnchor = tab.scroll || null;   // where this tab was left scrolled
  _pendingThumbScroll  = (typeof tab.thumbScroll === 'number') ? tab.thumbScroll : null;
  _renderTabBar();
  await _loadActiveTab();
  // Restore the panel (Viewer/Editor) this tab was last showing.
  _restoreTabPanel(tab);
}

async function _closeTab(idx, skipCleanup) {
  if (_tabs.length <= 1) { return; /* SPA: stay in viewer */ }
  const wasActive = (idx === _activeTabIdx);
  // Closing a tab throws its session away, and nothing else asks about it —
  // beforeunload only fires for the whole window.  skipCleanup means the
  // session lives on (detach / pop-out), so there is nothing to lose there.
  if (!skipCleanup) {
    // The active tab's state is the live doc-dirty flag; a background tab has
    // only the flag parked against it when it was left.  (Every edit commits
    // immediately, so there is no un-applied work to ask about separately.)
    const dirty = wasActive
      ? (typeof pwIsDocDirty === 'function' && pwIsDocDirty())
      : _tabs[idx].dirty === true;
    if (dirty) {
      const name = _tabs[idx].label || 'This document';
      const ok = await showConfirm(
        window.t('confirm.close_tab_unsaved_msg', {file: name}),
        window.t('confirm.close_tab_unsaved_title'),
        window.t('confirm.close_without_saving'), 'btn-danger');
      if (!ok) return;
    }
  }
  // The document in view is going away: nothing drawn on it is worth keeping.
  if (wasActive) _putToolAway(false);
  const closedSessionId = _tabs[idx].session_id;
  _tabs.splice(idx, 1);
  if (_activeTabIdx > idx)        _activeTabIdx--;
  if (_activeTabIdx >= _tabs.length) _activeTabIdx = _tabs.length - 1;
  if (wasActive) {
    const tab     = _tabs[_activeTabIdx];
    SESSION_ID    = tab.session_id;
    currentPageId = tab.page_id || '';
    zoomLevel     = tab.zoomLevel || 1.0;
    _fitWidthPending = !tab.zoomLevel;   // no stored zoom yet → open fitted
    _restoreTabDirty(tab);
    const zl = document.getElementById('zoom-label');
    if (zl) zl.textContent = Math.round(zoomLevel * 100) + '%';
    _resetViewerState();
    _pendingScrollAnchor = tab.scroll || null;   // where the promoted tab was left
    _pendingThumbScroll  = (typeof tab.thumbScroll === 'number') ? tab.thumbScroll : null;
    _renderTabBar();
    await _loadActiveTab();
    // Closing the active tab promotes a neighbour — show its own mode.
    _restoreTabPanel(tab);
  } else {
    _renderTabBar();
  }
  if (!skipCleanup) {
    // Free the backend session so its page bytes/thumbnails/undo history are
    // released.  Must go through apiFetch (not navigator.sendBeacon): the API is
    // token- and origin-guarded, and sendBeacon cannot set the X-App-Token
    // header, so a beacon is rejected (403) and the session would leak forever.
    // keepalive lets the request complete even if the tab teardown races it.
    try {
      apiFetch('/api/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: closedSessionId }),
        keepalive: true
      });
    } catch (_) { /* best-effort cleanup */ }
  }
}

// Tear a tab off into its own native window at (screenX, screenY) — the drop
// point, in CSS px, which equals the logical px create_window expects (spike
// confirmed).  Session bytes don't move: the session lives on in the backend,
// so the new window just opens the same session_id and this tab is dropped.
function _detachTab(idx, screenX, screenY) {
  const tab = _tabs[idx];
  if (!tab || !tab.session_id) return;
  // Mark the popout so its (fresh, per-window) sessionStorage doesn't cause the
  // launch-time crash-recovery prompt to re-fire — a detached window hosts an
  // already-live session, not a post-crash relaunch.  See _checkCrashRecovery.
  const url = location.origin + '/viewer?session_id=' + encodeURIComponent(tab.session_id) + '&detached=1';
  if (window.pywebview && window.pywebview.api && window.pywebview.api.popout) {
    window.pywebview.api.popout(url, Math.round(screenX), Math.round(screenY));
  } else {
    window.open(url, '_blank',
      'width=1100,height=850,menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=yes');
  }
  _closeTab(idx, true);   // session lives on in the new window
}

function openNewTab(session_id, page_id) {
  _putToolAway();
  _saveActiveTabMeta();
  // If the only open tab is the blank "Document" placeholder (the empty state
  // shown when the app is launched without a file), replace it instead of
  // appending — otherwise a file opened via double-click lands in tab 2 behind
  // an empty tab 1.  The placeholder is the boot tab with no session_id.
  if (_tabs.length === 1 && !_tabs[0].session_id) {
    _tabs[0] = {session_id, page_id, label: 'Document', zoomLevel: null,
                mode: 'viewer', scroll: null, thumbScroll: null};
    _activeTabIdx = 0;
  } else {
    _tabs.push({session_id, page_id, label: 'Document', zoomLevel: null,
                mode: 'viewer', scroll: null, thumbScroll: null});
    _activeTabIdx = _tabs.length - 1;
  }
  SESSION_ID    = session_id;
  currentPageId = page_id;
  zoomLevel     = 1.0;
  _fitWidthPending = true;   // new documents open fitted to width
  if (typeof pwSetDocDirty === 'function') pwSetDocDirty(false);   // nothing edited yet
  const zl = document.getElementById('zoom-label');
  if (zl) zl.textContent = '100%';
  _resetViewerState();
  _renderTabBar();
  // A new tab always opens in the Viewer — make that explicit here so callers
  // don't need to switch panels first (switching before this runs would cause
  // _saveActiveTabMeta above to mis-record the outgoing tab's mode).
  if (typeof _setActivePanel === 'function') _setActivePanel('panel-viewer');
  _loadActiveTab();
}

// ── Tab drag: reorder (Phase 1), tear-off & re-join (Phases 2–3) ─────────────
// Tabs are HTCLIENT in the native chrome, so JS owns the whole gesture. The spike
// (_spike/spike_tabtear.py) confirmed WebView2 keeps delivering captured
// pointermove with usable screenX/Y even when the cursor leaves the window, and
// that screen coords are CSS px (== logical px) — the currency we work in here.
// Capture is taken on the tab's CONTAINER (not the tab element) so the gesture
// survives the innerHTML re-render a live reorder performs.
const _TAB_DRAG_THRESH = 5;   // px of movement before a press becomes a drag
let _tabDrag = null;

function _tabContainerOf(el) {
  return el.closest('#tb-tabs, #tab-bar');
}

function _onTabPointerDown(e) {
  if (e.button !== 0) return;                       // left button only
  const tabEl = e.target.closest('.v-tab');
  if (!tabEl) return;
  if (e.target.closest('.v-tab-close')) return;     // let close-button clicks through
  const cont = _tabContainerOf(tabEl);
  if (!cont) return;
  const idx = [...cont.querySelectorAll('.v-tab')].indexOf(tabEl);
  if (idx < 0) return;
  const r = tabEl.getBoundingClientRect();
  _tabDrag = {
    cont, idx, startIdx: idx, pointerId: e.pointerId,
    startSX: e.screenX, startSY: e.screenY,
    grabDX: e.clientX - r.left, grabDY: e.clientY - r.top,
    grabClientX: e.clientX, grabClientY: e.clientY,   // cursor's offset in the window
    soleFollow: (_tabs.length === 1 && isNativeApp()), // sole tab → window follows cursor
    started: false, ghost: null,
  };
  try { cont.setPointerCapture(e.pointerId); } catch (_) {}
  cont.addEventListener('pointermove', _onTabPointerMove);
  cont.addEventListener('pointerup', _onTabPointerUp);
  cont.addEventListener('pointercancel', _onTabPointerUp);
  cont.addEventListener('lostpointercapture', _onTabPointerUp);
  e.preventDefault();                               // suppress text selection / native drag
}

function _beginTabDrag() {
  _tabDrag.started = true;
  document.body.classList.add('tab-dragging');
  const cur = _tabDrag.cont.querySelectorAll('.v-tab')[_tabDrag.idx];
  if (cur) cur.classList.add('dragging');
  // A single-tab window follows the cursor itself (Edge-style), so no ghost.
  if (_tabDrag.soleFollow) return;
  const ghost = document.createElement('div');
  ghost.className   = 'tab-drag-ghost';
  ghost.textContent = _tabs[_tabDrag.idx] ? _tabs[_tabDrag.idx].label : 'Document';
  document.body.appendChild(ghost);
  _tabDrag.ghost = ghost;
}

function _onTabPointerMove(e) {
  if (!_tabDrag || e.pointerId !== _tabDrag.pointerId) return;
  if (!_tabDrag.started) {
    if (Math.hypot(e.screenX - _tabDrag.startSX, e.screenY - _tabDrag.startSY) < _TAB_DRAG_THRESH) return;
    _beginTabDrag();
  }
  if (_tabDrag.soleFollow) {
    _followWindowThrottled();          // backend tracks the live cursor
  } else {
    if (_tabDrag.ghost) {
      _tabDrag.ghost.style.left = (e.clientX - _tabDrag.grabDX) + 'px';
      _tabDrag.ghost.style.top  = (e.clientY - _tabDrag.grabDY) + 'px';
    }
    // Reorder within the bar while the cursor is over (or near) it.
    const cr = _tabDrag.cont.getBoundingClientRect();
    const overBar = e.clientX >= cr.left - 30 && e.clientX <= cr.right + 30 &&
                    e.clientY >= cr.top - 12 && e.clientY <= cr.bottom + 24;
    if (overBar) {
      const to = _insertIndexAt(_tabDrag.cont, e.clientX, _tabDrag.idx);
      if (to !== _tabDrag.idx) _moveTabLive(_tabDrag.idx, to);
    }
  }
  // Cross-window dock indicator (throttled; the backend reads the live cursor).
  if (isNativeApp()) _dockHoverThrottled();
}

// Throttle the backend hover bridge call to ~one per frame; it reads the live
// cursor position itself and toggles the drop indicator on whichever window's
// tab bar the cursor is over.
let _dockHoverBusy = false;
function _dockHoverThrottled() {
  if (_dockHoverBusy) return;
  _dockHoverBusy = true;
  setTimeout(() => {
    _dockHoverBusy = false;
    if (!_tabDrag || !_tabDrag.started) return;
    try { window.pywebview.api.tab_drag_hover(); } catch (_) {}
  }, 55);
}

// Single-tab window-follow, throttled to ~one SetWindowPos per frame.  We pass
// only the constant grab offset; the backend reads the live cursor itself, which
// stays accurate across monitors with different DPI.
let _followBusy = false;
function _followWindowThrottled() {
  if (_followBusy) return;
  _followBusy = true;
  setTimeout(() => {
    _followBusy = false;
    if (!_tabDrag || !_tabDrag.started || !_tabDrag.soleFollow) return;
    try {
      window.pywebview.api.move_window_to_cursor(_tabDrag.grabClientX, _tabDrag.grabClientY);
    } catch (_) {}
  }, 16);
}

// Insertion slot for the cursor, expressed as a destination index for the
// dragged tab (already corrected for its own removal from the list).
function _insertIndexAt(cont, clientX, draggingIdx) {
  const els = [...cont.querySelectorAll('.v-tab')];
  let idx = els.length;
  for (let i = 0; i < els.length; i++) {
    const r = els[i].getBoundingClientRect();
    if (clientX < r.left + r.width / 2) { idx = i; break; }
  }
  if (idx > draggingIdx) idx--;
  return Math.max(0, Math.min(_tabs.length - 1, idx));
}

function _moveTabLive(from, to) {
  if (from === to) return;
  const active = _tabs[_activeTabIdx];              // follow the active tab by identity
  const [moved] = _tabs.splice(from, 1);
  _tabs.splice(to, 0, moved);
  _activeTabIdx = _tabs.indexOf(active);
  _tabDrag.idx  = to;
  _renderTabBar();                                  // rebuilds innerHTML; capture (on cont) survives
  const cur = _tabDrag.cont.querySelectorAll('.v-tab')[to];
  if (cur) cur.classList.add('dragging');
}

// Detach intent: the tab was pulled clearly off the bar — below the titlebar
// strip by a margin, or the cursor left the window entirely.  (Dragging within
// the strip is a reorder, handled live; dragging right onto the spacer is not a
// detach.)
function _shouldDetach(e) {
  const cr = _tabDrag.cont.getBoundingClientRect();
  const M = 28;
  return e.clientY > cr.bottom + M ||
         e.clientX < -10 || e.clientY < -10 ||
         e.clientX > window.innerWidth + 10 || e.clientY > window.innerHeight + 10;
}

async function _onTabPointerUp(e) {
  if (!_tabDrag || e.pointerId !== _tabDrag.pointerId) return;
  const d = _tabDrag;
  // Decide BEFORE teardown (which reads/clears _tabDrag).  Tearing off a sole
  // tab is pointless (it's already its own window), so require >1 tab.
  const wasStarted = d.started;
  const detach = wasStarted && _tabs.length > 1 && isNativeApp() && _shouldDetach(e);
  const dropX = e.screenX - d.grabDX, dropY = e.screenY - d.grabDY;
  // Tear down (clears _tabDrag) BEFORE any await, so the trailing
  // lostpointercapture event can't re-enter this handler and double-fire.
  _endTabDrag();   // also clears the drop indicator

  // Probe (without committing) whether the cursor is over another window's bar,
  // so we know if this gesture is about to MOVE the tab.
  let dockable = false;
  if (wasStarted && isNativeApp()) {
    try { dockable = !!(await window.pywebview.api.tab_dock_target()); } catch (_) {}
  }

  // Dock takes priority: drop the tab onto the window under the cursor.
  if (dockable) {
    const tab = _tabs[d.idx];
    if (tab && tab.session_id) {
      try {
        const res = await window.pywebview.api.tab_drop(tab.session_id, tab.page_id || '');
        if (res && res.docked) { _onTabDocked(d.idx); return; }
      } catch (_) {}
    }
  }
  if (detach) { _detachTab(d.idx, dropX, dropY); return; }
  // A press without a drag is a click → switch to that tab.
  if (!wasStarted) _switchTab(d.startIdx);
}

// The dragged tab was docked into another window — drop it from this one (its
// session lives on in the target).  If it was the only tab, close this window.
function _onTabDocked(idx) {
  if (_tabs.length <= 1) {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.close)
      window.pywebview.api.close();
    return;
  }
  _closeTab(idx, true);
}

function _endTabDrag() {
  const d = _tabDrag;
  _tabDrag = null;
  if (!d) return;
  try { d.cont.releasePointerCapture(d.pointerId); } catch (_) {}
  d.cont.removeEventListener('pointermove', _onTabPointerMove);
  d.cont.removeEventListener('pointerup', _onTabPointerUp);
  d.cont.removeEventListener('pointercancel', _onTabPointerUp);
  d.cont.removeEventListener('lostpointercapture', _onTabPointerUp);
  if (d.ghost) d.ghost.remove();
  document.body.classList.remove('tab-dragging');
  document.querySelectorAll('.v-tab.dragging').forEach(el => el.classList.remove('dragging'));
  // Clear any cross-window drop indicator the hover put up.
  if (isNativeApp()) { try { window.pywebview.api.tab_drag_end(); } catch (_) {} }
}

// Target-side: the backend calls this (via evaluate_js) on the window whose tab
// bar a tab from ANOTHER window is hovering over, to show a "drop here" cue.
window.showTabDock = function (on) {
  document.body.classList.toggle('tab-dock-target', !!on);
};

// Delegated pointerdown so it works for both the titlebar bar (#tb-tabs) and the
// in-panel bar (#tab-bar), no matter when their tabs are (re)rendered.
document.addEventListener('pointerdown', _onTabPointerDown);

// ── State ───────────────────────────────────────────────────────────────────
let allPages   = [];   // [{id, filename, page_num, total_pages, thumbnail}]
let currentIdx = 0;    // index of the currently visible page

// Navigation state for thumbnail/page jumps.  Far jumps land instantly (no
// animated sweep that would render every intervening page); _navGen lets a
// newer jump supersede an older one's re-pin, and _pendingPin keeps the target
// pinned to the top as the surrounding placeholders settle to real heights.
let _navGen              = 0;
let _pendingPin          = null;   // { pageId, idx, gen, dy, dx }
// The scroll position WE last wrote, so a scroll event can be told apart from
// the user's.  A one-shot "ignore the next event" flag isn't enough: rebuilding
// the page column clamps scrollTop, and that event lands after the pin is set —
// comparing positions makes any event that merely reports where we put the
// viewport harmless, while a real user scroll still cancels the pin.
let _selfScrollAt        = null;   // { top, left } | null
// Jumps farther than this animate smoothly; anything beyond jumps instantly.
const SMOOTH_JUMP_MAX = 3;
// currentPageId is declared above from URL params
// Current-page render info — updated when the visible page changes
// (pageW, pageH, renderScale, zoomLevel declared in shared JS)
const ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0];

// Upper guard on the per-page render scale requested on zoom-in.  Must match the
// server's _MAX_RENDER_SCALE so the client never asks for more than pdfium will
// produce (and never spins on a request it thinks didn't sharpen enough).
const _MAX_RENDER_SCALE_CLIENT = 8.0;
const PRERENDER_MARGIN = 2;  // extra pages to render beyond the visible area

// Search state
let searchMatches   = [];  // [{page_idx, page_id, rects:[{x,y,w,h}]}]
let searchFlatList  = [];  // flat list: [{match_i, rect_i, page_idx, page_id, rect}]
let searchCursor    = -1;  // index into searchFlatList
let searchQuery     = '';

// Bookmarks state (referenced by _resetViewerState, defined above)
let _leftMode          = 'pages';  // 'pages' | 'bookmarks'
let _bookmarks         = [];
let _ctxBookmarkTarget = null;     // {page_id, y} captured at right-click
let _bmSelected        = new Set(); // bookmark ids highlighted for a bulk delete
let _bmAnchorId        = null;      // shift-click range anchor
let _lastSelectedText  = '';       // most recent text selection (title pre-fill)

// Editable-annotation state: page_id → [info] for selection/lift-to-edit/delete
let _editableAnnots    = {};
let _pendingLift       = null;     // {info, page_id} awaiting page re-render to build draft

// Area-select + annotation clipboard (marquee → Ctrl+C → Ctrl+V, cross-page)
let _areaSelection  = null;  // { page_id, items:[info,...] } — live marquee selection
let _annotClipboard = null;  // { items:[add_annotation bodies], bbox:{x1,y1,x2,y2} } PDF pts
let _lastMouse      = null;  // last cursor position (clientX/Y) — paste targeting

// Annotation state
let pageAnnotations = [];  // annotations for the current page (compat reference)
let allAnnotations  = {};  // page_id → [{idx,x,y,w,h,text,author,color,subtype,markup,kind}]
                           // what the Comments list shows; idx is null for markup
let _annotHighlight = null; // { page_id, annIdx }
let _editingAnnot   = null; // { page_id, annot_idx, ann } while popup is open

// ── Left panel toggle ───────────────────────────────────────────────────────
let leftCollapsed = false;
function toggleLeftPanel() {
  leftCollapsed = !leftCollapsed;
  const panel = document.getElementById('left-panel');
  const arrow = document.getElementById('lp-arrow');
  const title = document.getElementById('lp-title-text');
  const btn   = document.getElementById('lp-toggle');
  panel.classList.toggle('collapsed', leftCollapsed);
  // Flip the chevron (the svg holds a single polyline, not a path "d")
  if (arrow) arrow.innerHTML = leftCollapsed
    ? '<polyline points="9 18 15 12 9 6"/>'    // > (expand)
    : '<polyline points="15 18 9 12 15 6"/>';  // < (collapse)
  if (btn) btn.title = leftCollapsed ? 'Expand panel' : 'Collapse panel';
  title.style.display = leftCollapsed ? 'none' : '';
}

// ── Left panel horizontal resize ────────────────────────────────────────────
// Drag the handle on the panel/canvas seam to widen the panel so long bookmark
// titles are readable. Width is stored on :root's --left-w and persisted.
const LEFT_W_MIN = 140, LEFT_W_MAX = 600;
function _setLeftWidth(px) {
  px = Math.max(LEFT_W_MIN, Math.min(LEFT_W_MAX, Math.round(px)));
  document.documentElement.style.setProperty('--left-w', px + 'px');
  return px;
}
function startLeftResize(ev) {
  ev.preventDefault();
  const panel  = document.getElementById('left-panel');
  const handle = document.getElementById('left-resize');
  const startX = ev.clientX;
  const startW = panel.getBoundingClientRect().width;
  document.body.classList.add('left-resizing');
  if (handle) handle.classList.add('dragging');

  function onMove(e) { _setLeftWidth(startW + (e.clientX - startX)); }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.body.classList.remove('left-resizing');
    if (handle) handle.classList.remove('dragging');
    const w = getComputedStyle(document.documentElement)
                .getPropertyValue('--left-w').trim();
    try { localStorage.setItem('pw_left_w', w); } catch (_) {}
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}
// Restore a saved panel width on boot.
(function _restoreLeftWidth() {
  try {
    const saved = parseInt(localStorage.getItem('pw_left_w'), 10);
    if (saved) _setLeftWidth(saved);
  } catch (_) {}
})();

// ── Right panel toggle ──────────────────────────────────────────────────────
let rightCollapsed = true;
function toggleRightPanel() {
  rightCollapsed = !rightCollapsed;
  document.getElementById('right-panel').classList.toggle('collapsed', rightCollapsed);
  // Text editing is gated on the panel being OPEN, not just on the tool being
  // armed — closing the panel is how the user puts the toolset away, and a
  // page click afterwards must go back to selecting text, not rewriting it.
  if (typeof _etActive === 'function') {
    if (_etActive()) _etLoadForCurrentPage();
    else {
      _etCloseEntry(); _etCancelBlock(); _etHideCtx();
      _etClearOverlays(); _etPage = null;
    }
  }
}

// The right panel has three tabs, split by what a tool does to the document:
//   comment — objects on top of the page, listed under the tools; removable
//   edit    — changes the document itself (Undo until you save)
//   sign    — form fields and signatures
// The comments list shows on the Comment tab only.  Every tool belongs to
// exactly one tab (_tabForTool), and arming a tool brings its tab forward, so
// a tool's settings are never armed out of sight.
const _RIGHT_MODES = ['comment', 'edit', 'sign'];
const _TOOL_TAB = {
  sticky: 'comment', text: 'comment', highlight: 'comment', cloud: 'comment',
  rect: 'comment', ellipse: 'comment', line: 'comment', arrow: 'comment',
  darrow: 'comment',
  edittext: 'edit', pagetext: 'edit', image: 'edit', redact: 'edit',
  watermark: 'edit', crop: 'edit',
  formfield: 'sign', signature: 'sign',
};
function _tabForTool(tool) { return _TOOL_TAB[tool] || 'comment'; }

let _rightMode = 'comment';
function setRightMode(mode) {
  // Callers from before v3.2 ask for the old two views: 'annots' was the list
  // (now on the Comment tab); 'tools' just meant "not the list".
  if (mode === 'annots') mode = 'comment';
  if (_RIGHT_MODES.indexOf(mode) === -1) return;
  _rightMode = mode;
  // A tool — or an annotation being edited — that lives on another tab would
  // otherwise stay armed with its settings hidden.  Put it away.
  const held = _activeAnnotTool || (_draft && _draft.tool);
  if (held && _tabForTool(held) !== mode) {
    commitDraft();
    if (_activeAnnotTool) _disarmTool();
  }
  _RIGHT_MODES.forEach(m => {
    const grp = document.getElementById('rp-group-' + m);
    if (grp) grp.style.display = (m === mode) ? '' : 'none';
    const btn = document.getElementById('rp-seg-' + m);
    if (btn) btn.classList.toggle('active', m === mode);
  });
  const list = document.getElementById('rp-comments');
  if (list) list.style.display = (mode === 'comment') ? '' : 'none';
  _refreshStyleRows();
}

// ── Load page list ──────────────────────────────────────────────────────────
async function loadPageList() {
  try {
    const res  = await _vFetch('/api/viewer/pages', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session_id: SESSION_ID})
    });
    const data = await res.json();
    allPages = data.pages || [];

    // Sync lock-button state with server
    _updateLockButton(!!data.is_protected);

    // Sync read-only state: the file opens freely but forbids editing
    _applyRestrictedMode(!!data.restricted);

    // File needs a password before it can be shown
    if (data.needs_password) {
      const filename = (SESSION_ID && sessions) ? 'this document' : 'document';
      _showOpenPasswordDialog(
        data.filename || 'this document',
        null, true  // isSession = true (preloaded file)
      );
      hideLoading();
      return;
    }

    if (_tabs[_activeTabIdx] && allPages.length) {
      _tabs[_activeTabIdx].label = allPages[0].filename;
      _renderTabBar();
    }
    const startIdx = allPages.findIndex(p => p.id === currentPageId);
    currentIdx = startIdx >= 0 ? startIdx : 0;
    if (allPages.length && !currentPageId) currentPageId = allPages[0].id;
    // Ask the server for the opening page's bitmap NOW, before the thumbnail
    // rail and the placeholder column are built, so the round-trip overlaps
    // that layout work instead of queueing behind it. Nothing is applied here
    // — see _prefetchRender.
    _prefetchRender(currentPageId);
    // Build the thumbnails after currentIdx settles, so the list marks the page
    // we are actually opening on rather than the one the previous state left in
    // the variable.
    const hadThumbPos = (typeof _pendingThumbScroll === 'number');
    renderThumbList();
    // Nothing remembered for the pane, but the document opens part-way in (a
    // session restored on a page, or a reload after pages were reordered):
    // show that page's thumb instead of a top-of-list matching nothing.
    if (!hadThumbPos && currentIdx > 0) _scrollThumbIntoView(currentIdx);
    // Build the continuous-scroll column of page blocks
    await buildPageColumn();
    // Position the viewport (instant, no animation on first load)
    _restoreScrollAnchor();
    hideLoading();
    _updateViewerEmptyState();
  } catch(e) {
    if (e.name === 'AbortError') return;  // tab switched — request cancelled, not an error
    showToast(window.t('toast.failed_to_load_pages') + e.message, 'error');
    hideLoading();
    _updateViewerEmptyState();
  }
}

// ── Thumbnail list ──────────────────────────────────────────────────────────
function renderThumbList() {
  const list = document.getElementById('thumb-list');
  if (!list) return;
  // A plain re-render (one page's thumbnail changed) keeps the pane where the
  // user left it; a tab switch overrides that with the position parked for the
  // tab being restored.
  const want = (typeof _pendingThumbScroll === 'number') ? _pendingThumbScroll
                                                         : list.scrollTop;
  _pendingThumbScroll = null;
  list.innerHTML = allPages.map((p, i) => `
    <div class="thumb-item${i === currentIdx ? ' active' : ''}"
         id="thumb-${p.id}" onclick="goToPage(${i})"
         oncontextmenu="showPageCtx(event, '${p.id}')">
      <img src="data:image/png;base64,${p.thumbnail}" alt="Page ${p.page_num}"
           loading="lazy"${_thumbBoxStyle(p)} onload="this.style.aspectRatio=''">
      <div class="thumb-label">p.${p.page_num}</div>
    </div>
  `).join('');
  _setThumbScroll(list, want);
}

// Reserve each thumbnail's box before its image decodes.  Without it every item
// is label-height until the (lazily decoded) image arrives, the pane is a
// fraction of its eventual height, and a scroll position set on it is clamped
// to that stub height and left there once the images grow the list.
// The ratio is an estimate — it comes from the MediaBox and ignores /Rotate —
// so the decoded image takes over via onload.
function _thumbBoxStyle(p) {
  const w = +p.width_pts, h = +p.height_pts;
  return (w > 0 && h > 0) ? ` style="aspect-ratio:${w}/${h}"` : '';
}

function _setThumbScroll(list, top) {
  list.scrollTop = top;
  if (!top) return;
  // Belt and braces for the first paint after a rebuild: if the pane is still
  // shorter than the target the browser clamped us, so try again once layout
  // has caught up.  This only ever moves *down* to the target, so a user scroll
  // in the meantime is left alone.
  requestAnimationFrame(() => { if (list.scrollTop < top - 1) list.scrollTop = top; });
}

function _scrollThumbIntoView(idx, smooth) {
  const el = document.getElementById('thumb-' + (allPages[idx]?.id || ''));
  if (el) el.scrollIntoView({block: 'nearest', behavior: smooth ? 'smooth' : 'auto'});
}

function updateThumbActive() {
  document.querySelectorAll('.thumb-item').forEach((el, i) => {
    el.classList.toggle('active', i === currentIdx);
  });
  _scrollThumbIntoView(currentIdx, true);   // keep the active thumb in view
}

function markThumbsWithMatches() {
  // Clear all match indicators
  document.querySelectorAll('.thumb-item').forEach(el => el.classList.remove('has-match'));
  // Set indicators for pages with matches
  const matchedPages = new Set(searchMatches.map(m => m.page_idx));
  matchedPages.forEach(idx => {
    const id = allPages[idx]?.id;
    if (id) document.getElementById('thumb-' + id)?.classList.add('has-match');
  });
}

// ── Page column — build multi-page DOM ──────────────────────────────────────
async function buildPageColumn() {
  const col = document.getElementById('canvas-column');
  col.innerHTML = '';
  _pageBlocks.clear();
  _clearRenderQueue();

  // Measure thumbnail aspect ratios to pre-size placeholder blocks,
  // minimising layout shift as real renders arrive.
  const thumbAspects = await Promise.all(allPages.map(p => new Promise(resolve => {
    const img = new Image();
    img.onload  = () => resolve(img.naturalHeight / Math.max(1, img.naturalWidth));
    img.onerror = () => resolve(11 / 8.5);  // fallback: letter portrait
    img.src = 'data:image/png;base64,' + p.thumbnail;
  })));

  const area = document.getElementById('canvas-area');
  // Placeholder width tracks the layout: full width in single, ~half (less the
  // grid gap) per column in two-up — so an unrendered placeholder never blows
  // out a grid column before its real (fitted) render arrives.
  const _cols = (_pageLayout === 'two-up') ? 2 : 1;
  const colW  = Math.max(300, ((area ? area.clientWidth : 800) - 48 - (_cols - 1) * 20) / _cols);

  allPages.forEach((p, i) => {
    const block = document.createElement('div');
    block.className      = 'page-block';
    block.id             = 'page-block-' + p.id;
    block.dataset.idx    = i;
    block.dataset.pageId = p.id;

    const ph = document.createElement('div');
    ph.className    = 'page-placeholder';
    ph.style.width  = colW + 'px';
    ph.style.height = Math.round(colW * thumbAspects[i]) + 'px';
    block.appendChild(ph);
    col.appendChild(block);

    if (i < allPages.length - 1) {
      const gap = document.createElement('div');
      gap.className = 'page-gap';
      col.appendChild(gap);
    }

    _pageBlocks.set(p.id, {
      block, placeholder: ph,
      canvas: null, hlOv: null, annotOv: null, selOv: null, linkOv: null,
      image: null, pageW: 0, pageH: 0, renderScale: 1.5,
      rendered: false, rendering: false,
    });
  });

  _setupLazyRender();
  _setupScrollTracking();
  updatePageNav();
}

// ── Lazy rendering (IntersectionObserver) ────────────────────────────────────
function _setupLazyRender() {
  if (_renderObserver) _renderObserver.disconnect();
  _renderObserver = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      _triggerRenderAround(parseInt(entry.target.dataset.idx, 10));
    });
  }, {
    root:       document.getElementById('canvas-area'),
    rootMargin: '400px 0px',
    threshold:  0,
  });
  allPages.forEach(p => {
    const block = document.getElementById('page-block-' + p.id);
    if (block) _renderObserver.observe(block);
  });
}

function _triggerRenderAround(centreIdx) {
  const lo = Math.max(0, centreIdx - PRERENDER_MARGIN);
  const hi = Math.min(allPages.length - 1, centreIdx + PRERENDER_MARGIN);
  for (let i = lo; i <= hi; i++) {
    const st = _pageBlocks.get(allPages[i].id);
    if (!st) continue;
    if (!st.rendered && !st.rendering) renderPageBlock(i);
    // Coming into view after a live zoom skipped it — draw it properly now,
    // before the CSS-stretched stand-in can be seen.
    else if (st.needsRedraw) redrawPageBlock(allPages[i].id);
  }
}

// ── Base-render queue ────────────────────────────────────────────────────────
// Every page that scrolls past fires _triggerRenderAround, and each render is a
// POST the server answers under a process-wide native lock (all pdfium work is
// serialised — see routes/viewer.py).  Firing them the moment they are asked
// for meant a fast flick through a long document queued a render for every page
// crossed: the pages the user actually STOPPED on sat behind a hundred pages
// already scrolled past, so the viewport stayed blank for seconds, and the
// request burst also blew through the server's rate limit.
//
// So renders go through a queue with a small concurrency window, and each entry
// is re-checked against the CURRENT viewport at dispatch time.  A page the user
// has already scrolled away from is dropped instead of rendered — the work is
// only worth doing for where they are now.
const RENDER_CONCURRENCY = 3;
let _renderQueue   = [];      // [{idx, at}] waiting for a base render
let _renderActive  = 0;       // renders currently in flight
let _renderDropped = false;   // something was skipped as stale — sweep on drain

// ── Opening-page render prefetch ─────────────────────────────────────────────
// The base-render POST for the page a document opens on, started the moment the
// page list lands and parked here until the normal render path asks for it.
//
// The wait it removes is dead time, not work: the lazy observer is the only
// thing that ever starts a render, and it cannot report a block visible until
// the browser has laid out and composited the whole placeholder column and
// decoded the thumbnail rail — both O(pages), both on the main thread. The
// server answers a base render in tens of milliseconds and spent all of that
// idle, waiting to be asked.
//
// Why a prefetch rather than simply rendering the opening page early: the FIRST
// render to arrive is what sets the opening fit-to-width zoom, and every block
// that swaps a placeholder for a real canvas moves the anchor _pendingPin is
// restoring. Rendering early reorders both, and both are pinned down by
// regression tests for field bugs (viewer_fit_width's mislisted landscape page,
// viewer_tab_scroll's per-tab positions). So only the round-trip moves:
// _renderPageBlockNow still builds the DOM at exactly the same point in exactly
// the same order, it just finds the answer already waiting.
let _renderPrefetch = new Map();   // page_id → Promise<render JSON | null>

// Not folded into _clearRenderQueue: that runs from buildPageColumn, which
// happens AFTER the prefetch is started and would throw it away.
function _clearRenderPrefetch() { _renderPrefetch = new Map(); }

// Start the base render for `page_id` now. Never rejects — a failure, or a tab
// switch aborting it, resolves to null and the consumer falls back to its own
// fetch. The prefetch can save time; it can never change the outcome.
function _prefetchRender(page_id) {
  if (!SESSION_ID || !page_id || _renderPrefetch.has(page_id)) return;
  _renderPrefetch.set(page_id, _vFetch('/api/viewer/render', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: SESSION_ID, page_id })
  })
  .then(r => r.json())
  .then(d => (d && !d.error) ? d : null)
  .catch(() => null));
}

// One-shot: hand the pending prefetch to the first render that asks, so a
// re-render (zoom, annotation commit) always goes back to the server.
function _takePrefetchedRender(page_id) {
  const pending = _renderPrefetch.get(page_id);
  if (pending === undefined) return null;
  _renderPrefetch.delete(page_id);
  return pending;
}

function _clearRenderQueue() {
  _renderQueue   = [];
  _renderActive  = 0;
  _renderDropped = false;
}

function _scrollTopNow() {
  const area = document.getElementById('canvas-area');
  return area ? area.scrollTop : 0;
}

// Is this page still worth rendering, or has the user moved on?
//
// `queuedAt` is the scroll position when the render was asked for.  If the
// viewport has not moved since, nothing has been scrolled past and whoever
// queued it still wants it — that keeps explicit, non-observer callers (and a
// stationary user) working exactly as before.  Only once the view HAS moved does
// distance decide, and the band kept has to be at least what the lazy observer
// legitimately warms: the viewport plus PRERENDER_MARGIN pages either side, or
// pre-rendering ahead of the scroll would be undone as fast as it is requested.
//
// Placeholder blocks are pre-sized from the thumbnails, so this geometry is
// meaningful before the real render lands.
function _renderStillWanted(idx, queuedAt) {
  const p = allPages[idx];
  if (!p) return false;
  if (p.id === currentPageId) return true;      // never drop the page in view
  const st   = _pageBlocks.get(p.id);
  const area = document.getElementById('canvas-area');
  if (!st || !st.block || !area) return true;   // can't tell — let it through
  if (queuedAt !== undefined && area.scrollTop === queuedAt) return true;
  const o = st.block.offsetTop, h = st.block.offsetHeight;
  const margin = Math.max(600, area.clientHeight) + (PRERENDER_MARGIN + 1) * h;
  const top = area.scrollTop - margin;
  const bot = area.scrollTop + area.clientHeight + margin;
  return (o + h >= top && o <= bot);
}

// Re-offer anything near the viewport that a stale-drop skipped, so a page can
// never be left blank just because the user scrolled through it and back.
function _sweepVisibleRenders() {
  for (let i = 0; i < allPages.length; i++) {
    const st = _pageBlocks.get(allPages[i].id);
    if (!st || st.rendered || st.rendering) continue;
    if (_renderStillWanted(i)) renderPageBlock(i);
  }
}

function _pumpRenderQueue() {
  while (_renderActive < RENDER_CONCURRENCY && _renderQueue.length) {
    const { idx, at } = _renderQueue.shift();
    const p   = allPages[idx];
    const st  = p && _pageBlocks.get(p.id);
    if (!st) continue;                          // page went away (tab switch)
    if (st.rendered) { st.rendering = false; continue; }
    if (!_renderStillWanted(idx, at)) {
      st.rendering   = false;                   // free it to be asked for again
      _renderDropped = true;
      continue;
    }
    _renderActive++;
    _renderPageBlockNow(idx).then(_afterRender, _afterRender);
  }
}

function _afterRender() {
  _renderActive--;
  if (_renderQueue.length) { _pumpRenderQueue(); return; }
  if (_renderActive > 0) return;                // wait for the rest to land
  // Drained after dropping stale work — make sure nothing visible was lost.
  // Guarded by the flag so a page that fails to render can't loop forever.
  if (_renderDropped) { _renderDropped = false; _sweepVisibleRenders(); }
}

// ── Scroll tracking — update the current-page indicator ─────────────────────
function _setupScrollTracking() {
  const area = document.getElementById('canvas-area');
  if (area) area.addEventListener('scroll', _onAreaScroll, { passive: true });
}

function _onAreaScroll() {
  // A scroll that moved the viewport somewhere we didn't put it means the user
  // is taking over — abandon any pending re-pin so we never yank them back.
  if (_pendingPin) {
    const area = document.getElementById('canvas-area');
    const ours = area && _selfScrollAt &&
                 Math.abs(area.scrollTop  - _selfScrollAt.top)  <= 1 &&
                 Math.abs(area.scrollLeft - _selfScrollAt.left) <= 1;
    if (!ours) _pendingPin = null;
  }
  // Sharpen pages that scroll/pan into view while zoomed in (debounced inside).
  _scheduleSharpRender();
  if (_scrollRafId) return;
  _scrollRafId = requestAnimationFrame(() => { _scrollRafId = null; _updateCurrentFromScroll(); });
}

function _updateCurrentFromScroll() {
  const area = document.getElementById('canvas-area');
  if (!area || !allPages.length) return;
  const areaRect = area.getBoundingClientRect();
  const viewRef  = areaRect.top + areaRect.height * 0.35;

  let bestIdx = currentIdx, bestDist = Infinity;
  allPages.forEach((p, i) => {
    const block = document.getElementById('page-block-' + p.id);
    if (!block) return;
    const r = block.getBoundingClientRect();
    const dist = Math.abs(r.top + r.height / 2 - viewRef);
    if (dist < bestDist) { bestDist = dist; bestIdx = i; }
  });

  if (bestIdx !== currentIdx) {
    currentIdx    = bestIdx;
    currentPageId = allPages[bestIdx].id;
    const st = _pageBlocks.get(currentPageId);
    if (st) { pageW = st.pageW; pageH = st.pageH; renderScale = st.renderScale; }
    pageAnnotations = allAnnotations[currentPageId] || [];
    updateThumbActive();
    updatePageNav();
    renderComments();
    // The watermark previews on the page the user is looking at, and that is
    // also what Stamp applies to — so it has to follow the scroll, or the
    // preview and the target drift apart silently.
    if (_activeAnnotTool === 'watermark') wmViewerPreview();
    // The Pointer loads one page's text at a time, so it follows too.
    if (_etActive()) _etLoadForCurrentPage();
  }
}

// ── Render a single page block ───────────────────────────────────────────────
// Public entry point: claim the page and get in line.  `rendering` is set here,
// at QUEUE time, so the same page is never asked for twice while it waits.
function renderPageBlock(idx) {
  if (idx < 0 || idx >= allPages.length) return;
  const p  = allPages[idx];
  const st = _pageBlocks.get(p.id);
  if (!st || st.rendered || st.rendering) return;
  st.rendering = true;
  _renderQueue.push({ idx, at: _scrollTopNow() });
  _pumpRenderQueue();
}

async function _renderPageBlockNow(idx) {
  const p  = allPages[idx];
  const st = _pageBlocks.get(p.id);
  if (!st) return;

  try {
    // The opening page's bitmap was asked for when the page list landed; take
    // it if it is waiting, otherwise ask now. A prefetch that failed or was
    // aborted resolves to null and falls through to the normal request.
    let data = null;
    const prefetched = _takePrefetchedRender(p.id);
    if (prefetched) data = await prefetched;
    if (!data) {
      const res = await _vFetch('/api/viewer/render', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({session_id: SESSION_ID, page_id: p.id})
      });
      data = await res.json();
    }
    if (data.error) throw apiErr(data);

    const img = new Image();
    img.onload = () => {
      st.pageW = data.page_w; st.pageH = data.page_h; st.renderScale = data.scale;
      st.image = img; st.imageScale = data.scale; st.rendered = true; st.rendering = false;
      // First render of a freshly opened document: fit the page to the
      // canvas width before building, so the document opens fully visible.
      if (_fitWidthPending) {
        _fitWidthPending = false;
        _fitWidestPts    = Math.max(st.pageW || 0, _docWidestWidthPts());
        _applyFitZoom(_fitZoomFor(_fitNaturalW(st)));
      } else if (st.pageW > _fitWidestPts + 1 && zoomLevel === _autoFitZoom) {
        // This page turned out wider than anything the opening fit knew about
        // — the page list under-reported it (dimensions recorded before a
        // /Rotate was taken into account, or a session recovered without
        // them).  Re-fit to what we now know, but only while the zoom is still
        // the one we chose: once the user has zoomed, the view is theirs.
        _fitWidestPts = st.pageW;
        _applyFitZoom(_fitZoomFor(_logicalW(st)));
      }
      _buildPageBlockDOM(p.id, img);
      if (p.id === currentPageId) {
        pageW = st.pageW; pageH = st.pageH; renderScale = st.renderScale;
        hideLoading();
        // Final bootstrap milestone — the opening page is now on screen.
        if (!window.__firstPageShown && window.bootMark) {
          window.__firstPageShown = true;
          bootMark('first page rendered & displayed');
        }
      }
      drawPageHighlights(p.id);
      drawPageAnnotations(p.id);
      drawEditableHitAreas(p.id);
      _populateLinkOverlay(p.id, st);
      // A re-render invalidates annot_idx values — drop any area selection on
      // this page rather than risk acting on stale records. (The clipboard is
      // unaffected: it was snapshotted at copy time.)
      if (_areaSelection && _areaSelection.page_id === p.id) _areaSelection = null;
      // A page near a pending jump just gained its real height — re-pin the
      // target so it stays at the top of the viewport.
      // (A held anchor cares about every page above it, not just its neighbours
      // — any of them settling to its real height shifts the anchor down.)
      if (_pendingPin && idx <= _pendingPin.idx
          && (_pendingPin.hold || idx >= _pendingPin.idx - PRERENDER_MARGIN)) _applyPendingPin();
      // If this re-render was triggered by lifting an annotation, build the
      // draft now (the block was just rebuilt, so it's safe to attach to).
      if (_pendingLift && _pendingLift.page_id === p.id) {
        const pl = _pendingLift; _pendingLift = null;
        _buildLiftedDraft(pl.info, pl.page_id);
      }
      // If the page opened already zoomed in, upgrade it to a sharp render.
      _scheduleSharpRender();
    };
    img.src = 'data:image/png;base64,' + data.image;
    // loadAllAnnotations() fetches every page's annotations in ONE call when the
    // document opens and keeps allAnnotations up to date on every edit, so the
    // per-page refetch is only needed when this page is missing from that cache
    // (a render that beat the bulk load).  Skipping it when the answer is
    // already known removes a whole request per page from a fast scroll —
    // img.onload above draws this page's annotations either way.
    if (!allAnnotations[p.id]) loadAnnotations(p.id);
    loadEditableAnnots(p.id);
  } catch(e) {
    st.rendering = false;
    if (e.name === 'AbortError') return;  // tab switched — request cancelled, not an error
    if (p.id === currentPageId) { showToast(window.t('toast.render_failed') + e.message, 'error'); hideLoading(); }
  }
}

function _setSvgSize(svg, w, h) {
  svg.setAttribute('width', w); svg.setAttribute('height', h);
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
}

// Logical (base-scale) pixel dimensions of a page — pageW/H in points × the
// *logical* renderScale that overlay coordinate math is calibrated to.  These
// are independent of the resolution st.image was actually rendered at: a sharp
// zoom-in re-render gives a bigger st.image, but the canvas stays this logical
// size and drawImage downsamples into it.  At base scale these equal the
// image's natural width/height, so existing geometry is unchanged.
function _logicalW(st) { return st.pageW * st.renderScale; }
function _logicalH(st) { return st.pageH * st.renderScale; }

function _buildPageBlockDOM(page_id, img) {
  const st = _pageBlocks.get(page_id);
  if (!st) return;
  const w = Math.round(_logicalW(st) * zoomLevel);
  const h = Math.round(_logicalH(st) * zoomLevel);
  const ns = 'http://www.w3.org/2000/svg';

  const canvas = document.createElement('canvas');
  canvas.className = 'page-canvas';
  canvas.width = w; canvas.height = h;
  canvas.getContext('2d').drawImage(img, 0, 0, w, h);

  const hlOv = document.createElementNS(ns, 'svg');
  hlOv.className.baseVal = 'page-hl-overlay'; _setSvgSize(hlOv, w, h);

  const annotOv = document.createElementNS(ns, 'svg');
  annotOv.className.baseVal = 'page-annot-overlay'; _setSvgSize(annotOv, w, h);

  const selOv = document.createElementNS(ns, 'svg');
  selOv.className.baseVal = 'page-sel-overlay'; _setSvgSize(selOv, w, h);

  // Link overlay: hit-areas over the document's own URL links.  A plain div of
  // absolutely-positioned children (like the form overlay) rather than SVG, so
  // the hover affordance is ordinary CSS :hover and the target URL can ride
  // along as a native `title` tooltip.  Deliberately BELOW the edit overlay and
  // the form widgets: an annotation or a fillable field sitting on top of a link
  // is the thing the click is for.
  const linkOv = document.createElement('div');
  linkOv.className = 'page-link-overlay';
  linkOv.id        = 'link-ov-' + page_id;
  linkOv.style.cssText = `width:${w}px;height:${h}px;`;

  // Edit overlay: transparent hit-areas for selecting/lifting committed
  // annotations. Sits above the others; pointer-events gated to pointer mode.
  const editOv = document.createElementNS(ns, 'svg');
  editOv.className.baseVal = 'page-edit-overlay'; _setSvgSize(editOv, w, h);
  editOv.style.position = 'absolute'; editOv.style.top = '0'; editOv.style.left = '0';
  editOv.style.zIndex = '450';

  const formOv = document.createElement('div');
  formOv.className = 'page-form-overlay';
  formOv.id        = 'form-ov-' + page_id;
  formOv.style.cssText = `width:${w}px;height:${h}px;`;

  st.canvas = canvas; st.hlOv = hlOv; st.annotOv = annotOv; st.selOv = selOv;
  st.editOv = editOv; st.linkOv = linkOv;
  st.block.innerHTML = '';
  st.block.appendChild(canvas);
  st.block.appendChild(hlOv);
  st.block.appendChild(annotOv);
  st.block.appendChild(selOv);
  st.block.appendChild(linkOv);
  st.block.appendChild(editOv);
  st.block.appendChild(formOv);
  // A live annotation draft on this page was just wiped with the old DOM.
  // Re-attach it: committing shape N re-renders the page async, and by the time
  // the fresh raster lands the user is often already drawing shape N+1 — without
  // this, that draft silently vanishes from screen (while staying live in JS)
  // and only shows up when the NEXT draw commits it.
  if (_draft && _draft.block === st.block) {
    if (_draft.svg)       st.block.appendChild(_draft.svg);
    if (_draft.textInput) st.block.appendChild(_draft.textInput);
  }
  _populateFormOverlay(page_id, st);
  _populateLinkOverlay(page_id, st);
  // _buildPageBlockDOM replaces the block's children, so the Pointer's
  // hit-areas go with them and have to be put back.
  if (typeof _etActive === 'function' && _etActive() && _etPage === page_id) {
    _etRenderOverlays();
  }
}

// Layout-only resize: give the block its new on-screen size without touching
// the bitmap.  The canvas keeps its old pixels, stretched by CSS — wrong-looking
// up close, but this is only used for pages that are off-screen during a live
// zoom gesture, where what matters is that the column's height stays right so
// the anchored scroll position lands where it should.  st.needsRedraw brings
// them back to a real redraw before they can be seen.
function _resizePageBlockCss(page_id) {
  const st = _pageBlocks.get(page_id);
  if (!st?.canvas) return;
  const w = Math.round(_logicalW(st) * zoomLevel);
  const h = Math.round(_logicalH(st) * zoomLevel);
  const els = [st.canvas, st.hlOv, st.annotOv, st.selOv, st.editOv,
               document.getElementById('form-ov-' + page_id),
               document.getElementById('link-ov-' + page_id)];
  els.forEach(el => { if (el) { el.style.width = w + 'px'; el.style.height = h + 'px'; } });
  st.needsRedraw = true;
}

function redrawPageBlock(page_id) {
  const st = _pageBlocks.get(page_id);
  if (!st?.image || !st.canvas) return;
  const w = Math.round(_logicalW(st) * zoomLevel);
  const h = Math.round(_logicalH(st) * zoomLevel);
  st.needsRedraw = false;
  // Drop any CSS stand-in size from a live gesture — the real sizes below own it.
  [st.canvas, st.hlOv, st.annotOv, st.selOv, st.editOv].forEach(el => {
    if (el) { el.style.width = ''; el.style.height = ''; }
  });
  st.canvas.width = w; st.canvas.height = h;
  const ctx = st.canvas.getContext('2d');
  // High-quality downsampling when st.image is a sharper-than-needed zoom render.
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(st.image, 0, 0, w, h);
  [st.hlOv, st.annotOv, st.selOv, st.editOv].forEach(ov => { if (ov) _setSvgSize(ov, w, h); });
  const formOv = document.getElementById('form-ov-' + page_id);
  if (formOv) { formOv.style.width = w + 'px'; formOv.style.height = h + 'px'; }
  const linkOv = document.getElementById('link-ov-' + page_id);
  if (linkOv) { linkOv.style.width = w + 'px'; linkOv.style.height = h + 'px'; }
  drawPageHighlights(page_id);
  drawPageAnnotations(page_id);
  drawEditableHitAreas(page_id);
  if (_selectedChars.length && _selectedPageId === page_id) drawSelectionHighlights(_selectedChars, page_id);
  _populateFormOverlay(page_id, st);
  _populateLinkOverlay(page_id, st);
  // Overlays positioned in screen pixels must follow the new zoom too.  The
  // Pointer's hit-areas were left at the old size and place, so after a zoom
  // clicks missed the words until the tool was toggled off and on.
  if (typeof _etActive === 'function' && _etActive() && _etPage === page_id) {
    if (_etEntry) _etCommitEntry();          // an open editor would sit in the wrong place
    _etRenderOverlays();
  }
  if (_activeAnnotTool === 'watermark' && page_id === currentPageId) wmViewerPreview();
}

// deferOffscreen: only the pages on screen are really redrawn; the rest get a
// CSS-only resize and are queued for a proper redraw.  A full redraw of every
// page ever rendered costs tens of milliseconds in a long document — too much
// to spend on every frame of a pinch gesture.
function redrawAllRendered(opts) {
  const defer = !!(opts && opts.deferOffscreen);
  const onScreen = defer ? new Set(_visiblePageBlocks().map(([pid]) => pid)) : null;
  _pageBlocks.forEach((st, page_id) => {
    if (!st.rendered) return;
    if (defer && !onScreen.has(page_id)) _resizePageBlockCss(page_id);
    else redrawPageBlock(page_id);
  });
}

// Redraw pages that a live zoom gesture left CSS-resized.
function _flushDeferredRedraws() {
  _pageBlocks.forEach((st, page_id) => { if (st.needsRedraw) redrawPageBlock(page_id); });
}

// ── Page navigation ─────────────────────────────────────────────────────────
function updatePageNav() {
  document.getElementById('page-nav-label').textContent =
    allPages.length ? `${currentIdx + 1} / ${allPages.length}` : '— / —';
}

// Double-click the "x / y" label to type a page number and jump to it.
function editPageNumber() {
  const total = allPages.length;
  if (!total) return;
  const label = document.getElementById('page-nav-label');
  if (!label || label.querySelector('input')) return;   // already editing

  label.textContent = '';
  const inp = document.createElement('input');
  inp.className = 'page-nav-input';
  inp.type = 'text';
  inp.inputMode = 'numeric';
  inp.value = String(currentIdx + 1);
  const suffix = document.createElement('span');
  suffix.textContent = ' / ' + total;
  label.appendChild(inp);
  label.appendChild(suffix);
  inp.focus();
  inp.select();

  let done = false;
  const finish = (go) => {
    if (done) return;
    done = true;
    let target = null;
    if (go) {
      const n = parseInt(inp.value, 10);
      if (!isNaN(n) && n >= 1 && n <= total) target = n - 1;
    }
    updatePageNav();                 // rebuilds the label (removes the input)
    if (target !== null) goToPage(target);
  };
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter')      { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape'){ e.preventDefault(); finish(false); }
  });
  inp.addEventListener('blur', () => finish(false));
}

function goToPage(idx) {
  if (idx < 0 || idx >= allPages.length) return;
  const p     = allPages[idx];
  const block = document.getElementById('page-block-' + p.id);
  // Every navigation bumps the generation so a stale re-pin (from a previous
  // jump) can't fight this one.
  const gen   = ++_navGen;
  const far   = Math.abs(idx - currentIdx) > SMOOTH_JUMP_MAX;
  if (block) {
    // Far jumps land instantly: animating a smooth scroll across hundreds of
    // pages makes the lazy-render observer fire for every page in between,
    // flooding the single-threaded native render lock and overshooting the
    // target as placeholder heights resolve mid-animation.  Nearby prev/next
    // hops keep the gentle smooth scroll.
    block.scrollIntoView({behavior: far ? 'auto' : 'smooth', block: 'start'});
    // After an instant jump the target sits at a position derived from
    // *estimated* placeholder heights; keep it pinned to the top until the
    // pages above it render to their true heights (or the user scrolls away).
    _pendingPin = far ? { pageId: p.id, idx, gen } : null;
  }
  // Optimistically update current-page state for instant thumb/nav feedback
  currentIdx    = idx;
  currentPageId = p.id;
  const st = _pageBlocks.get(p.id);
  if (st?.rendered) { pageW = st.pageW; pageH = st.pageH; renderScale = st.renderScale; }
  updateThumbActive();
  updatePageNav();
  if (searchQuery) {
    const mi = searchFlatList.findIndex(m => m.page_idx === idx);
    if (mi >= 0) { searchCursor = mi; updateSearchInfo(); _scrollToMatchInPage(searchFlatList[mi]); }
  }
}

// Position the viewport after the page column is (re)built: at the anchor the
// tab was left at when we're switching back to it, otherwise at the top of the
// starting page.  Either way the target is pinned until the pages above it have
// rendered their true heights, so the position doesn't drift as the placeholder
// estimates resolve — and the pin is dropped the moment the user scrolls.
function _restoreScrollAnchor() {
  const want = _pendingScrollAnchor;
  _pendingScrollAnchor = null;
  const area = document.getElementById('canvas-area');
  if (!area || !allPages.length) return;
  // Fall back to the current page's top if the remembered page is gone (it can
  // have been deleted in another window since we last showed this tab).
  const useAnchor = !!(want && want.pageId && _pageBlocks.has(want.pageId));
  const pageId = useAnchor ? want.pageId : (allPages[currentIdx] || {}).id;
  const idx    = allPages.findIndex(p => p.id === pageId);
  const block  = document.getElementById('page-block-' + pageId);
  if (!block || idx < 0) return;
  _pendingPin = {
    pageId, idx, gen: ++_navGen,
    dy: useAnchor ? (want.dy || 0) : 0,
    dx: useAnchor ? (want.dx || 0) : undefined,
    // Hold the anchor until the user scrolls (or another jump supersedes it)
    // rather than releasing once the neighbouring pages are in: pages further
    // up render whenever they like, and each one that arrives at its real
    // height moves everything below it.
    hold: true,
  };
  _applyPendingPin();
}

// Re-pin the pending-jump target to the top of the viewport.  Called as the
// pages around the target finish rendering (their real heights replace the
// placeholder estimates).  Stops once the target and the pages above it are
// rendered, or when the user scrolls (which clears _pendingPin).
function _applyPendingPin() {
  if (!_pendingPin || _pendingPin.gen !== _navGen) return;
  const area  = document.getElementById('canvas-area');
  const block = document.getElementById('page-block-' + _pendingPin.pageId);
  if (area && block) {
    // dy/dx are set when the pin is restoring a remembered scroll position
    // (a tab switch); a plain page jump pins the page top, dy = 0.
    const want  = block.offsetTop + (_pendingPin.dy || 0);
    const wantX = _pendingPin.dx;
    const movingX = (wantX !== undefined && Math.abs(area.scrollLeft - wantX) > 1);
    if (Math.abs(area.scrollTop - want) > 1 || movingX) {
      area.scrollTop = want;
      if (movingX) area.scrollLeft = wantX;
    }
    // Record where we left it either way — an event can arrive for a scroll we
    // performed earlier (or for the clamp the column rebuild caused).
    _selfScrollAt = { top: area.scrollTop, left: area.scrollLeft };
  }
  if (_pendingPin.hold) return;
  // Done once every page from the target upward to the prerender margin is in.
  const lo = Math.max(0, _pendingPin.idx - PRERENDER_MARGIN);
  for (let i = lo; i <= _pendingPin.idx; i++) {
    const st = _pageBlocks.get(allPages[i].id);
    if (st && !st.rendered) return;        // still settling — keep the pin
  }
  _pendingPin = null;
}

function prevPage() { if (currentIdx > 0) goToPage(currentIdx - 1); }
function nextPage()  { if (currentIdx < allPages.length - 1) goToPage(currentIdx + 1); }

// ── Zoom ────────────────────────────────────────────────────────────────────
// A pinch lands on values between the preset steps, so "next step up" is
// whatever step is clear of where we are now rather than the neighbour in the
// list; the epsilon stops a zoom that is a hair under a step from re-selecting
// the step it is already showing.
const _ZOOM_EPS = 0.005;
const ZOOM_MIN  = ZOOM_STEPS[0];
const ZOOM_MAX  = ZOOM_STEPS[ZOOM_STEPS.length - 1];

function zoomIn() {
  const next = ZOOM_STEPS.find(z => z > zoomLevel + _ZOOM_EPS);
  if (next) { zoomLevel = next; applyZoom(); }
}
function zoomOut() {
  const prev = [...ZOOM_STEPS].reverse().find(z => z < zoomLevel - _ZOOM_EPS);
  if (prev) { zoomLevel = prev; applyZoom(); }
}
function zoomReset() { zoomLevel = 1.0; applyZoom(); }

// ── Fit-width zoom ───────────────────────────────────────────────────────────
// Default view: pages open fitted to the canvas width.  The flag is consumed
// by the first renderPageBlock once the page's natural pixel width is known.
let _fitWidthPending = false;

// The widest page (in points) the current auto-fit was computed from, and the
// zoom it produced.  Together they let a later render that turns out wider than
// the metadata claimed correct the fit — and stop it from doing so once the
// user has set a zoom of their own.
let _fitWidestPts = 0;
let _autoFitZoom  = null;

function _applyFitZoom(z) {
  zoomLevel        = z;
  _autoFitZoom     = z;
  _lastAppliedZoom = z;
  const zl = document.getElementById('zoom-label');
  if (zl) zl.textContent = Math.round(zoomLevel * 100) + '%';
  redrawAllRendered();   // resize any page built before the fit applied
}

// Reading layout: 'single' (continuous column) or 'two-up' (side-by-side grid).
// Viewer-wide (persists across tabs); the class lives on #panel-viewer so it
// applies in both the windowed and fullscreen views.
let _pageLayout = 'single';

function _fitZoomFor(naturalW) {
  const area = document.getElementById('canvas-area');
  if (!area || !naturalW) return 1.0;
  // In two-up, the usable width is split between two columns (less the grid gap),
  // so pages fit to roughly half the viewport.
  const cols = (_pageLayout === 'two-up') ? 2 : 1;
  const gap  = (cols - 1) * 20;   // matches the grid gap in viewer.css
  // 24px column padding each side + a little room for the scrollbar
  const avail = Math.max(200, (area.clientWidth - 58 - gap) / cols);
  return Math.max(0.25, Math.min(4.0, avail / naturalW));
}

// Widest page width in PDF points across the whole document.  A page that has
// rendered reports its own width (pdfium's, post-/Rotate); the rest go on the
// page-list metadata.
function _docWidestWidthPts() {
  let maxW = 0;
  for (const p of allPages) {
    const st = _pageBlocks.get(p.id);
    const w  = (st && st.rendered && st.pageW) ? st.pageW : (+p.width_pts || 0);
    if (w > maxW) maxW = w;
  }
  return maxW;
}

// Logical-pixel width the canvas should fit to: the *widest* page in the
// document, expressed in the reference page's logical (render-scale) pixels.
// Fitting to the widest page (rather than just the current/first one) means a
// mixed portrait/landscape document opens with the landscape pages fitting the
// viewport and the narrower pages centred — no horizontal scrolling or sideways
// panning between page sizes. Falls back to the reference page's own width when
// per-page metadata isn't available.
function _fitNaturalW(st) {
  const widestPts = _docWidestWidthPts();
  return Math.max(_logicalW(st), widestPts ? widestPts * st.renderScale : 0);
}

function zoomFitWidth() {
  const st = _pageBlocks.get(currentPageId)
          || [..._pageBlocks.values()].find(s => s.image);
  if (!st?.image) { _fitWidthPending = true; return; }
  _fitWidestPts = Math.max(st.pageW || 0, _docWidestWidthPts());
  zoomLevel     = _fitZoomFor(_fitNaturalW(st));
  _autoFitZoom  = zoomLevel;   // a later, wider page may still correct this
  applyZoom();
}

// ── Two-up (side-by-side) reading layout ─────────────────────────────────────
// Toggle the same page column between a single continuous scroll and a 2-wide
// grid. Only the layout + fit change; every page-block (canvas + its highlight,
// selection and annotation overlays) moves as a unit, so annotation, search and
// text selection are unaffected. Carries into fullscreen since the class sits on
// #panel-viewer.
function toggleTwoUp() {
  _pageLayout = (_pageLayout === 'two-up') ? 'single' : 'two-up';
  const on = _pageLayout === 'two-up';
  document.getElementById('panel-viewer')?.classList.toggle('two-up', on);
  const btn = document.getElementById('zp-twoup');
  if (btn) { btn.classList.toggle('active', on); btn.setAttribute('aria-pressed', String(on)); }
  // Shrink/grow the not-yet-rendered placeholders to the new column width first,
  // so a stale full-width placeholder can't blow out a two-up grid column before
  // it lazily renders.
  _resizePlaceholders();
  // Re-fit to the new column count. zoomFitWidth → applyZoom redraws every
  // rendered page at the new size; if nothing has rendered yet it sets
  // _fitWidthPending so the first render fits (column-aware via _fitZoomFor).
  zoomFitWidth();
}

// Re-size the placeholder blocks of not-yet-rendered pages to the current
// layout's per-column width, preserving each page's aspect ratio. Rendered
// pages size to their own canvas and are left untouched.
function _resizePlaceholders() {
  const area = document.getElementById('canvas-area');
  if (!area) return;
  const cols = (_pageLayout === 'two-up') ? 2 : 1;
  const colW = Math.max(300, (area.clientWidth - 48 - (cols - 1) * 20) / cols);
  _pageBlocks.forEach(st => {
    if (st.rendered || !st.placeholder) return;
    const ph     = st.placeholder;
    const oldW   = parseFloat(ph.style.width)  || colW;
    const oldH   = parseFloat(ph.style.height) || oldW;
    const aspect = oldW > 0 ? oldH / oldW : 11 / 8.5;
    ph.style.width  = colW + 'px';
    ph.style.height = Math.round(colW * aspect) + 'px';
  });
}

// Ctrl+scroll / trackpad pinch.  A pinch streams dozens of small deltas per
// second; stepping through the preset zoom levels on a timer turned that into a
// series of lurches.  Each delta becomes a proportional scale factor instead,
// coalesced to one zoom per animation frame, anchored under the cursor — so the
// page grows and shrinks under the fingers.
let _pinchTarget = null;   // zoom accumulated since the last frame
let _pinchFocal  = null;   // {x, y} client coords of the gesture
let _pinchRaf    = null;

window.addEventListener('wheel', e => {
  if (!e.ctrlKey) return;
  if (document.getElementById('panel-viewer').style.display === 'none') return;
  e.preventDefault();
  // deltaMode 1 is lines and 2 is pages — normalise both to pixels.
  const px = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 100 : 1);
  // Exponential so zooming out undoes zooming in by the same gesture, clamped
  // so one notch of a mouse wheel (|delta| 100+) is a step rather than a leap.
  const factor = Math.min(1.25, Math.max(0.8, Math.exp(-px * 0.01)));
  const from   = (_pinchTarget !== null) ? _pinchTarget : zoomLevel;
  _pinchTarget = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, from * factor));
  _pinchFocal  = { x: e.clientX, y: e.clientY };
  if (_pinchRaf) return;
  _pinchRaf = requestAnimationFrame(() => {
    _pinchRaf = null;
    const target = _pinchTarget, focal = _pinchFocal;
    _pinchTarget = null;
    if (target === null || Math.abs(target - zoomLevel) < 0.001) return;
    zoomLevel = target;
    applyZoom({ focalX: focal.x, focalY: focal.y, live: true });
  });
}, { passive: false });

// Where the zoom should pivot: the page point under (clientX, clientY), or the
// middle of the viewport when no focal point is given (the +/- buttons).
// Recorded as a fraction of the page block, which is the one description that
// survives the resize — absolute offsets don't, and that is what made the page
// jump: the column grows taller but scrollTop stays put, so the content slides
// out from under the viewport.
// All of this works in *layout* geometry (offsetTop/offsetHeight + scrollTop),
// never getBoundingClientRect: the settle animation below scales the column, and
// a rect read mid-animation reports the painted size, which would anchor the
// next zoom to the wrong place.
function _zoomAnchor(clientX, clientY) {
  const area = document.getElementById('canvas-area');
  if (!area) return null;
  const r = area.getBoundingClientRect();
  // Offset of the focal point within the viewport, and the content point under it.
  const ox = (clientX === undefined) ? area.clientWidth  / 2 : clientX - r.left;
  const oy = (clientY === undefined) ? area.clientHeight / 2 : clientY - r.top;
  const cx = area.scrollLeft + ox, cy = area.scrollTop + oy;

  let best = null, bestDist = Infinity;
  _pageBlocks.forEach((st, pid) => {
    if (!st.block) return;
    const bt = st.block.offsetTop, bh = st.block.offsetHeight;
    const bl = st.block.offsetLeft, bw = st.block.offsetWidth;
    if (!bh || !bw) return;
    // Distance from the focal point to the block, 0 when inside it.
    const d = (cy < bt) ? bt - cy : (cy > bt + bh ? cy - (bt + bh) : 0);
    if (d < bestDist) { bestDist = d; best = { pid, bt, bh, bl, bw }; }
  });
  if (!best) return null;
  return { pid: best.pid, ox, oy,
           fx: (cx - best.bl) / best.bw,
           fy: (cy - best.bt) / best.bh };
}

// Put the anchored page point back under the focal point.
function _restoreZoomAnchor(a) {
  if (!a) return;
  const area = document.getElementById('canvas-area');
  const st   = _pageBlocks.get(a.pid);
  if (!area || !st?.block) return;
  area.scrollTop  = st.block.offsetTop  + a.fy * st.block.offsetHeight - a.oy;
  area.scrollLeft = st.block.offsetLeft + a.fx * st.block.offsetWidth  - a.ox;
}

let _lastAppliedZoom = 1.0;
let _zoomAnim = null;   // in-flight settle animation, superseded by the next zoom
// opts: {focalX, focalY} to pivot on a point (defaults to the viewport centre),
//       live:true while a gesture is in flight — skips the settle animation and
//       defers off-screen redraws.
function applyZoom(opts) {
  opts = opts || {};
  // A zoom retires a held scroll anchor: its offset into the page is in
  // old-zoom pixels, so re-applying it after the redraw would jump the view.
  if (_pendingPin && _pendingPin.hold) _pendingPin = null;
  document.getElementById('zoom-label').textContent = Math.round(zoomLevel * 100) + '%';
  const ratio = _lastAppliedZoom / zoomLevel;
  _lastAppliedZoom = zoomLevel;

  const anchor = _zoomAnchor(opts.focalX, opts.focalY);
  redrawAllRendered({ deferOffscreen: !!opts.live });  // also repopulates form overlays
  _restoreZoomAnchor(anchor);

  // The raster was just re-scaled; queue a crisp re-render (and, after a live
  // gesture, the real redraw of everything it skipped).
  _scheduleSharpRender();
  if (opts.live) return;

  // Discrete step (buttons, keyboard, Fit): the content is already at the new
  // size, so play it back from the old size and ease in — pivoting on the point
  // the scroll was anchored to, or the zoom would slide as it settles.
  const col  = document.getElementById('canvas-column');
  const area = document.getElementById('canvas-area');
  if (!col || !area || Math.abs(ratio - 1) < 0.01 || Math.abs(ratio - 1) > 1.5) return;
  try {
    // Origin in the column's own box: viewport offset + scroll − the column's
    // start.  (An animation, not a transition + timers: it cleans itself up, so
    // a frame that never comes — occluded window, background tab — can't strand
    // the column at half a zoom.)
    const ox = area.scrollLeft + (anchor ? anchor.ox : area.clientWidth  / 2) - col.offsetLeft;
    const oy = area.scrollTop  + (anchor ? anchor.oy : area.clientHeight / 2) - col.offsetTop;
    col.style.transformOrigin = `${ox}px ${oy}px`;
    if (_zoomAnim) _zoomAnim.cancel();
    _zoomAnim = col.animate(
      [{ transform: `scale(${ratio})` }, { transform: 'scale(1)' }],
      { duration: 160, easing: 'ease' });
  } catch (_) { /* no Web Animations — the zoom still applied, just not eased */ }
}

// ── Re-render on zoom (sharp text at any zoom, like Adobe) ───────────────────
// The base render is a ~144 DPI raster.  Scaling it up on the canvas (CSS-style)
// blurs text past 100%.  Instead, once a zoom/scroll settles we re-rasterize the
// *visible* pages from vector at a scale matching the current zoom, so the
// source bitmap has at least as many pixels as the screen shows.  Only visible
// pages are upgraded, and the bitmap is released back toward base when zoomed
// out again, so memory stays bounded.
let _sharpTimer = null;
function _scheduleSharpRender() {
  clearTimeout(_sharpTimer);
  _sharpTimer = setTimeout(_ensureSharpRenders, 240);
}

function _visiblePageBlocks() {
  const area = document.getElementById('canvas-area');
  if (!area) return [];
  const top = area.scrollTop - 100, bot = area.scrollTop + area.clientHeight + 100;
  const out = [];
  _pageBlocks.forEach((st, pid) => {
    if (!st.rendered || !st.block) return;
    const o = st.block.offsetTop, h = st.block.offsetHeight;
    if (o + h >= top && o <= bot) out.push([pid, st]);
  });
  return out;
}

async function _ensureSharpRenders() {
  // Only when the viewer panel is actually showing.
  const panel = document.getElementById('panel-viewer');
  if (panel && panel.style.display === 'none') return;
  // The zoom/scroll has settled — pay back any redraws a live gesture deferred.
  _flushDeferredRedraws();
  const dpr = window.devicePixelRatio || 1;

  for (const [pid, st] of _visiblePageBlocks()) {
    if (st.hiPending) continue;
    // Pixels-per-point the on-screen canvas currently shows on this device.
    const wantScale = st.renderScale * zoomLevel * dpr;
    // The most pdfium will actually give this page: the global guard, lowered to
    // st.capScale once a previous render came back clamped (large-format page
    // hitting the pixel budget).  Prevents re-requesting an unachievable scale
    // on every scroll/zoom-in.
    const capMax = Math.min(_MAX_RENDER_SCALE_CLIENT,
                            st.capScale || _MAX_RENDER_SCALE_CLIENT);
    const target = Math.max(st.renderScale, Math.min(capMax, wantScale));
    const have   = st.imageScale || st.renderScale;
    // One comparison handles both directions: sharpen (target > have) when
    // zoomed in, and release the heavy bitmap (target < have) when zoomed out.
    if (Math.abs(target - have) < 0.10) continue;

    st.hiPending = true;
    try {
      const res = await _vFetch('/api/viewer/render', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({session_id: SESSION_ID, page_id: pid, scale: target})
      });
      const data = await res.json();
      if (data.error) throw apiErr(data);
      // If pdfium clamped below what we asked, the page is pixel-budget limited;
      // remember that ceiling so we stop asking for more.
      if (data.scale < target - 0.05) st.capScale = data.scale;
      await new Promise((resolve, reject) => {
        const img = new Image();
        img.onload  = () => {
          // A later zoom may have superseded this request — only swap if the
          // page block still exists.
          const cur = _pageBlocks.get(pid);
          if (cur && cur.rendered) {
            cur.image = img; cur.imageScale = data.scale;
            redrawPageBlock(pid);
          }
          resolve();
        };
        img.onerror = reject;
        img.src = 'data:image/png;base64,' + data.image;
      });
    } catch (e) {
      if (e.name !== 'AbortError') console.warn('sharp re-render failed:', e.message);
    } finally {
      st.hiPending = false;
    }
  }
}

// ── Return / Layout / Edit ───────────────────────────────────────────────────
// Layout is a page-management view (reorder/rotate/delete/merge) and, like the
// editor, requires an open document.  Dropping a doc into an empty Layout panel
// would strand it: layout sets its own session but switchToViewerFromLayout()
// never adopts it, so the viewer returns to its empty state.  Opening a first
// doc is handled by the viewer's own empty-state drop zone instead.
function goLayout() {
  if (_blockedByRestriction()) return;
  if (SESSION_ID) switchToLayout();
  else showToast(window.t('toast.open_a_document_first_to_use'));
}
// ── Viewer empty state ────────────────────────────────────────────────────────
function _updateViewerEmptyState() {
  const es = document.getElementById('viewer-empty-state');
  if (!es) return;
  if (allPages.length === 0) {
    es.classList.add('show');
    _loadRecentFiles();
  } else {
    es.classList.remove('show');
  }
}

// ── Recent files ─────────────────────────────────────────────────────────────
// Shared fetch — used by both the empty-state cards and the File-menu list so
// the two stay in sync from a single source of truth.
async function _fetchRecentFiles() {
  try {
    const res = await apiFetch('/api/recent_files', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    });
    return (await res.json()).files || [];
  } catch (_) {
    return [];
  }
}

// Empty-state cards (shown on the canvas when no document is loaded).
async function _loadRecentFiles() {
  const wrap = document.getElementById('recent-files');
  const list = document.getElementById('recent-files-list');
  if (!wrap || !list) return;
  const files = await _fetchRecentFiles();
  list.innerHTML = '';
  files.forEach(f => {
      const card = document.createElement('button');
      card.className = 'recent-card';
      card.title     = f.path;
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.setAttribute('viewBox', '0 0 24 24');
      icon.innerHTML = '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" ' +
        'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>' +
        '<polyline points="14 2 14 8 20 8" fill="none" stroke="currentColor" ' +
        'stroke-width="1.6" stroke-linecap="round"/>';
      const name = document.createElement('span');
      name.className   = 'rc-name';
      name.textContent = f.name || f.path;
      const dir = document.createElement('span');
      dir.className   = 'rc-path';
      dir.textContent = (f.path || '').replace(/[\\/][^\\/]+$/, '');
      card.appendChild(icon); card.appendChild(name); card.appendChild(dir);
      card.onclick = () => _openRecent(f.path);
      list.appendChild(card);
  });
  wrap.style.display = files.length ? '' : 'none';
}

async function _openRecent(path) {
  showLoading('Opening…');
  const jobId = newProgressId();
  const stopPoll = startProgressPoll(jobId);
  try {
    const res = await apiFetch('/api/open_paths', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ paths: [path], progress_id: jobId })
    });
    await _viewerHandleOpenResponse(await res.json());
  } catch (e) {
    showToast(window.t('toast.open_failed_2') + e.message, 'error');
    hideLoading();
  } finally {
    stopPoll();
  }
}

// ── File menu (toolbar dropdown) ─────────────────────────────────────────────
// Surfaces New / Open / Save / Close / Print plus the recent-files list so they
// are reachable while a document is loaded (the empty-state cards only show when
// the canvas is empty). Each item dispatches to a function that already exists.
// Which of the three panels is currently visible (only one ever is):
// 'panel-viewer' | 'panel-layout', or null if none.
function _activePanelId() {
  for (const id of ['panel-viewer', 'panel-layout']) {
    if (_panelShown(document.getElementById(id))) return id;
  }
  return null;
}

/** Is an app panel on screen?  Panels are hidden by CSS until _setActivePanel
 *  writes an inline display, so an EMPTY inline value means hidden, not shown. */
function _panelShown(el) {
  return !!(el && el.style.display && el.style.display !== 'none');
}


// There is one #file-menu dropdown but a File button in each of the three
// toolbars; remember which one opened it so the menu lands under that button and
// a click on it counts as "inside".
let _fileMenuBtnId = 'file-menu-btn';
function _fileMenuBtn() { return document.getElementById(_fileMenuBtnId); }

function toggleFileMenu(ev, btnId) {
  if (ev) ev.stopPropagation();
  const menu = document.getElementById('file-menu');
  if (!menu) return;
  const wasOpen = menu.classList.contains('show');
  const sameBtn = !btnId || btnId === _fileMenuBtnId;
  closeFileMenu();
  if (btnId) _fileMenuBtnId = btnId;
  if (!(wasOpen && sameBtn)) openFileMenu();
}

async function openFileMenu() {
  const btn  = _fileMenuBtn();
  const menu = document.getElementById('file-menu');
  if (!btn || !menu) return;

  // Reflect document/tab state on the items that need it.  Layout can hold a
  // document the viewer has not loaded, so "is there a document" has to ask the
  // active panel, not just allPages.
  const hasDoc   = allPages.length > 0 ||
                   (typeof pages !== 'undefined' && Array.isArray(pages) && pages.length > 0);
  const multiTab = _tabs.length > 1;
  menu.querySelectorAll('[data-needs-doc]').forEach(el =>
    el.classList.toggle('disabled', !hasDoc));
  menu.querySelectorAll('[data-needs-tabs]').forEach(el =>
    el.classList.toggle('disabled', !multiTab));

  _populateFileMenuRecent();

  // Show first (so we can measure), then position under the button. Fixed
  // positioning keeps it clear of the toolbar's horizontal overflow.
  menu.style.visibility = 'hidden';
  menu.style.left = '0px';
  menu.style.top  = '0px';
  menu.classList.add('show');
  btn.setAttribute('aria-expanded', 'true');
  const r  = btn.getBoundingClientRect();
  const mw = menu.offsetWidth;
  let left = r.left;
  if (left + mw > window.innerWidth - 8) left = window.innerWidth - mw - 8;
  menu.style.left = Math.max(8, left) + 'px';
  menu.style.top  = (r.bottom + 4) + 'px';
  menu.style.visibility = '';

  document.addEventListener('mousedown', _fileMenuOutside, true);
  document.addEventListener('keydown', _fileMenuKey, true);

  // Land keyboard focus on the first item so arrow keys drive the menu.
  _fileMenuFocus(0);
}

function closeFileMenu() {
  const menu = document.getElementById('file-menu');
  if (menu) menu.classList.remove('show');
  ['file-menu-btn', 'file-menu-btn-layout', 'file-menu-btn-editor'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.setAttribute('aria-expanded', 'false');
  });
  document.removeEventListener('mousedown', _fileMenuOutside, true);
  document.removeEventListener('keydown', _fileMenuKey, true);
}

function _fileMenuOutside(e) {
  const menu = document.getElementById('file-menu');
  const btn  = _fileMenuBtn();
  if (menu && menu.contains(e.target)) return;
  if (btn  && btn.contains(e.target))  return;
  closeFileMenu();
}

// The enabled, visible menu items (static actions + recent files) in DOM order.
function _fileMenuItems() {
  const menu = document.getElementById('file-menu');
  if (!menu) return [];
  return Array.from(menu.querySelectorAll('[role="menuitem"]'))
    .filter(el => !el.classList.contains('disabled') && el.offsetParent !== null);
}

// Roving focus: exactly one item is tabbable (tabindex 0) and focused at a time.
function _fileMenuFocus(idx) {
  const items = _fileMenuItems();
  if (!items.length) return;
  const i = ((idx % items.length) + items.length) % items.length;
  items.forEach((el, j) => el.setAttribute('tabindex', j === i ? '0' : '-1'));
  items[i].focus();
}

// Menu keyboard model: ↑/↓ move (wrapping), Home/End jump, Enter/Space activate,
// Esc closes and returns focus to the button, Tab closes and lets focus move on.
function _fileMenuKey(e) {
  if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation();
    closeFileMenu();
    const btn = document.getElementById('file-menu-btn');
    if (btn) btn.focus();
    return;
  }
  if (e.key === 'Tab') { closeFileMenu(); return; }   // don't preventDefault: focus proceeds

  const items = _fileMenuItems();
  if (!items.length) return;
  const cur = items.indexOf(document.activeElement);
  // stopPropagation as well as preventDefault: this is a capture-phase listener,
  // so stopping here keeps the bubble-phase page-nav handler (Arrow = prev/next
  // page) from also firing while the menu owns the keyboard.
  const handled = () => { e.preventDefault(); e.stopPropagation(); };
  if (e.key === 'ArrowDown')      { handled(); _fileMenuFocus(cur + 1); }
  else if (e.key === 'ArrowUp')   { handled(); _fileMenuFocus(cur < 0 ? -1 : cur - 1); }
  else if (e.key === 'Home')      { handled(); _fileMenuFocus(0); }
  else if (e.key === 'End')       { handled(); _fileMenuFocus(items.length - 1); }
  else if (e.key === 'Enter' || e.key === ' ') {
    handled();
    if (cur >= 0) items[cur].click();
  }
}

async function _populateFileMenuRecent() {
  const box = document.getElementById('file-menu-recent');
  if (!box) return;
  const files = await _fetchRecentFiles();
  box.innerHTML = '';
  if (!files.length) {
    const empty = document.createElement('div');
    empty.className = 'fm-empty';
    empty.textContent = window.t('recent.none');
    box.appendChild(empty);
    return;
  }
  files.forEach(f => {
    const item = document.createElement('div');
    item.className = 'fm-recent';
    item.setAttribute('role', 'menuitem');
    item.setAttribute('tabindex', '-1');
    item.title = f.path || '';
    const name = document.createElement('div');
    name.className   = 'fm-rname';
    name.textContent = f.name || f.path;
    const path = document.createElement('div');
    path.className   = 'fm-rpath';
    path.textContent = (f.path || '').replace(/[\\/][^\\/]+$/, '');
    item.appendChild(name);
    item.appendChild(path);
    item.onclick = () => { closeFileMenu(); _openRecent(f.path); };
    box.appendChild(item);
  });
}

function fileMenuAction(kind) {
  closeFileMenu();
  // Open / Save mean different things per panel; _cmdOpen and _cmdSave already
  // encode that (they back the Ctrl+O / Ctrl+S shortcuts), so reuse them rather
  // than hard-wiring the viewer's version into a menu three panels now share.
  switch (kind) {
    case 'new':   newDocument();            break;
    case 'open':  _cmdOpen();               break;
    case 'save':  _cmdSave();               break;
    case 'print': printDocument();          break;
    case 'watermark-all': wmViewerApplyAll(); break;
    case 'close': if (_tabs.length > 1) _closeTab(_activeTabIdx); break;
  }
}

// Open files into a new tab. Native app → OS file dialog by real path; plain
// browser → the hidden <input type=file> fallback.
async function viewerOpenViaDialog() {
  const paths = await nativePickOpenFiles(null);   // null → don't bind to a session
  if (paths === undefined) {                        // not the native app
    const inp = document.getElementById('viewer-file-input');
    if (inp) inp.click();
    return;
  }
  if (!paths || !paths.length) return;              // dialog cancelled
  showLoading('Opening…');
  const jobId = newProgressId();
  const stopPoll = startProgressPoll(jobId);
  try {
    const res = await apiFetch('/api/open_paths', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ paths, progress_id: jobId })
    });
    await _viewerHandleOpenResponse(await res.json());
  } catch (e) {
    showToast(window.t('toast.open_failed_2') + e.message, 'error');
    hideLoading();
  } finally {
    stopPoll();
  }
}

function _viewerFileInputChange(e) {
  const files = Array.from(e.target.files || []);
  e.target.value = '';                              // allow re-picking the same file
  if (files.length) viewerOpenFiles(files);
}

// ── Viewer drag & drop (open files directly) ─────────────────────────────────
function viewerDragOver(e) {
  if (e.dataTransfer.types.includes('Files')) {
    e.preventDefault();
    document.getElementById('canvas-area').classList.add('drag-over');
  }
}
function viewerDragLeave(e) {
  if (!e.currentTarget.contains(e.relatedTarget))
    document.getElementById('canvas-area').classList.remove('drag-over');
}
async function viewerDrop(e) {
  e.preventDefault();
  document.getElementById('canvas-area').classList.remove('drag-over');
  const files = Array.from(e.dataTransfer.files);
  if (!files.length) return;
  // A viewer drop opens each file as its own tab (window.onViewerFilesDropped),
  // unlike the layout panel which appends them to one document.
  await dispatchFileDrop(files, window.onViewerFilesDropped,
                         () => viewerOpenFiles(files));
}

// Native drop entry point: open each dropped file (by real OS path) as a new tab.
window.onViewerFilesDropped = async function (paths) {
  if (!paths || !paths.length) return;
  // Real paths arrived — but only open them if the byte-upload fallback has not
  // already handled this same drop (see claimNativeDrop in shared.js).
  if (typeof claimNativeDrop === 'function' && !claimNativeDrop()) return;
  showLoading(window.t('toast.opening_files', {count: paths.length}));
  try {
    // Open EACH file as its OWN tab: one request (→ one fresh session) per file.
    // Sending them all in a single open_paths call merges them into one document
    // (one tab) — merging is the layout panel's job, not a viewer drop's.
    for (const p of paths) {
      const jobId = newProgressId();
      const stopPoll = startProgressPoll(jobId);
      try {
        const res = await apiFetch('/api/open_paths', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paths: [p], progress_id: jobId })
        });
        await _viewerHandleOpenResponse(await res.json());
      } finally {
        stopPoll();
      }
    }
  } catch (err) {
    showToast(window.t('toast.open_failed_2') + err.message, 'error');
  } finally {
    hideLoading();
  }
};

async function viewerOpenFiles(files) {
  const hasDocs = files.some(f => !f.name.toLowerCase().endsWith('.pdf'));
  showLoading(files.length > 1
    ? window.t('toast.opening_files', {count: files.length})
    : (hasDocs ? 'Converting & uploading…' : 'Uploading…'));
  // Never pass the existing SESSION_ID, and upload ONE file per request — each
  // file gets its own fresh session so it opens in its own tab rather than being
  // merged into a single document. (Appending is the layout panel's job.)
  try {
    for (const f of files) {
      const jobId = newProgressId();
      const stopPoll = startProgressPoll(jobId);
      const formData = new FormData();
      formData.append('files', f);
      formData.append('progress_id', jobId);
      try {
        const res = await apiFetch('/api/upload', { method: 'POST', body: formData });
        await _viewerHandleOpenResponse(await res.json());
      } finally {
        stopPoll();
      }
    }
  } catch(err) {
    showToast(window.t('toast.upload_failed_2') + err.message, 'error');
  } finally {
    hideLoading();
  }
}

// Shared post-open handling for both byte-upload and path-based opens.
async function _viewerHandleOpenResponse(data) {
  if (data.errors && data.errors.length > 0)
    setTimeout(() => showToast(window.t('toast.could_not_convert') + data.errors[0], 'error'), 600);

  // Handle encrypted files that need a password
  if (data.password_required && data.password_required.length > 0) {
    hideLoading();   // dismiss the "Uploading…" overlay so the dialog is reachable
    const first = data.password_required[0];
    _showOpenPasswordDialog(first.filename, first.ref, false);
  }

  const newSession = data.session_id;
  const firstPage  = data.pages && data.pages.length ? data.pages[0].id : '';
  if (data.pages && data.pages.length > 0) {
    if (SESSION_ID) {
      openNewTab(newSession, firstPage);
    } else {
      SESSION_ID = newSession;
      if (typeof sessionId !== 'undefined') sessionId = SESSION_ID;
      if (_tabs[_activeTabIdx]) _tabs[_activeTabIdx].session_id = SESSION_ID;
      _updateLockButton(!!data.is_protected);
      await loadPageList();
      await Promise.all([loadAllAnnotations(), loadFormFields(), loadAllLinks()]);
    }
  } else if (!data.password_required || !data.password_required.length) {
    // No pages and no password prompt — nothing loaded
    hideLoading();
  }
}

// ── New blank document (opens in a new tab) ──────────────────────────────────
async function newDocument() {
  try {
    const res  = await apiFetch('/api/new_document', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    if (SESSION_ID) {
      openNewTab(data.session_id, data.page_id);
    } else {
      SESSION_ID = data.session_id;
      if (typeof sessionId !== 'undefined') sessionId = SESSION_ID;
      if (_tabs[_activeTabIdx]) _tabs[_activeTabIdx].session_id = SESSION_ID;
      await loadPageList();
    }
    showToast(window.t('toast.new_document_created'), 'success');
  } catch (err) {
    showToast(window.t('toast.could_not_create_document') + err.message, 'error');
  }
}

// ── Save PDF ─────────────────────────────────────────────────────────────────
// After a confirmed save the working state is on disk, so clear the doc-dirty
// flag and drop the crash-recovery snapshot for this session.  shared.js owns
// the pair (pwMarkSaved) because the editor and layout Saves have to record
// exactly the same thing.
function _viewerClearRecoveryAfterSave() {
  if (typeof pwMarkSaved === 'function') pwMarkSaved(SESSION_ID);
}

async function viewerSaveToFile() {
  const baseName = (allPages[0]?.filename || 'document').replace(/\.pdf$/i, '');
  const suggestedName = baseName + '_edited.pdf';

  // Native app: OS save dialog defaulting to the document's source folder.
  const np = await nativePickSavePath(SESSION_ID, suggestedName);
  if (np !== undefined) {                 // native app
    if (!np) return;                      // dialog cancelled
    showLoading('Saving PDF…');
    try {
      await saveSessionToPath(SESSION_ID, np);   // throws with the reason
      _viewerClearRecoveryAfterSave();
      showToast(window.t('toast.saved') + np, 'success');
    } catch (e) {
      showToast(window.t('toast.save_failed') + e.message, 'error');
    } finally {
      hideLoading();
    }
    return;
  }

  showLoading('Saving PDF…');
  try {
    const resp = await apiFetch('/api/save_blob', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID }),
    });
    if (!resp.ok) throw new Error('Server error ' + resp.status);
    const blob = await resp.blob();
    if (window.showSaveFilePicker) {
      try {
        const fh = await window.showSaveFilePicker({
          suggestedName,
          types: [{ description: 'PDF Document', accept: { 'application/pdf': ['.pdf'] } }],
        });
        const ws = await fh.createWritable();
        await ws.write(blob);
        await ws.close();
        _viewerClearRecoveryAfterSave();
        showToast(window.t('toast.pdf_saved'));
        return;
      } catch (err) {
        if (err.name === 'AbortError') return;
        // fall through to download fallback
      }
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = suggestedName;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    _viewerClearRecoveryAfterSave();
    showToast(window.t('toast.pdf_downloaded'));
  } catch (e) {
    showToast(window.t('toast.save_failed') + e.message);
  } finally {
    hideLoading();
  }
}

// ── Search ───────────────────────────────────────────────────────────────────
let _searchTimer = null;
function onSearchInput() {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(runSearch, 350);
}
function onSearchKey(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    if (e.shiftKey) searchPrev(); else searchNext();
  } else if (e.key === 'Escape') {
    clearSearch();
  }
}

async function runSearch() {
  const q = document.getElementById('search-input').value.trim();
  if (q === searchQuery) return;
  searchQuery = q;
  document.getElementById('clear-search-btn').style.display = q ? '' : 'none';
  markThumbsWithMatches();

  if (!q) {
    searchMatches  = [];
    searchFlatList = [];
    searchCursor   = -1;
    _setSearchInfo('', false);
    clearAllHighlights();
    markThumbsWithMatches();
    return;
  }

  _setSearchInfo('Searching', true);

  try {
    const res  = await apiFetch('/api/viewer/search', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session_id: SESSION_ID, query: q})
    });
    const data = await res.json();
    searchMatches  = data.matches || [];
    searchFlatList = [];
    searchMatches.forEach((m, mi) => {
      m.rects.forEach((rect, ri) => {
        searchFlatList.push({match_i: mi, rect_i: ri, page_idx: m.page_idx,
                             page_id: m.page_id, rect});
      });
    });
    searchCursor = -1;
    markThumbsWithMatches();
    // Draw highlights on all already-rendered pages
    _pageBlocks.forEach((st, pid) => { if (st.rendered) drawPageHighlights(pid); });

    if (!searchFlatList.length) {
      _setSearchInfo('No matches', false);
      return;
    }
    searchCursor = 0;
    updateSearchInfo();
    await jumpToMatch(searchCursor);
  } catch(e) {
    _setSearchInfo('', false);
    showToast(window.t('toast.search_failed') + e.message, 'error');
  }
}

async function searchNext() {
  if (!searchFlatList.length) return;
  searchCursor = (searchCursor + 1) % searchFlatList.length;
  await jumpToMatch(searchCursor);
}
async function searchPrev() {
  if (!searchFlatList.length) return;
  searchCursor = (searchCursor - 1 + searchFlatList.length) % searchFlatList.length;
  await jumpToMatch(searchCursor);
}

async function jumpToMatch(cursor) {
  const item = searchFlatList[cursor];
  if (!item) return;
  updateSearchInfo();
  const st = _pageBlocks.get(item.page_id);
  if (!st?.rendered) {
    // Scroll the block into view so the IntersectionObserver triggers its render
    goToPage(item.page_idx);
    await new Promise(r => setTimeout(r, 700));
  }
  // Redraw so the newly-focused match gets its highlight style
  _pageBlocks.forEach((s, pid) => { if (s.rendered) drawPageHighlights(pid); });
  _scrollToMatchInPage(item);
}

// Centralised search-status setter. `busy` toggles the animated "Searching…"
// state (pulsing text + cycling dots) so long OCR searches clearly look active.
function _setSearchInfo(text, busy) {
  const el = document.getElementById('search-info');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('searching', !!busy);
}

function updateSearchInfo() {
  _setSearchInfo(
    searchFlatList.length ? `${searchCursor + 1} / ${searchFlatList.length}` : 'No matches',
    false);
}

function clearSearch() {
  document.getElementById('search-input').value = '';
  searchQuery = '';
  searchMatches  = [];
  searchFlatList = [];
  searchCursor   = -1;
  _setSearchInfo('', false);
  document.getElementById('clear-search-btn').style.display = 'none';
  clearAllHighlights();
  markThumbsWithMatches();
}

// ── Coordinate conversion ─────────────────────────────────────────────────────
// pd = { pageH, renderScale } — omit to use current-page globals.
// PDF y=0 is at the bottom; display y=0 is at the top.
function pdfToDisplay(rect, pd) {
  const pH = pd ? pd.pageH       : pageH;
  const rs = pd ? pd.renderScale : renderScale;
  const s  = rs * zoomLevel;
  return { x: rect.x * s, y: (pH - rect.y - rect.h) * s, w: rect.w * s, h: rect.h * s };
}

// ── Per-page highlight drawing ────────────────────────────────────────────────
function clearAllHighlights() {
  _pageBlocks.forEach(st => {
    if (st.hlOv) while (st.hlOv.firstChild) st.hlOv.removeChild(st.hlOv.firstChild);
  });
}

function drawPageHighlights(page_id) {
  const st = _pageBlocks.get(page_id);
  if (!st?.hlOv) return;
  const ov = st.hlOv;
  while (ov.firstChild) ov.removeChild(ov.firstChild);
  if (!searchQuery || !searchFlatList.length) return;

  const ns      = 'http://www.w3.org/2000/svg';
  const pageIdx = allPages.findIndex(p => p.id === page_id);
  const match   = searchMatches.find(m => m.page_idx === pageIdx);
  if (!match) return;

  const pd = { pageH: st.pageH, renderScale: st.renderScale };
  match.rects.forEach((rect, ri) => {
    const flat_i  = searchFlatList.findIndex(f => f.page_id === page_id && f.rect_i === ri);
    const focused = flat_i === searchCursor;
    const d = pdfToDisplay(rect, pd);
    const el = document.createElementNS(ns, 'rect');
    el.setAttribute('x',      Math.max(0, d.x - 1));
    el.setAttribute('y',      Math.max(0, d.y - 1));
    el.setAttribute('width',  d.w + 2);
    el.setAttribute('height', d.h + 2);
    el.setAttribute('rx',     '2');
    // Colours come from the --sel / --hl theme tokens (viewer.css)
    el.setAttribute('class',  focused ? 'hl-rect focused' : 'hl-rect');
    ov.appendChild(el);
  });
}

function _scrollToMatchInPage(item) {
  const st    = _pageBlocks.get(item.page_id);
  if (!st?.rendered) return;
  const d     = pdfToDisplay(item.rect, { pageH: st.pageH, renderScale: st.renderScale });
  const block = document.getElementById('page-block-' + item.page_id);
  const area  = document.getElementById('canvas-area');
  if (!block || !area) return;
  area.scrollTop  = block.offsetTop  + d.y + d.h / 2 - area.clientHeight / 2;
  area.scrollLeft = block.offsetLeft + d.x + d.w / 2 - area.clientWidth  / 2;
}

// ── Sticky note annotations ──────────────────────────────────────────────────

async function loadAllAnnotations() {
  try {
    const res  = await _vFetch('/api/viewer/all_annotations', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID})
    });
    const data = await res.json();
    allAnnotations = data.annotations_by_page || {};
    vRefreshHistory(data);
  } catch(_) {
    allAnnotations = {};
  }
  pageAnnotations = allAnnotations[currentPageId] || [];
  _pageBlocks.forEach((st, pid) => { if (st.rendered) drawPageAnnotations(pid); });
  renderComments();
}

async function loadAnnotations(page_id) {
  try {
    const res  = await _vFetch('/api/viewer/annotations', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID, page_id})
    });
    const data = await res.json();
    allAnnotations[page_id] = data.annotations || [];
  } catch(_) {
    allAnnotations[page_id] = [];
  }
  if (page_id === currentPageId) pageAnnotations = allAnnotations[page_id];
  const st = _pageBlocks.get(page_id);
  if (st?.rendered) drawPageAnnotations(page_id);
  renderComments();
}


function drawPageAnnotations(page_id) {
  const st = _pageBlocks.get(page_id);
  if (!st?.annotOv) return;
  const ov  = st.annotOv;
  while (ov.firstChild) ov.removeChild(ov.firstChild);
  const anns = allAnnotations[page_id] || [];
  if (!anns.length) return;

  const ns = 'http://www.w3.org/2000/svg';
  const pd = { pageH: st.pageH, renderScale: st.renderScale };
  const cw = st.canvas ? st.canvas.width : 0;
  const ch = st.canvas ? st.canvas.height : 0;
  _setSvgSize(ov, cw, ch);

  anns.forEach((ann, annIdx) => {
    const d  = pdfToDisplay({x: ann.x, y: ann.y, w: ann.w, h: ann.h}, pd);
    const iw = Math.max(d.w, 18), ih = Math.max(d.h, 18);
    const isHighlighted = _annotHighlight &&
      _annotHighlight.page_id === page_id && _annotHighlight.annIdx === annIdx;

    // Markup (shapes, highlights, Text-tool text) is listed but is not a
    // comment: no hit area or popup here — lift-to-edit owns its clicks.  It
    // only gets the ring, so a click on its card in the list shows where it is.
    if (ann.markup) {
      if (!isHighlighted) return;
      const ring = document.createElementNS(ns, 'rect');
      ring.setAttribute('x',      (d.x - 6).toFixed(1));
      ring.setAttribute('y',      (d.y - 6).toFixed(1));
      ring.setAttribute('width',  (iw + 12).toFixed(1));
      ring.setAttribute('height', (ih + 12).toFixed(1));
      ring.setAttribute('rx',     '4');
      ring.setAttribute('class',  'annot-ring');
      ring.style.pointerEvents = 'none';
      ov.appendChild(ring);
      return;
    }

    // FreeText — already rendered into the page image; draw an invisible hit area
    // (and a highlight ring when selected)
    if (ann.subtype === 'FreeText') {
      const grp = document.createElementNS(ns, 'g');
      grp.style.cursor = 'pointer';
      grp.style.pointerEvents = 'all';
      // Transparent hit area so the annotation is clickable
      const hit = document.createElementNS(ns, 'rect');
      hit.setAttribute('x',      d.x.toFixed(1));
      hit.setAttribute('y',      d.y.toFixed(1));
      hit.setAttribute('width',  iw.toFixed(1));
      hit.setAttribute('height', ih.toFixed(1));
      hit.setAttribute('fill',   'transparent');
      grp.appendChild(hit);
      if (isHighlighted) {
        const ring = document.createElementNS(ns, 'rect');
        ring.setAttribute('x',            (d.x - 6).toFixed(1));
        ring.setAttribute('y',            (d.y - 6).toFixed(1));
        ring.setAttribute('width',        (iw + 12).toFixed(1));
        ring.setAttribute('height',       (ih + 12).toFixed(1));
        ring.setAttribute('rx',           '4');
        ring.setAttribute('class',        'annot-ring');
        grp.appendChild(ring);
      }
      grp.addEventListener('click', e => openAnnotEdit(e, ann, page_id));
      ov.appendChild(grp);
      return;
    }

    // Sticky note (Text) — draw the folded-corner icon
    const [r,g,b] = (ann.color || [1, 0.92, 0]).map(c => Math.round(Math.min(255, c * 255)));
    const fill = `rgba(${r},${g},${b},0.88)`;
    const dark = `rgba(${Math.max(0,r-70)},${Math.max(0,g-70)},${Math.max(0,b-70)},1)`;
    const fold = iw * 0.28;

    const grp = document.createElementNS(ns, 'g');
    grp.style.cursor = 'pointer';
    grp.style.pointerEvents = 'all';

    if (isHighlighted) {
      const ring = document.createElementNS(ns, 'rect');
      ring.setAttribute('x',            (d.x - 6).toFixed(1));
      ring.setAttribute('y',            (d.y - 6).toFixed(1));
      ring.setAttribute('width',        (iw + 12).toFixed(1));
      ring.setAttribute('height',       (ih + 12).toFixed(1));
      ring.setAttribute('rx',           '4');
      ring.setAttribute('class',        'annot-ring');
      grp.appendChild(ring);
    }

    const body = document.createElementNS(ns, 'polygon');
    const x1 = d.x, y1 = d.y, x2 = d.x + iw, y2 = d.y + ih;
    body.setAttribute('points',
      `${x1},${y1} ${x2-fold},${y1} ${x2},${y1+fold} ${x2},${y2} ${x1},${y2}`);
    body.setAttribute('fill',            fill);
    body.setAttribute('stroke',          dark);
    body.setAttribute('stroke-width',    '1.5');
    body.setAttribute('stroke-linejoin', 'round');
    grp.appendChild(body);

    const corner = document.createElementNS(ns, 'polygon');
    corner.setAttribute('points',
      `${x2-fold},${y1} ${x2},${y1+fold} ${x2-fold},${y1+fold}`);
    corner.setAttribute('fill',
      `rgba(${Math.max(0,r-40)},${Math.max(0,g-40)},${Math.max(0,b-40)},0.55)`);
    corner.setAttribute('stroke',       dark);
    corner.setAttribute('stroke-width', '1');
    grp.appendChild(corner);

    [0.42, 0.65].forEach(frac => {
      const line = document.createElementNS(ns, 'line');
      line.setAttribute('x1',            (x1 + iw * 0.18).toFixed(1));
      line.setAttribute('y1',            (y1 + ih * frac).toFixed(1));
      line.setAttribute('x2',            (x1 + iw * 0.72).toFixed(1));
      line.setAttribute('y2',            (y1 + ih * frac).toFixed(1));
      line.setAttribute('stroke',        dark);
      line.setAttribute('stroke-width',  '1.5');
      line.setAttribute('stroke-linecap','round');
      line.setAttribute('opacity',       '0.45');
      grp.appendChild(line);
    });

    grp.addEventListener('mouseover', e => showAnnotTooltip(e, ann));
    grp.addEventListener('mousemove', moveAnnotTooltip);
    grp.addEventListener('mouseout',  hideAnnotTooltip);
    grp.addEventListener('click',     e => openAnnotEdit(e, ann, page_id));
    ov.appendChild(grp);
  });
}

function showAnnotTooltip(e, ann) {
  const tt  = document.getElementById('annot-tooltip');
  const ael = document.getElementById('annot-tooltip-author');
  const tel = document.getElementById('annot-tooltip-text');
  if (ann.author) { ael.textContent = ann.author; ael.style.display = ''; }
  else            { ael.style.display = 'none'; }
  tel.textContent = ann.text || '(no content)';
  tt.style.display = 'block';
  moveAnnotTooltip(e);
}

function moveAnnotTooltip(e) {
  const tt   = document.getElementById('annot-tooltip');
  const offX = 14, offY = 14;
  const maxX = window.innerWidth  - tt.offsetWidth  - 10;
  const maxY = window.innerHeight - tt.offsetHeight - 10;
  tt.style.left = Math.min(e.clientX + offX, maxX) + 'px';
  tt.style.top  = Math.min(e.clientY + offY, maxY) + 'px';
}

function hideAnnotTooltip() {
  document.getElementById('annot-tooltip').style.display = 'none';
}

// ── Annotation editing ────────────────────────────────────────────────────────
function openAnnotEdit(e, ann, page_id) {
  e.stopPropagation();
  hideAnnotTooltip();
  _editingAnnot = { page_id, annot_idx: ann.idx, ann };

  document.getElementById('annot-edit-author').value = ann.author || '';
  document.getElementById('annot-edit-text').value   = ann.text   || '';
  document.getElementById('annot-edit-save').disabled = false;

  // Position popup near click, clamped to viewport
  const popup = document.getElementById('annot-edit-popup');
  popup.style.display = 'block';
  const pw = popup.offsetWidth || 300, ph = popup.offsetHeight || 240;
  let left = e.clientX + 14, top = e.clientY + 14;
  if (left + pw > window.innerWidth  - 10) left = e.clientX - pw - 14;
  if (top  + ph > window.innerHeight - 10) top  = e.clientY - ph - 14;
  popup.style.left = Math.max(10, left) + 'px';
  popup.style.top  = Math.max(10, top)  + 'px';

  document.getElementById('annot-edit-text').focus();
}

function closeAnnotEdit() {
  document.getElementById('annot-edit-popup').style.display = 'none';
  _editingAnnot = null;
}

async function saveAnnotEdit() {
  if (!_editingAnnot) return;
  const btn       = document.getElementById('annot-edit-save');
  const newText   = document.getElementById('annot-edit-text').value;
  const newAuthor = document.getElementById('annot-edit-author').value;
  btn.disabled    = true;
  try {
    const res  = await _queueAnnotOp(() => _vFetch('/api/viewer/update_annotation', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        session_id: SESSION_ID,
        page_id:    _editingAnnot.page_id,
        annot_idx:  _editingAnnot.annot_idx,
        text:       newText,
        author:     newAuthor,
      })
    }));
    const data = await res.json();
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);

    const pid = _editingAnnot.page_id;
    closeAnnotEdit();

    // Refresh annotation overlays
    vRefreshHistory(data);
    allAnnotations[pid] = data.annotations || [];
    if (pid === currentPageId) pageAnnotations = allAnnotations[pid];
    drawPageAnnotations(pid);

    // Re-render page image (FreeText text is baked in) and update thumbnail
    _invalidateViewerPage(pid);
    if (data.thumbnail) {
      const pg = allPages.find(p => p.id === pid);
      if (pg) { pg.thumbnail = data.thumbnail; renderThumbList(); }
    }

    showToast(window.t('toast.annotation_saved'), 'success');
  } catch (err) {
    showToast(window.t('toast.save_failed') + err.message, 'error');
    btn.disabled = false;
  }
}

async function deleteAnnotEdit() {
  if (!_editingAnnot) return;
  const pid = _editingAnnot.page_id;
  const idx = _editingAnnot.annot_idx;
  try {
    const res  = await _queueAnnotOp(() => _vFetch('/api/viewer/update_annotation', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        session_id: SESSION_ID, page_id: pid, annot_idx: idx, delete: true,
      })
    }));
    const data = await res.json();
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    closeAnnotEdit();
    vRefreshHistory(data);
    allAnnotations[pid] = data.annotations || [];
    if (pid === currentPageId) pageAnnotations = allAnnotations[pid];
    drawPageAnnotations(pid);
    _invalidateViewerPage(pid);
    if (data.thumbnail) {
      const pg = allPages.find(p => p.id === pid);
      if (pg) { pg.thumbnail = data.thumbnail; renderThumbList(); }
    }
    renderComments();
    showToast(window.t('toast.annotation_deleted'), 'success');
  } catch (err) {
    showToast(window.t('toast.delete_failed_2') + err.message, 'error');
  }
}

// Close popup on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _editingAnnot) closeAnnotEdit();
});

// ── Comments ─────────────────────────────────────────────────────────────────
// ── Bookmarks (document outline) ─────────────────────────────────────────────
// State (_leftMode, _bookmarks, _ctxBookmarkTarget, _lastSelectedText) is
// declared in the main state section so _resetViewerState can reference it.

function setLeftMode(mode) {
  _leftMode = (mode === 'bookmarks') ? 'bookmarks' : 'pages';
  const isBm = _leftMode === 'bookmarks';
  const tl = document.getElementById('thumb-list');
  const bl = document.getElementById('bookmark-list');
  const tt = document.getElementById('lp-title-text');
  const add = document.getElementById('lp-add-bookmark');
  const auto = document.getElementById('lp-auto-bookmark');
  const mt  = document.getElementById('lp-mode-toggle');
  if (tl) tl.style.display = isBm ? 'none' : '';
  if (bl) bl.style.display = isBm ? '' : 'none';
  if (tt) tt.textContent = isBm ? 'Bookmarks' : 'Pages';
  if (add) add.style.display = isBm ? '' : 'none';
  if (auto) auto.style.display = isBm ? '' : 'none';
  if (mt) { mt.classList.toggle('active', isBm); mt.title = isBm ? 'Show pages' : 'Show bookmarks'; }
  if (isBm) loadBookmarks();
}
function toggleLeftMode() { setLeftMode(_leftMode === 'pages' ? 'bookmarks' : 'pages'); }

async function loadBookmarks() {
  try {
    const res  = await _vFetch('/api/viewer/bookmarks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID })
    });
    const data = await res.json();
    _bookmarks = data.bookmarks || [];
  } catch (_) { _bookmarks = []; }
  renderBookmarks();
}

// Store an outline the server just handed back, and flag the document dirty.
// Bookmarks reach the file only at save time (entry.py _write_outlines), so a
// change that isn't flagged is lost silently when the tab closes — every path
// that *edits* the outline goes through here.  loadBookmarks() deliberately
// does not: reading the existing outline is not a change.
function _setBookmarks(list) {
  _bookmarks = list || [];
  if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
}

function renderBookmarks() {
  const list = document.getElementById('bookmark-list');
  if (!list) return;
  // Drop ids that no longer exist (deleted here, or a different document loaded)
  // so a stale selection can never widen a later bulk delete.
  if (_bmSelected.size) {
    const live = new Set(_bookmarks.map(b => b.id));
    _bmSelected.forEach(id => { if (!live.has(id)) _bmSelected.delete(id); });
  }
  if (!_bookmarks.length) {
    list.innerHTML = '<div class="bm-empty">No bookmarks yet.<br>Right-click a page → “Add bookmark here”.</div>';
    return;
  }
  list.innerHTML = _bookmarks.map(bm => {
    const pg      = allPages.find(p => p.id === bm.page_id);
    const pageNum = pg ? pg.page_num : '?';
    const indent  = 8 + (bm.level || 0) * 14;
    const sel     = _bmSelected.has(bm.id) ? ' selected' : '';
    return `<div class="bm-item${sel}" style="padding-left:${indent}px"
         onmousedown="if(event.shiftKey)event.preventDefault()"
         onclick="bmItemClick(event,'${bm.id}')"
         oncontextmenu="showBookmarkCtx(event,'${bm.id}')"
         title="Go to bookmark · Ctrl/Shift-click to select · right-click for options">
      <svg class="bm-ico" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M6 2a2 2 0 0 0-2 2v18l8-5.5 8 5.5V4a2 2 0 0 0-2-2z"/></svg>
      <span class="bm-title">${escapeHtml(bm.title)}</span>
      <span class="bm-page">p.${pageNum}</span>
      <button class="bm-del" title="Delete bookmark"
              onclick="event.stopPropagation();deleteBookmark('${bm.id}')">&times;</button>
    </div>`;
  }).join('');
}

// Click in the bookmark list.  A plain click navigates (and makes that entry
// the sole selection / range anchor); Ctrl/Cmd-click toggles one entry and
// Shift-click takes the block from the anchor, neither of which navigates —
// that is what "Delete selected" then acts on.
function bmItemClick(e, id) {
  const idx = _bookmarks.findIndex(b => b.id === id);
  if (idx < 0) return;
  if (e && (e.ctrlKey || e.metaKey)) {
    if (_bmSelected.has(id)) _bmSelected.delete(id); else _bmSelected.add(id);
    _bmAnchorId = id;
    renderBookmarks();
    return;
  }
  if (e && e.shiftKey) {
    const from = _bookmarks.findIndex(b => b.id === _bmAnchorId);
    const a    = from < 0 ? idx : from;           // no anchor yet → just this one
    const lo   = Math.min(a, idx), hi = Math.max(a, idx);
    _bmSelected = new Set(_bookmarks.slice(lo, hi + 1).map(b => b.id));
    if (from < 0) _bmAnchorId = id;
    renderBookmarks();
    return;
  }
  _bmSelected = new Set([id]);
  _bmAnchorId = id;
  renderBookmarks();
  goToBookmark(id);
}

async function goToBookmark(id) {
  const bm = _bookmarks.find(b => b.id === id);
  if (!bm) return;
  const idx = allPages.findIndex(p => p.id === bm.page_id);
  if (idx < 0) { showToast(window.t('toast.bookmarked_page_not_found'), 'error'); return; }
  goToPage(idx);
  const st = _pageBlocks.get(bm.page_id);
  if (!st || !st.rendered) await new Promise(r => setTimeout(r, 700));
  const pst = _pageBlocks.get(bm.page_id);
  if (pst && pst.rendered && bm.y != null) {
    const scale = pst.renderScale * zoomLevel;
    const dispY = (pst.pageH - bm.y) * scale;
    const block = document.getElementById('page-block-' + bm.page_id);
    const area  = document.getElementById('canvas-area');
    if (block && area) area.scrollTop = block.offsetTop + dispY - 40;
  }
}

async function _addBookmark(page_id, y, title) {
  try {
    const res  = await _vFetch('/api/viewer/add_bookmark', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, page_id, y, title })
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    _setBookmarks(data.bookmarks);
    if (_leftMode !== 'bookmarks') setLeftMode('bookmarks'); else renderBookmarks();
    showToast(window.t('toast.bookmark_added'), 'success');
  } catch (e) { showToast(window.t('toast.failed') + e.message, 'error'); }
}

// Right-click → "Add bookmark here": bookmark the exact clicked spot.
function addBookmarkAtCtx() {
  _hideCtxMenu();
  if (_blockedByRestriction()) return;
  const t = _ctxBookmarkTarget;
  if (!t) { showToast(window.t('toast.right_click_on_a_page_to'), 'error'); return; }
  const pg  = allPages.find(p => p.id === t.page_id);
  const def = _lastSelectedText || (pg ? 'Page ' + pg.page_num : 'Bookmark');
  const title = window.prompt('Bookmark title:', def);
  if (title === null) return;
  _addBookmark(t.page_id, t.y, title.trim() || def);
}

// Panel "+" button: bookmark the top of the current page.
function addBookmarkCurrentPage() {
  if (_blockedByRestriction()) return;
  const pg = allPages[currentIdx];
  if (!pg) { showToast(window.t('toast.no_page_open'), 'error'); return; }
  const def = _lastSelectedText || ('Page ' + pg.page_num);
  const title = window.prompt('Bookmark title:', def);
  if (title === null) return;
  _addBookmark(pg.id, null, title.trim() || def);   // null y → top of page
}

async function deleteBookmark(id) {
  try {
    const res  = await _vFetch('/api/viewer/delete_bookmark', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, bookmark_id: id })
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    _setBookmarks(data.bookmarks);
    renderBookmarks();
  } catch (e) { showToast(window.t('toast.delete_failed_2') + e.message, 'error'); }
}

// ── Bookmark right-click menu (rename / move up / move down / delete) ─────────
let _ctxBookmarkId = null;

function showBookmarkCtx(e, id) {
  e.preventDefault();
  e.stopPropagation();
  _ctxBookmarkId = id;
  // Right-clicking outside the current selection acts on that entry alone —
  // otherwise the menu would offer to delete a block the user can no longer see.
  if (!_bmSelected.has(id)) {
    _bmSelected = new Set([id]);
    _bmAnchorId = id;
    renderBookmarks();
  }
  const menu = document.getElementById('bm-ctx-menu');
  if (!menu) return;
  // Grey out move options that aren't possible at the ends of the list.
  const idx  = _bookmarks.findIndex(b => b.id === id);
  const up   = document.getElementById('bmctx-up');
  const down = document.getElementById('bmctx-down');
  const disable = (el, off) => {
    if (!el) return;
    el.style.opacity       = off ? '.35' : '';
    el.style.pointerEvents = off ? 'none' : '';
  };
  disable(up,   idx <= 0);
  disable(down, idx < 0 || idx >= _bookmarks.length - 1);

  // "Delete selected" carries its count and stays greyed (but visible, so the
  // multi-select is discoverable) until more than one entry is selected.
  const nSel = _bmSelected.size;
  const lbl  = document.getElementById('bmctx-delsel-label');
  if (lbl) lbl.textContent = window.t('bmctx.delete_selected', {count: nSel});
  disable(document.getElementById('bmctx-delete-selected'), nSel < 2);
  disable(document.getElementById('bmctx-delete-all'), !_bookmarks.length);

  menu.style.display = 'block';
  const mw = menu.offsetWidth  || 168;
  const mh = menu.offsetHeight || 220;
  menu.style.left = Math.min(e.clientX, window.innerWidth  - mw - 8) + 'px';
  menu.style.top  = Math.min(e.clientY, window.innerHeight - mh - 8) + 'px';
}

// ── Page-rail context menu ───────────────────────────────────────────────────
// The viewer's thumbnail rail is a page list, but until now its only verb was
// "scroll to": rotating or deleting a page you spotted while reading meant a
// round trip through Layout and finding your reading position again.  These act
// on the single right-clicked page and reload the viewer in place.
let _ctxPageId = '';

function showPageCtx(e, pageId) {
  e.preventDefault();
  e.stopPropagation();
  _ctxPageId = pageId;
  const menu = document.getElementById('pg-ctx-menu');
  if (!menu) return;
  // The last page cannot be deleted — a zero-page document has nowhere to go.
  const del = document.getElementById('pgctx-delete');
  if (del) {
    const only = allPages.length <= 1;
    del.style.opacity       = only ? '.35' : '';
    del.style.pointerEvents = only ? 'none' : '';
  }
  menu.style.display = 'block';
  const mw = menu.offsetWidth  || 200;
  const mh = menu.offsetHeight || 300;
  menu.style.left = Math.min(e.clientX, window.innerWidth  - mw - 8) + 'px';
  menu.style.top  = Math.min(e.clientY, window.innerHeight - mh - 8) + 'px';
}

function _hidePageCtx() {
  const menu = document.getElementById('pg-ctx-menu');
  if (menu) menu.style.display = 'none';
}

// Reload the page list after a structural change, holding the reader's place.
// Rotation changes a page's recorded dimensions and insert/delete shifts every
// index after it, so a targeted re-render is not enough — the column has to be
// rebuilt from the server's view of the document.
// Every caller has just rotated / deleted / inserted a page server-side, so this
// is also the one place that flags the document unsaved for the whole rail menu.
async function _pgCtxReload(keepPageId) {
  if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
  currentPageId = keepPageId || currentPageId;
  _resetViewerState();
  await loadPageList();
  loadAllAnnotations();
  loadFormFields();
  loadAllLinks();
  // Same page id, new geometry (a rotate): the Pointer does not see a page
  // change, so ask it to load again.
  if (_etActive()) _etLoadForCurrentPage();
}

async function pgCtxRotate(deg) {
  _hidePageCtx();
  if (_blockedByRestriction()) return;
  const pageId = _ctxPageId;
  if (!pageId || !SESSION_ID) return;
  showLoading(window.t('toast.rotating_page'));
  try {
    const res = await apiFetch('/api/rotate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session_id: SESSION_ID, page_id: pageId, direction: deg })
    });
    if (!res.ok) throw new Error(apiErr(await res.json()));
    await _pgCtxReload(pageId);
    showToast(window.t('toast.page_rotated'), 'success');
  } catch (err) {
    showToast(window.t('toast.rotation_failed'), 'error');
  } finally { hideLoading(); }
}

async function pgCtxDelete() {
  _hidePageCtx();
  if (_blockedByRestriction()) return;
  const pageId = _ctxPageId;
  if (!pageId || !SESSION_ID || allPages.length <= 1) return;
  const idx = allPages.findIndex(p => p.id === pageId);
  if (!await showConfirm(window.t('confirm.delete_this_page', {n: idx + 1}),
                         window.t('confirm.delete_pages_title'),
                         window.t('confirm.delete'), 'btn-danger')) return;
  showLoading(window.t('toast.deleting_page'));
  try {
    const res = await apiFetch('/api/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session_id: SESSION_ID, page_id: pageId })
    });
    if (!res.ok) throw new Error(apiErr(await res.json()));
    // Land on the page that takes the deleted one's place (or the new last one).
    const next = allPages[idx + 1] || allPages[idx - 1];
    await _pgCtxReload(next ? next.id : '');
    showToast(window.t('toast.page_deleted'), 'success');
  } catch (err) {
    showToast(window.t('toast.delete_failed'), 'error');
  } finally { hideLoading(); }
}

async function pgCtxInsertBlank(before) {
  _hidePageCtx();
  if (_blockedByRestriction()) return;
  const pageId = _ctxPageId;
  if (!pageId || !SESSION_ID) return;
  // Match the reference page's size, which is what "insert a blank here" means
  // in a mixed-size document.
  const ref = allPages.find(p => p.id === pageId) || {};
  showLoading(window.t('toast.inserting_page'));
  try {
    const res = await apiFetch('/api/insert_blank_page', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        session_id: SESSION_ID, ref_page_id: pageId, insert_before: !!before,
        width_pts:  ref.width_pts  || 595.28,
        height_pts: ref.height_pts || 841.89,
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(apiErr(data));
    await _pgCtxReload((data.page && data.page.id) || pageId);
    showToast(window.t('toast.blank_page_inserted'), 'success');
  } catch (err) {
    showToast(window.t('toast.insert_failed'), 'error');
  } finally { hideLoading(); }
}

function _hideBookmarkCtx() {
  const menu = document.getElementById('bm-ctx-menu');
  if (menu) menu.style.display = 'none';
}

function renameBookmarkCtx() {
  _hideBookmarkCtx();
  if (_blockedByRestriction()) return;
  const bm = _bookmarks.find(b => b.id === _ctxBookmarkId);
  if (!bm) return;
  const title = window.prompt('Rename bookmark:', bm.title);
  if (title === null) return;
  const trimmed = title.trim();
  if (!trimmed || trimmed === bm.title) return;
  _renameBookmark(bm.id, trimmed);
}

async function _renameBookmark(id, title) {
  try {
    const res  = await _vFetch('/api/viewer/rename_bookmark', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, bookmark_id: id, title })
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    _setBookmarks(data.bookmarks);
    renderBookmarks();
  } catch (e) { showToast(window.t('toast.rename_failed') + e.message, 'error'); }
}

async function moveBookmark(direction) {
  if (_blockedByRestriction()) return;
  _hideBookmarkCtx();
  const id = _ctxBookmarkId;
  if (!id) return;
  try {
    const res  = await _vFetch('/api/viewer/move_bookmark', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, bookmark_id: id, direction })
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    _setBookmarks(data.bookmarks);
    renderBookmarks();
  } catch (e) { showToast(window.t('toast.move_failed') + e.message, 'error'); }
}

function deleteBookmarkCtx() {
  _hideBookmarkCtx();
  if (_blockedByRestriction()) return;
  if (_ctxBookmarkId) deleteBookmark(_ctxBookmarkId);
}

// ── Bulk delete (merged documents arrive with a mess of outline entries) ──────

async function deleteSelectedBookmarksCtx() {
  _hideBookmarkCtx();
  if (_blockedByRestriction()) return;
  // Walk _bookmarks, not the Set, so the ids sent are ones the server still has.
  const ids = _bookmarks.filter(b => _bmSelected.has(b.id)).map(b => b.id);
  if (!ids.length) return;
  const ok = await showConfirm(
    window.t('confirm.delete_selected_bookmarks', {count: ids.length}),
    window.t('confirm.delete_bookmarks_title'),
    window.t('confirm.delete'), 'btn-danger');
  if (!ok) return;
  _deleteBookmarks(ids, false);
}

async function deleteAllBookmarksCtx() {
  _hideBookmarkCtx();
  if (_blockedByRestriction()) return;
  const n = _bookmarks.length;
  if (!n) return;
  const ok = await showConfirm(
    window.t('confirm.delete_all_bookmarks', {count: n}),
    window.t('confirm.delete_bookmarks_title'),
    window.t('confirm.delete_all'), 'btn-danger');
  if (!ok) return;
  _deleteBookmarks([], true);
}

async function _deleteBookmarks(ids, all) {
  try {
    const res  = await _vFetch('/api/viewer/delete_bookmarks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, bookmark_ids: ids, all: !!all })
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    _setBookmarks(data.bookmarks);
    _bmSelected.clear();
    _bmAnchorId = null;
    renderBookmarks();
    const n = (data.deleted != null) ? data.deleted : (all ? 0 : ids.length);
    showToast(window.t('toast.bookmarks_deleted', {count: n}), 'success');
  } catch (e) { showToast(window.t('toast.delete_failed_2') + e.message, 'error'); }
}

// ── Auto-bookmarks (detect a contents page, else headings) ───────────────────
let _abProposed = [];

async function autoBookmarks() {
  if (_blockedByRestriction()) return;
  if (!SESSION_ID) { showToast(window.t('toast.open_a_document_first'), 'error'); return; }
  showLoading('Scanning document for bookmarks…');
  try {
    const res  = await _vFetch('/api/viewer/auto_bookmarks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID })
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    _abProposed = data.proposed || [];
    if (!_abProposed.length) {
      showToast(window.t('toast.no_contents_page_or_headings_detected'), 'info');
      return;
    }
    _openAutoBookmarkModal(data.source);
  } catch (e) {
    if (e.name !== 'AbortError') showToast(window.t('toast.auto_bookmark_failed') + e.message, 'error');
  } finally {
    hideLoading();
  }
}

function _openAutoBookmarkModal(source) {
  const sub  = document.getElementById('auto-bm-subtitle');
  const list = document.getElementById('auto-bm-list');
  if (sub) sub.innerHTML = source === 'toc'
    ? 'Found a <strong>contents page</strong>. Review the suggestions and add the ones you want — you can rename or reorder them afterwards.'
    : 'No contents page found, so these were detected from <strong>headings</strong> (by text size) and may be approximate. Pick the ones you want to keep.';
  if (list) {
    list.innerHTML = _abProposed.map((bm, i) => {
      const pg      = allPages.find(p => p.id === bm.page_id);
      const pageNum = pg ? pg.page_num : (bm.page_idx != null ? bm.page_idx + 1 : '?');
      const indent  = 4 + (bm.level || 0) * 16;
      return `<label class="ab-row" style="display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:6px;cursor:pointer">
        <input type="checkbox" class="ab-cb" data-i="${i}" checked style="flex-shrink:0">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:${indent}px">${escapeHtml(bm.title)}</span>
        <span style="flex-shrink:0;font-family:'DM Mono',monospace;font-size:10px;color:var(--text-dim)">p.${pageNum}</span>
      </label>`;
    }).join('');
  }
  _abUpdateCount();
  list.querySelectorAll('.ab-cb').forEach(cb => cb.addEventListener('change', _abUpdateCount));
  document.getElementById('auto-bm-modal').style.display = 'flex';
}

function _abUpdateCount() {
  const n = document.querySelectorAll('#auto-bm-list .ab-cb:checked').length;
  const c = document.getElementById('auto-bm-count');
  if (c) c.textContent = `${n} of ${_abProposed.length} selected`;
  const btn = document.getElementById('auto-bm-add-btn');
  if (btn) btn.disabled = n === 0;
}

function abSelectAll(check) {
  document.querySelectorAll('#auto-bm-list .ab-cb').forEach(cb => { cb.checked = check; });
  _abUpdateCount();
}

function closeAutoBookmarkModal() {
  document.getElementById('auto-bm-modal').style.display = 'none';
  _abProposed = [];
}

async function confirmAutoBookmarks() {
  const chosen = [...document.querySelectorAll('#auto-bm-list .ab-cb:checked')]
    .map(cb => _abProposed[parseInt(cb.dataset.i, 10)])
    .filter(Boolean)
    .map(bm => ({ title: bm.title, page_id: bm.page_id, y: bm.y, level: bm.level || 0 }));
  if (!chosen.length) return;
  try {
    const res  = await _vFetch('/api/viewer/add_bookmarks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, bookmarks: chosen })
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    _setBookmarks(data.bookmarks);
    closeAutoBookmarkModal();
    if (_leftMode !== 'bookmarks') setLeftMode('bookmarks'); else renderBookmarks();
    showToast(window.t('toast.added_bookmarks', {count: chosen.length}), 'success');
  } catch (e) { showToast(window.t('toast.failed_to_add') + e.message, 'error'); }
}

// Comments-list card label for each listed subtype (see _get_annotations).
const _COMMENT_TYPE_KEYS = {
  Text: 'comments.type_sticky', FreeText: 'comments.type_text',
  Highlight: 'comments.type_highlight', Square: 'comments.type_rect',
  Circle: 'comments.type_ellipse', Polygon: 'comments.type_cloud',
  PolyLine: 'comments.type_polyline', Ink: 'comments.type_ink',
  Underline: 'comments.type_underline', StrikeOut: 'comments.type_strikeout',
  Squiggly: 'comments.type_squiggly', Caret: 'comments.type_caret',
};
function _commentTypeLabel(ann) {
  if (ann.subtype === 'Line') {
    return window.t(ann.kind === 'darrow' ? 'comments.type_darrow'
                  : ann.kind === 'arrow'  ? 'comments.type_arrow'
                  : 'comments.type_line');
  }
  return window.t(_COMMENT_TYPE_KEYS[ann.subtype] || 'comments.type_markup');
}

// List filters.  '' = everything; _NO_AUTHOR / 'none' match an empty author /
// no status.  Kept across re-renders, dropped when the value is no longer
// present in the document (a filter nobody can see is a list that looks empty).
const _NO_AUTHOR = '\u0000';
let _commentFilter = { type: '', author: '', status: '' };
let _replyingTo = null;     // {page_id, annot_idx} while a reply box is open
let _replyDraft = '';       // its text, so a re-render does not lose it

function _commentTypeId(ann) {
  return ann.subtype === 'Line' ? (ann.kind || 'line') : ann.subtype;
}
function _commentState(ann) {
  return (ann.status && ann.status.state && ann.status.state !== 'None') ? ann.status.state : 'none';
}
function _fmtWhen(iso) {
  if (!iso) return '';
  try {
    const lang = (window.I18N && I18N.lang && I18N.lang()) || undefined;
    return new Date(iso).toLocaleString(lang, { dateStyle: 'medium', timeStyle: 'short' });
  } catch (_) { return ''; }
}
function _metaHtml(author, iso, extra) {
  const who  = author ? `<span class="who">${escapeHtml(author)}</span>` : '';
  const when = _fmtWhen(iso);
  const sep  = (who && when) ? '<span>·</span>' : '';
  return `<div class="comment-meta">${who}${sep}${when ? `<span>${escapeHtml(when)}</span>` : ''}${extra || ''}</div>`;
}
const _STATUS_KEYS = { none: 'comments.status_none', Accepted: 'comments.status_accepted',
                       Rejected: 'comments.status_rejected', Cancelled: 'comments.status_cancelled',
                       Completed: 'comments.status_completed' };

function _renderCommentFilters(items) {
  const el = document.getElementById('comment-filters');
  if (!el) return;
  if (!items.length) { el.innerHTML = ''; return; }
  const types = new Map(), authors = new Set(), states = new Set();
  items.forEach(({ ann }) => {
    types.set(_commentTypeId(ann), _commentTypeLabel(ann));
    authors.add(ann.author || _NO_AUTHOR);
    states.add(_commentState(ann));
  });
  // Drop a filter whose value has left the document.
  if (_commentFilter.type && !types.has(_commentFilter.type)) _commentFilter.type = '';
  if (_commentFilter.author && !authors.has(_commentFilter.author)) _commentFilter.author = '';
  if (_commentFilter.status && !states.has(_commentFilter.status)) _commentFilter.status = '';
  const opt = (v, label, cur) =>
    `<option value="${escapeHtml(v)}"${v === cur ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  const sel = (key, allKey, entries) =>
    `<select class="at-sel" onchange="setCommentFilter('${key}', this.value)" title="${escapeHtml(window.t(allKey))}">`
    + opt('', window.t(allKey), _commentFilter[key])
    + entries.map(([v, label]) => opt(v, label, _commentFilter[key])).join('') + '</select>';
  const byLabel = (x, y) => x[1].localeCompare(y[1]);
  el.innerHTML =
      sel('type', 'comments.filter_all_types', [...types.entries()].sort(byLabel))
    + sel('author', 'comments.filter_all_authors',
          [...authors].map(a => [a, a === _NO_AUTHOR ? window.t('comments.no_author') : a]).sort(byLabel))
    + sel('status', 'comments.filter_all_statuses',
          ['none', 'Accepted', 'Rejected', 'Cancelled', 'Completed'].filter(s => states.has(s))
            .map(s => [s, window.t(_STATUS_KEYS[s])]));
}

function setCommentFilter(key, value) {
  if (!(key in _commentFilter)) return;
  _commentFilter[key] = value || '';
  renderComments();
}

function _commentPasses(ann) {
  const f = _commentFilter;
  if (f.type && _commentTypeId(ann) !== f.type) return false;
  if (f.author && (ann.author || _NO_AUTHOR) !== f.author) return false;
  if (f.status && _commentState(ann) !== f.status) return false;
  return true;
}

function renderComments() {
  const list  = document.getElementById('comment-list');
  const badge = document.getElementById('rp-badge');

  // Everything listed — comments and review markup — across every page
  const items = [];
  allPages.forEach((p, pageIdx) => {
    (allAnnotations[p.id] || []).forEach((ann, annIdx) => items.push({ p, pageIdx, ann, annIdx }));
  });
  const total = items.length;

  _renderCommentFilters(items);
  if (!total) {
    list.innerHTML = `<div class="no-comments">${escapeHtml(window.t('comments.empty'))}</div>`;
    badge.style.display = 'none';
    return;
  }
  badge.style.display = '';
  badge.textContent = total;

  const shown = items.filter(({ ann }) => _commentPasses(ann));
  let html = '';
  if (shown.length !== total) {
    const fl = document.getElementById('comment-filters');
    if (fl) fl.innerHTML += `<div class="count">${escapeHtml(
      window.t('comments.showing', { shown: shown.length, total }))}</div>`;
  }
  if (!shown.length) {
    html = `<div class="no-comments">${escapeHtml(window.t('comments.none_match'))}</div>`;
  }
  const stop = 'event.stopPropagation();';
  shown.forEach(({ p, pageIdx, ann, annIdx }) => {
    const [r,g,b] = (ann.color || [1,0.92,0]).map(c => Math.round(Math.min(255,c*255)));
    const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;`
              + `background:rgb(${r},${g},${b});margin-right:6px;flex-shrink:0"></span>`;
    const isCurrentPage = p.id === currentPageId;
    const highlight = isCurrentPage ? 'border-color:color-mix(in srgb, var(--hl) 45%, transparent)' : '';
    const pageLbl = window.t('comments.page', { n: p.page_num });
    // A markup (shape, highlight…) usually has no text of its own — show
    // nothing rather than a "(no content)" placeholder for every one.
    const text = ann.text ? escapeHtml(ann.text)
               : (ann.markup ? '' : escapeHtml(window.t('comments.no_text')));
    const state = _commentState(ann);
    const chip = state === 'none' ? '' :
      `<span class="comment-status st-${state}" title="${escapeHtml(
        [ann.status.author, _fmtWhen(ann.status.modified || ann.status.created)].filter(Boolean).join(' · '))}">${
        escapeHtml(window.t(_STATUS_KEYS[state]))}</span>`;
    const pid = escapeHtml(p.id);
    const aidx = Number(ann.annot_idx);
    const canThread = Number.isInteger(aidx);
    const replies = (ann.replies || []).map(rp => `<div class="comment-reply">${
        _metaHtml(rp.author, rp.created || rp.modified,
          canThread ? `<button class="comment-reply-del" onclick="${stop}commentDeleteReply('${pid}',${Number(rp.annot_idx)})" title="${escapeHtml(window.t('comments.delete_reply'))}">&times;</button>` : '')}
        <div class="comment-text">${escapeHtml(rp.text || '')}</div></div>`).join('');
    const replying = _replyingTo && _replyingTo.page_id === p.id && _replyingTo.annot_idx === aidx;
    html += `<div class="comment-card" style="cursor:pointer;${highlight}"
         onclick="goToAnnotation('${pid}',${pageIdx},${annIdx})"
         title="${escapeHtml(window.t('comments.card_tip'))}">
      <div class="comment-page" style="display:flex;align-items:center">
        ${dot}${escapeHtml(pageLbl)} &mdash; ${escapeHtml(_commentTypeLabel(ann))}${chip}
      </div>
      ${(ann.author || ann.created || ann.modified) ? _metaHtml(ann.author, ann.created || ann.modified) : ''}
      ${text ? `<div class="comment-text">${text}</div>` : ''}
      ${replies ? `<div class="comment-replies">${replies}</div>` : ''}
      ${canThread ? `<div class="comment-actions" onclick="${stop}">
        <button class="comment-act" onclick="${stop}commentReplyStart('${pid}',${aidx})">${escapeHtml(window.t('comments.reply'))}</button>
        <select class="at-sel comment-status-sel" onclick="${stop}" onchange="commentSetStatus('${pid}',${aidx},this.value)" title="${escapeHtml(window.t('comments.status_tip'))}">
          ${['None', 'Accepted', 'Rejected', 'Cancelled', 'Completed'].map(s =>
            `<option value="${s}"${(s === 'None' ? 'none' : s) === state ? ' selected' : ''}>${
              escapeHtml(window.t(_STATUS_KEYS[s === 'None' ? 'none' : s]))}</option>`).join('')}
        </select>
      </div>` : ''}
      ${replying ? `<div class="comment-reply-box" onclick="${stop}">
        <textarea id="comment-reply-text" placeholder="${escapeHtml(window.t('comments.reply_ph'))}"
                  oninput="_replyDraft = this.value"
                  onkeydown="if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); commentReplyPost(); } else if (event.key === 'Escape') { event.stopPropagation(); commentReplyCancel(); }"></textarea>
        <div class="row">
          <button class="comment-act" onclick="${stop}commentReplyCancel()">${escapeHtml(window.t('comments.cancel'))}</button>
          <button class="comment-act" onclick="${stop}commentReplyPost()">${escapeHtml(window.t('comments.post_reply'))}</button>
        </div>
      </div>` : ''}
    </div>`;
  });

  // Re-rendering must not eat a half-typed reply or its focus.
  const hadFocus = document.activeElement && document.activeElement.id === 'comment-reply-text';
  list.innerHTML = html;
  const ta = document.getElementById('comment-reply-text');
  if (ta) {
    ta.value = _replyDraft;
    if (hadFocus || _replyFocusPending) {
      _replyFocusPending = false;
      try { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); } catch (_) {}
    }
  }
}

// ── Replies and status ──────────────────────────────────────────────────────
let _replyFocusPending = false;
function commentReplyStart(page_id, annot_idx) {
  if (_blockedByRestriction()) return;
  if (!_replyingTo || _replyingTo.page_id !== page_id || _replyingTo.annot_idx !== annot_idx) {
    _replyDraft = '';
  }
  _replyingTo = { page_id, annot_idx };
  _replyFocusPending = true;
  renderComments();
}
function commentReplyCancel() {
  _replyingTo = null; _replyDraft = '';
  renderComments();
}
function commentReplyPost() {
  if (!_replyingTo) return;
  const text = (_replyDraft || '').trim();
  if (!text) { showToast(window.t('toast.type_a_reply_first'), 'error'); return; }
  const { page_id, annot_idx } = _replyingTo;
  _replyingTo = null; _replyDraft = '';
  return _commentThreadOp(page_id, { action: 'reply', annot_idx, text }, 'toast.reply_added');
}
function commentSetStatus(page_id, annot_idx, state) {
  if (_blockedByRestriction()) { renderComments(); return; }
  return _commentThreadOp(page_id, { action: 'status', annot_idx, state }, 'toast.status_set');
}
function commentDeleteReply(page_id, annot_idx) {
  if (_blockedByRestriction()) return;
  return _commentThreadOp(page_id, { action: 'delete_reply', annot_idx }, 'toast.reply_deleted');
}

// One queued mutation, like every other annotation change (CLAUDE.md, "Viewer
// annotation mutations"): the thread routes push their own undo snapshot.
function _commentThreadOp(page_id, body, okKey) {
  return _queueAnnotOp(() => _vFetch('/api/viewer/comment_thread', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.assign({ session_id: SESSION_ID, page_id,
                                         author: _reviewer() }, body)),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    vRefreshHistory(data);
    allAnnotations[page_id] = data.annotations || [];
    if (page_id === currentPageId) pageAnnotations = allAnnotations[page_id];
    // Deleting a reply shifts the /Annots positions after it.
    if (data.editable) _editableAnnots[page_id] = data.editable;
    drawPageAnnotations(page_id);
    _refreshEditHitAreas();
    renderComments();
    showToast(window.t(okKey), 'success');
  }))
  .catch(err => { renderComments(); showToast(window.t('toast.failed') + err.message, 'error'); });
}

async function goToAnnotation(page_id, pageIdx, annIdx) {
  goToPage(pageIdx);
  const st = _pageBlocks.get(page_id);
  if (!st?.rendered) await new Promise(r => setTimeout(r, 700));

  _annotHighlight = { page_id, annIdx };
  drawPageAnnotations(page_id);

  const ann = (allAnnotations[page_id] || [])[annIdx];
  if (ann) {
    const pst = _pageBlocks.get(page_id);
    if (pst?.rendered) {
      const pd    = { pageH: pst.pageH, renderScale: pst.renderScale };
      const d     = pdfToDisplay({x: ann.x, y: ann.y, w: ann.w, h: ann.h}, pd);
      const block = document.getElementById('page-block-' + page_id);
      const area  = document.getElementById('canvas-area');
      if (block && area) {
        area.scrollTop  = block.offsetTop  + d.y + d.h / 2 - area.clientHeight / 2;
        area.scrollLeft = block.offsetLeft + d.x + d.w / 2 - area.clientWidth  / 2;
      }
    }
  }
  setTimeout(() => { _annotHighlight = null; drawPageAnnotations(page_id); }, 2500);
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Select / edit / delete existing annotations (lift-to-draft) ──────────────
// Clicking a committed annotation (in pointer mode) re-opens it as a live draft
// using the same machinery as drawing: the original is removed from the page and
// the draft is committed afresh on click-away.  Delete removes it permanently.

async function loadEditableAnnots(page_id) {
  try {
    const res  = await _vFetch('/api/viewer/editable_annotations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, page_id })
    });
    const data = await res.json();
    _editableAnnots[page_id] = data.annotations || [];
  } catch (_) { _editableAnnots[page_id] = []; }
  drawEditableHitAreas(page_id);
}

// Edit hit-areas are only "live" in pointer mode with no draw tool armed and no
// draft in progress — otherwise drawing/selection would conflict.
function _editSelectActive() {
  return viewerTool === 'pointer' && !_activeAnnotTool && !_draft;
}

function drawEditableHitAreas(page_id) {
  const st = _pageBlocks.get(page_id);
  if (!st || !st.editOv) return;
  const ov = st.editOv;
  while (ov.firstChild) ov.removeChild(ov.firstChild);
  ov.style.pointerEvents = 'none';        // root never blocks; child hit-areas opt-in
  const active = _editSelectActive();
  const list = _editableAnnots[page_id] || [];
  if (!list.length || !st.rendered) return;
  const scale = st.renderScale * zoomLevel, pageH = st.pageH;

  list.forEach(info => {
    // Comments (sticky notes + comment-list FreeText) keep their text/author
    // popup — only non-comment markup uses lift-to-draft editing.
    if (info.tool === 'sticky') return;
    if (info.tool === 'text' && info.is_comment) return;
    let el;
    if (_isLineTool(info.tool)) {
      if (!info.line) return;
      const [x1, y1, x2, y2] = info.line;
      el = document.createElementNS(NS_SVG, 'line');
      el.setAttribute('x1', x1 * scale); el.setAttribute('y1', (pageH - y1) * scale);
      el.setAttribute('x2', x2 * scale); el.setAttribute('y2', (pageH - y2) * scale);
      el.setAttribute('stroke', 'transparent');
      el.setAttribute('stroke-width', '10');
    } else {
      if (!info.rect) return;
      const r = info.rect;
      const rx1 = Math.min(r[0], r[2]), ry1 = Math.min(r[1], r[3]);
      const rx2 = Math.max(r[0], r[2]), ry2 = Math.max(r[1], r[3]);
      el = document.createElementNS(NS_SVG, 'rect');
      el.setAttribute('x', rx1 * scale); el.setAttribute('y', (pageH - ry2) * scale);
      el.setAttribute('width', (rx2 - rx1) * scale);
      el.setAttribute('height', (ry2 - ry1) * scale);
      el.setAttribute('fill', 'transparent');
    }
    el.style.pointerEvents = active ? 'all' : 'none';
    el.style.cursor = 'pointer';
    el.addEventListener('mousedown', (e) => {
      if (!_editSelectActive()) return;
      e.preventDefault(); e.stopPropagation();
      selectAnnotation(info, page_id);
    });
    ov.appendChild(el);
  });
}

function selectAnnotation(info, page_id) {
  liftAnnotation(info, page_id);   // sticky notes are filtered out before reaching here
}

// Lift = remove the committed annotation + re-render the page, THEN rebuild it as
// a live draft. We build the draft only after re-render because re-rendering
// wipes the page block (which would destroy a draft overlay attached to it).
function liftAnnotation(info, page_id) {
  const st = _pageBlocks.get(page_id);
  if (!st || !st.rendered) return;
  // Image stamps: the embedded image is fetched on demand (kept out of the bulk
  // editable payload). Load it first, then re-enter to build the draft.
  if (info.tool === 'image' && !info.imageData) {
    _vFetch('/api/viewer/stamp_image', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, page_id, annot_idx: info.annot_idx })
    })
    .then(r => r.json())
    .then(d => {
      if (d.error || !d.image) throw new Error(d.error || 'No image');
      info.imageData = d.image;
      liftAnnotation(info, page_id);
    })
    .catch(err => showToast(window.t('toast.could_not_edit_image') + err.message, 'error'));
    return;
  }
  commitDraft();                            // commit any other in-progress draft first
  _pendingLift = { info, page_id };         // built once the page re-renders
  if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);

  _queueAnnotOp(() => _vFetch('/api/viewer/delete_annotation', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: SESSION_ID, page_id, annot_idx: info.annot_idx })
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    vRefreshHistory(data);
    _editableAnnots[page_id] = data.editable || [];
    allAnnotations[page_id]  = data.annotations || [];
    if (page_id === currentPageId) pageAnnotations = allAnnotations[page_id];
    _invalidateViewerPage(page_id);         // re-render → img.onload builds the draft
  }))
  .catch(err => { _pendingLift = null; showToast(window.t('toast.could_not_edit') + err.message, 'error'); });
}

// Build the live draft for a lifted annotation. Called from renderPageBlock's
// onload once the page (minus the original) has re-rendered.
function _buildLiftedDraft(info, page_id) {
  const st = _pageBlocks.get(page_id);
  if (!st || !st.rendered) return;
  const scale = st.renderScale * zoomLevel, pageH = st.pageH;
  const isLine = _isLineTool(info.tool);

  const svg = document.createElementNS(NS_SVG, 'svg');
  svg.setAttribute('class', 'annot-draft');
  svg.style.cssText = 'position:absolute;top:0;left:0;z-index:500;pointer-events:none';
  svg.setAttribute('width',  st.block.offsetWidth);
  svg.setAttribute('height', st.block.offsetHeight);
  st.block.appendChild(svg);

  const style = {
    textColor: info.text_color || [0, 0, 0],
    fontSize:  info.font_size || 11,
    lineColor: info.border_color || (info.tool === 'highlight' ? [1, 0.92, 0] : [1, 0, 0]),
    lineWidth: (info.border_width != null ? info.border_width : 1),
    lineStyle: info.border_style || 'solid',
    textBox:   (info.tool === 'text' ? (info.border_width || 0) > 0 : true),
    fillOn:    !!info.fill_color,                 // restore rect/ellipse interior fill
    fillColor: info.fill_color || [1, 1, 0],
    font:      info.font || 'Helvetica',
  };

  _draft = {
    tool: info.tool, page_id, st, block: st.block,
    scale, kind: isLine ? 'line' : 'box',
    x: 0, y: 0, w: 0, h: 0, x1: 0, y1: 0, x2: 0, y2: 0,
    svg, style, textInput: null,
    isComment: (info.tool === 'text' ? !!info.is_comment : false),
    author: info.author || '',
    created: info.created || null,                      // keeps its creation date
    replies: info.replies || [],                        // ...and its thread
    imageData: info.imageData || null,                  // image stamps: re-editable
    lifted: true, fromIdx: null, fromPageId: page_id,   // original already deleted
  };

  if (isLine) {
    _draft.x1 = info.line[0] * scale; _draft.y1 = (pageH - info.line[1]) * scale;
    _draft.x2 = info.line[2] * scale; _draft.y2 = (pageH - info.line[3]) * scale;
  } else {
    const r = info.rect;
    const rx1 = Math.min(r[0], r[2]), ry1 = Math.min(r[1], r[3]);
    const rx2 = Math.max(r[0], r[2]), ry2 = Math.max(r[1], r[3]);
    _draft.x = rx1 * scale; _draft.y = (pageH - ry2) * scale;
    _draft.w = (rx2 - rx1) * scale; _draft.h = (ry2 - ry1) * scale;
  }

  _syncToolControlsFromStyle(style, info.tool);   // reflect the lifted style in the panel
  if (info.tool === 'text') {
    _attachTextInput();
    if (_draft.textInput) { _draft.textInput.value = info.text || ''; _fitTextHeight(); }
  }
  // Snapshot for Esc-restore (revert any edits back to the original).
  _draft.snap = {
    x: _draft.x, y: _draft.y, w: _draft.w, h: _draft.h,
    x1: _draft.x1, y1: _draft.y1, x2: _draft.x2, y2: _draft.y2,
    style: JSON.parse(JSON.stringify(style)), text: info.text || '',
  };
  _draftRender(); _syncTextInput();
  _updateAnnotDeleteBtn();
}

// Push a lifted annotation's style into the docked tool controls so subsequent
// live edits start from its current values.
function _syncToolControlsFromStyle(style, tool) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  const toHex = (c) => '#' + c.map(x => Math.round(Math.max(0, Math.min(1, x)) * 255)
                                       .toString(16).padStart(2, '0')).join('');
  if (tool === 'text') {
    set('at-text-color', toHex(style.textColor));
    set('at-text-size', Math.round(style.fontSize));
    if (style.font) set('at-text-font', style.font);
    const box = document.getElementById('at-text-box');
    if (box) box.checked = !!style.textBox;
  }
  // Stroke controls only matter for tools that actually draw a stroke. A
  // strokeless lifted annotation (image stamp, or a no-box text) reports
  // lineWidth 0 — never copy that into the shared Line weight, or every shape
  // drawn afterwards would commit with weight 0 (→ no stroke colour → black).
  if (tool !== 'image' && style.lineWidth > 0) {
    set('at-line-color',  toHex(style.lineColor));
    set('at-line-weight', style.lineWidth);
    set('at-line-style',  style.lineStyle || 'solid');
  }
  // Fill (rect/ellipse) — reflect the lifted shape's interior so a re-commit keeps it.
  const fillOnEl = document.getElementById('at-fill-on');
  if (fillOnEl) fillOnEl.checked = !!style.fillOn;
  if (style.fillColor) set('at-fill-color', toHex(style.fillColor));
}

// Permanently delete the selected/lifted annotation (already removed server-side
// on lift, so we just discard the draft without re-committing).
function deleteSelectedAnnotation() {
  if (!_draft) return false;
  const pid = _draft.fromPageId || _draft.page_id;
  const idx = _draft.fromIdx;
  _removeDraft();
  // If the lift's background delete hasn't completed yet, do it now.
  if (idx != null && pid) {
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    _queueAnnotOp(() => _vFetch('/api/viewer/delete_annotation', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, page_id: pid, annot_idx: idx })
    }).then(r => r.json()).then(data => {
      if (!data.error) {
        vRefreshHistory(data);
        _editableAnnots[pid] = data.editable || [];
        allAnnotations[pid] = data.annotations || [];
        _invalidateViewerPage(pid);
      }
    })).catch(() => {});
  }
  showToast(window.t('toast.annotation_deleted'), 'success');
  return true;
}

// ── Annotation drawing tools (live draw → edit → commit) ─────────────────────
// A drawn shape becomes a LIVE draft (client-side SVG with move/resize handles)
// whose style updates live from the docked tool panel.  It is committed to the
// PDF as a real annotation only when the user clicks away (another tool, empty
// page area, or any UI outside the tool panel).  Shapes (Square/Circle/Line)
// are intentionally not added to the comments table — only FreeText text notes.
const NS_SVG = 'http://www.w3.org/2000/svg';
let _activeAnnotTool = null;   // 'text'|'ellipse'|'rect'|'line'|'arrow'|'darrow'|null
let _draft           = null;   // live draft being drawn/edited
let _drawing         = null;   // transient state while dragging out a new shape
// EVERY annotation mutation is serialized through this promise chain — draws,
// sticky notes, signatures, popup edits, deletes, lifts, pastes, and undo/redo
// itself.  Two reasons, and the second one is what made undo look broken:
//
//  * a boolean re-entrancy guard silently DROPS the commit when the user starts
//    the next shape while the previous POST is still in flight (fast
//    draw-draw-draw), leaving the old draft live so the new drag mutates or
//    deletes it.  The chain never drops work.
//  * the server records one undo step per mutation in ARRIVAL order, so
//    anything fired without queueing can overtake a commit that is still in
//    flight.  Drawing a shape and immediately pressing Ctrl+Z did exactly that:
//    commitDraft() only QUEUES the add, so the undo POST overtook it and
//    reversed the PREVIOUS annotation while the new one landed on top — the
//    "undo removed lines out of order" report.  (The server holds the session
//    lock across each mutation, so nothing is corrupted either way; what this
//    chain buys is that the order matches what the user did.)
let _commitChain     = Promise.resolve();

// Queue one annotation mutation behind every mutation already in flight.
// Returns the caller's own promise so it can await/catch its result; the chain
// itself swallows failures so one bad request can't wedge everything after it.
function _queueAnnotOp(fn) {
  const run = _commitChain.then(fn, fn);
  _commitChain = run.then(() => {}, () => {});
  return run;
}

// Annotation tools, then the page tools.  A PAGE tool changes the page itself
// rather than laying an object on top of it, so it does not go through the
// draft/commitDraft machinery — see _annotMouseDown, which hands 'formfield'
// off to its own drag before any draft is created.
const _TOOL_IDS = ['text', 'ellipse', 'rect', 'line', 'arrow', 'darrow', 'cloud', 'highlight', 'sticky', 'image', 'signature',
                   'formfield', 'redact', 'watermark', 'crop',
                   'pagetext', 'edittext'];
// Watermark is a page tool but NOT a drag tool: it takes no geometry (the
// server positions it from the page's own centre), so it is not in
// _PAGE_TOOL_DRAG and _annotMouseDown must not start a drag for it.
const _PAGE_TOOL_IDS = ['formfield', 'redact', 'watermark', 'crop',
                        'pagetext', 'edittext'];
function _isPageTool(tool) { return _PAGE_TOOL_IDS.indexOf(tool) !== -1; }

// The three two-endpoint tools: plain line, single arrowhead, double-ended
// arrow. They share one draft shape ('line' kind) and one PDF subtype (/Line),
// differing only in which ends carry a head.
function _isLineTool(tool) {
  return tool === 'line' || tool === 'arrow' || tool === 'darrow';
}

function _hexToRgb01(hex) {
  const m = /^#?([0-9a-fA-F]{6})$/.exec(hex || '');
  if (!m) return [0, 0, 0];
  const n = parseInt(m[1], 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}
function _rgbCss(c) {
  return `rgb(${Math.round(c[0]*255)},${Math.round(c[1]*255)},${Math.round(c[2]*255)})`;
}
function _ctlVal(id, def) {
  const el = document.getElementById(id);
  return el ? el.value : def;
}
function _toolStyle() {
  const boxEl  = document.getElementById('at-text-box');
  const fillEl = document.getElementById('at-fill-on');
  return {
    textColor: _hexToRgb01(_ctlVal('at-text-color', '#000000')),
    fontSize:  Math.min(96, Math.max(4, parseFloat(_ctlVal('at-text-size', '11')) || 11)),
    lineColor: _hexToRgb01(_ctlVal('at-line-color', '#ff0000')),
    lineWidth: Math.min(20, Math.max(0.5, parseFloat(_ctlVal('at-line-weight', '1')) || 0.5)),
    lineStyle: _ctlVal('at-line-style', 'solid'),   // 'solid'|'dashed'|'dotted'
    textBox:   !!(boxEl && boxEl.checked),   // draw a border box around text?
    fillOn:    !!(fillEl && fillEl.checked), // fill rect/ellipse interior?
    fillColor: _hexToRgb01(_ctlVal('at-fill-color', '#ffff00')),
    font:      _ctlVal('at-text-font', 'Helvetica'),
  };
}

// Map a PDF base-14 font name → CSS for the inline text-editor preview.
function _pdfFontToCss(name) {
  const n = String(name || 'Helvetica');
  let family = 'Helvetica, Arial, sans-serif';
  if (/^Times/.test(n))   family = '"Times New Roman", Times, serif';
  else if (/^Courier/.test(n)) family = '"Courier New", Courier, monospace';
  const weight = /Bold/.test(n) ? '700' : '400';
  const style  = /(Italic|Oblique)/.test(n) ? 'italic' : 'normal';
  return { family, weight, style };
}

// Button click: toggle off if same tool, else commit current draft and arm.
function selectAnnotTool(tool) {
  if (_blockedByRestriction()) return;
  if (tool === 'image') { _pickImage(); return; }   // choose a file first, then arm
  if (_activeAnnotTool === tool) { commitDraft(); _disarmTool(); return; }
  commitDraft();
  _armTool(tool);
}

// Image tool: pick a file, then arm so the next drag places it as a Stamp.
let _pendingImage = null;        // {dataUrl, aspect} once a file is chosen
function _pickImage() {
  const inp = document.getElementById('at-image-input');
  if (!inp) return;
  inp.value = '';
  inp.onchange = () => {
    const file = inp.files && inp.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        _pendingImage = { dataUrl: reader.result,
                          aspect: img.naturalWidth / (img.naturalHeight || 1) };
        commitDraft();
        _armTool('image');
      };
      img.onerror = () => showToast(window.t('toast.could_not_read_that_image'), 'error');
      img.src = reader.result;
    };
    reader.onerror = () => showToast(window.t('toast.could_not_read_that_file'), 'error');
    reader.readAsDataURL(file);
  };
  inp.click();
}

// ── Paste an image from the system clipboard ────────────────────────────────
// Ctrl+V with a bitmap on the clipboard drops it onto the page as a live image
// draft — the same object the Image tool builds after a drag — so it moves,
// resizes, Escapes and commits through the existing draft machinery. Nothing
// reaches the PDF until the draft is committed (click away / Enter).

// A pasted image spans at most this fraction of the page in either axis. The
// cap is the point of the conversion below: a 4K screenshot is ~2880pt wide and
// would otherwise land mostly off a 612pt page with its handles unreachable.
const _PASTE_PAGE_FILL = 0.92;

// The clipboard bitmap, or null. Chromium exposes a copied bitmap through
// `items`; a file copied in Explorer/Finder arrives through `files` instead.
function _clipboardImageFile(e) {
  const dt = e.clipboardData;
  if (!dt) return null;
  for (const it of (dt.items || [])) {
    if (it.kind === 'file' && /^image\//.test(it.type || '')) {
      const f = it.getAsFile();
      if (f) return f;
    }
  }
  for (const f of (dt.files || [])) {
    if (/^image\//.test(f.type || '')) return f;
  }
  return null;
}

// Decode the blob, then hand its natural pixel size to _placePastedImage.
function _pasteImageFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    const img = new Image();
    img.onload  = () => _placePastedImage(reader.result, img.naturalWidth, img.naturalHeight);
    img.onerror = () => showToast(window.t('toast.could_not_read_that_image'), 'error');
    img.src = reader.result;
  };
  reader.onerror = () => showToast(window.t('toast.could_not_read_that_file'), 'error');
  reader.readAsDataURL(file);
}

function _placePastedImage(dataUrl, natW, natH) {
  if (!SESSION_ID || !allPages.length) {
    showToast(window.t('toast.open_a_document_first'), 'error'); return;
  }
  if (!natW || !natH) {
    showToast(window.t('toast.could_not_read_that_image'), 'error'); return;
  }

  // Target = the page under the cursor, else the current page (centred).
  let page_id = null, cx = null, cy = null;
  const pb = _lastMouse && _hitTestPageBlock(_lastMouse.x, _lastMouse.y);
  if (pb) {
    const pid = pb.dataset.pageId;
    if (_pageBlocks.get(pid)?.rendered) {
      const r = pb.getBoundingClientRect();
      page_id = pid;
      cx = _lastMouse.x - r.left; cy = _lastMouse.y - r.top;
    }
  }
  if (!page_id) page_id = currentPageId;
  const st = _pageBlocks.get(page_id);
  if (!st?.rendered || !st.block) {
    showToast(window.t('toast.paste_failed') + 'page not ready', 'error'); return;
  }

  commitDraft();          // accept whatever was being edited first
  _clearAreaSelection();  // selection rings would sit under the new draft

  // The page block is exactly the canvas box, so its offset dims ARE the page
  // in display px — draft geometry lives in that same frame.
  const blockW = st.block.offsetWidth, blockH = st.block.offsetHeight;
  const scale  = st.renderScale * zoomLevel;
  // Clipboard bitmaps are screen pixels; read them as 96 dpi → PDF points,
  // then into display px.
  let w = natW * 0.75 * scale, h = natH * 0.75 * scale;
  // Shrink (never enlarge) to fit the page, aspect preserved — a small icon
  // keeps its natural size, an oversized capture is scaled down to fit.
  const fit = Math.min(1, (blockW * _PASTE_PAGE_FILL) / w,
                          (blockH * _PASTE_PAGE_FILL) / h);
  w *= fit; h *= fit;

  // Centre on the cursor, then clamp so the whole box (and its handles) is on
  // the page.
  let x = (cx !== null ? cx : blockW / 2) - w / 2;
  let y = (cy !== null ? cy : blockH / 2) - h / 2;
  x = Math.min(Math.max(x, 0), Math.max(0, blockW - w));
  y = Math.min(Math.max(y, 0), Math.max(0, blockH - h));

  const svg = document.createElementNS(NS_SVG, 'svg');
  svg.setAttribute('class', 'annot-draft');
  svg.style.cssText = 'position:absolute;top:0;left:0;z-index:500;pointer-events:none';
  svg.setAttribute('width', blockW); svg.setAttribute('height', blockH);
  st.block.appendChild(svg);

  _draft = {
    tool: 'image', page_id, st, block: st.block, scale, kind: 'box',
    x, y, w, h, x1: 0, y1: 0, x2: 0, y2: 0,
    svg, style: _toolStyle(), textInput: null,
    isComment: false, imageData: dataUrl,
  };
  _draftRender();
  // Bring the target page into view — the cursor may be nowhere near it when
  // the paste fell back to the current page.
  const tIdx = allPages.findIndex(pg => pg.id === page_id);
  if (tIdx >= 0 && page_id !== currentPageId) goToPage(tIdx);
  showToast(window.t('toast.image_pasted_drag_to_place'), 'success');
}

// Sticky note colour-name → PDF rgb (0..1). Mirrors editor's ANNOT_COLOR_MAP.
const _STICKY_COLORS = {
  yellow: [1.0, 0.92, 0.0],
  green:  [0.0, 0.80, 0.2],
  blue:   [0.36, 0.73, 1.0],
  red:    [1.0, 0.30, 0.3],
  purple: [0.70, 0.30, 1.0],
};

function _armTool(tool) {
  // Arming a drawing tool must run under the pointer tool. The hand tool's
  // mousedown starts a pan drag on the same left-button press that begins the
  // shape, so both fire at once and the page slides around while you draw.
  // Switch to pointer so _onViewerMouseDown defers to _annotMouseDown.
  if (viewerTool !== 'pointer') setViewerTool('pointer');
  _activeAnnotTool = tool;
  if (_rightMode !== _tabForTool(tool)) setRightMode(_tabForTool(tool));
  if (tool !== 'image') _pendingImage = null;   // image keeps its picked file
  _TOOL_IDS.forEach(t => {
    const btn = document.getElementById('at-tool-' + t);
    if (btn) btn.classList.toggle('active', t === tool);
  });
  // Sticky / Signature tools expose their own fields; both place on a click.
  const stickyProps = document.getElementById('at-sticky-props');
  if (stickyProps) stickyProps.style.display = (tool === 'sticky') ? '' : 'none';
  const sigProps = document.getElementById('at-sig-props');
  if (sigProps) sigProps.style.display = (tool === 'signature') ? '' : 'none';
  if (tool === 'signature') _sigPreview();
  const ffProps = document.getElementById('at-ff-props');
  if (ffProps) ffProps.style.display = (tool === 'formfield') ? '' : 'none';
  _ffRefreshOverlays();
  if (tool === 'formfield') document.addEventListener('keydown', _ffKey);
  else                      document.removeEventListener('keydown', _ffKey);
  const rxProps = document.getElementById('at-redact-props');
  if (rxProps) rxProps.style.display = (tool === 'redact') ? '' : 'none';
  const wmProps = document.getElementById('at-wm-props');
  if (wmProps) wmProps.style.display = (tool === 'watermark') ? '' : 'none';
  if (tool === 'watermark') { wmViewerOpacityLabel(); wmViewerPreview(); }
  else _wmClearPreview();
  const crProps = document.getElementById('at-crop-props');
  if (crProps) crProps.style.display = (tool === 'crop') ? '' : 'none';
  const ptProps = document.getElementById('at-pagetext-props');
  if (ptProps) ptProps.style.display = (tool === 'pagetext') ? '' : 'none';
  const etProps = document.getElementById('at-edittext-props');
  if (etProps) etProps.style.display = (tool === 'edittext') ? '' : 'none';
  if (tool === 'edittext') _etArm(); else _etDisarm();
  _refreshStyleRows();
  const area = document.getElementById('canvas-area');
  area.classList.add('annot-draw-mode');
  area.addEventListener('mousedown', _annotMouseDown);
  document.addEventListener('keydown', _annotEscKey);
  _clearAreaSelection();         // selection rings would conflict with drawing
  _refreshEditHitAreas();        // disable select hit-areas while a tool is armed
  _refreshLinkOverlays();        // ...and links, so a drag over one still draws
  const armKey = { sticky: 'toast.arm_sticky', image: 'toast.arm_image',
                   signature: 'toast.arm_signature', formfield: 'toast.arm_formfield',
                   redact: 'toast.arm_redact', watermark: 'toast.arm_watermark',
                   crop: 'toast.arm_crop', pagetext: 'toast.arm_pagetext',
                   edittext: 'toast.arm_edittext' }[tool] || 'toast.arm_draw';
  showToast(window.t(armKey), 'info');
}

function _disarmTool() {
  _activeAnnotTool = null;
  _pendingImage = null;
  _TOOL_IDS.forEach(t => {
    const btn = document.getElementById('at-tool-' + t);
    if (btn) btn.classList.remove('active');
  });
  const stickyProps = document.getElementById('at-sticky-props');
  if (stickyProps) stickyProps.style.display = 'none';
  const sigProps = document.getElementById('at-sig-props');
  if (sigProps) sigProps.style.display = 'none';
  const ffProps = document.getElementById('at-ff-props');
  if (ffProps) ffProps.style.display = 'none';
  _ffSel = null;
  document.removeEventListener('keydown', _ffKey);
  _ffRefreshOverlays();
  const rxProps = document.getElementById('at-redact-props');
  if (rxProps) rxProps.style.display = 'none';
  const wmProps = document.getElementById('at-wm-props');
  if (wmProps) wmProps.style.display = 'none';
  _wmClearPreview();
  const crProps = document.getElementById('at-crop-props');
  if (crProps) crProps.style.display = 'none';
  const ptProps = document.getElementById('at-pagetext-props');
  if (ptProps) ptProps.style.display = 'none';
  const etProps = document.getElementById('at-edittext-props');
  if (etProps) etProps.style.display = 'none';
  _etDisarm();
  _ptCancelEntry();
  _refreshStyleRows();
  const area = document.getElementById('canvas-area');
  area.classList.remove('annot-draw-mode');
  area.removeEventListener('mousedown', _annotMouseDown);
  document.removeEventListener('keydown', _annotEscKey);
  _refreshEditHitAreas();        // re-enable select hit-areas
  _refreshLinkOverlays();        // ...and links
}

function _annotEscKey(e) {
  if (e.key !== 'Escape') return;
  if (!_draft) { _disarmTool(); showToast(window.t('toast.tool_off'), 'info'); }
}

// Redraw editable hit-areas on all rendered pages (their active/inactive state
// depends on the current tool/draft mode).
function _refreshEditHitAreas() {
  _pageBlocks.forEach((st, pid) => { if (st.rendered) drawEditableHitAreas(pid); });
}

// Global keys for a live draft: Esc cancels (restores original if lifted),
// Enter commits/accepts, Delete/Backspace removes it. Ignored while typing in
// the inline text editor (there Enter is a newline; click away or Esc instead).
document.addEventListener('keydown', (e) => {
  // Esc with no draft: dismiss any area selection of annotations.
  if (e.key === 'Escape' && !_draft && _areaSelection) { _clearAreaSelection(); return; }
  if (!_draft) return;
  const ae = document.activeElement;
  const typing = ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable);
  if (e.key === 'Escape') {
    e.preventDefault();
    if (_draft.lifted) { _restoreLiftedOriginal(); showToast(window.t('toast.edit_cancelled'), 'info'); }
    else               { _removeDraft(); showToast(window.t('toast.cancelled'), 'info'); }
  } else if (e.key === 'Enter' && !typing) {
    e.preventDefault();
    commitDraft();                 // accept the placement / edit
  } else if ((e.key === 'Delete' || e.key === 'Backspace') && !typing) {
    e.preventDefault();
    deleteSelectedAnnotation();
  }
});

function _restoreLiftedOriginal() {
  if (!_draft || !_draft.snap) { _removeDraft(); return; }
  const sn = _draft.snap;
  Object.assign(_draft, { x: sn.x, y: sn.y, w: sn.w, h: sn.h,
                          x1: sn.x1, y1: sn.y1, x2: sn.x2, y2: sn.y2,
                          style: JSON.parse(JSON.stringify(sn.style)) });
  if (_draft.textInput) _draft.textInput.value = sn.text;
  commitDraft();   // re-create the original unchanged
}

// The shared Text / Line / Fill style rows, and the tools each one styles.  They
// follow the armed tool, or — with nothing armed — the annotation being edited,
// so the panel never shows controls that do nothing (and Add text, which has its
// own font controls, never shows a second Font row under them).
const _STYLE_ROWS = {
  'at-text-props': ['text'],
  // The Text tool's Box checkbox borrows the Line colour & weight.
  'at-line-props': ['text', 'ellipse', 'rect', 'line', 'arrow', 'darrow',
                    'cloud', 'highlight'],
  'at-fill-props': ['rect', 'ellipse'],
};
// A highlight has a colour but no stroke to weigh or dash.
const _STROKE_ONLY = ['at-line-weight-fld', 'at-line-style-fld'];

function _refreshStyleRows() {
  const tool = _activeAnnotTool || (_draft && _draft.tool) || null;
  for (const [id, tools] of Object.entries(_STYLE_ROWS)) {
    const el = document.getElementById(id);
    if (el) el.style.display = tools.indexOf(tool) !== -1 ? '' : 'none';
  }
  _STROKE_ONLY.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = tool === 'highlight' ? 'none' : '';
  });
  const hint = document.getElementById('at-idle-hint');
  if (hint) hint.style.display = (tool || _rightMode !== 'comment') ? 'none' : '';
}

function _updateAnnotDeleteBtn() {
  _refreshStyleRows();
  const btn = document.getElementById('at-delete-btn');
  if (btn) btn.style.display = _draft ? '' : 'none';
  // A live draft means the user is drawing or editing an annotation; its delete
  // button and style controls belong to its tool's tab, so bring that forward
  // (an image lifted from the page while the Comment tab is showing, say).
  if (_draft && _rightMode !== _tabForTool(_draft.tool)) setRightMode(_tabForTool(_draft.tool));
}

// One-time wiring of live style controls → re-render active draft.
function _initAnnotToolControls() {
  ['at-text-color', 'at-text-size', 'at-line-color', 'at-line-weight', 'at-line-style', 'at-text-box',
   'at-text-font', 'at-fill-on', 'at-fill-color']
    .forEach(id => {
      const el = document.getElementById(id);
      if (el && !el._annotBound) {
        el._annotBound = true;
        const handler = () => {
          if (!_draft) return;
          _draft.style = _toolStyle();
          _syncTextInput();
          // Text: re-fit height to the new font/size (re-renders); others render directly.
          if (_draft.tool === 'text' && _draft.textInput) _fitTextHeight();
          else _draftRender();
        };
        el.addEventListener('input', handler);
        el.addEventListener('change', handler);   // checkboxes
      }
    });
}
// ── Your name: the author of everything the user adds ──────────────────────
// One field for the whole Comment tab (it used to be the Sticky tool's alone),
// persisted server-side by /api/reviewer_name — see entry.py for why not
// localStorage.  Stamped on new comments and markup, replies and statuses.
let _reviewerName = '';
function _reviewer() { return _reviewerName; }
function _initReviewerName() {
  const inp = document.getElementById('at-author-name');
  if (!inp || inp._reviewerBound) return;
  inp._reviewerBound = true;
  inp.addEventListener('input', () => { _reviewerName = inp.value.trim(); });
  inp.addEventListener('change', () => {
    _reviewerName = inp.value.trim();
    try {
      apiFetch('/api/reviewer_name', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: _reviewerName }),
      }).catch(() => {});
    } catch (_) {}
  });
  try {
    apiFetch('/api/reviewer_name').then(r => r.json()).then(d => {
      if (d && typeof d.name === 'string' && !inp.value) {
        inp.value = d.name; _reviewerName = d.name;
      }
    }).catch(() => {});
  } catch (_) {}
}

function _initAnnotPanel() { _initAnnotToolControls(); _refreshStyleRows(); _initReviewerName(); }
if (document.readyState !== 'loading') _initAnnotPanel();
else document.addEventListener('DOMContentLoaded', _initAnnotPanel);

// Place a sticky note: prompt for its text, then create a /Text annotation at
// the clicked point (an 18pt icon). Editable later via its popup.
async function _placeSticky(page_id, px, py) {
  const text   = (_ctlVal('at-sticky-text', '') || '').trim();
  const author = _reviewer();
  const colKey = _ctlVal('at-sticky-color', 'yellow');
  const fill_color = _STICKY_COLORS[colKey] || _STICKY_COLORS.yellow;
  const rect = [px, py - 18, px + 18, py];   // 18pt icon box
  try {
    const res  = await _queueAnnotOp(() => _vFetch('/api/viewer/add_annotation', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, page_id,
                             subtype: 'Text', rect, text, author, fill_color })
    }));
    const data = await res.json();
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    vRefreshHistory(data);
    allAnnotations[page_id] = data.annotations || [];
    if (page_id === currentPageId) pageAnnotations = allAnnotations[page_id];
    drawPageAnnotations(page_id);
    _invalidateViewerPage(page_id);
    renderComments();
    showToast(window.t('toast.sticky_note_added'), 'success');
  } catch (err) {
    showToast(window.t('toast.failed') + err.message, 'error');
  }
}

// ── Signature (typed name rasterised in a script font → image /Stamp) ─────────
function _sigPreview() {
  const el = document.getElementById('at-sig-preview');
  if (!el) return;
  const name = (_ctlVal('at-sig-name', '') || '').trim() || 'Your signature here';
  el.textContent      = name;
  el.style.fontFamily = _ctlVal('at-sig-font', "'Segoe Script', cursive");
  el.style.fontStyle  = 'italic';
  el.style.fontSize   = Math.min(28, Math.max(12, parseInt(_ctlVal('at-sig-size', '24'), 10) || 24)) + 'px';
  el.style.color      = _ctlVal('at-sig-color', '#1a1a80');
}

// Render the name to an offscreen canvas and return {dataUrl, w, h} (px).
function _renderSignaturePng(name, fontFamily, sizePx, colorHex) {
  const ss = 4;                                  // supersample for crisp output
  const c  = document.createElement('canvas');
  let ctx  = c.getContext('2d');
  const font = `italic ${sizePx * ss}px ${fontFamily}`;
  ctx.font = font;
  const m   = ctx.measureText(name);
  const asc = m.actualBoundingBoxAscent  || sizePx * ss * 0.8;
  const dsc = m.actualBoundingBoxDescent || sizePx * ss * 0.35;
  const pad = Math.round(sizePx * ss * 0.25);
  c.width   = Math.max(1, Math.ceil(m.width) + pad * 2);
  c.height  = Math.max(1, Math.ceil(asc + dsc) + pad * 2);
  ctx = c.getContext('2d');                       // re-fetch after resize
  ctx.font         = font;
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle    = colorHex;
  ctx.fillText(name, pad, pad + asc);
  return { dataUrl: c.toDataURL('image/png'), w: c.width, h: c.height };
}

async function _placeSignature(page_id, px, py) {
  const name = (_ctlVal('at-sig-name', '') || '').trim();
  if (!name) { showToast(window.t('toast.type_a_name_in_the_signature'), 'error'); return; }
  const font  = _ctlVal('at-sig-font', "'Segoe Script', cursive");
  const sizePt = Math.min(96, Math.max(8, parseInt(_ctlVal('at-sig-size', '24'), 10) || 24));
  const color = _ctlVal('at-sig-color', '#1a1a80');
  const img   = _renderSignaturePng(name, font, sizePt, color);
  // Place the image at its natural aspect; height ≈ the chosen point size × 1.4.
  const pdfH = sizePt * 1.4;
  const pdfW = pdfH * (img.w / img.h);
  const rect = [px, py - pdfH, px + pdfW, py];    // top-left at the click point
  try {
    const res  = await _queueAnnotOp(() => _vFetch('/api/viewer/add_annotation', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, page_id,
                             subtype: 'Stamp', rect, image: img.dataUrl })
    }));
    const data = await res.json();
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    vRefreshHistory(data);
    allAnnotations[page_id] = data.annotations || [];
    if (page_id === currentPageId) pageAnnotations = allAnnotations[page_id];
    drawPageAnnotations(page_id);
    _invalidateViewerPage(page_id);
    showToast(window.t('toast.signature_placed'), 'success');
  } catch (err) {
    showToast(window.t('toast.failed') + err.message, 'error');
  }
}

// ── Starting a new shape ──────────────────────────────────────────────────────
function _annotMouseDown(e) {
  if (e.button !== 0 || !_activeAnnotTool) return;
  // Interactions on the live draft (body/handles) are handled by their own
  // listeners — don't start a new shape.
  if (e.target.closest && e.target.closest('.annot-draft')) return;
  // Nor inside an open Add-text entry: that click places the caret.
  if (e.target.closest && e.target.closest('.pt-entry')) return;
  const block = e.target.closest('.page-block');
  if (!block) { if (_draft) commitDraft(); return; }   // click on empty canvas → commit only
  const page_id = block.dataset.pageId;
  const st = _pageBlocks.get(page_id);
  if (!st?.rendered) return;
  e.preventDefault();

  // Clicking the page with a draft already live → commit it first.
  if (_draft) commitDraft();

  const bRect  = block.getBoundingClientRect();
  const startX = e.clientX - bRect.left;
  const startY = e.clientY - bRect.top;

  // Sticky notes & signatures are point annotations — placed on a single click.
  if (_activeAnnotTool === 'sticky') {
    const scale = st.renderScale * zoomLevel;
    const px = startX / scale;
    const py = st.pageH - startY / scale;
    _placeSticky(page_id, px, py);
    return;
  }
  if (_activeAnnotTool === 'signature') {
    const scale = st.renderScale * zoomLevel;
    _placeSignature(page_id, startX / scale, st.pageH - startY / scale);
    return;
  }

  // The Pointer works entirely through its own hit-area overlay.
  if (_activeAnnotTool === 'edittext') return;

  // Page tools never become a draft: they change the page itself rather than
  // laying an annotation object on top of it, so there is nothing for
  // commitDraft to write.  Each runs its own drag and posts to its own endpoint.
  if (_isPageTool(_activeAnnotTool)) {
    _pageToolDragStart(_activeAnnotTool, page_id, st, block, bRect, startX, startY);
    return;
  }

  const isLine = _isLineTool(_activeAnnotTool);

  _drawing = { tool: _activeAnnotTool, page_id, st, block, bRect, startX, startY, isLine };
  document.addEventListener('mousemove', _annotMouseMove);
  document.addEventListener('mouseup',   _annotMouseUp);
}

function _annotMouseMove(e) {
  if (!_drawing) return;
  const d = _drawing;
  const cx = e.clientX - d.bRect.left, cy = e.clientY - d.bRect.top;
  if (!d.previewing) {
    if (Math.abs(cx - d.startX) < 3 && Math.abs(cy - d.startY) < 3) return;
    d.previewing = true;
  }
  d.curX = cx; d.curY = cy;
  // Live preview using a temporary draft object
  if (!_draft) _draft = _makeDraftFrom(d);
  _updateDraftGeomFromDrawing();
  _draftRender();
}

function _annotMouseUp(e) {
  document.removeEventListener('mousemove', _annotMouseMove);
  document.removeEventListener('mouseup',   _annotMouseUp);
  const d = _drawing; _drawing = null;
  if (!d) return;

  const cx = e.clientX - d.bRect.left, cy = e.clientY - d.bRect.top;
  d.curX = cx; d.curY = cy;

  if (!_draft) _draft = _makeDraftFrom(d);
  _updateDraftGeomFromDrawing();

  // Reject negligible drags. Text only needs a width — its height is auto-fitted
  // to the content, so a wide, flat drag (a single-line box) is valid.
  const tooSmall = d.isLine
    ? (Math.hypot(_draft.x2 - _draft.x1, _draft.y2 - _draft.y1) < 8)
    : (d.tool === 'text' ? _draft.w < 8 : (_draft.w < 8 || _draft.h < 8));
  if (tooSmall) { _removeDraft(); return; }

  if (_draft.tool === 'text') _attachTextInput();
  _draftRender();
}

function _makeDraftFrom(d) {
  const svg = document.createElementNS(NS_SVG, 'svg');
  svg.setAttribute('class', 'annot-draft');
  svg.style.cssText = 'position:absolute;top:0;left:0;z-index:500;pointer-events:none';
  svg.setAttribute('width',  d.block.offsetWidth);
  svg.setAttribute('height', d.block.offsetHeight);
  d.block.appendChild(svg);
  return {
    tool: d.tool, page_id: d.page_id, st: d.st, block: d.block,
    scale: d.st.renderScale * zoomLevel, kind: d.isLine ? 'line' : 'box',
    x: 0, y: 0, w: 0, h: 0, x1: 0, y1: 0, x2: 0, y2: 0,
    svg, style: _toolStyle(), textInput: null,
    isComment: false,             // only pre-v3.2 Add-Note text was a comment
    author: _reviewer(),
    imageData: (d.tool === 'image' && _pendingImage) ? _pendingImage.dataUrl : null,
  };
}

function _updateDraftGeomFromDrawing() {
  const d = _drawing; if (!d || !_draft) return;
  if (d.isLine) {
    _draft.x1 = d.startX; _draft.y1 = d.startY;
    _draft.x2 = d.curX;   _draft.y2 = d.curY;
  } else {
    _draft.x = Math.min(d.startX, d.curX);
    _draft.y = Math.min(d.startY, d.curY);
    _draft.w = Math.abs(d.curX - d.startX);
    _draft.h = Math.abs(d.curY - d.startY);
  }
}

// ── Rendering the live draft (shape + handles) ──────────────────────────────
// Dash pattern for the preview stroke — mirrors the server's /BS /D patterns
// (dashed = [3w,3w], dotted = [w,2w], scaled to display px). null = solid.
function _dashArray(style, swDisp) {
  if (style.lineStyle === 'dashed') return `${swDisp * 3} ${swDisp * 3}`;
  if (style.lineStyle === 'dotted') return `${swDisp} ${swDisp * 2}`;
  return null;
}

function _draftRender() {
  if (!_draft) return;
  _updateAnnotDeleteBtn();
  const svg = _draft.svg, s = _draft.style;
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const stroke = _rgbCss(s.lineColor);
  const swDisp = Math.max(1, s.lineWidth * _draft.scale);
  const dashes = _dashArray(s, swDisp);

  if (_draft.kind === 'line') {
    const ln = document.createElementNS(NS_SVG, 'line');
    ln.setAttribute('x1', _draft.x1); ln.setAttribute('y1', _draft.y1);
    ln.setAttribute('x2', _draft.x2); ln.setAttribute('y2', _draft.y2);
    ln.setAttribute('stroke', stroke);
    ln.setAttribute('stroke-width', swDisp);
    ln.setAttribute('stroke-linecap', 'round');
    if (dashes) ln.setAttribute('stroke-dasharray', dashes);
    ln.style.pointerEvents = 'stroke';
    ln.style.cursor = 'move';
    ln.addEventListener('mousedown', ev => _draftStartDrag(ev, 'move'));
    svg.appendChild(ln);
    if (_draft.tool === 'arrow' || _draft.tool === 'darrow')
      _drawArrowHead(svg, _draft.x1, _draft.y1, _draft.x2, _draft.y2, stroke, swDisp);
    if (_draft.tool === 'darrow')   // second head, pointing back at the start
      _drawArrowHead(svg, _draft.x2, _draft.y2, _draft.x1, _draft.y1, stroke, swDisp);
    _addHandle(svg, _draft.x1, _draft.y1, 'p1');
    _addHandle(svg, _draft.x2, _draft.y2, 'p2');
    return;
  }

  // Box tools (rect / ellipse / text / cloud / highlight / image)
  const x = _draft.x, y = _draft.y, w = _draft.w, h = _draft.h;

  // Image stamp — render the picked image filling the box, with resize handles.
  if (_draft.tool === 'image' && _draft.imageData) {
    const im = document.createElementNS(NS_SVG, 'image');
    im.setAttributeNS('http://www.w3.org/1999/xlink', 'href', _draft.imageData);
    im.setAttribute('href', _draft.imageData);
    im.setAttribute('x', x);     im.setAttribute('y', y);
    im.setAttribute('width', Math.max(0, w)); im.setAttribute('height', Math.max(0, h));
    im.setAttribute('preserveAspectRatio', 'none');   // matches the server's BBox scale
    im.style.pointerEvents = 'all';
    im.style.cursor = 'move';
    im.addEventListener('mousedown', ev => _draftStartDrag(ev, 'move'));
    svg.appendChild(im);
    _addHandle(svg, x,     y,     'nw');
    _addHandle(svg, x + w, y,     'ne');
    _addHandle(svg, x,     y + h, 'sw');
    _addHandle(svg, x + w, y + h, 'se');
    return;
  }

  // Interior fill — rect & ellipse only (cloud is stroke-only; text/highlight
  // manage their own fill). Mirrors the server's /IC handling.
  const fillCss = (s.fillOn && s.fillColor) ? _rgbCss(s.fillColor) : 'none';
  let shape;
  if (_draft.tool === 'ellipse') {
    shape = document.createElementNS(NS_SVG, 'ellipse');
    shape.setAttribute('cx', x + w / 2); shape.setAttribute('cy', y + h / 2);
    shape.setAttribute('rx', w / 2);     shape.setAttribute('ry', h / 2);
    shape.setAttribute('fill', fillCss);
    shape.setAttribute('stroke', stroke);
    shape.setAttribute('stroke-width', swDisp);
    if (dashes) shape.setAttribute('stroke-dasharray', dashes);
  } else if (_draft.tool === 'cloud') {
    shape = document.createElementNS(NS_SVG, 'path');
    const arc = Math.max(8, Math.min(28, Math.min(w, h) / 4)) || 12;
    shape.setAttribute('d', _cloudPathD(x, y, w, h, arc));
    shape.setAttribute('fill', 'none');
    shape.setAttribute('stroke', stroke);
    shape.setAttribute('stroke-width', swDisp);
    shape.setAttribute('stroke-linejoin', 'round');
  } else if (_draft.tool === 'highlight') {
    shape = document.createElementNS(NS_SVG, 'rect');
    shape.setAttribute('x', x); shape.setAttribute('y', y);
    shape.setAttribute('width', w); shape.setAttribute('height', h);
    shape.setAttribute('fill', stroke);
    shape.setAttribute('fill-opacity', '0.35');
    shape.setAttribute('stroke', 'none');
  } else {
    shape = document.createElementNS(NS_SVG, 'rect');
    shape.setAttribute('x', x); shape.setAttribute('y', y);
    shape.setAttribute('width', w); shape.setAttribute('height', h);
    shape.setAttribute('fill', _draft.tool === 'text' ? 'rgba(0,0,0,0.001)' : fillCss);
    if (_draft.tool === 'text' && !s.textBox) {
      // No box requested → show a faint dashed guide while editing only
      // (this guide is NOT part of the committed annotation).
      shape.setAttribute('stroke', 'rgba(120,120,140,0.9)');
      shape.setAttribute('stroke-width', '1');
      shape.setAttribute('stroke-dasharray', '4,3');
    } else {
      // Border: shapes always; text only when "Box" is ticked. The Style
      // (dash) option applies to drawn rectangles, not text boxes.
      shape.setAttribute('stroke', stroke);
      shape.setAttribute('stroke-width', swDisp);
      if (dashes && _draft.tool === 'rect') shape.setAttribute('stroke-dasharray', dashes);
    }
  }
  shape.style.pointerEvents = 'all';
  shape.style.cursor = 'move';
  shape.addEventListener('mousedown', ev => _draftStartDrag(ev, 'move'));
  svg.appendChild(shape);

  // Fat invisible "grab band" hugging the outline. For the text tool the inline
  // <textarea> sits on top of the box and covers all but a sliver of the border,
  // making the move cursor almost impossible to hit. This band extends outward
  // past the box edge (clear of the textarea) so there's a comfortable target
  // on and just outside the outline. Added before the handles so corner
  // resize still wins at the corners.
  _addMoveFrame(svg, x, y, w, h);

  // Resize handles. Text boxes auto-manage their height (it tracks the wrapped
  // text), so they expose left/right WIDTH handles only — changing the width
  // re-wraps the text and the height re-fits. Every other box tool keeps the
  // 4 corner handles for free width+height resizing.
  if (_draft.tool === 'text') {
    _addHandle(svg, x,     y + h / 2, 'w');
    _addHandle(svg, x + w, y + h / 2, 'e');
  } else {
    _addHandle(svg, x,     y,     'nw');
    _addHandle(svg, x + w, y,     'ne');
    _addHandle(svg, x,     y + h, 'sw');
    _addHandle(svg, x + w, y + h, 'se');
  }
}

// Revision-cloud outline as an SVG path 'd' (display coords). Bumps bulge
// outward from the box centre; mirrors the server-side cloud algorithm so the
// preview matches the committed appearance.
function _cloudPathD(bx, by, w, h, arc) {
  const x1 = bx, y1 = by, x2 = bx + w, y2 = by + h;
  const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2;
  const segs = [];
  let p;
  for (p = x1; p < x2; p += arc * 2) segs.push(['h', p, y1, Math.min(p + arc * 2, x2), y1]);
  for (p = y1; p < y2; p += arc * 2) segs.push(['v', x2, p, x2, Math.min(p + arc * 2, y2)]);
  for (p = x2; p > x1; p -= arc * 2) segs.push(['h', p, y2, Math.max(p - arc * 2, x1), y2]);
  for (p = y2; p > y1; p -= arc * 2) segs.push(['v', x1, p, x1, Math.max(p - arc * 2, y1)]);
  if (!segs.length) return `M${x1},${y1} L${x2},${y1} L${x2},${y2} L${x1},${y2} Z`;
  let d = `M${segs[0][1].toFixed(2)},${segs[0][2].toFixed(2)}`;
  segs.forEach(([kind, sx, sy, ex, ey]) => {
    const mx = (sx + ex) / 2, my = (sy + ey) / 2;
    const r = Math.hypot(ex - sx, ey - sy) / 2;
    if (r < 0.5) { d += ` L${ex.toFixed(2)},${ey.toFixed(2)}`; return; }
    let nx, ny;
    if (kind === 'h') { nx = 0; ny = (my > cy ? 1 : -1); }
    else              { nx = (mx > cx ? 1 : -1); ny = 0; }
    const dl = Math.hypot(ex - sx, ey - sy) || 1;
    const tx = (ex - sx) / dl, ty = (ey - sy) / dl;
    const bulge = r * 0.75, tang = r * 0.5;
    const c1x = sx + tx * tang + nx * bulge, c1y = sy + ty * tang + ny * bulge;
    const c2x = ex - tx * tang + nx * bulge, c2y = ey - ty * tang + ny * bulge;
    d += ` C${c1x.toFixed(2)},${c1y.toFixed(2)} ${c2x.toFixed(2)},${c2y.toFixed(2)} ${ex.toFixed(2)},${ey.toFixed(2)}`;
  });
  return d + ' Z';
}

function _drawArrowHead(svg, x1, y1, x2, y2, stroke, sw) {
  const ang = Math.atan2(y2 - y1, x2 - x1);
  const len = Math.max(10, sw * 4);
  const spread = 28 * Math.PI / 180;
  [+1, -1].forEach(sgn => {
    const hx = x2 - len * Math.cos(ang + sgn * spread);
    const hy = y2 - len * Math.sin(ang + sgn * spread);
    const l = document.createElementNS(NS_SVG, 'line');
    l.setAttribute('x1', x2); l.setAttribute('y1', y2);
    l.setAttribute('x2', hx); l.setAttribute('y2', hy);
    l.setAttribute('stroke', stroke); l.setAttribute('stroke-width', sw);
    l.setAttribute('stroke-linecap', 'round');
    svg.appendChild(l);
  });
}

// Transparent thick-stroked rect that acts as a generous "move" hit target on
// and just outside the box outline. pointer-events:'stroke' makes only the band
// (not the interior) catch the mouse, so clicking inside a text draft still
// reaches the textarea for caret placement. Inflated by `out` so the band sits
// mostly outside the box, clear of the overlaid textarea.
function _addMoveFrame(svg, x, y, w, h) {
  const out = 3, band = 12;   // band spans ~9px outside to ~3px inside the edge
  const f = document.createElementNS(NS_SVG, 'rect');
  f.setAttribute('x', x - out); f.setAttribute('y', y - out);
  f.setAttribute('width',  Math.max(0, w + out * 2));
  f.setAttribute('height', Math.max(0, h + out * 2));
  f.setAttribute('fill', 'none');
  f.setAttribute('stroke', 'transparent');
  f.setAttribute('stroke-width', band);
  f.style.pointerEvents = 'stroke';
  f.style.cursor = 'move';
  f.addEventListener('mousedown', ev => _draftStartDrag(ev, 'move'));
  svg.appendChild(f);
}

function _addHandle(svg, cx, cy, key) {
  const r = document.createElementNS(NS_SVG, 'rect');
  const sz = 9;
  r.setAttribute('x', cx - sz / 2); r.setAttribute('y', cy - sz / 2);
  r.setAttribute('width', sz); r.setAttribute('height', sz);
  r.setAttribute('rx', 2);
  r.setAttribute('class', 'draft-handle');
  r.style.pointerEvents = 'all';
  r.style.cursor = (key === 'nw' || key === 'se') ? 'nwse-resize'
                 : (key === 'ne' || key === 'sw') ? 'nesw-resize'
                 : (key === 'w'  || key === 'e')  ? 'ew-resize' : 'crosshair';
  r.addEventListener('mousedown', ev => _draftStartDrag(ev, 'handle', key));
  svg.appendChild(r);
}

// ── Alignment guides while moving/resizing a draft ──────────────────────────
// Four page-spanning lines pinned to the draft's extents (left/right/top/bottom),
// mirroring the editor's drag guides (editor.js computeAlignmentGuides). They
// live in their own overlay rather than _draft.svg because _draftRender() wipes
// that element on every frame. Block-relative px, same space as _draft.x/y/w/h.
const GUIDE_COLOR = '#ff0099';
let _guideSvg = null;

function _showDragGuides() {
  if (!_draft || _guideSvg) return;
  const svg = document.createElementNS(NS_SVG, 'svg');
  svg.setAttribute('class', 'annot-guides');
  svg.style.cssText = 'position:absolute;top:0;left:0;z-index:499;pointer-events:none';
  svg.setAttribute('width',  _draft.block.offsetWidth);
  svg.setAttribute('height', _draft.block.offsetHeight);
  for (let i = 0; i < 4; i++) {
    const ln = document.createElementNS(NS_SVG, 'line');
    ln.setAttribute('stroke', GUIDE_COLOR);
    ln.setAttribute('stroke-width', 1);
    ln.setAttribute('shape-rendering', 'crispEdges');
    svg.appendChild(ln);
  }
  _draft.block.appendChild(svg);
  _guideSvg = svg;
  _updateDragGuides();
}

function _updateDragGuides() {
  if (!_guideSvg || !_draft) return;
  // Lines are a bbox for both kinds: endpoint extents for line/arrow drafts,
  // the box itself for everything else.
  const left   = _draft.kind === 'line' ? Math.min(_draft.x1, _draft.x2) : _draft.x;
  const right  = _draft.kind === 'line' ? Math.max(_draft.x1, _draft.x2) : _draft.x + _draft.w;
  const top    = _draft.kind === 'line' ? Math.min(_draft.y1, _draft.y2) : _draft.y;
  const bottom = _draft.kind === 'line' ? Math.max(_draft.y1, _draft.y2) : _draft.y + _draft.h;

  const pw = _guideSvg.getAttribute('width'), ph = _guideSvg.getAttribute('height');
  // Half-pixel offsets keep the 1px strokes on a device pixel (no blur).
  const at = [
    [Math.round(left)  + 0.5, 0, Math.round(left)  + 0.5, ph],
    [Math.round(right) + 0.5, 0, Math.round(right) + 0.5, ph],
    [0, Math.round(top)    + 0.5, pw, Math.round(top)    + 0.5],
    [0, Math.round(bottom) + 0.5, pw, Math.round(bottom) + 0.5],
  ];
  _guideSvg.childNodes.forEach((ln, i) => {
    ln.setAttribute('x1', at[i][0]); ln.setAttribute('y1', at[i][1]);
    ln.setAttribute('x2', at[i][2]); ln.setAttribute('y2', at[i][3]);
  });
}

function _hideDragGuides() {
  if (_guideSvg) { _guideSvg.remove(); _guideSvg = null; }
}

// ── Move / resize the live draft ────────────────────────────────────────────
function _draftStartDrag(e, mode, key) {
  if (!_draft) return;
  e.preventDefault(); e.stopPropagation();
  const snap = { x: _draft.x, y: _draft.y, w: _draft.w, h: _draft.h,
                 x1: _draft.x1, y1: _draft.y1, x2: _draft.x2, y2: _draft.y2 };
  const mx = e.clientX, my = e.clientY;
  function mm(ev) {
    const dx = ev.clientX - mx, dy = ev.clientY - my;
    _applyDrag(mode, key, snap, dx, dy);
    // Text: a width change re-wraps the text, so re-fit the height live; a move
    // just repositions. _fitTextHeight() re-renders for us. Other tools render
    // directly.
    if (_draft.tool === 'text' && _draft.textInput) { _syncTextInput(); _fitTextHeight(); }
    else { _draftRender(); _syncTextInput(); }
    // After the refit above, so the guides track the final height.
    _showDragGuides();
    _updateDragGuides();
  }
  function mu() {
    _hideDragGuides();
    document.removeEventListener('mousemove', mm);
    document.removeEventListener('mouseup', mu);
  }
  document.addEventListener('mousemove', mm);
  document.addEventListener('mouseup', mu);
}

function _applyDrag(mode, key, g, dx, dy) {
  if (_draft.kind === 'line') {
    if (mode === 'move') {
      _draft.x1 = g.x1 + dx; _draft.y1 = g.y1 + dy;
      _draft.x2 = g.x2 + dx; _draft.y2 = g.y2 + dy;
    } else if (key === 'p1') {
      _draft.x1 = g.x1 + dx; _draft.y1 = g.y1 + dy;
    } else {
      _draft.x2 = g.x2 + dx; _draft.y2 = g.y2 + dy;
    }
    return;
  }
  let left = g.x, top = g.y, right = g.x + g.w, bottom = g.y + g.h;
  if (mode === 'move') {
    left += dx; right += dx; top += dy; bottom += dy;
  } else {
    if (key === 'nw') { left += dx; top += dy; }
    if (key === 'ne') { right += dx; top += dy; }
    if (key === 'sw') { left += dx; bottom += dy; }
    if (key === 'se') { right += dx; bottom += dy; }
    if (key === 'w')  { left  += dx; }   // text: width-only (height auto-fits)
    if (key === 'e')  { right += dx; }
  }
  _draft.x = Math.min(left, right); _draft.w = Math.abs(right - left);
  _draft.y = Math.min(top, bottom); _draft.h = Math.abs(bottom - top);
}

// ── Inline text editor for the Text tool ────────────────────────────────────
function _attachTextInput() {
  if (!_draft || _draft.tool !== 'text') return;
  const ta = document.createElement('textarea');
  ta.className = 'annot-draft annot-text-input';
  ta.value = '';
  ta.placeholder = window.t('annot_tools.type_text_ph');
  _draft.block.appendChild(ta);
  _draft.textInput = ta;
  ta.addEventListener('mousedown', e => e.stopPropagation());
  ta.addEventListener('input', _fitTextHeight);   // grow/shrink height while typing
  _syncTextInput();
  _fitTextHeight();                                // snap to the one-line floor on attach
  setTimeout(() => ta.focus(), 0);
}

function _syncTextInput() {
  const ta = _draft && _draft.textInput;
  if (!ta) return;
  // Padding mirrors the server's FreeText appearance (viewer.py: max(3, size*.35))
  // so the on-screen box and the committed PDF lay text out identically.
  const s = _draft.style, pad = _textPadPt(s.fontSize) * _draft.scale;
  const f = _pdfFontToCss(s.font);
  ta.style.cssText =
    `position:absolute;z-index:501;box-sizing:border-box;` +
    `left:${_draft.x}px;top:${_draft.y}px;` +
    `width:${_draft.w}px;height:${_draft.h}px;` +
    `padding:${pad}px;margin:0;border:none;outline:none;resize:none;` +
    `background:transparent;overflow:hidden;line-height:1.35;` +
    `font-family:${f.family};font-weight:${f.weight};font-style:${f.style};` +
    `font-size:${s.fontSize * _draft.scale}px;color:${_rgbCss(s.textColor)};`;
}

// Text-box padding & line height in PDF points — kept in lockstep with the
// server's _generate_freetext_ap so the editor box matches the committed output.
function _textPadPt(fontSize) { return Math.max(3, fontSize * 0.35); }
function _textLhPt(fontSize)  { return fontSize * 1.35; }

// Mirror the server's word-wrap (chars-per-line) to count how many display lines
// a paragraph needs at the given width — used so the auto-fitted box is never
// shorter than the committed appearance will be.
function _wrapCount(para, cpl) {
  if (!para) return 1;
  const words = para.split(' ');
  let lines = 1, cur = 0;
  for (const w of words) {
    if (cur === 0) cur = w.length;
    else if (cur + 1 + w.length <= cpl) cur += 1 + w.length;
    else { lines++; cur = w.length; }
  }
  return lines;
}

// Auto-fit a text draft's HEIGHT to its content: one line minimum (the "floor"),
// growing downward as the text wraps within the current width. Width is left
// untouched (the user controls it via the drag / side handles). Re-renders the
// box + handles so they track the new height.
function _fitTextHeight() {
  if (!_draft || _draft.tool !== 'text' || !_draft.textInput) return;
  const ta = _draft.textInput, s = _draft.style, scale = _draft.scale;
  const padPt = _textPadPt(s.fontSize), lhPt = _textLhPt(s.fontSize);

  // Server-projected height: word-wrap to the box width with the server's
  // chars-per-line estimate, so the committed PDF can't overflow the box.
  const boxWpt = _draft.w / scale;
  const cpl    = Math.max(1, Math.floor((boxWpt - 2 * padPt) / (s.fontSize * 0.55)));
  let nLines = 0;
  for (const para of (ta.value || '').replace(/\r\n?/g, '\n').split('\n')) {
    nLines += _wrapCount(para, cpl);
  }
  nLines = Math.max(1, nLines);
  const serverHpx = (2 * padPt + nLines * lhPt) * scale;

  // Browser-measured height: what the user actually sees with real font metrics
  // (covers fonts where the server's estimate wraps fewer lines than reality).
  ta.style.height = '0px';
  const domHpx = ta.scrollHeight;

  _draft.h = Math.round(Math.max(serverHpx, domHpx));
  ta.style.height = _draft.h + 'px';
  _draftRender();
}

function _removeDraft() {
  _hideDragGuides();   // a draft torn down mid-drag must not strand its guides
  if (!_draft) return;
  if (_draft.svg)       _draft.svg.remove();
  if (_draft.textInput) _draft.textInput.remove();
  _draft = null;
  _updateAnnotDeleteBtn();
  _refreshEditHitAreas();
}

// ── The live draft as an add_annotation body ────────────────────────────────
// Geometry converted from display px to PDF points. Session/page are NOT set —
// the caller decides which page it lands on, so a copied draft can be pasted
// onto a different one (mirrors _annotInfoToAddBody). Returns null when the
// draft holds nothing worth writing (an empty text note, an image with no
// bitmap); the caller decides what that means.
function _draftToAddBody(d) {
  const s = d.style;
  const scale = d.scale, pageH = d.st.pageH;

  const subtypeMap = { text:'FreeText', rect:'Square', ellipse:'Circle',
                       line:'Line', arrow:'Line', darrow:'Line', cloud:'Polygon',
                       highlight:'Highlight', image:'Stamp' };
  const subtype = subtypeMap[d.tool];
  if (!subtype) return null;

  const body = {
    subtype, border_color: s.lineColor, border_width: s.lineWidth,
  };
  if (d.author) body.author = d.author;   // preserve author across lift-edit
  // A lifted annotation's thread was deleted with it; send it back to be
  // re-attached, along with the original creation date.
  if (d.created) body.created = d.created;
  if (d.replies && d.replies.length) body.replies = d.replies;

  if (d.tool === 'text') {
    const text = (d.textInput ? d.textInput.value : '').trim();
    if (!text) return null;                  // don't create empty notes
    body.rect       = [d.x/scale, pageH - (d.y + d.h)/scale,
                       (d.x + d.w)/scale, pageH - d.y/scale];
    body.text       = text;
    body.text_color = s.textColor;
    body.font_size  = s.fontSize;
    body.font       = s.font || 'Helvetica';
    // Border box only when "Box" is ticked (weight clamped to ≥1); else none.
    body.border_width = s.textBox ? Math.max(1, s.lineWidth) : 0;
    body.is_comment = !!d.isComment;   // false → "dumb text", kept out of list
  } else if (_isLineTool(d.tool)) {
    body.line  = [d.x1/scale, pageH - d.y1/scale, d.x2/scale, pageH - d.y2/scale];
    body.arrow       = (d.tool === 'arrow' || d.tool === 'darrow');
    body.arrow_start = (d.tool === 'darrow');
    body.border_style = s.lineStyle || 'solid';
  } else {
    body.rect = [d.x/scale, pageH - (d.y + d.h)/scale,
                 (d.x + d.w)/scale, pageH - d.y/scale];
    // Dash style + interior fill — rect & ellipse only.
    if (d.tool === 'rect' || d.tool === 'ellipse') {
      body.border_style = s.lineStyle || 'solid';
      if (s.fillOn && s.fillColor) body.fill_color = s.fillColor;
    }
    // Image stamp — send the picked image; server embeds it as a /Stamp.
    if (d.tool === 'image') {
      if (!d.imageData) return null;
      body.image = d.imageData;
    }
  }
  return body;
}

// ── Commit the live draft to the PDF ────────────────────────────────────────
function commitDraft() {
  if (!_draft) return;
  const d = _draft;

  const body = _draftToAddBody(d);
  if (!body) { _removeDraft(); return; }   // nothing worth writing
  body.session_id = SESSION_ID;
  body.page_id    = d.page_id;

  const page_id   = d.page_id;
  const wasLifted  = !!d.lifted;
  // A lifted annotation's pre-edit state was already snapshotted by the lift's
  // delete, so the re-commit must not snapshot again — keeps a move/restyle to a
  // single undo step.
  if (wasLifted) body.no_snapshot = true;
  _removeDraft();   // remove the live overlay immediately for snappy feedback

  // Everything the POST needs was captured synchronously above, so queueing the
  // network call is safe even if the user is already drawing the next shape.
  _queueAnnotOp(() => _vFetch('/api/viewer/add_annotation', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    vRefreshHistory(data);
    allAnnotations[page_id] = data.annotations || [];
    if (page_id === currentPageId) pageAnnotations = allAnnotations[page_id];
    drawPageAnnotations(page_id);
    _invalidateViewerPage(page_id);
    if (data.thumbnail) {
      const pg = allPages.find(p => p.id === page_id);
      if (pg) { pg.thumbnail = data.thumbnail; renderThumbList(); }
    }
    renderComments();
    showToast(window.t(wasLifted ? 'toast.updated'
                       : body.subtype === 'FreeText' ? 'toast.text_added'
                       : 'toast.added_listed'), 'success');
  })
  // Swallow errors inside the chain (after surfacing a toast) so one failed
  // commit can't wedge every commit that follows it.
  .catch(err => showToast(window.t('toast.failed') + err.message, 'error')));
}

// Commit when the user clicks anywhere outside the page area and the tool panel
// (e.g. a toolbar button or menu) — "click elsewhere locks it in".
document.addEventListener('mousedown', e => {
  if (!_draft) return;
  if (e.target.closest && (e.target.closest('#canvas-area') ||
      e.target.closest('#annot-tools') || e.target.closest('.annot-draft'))) return;
  commitDraft();
}, true);

// ── Annotation undo / redo ───────────────────────────────────────────────────
// History lives server-side (page-bytes snapshots, see viewer.py). The client
// drives it via /api/viewer/undo|redo and keeps the toolbar buttons in sync from
// the undo_count/redo_count every annotation response carries.
let _vUndoCount = 0, _vRedoCount = 0;

function _vUpdateUndoButtons() {
  const u = document.getElementById('v-btn-undo');
  const r = document.getElementById('v-btn-redo');
  if (u) {
    u.disabled = !_vUndoCount;
    u.title = _vUndoCount ? `Undo (${_vUndoCount})  Ctrl+Z` : 'Nothing to undo';
  }
  if (r) {
    r.disabled = !_vRedoCount;
    r.title = _vRedoCount ? `Redo (${_vRedoCount})  Ctrl+Y` : 'Nothing to redo';
  }
}

// Update button state from any annotation response that carries history counts.
function vRefreshHistory(data) {
  if (!data) return;
  if (typeof data.undo_count === 'number') _vUndoCount = data.undo_count;
  if (typeof data.redo_count === 'number') _vRedoCount = data.redo_count;
  _vUpdateUndoButtons();
}

function viewerUndo() { _viewerHistory('undo'); }
function viewerRedo() { _viewerHistory('redo'); }

function _viewerHistory(which) {
  if (!SESSION_ID) return;
  // Lock in any in-progress draft first so it becomes part of the history.
  // commitDraft only QUEUES its POST, so this has to queue behind it rather
  // than fire now — otherwise the undo reverses the annotation BEFORE the one
  // the user just drew, and the draw then lands on top of the undone state.
  if (_draft) commitDraft();
  return _queueAnnotOp(() => _viewerHistoryNow(which));
}

async function _viewerHistoryNow(which) {
  try {
    const res  = await _vFetch('/api/viewer/' + which, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID })
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    vRefreshHistory(data);
    if (!data.page_id) {
      showToast(window.t(which === 'undo' ? 'toast.nothing_to_undo' : 'toast.nothing_to_redo'), 'error');
      return;
    }
    const pid = data.page_id;
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    allAnnotations[pid]  = data.annotations || [];
    _editableAnnots[pid] = data.editable || [];
    if (pid === currentPageId) pageAnnotations = allAnnotations[pid];
    if (data.thumbnail) {
      const pg = allPages.find(p => p.id === pid);
      if (pg) { pg.thumbnail = data.thumbnail; renderThumbList(); }
    }
    // A restored snapshot can be a different SIZE — undoing a crop is exactly
    // that — so take the dimensions the server measured rather than leaving the
    // page laid out at the size the undo just replaced.
    _applyPageSize(pid, data.width_pts, data.height_pts);
    _invalidateViewerPage(pid);    // re-render the page raster
    // The restored bytes are different text: the Pointer's ids are stale.
    _etInvalidate(pid);
    drawPageAnnotations(pid);
    renderComments();
    _scrollPageIntoView(pid);
    showToast(window.t(which === 'undo' ? 'toast.undone' : 'toast.redone'), 'success');
  } catch (err) {
    showToast(window.t(which === 'undo' ? 'toast.undo_failed' : 'toast.redo_failed')
              + err.message, 'error');
  }
}

// Bring the page an undo/redo touched into view (it may be off-screen) so the
// change is visible. No-op if it's already the current page.
function _scrollPageIntoView(page_id) {
  if (page_id === currentPageId) return;
  const block = document.getElementById('page-block-' + page_id);
  if (block) block.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// Keyboard: Ctrl/Cmd+Z = undo, Ctrl/Cmd+Y or Ctrl/Cmd+Shift+Z = redo.
// Only while the viewer panel is active and focus isn't in a text field (so it
// doesn't hijack typing in the comment box, search, or a text-annotation editor).
document.addEventListener('keydown', function(e) {
  if (!(e.ctrlKey || e.metaKey)) return;
  const k = (e.key || '').toLowerCase();
  if (k !== 'z' && k !== 'y') return;
  const pv = document.getElementById('panel-viewer');
  if (!pv || pv.style.display === 'none') return;
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || document.activeElement?.isContentEditable)
    return;
  if (k === 'z' && !e.shiftKey) { e.preventDefault(); viewerUndo(); }
  else                          { e.preventDefault(); viewerRedo(); }  // Ctrl+Y / Ctrl+Shift+Z
});

// True while the viewer owns the clipboard keys: its panel is up and the user
// isn't typing (a field must keep the native copy/paste).
function _viewerClipboardContext() {
  const pv = document.getElementById('panel-viewer');
  if (!pv || pv.style.display === 'none') return false;
  const ae = document.activeElement;
  const tag = (ae && ae.tagName) || '';
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || ae?.isContentEditable) return false;
  return true;
}

// Ctrl/Cmd+C copies whichever annotation selection is live, and there are two
// kinds. Clicking one annotation LIFTS it into a draft — that is what "selected"
// means for a single annotation, so copy it; a marquee round several yields an
// area selection. Without the first case the obvious gesture (click a shape,
// Ctrl+C) was a silent no-op even though the shape looked selected.
//
// Text copy is untouched: it happens eagerly at selection time in
// _selectTextInRect, and while a text note is being typed focus sits in the
// inline <textarea>, so _viewerClipboardContext() defers to the native copy.
document.addEventListener('keydown', function(e) {
  if (!(e.ctrlKey || e.metaKey)) return;
  if ((e.key || '').toLowerCase() !== 'c') return;
  if (!_viewerClipboardContext()) return;
  if (_draft) { e.preventDefault(); _copyLiveDraft(); return; }
  if (!_areaSelection?.items.length) return;
  e.preventDefault();
  _copyAreaSelection();
});

// Snapshot the live draft onto the annotation clipboard, in the same shape
// _copyAreaSelection produces so Ctrl+V handles both identically. The draft
// itself is left alone — Ctrl+V commits it before pasting, so a click-copy-paste
// keeps the original and adds the copy.
function _copyLiveDraft() {
  const body = _draftToAddBody(_draft);
  if (!body) { showToast(window.t('toast.nothing_to_copy'), 'error'); return; }
  // A copy is a new item: it starts its own history, and does not bring the
  // original's discussion with it (a lifted draft carries both).
  delete body.replies; delete body.created;
  const g = body.rect || body.line;
  _annotClipboard = { items: [body], bbox: {
    x1: Math.min(g[0], g[2]), y1: Math.min(g[1], g[3]),
    x2: Math.max(g[0], g[2]), y2: Math.max(g[1], g[3]) } };
  showToast(1 + window.t('toast.annots_copied'), 'success');
}

// Ctrl/Cmd+V, in two halves, and the split is deliberate.
//
// The in-app annotation clipboard is pasted from KEYDOWN, because it needs no
// access to the system clipboard at all — the bodies are already in memory. It
// must not be made to depend on a `paste` event: that event is delivered only
// when the embedder allows clipboard access AND the browser picks a paste
// target, neither of which this feature ever needed. (Routing it through
// `paste` in v2.23 is exactly what stopped it working.)
document.addEventListener('keydown', function(e) {
  if (!(e.ctrlKey || e.metaKey)) return;
  if ((e.key || '').toLowerCase() !== 'v') return;
  if (!_viewerClipboardContext()) return;
  if (!_annotClipboard?.items.length) return;   // nothing copied → leave the
  e.preventDefault();                           // key for the paste handler
  if (_draft) commitDraft();   // accept any in-progress edit first
  _pasteAnnotClipboard();
});

// A bitmap on the SYSTEM clipboard is only readable from the `paste` event, so
// image paste lives here. It is reached only when the keydown above declined —
// i.e. when nothing has been copied in-app — so an in-app copy keeps the
// precedence it has always had and an image paste can never double-fire.
document.addEventListener('paste', function(e) {
  if (!_viewerClipboardContext()) return;
  const file = _clipboardImageFile(e);
  if (!file) return;
  e.preventDefault();
  if (_blockedByRestriction()) return;
  _pasteImageFile(file);
});

// ── Print ────────────────────────────────────────────────────────────────────
// Ctrl+P opens an in-app page picker (see below). The user's selection is baked
// into the PDF server-side so the OS print preview only ever receives the chosen
// pages — it never has to re-render the whole document as pages are typed into
// its own (laggy, WebView2-owned) range field.
// Print is reachable from every panel (Ctrl+P is bound document-wide), so it
// must not assume the viewer already owns the document — a file dropped
// straight into Layout lives in a session the viewer has never loaded.
async function printDocument() {
  if (!(await adoptLayoutDocument())) {
    showToast(window.t('toast.no_document_loaded'), 'error');
    return;
  }
  _showPrintModal();
}

// Push keyboard focus back into the page, several times over ~2s.
//
// One push is not enough after a native modal: Windows keeps moving focus while
// the dialog is destroyed, so a push made a moment too early is silently undone
// and every document-level shortcut stays dead (Ctrl+P above all) until the user
// happens to click something focusable.  Spreading a handful of cheap pushes
// across the teardown costs nothing and removes the timing question.
function _reclaimNativeFocus() {
  const push = () => {
    try {
      document.body.tabIndex = -1;      // body isn't focusable by default
      document.body.focus();
      window.focus();
    } catch (_) {}
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.restore_focus)
        window.pywebview.api.restore_focus();
    } catch (_) {}
  };
  push();
  [200, 600, 1200, 2000].forEach(ms => setTimeout(push, ms));
}

// Fetch a PDF of the given page IDs (null ⇒ whole document) and hand it to the
// OS print dialog via a hidden iframe.
async function _sendToPrint(pageIds) {
  showLoading('Preparing document for print…');
  try {
    const res = await apiFetch('/api/viewer/pdf', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(
        pageIds && pageIds.length
          ? {session_id: SESSION_ID, pages: pageIds}
          : {session_id: SESSION_ID})
    });
    if (!res.ok) throw new Error('Server returned ' + res.status);
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    // Keep the overlay up: the server has finished baking the PDF, but WebView2
    // still has to parse it into the hidden iframe and spin up the OS print
    // dialog — several seconds on a big document, with nothing on screen unless
    // we hold the spinner until the dialog itself takes over.
    showLoading('Opening print dialog…');

    // Hidden iframe hosts the PDF so the browser sends it to the OS print dialog
    const iframe = document.createElement('iframe');
    iframe.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;';
    document.body.appendChild(iframe);
    iframe.src = url;
    // ONCE: WebView2's built-in PDF viewer can fire `load` again as it navigates
    // internally, and a second run would open a second print dialog and register
    // a second set of teardown listeners whose `cleaned` flag knows nothing about
    // the first — one of them then yanks the iframe out from under the other.
    iframe.addEventListener('load', () => {
      // Tear down only once the print dialog is actually done — never on a fixed
      // timer.  With a heavy document each page-range keystroke makes WebView2
      // re-render its preview (seconds each), so a hard 60s timer used to fire
      // WHILE the dialog was still open, yanking the iframe + revoking the blob
      // URL out from under it and collapsing the dialog mid-selection.
      let cleaned = false;
      const cleanup = () => {
        if (cleaned) return;
        cleaned = true;
        // Move DOM focus back into the main document BEFORE removing the iframe.
        // Printing put keyboard focus inside the iframe; removing the focused
        // frame leaves Chromium's focused-frame pointer dangling and key events
        // route NOWHERE — Ctrl+P stays dead until the user happens to click a
        // focusable element (a toolbar button revives it; clicks on plain
        // divs/canvas don't). window.focus() alone is window-level and does not
        // fix frame focus, so explicitly focus a real element in this document.
        try {
          document.body.tabIndex = -1;    // body isn't focusable by default
          document.body.focus();
          window.focus();
        } catch (_) {}
        if (iframe.parentNode) iframe.parentNode.removeChild(iframe);
        // ...and push focus back into the WebView2 content HWND, which the modal
        // print dialog took at the WINDOW level (JS can't reach that).  Repeated,
        // because Windows moves focus again while the dialog tears itself down,
        // and a single push made at the wrong instant is simply undone — which is
        // the "Ctrl+P is dead after a successful print" report.  The Python-side
        // guard armed before print() covers the case where we never get here at
        // all; this covers the case where we get here too early.
        _reclaimNativeFocus();
        URL.revokeObjectURL(url);
      };
      try {
        iframe.focus();
        // Primary signal: the preview finished (printed or cancelled).
        iframe.contentWindow.addEventListener('afterprint', cleanup, { once: true });
        // Fallback for WebView2, where afterprint is unreliable: the dialog is
        // modal, so the main window regaining focus means it has closed. Delay a
        // beat so a genuine print job isn't torn down before it's spooled.
        window.addEventListener('focus', () => setTimeout(cleanup, 1000), { once: true });
        // Keep the spinner up until the dialog visibly takes over: the window
        // losing focus IS the dialog appearing (it's modal), so hide on blur.
        // Passive listeners only — never touch focus while the dialog opens
        // (print() returns immediately; stealing focus back cancels the open).
        window.addEventListener('blur', hideLoading, { once: true });
        // Safety: if no dialog ever takes focus (silent failure, or a print path
        // that doesn't blur), don't strand the overlay on screen.
        setTimeout(hideLoading, 15000);
        // Recovery of last resort: the preview is modal, so a mousedown reaching
        // the main page means it has closed — even when afterprint AND the
        // window-focus fallback both failed to fire (seen on WebView2 cancel).
        // Same 1s grace as the focus fallback so a real job isn't torn down
        // before it's spooled.
        document.addEventListener('mousedown',
          () => setTimeout(cleanup, 1000), { once: true, capture: true });
        // Arm the OS-side watcher BEFORE the dialog opens.  It does not depend
        // on afterprint, on a window focus event, or on the user clicking — all
        // three of which can miss (see begin_print_focus_guard in entry.py).
        try {
          if (window.pywebview && window.pywebview.api &&
              window.pywebview.api.begin_print_focus_guard)
            window.pywebview.api.begin_print_focus_guard();
        } catch (_) {}
        iframe.contentWindow.print();
      } catch (_) {
        // Fallback: open in new tab so the user can print from the browser toolbar
        hideLoading();
        window.open(url, '_blank');
        cleanup();
        return;
      }
      // Last-resort safety net only, so a stuck dialog can't leak the iframe/blob
      // forever. Long enough that it never races the user's interaction.
      setTimeout(cleanup, 600000);
    });
  } catch (e) {
    hideLoading();
    showToast(window.t('toast.print_failed') + e.message, 'error');
  }
}

// ── Print dialog (in-app page picker) ─────────────────────────────────────────
// Single source of truth: a Set of 0-based indices into allPages. Thumbnails,
// the range field and the mode radios all read from and write to it. Pages are
// numbered 1..N by DOCUMENT POSITION here (what the range field controls), not by
// the source page_num shown in the left panel.
let _printSel  = new Set();
let _printMode = 'all';

// Above this many pages the thumbnail grid paginates into fixed-size sections
// instead of rendering every page at once — hundreds of base64 <img> nodes both
// tank the dialog and collapse to unusable slivers. Small docs keep the single
// scroll grid unchanged. Retune here if needed.
const PRINT_SECTION_THRESHOLD = 50;
const PRINT_SECTION_SIZE      = 24;   // thumbnails per section (even across the 4-wide grid)
let _printPage = 0;                   // current section index when sectioning is active

// Sectioning is purely a rendering concern; _printSel stays a Set of absolute
// page indices, so selection/range/count keep spanning the whole document.
function _printSectioned()    { return allPages.length > PRINT_SECTION_THRESHOLD; }
function _printSectionCount() { return Math.max(1, Math.ceil(allPages.length / PRINT_SECTION_SIZE)); }
function _printSectionHasSel(s) {
  const a = s * PRINT_SECTION_SIZE, b = Math.min(a + PRINT_SECTION_SIZE, allPages.length);
  for (let i = a; i < b; i++) if (_printSel.has(i)) return true;
  return false;
}

function _showPrintModal() {
  _printPage = 0;
  _printBuildGrid();
  _printSetMode('all');
  document.getElementById('print-modal-overlay').classList.add('show');
  // Focus the range field only once the user opts into Custom; start neutral.
  const inp = document.getElementById('print-range');
  if (inp) inp.value = '';
}

function _hidePrintModal() {
  document.getElementById('print-modal-overlay').classList.remove('show');
}

function _printBuildGrid() {
  const grid = document.getElementById('print-thumb-grid');
  const sectioned = _printSectioned();
  const start = sectioned ? _printPage * PRINT_SECTION_SIZE : 0;
  const end   = sectioned ? Math.min(start + PRINT_SECTION_SIZE, allPages.length) : allPages.length;
  let html = '';
  for (let i = start; i < end; i++) {
    const p = allPages[i];
    html += `
    <div class="pthumb${_printSel.has(i) ? ' sel' : ''}" id="pthumb-${i}" onclick="_printToggleThumb(${i})" title="Page ${i + 1}">
      <span class="pthumb-num">${i + 1}</span>
      <img src="data:image/png;base64,${p.thumbnail}" alt="Page ${i + 1}" loading="lazy">
    </div>`;
  }
  grid.innerHTML = html;
  grid.scrollTop = 0;
  _printBuildSectionNav();
}

// Render (or hide) the section navigator beneath the grid. The jump menu lists
// every section's page range and flags any that hold a selected page with •, so
// picks made in other sections never feel lost.
function _printBuildSectionNav() {
  const nav = document.getElementById('print-sections');
  if (!nav) return;
  if (!_printSectioned()) { nav.hidden = true; return; }
  nav.hidden = false;

  const total = _printSectionCount();
  const sel = document.getElementById('print-sec-jump');
  if (sel) {
    let opts = '';
    for (let s = 0; s < total; s++) {
      const a = s * PRINT_SECTION_SIZE + 1;
      const b = Math.min((s + 1) * PRINT_SECTION_SIZE, allPages.length);
      const mark = _printSectionHasSel(s) ? '• ' : '';
      opts += `<option value="${s}"${s === _printPage ? ' selected' : ''}>${mark}${window.t('print_section.pages_range', { a, b })}</option>`;
    }
    sel.innerHTML = opts;
  }
  const label = document.getElementById('print-sec-label');
  if (label) label.textContent = `${_printPage + 1} / ${total}`;
  const prev = document.getElementById('print-sec-prev');
  const next = document.getElementById('print-sec-next');
  if (prev) prev.disabled = (_printPage === 0);
  if (next) next.disabled = (_printPage === total - 1);
}

function _printSectionStep(delta) { _printSectionGo(_printPage + delta); }

function _printSectionGo(page) {
  const total = _printSectionCount();
  _printPage = Math.max(0, Math.min(page, total - 1));
  _printBuildGrid();   // re-renders the slice (with correct sel state) and the nav
}

// Switch selection mode. 'all' and 'current' are presets that overwrite the
// selection; 'custom' leaves the current selection in place for the user to edit.
function _printSetMode(mode) {
  _printMode = mode;
  const radio = document.querySelector(`input[name="print-mode"][value="${mode}"]`);
  if (radio) radio.checked = true;

  if (mode === 'all') {
    _printSel = new Set(allPages.map((_, i) => i));
  } else if (mode === 'current') {
    _printSel = new Set([currentIdx]);
    // Bring the current page's section into view so its highlight is visible.
    if (_printSectioned()) {
      _printPage = Math.floor(currentIdx / PRINT_SECTION_SIZE);
      _printBuildGrid();
    }
  }
  const inp = document.getElementById('print-range');
  if (inp) {
    inp.disabled = (mode !== 'custom');
    if (mode === 'custom') { inp.focus(); }
  }
  // Presets rewrite the field; Custom is user-driven so leave their text alone.
  _printSyncUI({ skipField: mode === 'custom' });
}

function _printToggleThumb(i) {
  // Any direct thumbnail interaction means the user is hand-picking → Custom.
  if (_printMode !== 'custom') {
    _printMode = 'custom';
    const radio = document.querySelector('input[name="print-mode"][value="custom"]');
    if (radio) radio.checked = true;
    const inp = document.getElementById('print-range');
    if (inp) inp.disabled = false;
  }
  if (_printSel.has(i)) _printSel.delete(i); else _printSel.add(i);
  _printSyncUI();   // rewrite the field to match the new selection
}

function _printRangeInput() {
  _printMode = 'custom';
  const inp = document.getElementById('print-range');
  const { indices, error } = _parseRange(inp.value, allPages.length);
  _printSel = indices;
  // Don't fight the user's cursor — update thumbnails/count/error, not the field.
  _printSyncUI({ skipField: true, error });
}

// Parse "1, 3, 10-12" (1-based positions) into a Set of 0-based indices.
// Blank tokens and a trailing "N-" (range still being typed) are tolerated so
// live typing doesn't flash errors; anything else is reported.
function _parseRange(str, max) {
  const indices = new Set();
  for (let raw of String(str).split(',')) {
    const tok = raw.trim();
    if (!tok) continue;                                   // trailing/empty comma
    let m;
    if ((m = tok.match(/^(\d+)$/))) {
      const n = +m[1];
      if (n < 1 || n > max) return { indices, error: `Page ${n} is out of range (1–${max})` };
      indices.add(n - 1);
    } else if ((m = tok.match(/^(\d+)-$/))) {
      const n = +m[1];                                    // incomplete range — treat as single
      if (n < 1 || n > max) return { indices, error: `Page ${n} is out of range (1–${max})` };
      indices.add(n - 1);
    } else if ((m = tok.match(/^(\d+)-(\d+)$/))) {
      const a = +m[1], b = +m[2];
      if (a < 1 || b > max)  return { indices, error: `Range ${a}-${b} is out of range (1–${max})` };
      if (a > b)             return { indices, error: `Range ${a}-${b} is backwards` };
      for (let n = a; n <= b; n++) indices.add(n - 1);
    } else {
      return { indices, error: `“${tok}” isn’t a valid page or range` };
    }
  }
  return { indices, error: '' };
}

// Compress the current selection back into a compact "1-3, 5, 8-10" string.
function _printSerialize() {
  const nums = [..._printSel].map(i => i + 1).sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < nums.length; i++) {
    let j = i;
    while (j + 1 < nums.length && nums[j + 1] === nums[j] + 1) j++;
    parts.push(i === j ? `${nums[i]}` : `${nums[i]}-${nums[j]}`);
    i = j;
  }
  return parts.join(', ');
}

function _printSyncUI(opts = {}) {
  const { skipField = false, error = '' } = opts;
  // Thumbnail highlights — only the currently-rendered section has DOM nodes.
  document.querySelectorAll('#print-thumb-grid .pthumb').forEach(el => {
    const i = +el.id.slice('pthumb-'.length);
    el.classList.toggle('sel', _printSel.has(i));
  });
  // Refresh the section jump menu's • markers to match the new selection.
  _printBuildSectionNav();
  // Range field reflects the selection (unless the field IS the input source)
  if (!skipField) {
    const inp = document.getElementById('print-range');
    if (inp) inp.value = _printSerialize();
  }
  // Error + count + Print button
  const errEl = document.getElementById('print-range-error');
  const inp   = document.getElementById('print-range');
  if (errEl) errEl.textContent = error;
  if (inp)   inp.classList.toggle('invalid', !!error);

  const count = _printSel.size;
  const countEl = document.getElementById('print-count');
  if (countEl) countEl.textContent =
    count === 0 ? 'No pages selected' : `${count} page${count === 1 ? '' : 's'} selected`;

  const go = document.getElementById('print-go-btn');
  if (go) go.disabled = (!!error || count === 0);
}

function _printConfirm() {
  if (!_printSel.size) { showToast(window.t('toast.select_at_least_one_page'), 'error'); return; }
  // Emit IDs in document order; the server also enforces order, this keeps intent clear.
  const ids = allPages.filter((_, i) => _printSel.has(i)).map(p => p.id);
  _hidePrintModal();
  // Whole document selected ⇒ send null so the server takes its fast all-pages path.
  _sendToPrint(ids.length === allPages.length ? null : ids);
}

// Backdrop click and Escape close the picker.
document.getElementById('print-modal-overlay').addEventListener('mousedown', function(e) {
  if (e.target === this) _hidePrintModal();
});
document.addEventListener('keydown', function(e) {
  if (!document.getElementById('print-modal-overlay').classList.contains('show')) return;
  if (e.key === 'Escape') { e.stopPropagation(); _hidePrintModal(); }
  else if (e.key === 'Enter') {                       // Enter confirms if valid
    const go = document.getElementById('print-go-btn');
    if (go && !go.disabled) { e.preventDefault(); e.stopPropagation(); _printConfirm(); }
  }
}, true);

// ── Keyboard shortcuts ───────────────────────────────────────────────────────
// Capture-phase listener intercepts Ctrl+P before the browser handles it
document.addEventListener('keydown', function(e) {
  if (e.key === 'p' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    e.stopPropagation();
    printDocument();
  }
}, true);

// Toolbar shortcuts — capture phase so the browser's own Open/Save/Close/Find
// defaults are pre-empted. Open (O), Save (S) and Close-tab (W) work in EVERY
// view and route to that view's own command; New (N) and Find (F) are
// viewer-only (a new tab / the find box have no meaning in layout or editor).
// (Ctrl+W is reliably interceptable in the native app window; a plain dev browser
// may close the tab before JS sees it.)
document.addEventListener('keydown', function(e) {
  if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
  const k = (e.key || '').toLowerCase();
  if (k !== 'n' && k !== 'o' && k !== 's' && k !== 'w' && k !== 'f') return;

  const panel = _activePanelId();
  if (!panel) return;
  const inViewer = (panel === 'panel-viewer');

  // New (new tab) and Find (the search box) only apply in the viewer; elsewhere
  // let the browser keep its default.
  if ((k === 'n' || k === 'f') && !inViewer) return;

  e.preventDefault();
  e.stopPropagation();
  closeFileMenu();
  const ae = document.activeElement;

  switch (k) {
    case 'f': {                                   // focus + select the find box
      const si = document.getElementById('search-input');
      if (si) { si.focus(); si.select(); }
      break;
    }
    case 'n':                                     // viewer-only: new blank doc → new tab
      newDocument();
      break;
    case 'w':                                     // close the active tab (any view)
      if (typeof _tabs !== 'undefined' && _tabs.length > 1) _closeTab(_activeTabIdx);
      break;
    case 'o':                                     // open — per view
      if (panel === 'panel-layout' && typeof addFiles === 'function') addFiles();
      else viewerOpenViaDialog();                 // viewer & editor → open as a new tab
      break;
    case 's':                                     // save — per view
      if (ae && typeof ae.blur === 'function') ae.blur();   // flush any in-progress field edit
      if      (panel === 'panel-layout') { if (typeof saveDocument === 'function') saveDocument(); }
      else if (allPages.length)            viewerSaveToFile();
      break;
  }
}, true);

document.addEventListener('keydown', e => {
  // Don't intercept when focus is on an input / textarea
  if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) return;
  if (e.key === 'Escape' && document.getElementById('panel-viewer').classList.contains('fullscreen-mode')) {
    exitFullscreen(); return;
  }
  // Page/zoom keys belong to the viewer only.  Without this guard they also ran
  // in layout and editor, and — because '=' matches with Ctrl held — a single
  // Ctrl+= ran BOTH this handler and the editor's over the shared `zoomLevel`,
  // leaving the viewer's label and the real zoom disagreeing.
  if (_curPanel() !== 'panel-viewer') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;   // bare keys only
  if (e.key === 'ArrowUp'   || e.key === 'PageUp')   { e.preventDefault(); prevPage(); }
  if (e.key === 'ArrowDown' || e.key === 'PageDown')  { e.preventDefault(); nextPage(); }
  if (e.key === '+'  || e.key === '=') { e.preventDefault(); zoomIn(); }
  if (e.key === '-')                   { e.preventDefault(); zoomOut(); }
  // Ctrl+F (find) is handled by the capture-phase toolbar-shortcuts listener above.
});

// ── Second-instance "open in new tab" ────────────────────────────────────────
// The pywebview window owns the process lifecycle, so no SSE keep-alive is
// needed.  When a second PDF is double-clicked, the backend opens it as a tab
// by calling window.openNewTab(sid, pid) directly via pywebview.evaluate_js.
// Expose it on window so that bridge can reach it.
window.openNewTab = openNewTab;

// Report focus to the backend so second-instance "open in new tab" requests are
// routed into whichever window the user is currently using — and, crucially, so
// they still reach a live window after the original window has been closed.
function _noteActiveWindow() {
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.note_active)
      window.pywebview.api.note_active();
  } catch (_) { /* not running in the native app */ }
}
window.addEventListener('focus', _noteActiveWindow);
// Fire once on load so a freshly-shown window registers itself as active even
// before it first receives a focus event.
_noteActiveWindow();

// ── Viewer Tool System ────────────────────────────────────────────────────────
let viewerTool      = 'pointer';
let _pageTextCache  = null;
let _pageTextPageId = null;
let _selectedChars  = [];
let _selectedPageId = null;   // page_id that owns the active selection highlight
let _vDrag          = null;
let _dragPageId     = null;   // which page the current pointer drag started on

function setViewerTool(tool) {
  // Hand (or any non-pointer tool) and an armed drawing tool collide on the same
  // left-button press — the pan and the draw both start at once. Keep them
  // mutually exclusive: leaving pointer disarms any active annotation tool.
  if (tool !== 'pointer' && _activeAnnotTool) { commitDraft(); _disarmTool(); }
  viewerTool = tool;
  _updateViewerCursor();
  _updateCtxMenuActive();
  _hideCtxMenu();
  if (tool !== 'pointer') { _selectedChars = []; clearAllSelectionOverlays(); }
  if (typeof _refreshEditHitAreas === 'function') _refreshEditHitAreas();
}

function _updateViewerCursor() {
  const area = document.getElementById('canvas-area');
  if (area) area.style.cursor = viewerTool === 'hand' ? 'grab' : 'default';
}

// ── Context menu ─────────────────────────────────────────────────────────────
function _showCtxMenu(e) {
  e.preventDefault();
  _updateCtxMenuActive();
  // Capture the page + PDF-space Y under the cursor for "Add bookmark here".
  _ctxBookmarkTarget = null;
  const block = _hitTestPageBlock(e.clientX, e.clientY);
  if (block) {
    const page_id = block.dataset.pageId;
    const st = _pageBlocks.get(page_id);
    if (st && st.rendered) {
      const bRect = block.getBoundingClientRect();
      const scale = st.renderScale * zoomLevel;
      const y = st.pageH - (e.clientY - bRect.top) / scale;  // PDF y (from bottom)
      _ctxBookmarkTarget = { page_id, y };
    }
  }
  const bmItem = document.getElementById('vctx-bookmark');
  if (bmItem) bmItem.style.display = _ctxBookmarkTarget ? '' : 'none';
  const menu = document.getElementById('viewer-ctx-menu');
  menu.style.display = 'block';
  // Position near cursor, keeping menu fully on-screen
  const mw = menu.offsetWidth  || 170;
  const mh = menu.offsetHeight || 80;
  menu.style.left = Math.min(e.clientX, window.innerWidth  - mw - 8) + 'px';
  menu.style.top  = Math.min(e.clientY, window.innerHeight - mh - 8) + 'px';
}

function _hideCtxMenu() {
  const menu = document.getElementById('viewer-ctx-menu');
  if (menu) menu.style.display = 'none';
}

function _updateCtxMenuActive() {
  const cp = document.getElementById('vctx-pointer');
  const ch = document.getElementById('vctx-hand');
  if (cp) cp.classList.toggle('active', viewerTool === 'pointer');
  if (ch) ch.classList.toggle('active', viewerTool === 'hand');
}

// Close menu on any click outside it (capture phase so it fires first)
document.addEventListener('mousedown', function(e) {
  const menu = document.getElementById('viewer-ctx-menu');
  if (menu && !menu.contains(e.target)) _hideCtxMenu();
  const bmenu = document.getElementById('bm-ctx-menu');
  if (bmenu && !bmenu.contains(e.target)) _hideBookmarkCtx();
  const pmenu = document.getElementById('pg-ctx-menu');
  if (pmenu && !pmenu.contains(e.target)) _hidePageCtx();
}, true);

// ── Mouse event handlers ──────────────────────────────────────────────────────
function _hitTestPageBlock(clientX, clientY) {
  let el = document.elementFromPoint(clientX, clientY);
  while (el && el !== document.body) {
    if (el.classList?.contains('page-block')) return el;
    el = el.parentElement;
  }
  return null;
}

function _onViewerMouseDown(e) {
  if (e.button !== 0) return;
  // Let form field elements receive focus and input natively
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  _hideCtxMenu();

  if (viewerTool === 'hand') {
    const area = document.getElementById('canvas-area');
    _vDrag = { tool: 'hand', startX: e.clientX, startY: e.clientY,
               initScrollX: area.scrollLeft, initScrollY: area.scrollTop };
    area.style.cursor = 'grabbing';
    e.preventDefault();

  } else if (viewerTool === 'pointer') {
    // An annotation tool is armed → _annotMouseDown owns this drag; don't also
    // start a text-selection marquee (its blue dashed box would render over the
    // shape being drawn).
    if (_activeAnnotTool) return;
    // A live draft is being edited (e.g. a lifted image/signature/shape). The
    // draft's own elements stop propagation, so reaching here means the click
    // landed OUTSIDE the draft → commit it (the natural "click away to accept").
    if (_draft) { commitDraft(); return; }
    const pageBlock = _hitTestPageBlock(e.clientX, e.clientY);
    if (!pageBlock) return;
    const page_id = pageBlock.dataset.pageId;
    const st = _pageBlocks.get(page_id);
    if (!st?.canvas) return;
    const r  = st.canvas.getBoundingClientRect();
    const cx = e.clientX - r.left, cy = e.clientY - r.top;
    if (cx < 0 || cy < 0 || cx > r.width || cy > r.height) return;
    clearAllSelectionOverlays();
    _vDrag      = { tool: 'pointer', startX: cx, startY: cy, currX: cx, currY: cy };
    _dragPageId = page_id;
    e.preventDefault();
  }
}

function _onViewerMouseMove(e) {
  if (!_vDrag) return;
  if (_vDrag.tool === 'hand') {
    const area = document.getElementById('canvas-area');
    area.scrollLeft = _vDrag.initScrollX - (e.clientX - _vDrag.startX);
    area.scrollTop  = _vDrag.initScrollY - (e.clientY - _vDrag.startY);
  } else if (_vDrag.tool === 'pointer' && _dragPageId) {
    const st = _pageBlocks.get(_dragPageId);
    if (!st?.canvas) return;
    const r = st.canvas.getBoundingClientRect();
    _vDrag.currX = e.clientX - r.left;
    _vDrag.currY = e.clientY - r.top;
    _drawSelectionRect(_dragPageId, _vDrag.startX, _vDrag.startY, _vDrag.currX, _vDrag.currY);
  }
}

async function _onViewerMouseUp(e) {
  if (!_vDrag) return;
  const drag = _vDrag; _vDrag = null;
  if (drag.tool === 'hand') {
    document.getElementById('canvas-area').style.cursor = 'grab';
  } else if (drag.tool === 'pointer' && _dragPageId) {
    const sx = Math.min(drag.startX, drag.currX), sy = Math.min(drag.startY, drag.currY);
    const sw = Math.abs(drag.currX - drag.startX), sh = Math.abs(drag.currY - drag.startY);
    if (sw > 5 && sh > 5) {
      // Auto-detect: a marquee fully enclosing annotations selects those for
      // copy/paste; otherwise it is a plain text selection as before.
      const hits = _annotsFullyInRect(_dragPageId, sx, sy, sw, sh);
      if (hits.length) _setAreaSelection(_dragPageId, hits);
      else await _selectTextInRect(_dragPageId, sx, sy, sw, sh);
    }
    else clearAllSelectionOverlays();
    _dragPageId = null;
  }
}

// ── Selection overlay (per-page) ──────────────────────────────────────────────
function clearAllSelectionOverlays() {
  _selectedChars  = [];
  _selectedPageId = null;
  _areaSelection  = null;
  _pageBlocks.forEach(st => {
    if (st.selOv) while (st.selOv.firstChild) st.selOv.removeChild(st.selOv.firstChild);
  });
}

function _drawSelectionRect(page_id, x1, y1, x2, y2) {
  const st = _pageBlocks.get(page_id);
  if (!st?.selOv) return;
  const ov = st.selOv;
  while (ov.firstChild) ov.removeChild(ov.firstChild);
  if (st.canvas) _setSvgSize(ov, st.canvas.width, st.canvas.height);
  const ns = 'http://www.w3.org/2000/svg';
  const el = document.createElementNS(ns, 'rect');
  el.setAttribute('x',               Math.min(x1, x2));
  el.setAttribute('y',               Math.min(y1, y2));
  el.setAttribute('width',           Math.abs(x2 - x1));
  el.setAttribute('height',          Math.abs(y2 - y1));
  el.setAttribute('class', 'sel-rect');
  ov.appendChild(el);
}

// ── Area-select + copy/paste of annotations ───────────────────────────────────
// A pointer-tool marquee that fully encloses one or more annotations selects
// them (instead of text). Ctrl+C snapshots the selection as ready-to-send
// add_annotation bodies (PDF points, independent of annot_idx, so later edits
// or deletes cannot corrupt a paste). Ctrl+V replays them centred on the
// cursor, on whichever page the cursor is over — including a different page.

function _annotsFullyInRect(page_id, sx, sy, sw, sh) {
  const b   = _displayRectToPdfBounds(page_id, sx, sy, sw, sh);
  const EPS = 0.5;   // PDF-point tolerance for float noise
  const inside = (x, y) => x >= b.minX - EPS && x <= b.maxX + EPS &&
                           y >= b.minY - EPS && y <= b.maxY + EPS;
  return (_editableAnnots[page_id] || []).filter(info => {
    // Same exclusions as lift-to-edit: comments keep their popup flow.
    if (info.tool === 'sticky') return false;
    if (info.tool === 'text' && info.is_comment) return false;
    if (_isLineTool(info.tool)) {
      if (!info.line) return false;
      return inside(info.line[0], info.line[1]) && inside(info.line[2], info.line[3]);
    }
    if (!info.rect) return false;
    const rx1 = Math.min(info.rect[0], info.rect[2]), ry1 = Math.min(info.rect[1], info.rect[3]);
    const rx2 = Math.max(info.rect[0], info.rect[2]), ry2 = Math.max(info.rect[1], info.rect[3]);
    return inside(rx1, ry1) && inside(rx2, ry2);
  });
}

function _setAreaSelection(page_id, items) {
  _selectedChars = []; _selectedPageId = null;   // exclusive with text selection
  _areaSelection = { page_id, items };
  _drawAreaSelectionRings(page_id);
  showToast(items.length + window.t('toast.annots_selected'), 'info');
}

function _clearAreaSelection() {
  if (!_areaSelection) return;
  const st = _pageBlocks.get(_areaSelection.page_id);
  _areaSelection = null;
  if (st?.selOv) while (st.selOv.firstChild) st.selOv.removeChild(st.selOv.firstChild);
}

function _drawAreaSelectionRings(page_id) {
  const st = _pageBlocks.get(page_id);
  if (!st?.selOv || _areaSelection?.page_id !== page_id) return;
  const ov = st.selOv;
  while (ov.firstChild) ov.removeChild(ov.firstChild);
  if (st.canvas) _setSvgSize(ov, st.canvas.width, st.canvas.height);
  const scale = st.renderScale * zoomLevel, pageH = st.pageH;
  const PAD = 3;   // px of breathing room around each ring
  _areaSelection.items.forEach(info => {
    const g = info.rect || info.line;
    if (!g) return;
    const rx1 = Math.min(g[0], g[2]), ry1 = Math.min(g[1], g[3]);
    const rx2 = Math.max(g[0], g[2]), ry2 = Math.max(g[1], g[3]);
    const el = document.createElementNS(NS_SVG, 'rect');
    el.setAttribute('x',      rx1 * scale - PAD);
    el.setAttribute('y',      (pageH - ry2) * scale - PAD);
    el.setAttribute('width',  (rx2 - rx1) * scale + PAD * 2);
    el.setAttribute('height', (ry2 - ry1) * scale + PAD * 2);
    el.setAttribute('class', 'annot-sel-ring');
    ov.appendChild(el);
  });
}

// Map an _editableAnnots record to the add_annotation body commitDraft would
// send (field-for-field mirror of commitDraft, but geometry is already in PDF
// points). Returns null for items that cannot be replayed (empty text, unknown
// tool) — mirrors commitDraft's own skips.
function _annotInfoToAddBody(info) {
  const subtypeMap = { text:'FreeText', rect:'Square', ellipse:'Circle',
                       line:'Line', arrow:'Line', darrow:'Line', cloud:'Polygon',
                       highlight:'Highlight', image:'Stamp' };
  const subtype = subtypeMap[info.tool];
  if (!subtype) return null;
  const body = { subtype,
                 border_color: info.border_color,
                 border_width: (info.border_width != null ? info.border_width : 1) };
  if (info.author) body.author = info.author;
  if (info.tool === 'text') {
    const text = (info.text || '').trim();
    if (!text) return null;                       // don't clone empty notes
    body.rect       = info.rect.slice();
    body.text       = text;
    body.text_color = info.text_color || [0, 0, 0];
    body.font_size  = info.font_size || 11;
    body.font       = info.font || 'Helvetica';
    body.border_width = info.border_width || 0;   // 0 = no border box
    body.is_comment = false;
  } else if (_isLineTool(info.tool)) {
    body.line  = info.line.slice();
    body.arrow       = (info.tool === 'arrow' || info.tool === 'darrow');
    body.arrow_start = (info.tool === 'darrow');
    body.border_style = info.border_style || 'solid';
  } else {
    body.rect = info.rect.slice();
    if (info.tool === 'rect' || info.tool === 'ellipse') {
      body.border_style = info.border_style || 'solid';
      if (info.fill_color) body.fill_color = info.fill_color;
    }
    if (info.tool === 'image') {
      if (!info.imageData) return null;           // image fetched at copy time
      body.image = info.imageData;
    }
  }
  return body;
}

async function _copyAreaSelection() {
  if (!_areaSelection?.items.length) return;
  const items = _areaSelection.items;
  const page_id = _areaSelection.page_id;
  // Image stamps: fetch the embedded image NOW, while annot_idx is still valid
  // (the clipboard must stay usable after the original is edited or deleted).
  try {
    await Promise.all(items
      .filter(i => i.tool === 'image' && !i.imageData)
      .map(info => _vFetch('/api/viewer/stamp_image', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: SESSION_ID, page_id, annot_idx: info.annot_idx })
      })
      .then(r => r.json())
      .then(d => {
        if (d.error || !d.image) throw new Error(d.error || 'No image');
        info.imageData = d.image;
      })));
  } catch (err) {
    showToast(window.t('toast.copy_failed') + err.message, 'error');
    return;
  }
  const bodies = items.map(_annotInfoToAddBody).filter(Boolean);
  if (!bodies.length) return;
  let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
  bodies.forEach(b => {
    const g = b.rect || b.line;
    x1 = Math.min(x1, g[0], g[2]); x2 = Math.max(x2, g[0], g[2]);
    y1 = Math.min(y1, g[1], g[3]); y2 = Math.max(y2, g[1], g[3]);
  });
  _annotClipboard = { items: bodies, bbox: { x1, y1, x2, y2 } };
  showToast(bodies.length + window.t('toast.annots_copied'), 'success');
}

function _pasteAnnotClipboard() {
  if (!_annotClipboard?.items.length) return;
  // Target = page under the cursor; fall back to the current page's centre.
  let page_id = null, cx = null, cy = null;
  const pb = _lastMouse && _hitTestPageBlock(_lastMouse.x, _lastMouse.y);
  if (pb) {
    const pid = pb.dataset.pageId;
    const pst = _pageBlocks.get(pid);
    if (pst?.canvas && pst.rendered) {
      const r = pst.canvas.getBoundingClientRect();
      page_id = pid;
      cx = _lastMouse.x - r.left; cy = _lastMouse.y - r.top;
    }
  }
  if (!page_id) page_id = currentPageId;
  const st = _pageBlocks.get(page_id);
  if (!st?.rendered) { showToast(window.t('toast.paste_failed') + 'page not ready', 'error'); return; }
  const s = st.renderScale * zoomLevel;
  const px = (cx !== null) ? cx / s : st.pageW / 2;
  const py = (cy !== null) ? st.pageH - cy / s : st.pageH / 2;

  // Translate the group so its centre lands on the cursor, clamped so the
  // group's bbox stays on the page (centred if it is larger than the page).
  const bb = _annotClipboard.bbox;
  const bw = bb.x2 - bb.x1, bh = bb.y2 - bb.y1;
  let dx = px - (bb.x1 + bw / 2), dy = py - (bb.y1 + bh / 2);
  dx = (bw >= st.pageW) ? (st.pageW - bw) / 2 - bb.x1
                        : Math.min(Math.max(dx, -bb.x1), st.pageW - bb.x2);
  dy = (bh >= st.pageH) ? (st.pageH - bh) / 2 - bb.y1
                        : Math.min(Math.max(dy, -bb.y1), st.pageH - bb.y2);

  const bodies = _annotClipboard.items.map((it, i) => {
    const b = JSON.parse(JSON.stringify(it));
    const g = b.rect || b.line;
    g[0] += dx; g[2] += dx; g[1] += dy; g[3] += dy;
    b.session_id = SESSION_ID;
    b.page_id    = page_id;
    // One undo step for the whole paste: only the first add snapshots (same
    // invariant lift-edit relies on — see commitDraft's no_snapshot).
    if (i > 0) b.no_snapshot = true;
    return b;
  });
  // Queue as one unit on the commit chain so it can't interleave with draws;
  // catch so a failed paste can't wedge the commits that follow it.
  _queueAnnotOp(() => _doPasteBatch(bodies, page_id))
    .catch(err => showToast(window.t('toast.paste_failed') + err.message, 'error'));
}

async function _doPasteBatch(bodies, page_id) {
  let done = 0, last = null;
  try {
    for (const body of bodies) {
      const r = await _vFetch('/api/viewer/add_annotation', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (data.error) throw apiErr(data);
      last = data; done++;
    }
  } catch (err) {
    // Abort the rest; one undo still removes whatever partially landed
    // (the batch shares a single pre-paste snapshot).
    showToast(window.t('toast.paste_failed') +
              (done ? `(${done}/${bodies.length}) ` : '') + err.message, 'error');
  }
  if (!last) return;
  if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
  vRefreshHistory(last);
  allAnnotations[page_id] = last.annotations || [];
  if (page_id === currentPageId) pageAnnotations = allAnnotations[page_id];
  drawPageAnnotations(page_id);
  _invalidateViewerPage(page_id);   // re-render → loadEditableAnnots refreshes indices
  if (last.thumbnail) {
    const pg = allPages.find(p => p.id === page_id);
    if (pg) { pg.thumbnail = last.thumbnail; renderThumbList(); }
  }
  renderComments();
  if (done === bodies.length)
    showToast(done + window.t('toast.annots_pasted'), 'success');
}

function drawSelectionHighlights(chars, page_id) {
  const pid = page_id || _dragPageId || currentPageId;
  const st  = _pageBlocks.get(pid);
  if (!st?.selOv || !chars.length) return;
  const ov  = st.selOv;
  while (ov.firstChild) ov.removeChild(ov.firstChild);
  if (st.canvas) _setSvgSize(ov, st.canvas.width, st.canvas.height);
  const ns = 'http://www.w3.org/2000/svg';
  const pd = { pageH: st.pageH, renderScale: st.renderScale };
  chars.forEach(ch => {
    const d  = pdfToDisplay({x: ch.x, y: ch.y, w: ch.w, h: ch.h}, pd);
    const el = document.createElementNS(ns, 'rect');
    el.setAttribute('x',      d.x);
    el.setAttribute('y',      d.y);
    el.setAttribute('width',  Math.max(1, d.w));
    el.setAttribute('height', Math.max(1, d.h));
    el.setAttribute('class',  'sel-char');
    ov.appendChild(el);
  });
}

// ── Text selection logic ──────────────────────────────────────────────────────

// Reconstruct text from a sorted char array, inserting spaces at word gaps.
// (Extracted from the old inline closure so both the normal and table paths share it.)
function _charsToText(chars) {
  if (!chars.length) return '';
  const lineH = Math.max(...chars.map(c => c.h));
  const gaps  = [];
  for (let i = 1; i < chars.length; i++)
    gaps.push(chars[i].x - (chars[i - 1].x + chars[i - 1].w));

  let threshold = lineH * 0.25;
  if (gaps.length >= 4) {
    const posGaps = gaps.filter(g => g > 0);
    if (posGaps.length >= 4) {
      const sorted = [...posGaps].sort((a, b) => a - b);
      let maxJump = 0;
      for (let i = 1; i < sorted.length; i++) {
        const jump = sorted[i] - sorted[i - 1];
        if (jump > maxJump) { maxJump = jump; threshold = (sorted[i] + sorted[i - 1]) / 2; }
      }
      if (maxJump < lineH * 0.1) threshold = lineH * 0.25;
    }
  }
  let out = chars[0].c;
  for (let i = 1; i < chars.length; i++) {
    if (gaps[i - 1] > threshold) out += ' ';
    out += chars[i].c;
  }
  return out;
}

// Returns [{chars, minX, maxX}] — one entry per detected column in the char array.
// A horizontal gap >= 0.8× line height is treated as a column separator.
function _splitIntoColumnGroups(chars) {
  if (!chars.length) return [];
  const lineH       = Math.max(...chars.map(c => c.h));
  const colGapThresh = lineH * 0.8;
  const groups = [{ chars: [chars[0]], minX: chars[0].x, maxX: chars[0].x + chars[0].w }];
  for (let i = 1; i < chars.length; i++) {
    const gap = chars[i].x - (chars[i - 1].x + chars[i - 1].w);
    if (gap >= colGapThresh) groups.push({ chars: [], minX: chars[i].x, maxX: chars[i].x });
    const g = groups[groups.length - 1];
    g.chars.push(chars[i]);
    g.maxX = Math.max(g.maxX, chars[i].x + chars[i].w);
  }
  return groups;
}


// Returns { isTable, rows } where rows is array-of-array-of-string (table rows × cells).
//
// Classification is based purely on column alignment, not vertical spacing, so it
// works regardless of how tightly rows are packed.
//
// For each visual line (top → bottom):
//   • ≥2 column groups detected  →  always a new table row.
//   • 1 column group             →  check x-overlap against the last row's columns:
//       - overlaps exactly 1 column  →  wrapped continuation; text appended to that cell.
//       - overlaps 0 or 2+ columns   →  new table row (header, spanning cell, etc.).
//
// "isTable" fires when ≥2 rows have multiple columns and make up ≥50% of rows.
function _detectTable(lines) {
  if (lines.length < 2) return { isTable: false };

  // tableRows: array of column-cell arrays, each cell is { text, minX, maxX }
  const tableRows = [];

  for (const line of lines) {
    const groups = _splitIntoColumnGroups(line.chars);

    if (groups.length >= 2) {
      // Multi-column line → new table row
      tableRows.push(groups.map(g => ({
        text: _charsToText(g.chars).trim(), minX: g.minX, maxX: g.maxX
      })));

    } else {
      // Single-column line: try to match it to one column of the last row
      const g        = groups[0];
      const lastRow  = tableRows.length ? tableRows[tableRows.length - 1] : null;
      const matches  = lastRow
        ? lastRow.filter(col => g.minX < col.maxX && g.maxX > col.minX)
        : [];

      if (matches.length === 1) {
        // Falls cleanly inside one existing column → wrapped continuation
        const lineText = _charsToText(g.chars).trim();
        if (lineText) matches[0].text += (matches[0].text ? '\n' : '') + lineText;
      } else {
        // Spans 0 or multiple columns → treat as its own new row
        tableRows.push(groups.map(g => ({
          text: _charsToText(g.chars).trim(), minX: g.minX, maxX: g.maxX
        })));
      }
    }
  }

  const rows     = tableRows.map(row => row.map(col => col.text));
  const multiCol = rows.filter(r => r.length >= 2).length;
  const isTable  = multiCol >= 2 && multiCol / rows.length >= 0.5;
  return { isTable, rows };
}

// Normalise rows to the same column count and encode as RFC-4180 CSV with UTF-8 BOM.
function _rowsToCSV(rows) {
  const maxCols = Math.max(...rows.map(r => r.length));
  const lines = rows.map(cells => {
    while (cells.length < maxCols) cells.push('');
    return cells.map(v => {
      const s = String(v);
      return (s.includes(',') || s.includes('"') || s.includes('\n'))
        ? '"' + s.replace(/"/g, '""') + '"'
        : s;
    }).join(',');
  });
  return '﻿' + lines.join('\r\n');   // BOM so Excel opens UTF-8 correctly
}

function _downloadCSV(csv) {
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = 'selection.csv'; a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 1000);
}

// Open the selected table in a spreadsheet app.  In the desktop app the backend
// writes an .xlsx to a temp file and launches it in the OS default handler for
// .xlsx (Excel, or LibreOffice/OpenOffice Calc) — the old <a download> blob
// click is silently dropped by the embedded WebView2 window.  When no app is
// registered (e.g. an industrial PC without Office) we fall back to letting the
// user save the data as CSV.
async function _openTableInExcel(rows) {
  showToast(window.t('toast.opening_in_excel'), 'success');
  let opened = false;
  try {
    const res  = await apiFetch('/api/viewer/open_table', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, rows })
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    opened = !!data.opened;
  } catch (e) { /* couldn't reach/open — fall through to the save fallback */ }
  if (opened) return;
  await _saveTableAsCSV(rows);
}

// Save fallback when nothing could auto-open the table.  Uses the same native
// file picker the PDF save / document export use (works in the WebView2 window,
// unlike <a download>); plain blob download is the last resort for old runtimes.
async function _saveTableAsCSV(rows) {
  const csv  = _rowsToCSV(rows);
  if (typeof window.showSaveFilePicker === 'function') {
    try {
      const fh = await window.showSaveFilePicker({
        suggestedName: 'selection.csv',
        types: [{ description: 'CSV (comma-separated)', accept: { 'text/csv': ['.csv'] } }],
      });
      const ws = await fh.createWritable();
      await ws.write(new Blob([csv], { type: 'text/csv;charset=utf-8;' }));
      await ws.close();
      showToast(window.t('toast.saved') + fh.name, 'success');
      return;
    } catch (e) {
      if (e.name === 'AbortError') return;   // user cancelled the save dialog
      // otherwise fall through to the plain-download last resort
    }
  }
  _downloadCSV(csv);
  showToast(window.t('toast.saved_table_as_csv'), 'success');
}

// ── Table modal ───────────────────────────────────────────────────────────────
let _tableModalOnExcel = null;
let _tableModalOnCopy  = null;

function _showTableModal(onExcel, onCopy) {
  _tableModalOnExcel = onExcel;
  _tableModalOnCopy  = onCopy;
  document.getElementById('table-modal-overlay').classList.add('show');
}
function _hideTableModal() {
  document.getElementById('table-modal-overlay').classList.remove('show');
  _tableModalOnExcel = _tableModalOnCopy = null;
}
function _tableModalExcel() { const cb = _tableModalOnExcel; _hideTableModal(); if (cb) cb(); }
function _tableModalCopy()  { const cb = _tableModalOnCopy;  _hideTableModal(); if (cb) cb(); }

// Dismiss on click outside the card
document.getElementById('table-modal-overlay').addEventListener('mousedown', function(e) {
  if (e.target === this) { const cb = _tableModalOnCopy; _hideTableModal(); if (cb) cb(); }
});

function _displayRectToPdfBounds(page_id, sx, sy, sw, sh) {
  const st = _pageBlocks.get(page_id);
  const s  = (st ? st.renderScale : renderScale) * zoomLevel;
  const pH = st ? st.pageH : pageH;
  return { minX: sx / s, maxX: (sx + sw) / s,
           minY: pH - (sy + sh) / s, maxY: pH - sy / s };
}

async function _copyTextToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    // Fallback for browsers where clipboard API isn't available
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); return true; } catch(_) { return false; }
    finally { document.body.removeChild(ta); }
  }
}

async function _selectTextInRect(page_id, sx, sy, sw, sh) {
  const st = _pageBlocks.get(page_id);
  if (!st) return;

  if (_pageTextPageId !== page_id || !_pageTextCache) {
    _pageTextCache  = null;
    _pageTextPageId = null;
    // A scanned page must be OCR'd server-side before its text is selectable.
    // Poll progress under a job id so the same loading overlay used for document
    // loading shows the OCR stages.  Native pages register no progress, so the
    // poll never surfaces the overlay — no flash on the common (fast) path.
    const jobId    = newProgressId();
    const stopPoll = startProgressPoll(jobId);
    try {
      const res  = await apiFetch('/api/viewer/page_text', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({session_id: SESSION_ID, page_id, progress_id: jobId})
      });
      const data = await res.json();
      if (!data.has_text) {
        showToast(window.t('toast.no_selectable_text_on_this_page'), 'error');
        clearAllSelectionOverlays();
        return;
      }
      _pageTextCache  = data.chars || [];
      _pageTextPageId = page_id;
    } catch (err) {
      showToast(window.t('toast.could_not_load_text_data'), 'error');
      clearAllSelectionOverlays();
      return;
    } finally {
      stopPoll();
      hideLoading();
    }
  }

  if (!_pageTextCache || !_pageTextCache.length) {
    showToast(window.t('toast.no_selectable_text_on_this_page'), 'error');
    clearAllSelectionOverlays();
    return;
  }

  const b = _displayRectToPdfBounds(page_id, sx, sy, sw, sh);

  // Find all characters whose bounding boxes overlap the selection rectangle
  const selected = _pageTextCache.filter(ch =>
    ch.x + ch.w > b.minX && ch.x < b.maxX &&
    ch.y + ch.h > b.minY && ch.y < b.maxY
  );

  if (!selected.length) {
    showToast(window.t('toast.no_text_in_selection'), 'error');
    clearAllSelectionOverlays();
    return;
  }

  // Group characters into lines using vertical-overlap detection.
  // Midpoint-based grouping misplaces descenders (y, g, p, q) onto a separate
  // line because their bounding box extends below the baseline, shifting the
  // midpoint down.  Overlap correctly groups any character whose y-range
  // intersects the running line range, regardless of descender depth.
  const lines = [];
  selected.forEach(ch => {
    let bestLine = null, bestOverlap = 0;
    lines.forEach(l => {
      const ov = Math.min(ch.y + ch.h, l.maxY) - Math.max(ch.y, l.minY);
      if (ov > bestOverlap) { bestOverlap = ov; bestLine = l; }
    });
    if (bestLine) {
      bestLine.minY = Math.min(bestLine.minY, ch.y);
      bestLine.maxY = Math.max(bestLine.maxY, ch.y + ch.h);
      bestLine.chars.push(ch);
    } else {
      lines.push({ minY: ch.y, maxY: ch.y + ch.h, chars: [ch] });
    }
  });
  lines.sort((a, b) => b.maxY - a.maxY);   // higher maxY = higher on page (PDF y increases upward)
  lines.forEach(l => l.chars.sort((a, b) => a.x - b.x));

  const text = lines.map(l => _charsToText(l.chars)).join('\n').trim();

  _selectedChars  = selected;
  _selectedPageId = page_id;
  _lastSelectedText = text;          // used to pre-fill bookmark titles
  drawSelectionHighlights(selected, page_id);

  if (!text) {
    showToast(window.t('toast.no_text_in_selection'), 'error');
    return;
  }

  // ── Table detection ───────────────────────────────────────────────────────
  const { isTable, rows } = _detectTable(lines);

  if (isTable) {
    _showTableModal(
      // "Open in Excel"
      () => { _openTableInExcel(rows); },
      // "Copy as text"
      async () => {
        const ok      = await _copyTextToClipboard(text);
        const preview = text.length > 40 ? text.slice(0, 40) + '…' : text;
        showToast(ok ? window.t('toast.copied') + preview
                    : window.t('toast.text_selected_ctrl_c'), 'success');
      }
    );
  } else {
    const ok      = await _copyTextToClipboard(text);
    const preview = text.length > 40 ? text.slice(0, 40) + '…' : text;
    showToast(ok ? window.t('toast.copied') + preview
                    : window.t('toast.text_selected_ctrl_c'), 'success');
  }
}

// ── Document Properties (metadata) ───────────────────────────────────────────
async function showDocProperties() {
  if (!SESSION_ID) return;
  const modal = document.getElementById('doc-props-modal');
  if (!modal) return;
  try {
    const res  = await apiFetch('/api/metadata', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID, action: 'get'})
    });
    const data = await res.json();
    const m    = data.metadata || {};
    document.getElementById('meta-title').value    = m.title    || '';
    document.getElementById('meta-author').value   = m.author   || '';
    document.getElementById('meta-subject').value  = m.subject  || '';
    document.getElementById('meta-keywords').value = m.keywords || '';
  } catch(_) {}
  modal.style.display = 'flex';
  setTimeout(() => document.getElementById('meta-title').focus(), 50);
}

function closeDocProperties() {
  const modal = document.getElementById('doc-props-modal');
  if (modal) modal.style.display = 'none';
}

async function saveDocProperties() {
  if (_blockedByRestriction()) return;
  if (!SESSION_ID) return;
  const metadata = {
    title:    document.getElementById('meta-title').value.trim(),
    author:   document.getElementById('meta-author').value.trim(),
    subject:  document.getElementById('meta-subject').value.trim(),
    keywords: document.getElementById('meta-keywords').value.trim(),
  };
  try {
    await apiFetch('/api/metadata', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID, action: 'set', metadata})
    });
    // Properties are held in the session and written at save time, so the
    // document now differs from the file on disk.
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    closeDocProperties();
    showToast(window.t('toast.properties_saved_will_be_written_on'), 'success');
  } catch(e) {
    showToast(window.t('toast.failed_to_save_properties') + e.message, 'error');
  }
}

// ── Password / encryption ─────────────────────────────────────────────────────
let _isProtected        = false;   // whether the current session has a save password
let _openPasswordRef    = null;    // ref id for an encrypted upload awaiting password
let _openPasswordIsSession = false; // true = unlocking preloaded session file

function _updateLockButton(protected_) {
  _isProtected = protected_;
  const btn    = document.getElementById('lock-btn');
  const open   = document.getElementById('lock-icon-open');
  const closed = document.getElementById('lock-icon-closed');
  if (!btn) return;
  btn.classList.toggle('protected', protected_);
  btn.title = protected_ ? 'Protected — click to change' : 'Password protect…';
  if (open)   open.style.display   = protected_ ? 'none' : '';
  if (closed) closed.style.display = protected_ ? ''     : 'none';
}

// ── Save-password modal ───────────────────────────────────────────────────────
function showPasswordProtectModal() {
  if (_blockedByRestriction()) return;
  if (!SESSION_ID) return;
  const modal = document.getElementById('pw-protect-modal');
  if (!modal) return;
  document.getElementById('pp-set-section').style.display       = _isProtected ? 'none' : '';
  document.getElementById('pp-protected-section').style.display = _isProtected ? ''     : 'none';
  document.getElementById('pp-password').value  = '';
  document.getElementById('pp-confirm').value   = '';
  document.getElementById('pp-error').style.display = 'none';
  modal.style.display = 'flex';
  if (!_isProtected) setTimeout(() => document.getElementById('pp-password').focus(), 50);
}

function closePasswordProtectModal() {
  const modal = document.getElementById('pw-protect-modal');
  if (modal) modal.style.display = 'none';
}

function showChangePassword() {
  document.getElementById('pp-protected-section').style.display = 'none';
  document.getElementById('pp-set-section').style.display       = '';
  document.getElementById('pp-password').value = '';
  document.getElementById('pp-confirm').value  = '';
  setTimeout(() => document.getElementById('pp-password').focus(), 50);
}

async function savePasswordProtection() {
  const pw  = document.getElementById('pp-password').value;
  const cfm = document.getElementById('pp-confirm').value;
  const err = document.getElementById('pp-error');
  if (!pw) { err.textContent = 'Please enter a password.'; err.style.display = ''; return; }
  if (pw !== cfm) { err.textContent = 'Passwords do not match.'; err.style.display = ''; return; }
  err.style.display = 'none';
  try {
    await apiFetch('/api/set_password', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID, password: pw})
    });
    // Encryption is applied at save time, so the session now differs from disk.
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    _updateLockButton(true);
    closePasswordProtectModal();
    showToast(window.t('toast.document_will_be_encrypted_when_saved'), 'success');
  } catch(e) { err.textContent = 'Failed: ' + e.message; err.style.display = ''; }
}

async function removePasswordProtection() {
  try {
    await apiFetch('/api/set_password', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID, password: ''})
    });
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    _updateLockButton(false);
    closePasswordProtectModal();
    showToast(window.t('toast.password_protection_removed'), 'success');
  } catch(e) { showToast(window.t('toast.failed') + e.message, 'error'); }
}

// ── Read-only (permission-restricted) documents ───────────────────────────────
// A PDF carrying only an owner password opens with no prompt but forbids
// changes.  We show every page, switch every editing control off, and flag the
// state in the toolbar so the operator can see why nothing can be changed.
let IS_RESTRICTED = false;

// Controls that change the document.  Viewing, search, zoom, print, theme and
// the File menu stay live.
const _RESTRICTED_CONTROLS = [
  'v-btn-layout', 'v-btn-edit',      // header: Layout and Edit panels
  'v-btn-undo', 'v-btn-redo',
  'lock-btn',                        // save-time password protection
  'annot-tools',                     // every tool on every tab (not the list)
  'lp-add-bookmark', 'lp-auto-bookmark',
];

function _applyRestrictedMode(on) {
  IS_RESTRICTED = !!on;
  for (const id of _RESTRICTED_CONTROLS) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('ro-locked', IS_RESTRICTED);
  }
  const badge = document.getElementById('restricted-badge');
  if (badge) badge.style.display = IS_RESTRICTED ? '' : 'none';
}

// Guard for edit actions reachable by other routes (keyboard shortcuts, context
// menus) that never pass through a greyed-out button.
function _blockedByRestriction() {
  if (!IS_RESTRICTED) return false;
  showToast(window.t('toast.document_is_read_only'), 'error');
  return true;
}

// Badge click — the owner password lifts the restrictions.
function unlockRestricted() {
  if (!SESSION_ID) return;
  const name = (allPages[0] && allPages[0].filename) || 'This document';
  _showOpenPasswordDialog(name, null, true);
}

// ── Open-file password dialog ─────────────────────────────────────────────────
function _showOpenPasswordDialog(filename, ref, isSession) {
  _openPasswordRef       = ref       || null;
  _openPasswordIsSession = isSession || false;
  const modal = document.getElementById('pw-open-modal');
  if (!modal) return;
  // A restricted document is already on screen — the password unlocks editing
  // rather than opening the file, so say so.
  document.getElementById('pw-open-filename').textContent = window.t(
    IS_RESTRICTED ? 'pw_open_modal.unlock_body' : 'pw_open_modal.open_body',
    {file: filename});
  document.getElementById('pw-open-input').value = '';
  document.getElementById('pw-open-error').style.display = 'none';
  modal.style.display = 'flex';
  setTimeout(() => document.getElementById('pw-open-input').focus(), 50);
}

function cancelOpenPassword() {
  document.getElementById('pw-open-modal').style.display = 'none';
  _openPasswordRef = null;
  hideLoading();
  _updateViewerEmptyState();
}

async function submitOpenPassword() {
  const pw  = document.getElementById('pw-open-input').value;
  const err = document.getElementById('pw-open-error');
  if (!pw) { err.textContent = 'Please enter a password.'; err.style.display = ''; return; }
  err.style.display = 'none';
  showLoading('Opening…');

  try {
    if (_openPasswordIsSession) {
      // Preloaded file (command-line or file-association)
      const res  = await apiFetch('/api/unlock_session', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({session_id: SESSION_ID, password: pw})
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error === 'Incorrect password' ? 'Incorrect password.' : data.error);
      document.getElementById('pw-open-modal').style.display = 'none';
      currentPageId = data.page_id;
      // loadPageList() re-syncs the lock button and read-only state from the
      // server, which now knows whether an open password is still in force.
      await loadPageList();
      await Promise.all([loadAllAnnotations(), loadFormFields(), loadAllLinks()]);
    } else {
      // Deferred upload ref
      const res  = await apiFetch('/api/retry_encrypted', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ref: _openPasswordRef, password: pw, session_id: SESSION_ID})
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error === 'Incorrect password' ? 'Incorrect password.' : data.error);
      document.getElementById('pw-open-modal').style.display = 'none';
      _openPasswordRef = null;
      const newSession = data.session_id;
      const firstPage  = data.pages && data.pages.length ? data.pages[0].id : '';
      if (SESSION_ID) {
        openNewTab(newSession, firstPage);
      } else {
        SESSION_ID = newSession;
        if (_tabs[_activeTabIdx]) _tabs[_activeTabIdx].session_id = SESSION_ID;
        _updateLockButton(true);
        await loadPageList();
        await Promise.all([loadAllAnnotations(), loadFormFields(), loadAllLinks()]);
      }
    }
  } catch(e) {
    hideLoading();
    err.textContent = e.message || 'Incorrect password.';
    err.style.display = '';
    document.getElementById('pw-open-input').select();
  }
}

// ── Viewer form field overlay ─────────────────────────────────────────────────
let _allFormFields  = {};   // page_id → [field_dict]
let _ffSaveTimer    = null;
const _ffPending    = {};   // field_id → value (batched before API call)

// ── Field tool: drag on the page to place a widget ──────────────────────────
//
// The coordinates sent are the VIEWER DISPLAY FRAME, exactly as drawn — the
// server translates into PDF user space.  Do not "helpfully" add the page's
// CropBox origin here: one wire convention, server-side translation, is what
// keeps this correct on cropped and rotated pages (CLAUDE.md, "Viewer page
// coordinates").
//
// Unlike a drawing tool this never creates a draft, because a form field is a
// real widget in the PDF rather than an annotation object.  Placement is a
// single POST, queued like every other mutation so it cannot overtake one still
// in flight.

let _pageToolDrag = null;   // {tool, page_id, st, block, bRect, startX, startY, box}

const _PAGE_TOOL_MIN_PX = 6;   // a drag smaller than this is a click, not an edit

// Per-tool drag behaviour: the preview class, and what to do with the finished
// rect.  Both tools drag out a box on a page and post it in the display frame,
// so the drag itself is shared — only these two differ.
const _PAGE_TOOL_DRAG = {
  formfield: {
    previewClass: 'ff-draw-box',
    commit: (page_id, r) => _ffPlace(page_id, {
      // `|| 'text'` because _ctlVal returns the element's value, which is ''
      // when the select has no selection — the route rejects an empty type, and
      // the sensible default is the commonest field.
      type: _ctlVal('at-ff-type', 'text') || 'text',
      x1: r.x1, y1: r.y1, x2: r.x2, y2: r.y2,
    }),
  },
  redact: {
    previewClass: 'rx-draw-box',
    commit: (page_id, r) => _rxApply(page_id, r),
  },
  crop: {
    previewClass: 'cr-draw-box',
    commit: (page_id, r) => _cropApply(page_id, r),
  },
  // Add text: a drag gives a wrapped box, a plain click a single line whose
  // baseline is where you clicked.
  pagetext: {
    previewClass: 'pt-draw-box',
    commit: (page_id, r) => _ptOpenBox(page_id, r),
    click:  (d) => _ptOpenLine(d.page_id, d.st, d.block, d.startX, d.startY),
  },
};

function _pageToolDragStart(tool, page_id, st, block, bRect, startX, startY) {
  const spec = _PAGE_TOOL_DRAG[tool];
  if (!spec) return;
  const box = document.createElement('div');
  box.className = spec.previewClass;
  block.appendChild(box);
  _pageToolDrag = { tool, page_id, st, block, bRect, startX, startY, box,
                    curX: startX, curY: startY };
  _pageToolDragUpdate();
  document.addEventListener('mousemove', _pageToolDragMove);
  document.addEventListener('mouseup',   _pageToolDragEnd);
}

function _pageToolDragUpdate() {
  const d = _pageToolDrag;
  if (!d) return;
  const x = Math.min(d.startX, d.curX), y = Math.min(d.startY, d.curY);
  const w = Math.abs(d.curX - d.startX), h = Math.abs(d.curY - d.startY);
  d.box.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px`;
}

function _pageToolDragMove(e) {
  if (!_pageToolDrag) return;
  _pageToolDrag.curX = e.clientX - _pageToolDrag.bRect.left;
  _pageToolDrag.curY = e.clientY - _pageToolDrag.bRect.top;
  _pageToolDragUpdate();
}

function _pageToolDragEnd(e) {
  document.removeEventListener('mousemove', _pageToolDragMove);
  document.removeEventListener('mouseup',   _pageToolDragEnd);
  const d = _pageToolDrag; _pageToolDrag = null;
  if (!d) return;
  if (d.box.parentNode) d.box.parentNode.removeChild(d.box);

  const x1px = Math.min(d.startX, d.curX), x2px = Math.max(d.startX, d.curX);
  const y1px = Math.min(d.startY, d.curY), y2px = Math.max(d.startY, d.curY);
  if ((x2px - x1px) < _PAGE_TOOL_MIN_PX || (y2px - y1px) < _PAGE_TOOL_MIN_PX) {
    // Too small to be a box.  Most tools ignore it; Add text treats it as the
    // click that opens a one-line entry.
    const click = _PAGE_TOOL_DRAG[d.tool].click;
    if (click) click(d);
    return;
  }

  // Display px → display-frame points (y-up), which is what the routes expect.
  // The CropBox origin is the SERVER's to add: one wire convention.
  const s  = d.st.renderScale * zoomLevel;
  const ph = d.st.pageH;
  _PAGE_TOOL_DRAG[d.tool].commit(d.page_id, {
    x1: x1px / s, y1: ph - y2px / s,
    x2: x2px / s, y2: ph - y1px / s,
  });
}

// ── Editing a placed field: select, move, resize, delete ────────────────────
//
// While the Field tool is armed the form overlay stops being fillable and
// becomes editable: the widgets themselves go pointer-events:none and a
// transparent hit-area per field sits on top.  Without that you cannot grab a
// text field to move it — the click lands in the input and starts typing.
//
// Geometry goes to the server in the DISPLAY frame, like every other page-tool
// mutation.  A drag that never moved sends nothing: the editor's ffDrag has the
// same rule, because a press that moved nothing should neither spend a round
// trip nor turn a saved document unsaved.

let _ffSel  = null;   // {page_id, field_id}
let _ffDrag = null;   // {page_id, id, mode, corner, st, bRect, sx, sy, orig, moved}

const _FF_HANDLE_PX = 9;   // corner grab size
const _FF_DRAG_SLOP = 3;   // px before a press counts as a drag

function _ffEditing() { return _activeAnnotTool === 'formfield'; }

function _ffRefreshOverlays() {
  _pageBlocks.forEach((st, pid) => { if (st.rendered) _populateFormOverlay(pid, st); });
}

/** Overlay furniture for one field while the Field tool is armed. */
function _ffDecorate(ov, field, r, page_id) {
  const hit = document.createElement('div');
  hit.className = 'ff-hit' + (_ffSel && _ffSel.field_id === field.id ? ' sel' : '');
  hit.style.cssText =
    `left:${r.x.toFixed(1)}px;top:${r.y.toFixed(1)}px;` +
    `width:${r.w.toFixed(1)}px;height:${r.h.toFixed(1)}px;`;
  hit.title = field.name || field.type;
  hit.addEventListener('mousedown', (e) => _ffGrab(e, page_id, field, null));
  ov.appendChild(hit);

  if (_ffSel && _ffSel.field_id === field.id) {
    // Corners: 0 = TL, 1 = TR, 2 = BL, 3 = BR, matching the editor's ffDrag.
    [[0, r.x, r.y], [1, r.x + r.w, r.y],
     [2, r.x, r.y + r.h], [3, r.x + r.w, r.y + r.h]].forEach(([c, hx, hy]) => {
      const g = document.createElement('div');
      g.className = 'ff-handle';
      g.style.cssText =
        `left:${(hx - _FF_HANDLE_PX / 2).toFixed(1)}px;` +
        `top:${(hy - _FF_HANDLE_PX / 2).toFixed(1)}px;` +
        `width:${_FF_HANDLE_PX}px;height:${_FF_HANDLE_PX}px;`;
      g.addEventListener('mousedown', (e) => _ffGrab(e, page_id, field, c));
      ov.appendChild(g);
    });
  }
}

function _ffGrab(e, page_id, field, corner) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();          // must not start a placement drag underneath
  const st = _pageBlocks.get(page_id);
  if (!st?.rendered) return;
  const block = document.getElementById('page-block-' + page_id);
  if (!block) return;

  const wasSelected = _ffSel && _ffSel.field_id === field.id;
  _ffSel = { page_id, field_id: field.id };
  const bRect = block.getBoundingClientRect();
  _ffDrag = {
    page_id, id: field.id,
    mode: (corner === null || corner === undefined) ? 'move' : 'resize',
    corner, st, bRect,
    sx: e.clientX - bRect.left, sy: e.clientY - bRect.top,
    orig: { x1: field.x1, y1: field.y1, x2: field.x2, y2: field.y2 },
    moved: false,
  };
  document.addEventListener('mousemove', _ffDragMove);
  document.addEventListener('mouseup',   _ffDragEnd);
  if (!wasSelected) _populateFormOverlay(page_id, st);   // draw the handles
}

function _ffDragMove(e) {
  const d = _ffDrag;
  if (!d) return;
  const dxPx = (e.clientX - d.bRect.left) - d.sx;
  const dyPx = (e.clientY - d.bRect.top)  - d.sy;
  if (!d.moved &&
      Math.abs(dxPx) < _FF_DRAG_SLOP && Math.abs(dyPx) < _FF_DRAG_SLOP) return;
  d.moved = true;

  const s  = d.st.renderScale * zoomLevel;
  const dx = dxPx / s, dy = -dyPx / s;     // screen y is down, PDF y is up
  const o  = d.orig;
  let r;
  if (d.mode === 'move') {
    r = { x1: o.x1 + dx, y1: o.y1 + dy, x2: o.x2 + dx, y2: o.y2 + dy };
  } else {
    r = { ...o };
    switch (d.corner) {
      case 0: r.x1 = o.x1 + dx; r.y2 = o.y2 + dy; break;
      case 1: r.x2 = o.x2 + dx; r.y2 = o.y2 + dy; break;
      case 2: r.x1 = o.x1 + dx; r.y1 = o.y1 + dy; break;
      case 3: r.x2 = o.x2 + dx; r.y1 = o.y1 + dy; break;
    }
  }
  // Live feedback only — the server is told once, on release.
  const f = (_allFormFields[d.page_id] || []).find(x => x.id === d.id);
  if (f) {
    f.x1 = Math.min(r.x1, r.x2); f.x2 = Math.max(r.x1, r.x2);
    f.y1 = Math.min(r.y1, r.y2); f.y2 = Math.max(r.y1, r.y2);
    _populateFormOverlay(d.page_id, d.st);
  }
}

function _ffDragEnd() {
  document.removeEventListener('mousemove', _ffDragMove);
  document.removeEventListener('mouseup',   _ffDragEnd);
  const d = _ffDrag; _ffDrag = null;
  if (!d || !d.moved) return;          // a click is a selection, not an edit
  const f = (_allFormFields[d.page_id] || []).find(x => x.id === d.id);
  if (!f) return;
  _ffUpdate(d.page_id, d.id, { x1: f.x1, y1: f.y1, x2: f.x2, y2: f.y2 });
}

function _ffUpdate(page_id, field_id, updates) {
  return _queueAnnotOp(() => _vFetch('/api/viewer/update_form_field', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: SESSION_ID, page_id, field_id, updates}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    const list = _allFormFields[page_id] || [];
    const i = list.findIndex(x => x.id === field_id);
    if (i >= 0 && data.field) list[i] = data.field;
    const st = _pageBlocks.get(page_id);
    if (st?.rendered) _populateFormOverlay(page_id, st);
  })
  .catch(err => {
    showToast(err.message || String(err), 'error');
    // The drag already moved the field on screen (live feedback); the server
    // did not.  Put the overlay back to the truth rather than leave it lying.
    loadFormFields();
  }));
}

/** Del / Backspace removes the selected field while the Field tool is armed. */
function _ffKey(e) {
  if (!_ffEditing() || !_ffSel) return;
  if (e.key !== 'Delete' && e.key !== 'Backspace') return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
            t.tagName === 'SELECT' || t.isContentEditable)) return;
  e.preventDefault();
  const { page_id, field_id } = _ffSel;
  _ffSel = null;
  _ffDelete(page_id, field_id);
}

function _ffPlace(page_id, field) {
  return _queueAnnotOp(() => _vFetch('/api/viewer/add_form_field', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: SESSION_ID, page_id, field}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    // Trust the echoed field rather than what was drawn: the server is the one
    // that decided the name and id, and it round-trips the geometry.
    (_allFormFields[page_id] = _allFormFields[page_id] || []).push(data.field);
    const st = _pageBlocks.get(page_id);
    if (st?.rendered) _populateFormOverlay(page_id, st);
    showToast(window.t('toast.field_added'), 'success');
  })
  .catch(err => showToast(err.message || String(err), 'error')));
}

// ── Watermark: stamp text across one page ───────────────────────────────────
//
// The odd one out among the page tools: it takes NO geometry.  apply_edits
// derives the page centre from the CropBox itself, which is why a watermark
// lands correctly even on a cropped+rotated page that Redact has to refuse
// (tests/test_viewer_watermark.py).  So there is nothing to drag — the panel
// previews live over the page in view and a button stamps it.
//
// The preview is an SVG overlay rather than a canvas draw, matching how the
// viewer draws every other overlay, and is deliberately approximate: it shows
// position, size, angle, colour and density so the user can judge the settings.
// The authority is the server.

// The live preview node, held directly rather than looked up by class when it
// needs removing: this code created it, so this code owns the reference, and a
// stray querySelectorAll would also pick up a preview on a page block that has
// since been rebuilt underneath us.
let _wmPreviewEl = null;

function _wmViewerParams() {
  return {
    text:     (_ctlVal('at-wm-text', 'DRAFT') || '').trim(),
    color:     _ctlVal('at-wm-color', '#808080') || '#808080',
    opacity:   (parseInt(_ctlVal('at-wm-opacity', '25'), 10) || 25) / 100,
    size:      parseInt(_ctlVal('at-wm-size', '60'), 10) || 60,
    angleDeg:  parseFloat(_ctlVal('at-wm-angle', '45')) || 0,
    layout:    _ctlVal('at-wm-layout', 'center') || 'center',
  };
}

function wmViewerOpacityLabel() {
  const el = document.getElementById('at-wm-opacity-lbl');
  if (el) el.textContent = _ctlVal('at-wm-opacity', '25') + '%';
}

function _wmClearPreview() {
  if (_wmPreviewEl && _wmPreviewEl.parentNode) {
    _wmPreviewEl.parentNode.removeChild(_wmPreviewEl);
  }
  _wmPreviewEl = null;
}

function wmViewerPreview() {
  _wmClearPreview();
  if (_activeAnnotTool !== 'watermark') return;
  const p  = _wmViewerParams();
  const st = _pageBlocks.get(currentPageId);
  if (!p.text || !st || !st.rendered || !st.block) return;

  const ns = 'http://www.w3.org/2000/svg';
  const w  = Math.round(_logicalW(st) * zoomLevel);
  const h  = Math.round(_logicalH(st) * zoomLevel);
  const s  = st.renderScale * zoomLevel;        // points → CSS px
  const svg = document.createElementNS(ns, 'svg');
  // setAttribute rather than className.baseVal (which the neighbouring overlays
  // use): both work on an SVG element in a browser, and this one is also
  // readable back, so the preview's presence can be asserted.
  svg.setAttribute('class', 'page-wm-preview');
  _setSvgSize(svg, w, h);

  const px = p.size * s;
  const mk = (cx, cy) => {
    const t = document.createElementNS(ns, 'text');
    t.setAttribute('x', cx);
    t.setAttribute('y', cy);
    t.setAttribute('text-anchor', 'middle');
    t.setAttribute('dominant-baseline', 'central');
    t.setAttribute('font-family', 'Helvetica, Arial, sans-serif');
    t.setAttribute('font-weight', 'bold');
    t.setAttribute('font-size', px);
    t.setAttribute('fill', p.color);
    t.setAttribute('fill-opacity', p.opacity);
    t.setAttribute('transform', `rotate(${-p.angleDeg} ${cx} ${cy})`);
    t.textContent = p.text;
    svg.appendChild(t);
  };

  if (p.layout === 'tiled') {
    // Mirrors the server's spacing formula closely enough to judge density.
    const spacing = Math.max(p.text.length * px * 0.6 * 1.8, px * 4);
    const steps   = Math.ceil(Math.max(w, h) / spacing) + 1;
    for (let i = -steps; i <= steps; i++)
      for (let j = -steps; j <= steps; j++)
        mk(w / 2 + i * spacing, h / 2 + j * spacing);
  } else {
    mk(w / 2, h / 2);
  }
  st.block.appendChild(svg);
  _wmPreviewEl = svg;
}

function wmViewerApply() {
  const p = _wmViewerParams();
  if (!p.text) { showToast(window.t('toast.enter_watermark_text_first'), 'error'); return; }
  if (!currentPageId) return;
  const page_id = currentPageId;
  return _queueAnnotOp(() => _vFetch('/api/viewer/watermark', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: SESSION_ID, page_id, watermark: p}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    vRefreshHistory(data);
    _invalidateViewerPage(page_id);
    if (data.thumbnail) {
      const pg = allPages.find(p2 => p2.id === page_id);
      if (pg) { pg.thumbnail = data.thumbnail; renderThumbList(); }
    }
    // The stamp is in the page now; leaving the preview up would double it.
    _wmClearPreview();
    showToast(window.t('toast.watermark_stamped'), 'success');
  })
  .catch(err => showToast(err.message || String(err), 'error')));
}

// Watermark every page — a DOCUMENT-level command from the File menu, not a
// panel tool.  It rewrites every page in one pass, so it cannot join the
// per-page undo history and it clears that history rather than leaving entries
// that would restore one page to a pre-watermark state.  Both facts are in the
// confirmation, because neither is guessable and neither is reversible.
async function wmViewerApplyAll() {
  if (_blockedByRestriction()) return;
  if (!SESSION_ID || !allPages.length) {
    showToast(window.t('toast.open_a_document_first_to_use'));
    return;
  }
  // Arm the panel so the settings being applied are the ones on screen, and the
  // user can see a preview of them before confirming.
  if (_activeAnnotTool !== 'watermark') {
    if (rightCollapsed) toggleRightPanel();
    selectAnnotTool('watermark');
  }
  const p = _wmViewerParams();
  if (!p.text) { showToast(window.t('toast.enter_watermark_text_first'), 'error'); return; }

  const ok = await showConfirm(
    window.t('confirm.watermark_all_msg', { count: allPages.length, text: p.text }),
    window.t('confirm.watermark_all_title'),
    window.t('confirm.watermark_all_ok'));
  if (!ok) return;

  showLoading(window.t('toast.watermarking_every_page'));
  return _queueAnnotOp(() => _vFetch('/api/viewer/watermark_all', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: SESSION_ID, watermark: p}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    vRefreshHistory(data);                 // the server cleared the history
    _wmClearPreview();
    (data.updated || []).forEach(u => {
      const pg = allPages.find(p2 => p2.id === u.id);
      if (pg && u.thumbnail) pg.thumbnail = u.thumbnail;
      _invalidateViewerPage(u.id);
    });
    renderThumbList();
    showToast(data.failed
      ? window.t('toast.watermark_all_partial', { failed: data.failed })
      : window.t('toast.watermark_all_done', { count: (data.updated || []).length }),
      data.failed ? 'error' : 'success');
  })
  .catch(err => showToast(err.message || String(err), 'error'))
  .finally(() => hideLoading()));
}

// ── The Pointer: edit the page's OWN text ───────────────────────────────────
//
// The gate, by design: text editing is only reachable with the Tools panel open
// AND this tool armed.  There is no separate "editing mode" and no modal panel
// — but a click on the page must not start editing text unless the user has
// deliberately asked for it, or people trying to select text for copying would
// find themselves rewriting the document.
//
// Elements load PER PAGE, on demand.  The extraction behind /api/viewer/
// page_elements runs OCR on a scanned page, so loading every page on screen
// would be ruinous on a long scan; only the page in view is loaded, and it
// reloads when the user scrolls to another one.
//
// Each edit is committed immediately — one request, one byte snapshot, one
// Ctrl+Z — rather than staged behind an Apply button.  That keeps one undo
// system instead of two, which was the biggest risk identified when this merge
// was planned, and it means the ids from the last load are stale afterwards:
// every successful edit reloads the element model.

let _etPage  = null;    // page_id the loaded elements belong to
let _etItems = [];      // the editable runs on that page
let _etSel   = null;    // id of the selected run
let _etEntry = null;    // {el, wrap, item} while the inline editor is open
let _etLoading = false;
// The runs come in the extraction's own frame, which is absolute user space:
// on a CROPPED page that is the display frame plus the CropBox origin.  The
// server says where that origin is (page_left / page_top); ignoring it drew
// every hit-area off by the crop and moved text twice as far as dragged.
let _etPageLeft = 0;
let _etPageTop  = null; // null = "same as the page height" (no offset)

function _etOff(st) {
  const oy = (_etPageTop == null || !st) ? 0 : _etPageTop - st.pageH;
  return { x: _etPageLeft || 0, y: Math.abs(oy) < 0.01 ? 0 : oy };
}

/** Screen box (px, relative to the page block) of an item-frame rectangle. */
function _etBoxPx(st, x1, y1, x2, y2) {
  const s = st.renderScale * zoomLevel, o = _etOff(st);
  return { left: (x1 - o.x) * s, top: (st.pageH - (y2 - o.y)) * s,
           width: (x2 - x1) * s, height: (y2 - y1) * s };
}

function _etActive() {
  // The tools panel must be OPEN, not merely the tool armed: collapsing the
  // panel is how the user puts the whole toolset away.
  return _activeAnnotTool === 'edittext' && !rightCollapsed;
}

function _etArm() {
  document.addEventListener('keydown', _etKey);
  // A canvas measures with a FALLBACK face until the webfont has loaded, and
  // the row/area editor wraps by measuring.  Ask for the bundled faces now so
  // they are there by the time anything is edited.
  try {
    if (document.fonts && document.fonts.load) {
      for (const f of ["12px 'PWCalibri'", "bold 12px 'PWCalibri'",
                       "12px 'PWArial'", "12px 'PWTimes'", "12px 'PWCourier'"]) {
        document.fonts.load(f).catch(() => {});
      }
    }
  } catch (_) {}
  document.addEventListener('mousedown', _etPageClick, true);
  _etLoadForCurrentPage();
}

/** A click on a page the Pointer is not on moves it there, and — once that
 *  page's text has loaded — selects the word that was clicked. */
function _etPageClick(e) {
  if (!_etActive() || e.button !== 0 || _etEntry) return;
  const t = e.target;
  const block = t && t.closest ? t.closest('.page-block') : null;
  if (!block || !block.id || !block.id.startsWith('page-block-')) return;
  const pid = block.id.slice('page-block-'.length);
  if (pid === _etPage) return;
  e.preventDefault();
  e.stopPropagation();
  _etPending = { pid, x: e.clientX, y: e.clientY };
  _etLoadPage(pid);
}

function _etServePendingClick(pid) {
  const P = _etPending;
  _etPending = null;
  if (!P || P.pid !== pid || !document.elementFromPoint) return;
  const el = document.elementFromPoint(P.x, P.y);
  const ov = document.getElementById('et-ov-' + pid);
  if (!el || !ov || el.parentNode !== ov) return;
  const idx = [...ov.children].indexOf(el);
  if (idx >= 0 && _etItems[idx]) _etSelect(_etItems[idx]);
}

function _etDisarm() {
  document.removeEventListener('keydown', _etKey);
  document.removeEventListener('mousedown', _etPageClick, true);
  _etWant = null; _etPending = null;
  _etCloseEntry();
  _etCancelBlock();
  _etHideCtx();
  _etPage = null; _etItems = []; _etSel = null;
  _etRenderOverlays();
}

/** The page's bytes changed under the Pointer (Undo/Redo, a rotate, a reload):
 *  every run id it holds is POSITIONAL (`txt_12`, `ocr_90`) and may now name a
 *  different run, so an edit through a stale hit-area could rewrite the wrong
 *  words.  Drop the model — and any editor or box open over it — and reload.
 *  *page_id* null means "whatever page it holds". */
function _etInvalidate(page_id, reload = true) {
  if (page_id && _etPage !== page_id) return;
  _etCloseEntry();
  _etCancelBlock();
  _etHideCtx();
  _etPage = null; _etItems = []; _etSel = null;
  _etClearOverlays();
  if (!reload) { _etWant = null; _etPending = null; return; }
  if (_etActive()) _etLoadPage(_etWant || currentPageId);
}

function _etOverlayFor(page_id, make) {
  const block = document.getElementById('page-block-' + page_id);
  if (!block) return null;
  let ov = document.getElementById('et-ov-' + page_id);
  if (!ov && make) {
    ov = document.createElement('div');
    ov.className = 'page-edittext-overlay';
    ov.id = 'et-ov-' + page_id;
    block.appendChild(ov);
  }
  return ov;
}

function _etClearOverlays() {
  // Walk the blocks rather than trusting a stale id lookup: _buildPageBlockDOM
  // replaces a block's children wholesale, so an overlay can be detached and
  // still answer to getElementById until it is collected.
  _pageBlocks.forEach((st, _pid) => {
    const block = st && st.block;
    if (!block || !block.children) return;
    for (const c of [...block.children]) {
      // classList, NOT className: a page block also holds SVG overlays, and an
      // SVG element's className is an SVGAnimatedString with no string methods.
      if (c.classList && c.classList.contains('page-edittext-overlay')) {
        block.removeChild(c);
      }
    }
  });
}

function _etRenderOverlays() {
  _etClearOverlays();
  if (!_etActive() || !_etPage) return;
  const st = _pageBlocks.get(_etPage);
  if (!st?.rendered) return;
  const ov = _etOverlayFor(_etPage, true);
  if (!ov) return;
  ov.style.cssText = `width:${Math.round(_logicalW(st) * zoomLevel)}px;` +
                     `height:${Math.round(_logicalH(st) * zoomLevel)}px;`;
  // Start empty: the row/area box re-renders on every drag step, and a node
  // that was detached but still reachable must not keep yesterday's children.
  ov.innerHTML = '';
  const inBlock = _etBlock ? new Set(_etBlockItems().map(i => i.id)) : null;
  for (const it of _etItems) {
    const d = document.createElement('div');
    d.className = 'et-run' + (it.id === _etSel ? ' sel' : '') +
                  (inBlock && inBlock.has(it.id) ? ' inblk' : '');
    const r = _etBoxPx(st, it.x, it.y, it.x2, it.y2);
    d.style.cssText =
      `left:${r.left.toFixed(1)}px;top:${r.top.toFixed(1)}px;` +
      `width:${r.width.toFixed(1)}px;height:${r.height.toFixed(1)}px;`;
    d.title = it.text;
    d.addEventListener('mousedown', (e) => _etGrab(e, it));
    d.addEventListener('dblclick', (e) => { e.preventDefault(); _etOpenEntry(it); });
    d.addEventListener('contextmenu', (e) => _etShowCtx(e, it));
    ov.appendChild(d);
  }
  // The row/area box sits on top of the runs, AFTER them, so a run's index in
  // ov.children is still its index in _etItems (_etSelect relies on that).
  if (_etBlock) _etDrawBlock(ov, st);
}

// The page the Pointer should be on.  Normally the page in view (it follows the
// scroll), but clicking text on ANOTHER visible page moves it there — with
// several pages on screen only the "current" one had hit-areas, so text on
// the others looked editable and wasn't (the "sometimes it works after
// scrolling" report).  It is still one page at a time: loading every visible
// page would OCR every visible scan.
let _etWant = null;
let _etPending = null;   // {pid, x, y}: a click that asked for another page

async function _etLoadForCurrentPage() {
  return _etLoadPage(currentPageId);
}

async function _etLoadPage(pid) {
  if (!_etActive() || !SESSION_ID || !pid) return;
  _etWant = pid;
  if (_etLoading || _etPage === pid) return;
  const want = pid;
  _etLoading = true;
  _etSel = null;
  try {
    const res = await _vFetch('/api/viewer/page_elements', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID, page_id: want}),
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    // The user may have scrolled (or clicked) on while this was in flight.
    if (want !== _etWant || !_etActive()) return;
    _etPage  = want;
    _etItems = data.items || [];
    _etPageLeft = +data.page_left || 0;
    _etPageTop  = (data.page_top == null) ? null : +data.page_top;
    _etBlock = null;             // a box drawn over the old ids means nothing now
    _etRenderOverlays();
    _etSelect(null);             // and the panel must not keep naming a stale run
    _etServePendingClick(want);
  } catch (err) {
    showToast(err.message || String(err), 'error');
  } finally {
    _etLoading = false;
    // A page change that arrived mid-flight still needs serving.
    if (_etActive() && _etWant && _etPage !== _etWant) _etLoadPage(_etWant);
  }
}

function _etSelect(item) {
  _etSel = item ? item.id : null;
  // Re-style the EXISTING nodes; never rebuild the overlay here.  This runs on
  // MOUSEDOWN, and a rebuild replaces the very node the pointer is over, so the
  // browser sees the two halves of a double-click land on different elements
  // and never fires `dblclick` at all — "it goes yellow but I can't edit it".
  // The children are built from _etItems in order, which is what lets an index
  // stand in for a lookup (the drag preview relies on the same thing).
  const ov = document.getElementById('et-ov-' + _etPage);
  if (ov && ov.children) {
    for (let i = 0; i < ov.children.length; i++) {
      const c = ov.children[i], it = _etItems[i];
      if (!c.classList) continue;
      if (it && it.id === _etSel) c.classList.add('sel');
      else                       c.classList.remove('sel');
    }
  }
  const box = document.getElementById('at-et-selected');
  if (box) box.textContent = item ? item.text : '';
  const del = document.getElementById('at-et-delete');
  if (del) del.disabled = !item;
  const blk = document.getElementById('at-et-block');
  if (blk) blk.disabled = !item;
}

// ── move ────────────────────────────────────────────────────────────────────

let _etDrag = null;

function _etGrab(e, item) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  _etSelect(item);
  const st = _pageBlocks.get(_etPage);
  if (!st) return;
  _etDrag = { item, st, sx: e.clientX, sy: e.clientY, moved: false };
  document.addEventListener('mousemove', _etDragMove);
  document.addEventListener('mouseup',   _etDragEnd);
}

function _etDragMove(e) {
  const d = _etDrag;
  if (!d) return;
  const dx = e.clientX - d.sx, dy = e.clientY - d.sy;
  if (!d.moved && Math.abs(dx) < 3 && Math.abs(dy) < 3) return;
  d.moved = true;
  d.dx = dx; d.dy = dy;
  // Live feedback without a round trip: nudge the hit-area itself.  The
  // overlay is rebuilt from the server's answer on commit, so a transform is
  // enough and nothing has to be un-done if the drag is abandoned.
  const ov = document.getElementById('et-ov-' + _etPage);
  if (ov && ov.children && ov.children.length) {
    const idx = _etItems.findIndex(i => i.id === d.item.id);
    const el = ov.children[idx];
    if (el) el.style.transform = `translate(${dx}px, ${dy}px)`;
  }
}

function _etDragEnd() {
  document.removeEventListener('mousemove', _etDragMove);
  document.removeEventListener('mouseup',   _etDragEnd);
  const d = _etDrag; _etDrag = null;
  if (!d || !d.moved) return;          // a press is a selection, not a move
  const s = d.st.renderScale * zoomLevel, o = _etOff(d.st);
  // 'move' speaks the DISPLAY frame (the server adds the CropBox origin back).
  _etCommit(d.item.id, 'move',
            { x: d.item.x - o.x + d.dx / s, y: d.item.y - o.y - d.dy / s });
}

// ── inline text entry ───────────────────────────────────────────────────────

function _etCloseEntry() {
  if (_etEntry && _etEntry.wrap && _etEntry.wrap.parentNode) {
    _etEntry.wrap.parentNode.removeChild(_etEntry.wrap);
  }
  _etEntry = null;
  document.removeEventListener('mousedown', _etOutside, true);
}

function _etOutside(e) {
  if (!_etEntry) return;
  if (_etEntry.wrap.contains && _etEntry.wrap.contains(e.target)) return;
  _etCommitEntry();
}

/** True for a colour that would vanish on the editor's white background —
 *  white-on-band header text, for one.  OCR samples the real ink colour now,
 *  so the entry must not assume dark text: typing "GST" into a white header
 *  was invisible, white on white. */
function _etIsLight(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(hex || ''));
  if (!m) return false;
  const [r, g, b] = [m[1], m[2], m[3]].map(v => parseInt(v, 16) / 255);
  return 0.299 * r + 0.587 * g + 0.114 * b > 0.6;
}

/** Paint an entry field in the run's own colour on a background it can be
 *  read against. */
function _etEntryColours(el, color) {
  const fg = color || '#000000';
  el.style.color = fg;
  el.style.caretColor = fg;
  el.style.background = _etIsLight(fg) ? '#2b2f36' : '#ffffff';
}

function _etMeasure(text, font) {
  if (!_ptMeasureCtx) _ptMeasureCtx = document.createElement('canvas').getContext('2d');
  _ptMeasureCtx.font = font;
  const m = _ptMeasureCtx.measureText(String(text || ''));
  return (m && m.width) || 0;
}

/** The run's EM size in points.  `size` is the measured cap/ink height and is
 *  ~70% of the em, so sizing the field from it made the box too short for
 *  the line and pushed what was typed out of view. */
function _etEm(item) {
  return +item.font_size || (+item.size || 8) * 1.35;
}

function _etOpenEntry(item) {
  _etCloseEntry();
  _etHideCtx();
  const st = _pageBlocks.get(_etPage);
  const block = document.getElementById('page-block-' + _etPage);
  if (!st?.rendered || !block) return;
  const s = st.renderScale * zoomLevel;
  const r = _etBoxPx(st, item.x, item.y, item.x2, item.y2);
  const fontPx = Math.max(9, _etEm(item) * s);
  const font = _ptFontCss(item.rl_font || 'Helvetica', fontPx);
  const h = Math.ceil(fontPx * 1.3) + 4;
  // 3.5px = the 1.5px border + 2px padding, so the first letter typed sits
  // where the run's first letter is.
  const PAD = 3.5;

  const wrap = document.createElement('div');
  wrap.className = 'et-entry';
  wrap.style.cssText =
    `left:${(r.left - PAD).toFixed(1)}px;` +
    `top:${(r.top + r.height / 2 - h / 2).toFixed(1)}px;` +
    `height:${h}px;`;
  const el = document.createElement('input');
  el.type = 'text';
  el.className = 'et-entry-input';
  el.value = item.text;
  el.style.font = font;
  _etEntryColours(el, item.color);
  el.setAttribute('spellcheck', 'false');
  // Exactly as wide as the words (never less than the run), growing as you
  // type.  The field used to take the <input>'s default ~20-character width.
  const fit = () => {
    const w = Math.max(r.width, _etMeasure(el.value, font)) + PAD * 2 + fontPx * 0.6;
    wrap.style.width = w.toFixed(1) + 'px';
  };
  fit();
  el.addEventListener('input', fit);
  wrap.appendChild(el);
  block.appendChild(wrap);
  _etEntry = { el, wrap, item };

  el.onkeydown = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); _etCloseEntry(); return; }
    if (e.key === 'Enter')  { e.preventDefault(); _etCommitEntry(); }
  };
  if (el.focus) el.focus();
  if (el.select) el.select();
  document.addEventListener('mousedown', _etOutside, true);
}

function _etCommitEntry() {
  const E = _etEntry;
  if (!E) return;
  if (E.block) { _etCloseEntry(); return _etCommitBlock(E); }
  const next = E.el.value;
  const item = E.item;
  _etCloseEntry();
  // An unchanged editor commits nothing: re-injecting identical text would
  // still re-render the run in the app's font and cost an undo step for a
  // change the user never made.
  if (next === item.text) return;
  _etCommit(item.id, 'text', { text: next });
}

// ── committing ──────────────────────────────────────────────────────────────

function _etCommit(id, op, extra) {
  const page_id = _etPage;
  // Say which words we mean: ids are positional, and the server refuses an id
  // that no longer holds this text rather than edit whatever it names now.
  const it = _etItems.find(i => i.id === id);
  const body = Object.assign({ session_id: SESSION_ID, page_id, id, op },
                             it ? { expect: it.text } : {}, extra || {});
  return _etPost('/api/viewer/edit_text', page_id, body);
}

/** One page-content rewrite: queued like every other mutation, then the page,
 *  its thumbnail, Undo and the element model are all brought up to date. */
function _etPost(url, page_id, body) {
  return _queueAnnotOp(() => _vFetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    // PDF does not reflow, so a replacement longer than the run it replaces is
    // condensed to fit the gap before the next word.  Mild condensing needs no
    // comment; being made SMALLER does, or the only clue is that the words came
    // out a different size from their neighbours.  And when even that was not
    // enough, say so plainly rather than leave the user to spot the overlap.
    if (data.too_long_for_space)  showToast(window.t('toast.text_too_long_for_space'), 'warn');
    else if (data.shrunk_to_fit)  showToast(window.t('toast.text_shrunk_to_fit'));
    vRefreshHistory(data);
    _invalidateViewerPage(page_id);
    if (data.thumbnail) {
      const pg = allPages.find(p => p.id === page_id);
      if (pg) { pg.thumbnail = data.thumbnail; renderThumbList(); }
    }
    // The page was rewritten, so every element id from the last load is stale.
    _etPage = null;
    return _etLoadPage(page_id);
  })
  .catch(err => {
    showToast(err.message || String(err), 'error');
    _etPage = null;
    return _etLoadPage(page_id);
  }));
}

function editTextDeleteSelected() {
  if (!_etSel) return;
  _etCommit(_etSel, 'delete', {});
  _etSel = null;
}

function editTextRestyleSelected() {
  if (!_etSel) return;
  _etCommit(_etSel, 'style', {
    size:    parseFloat(_ctlVal('at-et-size', '')) || undefined,
    color:   _ctlVal('at-et-color', '') || undefined,
  });
}

function _etKey(e) {
  if (!_etActive() || _etEntry) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
            t.tagName === 'SELECT' || t.isContentEditable)) return;
  if (e.key === 'Escape') _etHideCtx();
  // While a row/area box is up, Enter and Escape belong to it.
  if (_etBlock) {
    if (e.key === 'Enter')  { e.preventDefault(); _etOpenBlockEntry(); }
    if (e.key === 'Escape') { e.preventDefault(); _etCancelBlock(); }
    return;
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && _etSel) {
    e.preventDefault(); editTextDeleteSelected(); return;
  }
  if (e.key === 'Enter' && _etSel) {
    const item = _etItems.find(i => i.id === _etSel);
    if (item) { e.preventDefault(); _etOpenEntry(item); }
  }
}

// ── right-click menu on a run ───────────────────────────────────────────────

let _etCtxItem = null;

function _etShowCtx(e, item) {
  e.preventDefault();
  e.stopPropagation();          // not the viewer's own tool menu
  if (_etEntry) _etCommitEntry();
  _etSelect(item);
  _etCtxItem = item;
  const menu = document.getElementById('et-ctx-menu');
  if (!menu) return;
  menu.style.display = 'block';
  const mw = menu.offsetWidth || 200, mh = menu.offsetHeight || 120;
  menu.style.left = Math.min(e.clientX, window.innerWidth  - mw - 8) + 'px';
  menu.style.top  = Math.min(e.clientY, window.innerHeight - mh - 8) + 'px';
  document.addEventListener('mousedown', _etCtxOutside, true);
}

function _etHideCtx() {
  const menu = document.getElementById('et-ctx-menu');
  if (menu) menu.style.display = 'none';
  document.removeEventListener('mousedown', _etCtxOutside, true);
}

function _etCtxOutside(e) {
  const menu = document.getElementById('et-ctx-menu');
  if (menu && menu.contains && menu.contains(e.target)) return;
  _etHideCtx();
}

function etCtxEdit()   { const it = _etCtxItem; _etHideCtx(); if (it) _etOpenEntry(it); }
function etCtxBlock()  { const it = _etCtxItem; _etHideCtx(); if (it) _etStartBlock(it); }
function etCtxDelete() { const it = _etCtxItem; _etHideCtx(); if (it) { _etSelect(it); editTextDeleteSelected(); } }

// ── editing a row, or an area spanning rows, as one block ───────────────────
//
// v2.24's editor had this as "Merge row" and the merge into the viewer lost
// it: select a word, ask for its row, and an adjustable box appears around the
// words on that line.  Drag the box or its handles to take words in or leave
// them out — across as many rows as you like — then edit the whole lot as one
// paragraph.  The server replaces those runs with ONE wrapped block of text
// (/api/viewer/edit_block), which is one Ctrl+Z.
//
// The box is kept in the ITEM frame (PDF y-up, the same frame as _etItems),
// because that is what the server's apply step takes the replaced words in.

let _etBlock = null;    // {x1, y1, x2, y2} while the box is up
let _etBlockDrag = null;

/** The x-contiguous run of words on *item*'s line: the seed for the box.
 *  A gap much wider than a word space is a jump to another column, so a label
 *  and a far right-aligned value on the same baseline stay apart. */
function _etRowRun(item) {
  const h = Math.max(1, item.y2 - item.y);
  const cy = (item.y + item.y2) / 2;
  const line = _etItems
    .filter(t => Math.abs((t.y + t.y2) / 2 - cy) <= h * 0.6)
    .sort((a, b) => a.x - b.x);
  const gap = h * 2.2;
  let i = line.findIndex(t => t.id === item.id);
  if (i < 0) return [item];
  let lo = i, hi = i;
  while (lo > 0 && line[lo].x - line[lo - 1].x2 <= gap) lo--;
  while (hi < line.length - 1 && line[hi + 1].x - line[hi].x2 <= gap) hi++;
  return line.slice(lo, hi + 1);
}

/** Runs whose centre is inside the box, in reading order. */
function _etBlockItems() {
  const b = _etBlock;
  if (!b) return [];
  const inside = _etItems.filter(t => {
    const cx = (t.x + t.x2) / 2, cy = (t.y + t.y2) / 2;
    return cx >= b.x1 && cx <= b.x2 && cy >= b.y1 && cy <= b.y2;
  });
  const hs = inside.map(t => Math.max(1, t.y2 - t.y)).sort((a, c) => a - c);
  const band = (hs[Math.floor(hs.length / 2)] || 8) * 0.6;
  return inside.sort((a, c) => {
    const dy = (c.y + c.y2) / 2 - (a.y + a.y2) / 2;     // higher on the page first
    return Math.abs(dy) > band ? dy : a.x - c.x;
  });
}

function _etBlockText(items) {
  return items.map(t => t.text).join(' ').replace(/\s+/g, ' ').trim();
}

function _etStartBlock(item) {
  _etCloseEntry();
  const seed = _etRowRun(item || _etItems.find(i => i.id === _etSel));
  if (!seed.length || !seed[0]) {
    showToast(window.t('toast.select_a_text_word_first'), 'error');
    return;
  }
  const pad = 2;
  _etBlock = {
    x1: Math.min(...seed.map(t => t.x))  - pad, y1: Math.min(...seed.map(t => t.y))  - pad,
    x2: Math.max(...seed.map(t => t.x2)) + pad, y2: Math.max(...seed.map(t => t.y2)) + pad,
  };
  _etRenderOverlays();
}

function editTextBlockSelected() {
  const it = _etItems.find(i => i.id === _etSel);
  if (it) _etStartBlock(it);
}

function _etCancelBlock() {
  _etBlockDrag = null;
  document.removeEventListener('mousemove', _etBlockDragMove);
  document.removeEventListener('mouseup',   _etBlockDragEnd);
  document.removeEventListener('mousedown', _etBlockOutside, true);
  if (!_etBlock) return;
  _etBlock = null;
  _etRenderOverlays();
}

function _etBlockOutside(e) {
  if (!_etBlock) return;
  const ov = document.getElementById('et-ov-' + _etPage);
  const el = ov && ov.querySelector ? ov.querySelector('.et-block') : null;
  const hint = ov && ov.querySelector ? ov.querySelector('.et-block-hint') : null;
  if ((el && el.contains && el.contains(e.target)) ||
      (hint && hint.contains && hint.contains(e.target))) return;
  if (_etEntry && _etEntry.wrap.contains && _etEntry.wrap.contains(e.target)) return;
  _etCancelBlock();
}

const _ET_HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];

function _etDrawBlock(ov, st) {
  const b = _etBlock;
  const r = _etBoxPx(st, b.x1, b.y1, b.x2, b.y2);
  const box = document.createElement('div');
  box.className = 'et-block';
  box.style.cssText = `left:${r.left.toFixed(1)}px;top:${r.top.toFixed(1)}px;` +
                      `width:${r.width.toFixed(1)}px;height:${r.height.toFixed(1)}px;`;
  box.addEventListener('mousedown', (e) => _etBlockGrab(e, null));
  box.addEventListener('dblclick', (e) => { e.preventDefault(); _etOpenBlockEntry(); });
  for (const k of _ET_HANDLES) {
    const hd = document.createElement('div');
    hd.className = 'et-bh et-bh-' + k;
    hd.addEventListener('mousedown', (e) => _etBlockGrab(e, k));
    box.appendChild(hd);
  }
  ov.appendChild(box);

  // Say what will be edited, and how — above the box, or below it near the top.
  const items = _etBlockItems();
  const hint = document.createElement('div');
  hint.className = 'et-block-hint';
  const pv = document.createElement('div');
  pv.className = 'et-block-preview';
  pv.textContent = _etBlockText(items) || window.t('toast.no_text_inside_the_box');
  const how = document.createElement('div');
  how.className = 'et-block-how';
  how.textContent = window.t('et_block.hint');
  hint.appendChild(pv);
  hint.appendChild(how);
  hint.style.cssText = `left:${r.left.toFixed(1)}px;` +
    (r.top > 60 ? `bottom:${(ov.offsetHeight || Math.round(_logicalH(st) * zoomLevel)) - r.top + 6}px;`
                : `top:${(r.top + r.height + 6).toFixed(1)}px;`);
  ov.appendChild(hint);

  document.removeEventListener('mousedown', _etBlockOutside, true);
  document.addEventListener('mousedown', _etBlockOutside, true);
}

function _etBlockGrab(e, handle) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  _etBlockDrag = { handle, sx: e.clientX, sy: e.clientY, orig: Object.assign({}, _etBlock) };
  document.addEventListener('mousemove', _etBlockDragMove);
  document.addEventListener('mouseup',   _etBlockDragEnd);
}

function _etBlockDragMove(e) {
  const d = _etBlockDrag;
  const st = _pageBlocks.get(_etPage);
  if (!d || !st) return;
  const s = st.renderScale * zoomLevel;
  const dx = (e.clientX - d.sx) / s, dy = -(e.clientY - d.sy) / s;   // PDF y is up
  const o = d.orig;
  let { x1, y1, x2, y2 } = o;
  if (!d.handle) { x1 += dx; x2 += dx; y1 += dy; y2 += dy; }
  else {
    if (d.handle.includes('w')) x1 = o.x1 + dx;
    if (d.handle.includes('e')) x2 = o.x2 + dx;
    if (d.handle.includes('n')) y2 = o.y2 + dy;           // north = top = larger y
    if (d.handle.includes('s')) y1 = o.y1 + dy;
  }
  _etBlock = { x1: Math.min(x1, x2), x2: Math.max(x1, x2),
               y1: Math.min(y1, y2), y2: Math.max(y1, y2) };
  _etRenderOverlays();
}

function _etBlockDragEnd() {
  _etBlockDrag = null;
  document.removeEventListener('mousemove', _etBlockDragMove);
  document.removeEventListener('mouseup',   _etBlockDragEnd);
}

/** *items* split into rows (top of the page first), each left to right. */
function _etRows(items) {
  const hs = items.map(t => Math.max(1, t.y2 - t.y)).sort((a, b) => a - b);
  const band = (hs[Math.floor(hs.length / 2)] || 8) * 0.6;
  const rows = [];
  for (const t of [...items].sort((a, b) => b.y2 - a.y2)) {
    const row = rows.find(r => Math.abs(r[0].y2 - t.y2) <= band);
    if (row) row.push(t); else rows.push([t]);
  }
  return rows.map(r => r.sort((a, b) => a.x - b.x));
}

/** Median distance between the tops of successive rows, in points, or null
 *  for a single row — the same measure the server uses for the block. */
function _etRowPitch(items) {
  const hs = items.map(t => Math.max(1, t.y2 - t.y)).sort((a, b) => a - b);
  const band = (hs[Math.floor(hs.length / 2)] || 8) * 0.6;
  const tops = items.map(t => t.y2).sort((a, b) => b - a);
  const rows = [];
  for (const y of tops) {
    if (rows.length && rows[rows.length - 1][rows[rows.length - 1].length - 1] - y <= band) {
      rows[rows.length - 1].push(y);
    } else rows.push([y]);
  }
  if (rows.length < 2) return null;
  const means = rows.map(r => r.reduce((a, b) => a + b, 0) / r.length);
  const gaps = means.slice(1).map((m, i) => means[i] - m).sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)];
}

/** How the captured rows were aligned: {align, x} with x the shared right
 *  edge or centre (item frame), or {align:'left', x:null}.  The same rule the
 *  server applies (viewer.py _block_alignment) — this copy only drives the
 *  preview, the server decides what is written. */
function _etAlign(items, em) {
  const rows = _etRows(items);
  if (rows.length < 2) return { align: 'left', x: null };
  const lefts   = rows.map(r => Math.min(...r.map(t => t.x)));
  const rights  = rows.map(r => Math.max(...r.map(t => t.x2)));
  const centres = lefts.map((l, i) => (l + rights[i]) / 2);
  const spread = v => Math.max(...v) - Math.min(...v);
  const cands = [
    { raw: spread(lefts),   bias: 0,   align: 'left',   x: null },
    { raw: spread(rights),  bias: 0.5, align: 'right',  x: Math.max(...rights) },
    { raw: spread(centres), bias: 0.5, align: 'center',
      x: centres.reduce((a, b) => a + b, 0) / centres.length },
  ].sort((a, b) => (a.raw + a.bias) - (b.raw + b.bias));
  return cands[0].raw > Math.max(2, em * 0.3) ? { align: 'left', x: null } : cands[0];
}

/** Scale *em* so the captured rows, re-set in *rl*, measure what they did on
 *  the page (mirrors the server's calibration; clamped to 0.85–1.05). */
function _etCalibratedEm(items, rl, em) {
  const ratios = [];
  for (const row of _etRows(items)) {
    const natural = _etMeasure(_etBlockText(row), _ptFontCss(rl, em));
    const span = Math.max(...row.map(t => t.x2)) - Math.min(...row.map(t => t.x));
    if (natural > 1 && span > 1) ratios.push(span / natural);
  }
  if (!ratios.length) return em;
  ratios.sort((a, b) => a - b);
  return em * Math.max(0.85, Math.min(1.05, ratios[Math.floor(ratios.length / 2)]));
}

function _etMode(items, key, dflt) {
  const c = {};
  for (const t of items) { const v = t[key] || dflt; c[v] = (c[v] || 0) + 1; }
  return Object.keys(c).sort((a, b) => c[b] - c[a])[0] || dflt;
}

/** Open the paragraph editor over the box: the captured words as one text,
 *  in their own font, size and colour, wrapping where the result will. */
function _etOpenBlockEntry() {
  const items = _etBlockItems();
  if (!items.length) {
    showToast(window.t('toast.no_text_inside_the_box'), 'error');
    return;
  }
  const st = _pageBlocks.get(_etPage);
  const block = document.getElementById('page-block-' + _etPage);
  if (!st?.rendered || !block) return;
  _etCloseEntry();
  const s = st.renderScale * zoomLevel;
  const b = Object.assign({}, _etBlock);
  const ems = items.map(_etEm).sort((a, c) => a - c);
  const rl = _etMode(items, 'rl_font', 'Helvetica');
  const em = _etCalibratedEm(items, rl, ems[Math.floor(ems.length / 2)]);
  const al = _etAlign(items, em);
  const color = _etMode(items, 'color', '#000000');
  const pitch = Math.max(em * 0.8, Math.min(em * 2.5, _etRowPitch(items) || em * 1.35));
  const multiRow = _etRowPitch(items) != null;
  // A single row grows to the right as it is edited, up to the page margin, and
  // only then wraps; several rows stay a paragraph.  Its width is the box — but
  // never narrower than the widest ORIGINAL row set in our font: the box hugs
  // the scanned glyphs, and re-set at the estimated em a row can come out a
  // hair wider than its own footprint, which wrapped a two-line header cell
  // ("Unit Price / Excl. VAT") into three before a single letter was changed.
  const o = _etOff(st);
  const pageRight = o.x + st.pageW - 4;
  const emFont = _ptFontCss(rl, em);
  const widestRow = Math.max(0, ..._etRows(items).map(
    row => _etMeasure(_etBlockText(row), emFont)));
  const wrapW = multiRow ? Math.max(b.x2 - b.x1, widestRow + 1)
                         : Math.max(b.x2 - b.x1, pageRight - b.x1);
  const fontPx = Math.max(9, em * s);
  const font = _ptFontCss(rl, fontPx);
  const r = _etBoxPx(st, b.x1, b.y1, b.x2, b.y2);
  const PAD = 3.5;

  const wrap = document.createElement('div');
  wrap.className = 'et-entry et-entry-block';
  const el = document.createElement('textarea');
  el.className = 'et-entry-input';
  el.value = _etBlockText(items);
  el.style.font = font;
  el.style.lineHeight = (pitch * s).toFixed(1) + 'px';
  el.style.textAlign = al.align;
  _etEntryColours(el, color);
  el.setAttribute('spellcheck', 'false');
  const fit = () => {
    const widest = Math.max(...String(el.value).split('\n').map(l => _etMeasure(l, font)));
    // The text area wraps where the committed block will: at wrapW.
    const wPx = multiRow ? wrapW * s
                         : Math.min(wrapW * s, Math.max(r.width, widest + fontPx * 0.6));
    const nLines = _ptWrap(el.value, _ptFontCss(rl, em), wrapW).length;
    // Hang the field off the rows' shared edge, so the preview lines up
    // where the committed block will.
    let left = r.left;
    if (al.x != null) {
      const ax = _etBoxPx(st, al.x, b.y1, al.x, b.y2).left;
      left = al.align === 'right' ? ax - wPx : ax - wPx / 2;
    }
    wrap.style.cssText =
      `left:${(left - PAD).toFixed(1)}px;top:${(r.top - PAD).toFixed(1)}px;` +
      `width:${(wPx + PAD * 2).toFixed(1)}px;` +
      `height:${(Math.max(1, nLines) * pitch * s + PAD * 2 + 4).toFixed(1)}px;`;
  };
  fit();
  el.addEventListener('input', fit);
  wrap.appendChild(el);
  block.appendChild(wrap);
  const original = el.value;
  _etEntry = { el, wrap, item: null, block: { items, b, rl, em, wrapW, original } };

  el.onkeydown = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); _etCloseEntry(); _etCancelBlock(); return; }
    // Enter finishes, like the one-word editor; Shift+Enter starts a new line.
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _etCommitEntry(); }
  };
  if (el.focus) el.focus();
  if (el.select) el.select();
  document.addEventListener('mousedown', _etOutside, true);
}

function _etCommitBlock(E) {
  const { items, b, rl, em, wrapW, original } = E.block;
  const text = String(E.el.value || '').replace(/[ \t]+/g, ' ').trim();
  _etCancelBlock();
  if (text === original) return;             // an unchanged editor commits nothing
  // Wrapped HERE, with the face the server embeds, for the same reason as
  // Add text's box: only the client can measure what it previews.
  const lines = text ? _ptWrap(text, _ptFontCss(rl, em), wrapW) : [];
  const widest = Math.max(0, ...lines.map(l => _etMeasure(l, _ptFontCss(rl, em))));
  const box = { x1: b.x1, y1: b.y1, y2: b.y2, x2: Math.max(b.x2, b.x1 + widest + 2) };
  _etSel = null;
  return _etPost('/api/viewer/edit_block', _etPage, {
    session_id: SESSION_ID, page_id: _etPage,
    ids: items.map(t => t.id), expect: items.map(t => t.text), text, lines, box,
  });
}

// ── Add text: stamp real text into the page ─────────────────────────────────
//
// NOT the annotation Text tool.  That one lays a FreeText object on top of the
// page which can be selected and deleted afterwards; this writes into the page
// content, where it is indistinguishable from the document's own text.  The
// panel groups them apart and names them differently for exactly that reason.
//
// One tool (Add text), two shapes, one endpoint:
//   click — type one line, Enter to place.
//   drag  — a box: type, Ctrl+Enter to place; the text is wrapped to the box
//           HERE, because only the client can measure the font it is
//           previewing with.
//
// Clicking away commits; only Escape discards.  That is the rule every other
// text entry in this app follows (CLAUDE.md, "Editor text entry"), and it is
// how people expect to finish an edit.

let _ptEntry = null;   // {kind, page_id, st, el, wrap, geom, done}

function _ptParams() {
  return {
    size:    parseFloat(_ctlVal('at-pt-size', '12')) || 12,
    color:   _ctlVal('at-pt-color', '#000000') || '#000000',
    rl_font: _ctlVal('at-pt-font', 'Helvetica') || 'Helvetica',
  };
}

/** Measure-only canvas: the viewer must not borrow the editor's, which only
 *  exists once the editor panel has been opened. */
let _ptMeasureCtx = null;
function _ptWrap(text, fontStr, maxPx) {
  if (!_ptMeasureCtx) {
    _ptMeasureCtx = document.createElement('canvas').getContext('2d');
  }
  const ctx2 = _ptMeasureCtx;
  ctx2.font = fontStr;
  const out = [];
  for (const para of String(text || '').split('\n')) {
    let line = '';
    for (const word of para.split(' ')) {
      const test = line ? line + ' ' + word : word;
      if (ctx2.measureText(test).width > maxPx && line) { out.push(line); line = word; }
      else line = test;
    }
    out.push(line);
  }
  return out;
}

function _ptFontCss(rl, sizePx) {
  const l = String(rl || '').toLowerCase();
  const weight = l.includes('bold') ? 'bold' : 'normal';
  const style  = (l.includes('italic') || l.includes('oblique')) ? 'italic' : 'normal';
  // The bundled faces first — the SAME fonts the server embeds (viewer.css
  // declares them), so what is measured and previewed here is what lands.
  // The editor's cssFontFor did this; the viewer's copy fell back to Helvetica
  // for Calibri, which is noticeably wider and wrapped text boxes early.
  const family = l.startsWith('calibri')    ? "'PWCalibri',sans-serif"
               : l.startsWith('arial')      ? "'PWArial',Arial,sans-serif"
               : l.startsWith('timesnr')    ? "'PWTimes','Times New Roman',serif"
               : l.startsWith('couriernew') ? "'PWCourier','Courier New',monospace"
               : l.includes('times') || l.includes('roman') ? 'Times New Roman,serif'
               : l.includes('courier') ? 'Courier New,monospace'
               : 'Helvetica,Arial,sans-serif';
  return `${style} ${weight} ${sizePx}px ${family}`;
}

function _ptCancelEntry() {
  if (_ptEntry && _ptEntry.wrap && _ptEntry.wrap.parentNode) {
    _ptEntry.wrap.parentNode.removeChild(_ptEntry.wrap);
  }
  _ptEntry = null;
  document.removeEventListener('mousedown', _ptOutside, true);
}

function _ptOutside(e) {
  if (!_ptEntry) return;
  if (_ptEntry.wrap.contains && _ptEntry.wrap.contains(e.target)) return;
  // The style controls are outside the overlay — adjusting them mid-edit must
  // not count as clicking away.
  const panel = document.getElementById('at-pagetext-props');
  if (panel && panel.contains && panel.contains(e.target)) return;
  _ptCommit();
}

function _ptBeginEntry(kind, page_id, st, block, cssRect, geom) {
  _ptCancelEntry();
  const p = _ptParams();
  const sizePx = p.size * st.renderScale * zoomLevel;

  const wrap = document.createElement('div');
  wrap.className = 'pt-entry';
  wrap.style.cssText =
    `left:${cssRect.x}px;top:${cssRect.y}px;` +
    `width:${cssRect.w}px;height:${cssRect.h}px;`;

  const el = document.createElement(kind === 'box' ? 'textarea' : 'input');
  if (kind !== 'box') el.type = 'text';
  el.className = 'pt-entry-input';
  el.style.font      = _ptFontCss(p.rl_font, sizePx);
  el.style.fontSize  = sizePx + 'px';
  el.style.color     = p.color;
  el.style.lineHeight = kind === 'box' ? '1.35' : '';
  el.setAttribute('spellcheck', 'false');
  wrap.appendChild(el);
  block.appendChild(wrap);

  _ptEntry = { kind, page_id, st, el, wrap, geom, done: false };

  el.onkeydown = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); _ptCancelEntry(); return; }
    if (e.key === 'Enter') {
      if (kind === 'box' ? (e.ctrlKey || e.metaKey) : true) {
        e.preventDefault();
        _ptCommit();
      }
    }
  };
  if (el.focus) el.focus();
  document.addEventListener('mousedown', _ptOutside, true);
}

/** Add text, clicked: a one-line entry whose baseline is where you clicked. */
function _ptOpenLine(page_id, st, block, xPx, yPx) {
  const s = st.renderScale * zoomLevel;
  const p = _ptParams();
  const sizePx = p.size * s;
  const boxH = Math.round(sizePx * 1.6 + 8);
  const boxW = 320;
  const top  = Math.max(0, Math.round(yPx - boxH / 2));
  // Match the editor's baseline correction: the input centres text vertically,
  // but the PDF draws from the alphabetic baseline, ~20% of the size lower.
  const baselinePx = top + Math.round(boxH / 2) + Math.round(sizePx * 0.2);
  _ptBeginEntry('line', page_id, st, block,
                { x: xPx, y: top, w: boxW, h: boxH },
                { x: xPx / s, y: st.pageH - baselinePx / s });
}

/** Add text, dragged: the drag gives the box; x,y is its TOP-LEFT in page points. */
function _ptOpenBox(page_id, rect) {
  const st = _pageBlocks.get(page_id);
  const block = document.getElementById('page-block-' + page_id);
  if (!st?.rendered || !block) return;
  const s = st.renderScale * zoomLevel;
  _ptBeginEntry('box', page_id, st, block,
                { x: rect.x1 * s, y: (st.pageH - rect.y2) * s,
                  w: (rect.x2 - rect.x1) * s, h: (rect.y2 - rect.y1) * s },
                { x: rect.x1, y: rect.y2,
                  boxW: rect.x2 - rect.x1, boxH: rect.y2 - rect.y1 });
}

function _ptCommit() {
  const E = _ptEntry;
  if (!E || E.done) return;
  E.done = true;
  const text = (E.el.value || '').trim();
  const p = _ptParams();
  const { page_id, geom } = E;
  _ptCancelEntry();
  if (!text) return;               // nothing typed — not an edit

  const item = { text, size: p.size, color: p.color, rl_font: p.rl_font,
                 x: geom.x, y: geom.y };
  if (E.kind === 'box') {
    const s = E.st.renderScale * zoomLevel;
    item.multiline = true;
    item.boxW = geom.boxW;
    item.boxH = geom.boxH;
    // Wrap to the box in the same units the backend will lay the lines out in.
    item.lines = _ptWrap(text, _ptFontCss(p.rl_font, p.size * s),
                         geom.boxW * s - 6);
  }
  return _ptSend(page_id, item);
}

function _ptSend(page_id, item) {
  return _queueAnnotOp(() => _vFetch('/api/viewer/add_text', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: SESSION_ID, page_id, item}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    vRefreshHistory(data);
    _invalidateViewerPage(page_id);
    if (data.thumbnail) {
      const pg = allPages.find(p2 => p2.id === page_id);
      if (pg) { pg.thumbnail = data.thumbnail; renderThumbList(); }
    }
    showToast(window.t('toast.text_placed'), 'success');
  })
  .catch(err => showToast(err.message || String(err), 'error')));
}

// ── Crop: set the page's CropBox ────────────────────────────────────────────
//
// The only page tool that changes the page's SIZE, which is the whole of its
// extra complexity.  Everything that describes a page's dimensions has to be
// updated together — the page-list metadata the opening zoom is fitted to, and
// the per-block state the overlays are positioned against — or the page is laid
// out at the size it used to be.
//
// The block itself needs no help: it has no explicit height (buildPageColumn
// sizes a placeholder, _buildPageBlockDOM then replaces the block's contents
// wholesale), so it reflows to whatever the new canvas measures.

function _applyPageSize(page_id, w, h) {
  if (!w || !h) return;
  const pg = allPages.find(p => p.id === page_id);
  if (pg) { pg.width_pts = w; pg.height_pts = h; }
  const st = _pageBlocks.get(page_id);
  if (st) { st.pageW = w; st.pageH = h; }
  if (page_id === currentPageId) { pageW = w; pageH = h; }
  // A page that shrank may no longer be the widest in the document, and one
  // that grew may now exceed the zoom the document was fitted to.
  if (typeof _docWidestWidthPts === 'function') {
    _fitWidestPts = Math.max(w, _docWidestWidthPts());
  }
}

function _cropApply(page_id, rect) {
  return _cropRequest({ session_id: SESSION_ID, page_id,
                        rect: { x: rect.x1, y: rect.y1,
                                x2: rect.x2, y2: rect.y2 } },
                      window.t('toast.crop_applied'));
}

function clearViewerCrop() {
  if (!SESSION_ID || !currentPageId) return;
  return _cropRequest({ session_id: SESSION_ID, page_id: currentPageId,
                        clear: true },
                      window.t('toast.crop_cleared'));
}

function _cropRequest(body, okMsg) {
  const page_id = body.page_id;
  return _queueAnnotOp(() => _vFetch('/api/viewer/crop', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    vRefreshHistory(data);
    _applyPageSize(page_id, data.width_pts, data.height_pts);
    _invalidateViewerPage(page_id);
    if (data.thumbnail) {
      const pg = allPages.find(p => p.id === page_id);
      if (pg) { pg.thumbnail = data.thumbnail; renderThumbList(); }
    }
    showToast(okMsg, 'success');
  })
  .catch(err => showToast(err.message || String(err), 'error')));
}

// ── Redact: burn a box into the page ────────────────────────────────────────
//
// Destructive on the server (the text is scrubbed and the pixels under the box
// are destroyed inside any image XObject), so it is NOT a draft and there is
// nothing to select afterwards.  It is still one Ctrl+Z, because the viewer's
// undo stack snapshots page bytes.
//
// The page's render is invalidated rather than patched: the redaction changed
// the page content, so the cached raster is stale.
function _rxApply(page_id, rect) {
  return _queueAnnotOp(() => _vFetch('/api/viewer/redact', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      session_id: SESSION_ID, page_id,
      rect: { x: rect.x1, y: rect.y1, x2: rect.x2, y2: rect.y2 },
    }),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    vRefreshHistory(data);
    _invalidateViewerPage(page_id);
    if (data.thumbnail) {
      const pg = allPages.find(p => p.id === page_id);
      if (pg) { pg.thumbnail = data.thumbnail; renderThumbList(); }
    }
    showToast(window.t('toast.redaction_applied'), 'success');
  })
  .catch(err => showToast(err.message || String(err), 'error')));
}

function _ffDelete(page_id, field_id) {
  return _queueAnnotOp(() => _vFetch('/api/viewer/delete_form_field', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: SESSION_ID, page_id, field_id}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) throw apiErr(data);
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
    _allFormFields[page_id] = (_allFormFields[page_id] || [])
      .filter(f => f.id !== field_id);
    const st = _pageBlocks.get(page_id);
    if (st?.rendered) _populateFormOverlay(page_id, st);
  })
  .catch(err => showToast(err.message || String(err), 'error')));
}

async function loadFormFields() {
  if (!SESSION_ID) return;
  try {
    const res  = await _vFetch('/api/viewer/form_fields', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID})
    });
    const data = await res.json();
    _allFormFields = data.fields_by_page || {};
  } catch(_) {
    _allFormFields = {};
  }
  // Draw overlays on any already-rendered page blocks
  _pageBlocks.forEach((st, pid) => { if (st.rendered) _populateFormOverlay(pid, st); });
}

// ── Document links (clickable URLs) ─────────────────────────────────────────
// A /Link annotation with a URI action becomes a transparent hit-area over the
// page image.  Hovering one shows the pointer cursor, tints the box and puts the
// destination in a tooltip; clicking hands it to the OS default browser.
//
// The click sends an INDEX, never the URL — /api/viewer/open_link re-reads the
// destination from the document, so what opens is always what the PDF actually
// says (see its docstring).  Everything here is display-only.

let _allLinks   = {};     // page_id → [{idx, x, y, w, h, url}]
let _linkDownAt = null;   // pointer position at mousedown, to tell click from drag

// How far the pointer may travel between press and release and still count as a
// click.  Without this, starting a pan (hand tool) or a selection marquee on top
// of a link would open it on release.
const _LINK_DRAG_SLOP = 5;

async function loadAllLinks() {
  if (!SESSION_ID) return;
  try {
    const res  = await _vFetch('/api/viewer/links', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID})
    });
    const data = await res.json();
    _allLinks = data.links_by_page || {};
  } catch(_) {
    _allLinks = {};
  }
  _pageBlocks.forEach((st, pid) => { if (st.rendered) _populateLinkOverlay(pid, st); });
}

// Links are live whenever nothing else wants the click: an armed drawing tool or
// a live draft owns the page, and the hit-areas would eat the first drag.  They
// stay live under the HAND tool — panning is a drag, and the slop check above
// keeps the two apart — because following a link is reading, not editing.
function _linksActive() {
  return !_activeAnnotTool && !_draft;
}

function _refreshLinkOverlays() {
  _pageBlocks.forEach((st, pid) => { if (st.rendered) _populateLinkOverlay(pid, st); });
}

function _populateLinkOverlay(page_id, st) {
  const ov = document.getElementById('link-ov-' + page_id);
  if (!ov) return;
  ov.innerHTML = '';
  const links = _allLinks[page_id];
  if (!links || !links.length || !st.rendered) return;

  const active = _linksActive();
  const scale  = st.renderScale * zoomLevel;
  links.forEach(link => {
    const el = document.createElement('div');
    el.className = 'pdf-link-hit';
    // Same frame as every other overlay: PDF points, y up from the bottom, so
    // the top edge is measured down from the page top.
    el.style.left   = (link.x * scale).toFixed(1) + 'px';
    el.style.top    = ((st.pageH - (link.y + link.h)) * scale).toFixed(1) + 'px';
    el.style.width  = (link.w * scale).toFixed(1) + 'px';
    el.style.height = (link.h * scale).toFixed(1) + 'px';
    el.style.pointerEvents = active ? 'all' : 'none';
    // The tooltip is the whole point of the hover state: it answers "where does
    // this go?" before the click, which is the one question a link overlay on
    // someone else's document has to be able to answer.
    el.title = link.url;
    el.setAttribute('role', 'link');
    el.setAttribute('aria-label', link.url);
    el.addEventListener('mousedown', (e) => {
      _linkDownAt = { x: e.clientX, y: e.clientY };
      // Deliberately NOT stopped: the hand tool's pan and the pointer tool's
      // marquee both start on mousedown, and a press that turns into a drag
      // must still reach them.
    });
    el.addEventListener('click', (e) => {
      if (!_linksActive()) return;
      const d = _linkDownAt;
      _linkDownAt = null;
      if (d && (Math.abs(e.clientX - d.x) > _LINK_DRAG_SLOP ||
                Math.abs(e.clientY - d.y) > _LINK_DRAG_SLOP)) return;   // that was a drag
      e.preventDefault(); e.stopPropagation();
      openDocumentLink(page_id, link.idx);
    });
    ov.appendChild(el);
  });
}

async function openDocumentLink(page_id, idx) {
  try {
    const res  = await _vFetch('/api/viewer/open_link', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID, page_id, idx})
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    showToast(window.t('toast.link_opened') + data.url, 'success');
  } catch(e) {
    if (e.name === 'AbortError') return;      // tab switched mid-click
    showToast(window.t('toast.link_open_failed') + e.message, 'error');
  }
}

function _formFieldDisplayRect(field, renderScale) {
  const s = renderScale * zoomLevel;
  return {
    x: field.x1 * s,
    y: (field.page_h - field.y2) * s,
    w: (field.x2 - field.x1) * s,
    h: (field.y2 - field.y1) * s,
  };
}

function _populateFormOverlay(page_id, st) {
  const ov = document.getElementById('form-ov-' + page_id);
  if (!ov) return;
  ov.innerHTML = '';
  const fields = _allFormFields[page_id];
  if (!fields || !fields.length) return;

  const editing = _ffEditing();
  ov.classList.toggle('ff-edit-mode', editing);
  fields.forEach(field => {
    const r   = _formFieldDisplayRect(field, st.renderScale);
    const el  = _makeFormElement(field, r);
    if (el) ov.appendChild(el);
    // While the Field tool is armed the widgets are not fillable — a
    // transparent hit-area on top is what you grab to move or resize.
    if (editing) _ffDecorate(ov, field, r, page_id);
  });
}

function _makeFormElement(field, r) {
  const ftype     = field.type;
  const baseFontSz = Math.max(8, Math.min(r.h * 0.62, 20));
  const baseStyle  =
    `left:${r.x.toFixed(1)}px;top:${r.y.toFixed(1)}px;` +
    `width:${r.w.toFixed(1)}px;height:${r.h.toFixed(1)}px;` +
    `font-size:${baseFontSz.toFixed(0)}px;`;

  let el;

  if (ftype === 'text') {
    el = document.createElement('input');
    el.type = 'text';
    el.className = 'pdf-form-field';
    el.style.cssText = baseStyle;
    el.value = field.value || '';

  } else if (ftype === 'multiline') {
    el = document.createElement('textarea');
    el.className = 'pdf-form-field';
    el.style.cssText = baseStyle + 'resize:none;overflow:auto;padding:2px 3px;';
    el.value = field.value || '';

  } else if (ftype === 'checkbox') {
    const sz  = Math.min(r.w, r.h) * 0.85;
    const off = { x: r.x + (r.w - sz) / 2, y: r.y + (r.h - sz) / 2 };
    el = document.createElement('input');
    el.type = 'checkbox';
    el.className = 'pdf-form-field';
    el.style.cssText =
      `left:${off.x.toFixed(1)}px;top:${off.y.toFixed(1)}px;` +
      `width:${sz.toFixed(1)}px;height:${sz.toFixed(1)}px;` +
      'accent-color:var(--accent2);cursor:pointer;';
    el.checked = field.value === 'on' || field.value === 'Yes';

  } else if (ftype === 'radio') {
    const sz  = Math.min(r.w, r.h) * 0.85;
    const off = { x: r.x + (r.w - sz) / 2, y: r.y + (r.h - sz) / 2 };
    el = document.createElement('input');
    el.type = 'radio';
    el.name = 'ff_radio_' + field.name;
    el.className = 'pdf-form-field';
    el.style.cssText =
      `left:${off.x.toFixed(1)}px;top:${off.y.toFixed(1)}px;` +
      `width:${sz.toFixed(1)}px;height:${sz.toFixed(1)}px;` +
      'accent-color:var(--accent2);cursor:pointer;';
    el.checked = field.value === 'on';

  } else if (ftype === 'dropdown') {
    el = document.createElement('select');
    el.className = 'pdf-form-select';
    el.style.cssText = baseStyle;
    const blank = document.createElement('option');
    blank.value = ''; blank.textContent = '';
    el.appendChild(blank);
    (field.options || []).forEach(opt => {
      const o = document.createElement('option');
      o.value = opt; o.textContent = opt;
      if (opt === field.value) o.selected = true;
      el.appendChild(o);
    });

  } else if (ftype === 'listbox') {
    el = document.createElement('select');
    el.multiple = true;
    el.className = 'pdf-form-select';
    el.style.cssText = baseStyle + 'overflow-y:auto;';
    (field.options || []).forEach(opt => {
      const o = document.createElement('option');
      o.value = opt; o.textContent = opt;
      if (opt === field.value) o.selected = true;
      el.appendChild(o);
    });

  } else {
    return null;
  }

  // A restricted document forbids filling its fields as much as any other edit.
  if (field.readonly || IS_RESTRICTED) { el.disabled = true; }
  el.title        = field.label || field.name || '';
  el.dataset.ffId = field.id;

  el.addEventListener('change',  _onFFChange);
  if (ftype === 'text' || ftype === 'multiline')
    el.addEventListener('input', _onFFChange);

  return el;
}

function _onFFChange(e) {
  const el  = e.currentTarget;
  const fid = el.dataset.ffId;
  const val = el.type === 'checkbox' || el.type === 'radio'
    ? (el.checked ? 'on' : 'off')
    : el.value;
  _ffPending[fid] = val;
  // Update local cache so the value persists across zoom redraws
  for (const fields of Object.values(_allFormFields)) {
    const f = fields.find(f => f.id === fid);
    if (f) { f.value = val; break; }
  }
  clearTimeout(_ffSaveTimer);
  _ffSaveTimer = setTimeout(_flushFF, 800);
}

async function _flushFF() {
  if (!Object.keys(_ffPending).length) return;
  const values = Object.assign({}, _ffPending);
  Object.keys(values).forEach(k => delete _ffPending[k]);
  try {
    await apiFetch('/api/viewer/fill_form', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: SESSION_ID, values})
    });
    // Filled values live in the session until save writes them into the PDF —
    // the same "committed but not on disk" state an applied edit is in.
    if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
  } catch(_) {}
}

function _clearFormOverlays() {
  _allFormFields = {};
  document.querySelectorAll('.page-form-overlay').forEach(ov => ov.remove());
}

// ── Fullscreen ────────────────────────────────────────────────────────────────
let _fsHintTimer = null;

function enterFullscreen() {
  _hideCtxMenu();
  document.getElementById('panel-viewer').classList.add('fullscreen-mode');
  _showFsHint();
}

function exitFullscreen() {
  document.getElementById('panel-viewer').classList.remove('fullscreen-mode');
  _hideFsHint();
}

function _showFsHint() {
  const hint = document.getElementById('fullscreen-esc-hint');
  if (!hint) return;
  clearTimeout(_fsHintTimer);
  hint.style.opacity = '1';
  hint.style.display = 'block';
  // After 2 s visible, fade out over 0.5 s (matches the CSS transition duration)
  _fsHintTimer = setTimeout(() => {
    hint.style.opacity = '0';
    setTimeout(() => { hint.style.display = 'none'; }, 500);
  }, 2000);
}

function _hideFsHint() {
  const hint = document.getElementById('fullscreen-esc-hint');
  if (!hint) return;
  clearTimeout(_fsHintTimer);
  hint.style.opacity = '0';
  hint.style.display = 'none';
}

// ── Wire up canvas-area events ────────────────────────────────────────────────
(function _initViewerTools() {
  const area = document.getElementById('canvas-area');
  if (!area) return;
  area.addEventListener('contextmenu', _showCtxMenu);
  area.addEventListener('mousedown',   _onViewerMouseDown);
  document.addEventListener('mousemove', _onViewerMouseMove);
  document.addEventListener('mouseup',   _onViewerMouseUp);
  // Passive cursor tracker — paste (Ctrl+V) targets the page under the cursor.
  document.addEventListener('mousemove', (e) => { _lastMouse = { x: e.clientX, y: e.clientY }; });
  // Set initial cursor
  _updateViewerCursor();
})();

// ── Crash recovery (launch-time) ────────────────────────────────────────────
// On launch, if the disk-backed draft store holds un-applied editor edits from a
// previous run, surface them up front so the user doesn't have to remember and
// reopen what they were working on. "Restore" reopens the source file and drops
// the user straight into the editor on the affected page with edits restored.
let _recoveryDrafts = [];

async function _checkCrashRecovery() {
  // A window spawned by tab detach (popout) hosts an already-live session, not a
  // post-crash relaunch — it must never show the recovery prompt.  Its
  // sessionStorage is fresh (per-window), so the once-per-launch guard below
  // wouldn't stop it; key off the URL marker instead and set the guard so any
  // later in-window navigation stays quiet too.
  try {
    if (_params.get('detached')) { sessionStorage.setItem('_pw_recovery_checked', '1'); return; }
  } catch (_) {}

  // Run once per app launch. sessionStorage survives the in-app layout round-trip
  // (so we don't re-prompt on every navigation) but is empty on a fresh launch.
  try {
    if (sessionStorage.getItem('_pw_recovery_checked')) return;
    sessionStorage.setItem('_pw_recovery_checked', '1');
  } catch (_) {}

  // Drafts were the editor's un-applied edits.  Nothing writes them now — every
  // viewer edit commits immediately — so any on disk are leftovers from an
  // older build that nothing can restore.  Dispose of them rather than leaving
  // them to sit there forever unmentioned.
  pwDraftClear().catch(() => {});

  let snaps = [];
  try { snaps  = await pwRecoveryList(); } catch (_) {}

  const items = [];
  // Committed working-bytes snapshots.  Full bytes are stored, so these
  // restore without needing the original file.
  for (const s of (snaps || [])) {
    items.push({
      _type: 'snapshot', docId: s.docId, filename: s.filename,
      page_num: s.page_num, total_pages: s.total_pages,
      updatedAt: s.updatedAt, _restorable: true,
    });
  }
  if (!items.length) return;
  items.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));

  _recoveryDrafts = items;
  _renderRecoveryList();
  const ov = document.getElementById('recovery-overlay');
  if (ov) { ov.style.display = 'flex'; ov.style.pointerEvents = 'all'; }
}

function _renderRecoveryList() {
  const list = document.getElementById('recovery-list');
  if (!list) return;
  list.innerHTML = '';
  _recoveryDrafts.forEach((d, i) => {
    const missing = !d._restorable;
    const when = d.updatedAt ? new Date(d.updatedAt).toLocaleString() : '';

    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 11px;border:1px solid var(--border);border-radius:8px;background:var(--surface2)';

    const info = document.createElement('div');
    info.style.cssText = 'min-width:0;flex:1';
    const title = document.createElement('div');
    title.style.cssText = 'font-size:13px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
    title.textContent = `${d.filename || 'Document'} · page ${d.page_num || '?'}`;
    const sub = document.createElement('div');
    sub.style.cssText = 'font-size:11px;color:var(--text-dim)';
    sub.textContent = (d._type === 'snapshot')
      ? (when ? 'unsaved changes · ' + when : 'unsaved changes')
      : (missing ? '⚠ original file not found' : (when ? 'saved ' + when : 'unsaved changes'));
    info.appendChild(title); info.appendChild(sub);

    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;gap:6px;flex-shrink:0';
    if (!missing) {
      const rb = document.createElement('button');
      rb.className = 'btn btn-ghost'; rb.textContent = 'Restore';
      rb.onclick = () => _recoverByIndex(i);
      btns.appendChild(rb);
    }
    const db = document.createElement('button');
    db.className = 'btn btn-ghost'; db.textContent = 'Discard';
    db.onclick = () => _discardByIndex(i);
    btns.appendChild(db);

    row.appendChild(info); row.appendChild(btns);
    list.appendChild(row);
  });
}

function _hideRecovery() {
  const ov = document.getElementById('recovery-overlay');
  if (ov) { ov.style.display = 'none'; ov.style.pointerEvents = 'none'; }
}

async function _recoverByIndex(i) {
  const d = _recoveryDrafts[i];
  if (!d) return;
  _hideRecovery();
  if (d._type === 'snapshot') await _recoverSnapshot(d);
}

async function _discardByIndex(i) {
  const d = _recoveryDrafts[i];
  if (!d) return;
  if (d._type === 'snapshot') await pwRecoveryClear({ docId: d.docId });
  _recoveryDrafts.splice(i, 1);
  if (!_recoveryDrafts.length) { _hideRecovery(); return; }
  _renderRecoveryList();
}

async function recoveryRestoreAll() {
  _hideRecovery();
  const recoverable = _recoveryDrafts.filter(d => d._restorable);
  let entered = false;
  for (const d of recoverable) {
    if (d._type === 'snapshot') await _recoverSnapshot(d);
    entered = true;
  }
  const missing = _recoveryDrafts.length - recoverable.length;
  if (missing > 0)
    showToast(window.t('toast.recovery_missing', { count: missing }), 'error');
}

async function recoveryDiscardAll() {
  _hideRecovery();
  const snaps = _recoveryDrafts.filter(d => d._type === 'snapshot');
  for (const s of snaps) await pwRecoveryClear({ docId: s.docId });
  _recoveryDrafts = [];
}

// Restore an applied/committed working-bytes snapshot: the server rebuilds a
// full session from the stored page bytes (no original file needed) and we open
// it like any freshly-opened document.  The restored bytes are unsaved, so the
// doc stays dirty until the user saves.
async function _recoverSnapshot(d) {
  const data = await pwRecoveryRestore(d.docId);
  if (!data || !data.pages || !data.pages.length) {
    showToast(window.t('toast.could_not_restore') + (d.filename || 'document'), 'error');
    return;
  }
  await _viewerHandleOpenResponse(data);
  if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
}

// "Not now" — keep the drafts on disk so they're offered again next launch.
function recoveryDismiss() { _hideRecovery(); }

// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener('load', () => {
  if (SESSION_ID) showLoading('Loading…');

  // When returning from the layout editor, sessionStorage holds the full tab
  // list that was open before navigation.  Restore it so all tabs reappear.
  const savedJson = sessionStorage.getItem('_pw_tabs');
  if (savedJson) {
    try {
      const savedTabs = JSON.parse(savedJson);
      sessionStorage.removeItem('_pw_tabs');
      sessionStorage.removeItem('_pw_active_tab');

      if (Array.isArray(savedTabs) && savedTabs.length) {
        // The active tab is identified by the session_id in the current URL
        // (entry.html always returns to /viewer?session_id=ACTIVE_SESSION).
        const urlSession  = _params.get('session_id') || '';
        let   activeIdx   = savedTabs.findIndex(t => t.session_id === urlSession);
        if (activeIdx < 0) activeIdx = 0;

        _tabs         = savedTabs;
        _activeTabIdx = activeIdx;
        SESSION_ID    = _tabs[_activeTabIdx].session_id;
        currentPageId = _tabs[_activeTabIdx].page_id || '';
        zoomLevel     = _tabs[_activeTabIdx].zoomLevel || 1.0;
        _fitWidthPending = !_tabs[_activeTabIdx].zoomLevel;
        _pendingScrollAnchor = _tabs[_activeTabIdx].scroll || null;
        const zl = document.getElementById('zoom-label');
        if (zl) zl.textContent = Math.round(zoomLevel * 100) + '%';
        _renderTabBar();
      }
    } catch(_) {
      // Malformed storage — fall through to normal single-tab init
    }
  }

  // If _tabs wasn't populated above, initialise the single first tab
  if (!_tabs.length) {
    _tabs         = [{session_id: SESSION_ID, page_id: currentPageId,
                      label: 'Document', zoomLevel: null,
                      mode: 'viewer', scroll: null}];
    _activeTabIdx = 0;
    _fitWidthPending = true;   // boot document opens fitted to width
  }

  if (SESSION_ID) {
    loadPageList().then(() => {
      if (window.bootMark) bootMark('page list loaded');
      return Promise.all([loadAllAnnotations(), loadFormFields(), loadAllLinks()]);
    });
  } else {
    // No session on fresh start — show the empty state immediately
    if (window.bootMark) bootMark('empty state (no document)');
    _updateViewerEmptyState();
  }

  // After boot settles, offer to recover any unsaved editor drafts left behind by
  // a previous crash/forced close. Guarded to run once per app launch.
  setTimeout(_checkCrashRecovery, 600);
});

