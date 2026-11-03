/* Regression tests: the viewer's Field tool (W2 of the editor→viewer merge).
 *
 * A form field is NOT an annotation.  It is a real widget in the PDF, so it does
 * not go through the draft/commitDraft machinery that every drawing tool uses —
 * it runs its own drag and posts to its own endpoint.  Three things about that
 * have to hold, and each has a matching failure mode elsewhere in this codebase:
 *
 *   1. It posts the DISPLAY frame, untranslated.  The server adds the page's
 *      CropBox origin.  A client that "helpfully" converted to PDF user space
 *      would put two coordinate conventions on the wire, which is precisely what
 *      displaced every annotation on a cropped page (CLAUDE.md, "Viewer page
 *      coordinates").
 *   2. It goes through _queueAnnotOp.  The server records one undo step per
 *      mutation in ARRIVAL order, so an unqueued POST overtakes one still in
 *      flight (see viewer_annot_order.test.mjs).
 *   3. A press that does not move places nothing.  The editor's ffDrag has the
 *      same rule — a click is not a placement, and charging one a round trip
 *      also turns a saved document unsaved for nothing.
 *
 * Run: node tests/js/viewer_field_tool.test.mjs
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

/** Drag on the page from (x1,y1) to (x2,y2) in block-local CSS px. */
function drag(h, x1, y1, x2, y2, tool = 'formfield') {
  // The block's bounding rect is the origin the handlers measure against.
  h.run(`(() => {
    const block = document.getElementById('page-block-sessA-p1');
    const st    = _pageBlocks.get('sessA-p1');
    const r     = block.getBoundingClientRect();
    _pageToolDragStart('${tool}', 'sessA-p1', st, block, r, ${x1}, ${y1});
    _pageToolDrag.curX = ${x2}; _pageToolDrag.curY = ${y2};
    _pageToolDragEnd({ clientX: r.left + ${x2}, clientY: r.top + ${y2} });
  })()`);
}

const addCalls = (h) => h.callsTo('/api/viewer/add_form_field');

// ── the tool is registered the same way every other tool is ─────────────────

test('the Field tool arms and disarms like an annotation tool', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('formfield')");
  eq(h.run('_activeAnnotTool'), 'formfield', 'tool did not arm');
  eq(h.run("document.getElementById('at-ff-props').style.display"), '',
     'the field-type control should be revealed while armed');

  h.run("selectAnnotTool('formfield')");           // second click = toggle off
  eq(h.run('_activeAnnotTool'), null, 'tool did not disarm');
  eq(h.run("document.getElementById('at-ff-props').style.display"), 'none',
     'the field-type control should be hidden again');
  h.dispose();
});

test('arming a drawing tool hides the field-type control', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('formfield')");
  h.run("selectAnnotTool('rect')");
  eq(h.run("document.getElementById('at-ff-props').style.display"), 'none');
  h.dispose();
});

// ── placement ───────────────────────────────────────────────────────────────

test('a drag places one field, in display-frame coordinates', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('formfield')");
  // Page is 612x792pt; the harness renders at a known scale.
  const scale = h.run('(() => { const st=_pageBlocks.get("sessA-p1");'
                    + ' return st.renderScale * zoomLevel; })()');
  const pageH = h.run('_pageBlocks.get("sessA-p1").pageH');
  drag(h, 20, 30, 120, 70);
  await h.flush(20);

  const calls = addCalls(h);
  eq(calls.length, 1, 'expected exactly one placement POST');
  const f = calls[0].body.field;
  eq(f.type, 'text', 'the armed field type should be sent');
  // Display px → display-frame points, y flipped about the page height.
  near(f.x1, 20 / scale,  'x1');
  near(f.x2, 120 / scale, 'x2');
  near(f.y1, pageH - 70 / scale, 'y1');
  near(f.y2, pageH - 30 / scale, 'y2');
  // The CropBox origin is the SERVER's job — nothing here may pre-apply it.
  assert(f.x1 < f.x2 && f.y1 < f.y2, 'rect should be normalised');
  h.dispose();
});

