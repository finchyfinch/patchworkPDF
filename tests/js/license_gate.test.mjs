/* Regression tests: the first-run licence gate must fail CLOSED.
 *
 * The report: the licence acceptance page appeared on every launch, even though
 * acceptance was recorded on disk.  The backend had started answering 403 to
 * GET /api/ routes, but that alone would only have been a logged warning — what
 * put the licence wall in front of the app was this gate reading the error body
 * as an answer.  fetch does not reject on 4xx/5xx, so `r.json()` cheerfully
 * parses `{"error": ...}`, which has no `accepted` field, and `!s.accepted` is
 * true for it.
 *
 * A reply the app cannot read is not evidence the user never accepted, so only
 * an explicit `accepted: false` on a successful response may open the dialog.
 *
 * These drive the real src/frontend/js/layout.js.
 *
 * Run: node tests/js/license_gate.test.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE      = path.dirname(fileURLToPath(import.meta.url));
const LAYOUT_JS = path.resolve(HERE, '..', '..', 'src', 'frontend', 'js', 'layout.js');

// ── tiny test runner ─────────────────────────────────────────────────────────
const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function eq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`${msg || 'values differ'}: expected ${expected}, got ${actual}`);
  }
}

function stubElement() {
  return {
    style: {
      setProperty(n, v) { this[n] = String(v); },
      getPropertyValue(n) { return this[n] != null ? this[n] : ''; },
      removeProperty(n) { const v = this[n]; delete this[n]; return v || ''; },
    },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
    addEventListener() {}, removeEventListener() {}, appendChild() {}, remove() {},
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
    focus() {}, blur() {}, click() {}, contains: () => false,
    children: [], childNodes: [], textContent: '', innerHTML: '', value: '',
  };
}

/** Load layout.js with /api/license_status answering `reply`, and record
 *  whether the blocking first-run dialog was raised. */
function loadLayout(reply) {
  const doc = {
    body: stubElement(), documentElement: stubElement(),
    addEventListener() {}, removeEventListener() {},
    getElementById: () => stubElement(),
    querySelector: () => null, querySelectorAll: () => [], createElement: () => stubElement(),
  };
  const seen = { firstRunDialog: 0, licenseCalls: 0 };
  const sb = {
    console, document: doc,
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    requestAnimationFrame: (f) => setTimeout(f, 0), cancelAnimationFrame: clearTimeout,
    Promise, JSON, Math, Date, Set, Map, Object, Array, String, Number, Boolean, RegExp, Error,
    URLSearchParams, URL, AbortController,
    navigator: { userAgent: 'node' },
    location: { origin: 'http://localhost:49371', search: '' },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    isNativeApp: () => false, showToast() {}, showLoading() {}, hideLoading() {},
    showConfirm: () => Promise.resolve(true),
    pwRegisterDirtyCheck() {}, pwSetDocDirty() {},
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '' }),
    apiFetch: async (url) => {
      if (url === '/api/license_status') {
        seen.licenseCalls++;
        return reply();
      }
      return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
    },
  };
  sb.window = sb; sb.globalThis = sb;
  sb.window.addEventListener = () => {};
  sb.window.t = (k) => k;

  const ctx = vm.createContext(new Proxy(sb, {
    has: () => true,
    get: (t, k) => (k in t ? t[k] : globalThis[k]),
    set: (t, k, v) => { t[k] = v; return true; },
  }));
  vm.runInContext(fs.readFileSync(LAYOUT_JS, 'utf8'), ctx, { filename: 'layout.js' });
  // Count only the BLOCKING first-run form; showLicense(false) is the About box.
  const real = ctx.showLicense;
  ctx.showLicense = (firstRun) => { if (firstRun) seen.firstRunDialog++; return real(firstRun); };
  return { ctx, seen, run: (code) => vm.runInContext(code, ctx) };
}

/** Let layout.js's own top-level gate settle.  It fires on load and resolves on
 *  a later microtask, so the instrumentation above is in place in time — this
 *  drives the real gate rather than a copy of it. */
async function gate(h) {
  for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0));
}

const ok   = (body) => () => ({ ok: true,  status: 200, json: async () => body });
const fail = (code, body) => () => ({ ok: false, status: code, json: async () => body });

// ── the regression ───────────────────────────────────────────────────────────

test('a 403 error body does not count as "never accepted"', async () => {
  const h = loadLayout(fail(403, { error: 'permission denied', status: 403 }));
  await gate(h);
  eq(h.seen.firstRunDialog, 0,
     'an unreadable reply must not put the licence wall in front of the app');
});

test('a rate-limit reply does not re-open the licence wall', async () => {
  const h = loadLayout(fail(429, { error: 'Too many requests', retry_after: 2 }));
  await gate(h);
  eq(h.seen.firstRunDialog, 0, 'a throttle is not a licence decision');
});

test('a server error does not re-open the licence wall', async () => {
  const h = loadLayout(fail(500, { error: 'boom', status: 500 }));
  await gate(h);
  eq(h.seen.firstRunDialog, 0, 'nor is a crash');
});

test('a reply that is missing the field entirely is not an answer', async () => {
  const h = loadLayout(ok({}));
  await gate(h);
  eq(h.seen.firstRunDialog, 0, 'only an explicit false counts');
});

// ── and it still works ───────────────────────────────────────────────────────

test('a genuine first run still shows the acceptance dialog', async () => {
  const h = loadLayout(ok({ accepted: false }));
  await gate(h);
  eq(h.seen.firstRunDialog, 1, 'a real first run must still be gated');
});

test('an accepted licence is never asked about again', async () => {
  const h = loadLayout(ok({ accepted: true }));
  await gate(h);
  eq(h.seen.firstRunDialog, 0, 'the whole point of persisting acceptance');
});

test('the gate asks the backend exactly once per load', async () => {
  const h = loadLayout(ok({ accepted: true }));
  eq(h.seen.licenseCalls, 1, 'checked on load');
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
