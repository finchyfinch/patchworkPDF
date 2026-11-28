/* Line ends, Polygon / Polyline and Pen in the viewer (v3.5).
 *
 *   * Line sends its per-end styles (line_ends) — Arrow / Dbl Arrow are gone
 *     as tools; a lifted old arrow keeps its heads;
 *   * Polygon: each click is a vertex (the click must NOT commit the shape
 *     being built), a double-click or Enter finishes, a click on the first
 *     point closes, Backspace drops the last point, Esc cancels; an open
 *     polyline sends its ends, a closed cloud sends border_style 'cloud';
 *   * Pen: a stroke is written on release, simplified;
 *   * Rectangle in Cloud style is sent as a cloud, never filled;
 *   * a lifted polygon's vertex handle moves one vertex; a pasted polygon or
 *     ink drawing moves all of its points.
 *
 * Run: node tests/js/viewer_shapes_v35.test.mjs
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
  const h = loadViewer({ sessA: { pages: makePages('sessA', 1, 'a.pdf') } });
  h.ctx.showToast = () => {};
  // Controls the tools read (the stub hands out a fresh element per lookup
  // unless one is registered under that id).
  const mk = (id, props) => { const el = h.doc.createElement('input'); el.id = id; Object.assign(el, props); return el; };
  h.c = {
    start: mk('at-line-start', { value: 'none' }), end: mk('at-line-end', { value: 'closed' }),
    closed: mk('at-poly-closed', { checked: true }), hl: mk('at-pen-hl', { checked: false }),
    style: mk('at-line-style', { value: 'solid' }), color: mk('at-line-color', { value: '#ff0000' }),
    weight: mk('at-line-weight', { value: '2' }), fillOn: mk('at-fill-on', { checked: false }),
  };
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 0);
  h.sc = h.run("_pageBlocks.get('sessA-p1').renderScale * zoomLevel");
  h.H  = h.run("_pageBlocks.get('sessA-p1').pageH");
  return h;
}
const sent = (h) => h.callsTo('/api/viewer/add_annotation').map(c => c.body);

/** A real press on the page with the armed tool, at block px (x,y). */
function press(h, x, y, detail = 1) {
  h.ctx._p = { x, y, detail };
  h.run(`(() => {
    const st = _pageBlocks.get('sessA-p1');
    const target = { closest: (sel) => sel === '.page-block' ? st.block : null };
    _annotMouseDown({ button: 0, target, clientX: _p.x, clientY: _p.y, detail: _p.detail,
                      preventDefault() {} });
  })()`);
}
function key(h, k) {
  h.ctx._k = k;
  h.run("document.dispatchEvent && 0");
  h.run(`(() => {
    const ev = { key: _k, preventDefault() {}, stopPropagation() {} };
    // The polygon key handler is registered in the capture phase on document;
    // the stub DOM keeps document listeners on doc._listeners if any — call
    // the same logic directly through its public effects instead.
    if (_polyDraw && _draft && _draft.kind === 'poly') {
      if (_k === 'Enter') _polyFinish();
      else if (_k === 'Escape') { _polyStop(); _removeDraft(); }
      else if (_k === 'Backspace') { if (_draft.verts.length > 1) { _draft.verts.pop(); _draftRender(); } }
    }
  })()`);
}

// ── Line ─────────────────────────────────────────────────────────────────────
test('Line sends its per-end styles', async () => {
  const h = await open();
  h.run(`_draft = { tool: 'line', kind: 'line', page_id: 'sessA-p1', st: _pageBlocks.get('sessA-p1'),
                    scale: ${1}, x1: 10, y1: 10, x2: 100, y2: 10, style: _toolStyle(), svg: null };
         commitDraft()`);
  await h.flush();
  const b = sent(h)[0];
  eq(b.line_ends, ['none', 'closed']);
  eq([b.arrow, b.arrow_start], [true, false], 'old flags kept in step');
  h.dispose();
});

test('a lifted old double arrow keeps both heads', async () => {
  const h = await open();
  h.ctx._i = { annot_idx: 0, tool: 'line', line: [10, 700, 200, 700], line_ends: ['open', 'open'],
               border_color: [1, 0, 0], border_width: 1, replies: [] };
  h.run("_buildLiftedDraft(_i, 'sessA-p1'); commitDraft()");
  await h.flush();
  eq(sent(h)[0].line_ends, ['open', 'open']);
  h.dispose();
});

test('the Arrow and Dbl Arrow tools are gone; Polygon and Pen are tools', async () => {
  const h = await open();
  const ids = h.run('_TOOL_IDS');
  assert(!ids.includes('arrow') && !ids.includes('darrow') && !ids.includes('cloud'), ids.join());
  assert(ids.includes('polygon') && ids.includes('pen'));
  h.dispose();
});

