/* Regression tests: "the app always reverts to dark mode after a restart".
 *
 * The theme used to be restored from localStorage only.  localStorage belongs
 * to the page's origin, whose port can change between launches, and it lives
 * in WebView2's roaming data folder — so on a work PC the choice was lost and
 * every start came back dark.  The server copy (window.PW_PREFS.theme, from
 * ui_prefs.json) now wins, and every change is saved to it.
 *
 * These drive the real src/frontend/js/shared.js.
 *
 * Run: node tests/js/theme_persist.test.mjs
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

/** shared.js loaded with the given server prefs and localStorage contents.
 *  Records the body's theme class and every POST to /api/ui_prefs. */
function loadShared({ prefs, stored }) {
  const classes = new Set();
  const body = stubElement();
  body.classList = {
    add: (c) => classes.add(c), remove: (c) => classes.delete(c),
    toggle: (c, on) => { (on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c); },
    contains: (c) => classes.has(c),
  };
  const store = new Map(stored ? [['pw_theme', stored]] : []);
  const saved = [];
  const doc = {
    body, documentElement: stubElement(), activeElement: body,
    addEventListener() {}, removeEventListener() {},
    getElementById: () => stubElement(),
    querySelector: () => null, querySelectorAll: () => [], createElement: () => stubElement(),
  };
  const sandbox = {
    console, document: doc,
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    requestAnimationFrame: (fn) => setTimeout(fn, 0), cancelAnimationFrame: clearTimeout,
    Promise, JSON, Math, Date, Set, Map, Object, Array, String, Number, Boolean, RegExp, Error,
    URLSearchParams, URL, AbortController,
    navigator: { userAgent: 'node', platform: 'node' },
    location: { origin: 'http://localhost:49371', search: '' },
    localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    PW_PREFS: prefs,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.window.addEventListener = () => {};
  sandbox.window.removeEventListener = () => {};
  sandbox.window.t = (k) => k;
  sandbox.window.apiFetch = async (url, opts) => {
    if (url === '/api/ui_prefs') saved.push(JSON.parse(opts.body).theme);
    return { ok: true, json: async () => ({}) };
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SHARED_JS, 'utf8'), ctx, { filename: 'shared.js' });
  return {
    light: () => classes.has('theme-light'), saved, store,
    run: (code) => vm.runInContext(code, ctx),
  };
}

test('the server copy restores light mode when localStorage has nothing', () => {
  // A new port (or a reset roaming profile) means an empty localStorage.
  const h = loadShared({ prefs: { theme: 'light', lang: null }, stored: null });
  eq(h.light(), true, 'light mode must come back from window.PW_PREFS');
  eq(h.saved.length, 0, 'nothing to migrate when the server already has it');
});

test('the server copy wins over a stale localStorage value', () => {
  const h = loadShared({ prefs: { theme: 'light', lang: null }, stored: 'dark' });
  eq(h.light(), true);
});

test('a choice only localStorage knows is adopted and copied to the server', () => {
  // Saved by a build before ui_prefs.json existed.
  const h = loadShared({ prefs: { theme: null, lang: null }, stored: 'light' });
  eq(h.light(), true, 'keep the old choice on the first launch after upgrading');
  eq(h.saved.join(), 'light', 'and save it so the next launch does not need localStorage');
});

test('nothing saved anywhere stays dark and saves nothing', () => {
  const h = loadShared({ prefs: { theme: null, lang: null }, stored: null });
  eq(h.light(), false);
  eq(h.saved.length, 0);
});

test('toggling the theme saves it to the server', () => {
  const h = loadShared({ prefs: { theme: null, lang: null }, stored: null });
  h.run('toggleTheme()');
  eq(h.light(), true);
  h.run('toggleTheme()');
  eq(h.saved.join(), 'light,dark', 'each change must reach ui_prefs.json');
});

test('works with no injected prefs at all (plain browser / older page)', () => {
  const h = loadShared({ prefs: undefined, stored: 'light' });
  eq(h.light(), true);
});

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
