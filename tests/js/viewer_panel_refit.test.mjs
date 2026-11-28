/* Opening or closing a side panel re-fits the page (v3.5).
 *
 * Report: open the Tools panel and the right-hand side of the page goes under
 * it — the document has to be scrolled to reach what you want to edit.  The
 * viewer fitted the width once, on open, and never again.
 *
 * Now (_refitForCanvasWidth, driven by a ResizeObserver on #canvas-area):
 *   * a fitted zoom follows the canvas width, narrower AND wider;
 *   * a zoom the user chose is kept, except that a page which fitted is shrunk
 *     to keep fitting, and the user's zoom comes back when the room does;
 *   * a page zoomed past the edge on purpose is left alone.
 *
 * Run: node tests/js/viewer_panel_refit.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
function eq(a, b, m) { if (a !== b) throw new Error(`${m || 'differs'}: expected ${b}, got ${a}`); }
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function near(actual, expected, tol, msg) {
  if (!(Math.abs(actual - expected) <= tol)) {
    throw new Error(`${msg || 'values differ'}: expected ~${expected} (±${tol}), got ${actual}`);
  }
}

const NAT = 595 * 1.5;                       // portrait page at the base render scale
const fit = (w) => (w - 58) / NAT;           // _fitZoomFor in single-page layout

async function openDoc() {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 3, 'a.pdf', () => ({ w: 595, h: 842 })) } });
  h.ctx.showToast = (m) => { throw new Error('viewer error toast: ' + m); };
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 2);
  h.run('_refitForCanvasWidth()');           // the observer's first sighting: 900px
  return h;
}
async function resize(h, w) {
  h.area.clientWidth = w;
  h.run('_refitForCanvasWidth()');
  await h.flush();
}
const zoom = (h) => h.run('zoomLevel');

test('a fitted page re-fits when the Tools panel opens, and again when it closes', async () => {
  const h = await openDoc();
  near(zoom(h), fit(900), 0.002, 'opens fitted');
  await resize(h, 640);                      // panel open
  near(zoom(h), fit(640), 0.002, 'fits the narrower canvas');
  await resize(h, 900);                      // panel closed
  near(zoom(h), fit(900), 0.002, 'fits the full width again');
  h.dispose();
});

test('the FIRST panel toggle after opening re-fits, with no earlier resize seen', async () => {
  // In the app the observer's first sighting came before the document (canvas
  // empty), so the first toggle only recorded a baseline and did nothing.  The
  // fit itself now records the width it was made for.
  const h = loadViewer({ sessA: { pages: makePages('sessA', 3, 'a.pdf', () => ({ w: 595, h: 842 })) } });
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 2);
  await resize(h, 640);
  near(zoom(h), fit(640), 0.002, 'fits the narrower canvas on the first toggle');
  h.dispose();
});

test('the re-fit waits for the panel slide to FINISH, both ways', async () => {
  // Field report (v3.5): opening the Tools panel on the manual shrank the page
  // "a bit, but not enough".  The re-fit ran mid-slide (a fixed 220ms guess;
  // the slide ran late on a heavy document) and measured a canvas still on
  // its way; nothing re-fitted when the slide ended.
  const h = await openDoc();
  const rp = h.doc.getElementById('right-panel');
  h.run('toggleRightPanel()');               // open: the slide starts
  h.area.clientWidth = 760;                  // part-way
  h.run('_refitForCanvasWidth()');           // what the observer does mid-slide
  near(zoom(h), fit(900), 0.002, 'no re-fit against a canvas still sliding');
  h.area.clientWidth = 640;                  // slide done
  rp.dispatch('transitionend', { target: rp, propertyName: 'width' });
  await h.flush();
  near(zoom(h), fit(640), 0.002, 'fits the final width once the slide ends');

  h.run('toggleRightPanel()');               // close
  h.area.clientWidth = 760;
  h.run('_refitForCanvasWidth()');
  near(zoom(h), fit(640), 0.002, 'mid-slide again: untouched');
  h.area.clientWidth = 900;
  rp.dispatch('transitionend', { target: rp, propertyName: 'width' });
  await h.flush();
  near(zoom(h), fit(900), 0.002, 'back to the full-width fit');
  h.dispose();
});

test('a slide that never reports its end is still judged (timeout)', async () => {
  const h = await openDoc();
  h.run('toggleRightPanel()');
  h.area.clientWidth = 640;
  await new Promise(r => setTimeout(r, 800));
  await h.flush();
  near(zoom(h), fit(640), 0.002, 'fitted after the fallback timeout');
  h.dispose();
});

test('a zoom the user chose is shrunk only when the page would stop fitting, then given back', async () => {
  const h = await openDoc();
  h.run("zoomLevel = 0.5; _zoomMode = 'custom'; applyZoom()"); await h.flush();
  await resize(h, 640);                      // 0.5 still fits (fit ~0.65)
  near(zoom(h), 0.5, 1e-9, 'still fits: untouched');
  await resize(h, 400);                      // fit ~0.38: would overflow
  near(zoom(h), fit(400), 0.002, 'shrunk to keep the page fitting');
  await resize(h, 900);
  near(zoom(h), 0.5, 1e-9, "the user's zoom comes back with the room");
  h.dispose();
});

test('a page zoomed past the edge on purpose is left alone', async () => {
  const h = await openDoc();
  h.run("zoomLevel = 1.5; _zoomMode = 'custom'; applyZoom()"); await h.flush();
  await resize(h, 640);
  near(zoom(h), 1.5, 1e-9, 'zoom unchanged');
  await resize(h, 900);
  near(zoom(h), 1.5, 1e-9, 'still unchanged');
  h.dispose();
});

test('zooming while squeezed makes the new zoom the one kept', async () => {
  const h = await openDoc();
  h.run("zoomLevel = 0.5; _zoomMode = 'custom'; applyZoom()"); await h.flush();
  await resize(h, 400);                      // squeezed
  h.run("zoomLevel = 0.3; _zoomMode = 'custom'; applyZoom()"); await h.flush();
  await resize(h, 900);
  near(zoom(h), 0.3, 1e-9, 'the later choice wins, not the pre-squeeze zoom');
  h.dispose();
});

// ── v3.5 field report: "the panel overlaps again" ─────────────────────────
// Whether the zoom is a fit used to be GUESSED from zoomLevel === _autoFitZoom.
// _resetViewerState clears _autoFitZoom but keeps the zoom, so after a page-rail
// reload (or a tab switch) a fitted page counted as "the user's zoom" — and as
// it was a hair over the fit, "zoomed past the edge on purpose": left alone.

test('a fitted page still re-fits after a page-rail reload', async () => {
  const h = await openDoc();
  h.run("_pgCtxReload('sessA-p1')");
  await h.flush();
  await h.renderPages(0, 2);
  await resize(h, 640);
  near(zoom(h), fit(640), 0.002, 'still treated as fitted');
  h.dispose();
});

test('each tab keeps its own fit / own-zoom state', async () => {
  const h = loadViewer({
    sessA: { pages: makePages('sessA', 2, 'a.pdf', () => ({ w: 595, h: 842 })) },
    sessB: { pages: makePages('sessB', 2, 'b.pdf', () => ({ w: 595, h: 842 })) } });
  h.run("openNewTab('sessA','')"); await h.flush(); await h.renderPages(0, 1);
  h.run('_refitForCanvasWidth()');
  h.run("openNewTab('sessB','')"); await h.flush(); await h.renderPages(0, 1);
  h.run('zoomIn()'); await h.flush();                 // B: the user's own zoom
  h.run('_switchTab(0)'); await h.flush(); await h.renderPages(0, 1);
  eq(h.run('_zoomMode'), 'fit', 'A is still fitted');
  await resize(h, 640);
  near(zoom(h), fit(640), 0.002, 'A follows the width');
  h.run('_switchTab(1)'); await h.flush();
  eq(h.run('_zoomMode'), 'custom', "B kept the user's zoom");
  h.dispose();
});

test('a zoom a scrollbar-width over the fit is not "past the edge on purpose"', async () => {
  const h = await openDoc();
  // The user's zoom, sitting 1.5% over what fits (a re-layout, a scrollbar).
  h.ctx._z = fit(900) * 1.015;
  h.run("zoomLevel = _z; _zoomMode = 'custom'; applyZoom()"); await h.flush();
  await resize(h, 640);
  near(zoom(h), fit(640), 0.002, 'shrunk to fit, not left overflowing');
  h.dispose();
});

// ── v3.5, measured in the real app: the page fitted, the COLUMN did not ──
// [ZOOM_FIT] logged canvas 1272, page 1214, column 1510: the placeholders of
// pages not yet rendered kept the width the column was BUILT at, so after the
// panel opened they held the column wide and the fitted page was centred in
// it, pushed under the panel.  Every earlier test here rendered every page and
// measured page 1 only, which is exactly why this shipped.
function widestBlock(h) {
  return h.run('allPages.map(p => p.id)')
    .map(id => h.doc.getElementById('page-block-' + id))
    .filter(Boolean).reduce((m, b) => Math.max(m, b.offsetWidth), 0);
}

test('unrendered pages shrink with the fit too — nothing holds the column wide', async () => {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 8, 'long.pdf', () => ({ w: 595, h: 842 })) } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')"); await h.flush();
  await h.renderPages(0, 1);                       // pages 3-8 stay placeholders
  h.run('_refitForCanvasWidth()');
  await resize(h, 640);
  near(zoom(h), fit(640), 0.002, 'fitted');
  assert(widestBlock(h) <= 640 - 48 + 1,
         'every block fits the narrower canvas, placeholders included: ' + widestBlock(h));
  await resize(h, 900);
  assert(widestBlock(h) <= 900 - 48 + 1, 'and again at full width: ' + widestBlock(h));
  h.dispose();
});

test("placeholders follow the user's own zoom, not the canvas", async () => {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 8, 'long.pdf', () => ({ w: 595, h: 842 })) } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')"); await h.flush();
  await h.renderPages(0, 1);
  h.run("zoomLevel = 0.4; _zoomMode = 'custom'; applyZoom()"); await h.flush();
  const ph = h.doc.getElementById('page-block-sessA-p8').offsetWidth;
  near(ph, 595 * 1.5 * 0.4, 1, 'a placeholder is as wide as its page will render');
  h.dispose();
});

test('a hidden canvas (Layout in front) is ignored, then judged when shown', async () => {
  const h = await openDoc();
  h.area.clientWidth = 0;
  h.run('_refitForCanvasWidth()');
  near(zoom(h), fit(900), 0.002, 'no re-fit against a zero width');
  await resize(h, 640);
  near(zoom(h), fit(640), 0.002, 'fits once it is measurable again');
  h.dispose();
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failed++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}
console.log(failed ? `${failed} failing` : `${tests.length} passing`);
process.exit(failed ? 1 : 0);
