/* Regression tests: the viewer's Watermark tool (W4 of the editor→viewer merge).
 *
 * The only page tool with no drag.  It takes no geometry — the server positions
 * the mark from the page's own CropBox centre — so what needs pinning here is
 * different from Redact and Field:
 *
 *   1. The PREVIEW tracks the page you are looking at, because that is also what
 *      Stamp applies to.  If the preview stops following the scroll, the user
 *      judges the settings against one page and stamps another.
 *   2. The preview is never left behind: not on disarm, not on tool switch, and
 *      not after stamping (where leaving it would show the mark twice).
 *   3. It sends exactly the panel's settings, and stamps `currentPageId`.
 *   4. Empty text stamps nothing — the server rejects it, and spending a round
 *      trip to be told so is just a worse error message.
 *
 * Run: node tests/js/viewer_watermark_tool.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || 'not equal'}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}

async function viewer(nPages = 2) {
  const h = loadViewer({ sessA: { pages: makePages('sessA', nPages, 'doc.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, nPages - 1);
  return h;
}

const wmCalls   = (h) => h.callsTo('/api/viewer/watermark');
// The DOM stub initialises every control's `value` to '' and never reads the
// `value="DRAFT"` attribute from the markup, so a test that wants the panel's
// real default has to set it. (A browser gives 'DRAFT'; verified in the app.)
function arm(h, text = 'DRAFT') {
  h.run("selectAnnotTool('watermark')");
  setCtl(h, 'at-wm-text', text);
  h.run('wmViewerPreview()');
}
// The stub's document.querySelectorAll always returns [], so assert on the
// node the viewer tracks instead — which is also what production code removes.
const previews  = (h) => h.run('_wmPreviewEl ? 1 : 0');
const previewOn = (h) => h.run('_wmPreviewEl && _wmPreviewEl.parentNode ? _wmPreviewEl.parentNode.id : null');
const marks     = (h) => h.run('_wmPreviewEl ? _wmPreviewEl.children.length : 0');
const setCtl    = (h, id, v) =>
  h.run(`document.getElementById('${id}').value = ${JSON.stringify(v)}`);

// ── arming and the preview ──────────────────────────────────────────────────

test('arming the tool shows its settings and previews the current page',
     async () => {
  const h = await viewer();
  arm(h);
  eq(h.run('_activeAnnotTool'), 'watermark');
  eq(h.run("document.getElementById('at-wm-props').style.display"), '');
  eq(previews(h), 1, 'expected exactly one preview overlay');
  eq(previewOn(h), 'page-block-sessA-p1',
     'the preview must be on the page the user is viewing');
  h.dispose();
});

test('disarming removes the preview', async () => {
  const h = await viewer();
  arm(h);
  eq(previews(h), 1);
  h.run("selectAnnotTool('watermark')");     // toggle off
  eq(h.run('_activeAnnotTool'), null);
  eq(previews(h), 0, 'the preview outlived the tool');
  h.dispose();
});

test('switching to another tool removes the preview', async () => {
  const h = await viewer();
  arm(h);
  h.run("selectAnnotTool('rect')");
  eq(previews(h), 0);
  eq(h.run("document.getElementById('at-wm-props').style.display"), 'none');
  h.dispose();
});

test('Escape removes the preview', async () => {
  const h = await viewer();
  arm(h);
  h.run("_annotEscKey({ key: 'Escape' })");
  eq(h.run('_activeAnnotTool'), null);
  eq(previews(h), 0);
  h.dispose();
});

test('empty text previews nothing rather than an empty overlay', async () => {
  const h = await viewer();
  arm(h);
  setCtl(h, 'at-wm-text', '   ');
  h.run('wmViewerPreview()');
  eq(previews(h), 0);
  h.dispose();
});

test('tiled layout draws many marks, centred draws one', async () => {
  const h = await viewer();
  arm(h);
  const one = marks(h);
  eq(one, 1, 'centred should be a single mark');
  setCtl(h, 'at-wm-layout', 'tiled');
  h.run('wmViewerPreview()');
  const many = marks(h);
  assert(many > one * 3, `tiled drew ${many}, centred drew ${one}`);
  h.dispose();
});

test('the preview follows the current page as the user scrolls', async () => {
  const h = await viewer(2);
  arm(h);
  eq(previewOn(h), 'page-block-sessA-p1');
  const p1Children = h.run("document.getElementById('page-block-sessA-p1').children.length");

  // Move the current page the way the scroll tracker does.
  h.run(`currentPageId = 'sessA-p2';
         if (_activeAnnotTool === 'watermark') wmViewerPreview();`);
  eq(previews(h), 1, 'the preview vanished instead of moving');
  eq(previewOn(h), 'page-block-sessA-p2',
     'preview and stamp target drifted apart');
  eq(h.run("document.getElementById('page-block-sessA-p1').children.length"),
     p1Children - 1, 'the old preview was left behind on the previous page');
  h.dispose();
});

test('the opacity label tracks the slider', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('watermark')");
  setCtl(h, 'at-wm-opacity', '60');
  h.run('wmViewerOpacityLabel()');
  eq(h.run("document.getElementById('at-wm-opacity-lbl').textContent"), '60%');
  h.dispose();
});

// ── stamping ────────────────────────────────────────────────────────────────

test('Stamp sends the panel settings for the current page', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('watermark')");
  setCtl(h, 'at-wm-text', 'CONFIDENTIAL');
  setCtl(h, 'at-wm-size', '80');
  setCtl(h, 'at-wm-color', '#ff0000');
  setCtl(h, 'at-wm-opacity', '40');
  setCtl(h, 'at-wm-angle', '-45');
  setCtl(h, 'at-wm-layout', 'tiled');
  h.run('wmViewerApply()');
  await h.flush(20);

  const calls = wmCalls(h);
  eq(calls.length, 1);
  eq(calls[0].body.page_id, 'sessA-p1');
  const w = calls[0].body.watermark;
  eq(w.text, 'CONFIDENTIAL');
  eq(w.size, 80);
  eq(w.color, '#ff0000');
  eq(w.opacity, 0.4);
  eq(w.angleDeg, -45);
  eq(w.layout, 'tiled');
  h.dispose();
});

test('Stamp with empty text sends nothing', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('watermark')");
  setCtl(h, 'at-wm-text', '');
  h.run('wmViewerApply()');
  await h.flush(20);
  eq(wmCalls(h).length, 0);
  h.dispose();
});

test('stamping clears the preview so the mark is not shown twice', async () => {
  const h = await viewer();
  arm(h);
  eq(previews(h), 1);
  h.run('wmViewerApply()');
  await h.flush(20);
  eq(previews(h), 0,
     'the live preview on top of the stamped page doubles the watermark');
  h.dispose();
});

test('stamping flags the document, updates Undo and re-renders the page',
     async () => {
  const h = await viewer();
  let dirty = null, invalidated = null;
  h.ctx.pwSetDocDirty = (v) => { dirty = v; };
  h.ctx._invalidateViewerPage = (pid) => { invalidated = pid; };
  arm(h);
  h.run('wmViewerApply()');
  await h.flush(20);
  eq(dirty, true);
  eq(h.run('_vUndoCount'), 1, 'a watermark is an undo step');
  eq(invalidated, 'sessA-p1', 'the cached raster no longer matches the page');
  h.dispose();
});

test('stamping is queued behind a mutation in flight', async () => {
  const h = await viewer();
  h.run(`_commitChain = new Promise(res => setTimeout(res, 0))
            .then(() => new Promise(res => setTimeout(res, 0)))`);
  arm(h);
  h.run('wmViewerApply()');
  eq(wmCalls(h).length, 0, 'fired without waiting for the chain');
  await h.flush(20);
  eq(wmCalls(h).length, 1);
  h.dispose();
});

test('the watermark tool never starts a page-tool drag', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('watermark')");
  // It is a page tool, so _annotMouseDown routes it to _pageToolDragStart —
  // which must no-op, because there is no drag spec and nothing to drag.
  h.run(`(() => {
    const block = document.getElementById('page-block-sessA-p1');
    const st    = _pageBlocks.get('sessA-p1');
    _pageToolDragStart('watermark', 'sessA-p1', st, block,
                       block.getBoundingClientRect(), 10, 10);
  })()`);
  eq(h.run('_pageToolDrag'), null, 'a drag was started for a tool with no drag');
  eq(h.run('_wmPreviewEl ? 1 : 0'), 0, 'a drag should not have made a preview');
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
