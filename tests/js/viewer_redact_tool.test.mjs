/* Regression tests: the viewer's Redact tool (W3 of the editor→viewer merge).
 *
 * Redaction is the first PAGE tool that rewrites content, and it is the one
 * where getting the client wrong is a security problem rather than a cosmetic
 * one — a box that lands elsewhere covers nothing while the app says it worked.
 * So the things pinned here are narrow and specific:
 *
 *   1. It posts the DISPLAY frame, untranslated, exactly as drawn.  The server
 *      adds the CropBox origin (CLAUDE.md, "Viewer page coordinates").
 *   2. It goes through _queueAnnotOp, so it cannot overtake a mutation still in
 *      flight and have the server record the undo steps in the wrong order.
 *   3. It refreshes the toolbar's Undo/Redo from the reply, because a redaction
 *      IS an undo step — the viewer's history holds page bytes, not annotation
 *      objects, which is the observation the whole of Phase 1 rests on.
 *   4. It invalidates the page's cached render.  The page content changed, so
 *      the raster the viewer is holding is stale; leaving it would show the
 *      user un-redacted content that is no longer in the document.
 *
 * Run: node tests/js/viewer_redact_tool.test.mjs
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

async function viewer() {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 1, 'doc.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 0);
  return h;
}

function drag(h, x1, y1, x2, y2) {
  h.run(`(() => {
    const block = document.getElementById('page-block-sessA-p1');
    const st    = _pageBlocks.get('sessA-p1');
    const r     = block.getBoundingClientRect();
    _pageToolDragStart('redact', 'sessA-p1', st, block, r, ${x1}, ${y1});
    _pageToolDrag.curX = ${x2}; _pageToolDrag.curY = ${y2};
    _pageToolDragEnd({ clientX: r.left + ${x2}, clientY: r.top + ${y2} });
  })()`);
}

const redactCalls = (h) => h.callsTo('/api/viewer/redact');

// ── arming ──────────────────────────────────────────────────────────────────

test('the Redact tool arms, shows its warning, and disarms', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('redact')");
  eq(h.run('_activeAnnotTool'), 'redact', 'tool did not arm');
  eq(h.run("document.getElementById('at-redact-props').style.display"), '',
     'the permanence warning must be visible while the tool is armed');

  h.run("selectAnnotTool('redact')");
  eq(h.run('_activeAnnotTool'), null, 'tool did not disarm');
  eq(h.run("document.getElementById('at-redact-props').style.display"), 'none');
  h.dispose();
});

test('arming Redact hides the Field controls and vice versa', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('formfield')");
  h.run("selectAnnotTool('redact')");
  eq(h.run("document.getElementById('at-ff-props').style.display"), 'none');
  h.run("selectAnnotTool('formfield')");
  eq(h.run("document.getElementById('at-redact-props').style.display"), 'none');
  h.dispose();
});

test('Escape disarms the tool', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('redact')");
  h.run("_annotEscKey({ key: 'Escape' })");
  eq(h.run('_activeAnnotTool'), null);
  h.dispose();
});

// ── the request ─────────────────────────────────────────────────────────────

test('a drag posts the drawn rect in display-frame points', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('redact')");
  const scale = h.run('(() => { const st=_pageBlocks.get("sessA-p1");'
                    + ' return st.renderScale * zoomLevel; })()');
  const pageH = h.run('_pageBlocks.get("sessA-p1").pageH');
  drag(h, 40, 60, 200, 110);
  await h.flush(20);

  const calls = redactCalls(h);
  eq(calls.length, 1, 'expected exactly one redact POST');
  const r = calls[0].body.rect;
  near(r.x,  40 / scale,  'x');
  near(r.x2, 200 / scale, 'x2');
  near(r.y,  pageH - 110 / scale, 'y');
  near(r.y2, pageH - 60 / scale,  'y2');
  assert(r.x < r.x2 && r.y < r.y2, 'rect should be normalised');
  h.dispose();
});

test('a backwards drag is normalised before it is sent', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('redact')");
  drag(h, 200, 110, 40, 60);
  await h.flush(20);
  const r = redactCalls(h)[0].body.rect;
  assert(r.x < r.x2, `x ${r.x} should be < x2 ${r.x2}`);
  assert(r.y < r.y2, `y ${r.y} should be < y2 ${r.y2}`);
  h.dispose();
});

test('a press that does not move redacts nothing', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('redact')");
  drag(h, 50, 50, 52, 52);
  await h.flush(20);
  eq(redactCalls(h).length, 0,
     'a stray click must never destroy content');
  h.dispose();
});

test('the black preview is removed once the drag ends', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('redact')");
  drag(h, 40, 60, 200, 110);
  await h.flush(20);
  eq(h.run("document.querySelectorAll('.rx-draw-box').length"), 0,
     'the preview box leaked into the page');
  h.dispose();
});

// ── it behaves like every other mutation ────────────────────────────────────

test('a redaction flags the document as unsaved', async () => {
  const h = await viewer();
  let dirty = null;
  h.ctx.pwSetDocDirty = (v) => { dirty = v; };
  h.run("selectAnnotTool('redact')");
  drag(h, 40, 60, 200, 110);
  await h.flush(20);
  eq(dirty, true);
  h.dispose();
});

test('a redaction updates the Undo button from the reply', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('redact')");
  drag(h, 40, 60, 200, 110);
  await h.flush(20);
  eq(h.run('_vUndoCount'), 1,
     'a redaction is an undo step — the toolbar has to know');
  eq(h.run("document.getElementById('v-btn-undo').disabled"), false,
     'Undo should be enabled after a redaction');
  h.dispose();
});

test('a redaction invalidates the page render', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('redact')");
  eq(h.run("_pageBlocks.get('sessA-p1').rendered"), true, 'precondition');
  let invalidated = null;
  h.ctx._invalidateViewerPage = (pid) => { invalidated = pid; };
  drag(h, 40, 60, 200, 110);
  await h.flush(20);
  eq(invalidated, 'sessA-p1',
     'the cached raster still shows content that is no longer in the document');
  h.dispose();
});

test('redaction is queued, so it cannot overtake a mutation in flight',
     async () => {
  const h = await viewer();
  h.run(`_commitChain = new Promise(res => setTimeout(res, 0))
            .then(() => new Promise(res => setTimeout(res, 0)))`);
  h.run("selectAnnotTool('redact')");
  drag(h, 40, 60, 200, 110);
  eq(redactCalls(h).length, 0, 'the POST was fired without waiting for the chain');
  await h.flush(20);
  eq(redactCalls(h).length, 1, 'the POST never ran after the chain drained');
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
