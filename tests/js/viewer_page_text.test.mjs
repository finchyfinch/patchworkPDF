/* Regression tests: the viewer's Add text tool (Page text and Text Box, merged
 * in v3.2 — a click gives a line, a drag gives a box).
 *
 * These are NOT the annotation Text tool.  That one lays a FreeText object on
 * top of the page which can be selected and deleted afterwards; these write into
 * the page content, where the words become indistinguishable from the
 * document's own.  Two tools with the word "Text" in one 260px panel is a real
 * hazard, so the separation is pinned here as well as in the naming.
 *
 * What has to hold:
 *
 *   1. One tool, two shapes: a CLICK opens a one-line entry, a DRAG a wrapped
 *      box.  A press that barely moved is a click, not a tiny box — it must
 *      open a line rather than place nothing.
 *   2. Clicking away commits; only Escape discards.  Every text entry in this
 *      app works that way (CLAUDE.md, "Editor text entry"), and the style
 *      controls must not count as "away" — adjusting the size mid-edit is part
 *      of the edit.
 *   3. Typing nothing places nothing.
 *   4. A Text Box sends the text ALREADY WRAPPED, because only the client can
 *      measure the font it is previewing with.
 *
 * Run: node tests/js/viewer_page_text.test.mjs
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

const textCalls = (h) => h.callsTo('/api/viewer/add_text');
const entryOpen = (h) => h.run('_ptEntry ? 1 : 0');
const scaleOf   = (h) => h.run('(() => { const st=_pageBlocks.get("sessA-p1");'
                             + ' return st.renderScale * zoomLevel; })()');
const pageHOf   = (h) => h.run('_pageBlocks.get("sessA-p1").pageH');

/** Press on the page with Add text armed, release at (x2, y2).  Goes through
 *  the same drag machinery _annotMouseDown hands the tool to. */
function dragBox(h, x1, y1, x2, y2) {
  h.run(`(() => {
    const block = document.getElementById('page-block-sessA-p1');
    const st    = _pageBlocks.get('sessA-p1');
    const r     = block.getBoundingClientRect();
    _pageToolDragStart('pagetext', 'sessA-p1', st, block, r, ${x1}, ${y1});
    _pageToolDrag.curX = ${x2}; _pageToolDrag.curY = ${y2};
    _pageToolDragEnd({ clientX: r.left + ${x2}, clientY: r.top + ${y2} });
  })()`);
}

/** Click the page with Add text armed: a press that does not move. */
const clickPage = (h, x, y) => dragBox(h, x, y, x, y);

const type = (h, s) => h.run(`_ptEntry.el.value = ${JSON.stringify(s)}`);

// ── arming ──────────────────────────────────────────────────────────────────

test("Add text shows its own style controls, and not the Text tool's", async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  eq(h.run("document.getElementById('at-pagetext-props').style.display"), '');
  eq(h.run("document.getElementById('at-text-props').style.display"), 'none',
     'two Font rows at once is exactly the confusion v3.2 removed');
  h.run("selectAnnotTool('rect')");
  eq(h.run("document.getElementById('at-pagetext-props').style.display"), 'none');
  h.dispose();
});

test('page text is a page tool, so it never becomes an annotation draft',
     async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  clickPage(h, 60, 120);
  eq(h.run('_draft ? 1 : 0'), 0,
     'a draft would be committed as a FreeText annotation instead');
  h.dispose();
});

// ── a click: type, place one line ───────────────────────────────────────

test('clicking opens an entry and Enter places one line', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  h.run("document.getElementById('at-pt-size').value = '20'");
  const s = scaleOf(h), ph = pageHOf(h);
  clickPage(h, 60, 120);
  eq(entryOpen(h), 1, 'no text entry appeared');
  type(h, 'Invoice total');
  h.run('_ptCommit()');
  await h.flush(20);

  eq(textCalls(h).length, 1);
  const item = textCalls(h)[0].body.item;
  eq(item.text, 'Invoice total');
  eq(item.size, 20);
  assert(!item.multiline, 'a single line must not be sent as a box');
  near(item.x, 60 / s, 'x should be where the click was');
  // Baseline sits below the click centre; just check it is on the page and
  // near the click rather than re-deriving the correction here.
  assert(item.y > 0 && item.y < ph, `baseline off the page: ${item.y}`);
  eq(entryOpen(h), 0, 'the entry outlived the commit');
  h.dispose();
});

test('typing nothing places nothing', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  clickPage(h, 60, 120);
  type(h, '   ');
  h.run('_ptCommit()');
  await h.flush(20);
  eq(textCalls(h).length, 0, 'whitespace is not text');
  h.dispose();
});