test('the chosen field type is the one sent', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('formfield')");
  h.run("document.getElementById('at-ff-type').value = 'checkbox'");
  drag(h, 10, 10, 60, 60);
  await h.flush(20);
  eq(addCalls(h)[0].body.field.type, 'checkbox');
  h.dispose();
});

test('a drag backwards still sends a normalised rect', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('formfield')");
  drag(h, 120, 70, 20, 30);           // bottom-right to top-left
  await h.flush(20);
  const f = addCalls(h)[0].body.field;
  assert(f.x1 < f.x2, `x1 ${f.x1} should be < x2 ${f.x2}`);
  assert(f.y1 < f.y2, `y1 ${f.y1} should be < y2 ${f.y2}`);
  h.dispose();
});

test('a press that does not move places nothing', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('formfield')");
  drag(h, 40, 40, 41, 41);            // inside the slop
  await h.flush(20);
  eq(addCalls(h).length, 0,
     'a click is not a placement — it must not spend a round trip, '
     + 'and must not turn a saved document unsaved');
  h.dispose();
});

test('the drag outline is removed once the drag ends', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('formfield')");
  drag(h, 20, 30, 120, 70);
  await h.flush(20);
  eq(h.run("document.querySelectorAll('.ff-draw-box').length"), 0,
     'the live outline leaked into the page');
  h.dispose();
});

// ── it behaves like every other mutation ────────────────────────────────────

test('placing a field flags the document as unsaved', async () => {
  const h = await viewer();
  let dirty = null;
  h.ctx.pwSetDocDirty = (v) => { dirty = v; };
  h.run("selectAnnotTool('formfield')");
  drag(h, 20, 30, 120, 70);
  await h.flush(20);
  eq(dirty, true, 'a placed field must mark the document dirty');
  h.dispose();
});

test('placement is queued, so it cannot overtake a mutation in flight',
     async () => {
  const h = await viewer();
  // Chain a long-running op, then place a field: the field POST must wait.
  h.run(`_commitChain = new Promise(res => setTimeout(res, 0))
            .then(() => new Promise(res => setTimeout(res, 0)))`);
  h.run("selectAnnotTool('formfield')");
  drag(h, 20, 30, 120, 70);
  eq(addCalls(h).length, 0, 'the POST was fired without waiting for the chain');
  await h.flush(20);
  eq(addCalls(h).length, 1, 'the POST never ran after the chain drained');
  h.dispose();
});

test('the echoed field is what gets drawn, not the drawn rect', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('formfield')");
  drag(h, 20, 30, 120, 70);
  await h.flush(20);
  const stored = h.run("JSON.stringify(_allFormFields['sessA-p1'] || [])");
  const fields = JSON.parse(stored);
  eq(fields.length, 1, 'the placed field was not added to the overlay model');
  eq(fields[0].id, 'vff-text',
     'the id must come from the server — it is the one that assigns it');
  h.dispose();
});


// ── editing a placed field: the parity the Editor panel used to provide ─────
//
// Removing the editor's Field tool would have been a REGRESSION without these:
// the editor could select, move, resize and delete a placed field, and until
// W7 the viewer could only place one.

function fieldsOn(h, pid = 'sessA-p1') {
  return JSON.parse(h.run(`JSON.stringify(_allFormFields['${pid}'] || [])`));
}

async function withField(h) {
  h.run("selectAnnotTool('formfield')");
  drag(h, 20, 30, 120, 70);
  await h.flush(20);
  return fieldsOn(h)[0];
}

// The stub's querySelectorAll returns [] on an element, so overlay children are
// found by className instead.
const OV = "document.getElementById('form-ov-sessA-p1')";
const countIn = (h, cls) =>
  h.run(`${OV}.children.filter(c => (c.className||'').split(' ').includes('${cls}')).length`);

/** Press on the Nth overlay node with `cls`, optionally drag, release. */
function press(h, cls, dx = 0, dy = 0, idx = 0) {
  h.run(`(() => {
    const ov = ${OV};
    const el = ov.children.filter(c => (c.className||'').split(' ').includes('${cls}'))[${idx}];
    if (!el) throw new Error('no .${cls} in the overlay');
    el.dispatch('mousedown', { button: 0, clientX: 0, clientY: 0,
                               preventDefault(){}, stopPropagation(){} });
    if (${dx} !== 0 || ${dy} !== 0) _ffDragMove({ clientX: ${dx}, clientY: ${dy} });
    _ffDragEnd();
  })()`);
}

