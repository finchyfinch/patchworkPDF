/* Regression tests: scrolling fast through a long document does not flood the
 * server, and does not leave the viewport blank.
 *
 * Every page that scrolls into view used to POST /api/viewer/render the moment
 * the lazy observer noticed it.  The server answers those under one global
 * native lock (all pdfium work is serialised), so a fast flick through a few
 * hundred pages queued a render for every page crossed and the pages the user
 * actually STOPPED on sat behind all of them — seconds of blank viewport.  The
 * same burst also blew past the server's rate limit, and the 429 came back as
 * an HTML error page that res.json() rejected with
 * "Unexpected token '<', \"<!doctype\"...".
 *
 * These drive the real src/frontend/js/viewer-panel.js with a render endpoint
 * the test releases by hand, so the queue's behaviour under load is observable.
 *
 * Run: node tests/js/viewer_render_queue.test.mjs
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

const PAGES = 200;
const PAGE_H = 1000;      // each block's settled height, so scrolling is meaningful

/** A viewer over a long document whose /api/viewer/render is held open until
 *  the test releases it, so in-flight requests can be counted. */
async function viewerWithGatedRenders() {
  const h = loadViewer({ big: { pages: makePages('big', PAGES, 'long.pdf') } });
  h.ctx.showToast = () => {};

  const state = { pending: [], asked: [], inFlight: 0, maxInFlight: 0, other: [] };
  const realFetch = h.ctx.apiFetch;
  h.ctx.apiFetch = (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    if (!url.startsWith('/api/viewer/render')) {
      state.other.push({ url, body });
      return realFetch(url, opts);
    }
    state.asked.push(body.page_id);
    state.inFlight++;
    state.maxInFlight = Math.max(state.maxInFlight, state.inFlight);
    return new Promise((resolve) => {
      state.pending.push(() => {
        state.inFlight--;
        resolve({ ok: true, status: 200,
                  json: async () => ({ image: '', page_w: 612, page_h: 792, scale: 1.5 }) });
      });
    });
  };

  /** Let every currently-held render complete. */
  state.release = async () => {
    while (state.pending.length) {
      const batch = state.pending.splice(0, state.pending.length);
      batch.forEach(fn => fn());
      await h.flush();
    }
  };

  h.run("openNewTab('big','')");
  await h.flush();
  // Give the blocks real heights so scroll positions map to pages.
  h.run('allPages').forEach(p => h.setBlockHeight(p.id, PAGE_H));
  return { h, state };
}

/** Page index a page_id refers to. */
const idxOf = (pid) => parseInt(String(pid).replace('big-p', ''), 10) - 1;

// ── tests ────────────────────────────────────────────────────────────────────
test('renders are capped to a small in-flight window, not fired all at once', async () => {
  const { h, state } = await viewerWithGatedRenders();
  await state.release();                  // let the opening renders settle
  await h.flush();
  // A viewport tall enough that all 60 pages below genuinely want rendering —
  // otherwise the staleness check would drop them and mask the window cap.
  h.area.clientHeight = 60 * PAGE_H;
  state.asked.length = 0; state.maxInFlight = 0; state.inFlight = 0;

  const want = [];
  for (let i = 5; i < 65; i++) { want.push(i); h.run(`renderPageBlock(${i})`); }
  await h.flush();

  const win = h.run('RENDER_CONCURRENCY');
  eq(state.maxInFlight, win, 'the window should be full, and never exceeded');
  eq(state.asked.length, win, 'only the window should have been requested');
  eq(h.run('_renderQueue.length'), want.length - win, 'the rest wait their turn');
  h.dispose();
});

test('pages scrolled past before their turn are dropped, not rendered', async () => {
  const { h, state } = await viewerWithGatedRenders();
  state.asked.length = 0;

  // Ask for a long run of pages near the top...
  for (let i = 0; i < 60; i++) h.run(`renderPageBlock(${i})`);
  await h.flush();
  // ...then the user flicks far down the document before they are served.
  h.userScroll(150 * PAGE_H);
  await h.flush();
  await state.release();

  const far = state.asked.map(idxOf).filter(i => i >= 10 && i < 100);
  eq(far.length, 0, 'pages the user had already scroll past must not be rendered');
  h.dispose();
});

test('after dropping stale work, the pages actually on screen still render', async () => {
  const { h, state } = await viewerWithGatedRenders();

  for (let i = 0; i < 60; i++) h.run(`renderPageBlock(${i})`);
  await h.flush();
  h.userScroll(150 * PAGE_H);
  await h.flush();
  await state.release();
  await h.flush();
  await state.release();          // the sweep's renders

  const cur = h.run('currentIdx');
  const st  = h.run(`(() => { const s = _pageBlocks.get(allPages[${cur}].id);
                              return s ? {rendered: s.rendered} : null; })()`);
  assert(st && st.rendered, `the page in view (idx ${cur}) must end up rendered`);
  h.dispose();
});

test('the queue is dropped when the document changes, so no page of the old one renders', async () => {
  const { h, state } = await viewerWithGatedRenders();
  for (let i = 0; i < 60; i++) h.run(`renderPageBlock(${i})`);
  await h.flush();

  h.run('_clearRenderQueue()');
  eq(h.run('_renderQueue.length'), 0, 'queue emptied');
  eq(h.run('_renderActive'), 0, 'in-flight count reset');
  h.dispose();
});

test('a rendered page does not refetch annotations the bulk load already has', async () => {
  const { h, state } = await viewerWithGatedRenders();
  // The bulk /api/viewer/all_annotations answer, as loadAllAnnotations stores it.
  h.run("allAnnotations = Object.fromEntries(allPages.map(p => [p.id, []]))");
  state.other.length = 0;

  h.run('renderPageBlock(0)');
  await h.flush();
  await state.release();

  const perPage = state.other.filter(r => r.url.startsWith('/api/viewer/annotations'));
  eq(perPage.length, 0, 'annotations already cached must not be refetched per page');
  h.dispose();
});