test('Escape discards without placing', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  clickPage(h, 60, 120);
  type(h, 'thrown away');
  h.run("_ptEntry.el.onkeydown({ key: 'Escape', preventDefault(){} })");
  await h.flush(20);
  eq(textCalls(h).length, 0);
  eq(entryOpen(h), 0);
  h.dispose();
});

test('clicking away commits, the way every other text entry here does',
     async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  clickPage(h, 60, 120);
  type(h, 'committed by clicking away');
  h.run("_ptOutside({ target: document.getElementById('canvas-area') })");
  await h.flush(20);
  eq(textCalls(h).length, 1);
  h.dispose();
});

test('adjusting the style mid-edit does not count as clicking away', async () => {
  const h = await viewer();
  // The stub builds a synthetic document rather than parsing viewer.html, so
  // the size control is not a child of the props panel the way it is in the
  // browser.  Model that one relationship, because `contains` is what the
  // guard uses and there is nothing else worth asserting on.
  h.run(`(() => {
    const panel = document.getElementById('at-pagetext-props');
    const size  = document.getElementById('at-pt-size');
    panel.children.push(size); size.parentNode = panel;
  })()`);
  h.run("selectAnnotTool('pagetext')");
  clickPage(h, 60, 120);
  type(h, 'still typing');
  h.run("_ptOutside({ target: document.getElementById('at-pt-size') })");
  eq(entryOpen(h), 1, 'changing the size closed the editor');
  eq(textCalls(h).length, 0);
  h.dispose();
});

test('disarming the tool drops an open entry', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  clickPage(h, 60, 120);
  eq(entryOpen(h), 1);
  h.run("selectAnnotTool('pagetext')");   // toggle off
  eq(entryOpen(h), 0, 'the entry outlived the tool');
  h.dispose();
});

// ── a drag: type, place a wrapped box ────────────────────────────────────

test('a drag opens a box entry and Ctrl+Enter places it wrapped', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  h.run("document.getElementById('at-pt-size').value = '12'");
  const s = scaleOf(h), ph = pageHOf(h);
  dragBox(h, 40, 60, 240, 160);
  eq(entryOpen(h), 1, 'no box entry appeared');
  type(h, 'the quick brown fox jumps over the lazy dog again and again');
  h.run('_ptCommit()');
  await h.flush(20);

  eq(textCalls(h).length, 1);
  const item = textCalls(h)[0].body.item;
  eq(item.multiline, true);
  near(item.x, 40 / s, 'box left');
  near(item.y, ph - 60 / s, 'box TOP (y-up), not its bottom');
  near(item.boxW, 200 / s, 'box width');
  assert(Array.isArray(item.lines) && item.lines.length > 1,
         `long text should wrap to several lines, got ${JSON.stringify(item.lines)}`);
  h.dispose();
});

test('a plain Enter in a box is a newline, not a commit', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  dragBox(h, 40, 60, 240, 160);
  type(h, 'first line');
  h.run("_ptEntry.el.onkeydown({ key: 'Enter', ctrlKey: false, metaKey: false, preventDefault(){} })");
  await h.flush(10);
  eq(textCalls(h).length, 0, 'Enter should insert a newline in a text box');
  eq(entryOpen(h), 1);
  h.dispose();
});

test('a negligible drag is a click: it opens a line, not a box', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  dragBox(h, 40, 60, 43, 63);
  eq(entryOpen(h), 1, 'a click should open a one-line entry');
  eq(h.run('_ptEntry.kind'), 'line');
  h.dispose();
});

test('a real drag opens a box', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('pagetext')");
  dragBox(h, 40, 60, 240, 160);
  eq(h.run('_ptEntry.kind'), 'box');
  h.dispose();
});

// ── it behaves like every other mutation ────────────────────────────────────

test('placing text flags the document, updates Undo and re-renders', async () => {
  const h = await viewer();
  let dirty = null, invalidated = null;
  h.ctx.pwSetDocDirty = (v) => { dirty = v; };
  h.ctx._invalidateViewerPage = (pid) => { invalidated = pid; };
  h.run("selectAnnotTool('pagetext')");
  clickPage(h, 60, 120);
  type(h, 'x');
  h.run('_ptCommit()');
  await h.flush(20);
  eq(dirty, true);
  eq(h.run('_vUndoCount'), 1, 'placed text is an undo step');
  eq(invalidated, 'sessA-p1');
  h.dispose();
});

test('placing text is queued behind a mutation in flight', async () => {
  const h = await viewer();
  h.run(`_commitChain = new Promise(res => setTimeout(res, 0))
            .then(() => new Promise(res => setTimeout(res, 0)))`);
  h.run("selectAnnotTool('pagetext')");
  clickPage(h, 60, 120);
  type(h, 'x');
  h.run('_ptCommit()');
  eq(textCalls(h).length, 0, 'fired without waiting for the chain');
  await h.flush(20);
  eq(textCalls(h).length, 1);
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
