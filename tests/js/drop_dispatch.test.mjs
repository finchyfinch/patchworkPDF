/* Regression tests: one dropped file is added exactly ONCE.
 *
 * A drop in the native app has two racing openers: pywebview pushes the real OS
 * paths to onLayoutFilesDropped/onViewerFilesDropped, and the page arms a
 * byte-upload fallback in case that push never comes.  The push travels over
 * pywebview's DOM-event bridge, which serialises the drop target — on a big
 * Layout grid (hundreds of inline base64 thumbnails) that took SECONDS, long
 * past the 700 ms fallback.  Cancelling a timer that had already fired did
 * nothing, so the file was added twice: once by the upload, once by the late
 * push.  With a .docx the second add also collided with the first inside Word's
 * single-instance COM server and surfaced as "no compatible office application".
 *
 * These drive the real src/frontend/js/shared.js.
 *
 * Run: node tests/js/drop_dispatch.test.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE      = path.dirname(fileURLToPath(import.meta.url));
const SHARED_JS = path.resolve(HERE, '..', '..', 'src', 'frontend', 'js', 'shared.js');

// ── tiny test runner ─────────────────────────────────────────────────────────
const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function eq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`${msg || 'values differ'}: expected ${expected}, got ${actual}`);
  }
}

// ── the smallest DOM shared.js will load against ─────────────────────────────
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

function loadShared() {
  const body = stubElement();
  const doc = {
    body,
    documentElement: stubElement(),
    activeElement: body,
    addEventListener() {}, removeEventListener() {},
    getElementById: () => stubElement(),
    querySelector: () => null,
    querySelectorAll: () => [],
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
  return { ctx, run: (code) => vm.runInContext(code, ctx) };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** A drop in the native (Windows push) app, with recorders for both openers. */
function nativeDrop(h) {
  const calls = { byPath: [], upload: 0 };
  h.ctx.window._nativeDropActive = true;
  h.ctx.openFilesByPath = (paths) => { calls.byPath.push(paths); };
  h.ctx._testOpenByPath = (paths) => { calls.byPath.push(paths); };
  h.ctx._testUpload     = () => { calls.upload += 1; };
  h.run('dispatchFileDrop([{name:"big.pdf"}], _testOpenByPath, _testUpload)');
  return calls;
}

// ── tests ────────────────────────────────────────────────────────────────────
test('a native path push that beats the fallback opens by path, and no upload follows', async () => {
  const h = loadShared();
  const calls = nativeDrop(h);
  await sleep(50);
  h.run('window.onLayoutFilesDropped(["C:/docs/big.pdf"])');
  eq(calls.byPath.length, 1, 'opened by path once');
  await sleep(900);                       // well past NATIVE_DROP_FALLBACK_MS
  eq(calls.upload, 0, 'byte upload must not also run');
  eq(calls.byPath.length, 1, 'still opened exactly once');
});

test('a native path push that arrives AFTER the fallback ran is ignored', async () => {
  const h = loadShared();
  const calls = nativeDrop(h);
  await sleep(900);                       // fallback has fired by now
  eq(calls.upload, 1, 'fallback uploaded the bytes');
  // The slow bridge finally delivers — this is the second-add path.
  h.run('window.onLayoutFilesDropped(["C:/docs/big.pdf"])');
  await sleep(20);
  eq(calls.byPath.length, 0, 'late paths must NOT add the file a second time');
  eq(calls.upload, 1, 'and nothing else uploads');
});

test('a late viewer push is ignored too', async () => {
  const h = loadShared();
  const calls = nativeDrop(h);
  await sleep(900);
  eq(calls.upload, 1, 'fallback ran');
  let opened = 0;
  h.ctx._testViewerOpen = () => { opened += 1; };
  // Mirrors onViewerFilesDropped's guard in viewer-panel.js.
  h.run('window.onViewerFilesDropped = function (paths) {' +
        '  if (!paths || !paths.length) return;' +
        '  if (typeof claimNativeDrop === "function" && !claimNativeDrop()) return;' +
        '  _testViewerOpen();' +
        '}; window.onViewerFilesDropped(["C:/docs/big.pdf"])');
  await sleep(20);
  eq(opened, 0, 'the viewer opener must respect the same claim');
});

test('the NEXT drop is claimable again', async () => {
  const h = loadShared();
  const first = nativeDrop(h);
  await sleep(900);
  eq(first.upload, 1, 'first drop fell back to upload');
  const second = nativeDrop(h);           // a brand-new drop
  await sleep(50);
  h.run('window.onLayoutFilesDropped(["C:/docs/second.pdf"])');
  eq(second.byPath.length, 1, 'second drop opens by its real path');
  await sleep(900);
  eq(second.upload, 0, 'and does not also upload');
});

test('a push with no page-side drop on record still opens (macOS / GTK forward)', async () => {
  const h = loadShared();
  let opened = 0;
  h.ctx.openFilesByPath = () => { opened += 1; };
  h.run('window.onLayoutFilesDropped(["C:/docs/from-gtk.pdf"])');
  eq(opened, 1, 'nothing to race with — the push must be honoured');
});

test('a plain browser drop (no native handler) uploads exactly once', async () => {
  const h = loadShared();
  const calls = { upload: 0 };
  h.ctx.window._nativeDropActive = false;
  h.ctx.window._nativeDropPull   = false;
  h.ctx._testOpenByPath = () => { throw new Error('must not open by path in a browser'); };
  h.ctx._testUpload     = () => { calls.upload += 1; };
  h.run('dispatchFileDrop([{name:"a.pdf"}], _testOpenByPath, _testUpload)');
  await sleep(900);
  eq(calls.upload, 1, 'uploaded once');
});

// ── run ──────────────────────────────────────────────────────────────────────
let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failed++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}
console.log(failed ? `${failed} failing` : `${tests.length} passing`);
process.exit(failed ? 1 : 0);
