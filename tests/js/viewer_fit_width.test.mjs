/* Regression tests: a mixed portrait/landscape document opens fitted to its
 * WIDEST page.
 *
 * The bug these pin down (Mixed_orientation.pdf — portrait, landscape,
 * portrait): the document opened zoomed to the portrait width, so the landscape
 * page overflowed the viewport.  #canvas-column is `width:max-content` with
 * `align-items:center`, so that overflow re-centres every narrower page inside
 * a column wider than the viewport — while scrollLeft is still 0.  Page 1 then
 * sits shifted right with its right-hand edge clipped, which is what the user
 * sees on opening.
 *
 * The fit already aimed at the widest page; what it read was wrong.  The page
 * list measures the MediaBox and the landscape page in that file is a portrait
 * MediaBox with /Rotate 90, so it was listed as 595pt wide when the renderer
 * shows it 842pt wide.  Fixed at the source (_page_dimensions now reports the
 * displayed box) with a front-end backstop: a render is ground truth, so a page
 * that comes back wider than the fit assumed corrects it.
 *
 * Run: node tests/js/viewer_fit_width.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

// ── tiny test runner ─────────────────────────────────────────────────────────
const tests = [];
const test  = (name, fn) => tests.push([name, fn]);

function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function near(actual, expected, tol, msg) {
  if (!(Math.abs(actual - expected) <= tol)) {
    throw new Error(`${msg || 'values differ'}: expected ~${expected} (±${tol}), got ${actual}`);
  }
}

// ── fixture: the shape of Mixed_orientation.pdf ──────────────────────────────
const PORTRAIT  = { w: 595, h: 842 };
const LANDSCAPE = { w: 842, h: 595 };
// The same document as the page list used to describe it: the landscape page's
// MediaBox is portrait, its /Rotate 90 unaccounted for.
const LANDSCAPE_LISTED_PORTRAIT = { w: 595, h: 842, rw: 842, rh: 595 };

async function openDoc(sizeFor, n = 3) {
  const h = loadViewer({ sessA: { pages: makePages('sessA', n, 'mixed.pdf', sizeFor) } });
  h.ctx.showToast = (m) => { throw new Error('viewer error toast: ' + m); };
  h.run("openNewTab('sessA','')");
  await h.flush();
  return h;
}

const mixed         = (i) => (i === 1 ? LANDSCAPE : PORTRAIT);
const mixedMislisted = (i) => (i === 1 ? LANDSCAPE_LISTED_PORTRAIT : PORTRAIT);

/** Widest page block on screen, in CSS px. */
function widestBlock(h) {
  return h.run('allPages.map(p => p.id)')
    .map(id => h.doc.getElementById('page-block-' + id))
    .filter(Boolean)
    .reduce((m, b) => Math.max(m, b.offsetWidth), 0);
}

// The fit leaves 58px of the 900px viewport for padding and the scrollbar.
const AVAIL = 900 - 58;

// ── tests ────────────────────────────────────────────────────────────────────

test('the document opens fitted to its widest page, not its first', async () => {
  const h = await openDoc(mixed);
  await h.renderPages(0, 2);

  // 842pt at the 1.5 base render scale = 1263 logical px; fitting that into 842
  // usable px is a zoom of ~0.667.  Fitting the portrait page instead would be
  // ~0.94 and push the landscape page 400px past the viewport.
  near(h.run('zoomLevel'), AVAIL / (842 * 1.5), 0.005, 'zoom fits the landscape page');
  assert(widestBlock(h) <= AVAIL + 1,
         'no page is wider than the viewport: ' + widestBlock(h));

  h.dispose();
});

test('the portrait pages are centred, not pushed off the right edge', async () => {
  const h = await openDoc(mixed);
  await h.renderPages(0, 2);

  const p1 = h.doc.getElementById('page-block-sessA-p1');
  const p2 = h.doc.getElementById('page-block-sessA-p2');
  // Equal margins either side of page 1 within the column, and its right edge
  // inside the viewport — the clipping in the report was neither.
  const leftGap  = p1.offsetLeft;
  const rightGap = p2.offsetWidth - (p1.offsetLeft - p2.offsetLeft) - p1.offsetWidth;
  near(leftGap - p2.offsetLeft, rightGap, 1.5, 'page 1 sits centred over the landscape page');
  assert(p1.offsetLeft + p1.offsetWidth <= h.area.clientWidth,
         'page 1 ends inside the viewport');

  h.dispose();
});

test('a page listed narrower than it renders still corrects the fit', async () => {
  // The actual defect: /Rotate 90 made the landscape page look portrait in the
  // page list.  The metadata is fixed at the source now, but a recovered
  // session (or any document whose list is missing dimensions) can still
  // under-report, and the render is the ground truth that settles it.
  const h = await openDoc(mixedMislisted);

  await h.renderPages(0, 0);          // page 1: everything still looks portrait
  near(h.run('zoomLevel'), AVAIL / (595 * 1.5), 0.005, 'fitted to the listed width');

  await h.renderPages(1, 1);          // the landscape page arrives 842pt wide
  near(h.run('zoomLevel'), AVAIL / (842 * 1.5), 0.005, 're-fitted to the real width');

  await h.renderPages(2, 2);          // (a placeholder still spans the column)
  assert(widestBlock(h) <= AVAIL + 1,
         'no page is wider than the viewport: ' + widestBlock(h));

  h.dispose();
});

test('a zoom the user chose is not overridden by a later page', async () => {
  const h = await openDoc(mixedMislisted);
  await h.renderPages(0, 0);

  h.run('zoomIn()'); await h.flush();
  const chosen = h.run('zoomLevel');

  await h.renderPages(1, 1);          // the wider page lands after the user zoomed
  near(h.run('zoomLevel'), chosen, 1e-9, 'the view stayed where the user put it');

  h.dispose();
});

test('an all-portrait document is unaffected', async () => {
  const h = await openDoc(() => PORTRAIT);
  await h.renderPages(0, 2);

  near(h.run('zoomLevel'), AVAIL / (595 * 1.5), 0.005, 'fitted to the page width');
  near(widestBlock(h), AVAIL, 1.5, 'the pages fill the viewport width');

  h.dispose();
});

test('Fit Width uses the widest page once pages have rendered', async () => {
  const h = await openDoc(mixedMislisted);
  await h.renderPages(0, 2);

  h.run('zoomIn(); zoomIn();'); await h.flush();
  h.run('zoomFitWidth()'); await h.flush();

  near(h.run('zoomLevel'), AVAIL / (842 * 1.5), 0.005, 'fits the widest rendered page');

  h.dispose();
});

test('the fit does not carry over to the next document', async () => {
  const h = loadViewer({
    sessA: { pages: makePages('sessA', 3, 'mixed.pdf',    mixed) },
    sessB: { pages: makePages('sessB', 3, 'portrait.pdf', () => PORTRAIT) },
  });
  h.ctx.showToast = (m) => { throw new Error('viewer error toast: ' + m); };

  h.run("openNewTab('sessA','')"); await h.flush();
  await h.renderPages(0, 2);
  near(h.run('zoomLevel'), AVAIL / (842 * 1.5), 0.005, 'document A fits its landscape page');

  h.run("openNewTab('sessB','')"); await h.flush();
  await h.renderPages(0, 2);
  near(h.run('zoomLevel'), AVAIL / (595 * 1.5), 0.005,
       'document B fits its own pages, not the widest page of A');

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