test('a page missing from the bulk cache still fetches its own annotations', async () => {
  const { h, state } = await viewerWithGatedRenders();
  h.run('allAnnotations = {}');
  state.other.length = 0;

  h.run('renderPageBlock(0)');
  await h.flush();
  await state.release();

  const perPage = state.other.filter(r => r.url.startsWith('/api/viewer/annotations'));
  eq(perPage.length, 1, 'an uncached page must still load its annotations');
  h.dispose();
});

// ── opening-page render prefetch ─────────────────────────────────────────────
// The document used to open on a spinner while the server sat idle: the lazy
// observer is the only thing that starts a render, and it cannot report a block
// visible until the whole placeholder column has been laid out and the thumbnail
// rail decoded.  So the opening page's POST is now issued the moment the page
// list lands.  What must NOT change is when the render is APPLIED — the first
// render to land sets the opening fit (viewer_fit_width) and every block that
// gains its real height moves the restored scroll anchor (viewer_tab_scroll).

/** A viewer whose render endpoint is recorded but answers normally. */
async function viewerRecordingRenders(pages = 5) {
  const h = loadViewer({ big: { pages: makePages('big', pages, 'long.pdf') } });
  h.ctx.showToast = () => {};
  const asked = [];
  const realFetch = h.ctx.apiFetch;
  h.ctx.apiFetch = (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    if (url.startsWith('/api/viewer/render')) asked.push(body.page_id);
    return realFetch(url, opts);
  };
  h.run("openNewTab('big','')");
  await h.flush();
  return { h, asked };
}

test('the opening page is requested from the server as the page list lands', async () => {
  const { h, asked } = await viewerRecordingRenders();
  const first = h.run('allPages[0].id');
  eq(asked.length, 1, 'exactly the opening page should have been asked for');
  eq(asked[0], first, 'and it should be the page the document opens on');
  h.dispose();
});

test('prefetching does not render the page early', async () => {
  // The safety property the two earlier attempts at this broke: no block may be
  // built, and no fit applied, until something actually asks for the render.
  const { h } = await viewerRecordingRenders();
  const st = h.run(`(() => { const s = _pageBlocks.get(allPages[0].id);
                             return { rendered: s.rendered, rendering: s.rendering }; })()`);
  eq(st.rendered, false, 'the opening page must not be rendered by the prefetch');
  eq(st.rendering, false, 'nor marked as rendering');
  eq(h.run('_fitWidthPending'), true, 'the opening fit must still be waiting for a real render');
  eq(h.run('_renderQueue.length'), 0, 'nothing queued');
  h.dispose();
});

test('the render consumes the prefetch instead of asking twice', async () => {
  const { h, asked } = await viewerRecordingRenders();
  eq(asked.length, 1, 'prefetched');
  h.run('renderPageBlock(0)');
  await h.flush();
  eq(asked.length, 1, 'the render must reuse the prefetched answer, not refetch');
  const st = h.run(`(() => { const s = _pageBlocks.get(allPages[0].id);
                             return { rendered: s.rendered, w: s.pageW }; })()`);
  assert(st.rendered, 'and the page still ends up rendered');
  eq(st.w, 612, 'with the dimensions the server reported');
  h.dispose();
});

test('the prefetch is one-shot — a later re-render goes back to the server', async () => {
  const { h, asked } = await viewerRecordingRenders();
  h.run('renderPageBlock(0)');
  await h.flush();
  eq(asked.length, 1, 'first render used the prefetch');

  h.run(`(() => { const s = _pageBlocks.get(allPages[0].id);
                  s.rendered = false; s.rendering = false; })()`);
  h.run('renderPageBlock(0)');
  await h.flush();
  eq(asked.length, 2, 'a re-render must fetch again, not replay a stale bitmap');
  h.dispose();
});

test('a failed prefetch still lets the page render', async () => {
  const h = loadViewer({ big: { pages: makePages('big', 5, 'long.pdf') } });
  h.ctx.showToast = () => {};
  let renderCalls = 0;
  const realFetch = h.ctx.apiFetch;
  h.ctx.apiFetch = (url, opts) => {
    if (url.startsWith('/api/viewer/render')) {
      renderCalls++;
      if (renderCalls === 1) return Promise.reject(new Error('network went away'));
    }
    return realFetch(url, opts);
  };
  h.run("openNewTab('big','')");
  await h.flush();
  eq(renderCalls, 1, 'the prefetch was attempted and failed');

  h.run('renderPageBlock(0)');
  await h.flush();
  eq(renderCalls, 2, 'the render falls back to its own request');
  const rendered = h.run('_pageBlocks.get(allPages[0].id).rendered');
  assert(rendered, 'and the page renders anyway');
  h.dispose();
});

test('a prefetch does not survive into the next document', async () => {
  const h = loadViewer({
    a: { pages: makePages('a', 3, 'a.pdf') },
    b: { pages: makePages('b', 3, 'b.pdf') },
  });
  h.ctx.showToast = () => {};
  h.run("openNewTab('a','')");
  await h.flush();
  eq(h.run('_renderPrefetch.size'), 1, 'document A prefetched its opening page');

  h.run('_resetViewerState()');
  eq(h.run('_renderPrefetch.size'), 0, 'switching documents drops it');
  h.dispose();
});

// ── run ──────────────────────────────────────────────────────────────────────
let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failed++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}
console.log(failed ? `${failed} failing` : `${tests.length} passing`);
process.exit(failed ? 1 : 0);