// ── Polygon ──────────────────────────────────────────────────────────────────
test('clicks build a polygon; the double-click finishes and writes it', async () => {
  const h = await open();
  h.run("_armTool('polygon')");
  press(h, 100, 100); press(h, 200, 100); press(h, 200, 200);
  eq(sent(h).length, 0, 'nothing written while building');
  eq(h.run('_draft.verts.length'), 3, 'the clicks did not commit the shape');
  press(h, 200, 200, 2);                              // second click of a dbl-click
  await h.flush();
  const b = sent(h)[0];
  eq(b.subtype, 'Polygon');
  eq(b.vertices.length, 6, 'three vertices, no duplicate from the double-click');
  eq(b.vertices.slice(0, 2), [100 / h.sc, h.H - 100 / h.sc]);
  eq(h.run('_activeAnnotTool'), 'polygon', 'tool stays armed');
  eq(h.run('_polyDraw'), null);
  h.dispose();
});

test('a click on the first point closes it', async () => {
  const h = await open();
  h.run("_armTool('polygon')");
  press(h, 100, 100); press(h, 200, 100); press(h, 150, 180); press(h, 103, 102);
  await h.flush();
  eq(sent(h).length, 1);
  eq(sent(h)[0].vertices.length, 6);
  h.dispose();
});

// v3.5 field report: "closing the shape is very hard".  The first corner's
// marker was a draggable HANDLE, so clicking right on it dragged the corner
// instead of closing — only a click near-but-not-on it closed the shape.
test('clicking the first-corner marker closes the shape', async () => {
  const h = await open();
  h.run("_armTool('polygon')");
  press(h, 100, 100); press(h, 200, 100); press(h, 150, 180);
  const kids = h.run('_draft.svg.children.map(c => c.getAttribute("class"))');
  const i = kids.indexOf('poly-close-target');
  assert(i >= 0, 'a close target is drawn once there are three corners: ' + kids.join());
  h.ctx._i = i;
  h.run('_draft.svg.children[_i].dispatch("mousedown", { preventDefault() {}, stopPropagation() {} })');
  await h.flush();
  eq(sent(h).length, 1, 'written');
  eq(sent(h)[0].vertices.length, 6, 'closed on the three corners — no corner dragged');
  eq(h.run('_polyDraw'), null);
  h.dispose();
});

// ...and "Cloud does not work with Polygon": while building, the preview was
// straight lines with no closing edge, so the cloud only appeared at the end.
test('a cloud polygon previews as a cloud while it is being built', async () => {
  const h = await open();
  h.c.style.value = 'cloud';
  h.run("_armTool('polygon')");
  press(h, 100, 100); press(h, 300, 100);
  h.run("_draft.cursor = [200, 260]; _draftRender()");
  const d = h.run('_draft.svg.children.filter(c => /^path$/i.test(c.tagName)).pop().getAttribute("d")');
  assert(/C/.test(d), 'cloud bumps (curves) in the preview: ' + d.slice(0, 60));
  assert(/Z$/.test(d.trim()), 'closed back to the first corner');
  h.dispose();
});

test('Backspace drops the last point, Enter finishes, Esc cancels', async () => {
  const h = await open();
  h.run("_armTool('polygon')");
  press(h, 100, 100); press(h, 200, 100); press(h, 200, 200); press(h, 120, 260);
  key(h, 'Backspace');
  eq(h.run('_draft.verts.length'), 3);
  key(h, 'Enter');
  await h.flush();
  eq(sent(h)[0].vertices.length, 6);
  press(h, 300, 300); press(h, 400, 300);
  key(h, 'Escape');
  await h.flush();
  eq(sent(h).length, 1, 'the cancelled shape is not written');
  eq(h.run('_draft'), null);
  h.dispose();
});

test('an open polyline sends PolyLine with its ends; a closed cloud sends the cloud style', async () => {
  const h = await open();
  h.c.closed.checked = false;
  h.run("_armTool('polygon')");
  press(h, 100, 100); press(h, 200, 150); press(h, 300, 100, 1); press(h, 300, 100, 2);
  await h.flush();
  let b = sent(h)[0];
  eq(b.subtype, 'PolyLine');
  eq(b.line_ends, ['none', 'closed']);
  h.c.closed.checked = true; h.c.style.value = 'cloud'; h.c.fillOn.checked = true;
  press(h, 100, 300); press(h, 200, 300); press(h, 150, 400); press(h, 150, 400, 2);
  await h.flush();
  b = sent(h)[1];
  eq(b.subtype, 'Polygon');
  eq(b.border_style, 'cloud');
  assert(!b.fill_color, 'a cloud is never filled');
  h.dispose();
});

