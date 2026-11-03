/* Regression tests: each open document keeps its own scroll position — in the
 * page column AND in the thumbnail pane beside it.
 *
 * The bugs these pin down: with two documents open, scrolling in one, switching
 * to the other, then switching back put you at the *top of the page* you had
 * been on — the offset into that page was thrown away, because switching tabs
 * rebuilds the page column from scratch and only the page id was remembered.
 * And the thumbnail pane was never part of any tab's state at all: rebuilding
 * its list leaves scrollTop alone, so opening a second document showed page 1
 * in the viewer while the thumbnails stayed parked at the first document's
 * position.
 *
 * These drive the real src/frontend/js/viewer-panel.js (see viewer_harness.mjs)
 * over a headless DOM, so they exercise the actual switch → reload → restore
 * path rather than a re-implementation of it.
 *
 * Run: node tests/js/viewer_tab_scroll.test.mjs
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

// ── fixture: two documents, opened as two tabs ───────────────────────────────
async function twoTabs() {
  const h = loadViewer({
    sessA: { pages: makePages('sessA', 12, 'alpha.pdf') },
    sessB: { pages: makePages('sessB', 12, 'beta.pdf') },
  });
  // Surface any error the viewer would merely have toasted.
  h.ctx.showToast = (m) => { throw new Error('viewer error toast: ' + m); };
  h.run("openNewTab('sessA','')");
  await h.flush();
  h.run("openNewTab('sessB','')");
  await h.flush();
  eq(h.run('_tabs.length'), 2, 'two tabs open');
  return h;
}

// ── tests ────────────────────────────────────────────────────────────────────

test('each tab keeps its own scroll position across switches', async () => {
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.userScroll(1500); await h.flush();
  eq(h.run('SESSION_ID'), 'sessA', 'tab 0 is document A');

  await h.run('_switchTab(1)'); await h.flush();
  h.userScroll(620); await h.flush();
  eq(h.run('SESSION_ID'), 'sessB', 'tab 1 is document B');

  await h.run('_switchTab(0)'); await h.flush();
  eq(h.area.scrollTop, 1500, 'document A came back to where it was left');

  await h.run('_switchTab(1)'); await h.flush();
  eq(h.area.scrollTop, 620, 'document B came back to where it was left');

  h.dispose();
});

test('restoring lands mid-page, not snapped to the page top', async () => {
  // The regression itself: the old code scrolled the remembered *page* into
  // view, silently dropping the offset into it.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  const pageTop = h.blockTop('sessA-p3');
  assert(pageTop > 0, 'page 3 has a position');
  h.userScroll(pageTop + 400); await h.flush();
  eq(h.run('currentPageId'), 'sessA-p3', 'page 3 is the current page');

  await h.run('_switchTab(1)'); await h.flush();
  await h.run('_switchTab(0)'); await h.flush();

  const back = h.blockTop('sessA-p3');
  eq(h.area.scrollTop - back, 400, 'the offset into page 3 survived the switch');
  assert(h.area.scrollTop !== back, 'not snapped back to the top of page 3');

  h.dispose();
});

test('horizontal scroll is restored too', async () => {
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.userScroll(900, 150); await h.flush();

  await h.run('_switchTab(1)'); await h.flush();
  await h.run('_switchTab(0)'); await h.flush();

  eq(h.area.scrollTop, 900, 'vertical position restored');
  eq(h.area.scrollLeft, 150, 'horizontal position restored');

  h.dispose();
});

test('the restored position holds as page heights settle', async () => {
  // On switch the column is rebuilt from estimated placeholder heights; the
  // real heights arrive later, page by page.  The viewport must stay put
  // relative to the page it was restored to, not drift with the estimates.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  const top = h.blockTop('sessA-p4');
  h.userScroll(top + 250); await h.flush();

  await h.run('_switchTab(1)'); await h.flush();
  await h.run('_switchTab(0)'); await h.flush();
  eq(h.area.scrollTop - h.blockTop('sessA-p4'), 250, 'restored 250px into page 4');

  // Pages 1–3 render taller than estimated; renderPageBlock re-pins.
  ['sessA-p1', 'sessA-p2', 'sessA-p3'].forEach(id => h.setBlockHeight(id, 1203));
  h.run('_applyPendingPin()');
  await h.flush();

  eq(h.area.scrollTop - h.blockTop('sessA-p4'), 250, 'still 250px into page 4');

  h.dispose();
});

test('a page far above the anchor settling late does not shift the view', async () => {
  // Pages render lazily and in no fixed order — one arriving well above the
  // restored position (outside the pin's neighbourhood) still moves everything
  // below it, so the anchor has to hold until the user takes over.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  const top = h.blockTop('sessA-p8');
  h.userScroll(top + 120); await h.flush();

  await h.run('_switchTab(1)'); await h.flush();
  await h.run('_switchTab(0)'); await h.flush();
  assert(h.run('_pendingPin') !== null, 'the anchor is still held');

  h.setBlockHeight('sessA-p1', 2000);      // nowhere near page 8
  h.run('_applyPendingPin()');
  await h.flush();

  eq(h.area.scrollTop - h.blockTop('sessA-p8'), 120, 'still 120px into page 8');

  h.dispose();
});

test('zooming releases the anchor instead of jumping the view back', async () => {
  // The remembered offset is in pixels at the zoom it was taken at, so it must
  // not be re-applied after a zoom changes every page height.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.userScroll(1500); await h.flush();
  await h.run('_switchTab(1)'); await h.flush();
  await h.run('_switchTab(0)'); await h.flush();
  assert(h.run('_pendingPin') !== null, 'the anchor is held after the switch');

  h.run('zoomLevel = 2.0; applyZoom();');
  eq(h.run('_pendingPin'), null, 'the anchor was retired by the zoom');

  h.dispose();
});

test('a user scroll while the pages settle is not overridden', async () => {
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.userScroll(2000); await h.flush();
  await h.run('_switchTab(1)'); await h.flush();
  await h.run('_switchTab(0)'); await h.flush();
  eq(h.area.scrollTop, 2000, 'restored');

  h.userScroll(0); await h.flush();                       // user takes over
  eq(h.run('_pendingPin'), null, 'the restore stopped chasing the user');

  ['sessA-p1', 'sessA-p2'].forEach(id => h.setBlockHeight(id, 1203));
  h.run('_applyPendingPin()');
  await h.flush();
  eq(h.area.scrollTop, 0, 'the user is left where they scrolled to');

  h.dispose();
});

test('a newly opened document starts at the top', async () => {
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.userScroll(3000); await h.flush();

  h.run("openNewTab('sessB','')");   // reopening B as a third tab
  await h.flush();

  eq(h.run('_tabs.length'), 3, 'a third tab opened');
  eq(h.area.scrollTop, h.blockTop('sessB-p1'), 'the new tab shows page 1, not a stale offset');

  h.dispose();
});

test('a remembered page that no longer exists falls back to the current page', async () => {
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.userScroll(1500); await h.flush();
  await h.run('_switchTab(1)'); await h.flush();

  // Simulate the anchored page having gone away (deleted in another window).
  h.run("_tabs[0].scroll = {pageId: 'sessA-gone', dy: 400, dx: 0}");
  await h.run('_switchTab(0)'); await h.flush();

  const cur = h.run('currentPageId');
  assert(Number.isFinite(h.area.scrollTop), 'scroll position is a real number');
  eq(h.area.scrollTop, h.blockTop(cur), 'fell back to the top of the current page');

  h.dispose();
});

test('the scroll position is kept while the tab is in Layout', async () => {
  // In Layout mode the viewer panel is hidden, so there is no live
  // scroll position to read.  Saving tab state then must not overwrite the
  // remembered one with a bogus zero.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.userScroll(1500); await h.flush();
  h.run('_saveActiveTabMeta()');
  const saved = JSON.stringify(h.run('_tabs[0].scroll'));

  h.area.clientHeight = 0;         // panel-viewer display:none
  h.area.scrollTop = 0;
  eq(h.run('_captureScrollAnchor()'), null, 'nothing to capture while hidden');
  h.run('_saveActiveTabMeta()');
  eq(JSON.stringify(h.run('_tabs[0].scroll')), saved, 'the remembered position survived');

  h.dispose();
});


// ── thumbnail pane ──────────────────────────────────────────────

// Ids of the thumbnails the list marks as the current page.
function activeThumbIds(h) {
  return [...h.thumbs.innerHTML.matchAll(/thumb-item active"[^]*?id="thumb-([^"]+)"/g)]
    .map(m => m[1]);
}

test('a newly opened document starts the thumbnail pane at the top', async () => {
  // The reported bug: open a document, scroll well down it, drop in a second
  // one — the viewer shows page 1 but the thumbnails sat where the first
  // document had been left.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.userScroll(3000);
  h.thumbs.scrollTop = 4200;      // pane scrolled deep into document A
  await h.flush();

  h.run("openNewTab('sessB','')");
  await h.flush();

  eq(h.thumbs.scrollTop, 0, 'the new document opens with its thumbnails at page 1');
  eq(activeThumbIds(h).join(), 'sessB-p1', 'page 1 is the marked thumbnail');

  h.dispose();
});

test('each tab keeps its own thumbnail-pane position', async () => {
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.thumbs.scrollTop = 1200; await h.flush();

  await h.run('_switchTab(1)'); await h.flush();
  eq(h.thumbs.scrollTop, 0, 'document B did not inherit A\'s pane position');
  h.thumbs.scrollTop = 300; await h.flush();

  await h.run('_switchTab(0)'); await h.flush();
  eq(h.thumbs.scrollTop, 1200, 'document A came back to its own pane position');

  await h.run('_switchTab(1)'); await h.flush();
  eq(h.thumbs.scrollTop, 300, 'document B came back to its own pane position');

  h.dispose();
});

test('the thumbnail list marks the page the tab is restored to', async () => {
  // The list used to be built before currentIdx was worked out, so a tab
  // restored to page 8 briefly marked page 1 as current.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.userScroll(h.blockTop('sessA-p8') + 60); await h.flush();
  eq(h.run('currentPageId'), 'sessA-p8', 'left on page 8');

  await h.run('_switchTab(1)'); await h.flush();
  await h.run('_switchTab(0)'); await h.flush();

  eq(activeThumbIds(h).join(), 'sessA-p8', 'page 8 is the marked thumbnail');

  h.dispose();
});

test('the thumbnail position is kept while the pane is hidden', async () => {
  // In Layout mode — and while the left panel shows bookmarks — the pane
  // is display:none, where scrollTop reads 0.  Saving tab state then must not
  // overwrite the remembered position with that zero.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.thumbs.scrollTop = 900; await h.flush();
  h.run('_saveActiveTabMeta()');
  eq(h.run('_tabs[0].thumbScroll'), 900, 'the visible position was captured');

  h.thumbs.clientHeight = 0;      // pane hidden
  h.thumbs.scrollTop = 0;
  eq(h.run('_captureThumbScroll()'), null, 'nothing to capture while hidden');
  h.run('_saveActiveTabMeta()');
  eq(h.run('_tabs[0].thumbScroll'), 900, 'the remembered position survived');

  h.dispose();
});

test('re-rendering one page\'s thumbnail leaves the pane where it is', async () => {
  // renderThumbList() rebuilds the whole list whenever a single thumbnail is
  // refreshed (an annotation baked in, a rotation).  That must not move the
  // pane out from under the user.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  h.thumbs.scrollTop = 640; await h.flush();

  h.run('renderThumbList()');
  await h.flush();

  eq(h.thumbs.scrollTop, 640, 'the pane stayed put');

  h.dispose();
});

test('thumbnails reserve their box before the images decode', async () => {
  // Without a pre-sized box the list is a fraction of its eventual height while
  // the (lazily decoded) thumbnails arrive, so any position set on it is
  // clamped to that stub height — which is how a restored position silently
  // became the wrong page.
  const h = await twoTabs();

  await h.run('_switchTab(0)'); await h.flush();
  const items = h.thumbs.innerHTML.match(/aspect-ratio:612\/792/g) || [];
  eq(items.length, 12, 'every thumbnail declares the page aspect ratio');
  assert(/onload="this\.style\.aspectRatio=''"/.test(h.thumbs.innerHTML),
         'the decoded image takes over from the estimate');

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
