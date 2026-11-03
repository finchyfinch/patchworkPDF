/* Loads the real src/frontend/js/viewer-panel.js into a Node vm context on top
 * of the headless DOM in dom_stub.mjs, so tests can drive the actual tab /
 * scroll code rather than a copy of it.
 *
 * viewer-panel.js is a plain <script> (no modules): its top-level `let`s land
 * in the context's lexical scope, where a follow-up script can read and write
 * them — the same sharing the browser gives it with shared.js.  The globals it
 * expects from shared.js are declared in the prelude below.
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { makeDocument } from './dom_stub.mjs';

const HERE     = path.dirname(fileURLToPath(import.meta.url));
const SRC_JS    = path.resolve(HERE, '..', '..', 'src', 'frontend', 'js');
const VIEWER_JS = path.join(SRC_JS, 'viewer-panel.js');
const SHARED_JS = path.join(SRC_JS, 'shared.js');
const LAYOUT_JS = path.join(SRC_JS, 'layout.js');

// Pages served for a session id by the fake /api/viewer/pages.
//
// sizeFor(i) optionally gives a page its own dimensions: {w, h} are what the
// page list reports (width_pts/height_pts), and {rw, rh} what /api/viewer/render
// reports for it.  They differ in real documents — the page list measures the
// MediaBox, the renderer measures the displayed page — which is how a landscape
// /Rotate 90 page can be listed as portrait.
function makePages(sessionId, count, filename, sizeFor) {
  return Array.from({ length: count }, (_, i) => {
    const d = (sizeFor && sizeFor(i)) || {};
    const w = d.w || 612, h = d.h || 792;
    return {
      id: `${sessionId}-p${i + 1}`,
      filename,
      page_num: i + 1,
      total_pages: count,
      width_pts: w,
      height_pts: h,
      render_w: d.rw || w,   // harness-only: what the render endpoint answers
      render_h: d.rh || h,
      thumbnail: '',    // the Image stub ignores the data and reports a fixed aspect
    };
  });
}

/** loadViewer(docs, opts) builds one page-like context and loads the real panel
 *  scripts into it, the way viewer.html loads them all into one page.
 *
 *  opts:
 *    layout  — also load layout.js
 *    shared  — load the REAL shared.js instead of the prelude's shims.  Needed by
 *              anything testing the unsaved-work machinery, because that lives in
 *              the seam between shared.js and the panels: the doc-dirty flag and
 *              the bridge push are shared.js's, the per-tab parking and each
 *              panel's Save are the panels'.  A shim of one half cannot catch a
 *              panel that never calls the other.
 *    native  — install a window.pywebview bridge, so isNativeApp() is true and
 *              the OS-save-dialog path is the one that runs (h.bridge records it).
 */
