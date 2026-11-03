/* Regression tests: the page half of the "closing the app would lose work" guard.
 *
 * `beforeunload` never fires in the native host, and the backend's close handler
 * runs on the UI thread where the CSP forbids eval — so it cannot ask the page
 * anything at close time.  The page therefore PUSHES its answer across the
 * bridge whenever it changes (pwPublishUnsaved), and the backend calls back into
 * pwPromptQuit() when a close would discard work.
 *
 * What matters here: the pushed value is right (it must count work sitting in
 * background tabs, not just the document on screen), it is not spammed across
 * the bridge, a change made before the bridge exists is not lost, and the
 * prompt reports the user's answer back exactly once.
 *
 * These drive the real src/frontend/js/shared.js.
 *
 * Run: node tests/js/quit_guard.test.mjs
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
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`${msg || 'values differ'}: expected ${expected}, got ${actual}`);
  }
}
function deepEq(actual, expected, msg) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${msg || 'values differ'}: expected ${b}, got ${a}`);
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

/** shared.js in a context whose bridge records every call. `native:false`
 *  loads it with no pywebview at all (the plain-browser / dev-server case). */
function loadShared(opts) {
  const body = stubElement();
  const doc = {
    body, documentElement: stubElement(), activeElement: body,
    addEventListener() {}, removeEventListener() {},
    getElementById: () => stubElement(),
    querySelector: () => null, querySelectorAll: () => [], createElement: () => stubElement(),
  };
  const bridge = { unsaved: [], confirmed: 0, cancelled: 0, closed: 0 };
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
  const api = {
    set_unsaved: (v) => { bridge.unsaved.push(v); return true; },
    confirm_quit: () => { bridge.confirmed++; return true; },
    cancel_quit:  () => { bridge.cancelled++; return true; },
    close:        () => { bridge.closed++; },
  };
  if (!opts || opts.native !== false) sandbox.window.pywebview = { api };

  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SHARED_JS, 'utf8'), ctx, { filename: 'shared.js' });
  return {
    ctx, bridge, api,
    run: (code) => vm.runInContext(code, ctx),
    /** Attach the bridge later, the way pywebview appears after page load. */
    attachBridge: () => { sandbox.window.pywebview = { api }; },
  };
}

const flush = async (n = 4) => { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 0)); };

/** Await a promise, but fail rather than hang if it never settles — a dropped
 *  re-entrancy guard leaves the first prompt's promise unresolved forever, and
 *  a suite that stalls blocks the whole pytest run instead of reporting. */
function settles(p, what) {
  let timer;
  return Promise.race([
    p.finally(() => clearTimeout(timer)),
    new Promise((_, rej) => { timer = setTimeout(
      () => rej(new Error((what || 'promise') + ' never settled')), 1000); }),
  ]);
}

// ── what gets pushed ─────────────────────────────────────────────────────────

test('the dirty flag is pushed across the bridge as it changes', async () => {
  const h = loadShared();
  h.run('pwSetDocDirty(true)');
  deepEq(h.bridge.unsaved, [true], 'edit reported');
  h.run('pwSetDocDirty(false)');
  deepEq(h.bridge.unsaved, [true, false], 'save reported');
});

test('an unchanged answer is not pushed again', async () => {
  const h = loadShared();
  h.run('pwSetDocDirty(true); pwSetDocDirty(true); pwSetDocDirty(true);');
  deepEq(h.bridge.unsaved, [true], 'the bridge is crossed once, not per call');
});

test('work registered by another check (a background tab) is counted', async () => {
  const h = loadShared();
  // Stands in for viewer-panel's "any background tab dirty?" check.
  h.run('var __bgDirty = false; pwRegisterDirtyCheck(() => __bgDirty);');
  h.run('pwSetDocDirty(false)');
  deepEq(h.bridge.unsaved, [], 'nothing to report yet');

  h.run('__bgDirty = true; pwPublishUnsaved();');
  deepEq(h.bridge.unsaved, [true],
         'the document on screen is clean, but the window still holds unsaved work');

  h.run('__bgDirty = false; pwPublishUnsaved();');
  deepEq(h.bridge.unsaved, [true, false], 'and it clears when that tab is dealt with');
});

