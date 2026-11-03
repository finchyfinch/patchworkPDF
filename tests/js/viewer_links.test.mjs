/* Regression tests: a URL embedded in the document is clickable in the Viewer.
 *
 * The overlay is the same shape as the form-field one — a positioned div per
 * link over the page image — and the three things that make it behave sit in
 * different places, so each is pinned here:
 *
 *   * GEOMETRY. Link rects arrive in PDF points, y up from the bottom, like
 *     every other overlay; the div is placed in CSS pixels, y down from the
 *     top, and has to track the zoom. Get the flip wrong and links sit mirrored
 *     down the page, which "works" on a single centred link and nowhere else.
 *   * PRECEDENCE. A hit-area that is always live eats the first drag of every
 *     drawing tool, so an armed tool switches the whole overlay off.
 *   * CLICK vs DRAG. The hand tool pans with a drag that starts wherever the
 *     pointer happens to be — often on a link. Press-move-release must pan, not
 *     navigate.
 *
 * What the click sends is a link INDEX, never a URL: the backend re-reads the
 * destination from the document (routes/viewer.py, /api/viewer/open_link), so
 * nothing here can point the OS browser somewhere the PDF doesn't.
 *
 * Run: node tests/js/viewer_links.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function near(actual, expected, tol, msg) {
  if (!(Math.abs(actual - expected) <= tol)) {
    throw new Error(`${msg || 'values differ'}: expected ~${expected} (±${tol}), got ${actual}`);
  }
}

// ── fixture ──────────────────────────────────────────────────────────────────
// Letter portrait, rendered at scale 1.5 (what the harness's render endpoint
// reports).  One link near the TOP of the page: y is measured up from the
// bottom in PDF space, so a large y must become a small CSS `top`.
const PAGE = { w: 612, h: 792 };
const LINK_TOP    = { idx: 0, x: 72, y: 700, w: 228, h: 20, url: 'https://example.com/docs' };
const LINK_BOTTOM = { idx: 1, x: 72, y:  60, w: 100, h: 12, url: 'mailto:hello@example.com' };

async function openDoc(links) {
  const h = loadViewer({
    sessA: {
      pages: makePages('sessA', 2, 'linked.pdf', () => PAGE),
      links: links === undefined ? { 'sessA-p1': [LINK_TOP, LINK_BOTTOM] } : links,
    },
  });
  h.toasts = [];
  h.ctx.showToast = (m, kind) => { h.toasts.push({ m, kind }); };
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 1);
  return h;
}

const hits = (h, pageId) => {
  const ov = h.doc.getElementById('link-ov-' + pageId);
  return ov ? ov.children : [];
};
const px = (el, prop) => parseFloat(el.style[prop]);

/** Press, optionally move, release — the gesture a hit-area actually sees. */
function clickHit(el, dx = 0, dy = 0) {
  const at = { clientX: 400, clientY: 300 };
  el.dispatch('mousedown', { clientX: at.clientX, clientY: at.clientY,
                             preventDefault() {}, stopPropagation() {} });
  el.dispatch('click', { clientX: at.clientX + dx, clientY: at.clientY + dy,
                         preventDefault() {}, stopPropagation() {} });
}

// ── the feature ──────────────────────────────────────────────────────────────

test('a link in the document becomes a hit-area over the page', async () => {
  const h = await openDoc();
  assert(hits(h, 'sessA-p1').length === 2, 'both links on page 1 get a hit-area');
  assert(hits(h, 'sessA-p2').length === 0, 'a page with no links gets none');
  h.dispose();
});

test('the hit-area lands where the link is, y flipped and zoom applied', async () => {
  const h = await openDoc();
  const scale = 1.5 * h.run('zoomLevel');       // renderScale × zoom
  const [top, bottom] = hits(h, 'sessA-p1');

  near(px(top, 'left'),   72 * scale, 0.5, 'left edge');
  near(px(top, 'width'),  228 * scale, 0.5, 'width');
  near(px(top, 'height'), 20 * scale, 0.5, 'height');
  // y=700..720 up from the bottom of a 792pt page → 72pt down from the top.
  near(px(top, 'top'), (792 - 720) * scale, 0.5, 'y is flipped, not copied');
  assert(px(top, 'top') < px(bottom, 'top'),
         'the link high on the page must sit above the one low on it — a missing '
         + 'flip passes a single-link check and mirrors the whole page');
  h.dispose();
});

test('hovering shows the destination', async () => {
  const h = await openDoc();
  const el = hits(h, 'sessA-p1')[0];
  assert(el.title === LINK_TOP.url, 'the tooltip names where the link goes');
  assert(el.className.includes('pdf-link-hit'), 'hover styling hangs off the class');
  assert(el.getAttribute('role') === 'link');
  h.dispose();
});