export function loadViewer(docs, opts) {
  const useShared = !!(opts && opts.shared);
  const { doc, area, column, thumbs, edArea, edCanvas } = makeDocument();

  const timers = [];
  const calls  = [];
  // What the page pushed across the native bridge, in order (entry.py's
  // WindowApi caches exactly this and the close guard reads nothing else).
  const bridge = { unsaved: [], savedPaths: [], confirmed: 0, cancelled: 0, closed: 0 };
  const sandbox = {
    console,
    document: doc,
    setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); timers.push(t); return t; },
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    requestAnimationFrame: (fn) => sandbox.setTimeout(fn, 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
    AbortController,
    URLSearchParams,
    Map, Set, Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp, Error,
    // Thumbnail probing in buildPageColumn: every page reports letter portrait.
    Image: class { set src(_v) { this.naturalWidth = 850; this.naturalHeight = 1100;
                                 sandbox.setTimeout(() => this.onload && this.onload(), 0); } },
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    MutationObserver: class { observe() {} disconnect() {} },
    localStorage:   _memStorage(),
    sessionStorage: _memStorage(),
    location: { origin: 'http://localhost:49371', search: '', href: '', pathname: '/viewer' },
    navigator: { userAgent: 'node', clipboard: {} },
    // Every fetch the viewer makes during a load.  Calls are recorded so a test
    // can assert on side effects that leave no trace in the page — the recovery
    // snapshot being dropped after a save is exactly that.
    apiFetch: async (url, opts) => {
      const body = opts && opts.body ? JSON.parse(opts.body) : {};
      calls.push({ url, body });
      return {
        ok: true, status: 200,
        headers: { get: () => 'application/json' },
        json: async () => _api(url, body, docs),
        blob: async () => ({ size: 1, type: 'application/pdf' }),
      };
    },
  };
  sandbox.URL = { createObjectURL: () => 'blob:pdf', revokeObjectURL() {} };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  // Window-level listeners are kept so tests can fire real gestures at them
  // (ctrl+wheel zoom, keyboard shortcuts).
  const winListeners = {};
  sandbox.window.addEventListener = (type, fn) => {
    (winListeners[type] = winListeners[type] || []).push(fn);
  };
  sandbox.window.removeEventListener = (type, fn) => {
    winListeners[type] = (winListeners[type] || []).filter(f => f !== fn);
  };
  sandbox.window.t = (k) => k;
  sandbox.window.matchMedia = () => ({ matches: false, addEventListener() {} });
  sandbox.window.getComputedStyle = () => ({ getPropertyValue: () => '' });
  sandbox.window.innerWidth = 1200;
  sandbox.window.innerHeight = 900;
  sandbox.navigator.platform = 'node';

  // The native host, when a test asks for it: pick_save_path answers a path (so
  // the OS-dialog branch of every panel's Save is the one exercised) and
  // set_unsaved records what the close guard would read.
  if (opts && opts.native) {
    sandbox.window.pywebview = {
      api: {
        set_unsaved:  (v) => { bridge.unsaved.push(v); return true; },
        confirm_quit: () => { bridge.confirmed++;  return true; },
        cancel_quit:  () => { bridge.cancelled++;  return true; },
        close:        () => { bridge.closed++; },
        pick_save_path: async (_sid, name) => {
          const p = 'C:/docs/' + (name || 'document.pdf');
          bridge.savedPaths.push(p);
          return p;
        },
        pick_open_files: async () => null,
        restore_focus: () => true,
      },
    };
  }

  const ctx = vm.createContext(new Proxy(sandbox, {
    // Unknown identifiers resolve to Node's own globals (intrinsics), then to
    // undefined — the viewer guards optional helpers with `typeof x ===
    // 'function'`, so undefined is the right answer for layout.js.
    has: () => true,
    get: (t, k) => (k in t ? t[k] : globalThis[k]),
    set: (t, k, v) => { t[k] = v; return true; },
  }));

  // Helpers viewer.html defines inline or that shared.js would otherwise pull in
  // real UI for.  Function declarations, so loading the REAL shared.js after this
  // simply replaces the ones it owns — the same last-one-wins the page gives us.
  const COMMON_SHIMS = `
    function showToast() {}
    function showLoading() {}
    function hideLoading() {}
    function showPrompt() { return Promise.resolve(null); }
    function _reportDragRegions() {}
    function bootMark() {}
    function newProgressId() { return 'job'; }
    function startProgressPoll() { return function () {}; }
    let sessions = null;
  `;

  // Only used WITHOUT shared.js: stand-ins for the globals it owns.  Every one of
  // these is a `let`/`const` shared.js also declares, so they must not both run —
  // a redeclaration in the context's lexical scope is a SyntaxError.
  const SHARED_SHIMS = `
    const _params = new URLSearchParams('');
    let SESSION_ID = '';
    let currentPageId = '';
    let pageW = 0, pageH = 0, renderScale = 1.5, zoomLevel = 1.0;
    function isNativeApp() { return false; }
    function apiErr(d) { return new Error((d && d.error) || 'error'); }
    function showConfirm() { return Promise.resolve(true); }
    function _setActivePanel() {}
    // Mirrors shared.js: one doc-dirty flag plus a registry of extra checks, so
    // a test can ask the same question the beforeunload guard does.
    let _pwDocDirty = false;
    const _pwDirtyChecks = [];
    function pwSetDocDirty(v) { _pwDocDirty = (v !== false); }
    function pwIsDocDirty() { return _pwDocDirty; }
    function pwRegisterDirtyCheck(fn) { if (typeof fn === 'function') _pwDirtyChecks.push(fn); }
    function pwHasUnsavedWork() {
      if (_pwDocDirty) return true;
      for (const fn of _pwDirtyChecks) { try { if (fn()) return true; } catch (_) {} }
      return false;
    }
    function pwPublishUnsaved() {}
    function pwMarkSaved() { pwSetDocDirty(false); }
    // Every viewer mutation calls this in its .then() tail. Without a shim the
    // call THROWS, the promise rejects, and everything after it in that tail is
    // silently skipped — so a test asserting on the tail passes or fails for
    // the wrong reason. A no-op is enough; tests that care override it.
    function _invalidateViewerPage() {}
    let _viewerZoomBeforeEdit = 1.0, _editorEntryDispW = null;
  `;

  // layout.js declares `let sessionId` itself; without it, the viewer's
  // layout-adoption paths still read the name.
  const LAYOUT_SHIM = (opts && opts.layout) ? '' : `let sessionId = '';`;

  vm.runInContext(COMMON_SHIMS + LAYOUT_SHIM + (useShared ? '' : SHARED_SHIMS),
                  ctx, { filename: 'prelude.js' });

  // Page load order, as in viewer.html: shared.js, then the panels.
  if (useShared) {
    vm.runInContext(fs.readFileSync(SHARED_JS, 'utf8'), ctx, { filename: 'shared.js' });
  }
  if (opts && opts.layout) {
    vm.runInContext(fs.readFileSync(LAYOUT_JS, 'utf8'), ctx, { filename: 'layout.js' });
  }
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), ctx, { filename: 'viewer-panel.js' });

  const api = {
    ctx, doc, area, column, thumbs, edArea, edCanvas, calls, bridge,
    /** Every recorded call to one endpoint. */
    callsTo: (url) => calls.filter(c => c.url.startsWith(url)),
    /** Evaluate an expression against the viewer's own scope. */
    run: (expr) => vm.runInContext(expr, ctx, { filename: 'test.js' }),
    /** Let queued timers/microtasks (image decodes, rAF, fetches) drain. */
    flush: async (rounds = 8) => {
      for (let i = 0; i < rounds; i++) await new Promise(r => setTimeout(r, 0));
    },
    /** A scroll the *user* performed (the scroll event follows on flush). */
    userScroll: (top, left) => {
      area.scrollTop = top;
      if (left !== undefined) area.scrollLeft = left;
    },
    blockTop: (pageId) => {
      const b = doc.getElementById('page-block-' + pageId);
      return b ? b.offsetTop : null;
    },
    /** Fire an event at the window listeners the viewer registered. */
    fireWindow: (type, ev) => {
      (winListeners[type] || []).slice().forEach(fn =>
        fn(Object.assign({ type, preventDefault() {}, stopPropagation() {} }, ev)));
    },
    /** Render pages the way the lazy observer would once they scroll into view. */
    renderPages: async (from, to) => {
      for (let i = from; i <= to; i++) api.run(`renderPageBlock(${i})`);
      await api.flush();
    },
    /** Grow a page block to its "real" rendered height (renders settling). */
    setBlockHeight: (pageId, px) => {
      const b = doc.getElementById('page-block-' + pageId);
      if (b) b.setHeight(px);
    },
    dispose: () => timers.forEach(clearTimeout),
  };
  return api;
}