test('arming the Field tool makes the widgets non-fillable and grabbable',
     async () => {
  const h = await viewer();
  await withField(h);
  eq(h.run(`${OV}.className.includes('ff-edit-mode')`), true,
     'the overlay must stop being fillable, or a click types instead of grabs');
  eq(countIn(h, 'ff-hit'), 1, 'no hit-area to grab the field by');

  h.run("selectAnnotTool('formfield')");   // disarm
  await h.flush(10);
  eq(h.run(`document.getElementById('form-ov-sessA-p1').className.includes('ff-edit-mode')`),
     false, 'the field must be fillable again once the tool is put away');
  h.dispose();
});

test('clicking a field selects it and shows four resize handles', async () => {
  const h = await viewer();
  await withField(h);
  press(h, 'ff-hit');            // click, no movement
  await h.flush(10);
  eq(countIn(h, 'ff-handle'), 4,
     'a selected field needs corner handles to resize by');
  eq(h.run("_ffSel && _ffSel.field_id"), 'vff-text');
  h.dispose();
});

test('a click that does not move sends no update', async () => {
  const h = await viewer();
  await withField(h);
  press(h, 'ff-hit');
  await h.flush(20);
  eq(h.callsTo('/api/viewer/update_form_field').length, 0,
     'selecting is not editing — it must not spend a round trip or dirty the doc');
  h.dispose();
});

test('dragging a field moves it and tells the server once', async () => {
  const h = await viewer();
  const f0 = await withField(h);
  const s = h.run('(() => { const st=_pageBlocks.get("sessA-p1");'
                + ' return st.renderScale * zoomLevel; })()');
  press(h, 'ff-hit', 40, 20);    // 40px right, 20px down
  await h.flush(20);

  const calls = h.callsTo('/api/viewer/update_form_field');
  eq(calls.length, 1, 'expected exactly one update');
  const u = calls[0].body.updates;
  near(u.x1, f0.x1 + 40 / s, 'x1 should follow the drag');
  near(u.y1, f0.y1 - 20 / s, 'screen y is down, PDF y is up');
  near(u.x2 - u.x1, f0.x2 - f0.x1, 'a move must not resize');
  h.dispose();
});

test('dragging a corner resizes rather than moves', async () => {
  const h = await viewer();
  const f0 = await withField(h);
  press(h, 'ff-hit');            // select, to get the handles
  await h.flush(10);
  press(h, 'ff-handle', 30, 0);  // handle 0 = top-left
  await h.flush(20);

  const calls = h.callsTo('/api/viewer/update_form_field');
  eq(calls.length, 1);
  const u = calls[0].body.updates;
  const s = h.run('(() => { const st=_pageBlocks.get("sessA-p1");'
                + ' return st.renderScale * zoomLevel; })()');
  near(u.x1, f0.x1 + 30 / s, 'the grabbed edge should move');
  near(u.x2, f0.x2, 'the opposite edge should not');
  h.dispose();
});

test('Del removes the selected field', async () => {
  const h = await viewer();
  await withField(h);
  press(h, 'ff-hit');
  await h.flush(10);
  h.run("_ffKey({ key: 'Delete', target: { tagName: 'DIV' }, preventDefault(){} })");
  await h.flush(20);
  eq(h.callsTo('/api/viewer/delete_form_field').length, 1);
  eq(fieldsOn(h).length, 0, 'the field is still in the overlay model');
  h.dispose();
});

test('Del while typing in a field does not delete it', async () => {
  const h = await viewer();
  await withField(h);
  press(h, 'ff-hit');
  await h.flush(10);
  h.run("_ffKey({ key: 'Delete', target: { tagName: 'INPUT' }, preventDefault(){} })");
  await h.flush(20);
  eq(h.callsTo('/api/viewer/delete_form_field').length, 0,
     'Del inside a text field is an edit, not a command');
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