test('clicking opens the link by index, never by URL', async () => {
  const h = await openDoc();
  clickHit(hits(h, 'sessA-p1')[1]);
  await h.flush();

  const posts = h.callsTo('/api/viewer/open_link');
  assert(posts.length === 1, 'one open request');
  assert(posts[0].body.idx === 1 && posts[0].body.page_id === 'sessA-p1',
         'the request names the link, not its destination');
  assert(!('url' in posts[0].body),
         'sending a URL would let the page choose where the browser goes');
  assert(h.toasts.some(t => t.kind === 'success'), 'the user is told it opened');
  h.dispose();
});

test('the hit-areas follow a zoom', async () => {
  const h = await openDoc();
  const before = px(hits(h, 'sessA-p1')[0], 'left');
  const z0 = h.run('zoomLevel');
  h.run('zoomIn()');
  await h.flush();
  const ratio = h.run('zoomLevel') / z0;
  const after = px(hits(h, 'sessA-p1')[0], 'left');
  near(after, before * ratio, 1.0, 'a zoomed page must not leave its links behind');
  h.dispose();
});

// ── precedence: other tools own the click while they are armed ───────────────

test('an armed drawing tool switches the links off', async () => {
  const h = await openDoc();
  assert(hits(h, 'sessA-p1')[0].style.pointerEvents === 'all', 'live to begin with');

  h.run("_armTool('rect')");
  assert(Array.from(hits(h, 'sessA-p1')).every(el => el.style.pointerEvents === 'none'),
         'a hit-area that stays live eats the first drag of every drawing tool');

  h.run('_disarmTool()');
  assert(Array.from(hits(h, 'sessA-p1')).every(el => el.style.pointerEvents === 'all'),
         'and comes back when the tool is put down');
  h.dispose();
});

test('a click that arrives with a tool armed is ignored', async () => {
  const h = await openDoc();
  const el = hits(h, 'sessA-p1')[0];
  h.run("_armTool('rect')");
  clickHit(el);
  await h.flush();
  assert(h.callsTo('/api/viewer/open_link').length === 0,
         'pointer-events is CSS; the handler must refuse for itself too');
  h.dispose();
});

test('links stay live under the hand tool — reading is not editing', async () => {
  const h = await openDoc();
  h.run("setViewerTool('hand')");
  h.run('_refreshLinkOverlays()');
  assert(hits(h, 'sessA-p1')[0].style.pointerEvents === 'all');
  h.dispose();
});

// ── click vs drag ────────────────────────────────────────────────────────────

test('a press that turns into a drag pans, it does not open the link', async () => {
  const h = await openDoc();
  clickHit(hits(h, 'sessA-p1')[0], 40, 15);     // released 40px away
  await h.flush();
  assert(h.callsTo('/api/viewer/open_link').length === 0,
         'starting a pan on top of a link must not navigate on release');
  h.dispose();
});

test('a click with a hand-tremor still opens the link', async () => {
  const h = await openDoc();
  clickHit(hits(h, 'sessA-p1')[0], 2, 1);       // inside the slop
  await h.flush();
  assert(h.callsTo('/api/viewer/open_link').length === 1,
         'the drag guard must not make ordinary clicks flaky');
  h.dispose();
});

// ── lifecycle ────────────────────────────────────────────────────────────────

test('links are fetched once per document, not once per page', async () => {
  const h = await openDoc();
  await h.renderPages(0, 1);
  assert(h.callsTo('/api/viewer/links').length === 1,
         'a per-page fetch would add a request to every page of a fast scroll');
  h.dispose();
});

test("switching documents drops the previous document's links", async () => {
  const h = loadViewer({
    sessA: { pages: makePages('sessA', 1, 'linked.pdf', () => PAGE),
             links: { 'sessA-p1': [LINK_TOP] } },
    sessB: { pages: makePages('sessB', 1, 'plain.pdf', () => PAGE) },
  });
  h.ctx.showToast = () => {};

  h.run("openNewTab('sessA','')"); await h.flush(); await h.renderPages(0, 0);
  assert(hits(h, 'sessA-p1').length === 1);

  h.run("openNewTab('sessB','')"); await h.flush(); await h.renderPages(0, 0);
  assert(Object.keys(h.run('_allLinks')).length === 0,
         'stale links would put phantom hit-areas over the new document');
  assert(hits(h, 'sessB-p1').length === 0);
  h.dispose();
});

test('a document with no links adds no overlay children and still renders', async () => {
  const h = await openDoc({});
  assert(hits(h, 'sessA-p1').length === 0);
  assert(h.doc.getElementById('link-ov-sessA-p1'), 'the overlay exists, just empty');
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
