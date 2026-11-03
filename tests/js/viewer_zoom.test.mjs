/* Regression tests: zooming the viewer holds its place and follows the gesture.
 *
 * Two complaints these pin down:
 *   • the +/- buttons made the page jump — the column grows with the zoom but
 *     scrollTop stayed where it was, so the content slid out from under the
 *     viewport;
 *   • a trackpad pinch lurched repeatedly — every gesture delta was rounded to
 *     the next preset zoom level, on a 250 ms throttle.
 *
 * These drive the real src/frontend/js/viewer-panel.js over a headless DOM whose
 * page blocks are laid out at their canvas size, so zooming really does change
 * the geometry the scroll math has to compensate for.
 *
 * Run: node tests/js/viewer_zoom.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

// ── tiny test runner ─────────────────────────────────────────────────────────
const tests = [];
const test  = (name, fn) => tests.push([name, fn]);

function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`${msg || 'values differ'}: expected ${expected}, got ${actual}`);
  }
}
function near(actual, expected, tol, msg) {
  if (!(Math.abs(actual - expected) <= tol)) {
    throw new Error(`${msg || 'values differ'}: expected ~${expected} (±${tol}), got ${actual}`);
  }
}

// ── fixture ──────────────────────────────────────────────────────────────────
async function viewerWithPages(n = 8) {
  const h = loadViewer({ sessA: { pages: makePages('sessA', n, 'alpha.pdf') } });
  h.ctx.showToast = (m) => { throw new Error('viewer error toast: ' + m); };
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, n - 1);
  return h;
}

/** Which page point sits at a given offset down the viewport, in layout space. */
function pointAt(h, offsetY, offsetX) {
  const area = h.area;
  const cy = area.scrollTop + (offsetY === undefined ? area.clientHeight / 2 : offsetY);
  const cx = area.scrollLeft + (offsetX === undefined ? area.clientWidth / 2 : offsetX);
  const pages = h.run('allPages.map(p => p.id)');
  for (const pid of pages) {
    const b = h.doc.getElementById('page-block-' + pid);
    if (!b) continue;
    if (cy >= b.offsetTop && cy <= b.offsetTop + b.offsetHeight) {
      return { pid,
               fy: (cy - b.offsetTop)  / b.offsetHeight,
               fx: (cx - b.offsetLeft) / b.offsetWidth };
    }
  }
  return null;
}

function wheel(h, deltaY, at) {
  h.fireWindow('wheel', {
    ctrlKey: true, deltaY, deltaMode: 0,
    clientX: (at && at.x) !== undefined ? at.x : 400,
    clientY: (at && at.y) !== undefined ? at.y : 300,
  });
}

// ── tests ────────────────────────────────────────────────────────────────────

test('the +/- buttons hold the viewport centre in place', async () => {
  const h = await viewerWithPages();
  h.userScroll(2600); await h.flush();

  const before = pointAt(h);
  assert(before, 'a page is under the viewport centre');
  const scrollBefore = h.area.scrollTop;

  for (let i = 0; i < 4; i++) { h.run('zoomIn()'); await h.flush(); }
  const zoomed = pointAt(h);
  eq(zoomed.pid, before.pid, 'still looking at the same page');
  near(zoomed.fy, before.fy, 0.002, 'the same point on the page is centred');
  assert(h.area.scrollTop !== scrollBefore,
         'the scroll position must move to keep the page still');

  for (let i = 0; i < 4; i++) { h.run('zoomOut()'); await h.flush(); }
  const back = pointAt(h);
  eq(back.pid, before.pid, 'still the same page after zooming back out');
  near(back.fy, before.fy, 0.002, 'and the same point on it');

  h.dispose();
});

test('without compensation the page would have jumped', async () => {
  // Guards the guard: confirms the fixture is sensitive enough to see the bug.
  const h = await viewerWithPages();
  h.userScroll(2600); await h.flush();
  const before = pointAt(h);

  // What the old code did — resize the pages, leave scrollTop alone.
  h.run('zoomLevel = 1.6; redrawAllRendered();');
  await h.flush();
  const after = pointAt(h);
  assert(!after || after.pid !== before.pid || Math.abs(after.fy - before.fy) > 0.2,
         'un-compensated zoom should visibly move the content');

  h.dispose();
});

