/* Regression tests: "Watermark every page" — the File-menu command (W5).
 *
 * A document-level command, not a panel tool, and the client half has three
 * jobs that the per-page tool does not:
 *
 *   1. CONFIRM before running.  It rewrites every page and cannot be undone,
 *      and neither fact is guessable from a menu item.
 *   2. Send ONE request.  The shipped editor path looped over the pages calling
 *      an all-pages route once per page, stacking N watermarks on every page.
 *      The whole point of this route is that the loop lives on the server.
 *   3. Refresh what the user can see: every page's cached render is now stale,
 *      and so is every thumbnail.
 *
 * Run: node tests/js/viewer_watermark_all.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || 'not equal'}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}

async function viewer(nPages = 2, confirmAnswer = true) {
  const h = loadViewer({ sessA: { pages: makePages('sessA', nPages, 'doc.pdf') } });
  h.ctx.showToast = () => {};
  h.confirms = [];
  h.ctx.showConfirm = (msg, title, ok) => {
    h.confirms.push({ msg, title, ok });
    return Promise.resolve(confirmAnswer);
  };
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, nPages - 1);
  // The stub never reads a control's value attribute, so set the text the panel
  // ships with (verified as 'DRAFT' in the real app).
  h.run("document.getElementById('at-wm-text').value = 'DRAFT'");
  return h;
}

const allCalls = (h) => h.callsTo('/api/viewer/watermark_all');
const oneCalls = (h) => h.callsTo('/api/viewer/watermark')
                         .filter(c => !c.url.startsWith('/api/viewer/watermark_all'));

// ── the confirmation ────────────────────────────────────────────────────────

test('it confirms before touching anything', async () => {
  const h = await viewer(3);
  await h.run('wmViewerApplyAll()');
  await h.flush(20);
  eq(h.confirms.length, 1, 'no confirmation was shown');
  eq(allCalls(h).length, 1);
  h.dispose();
});

test('declining the confirmation sends nothing', async () => {
  const h = await viewer(3, false);
  await h.run('wmViewerApplyAll()');
  await h.flush(20);
  eq(h.confirms.length, 1);
  eq(allCalls(h).length, 0, 'it ran despite the user declining');
  h.dispose();
});

test('the confirmation names the page count and the text', async () => {
  const h = await viewer(7);
  // window.t is identity in the harness, so assert on the params the message is
  // built from rather than the rendered English.  Key them: the success toast
  // is also interpolated, and it runs last.
  const byKey = {};
  h.ctx.window.t = (k, p) => { if (p) byKey[k] = p; return k; };
  await h.run('wmViewerApplyAll()');
  await h.flush(20);
  const confirmParams = byKey['confirm.watermark_all_msg'];
  assert(confirmParams, `no interpolated confirm message: ${JSON.stringify(byKey)}`);
  eq(confirmParams.count, 7, 'the user must be told how many pages');
  eq(confirmParams.text, 'DRAFT', 'and what is about to be stamped');
  h.dispose();
});

// ── one request, not one per page ───────────────────────────────────────────

test('it sends exactly one request however many pages there are', async () => {
  const h = await viewer(9);
  await h.run('wmViewerApplyAll()');
  await h.flush(20);
  eq(allCalls(h).length, 1,
     'one request per page is what stacked N watermarks on every page');
  eq(oneCalls(h).length, 0, 'it must not go through the per-page route');
  const body = allCalls(h)[0].body;
  assert(!('page_id' in body), 'an all-pages command has no page');
  eq(body.watermark.text, 'DRAFT');
  h.dispose();
});

// ── refreshing what the user sees ───────────────────────────────────────────

test('every changed page is re-rendered and re-thumbnailed', async () => {
  const h = await viewer(2);
  const invalidated = [];
  h.ctx._invalidateViewerPage = (pid) => invalidated.push(pid);
  await h.run('wmViewerApplyAll()');
  await h.flush(20);
  eq(invalidated.join(','), 'sessA-p1,sessA-p2',
     'a stale raster shows a page without the watermark it now has');
  eq(h.run("allPages.map(p => p.thumbnail).join(',')"), 'x,y',
     'the page rail still shows the old thumbnails');
  h.dispose();
});

test('it flags the document and reports the cleared history', async () => {
  const h = await viewer(2);
  let dirty = null;
  h.ctx.pwSetDocDirty = (v) => { dirty = v; };
  // Pretend an earlier action left an undo entry.
  h.run('_vUndoCount = 3; _vUpdateUndoButtons();');
  await h.run('wmViewerApplyAll()');
  await h.flush(20);
  eq(dirty, true);
  eq(h.run('_vUndoCount'), 0,
     'the server clears the history; the toolbar has to follow, or Undo looks '
     + 'available and does something the user will not expect');
  h.dispose();
});

test('it clears the live preview', async () => {
  const h = await viewer(2);
  h.run("selectAnnotTool('watermark')");
  h.run("document.getElementById('at-wm-text').value = 'DRAFT'");
  h.run('wmViewerPreview()');
  eq(h.run('_wmPreviewEl ? 1 : 0'), 1, 'precondition');
  await h.run('wmViewerApplyAll()');
  await h.flush(20);
  eq(h.run('_wmPreviewEl ? 1 : 0'), 0,
     'the preview on top of a stamped page doubles the watermark');
  h.dispose();
});

// ── guards ──────────────────────────────────────────────────────────────────

test('empty text asks nothing and sends nothing', async () => {
  const h = await viewer(2);
  h.run("document.getElementById('at-wm-text').value = '   '");
  await h.run('wmViewerApplyAll()');
  await h.flush(20);
  eq(h.confirms.length, 0, 'it should not ask about a watermark it cannot make');
  eq(allCalls(h).length, 0);
  h.dispose();
});

test('the File menu routes to it', async () => {
  const h = await viewer(2);
  let called = false;
  h.ctx.wmViewerApplyAll = () => { called = true; };
  h.run("fileMenuAction('watermark-all')");
  eq(called, true, 'the menu item is wired to nothing');
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