test('a lifted polygon: a vertex handle moves one vertex, a move moves all', async () => {
  const h = await open();
  h.ctx._i = { annot_idx: 0, tool: 'polygon', closed: true, vertices: [100, 700, 200, 700, 150, 600],
               rect: [99, 599, 201, 701],
               border_color: [1, 0, 0], border_width: 1, line_ends: ['none', 'none'], replies: [] };
  h.run("_buildLiftedDraft(_i, 'sessA-p1')");
  const v0 = h.run('_draft.verts.map(p => p.slice())');
  h.ctx._g = { verts: v0 };
  h.run("_applyDrag('handle', 'v1', _g, 10, 5)");
  const v1 = h.run('_draft.verts');
  eq(v1[0], v0[0]); eq(v1[1], [v0[1][0] + 10, v0[1][1] + 5]); eq(v1[2], v0[2]);
  h.ctx._g = { verts: v1 };
  h.run("_applyDrag('move', null, _g, -4, 3)");
  eq(h.run('_draft.verts')[2], [v1[2][0] - 4, v1[2][1] + 3]);
  h.run('commitDraft()');
  await h.flush();
  eq(sent(h)[0].no_snapshot, true, 'one undo step');
  h.dispose();
});

// ── Pen ──────────────────────────────────────────────────────────────────────
test('a pen stroke is written on release, simplified', async () => {
  const h = await open();
  h.run("_armTool('pen')");
  press(h, 100, 100);
  for (let x = 102; x <= 300; x += 2) h.run(`_annotMouseMove({ clientX: ${x}, clientY: 100 })`);
  h.run('_annotMouseUp({ clientX: 300, clientY: 100 })');
  await h.flush();
  const b = sent(h)[0];
  eq(b.subtype, 'Ink');
  eq(b.ink.length, 1);
  eq(b.ink[0].length, 4, 'a straight stroke collapses to its two ends');
  eq(b.highlighter, false);
  eq(h.run('_activeAnnotTool'), 'pen', 'the pen stays in the hand');
  h.dispose();
});

test('the highlighter sends highlighter: true', async () => {
  const h = await open();
  h.c.hl.checked = true;
  h.run("_armTool('pen')");
  press(h, 100, 100);
  h.run('_annotMouseMove({ clientX: 150, clientY: 120 }); _annotMouseUp({ clientX: 200, clientY: 100 })');
  await h.flush();
  eq(sent(h)[0].highlighter, true);
  h.dispose();
});

// ── Rectangle cloud, paste ──────────────────────────────────────────────────
test('a rectangle in Cloud style is sent as a cloud, never filled', async () => {
  const h = await open();
  h.c.style.value = 'cloud'; h.c.fillOn.checked = true;
  h.run(`_draft = { tool: 'rect', kind: 'box', page_id: 'sessA-p1', st: _pageBlocks.get('sessA-p1'),
                    scale: 1, x: 10, y: 10, w: 100, h: 60, style: _toolStyle(), svg: null };
         commitDraft()`);
  await h.flush();
  const b = sent(h)[0];
  eq([b.subtype, b.border_style], ['Square', 'cloud']);
  assert(!b.fill_color);
  h.dispose();
});

test('pasted polygons and drawings move all their points', async () => {
  const h = await open();
  h.ctx._a = { tool: 'polygon', closed: false, vertices: [100, 600, 200, 650], line_ends: ['none', 'open'],
               border_color: [1, 0, 0], border_width: 1 };
  h.ctx._b = { tool: 'pen', strokes: [[300, 600, 310, 620]], border_color: [1, 0, 0], border_width: 2 };
  const a = h.run('_annotInfoToAddBody(_a)'), b = h.run('_annotInfoToAddBody(_b)');
  eq(a.subtype, 'PolyLine'); eq(b.subtype, 'Ink');
  h.ctx._ba = a; h.ctx._bb = b;
  h.run(`_annotClipboard = { items: [_ba, _bb], bbox: { x1: 100, y1: 600, x2: 310, y2: 650 } };
         _lastMouse = null; _pasteAnnotClipboard()`);
  await h.flush();
  const [pa, pb] = sent(h).slice(-2);
  const dx = pa.vertices[0] - 100, dy = pa.vertices[1] - 600;
  eq(pa.vertices, [100 + dx, 600 + dy, 200 + dx, 650 + dy]);
  eq(pb.ink[0], [300 + dx, 600 + dy, 310 + dx, 620 + dy]);
  h.dispose();
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failed++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}
console.log(failed ? `${failed} failing` : `${tests.length} passing`);
process.exit(failed ? 1 : 0);
