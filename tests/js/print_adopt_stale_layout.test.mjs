/* Regression tests: Ctrl+P must print the document in front, not Layout's.
 *
 * v3.5 field report: with one PDF open, double-click a second in Explorer (it
 * opens in a new tab), press Ctrl+P — the new tab was replaced by the FIRST
 * document and that went to print.  openNewTab moves the viewer's SESSION_ID
 * but Layout's `sessionId` stays on whatever it last held; printDocument calls
 * adoptLayoutDocument, which read the mismatch as "Layout holds a document the
 * viewer has never seen" and loaded it into the active tab.
 *
 * Layout's id is only live while Layout is showing, so adoption now happens
 * only when the viewer has no document, or Layout is the panel in front.
 *
 * These drive the real src/frontend/js/shared.js.
 *
 * Run: node tests/js/print_adopt_stale_layout.test.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE      = path.dirname(fileURLToPath(import.meta.url));
const SHARED_JS = path.resolve(HERE, '..', '..', 'src', 'frontend', 'js', 'shared.js');

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function eq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`${msg || 'values differ'}: expected ${expected}, got ${actual}`);
  }
}

function stubElement() {
  return {
    style: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
    addEventListener() {}, removeEventListener() {}, appendChild() {}, remove() {},
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
    focus() {}, blur() {}, click() {}, contains: () => false,
    children: [], childNodes: [], textContent: '', innerHTML: '', value: '',
  };
}

/** shared.js plus the viewer/layout globals adoptLayoutDocument reads. */
function load({ viewerSid, layoutSid, layoutShowing }) {
  const panels = { 'panel-viewer': stubElement(), 'panel-layout': stubElement() };
  panels['panel-viewer'].style.display = layoutShowing ? 'none' : 'flex';
  panels['panel-layout'].style.display = layoutShowing ? 'flex' : 'none';
  const body = stubElement();
  const doc = {
    body, documentElement: stubElement(), activeElement: body,
    addEventListener() {}, removeEventListener() {},
    getElementById: (id) => panels[id] || stubElement(),
    querySelector: () => null, querySelectorAll: () => [],
    createElement: () => stubElement(),
  };
  const sandbox = {
    console, document: doc,
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    requestAnimationFrame: (fn) => setTimeout(fn, 0), cancelAnimationFrame: clearTimeout,
    Promise, JSON, Math, Date, Set, Map, Object, Array, String, Number, Boolean, RegExp, Error,
    URLSearchParams, URL, AbortController,
    navigator: { userAgent: 'node', platform: 'node' },
    location: { origin: 'http://localhost:49371', search: '' },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.window.addEventListener = () => {};
  sandbox.window.removeEventListener = () => {};
  sandbox.window.t = (k) => k;
  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SHARED_JS, 'utf8'), ctx, { filename: 'shared.js' });
  const run = (code) => vm.runInContext(code, ctx);
  ctx._cfg = { viewerSid, layoutSid };
  run(`
    SESSION_ID = _cfg.viewerSid;
    var sessionId = _cfg.layoutSid;
    currentPageId = 'pB1';
    var allPages = SESSION_ID ? [{ id: 'pB1' }] : [];
    var _tabs = [{ session_id: 'A', page_id: 'pA1' }, { session_id: SESSION_ID, page_id: 'pB1' }];
    var _activeTabIdx = 1;
    var _loads = [];
    function _resetViewerState() { allPages = []; }
    async function loadPageList() { _loads.push(SESSION_ID); allPages = [{ id: SESSION_ID + '1' }]; }
  `);
  return { ctx, run };
}

test('Ctrl+P after opening a second document keeps that document (the field bug)', async () => {
  // Viewer on B (just double-clicked); Layout still on A from the first open.
  const h = load({ viewerSid: 'B', layoutSid: 'A', layoutShowing: false });
  const ok = await h.run('adoptLayoutDocument()');
  eq(ok, true, 'a document is available');
  eq(h.run('SESSION_ID'), 'B', 'viewer must stay on the document in front');
  eq(h.run('_tabs[1].session_id'), 'B', 'the active tab must not be rewritten');
  eq(h.run('_loads.length'), 0, 'nothing reloaded');
});

test('an empty viewer still adopts a document dropped straight into Layout', async () => {
  const h = load({ viewerSid: '', layoutSid: 'L', layoutShowing: true });
  const ok = await h.run('adoptLayoutDocument()');
  eq(ok, true, 'Layout\'s document is adopted');
  eq(h.run('SESSION_ID'), 'L', 'viewer now on the Layout session');
  eq(h.run('_loads.join()'), 'L', 'page list loaded once');
});

test('with Layout in front, its live session still wins', async () => {
  const h = load({ viewerSid: 'B', layoutSid: 'L', layoutShowing: true });
  await h.run('adoptLayoutDocument()');
  eq(h.run('SESSION_ID'), 'L', 'Layout is live, so its session is adopted');
});

test('an empty viewer with nothing in Layout reports no document', async () => {
  const h = load({ viewerSid: '', layoutSid: null, layoutShowing: false });
  eq(await h.run('adoptLayoutDocument()'), false, 'nothing to print');
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failed++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}
console.log(failed ? `${failed} failing` : `${tests.length} passing`);
process.exit(failed ? 1 : 0);
