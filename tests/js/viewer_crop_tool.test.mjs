/* Regression tests: the viewer's Crop tool (W6 — the last Phase 1 tool).
 *
 * Crop is the only page tool that changes the page's SIZE, and that is all of
 * its extra difficulty on the client.  Everything describing a page's
 * dimensions has to move together:
 *
 *   * `allPages[i].width_pts / height_pts` — the page-list metadata the
 *     opening zoom is fitted to;
 *   * `_pageBlocks.get(id).pageW / pageH` — what every overlay's coordinate
 *     maths is calibrated against;
 *   * `pageW` / `pageH`, when it is the page in view.
 *
 * Miss any one and the page is laid out at the size it used to be: annotations
 * land in the wrong place, or the document opens at the wrong zoom.
 *
 * The same applies in reverse to UNDO, which restores a snapshot that may be a
 * different size from the page currently on screen.
 *
 * Run: node tests/js/viewer_crop_tool.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || 'not equal'}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}
function near(a, b, msg, eps = 0.01) {
  if (Math.abs(a - b) > eps) throw new Error(`${msg || 'not near'}: expected ~${b}, got ${a}`);
}

async function viewer(nPages = 1) {
  const h = loadViewer({ sessA: { pages: makePages('sessA', nPages, 'doc.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, nPages - 1);
  return h;
}

function drag(h, x1, y1, x2, y2) {
  h.run(`(() => {
    const block = document.getElementById('page-block-sessA-p1');
    const st    = _pageBlocks.get('sessA-p1');
    const r     = block.getBoundingClientRect();
    _pageToolDragStart('crop', 'sessA-p1', st, block, r, ${x1}, ${y1});
    _pageToolDrag.curX = ${x2}; _pageToolDrag.curY = ${y2};
    _pageToolDragEnd({ clientX: r.left + ${x2}, clientY: r.top + ${y2} });
  })()`);
}

const cropCalls = (h) => h.callsTo('/api/viewer/crop');
const sizes = (h) => JSON.parse(h.run(`JSON.stringify({
  list:  [allPages[0].width_pts, allPages[0].height_pts],
  block: [_pageBlocks.get('sessA-p1').pageW, _pageBlocks.get('sessA-p1').pageH],
  cur:   [pageW, pageH],
})`));

// ── arming ──────────────────────────────────────────────────────────────────

test('the Crop tool arms, shows its options, and disarms', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('crop')");
  eq(h.run('_activeAnnotTool'), 'crop');
  eq(h.run("document.getElementById('at-crop-props').style.display"), '');
  h.run("selectAnnotTool('crop')");
  eq(h.run('_activeAnnotTool'), null);
  eq(h.run("document.getElementById('at-crop-props').style.display"), 'none');
  h.dispose();
});

test('switching away from Crop hides its options', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('crop')");
  h.run("selectAnnotTool('redact')");
  eq(h.run("document.getElementById('at-crop-props').style.display"), 'none');
  h.dispose();
});

// ── the request ─────────────────────────────────────────────────────────────

test('a drag posts the kept area in display-frame points', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('crop')");
  const scale = h.run('(() => { const st=_pageBlocks.get("sessA-p1");'
                    + ' return st.renderScale * zoomLevel; })()');
  const pageH = h.run('_pageBlocks.get("sessA-p1").pageH');
  drag(h, 30, 40, 250, 300);
  await h.flush(20);

  eq(cropCalls(h).length, 1);
  const r = cropCalls(h)[0].body.rect;
  near(r.x,  30 / scale,  'x');
  near(r.x2, 250 / scale, 'x2');
  near(r.y,  pageH - 300 / scale, 'y');
  near(r.y2, pageH - 40 / scale,  'y2');
  h.dispose();
});

test('a press that does not move crops nothing', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('crop')");
  drag(h, 50, 50, 53, 53);
  await h.flush(20);
  eq(cropCalls(h).length, 0);
  h.dispose();
});

test('the crop outline is removed once the drag ends', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('crop')");
  drag(h, 30, 40, 250, 300);
  await h.flush(20);
  eq(h.run("document.querySelectorAll('.cr-draw-box').length"), 0);
  h.dispose();
});

// ── the size change, which is the point of this tool ────────────────────────

test('a crop updates every record of the page size', async () => {
  const h = await viewer();
  const before = sizes(h);
  eq(before.list[0], 612, 'precondition');
  h.run("selectAnnotTool('crop')");
  drag(h, 30, 40, 250, 300);
  await h.flush(20);

  const after = sizes(h);
  // The stub answers 300x400 whatever was dragged.
  eq(after.list.join(','), '300,400', 'the page-list metadata is stale');
  eq(after.block.join(','), '300,400', 'the block state is stale');
  eq(after.cur.join(','), '300,400', 'the in-view page size is stale');
  h.dispose();
});

test('a crop re-renders the page and refreshes its thumbnail', async () => {
  const h = await viewer();
  let invalidated = null;
  h.ctx._invalidateViewerPage = (pid) => { invalidated = pid; };
  h.run("selectAnnotTool('crop')");
  drag(h, 30, 40, 250, 300);
  await h.flush(20);
  eq(invalidated, 'sessA-p1');
  eq(h.run('allPages[0].thumbnail'), 'c');
  h.dispose();
});

test('a crop flags the document and updates Undo', async () => {
  const h = await viewer();
  let dirty = null;
  h.ctx.pwSetDocDirty = (v) => { dirty = v; };
  h.run("selectAnnotTool('crop')");
  drag(h, 30, 40, 250, 300);
  await h.flush(20);
  eq(dirty, true);
  eq(h.run('_vUndoCount'), 1);
  h.dispose();
});

test('cropping is queued behind a mutation in flight', async () => {
  const h = await viewer();
  h.run(`_commitChain = new Promise(res => setTimeout(res, 0))
            .then(() => new Promise(res => setTimeout(res, 0)))`);
  h.run("selectAnnotTool('crop')");
  drag(h, 30, 40, 250, 300);
  eq(cropCalls(h).length, 0, 'fired without waiting for the chain');
  await h.flush(20);
  eq(cropCalls(h).length, 1);
  h.dispose();
});

// ── clearing ────────────────────────────────────────────────────────────────

test('Clear crop asks the server to restore the full page', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('crop')");
  h.run('clearViewerCrop()');
  await h.flush(20);
  eq(cropCalls(h).length, 1);
  eq(cropCalls(h)[0].body.clear, true);
  eq(cropCalls(h)[0].body.page_id, 'sessA-p1');
  assert(!cropCalls(h)[0].body.rect, 'a clear carries no rect');
  h.dispose();
});

test('Clear crop applies the restored size too', async () => {
  const h = await viewer();
  h.run('clearViewerCrop()');
  await h.flush(20);
  eq(sizes(h).list.join(','), '300,400',
     'the restored size has to reach the page records like any other');
  h.dispose();
});

// ── undo has to carry the size back ─────────────────────────────────────────

test('undo applies the size of the snapshot it restored', async () => {
  const h = await viewer();
  // The undo endpoint reports the measured size of the restored page.
  const realFetch = h.ctx.apiFetch;
  h.ctx.apiFetch = (url, o) => {
    if (url.startsWith('/api/viewer/undo')) {
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => 'application/json' },
        json: async () => ({ page_id: 'sessA-p1', annotations: [], editable: [],
                             thumbnail: 'u', width_pts: 612, height_pts: 792,
                             undo_count: 0, redo_count: 1 }),
      });
    }
    return realFetch(url, o);
  };
  h.run("selectAnnotTool('crop')");
  drag(h, 30, 40, 250, 300);
  await h.flush(20);
  eq(sizes(h).list.join(','), '300,400', 'precondition: the crop applied');

  await h.run('viewerUndo()');
  await h.flush(20);
  const after = sizes(h);
  eq(after.list.join(','), '612,792',
     'undoing a crop left the page laid out at the cropped size');
  eq(after.block.join(','), '612,792');
  h.dispose();
});

// ── runner ──────────────────────────────────────────────────────────────────

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`PASS  ${name}`); }
  catch (e) { failed++; console.log(`FAIL  ${name}\n      ${e.message}`); }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