test('ctrl+wheel zooms about the pointer, not the centre', async () => {
  const h = await viewerWithPages();
  h.userScroll(2600); await h.flush();

  // A point well away from the viewport centre (which is 450, 400 here).
  const focal = { x: 300, y: 120 };
  const before = pointAt(h, focal.y, focal.x);
  assert(before, 'a page is under the pointer');

  wheel(h, -6, focal); await h.flush();
  wheel(h, -6, focal); await h.flush();
  wheel(h, -6, focal); await h.flush();

  const after = pointAt(h, focal.y, focal.x);
  eq(after.pid, before.pid, 'the page under the pointer is unchanged');
  near(after.fy, before.fy, 0.003, 'the point under the pointer stayed put');
  near(after.fx, before.fx, 0.01, 'horizontally too');

  h.dispose();
});

test('a pinch zooms continuously instead of stepping between presets', async () => {
  const h = await viewerWithPages();
  h.userScroll(1200); await h.flush();

  const steps = h.run('ZOOM_STEPS');
  const seen = [];
  for (let i = 0; i < 8; i++) { wheel(h, -4); await h.flush(); seen.push(h.run('zoomLevel')); }

  // Strictly growing, and in increments far smaller than the preset ladder.
  for (let i = 1; i < seen.length; i++) {
    assert(seen[i] > seen[i - 1], 'each gesture delta zooms in a little further');
    assert(seen[i] / seen[i - 1] < 1.08, 'and by a small proportional amount');
  }
  assert(seen.some(z => !steps.includes(z)),
         'a pinch should land between the preset levels, not snap to them');

  h.dispose();
});

test('a burst of gesture deltas costs one zoom per frame', async () => {
  const h = await viewerWithPages();
  h.userScroll(1200); await h.flush();

  let applied = 0;
  const real = h.ctx.applyZoom;
  h.ctx.applyZoom = function (...args) { applied++; return real.apply(this, args); };

  const before = h.run('zoomLevel');
  for (let i = 0; i < 12; i++) wheel(h, -3);   // one frame's worth of a pinch
  eq(applied, 0, 'nothing is applied until the frame runs');
  await h.flush();
  eq(applied, 1, 'twelve deltas collapse into a single zoom');
  // …and that one zoom carries the whole burst, not just the last delta.
  assert(h.run('zoomLevel') / before > 1.3, 'the whole gesture was applied');

  h.dispose();
});

test('the gesture is clamped to the zoom range', async () => {
  const h = await viewerWithPages();
  const max = h.run('ZOOM_MAX'), min = h.run('ZOOM_MIN');

  for (let i = 0; i < 40; i++) { wheel(h, -20); await h.flush(); }
  eq(h.run('zoomLevel'), max, 'zooming in stops at the maximum');

  for (let i = 0; i < 80; i++) { wheel(h, 20); await h.flush(); }
  eq(h.run('zoomLevel'), min, 'zooming out stops at the minimum');

  h.dispose();
});

test('one mouse-wheel notch is a step, not a leap', async () => {
  // A wheel sends |deltaY| 100+ where a trackpad sends single digits; without a
  // clamp the exponential would multiply the zoom by e (or divide by it).
  const h = await viewerWithPages();
  const before = h.run('zoomLevel');
  wheel(h, -120); await h.flush();
  const after = h.run('zoomLevel');
  assert(after > before, 'it zooms in');
  assert(after / before <= 1.25 + 1e-9, `one notch multiplied zoom by ${after / before}`);

  h.dispose();
});

test('pages off screen during a gesture are resized now and redrawn after', async () => {
  const h = await viewerWithPages();
  h.userScroll(0); await h.flush();

  const lastId = h.run('allPages[allPages.length - 1].id');
  const lastBlock = h.doc.getElementById('page-block-' + lastId);
  const heightBefore = lastBlock.offsetHeight;
  const zoomBefore   = h.run('zoomLevel');

  wheel(h, -8); await h.flush();

  assert(h.run(`_pageBlocks.get('${lastId}').needsRedraw`) === true,
         'an off-screen page is deferred rather than redrawn mid-gesture');
  // Deferred or not, its layout size has to be right or the scroll math breaks.
  near(lastBlock.offsetHeight / heightBefore, h.run('zoomLevel') / zoomBefore, 0.005,
       'the deferred page still takes up its new height');

  h.run('_flushDeferredRedraws()');
  eq(h.run(`_pageBlocks.get('${lastId}').needsRedraw`), false,
     'and it is redrawn properly once the gesture settles');

  h.dispose();
});

// ── run ──────────────────────────────────────────────────────────────────────
let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log(`  ok    ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name}\n        ${e.message}`);
    if (process.env.PW_TEST_TRACE) console.log(e.stack);
  }
}
console.log(failed ? `\n${failed} of ${tests.length} tests failed` : `\n${tests.length} tests passed`);
process.exit(failed ? 1 : 0);
