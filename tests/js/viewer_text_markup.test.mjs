/* Text markup from a text selection (v3.5): the pop-up, what it sends, and
 * how a click on existing markup selects it instead of lifting it.
 *
 *   * a selection becomes one quad per line, split at column gaps (a two-column
 *     selection must not paint the gutter);
 *   * a kind button sends ONE add_annotation with those quads, the words and
 *     the reviewer's name, through the annotation queue;
 *   * a click on a highlight selects it (pop-up in edit mode); colour / kind go
 *     to restyle_text_markup, Delete to delete_annotation — never a lift;
 *   * a press on a highlight that turns into a DRAG is a text selection, so
 *     already-highlighted words can still be selected and copied;
 *   * a read-only document offers no pop-up.
 *
 * Run: node tests/js/viewer_text_markup.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(c, m) { if (!c) throw new Error(m || 'assertion failed'); }
function eq(a, b, m) {
  const A = JSON.stringify(a), B = JSON.stringify(b);
  if (A !== B) throw new Error(`${m || 'differs'}: expected ${B}, got ${A}`);
}

async function open() {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 2, 'a.pdf') } });
  h.ctx.showToast = () => {};
  const pop = h.doc.createElement('div');
  pop.id = 'tm-popup';                          // registers it with the stub DOM
  h.pop = pop;
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 1);
  h.run("_reviewerName = 'GF'");
  return h;
}

// chars in the display frame, PDF points, y up: two lines; the first has a
// wide gap (a column gutter) after "ab".
const ch = (c, x, y) => ({ c, x, y, w: 6, h: 10 });
const LINE1 = [ch('a', 72, 700), ch('b', 78, 700), ch('c', 200, 700), ch('d', 206, 700)];
const LINE2 = [ch('e', 72, 686), ch('f', 78, 686)];

function selectText(h, chars) {
  h.ctx._testChars = chars;
  h.run("_selectedChars = _testChars; _selectedPageId = 'sessA-p1';" +
        "_tmShowForSelection('sessA-p1', _groupCharsIntoLines(_testChars))");
}

test('a selection becomes one quad per line, split at the column gap', async () => {
  const h = await open();
  h.ctx._testChars = [...LINE1, ...LINE2];
  const quads = h.run('_tmQuadsFromLines(_groupCharsIntoLines(_testChars))');
  eq(quads, [[72, 700, 84, 710], [200, 700, 212, 710], [72, 686, 84, 696]]);
  h.dispose();
});

test('Underline sends one add_annotation with the quads, words and reviewer', async () => {
  const h = await open();
  selectText(h, [...LINE2]);
  assert(h.pop.classList.contains('show'), 'pop-up shown over the selection');
  assert(!h.pop.classList.contains('editing'), 'selection mode: no colours/delete');
  h.run("_tmKind('Underline')");
  await h.flush();
  const calls = h.callsTo('/api/viewer/add_annotation');
  eq(calls.length, 1, 'one annotation');
  const b = calls[0].body;
  eq(b.subtype, 'Underline');
  eq(b.quads, [[72, 686, 84, 696]]);
  eq(b.text, 'ef');
  eq(b.author, 'GF');
  eq(b.page_id, 'sessA-p1');
  assert(!h.pop.classList.contains('show'), 'pop-up put away');
  eq(h.run('_selectedChars.length'), 0, 'selection cleared');
  h.dispose();
});

const MARKUP = { annot_idx: 3, tool: 'textmarkup', markup_kind: 'Highlight',
                 quads: [[72, 700, 212, 710]], border_color: [1, 0.92, 0] };

test('a selected highlight is recoloured / retyped in place, never lifted', async () => {
  const h = await open();
  h.ctx._m = MARKUP;
  h.run("_tmShowForMarkup(_m, 'sessA-p1')");
  assert(h.pop.classList.contains('editing'), 'edit mode: colours and delete');
  h.run('_tmColor([0.3, 0.85, 0.35])');
  h.run("_tmKind('Highlight')");                // same kind: nothing to do
  h.run("_tmKind('StrikeOut')");
  await h.flush();
  const rs = h.callsTo('/api/viewer/restyle_text_markup').map(c => c.body);
  eq(rs.length, 2, 'colour + kind, and no call for the unchanged kind');
  eq([rs[0].annot_idx, rs[0].color], [3, [0.3, 0.85, 0.35]]);
  eq([rs[1].annot_idx, rs[1].kind], [3, 'StrikeOut']);
  eq(h.callsTo('/api/viewer/delete_annotation').length, 0, 'no lift (delete + re-add)');
  eq(h.callsTo('/api/viewer/add_annotation').length, 0);
  h.dispose();
});

test('Delete removes the selected markup', async () => {
  const h = await open();
  h.ctx._m = MARKUP;
  h.run("_tmShowForMarkup(_m, 'sessA-p1')");
  h.run('_tmDelete()');
  await h.flush();
  const d = h.callsTo('/api/viewer/delete_annotation');
  eq(d.length, 1);
  eq(d[0].body.annot_idx, 3);
  assert(!h.pop.classList.contains('show'));
  h.dispose();
});

test('a click on markup selects it; a drag from it selects text instead', async () => {
  const h = await open();
  h.ctx._m = MARKUP;
  // Click: press on the hit-area, release without moving.
  h.run("_tmPending = { info: _m, page_id: 'sessA-p1' }; _dragPageId = 'sessA-p1';" +
        "_vDrag = { tool: 'pointer', startX: 100, startY: 100, currX: 101, currY: 101 }");
  await h.run('_onViewerMouseUp({})');
  eq(h.run("_tmSel && _tmSel.mode"), 'markup', 'the click selected the highlight');
  h.run('_tmHide()');
  // Drag: same press, released 80px away — that is a marquee, not a select.
  h.run("_tmPending = { info: _m, page_id: 'sessA-p1' }; _dragPageId = 'sessA-p1';" +
        "_vDrag = { tool: 'pointer', startX: 100, startY: 100, currX: 180, currY: 160 }");
  await h.run('_onViewerMouseUp({})');
  await h.flush();
  assert(h.run('_tmSel === null || _tmSel.mode !== "markup"'), 'the drag did not select the highlight');
  eq(h.run('_tmPending'), null, 'the press is forgotten');
  h.dispose();
});

test('a read-only document offers no pop-up', async () => {
  const h = await open();
  h.run('IS_RESTRICTED = true');
  selectText(h, [...LINE2]);
  assert(!h.pop.classList.contains('show'), 'no pop-up');
  eq(h.run('_tmSel'), null);
  h.dispose();
});

test('zooming or changing tab puts the pop-up away', async () => {
  const h = await open();
  selectText(h, [...LINE2]);
  h.run('zoomIn()');
  assert(!h.pop.classList.contains('show'), 'zoom hid it');
  selectText(h, [...LINE2]);
  h.run('_resetViewerState()');
  assert(!h.pop.classList.contains('show'), 'tab reset hid it');
  h.dispose();
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failed++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}
console.log(failed ? `${failed} failing` : `${tests.length} passing`);
process.exit(failed ? 1 : 0);