test('a change made before the bridge exists is pushed once it appears', async () => {
  const h = loadShared({ native: false });
  h.run('pwSetDocDirty(true)');          // no pywebview yet — must not throw
  deepEq(h.bridge.unsaved, [], 'nothing sent, nothing lost');

  h.attachBridge();
  h.run('pwPublishUnsaved()');
  deepEq(h.bridge.unsaved, [true], 'the pending state goes over as soon as it can');
});

test('a push that fails is retried, not recorded as delivered', async () => {
  const h = loadShared();
  let broken = true;
  h.api.set_unsaved = (v) => {           // bridge there, but the call fails
    if (broken) throw new Error('window is going away');
    h.bridge.unsaved.push(v);
    return true;
  };
  h.run('pwSetDocDirty(true)');
  deepEq(h.bridge.unsaved, [], 'nothing got through');

  broken = false;
  h.run('pwPublishUnsaved()');
  deepEq(h.bridge.unsaved, [true],
         'the same value is sent again — a failed push is not "already reported"');
});

test('publishing in a plain browser is a no-op, not a crash', async () => {
  const h = loadShared({ native: false });
  h.run('pwSetDocDirty(true); pwPublishUnsaved(); pwSetDocDirty(false);');
  deepEq(h.bridge.unsaved, [], 'the dev server has beforeunload instead');
});

// ── answering the prompt ─────────────────────────────────────────────────────

test('accepting the prompt latches the decision, then closes', async () => {
  const h = loadShared();
  h.run('pwSetDocDirty(true)');
  const p = h.run('pwPromptQuit()');
  await flush();
  h.run('dismissConfirm(true)');
  await settles(p, 'the quit prompt'); await flush();
  eq(h.bridge.confirmed, 1, 'the backend is told the user accepted');
  eq(h.bridge.closed, 1, 'and the window is closed');
  eq(h.bridge.cancelled, 0, 'without also reporting a cancel');
});

test('declining reports the cancel and leaves the window open', async () => {
  const h = loadShared();
  h.run('pwSetDocDirty(true)');
  const p = h.run('pwPromptQuit()');
  await flush();
  h.run('dismissConfirm(false)');
  await settles(p, 'the quit prompt'); await flush();
  eq(h.bridge.cancelled, 1, 'the backend is told to re-arm');
  eq(h.bridge.closed, 0, 'the app stays open');
  eq(h.bridge.confirmed, 0, 'and nothing was latched');
});

test('a second prompt while one is open does not stack', async () => {
  const h = loadShared();
  h.run('pwSetDocDirty(true)');
  const p = h.run('pwPromptQuit()');
  await flush();
  h.run('pwPromptQuit()');               // backend asked again; must be ignored
  await flush();
  h.run('dismissConfirm(true)');
  await settles(p, 'the quit prompt'); await flush();
  eq(h.bridge.confirmed, 1, 'answered exactly once');
  eq(h.bridge.closed, 1, 'and closed exactly once');
});

test('the prompt is reachable under the name the backend calls', async () => {
  const h = loadShared();
  eq(h.run('typeof window.pwPromptQuit'), 'function',
     'entry.py run_js()es window.pwPromptQuit() by that exact name');
});

// ── run ──────────────────────────────────────────────────────────────────────
let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log('  ok   ' + name);
  } catch (e) {
    failed++;
    console.log('  FAIL ' + name + '\n       ' + e.message);
  }
}
console.log(failed ? `\n${failed} of ${tests.length} failed` : `\n${tests.length} passed`);
process.exit(failed ? 1 : 0);