export { makePages };

function _memStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null),
           setItem: (k, v) => m.set(k, String(v)),
           removeItem: (k) => m.delete(k), clear: () => m.clear() };
}

function _api(url, body, docs) {
  if (url.startsWith('/api/viewer/pages')) {
    const d = docs[body.session_id];
    if (!d) return { error: 'no such session' };
    return { pages: d.pages, is_protected: false, restricted: false };
  }
  // Document links, keyed by page id — docs[sid].links, absent for most fixtures.
  if (url.startsWith('/api/viewer/links')) {
    const d = docs[body.session_id];
    return { links_by_page: (d && d.links) || {} };
  }
  if (url.startsWith('/api/viewer/open_link')) {
    const d  = docs[body.session_id];
    const ls = (d && d.links && d.links[body.page_id]) || [];
    const l  = ls[body.idx];
    return l ? { ok: true, url: l.url } : { error: 'Link not found' };
  }
  if (url.startsWith('/api/viewer/render')) {
    const d  = docs[body.session_id];
    const pg = d && d.pages.find(p => p.id === body.page_id);
    return { image: '', page_w: pg ? pg.render_w : 612,
             page_h: pg ? pg.render_h : 792, scale: 1.5 };
  }
  // Opening files: one page per path, so a test can tell an open (empty grid)
  // from a merge (grid already holding pages) apart.
  if (url.startsWith('/api/open_paths')) {
    const paths = body.paths || [];
    return {
      session_id: body.session_id || 'sessA',
      save_dir:   'C:/docs',
      errors:     [],
      pages: paths.map((f, i) => ({
        id: 'opened-' + f.replace(/[^a-z0-9]/gi, '') + '-' + i,
        filename: f.split('/').pop(), page_num: 1, total_pages: 1,
        rotation: 0, thumbnail: '', width_pts: 612, height_pts: 792,
      })),
    };
  }
  // Saves and mutations: enough shape for the callers to treat them as success.
  if (url.startsWith('/api/save'))         return { success: true, saved_to: body.save_path };
  if (url.startsWith('/api/rotate'))       return { thumbnail: '', rotation: 90 };
  if (url.startsWith('/api/delete'))       return { success: true };
  if (url.startsWith('/api/reorder'))      return { success: true };
  if (url.startsWith('/api/insert_blank_page')) {
    return { success: true, page: { id: 'new-blank', filename: 'blank.pdf', page_num: 1,
                                    total_pages: 1, rotation: 0, thumbnail: '',
                                    width_pts: 595.28, height_pts: 841.89 } };
  }
  if (url.startsWith('/api/compress'))     return { old_size: 100, new_size: 50, failed: 0, pages: [] };
  if (url.startsWith('/api/metadata'))     return { success: true, metadata: {} };
  if (url.startsWith('/api/set_password')) return { success: true, protected: !!body.password };
  if (url.startsWith('/api/viewer/fill_form')) return { success: true, updated: 1 };
  // The viewer's own form-field routes speak the DISPLAY frame and echo the
  // stored field back in it, so the stub returns the rect it was given.
  if (url.startsWith('/api/viewer/add_form_field')) {
    const f = (body && body.field) || {};
    return { success: true,
             field: { id: 'vff-' + (f.type || 'text'), name: 'n1', options: [],
                      page_h: 792, value: '', ...f } };
  }
  if (url.startsWith('/api/viewer/update_form_field')) {
    return { success: true, field: { id: 'vff-text', ...(body && body.updates || {}) } };
  }
  if (url.startsWith('/api/viewer/delete_form_field')) return { success: true };
  // Redaction is a page-byte rewrite: it reports history counts like every
  // other mutation so the toolbar's Undo/Redo stay in step.
  if (url.startsWith('/api/viewer/page_elements')) {
    return { page_top: 792, page_left: 0, items: [
      { id: 'txt_1', text: 'Alpha', x: 72, y: 700, x2: 120, y2: 712,
        size: 10, font_size: 11, color: '#000000', rl_font: 'Helvetica' },
      { id: 'txt_2', text: 'Beta',  x: 130, y: 700, x2: 170, y2: 712,
        size: 10, font_size: 11, color: '#000000', rl_font: 'Helvetica' },
    ] };
  }
  if (url.startsWith('/api/viewer/edit_text')) {
    return { success: true, thumbnail: 'e', undo_count: 1, redo_count: 0 };
  }
  if (url.startsWith('/api/viewer/edit_block')) {
    return { success: true, thumbnail: 'b', undo_count: 1, redo_count: 0 };
  }
  if (url.startsWith('/api/viewer/add_text')) {
    return { success: true, thumbnail: 't', undo_count: 1, redo_count: 0 };
  }
  if (url.startsWith('/api/viewer/crop')) {
    // A crop changes the page SIZE, so the reply carries the new dimensions.
    return { success: true, thumbnail: 'c', width_pts: 300, height_pts: 400,
             undo_count: 1, redo_count: 0 };
  }
  if (url.startsWith('/api/viewer/redact')) {
    return { success: true, thumbnail: '', undo_count: 1, redo_count: 0 };
  }
  // Order matters: watermark_all is the more specific prefix.
  if (url.startsWith('/api/viewer/watermark_all')) {
    return { success: true, failed: 0, undo_count: 0, redo_count: 0,
             updated: [{ id: 'sessA-p1', thumbnail: 'x' },
                       { id: 'sessA-p2', thumbnail: 'y' }] };
  }
  if (url.startsWith('/api/viewer/watermark')) {
    return { success: true, thumbnail: '', undo_count: 1, redo_count: 0 };
  }
  if (url.startsWith('/api/recovery/clear')) return { ok: true };
  if (url.startsWith('/api/annotations'))  return { annotations: {} };
  if (url.startsWith('/api/forms'))        return { fields: [] };
  if (url.startsWith('/api/bookmarks'))    return { bookmarks: [] };
  return {};
}
