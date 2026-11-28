/* Callout tool (v3.5): a text box with a leader line pointing at something.
 *
 *   * the leader rule is the SERVER's (viewer.py _callout_leader) — the live
 *     preview is only honest if both pick the same side and knee;
 *   * press = the tip, release = where the box goes (left edge at the pointer
 *     when it is right of the tip, right edge when left); a bare click makes
 *     nothing — with the tool armed, that click is how a draft is accepted;
 *   * the commit sends a FreeText with `callout_tip`, in PDF points;
 *   * the tip handle moves only the tip, moving the box leaves the tip put;
 *   * a lifted callout comes back with its tip, and Esc restores both;
 *   * a pasted callout's tip moves with its box.
 *
 * Run: node tests/js/viewer_callout.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(c, m) { if (!c) throw new Error(m || 'assertion failed'); }
function eq(a, b, m) {
  const A = JSON.stringify(a), B = JSON.stringify(b);
  if (A !== B) throw new Error(`${m || 'differs'}: expected ${B}, got ${A}`);
}
function near(a, b, tol, m) {
  if (!(Math.abs(a - b) <= tol)) throw new Error(`${m || 'differs'}: expected ~${b}, got ${a}`);
}

async function open() {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 1, 'a.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 0);
  return h;
}

/** The real gesture: arm, press at (sx,sy), optionally move, release. */
function drawCallout(h, sx, sy, ex, ey) {
  h.run("_armTool('callout')");
  h.ctx._g = { sx, sy, ex, ey };
  h.run(`
    (() => {
      const st = _pageBlocks.get('sessA-p1');
      _drawing = { tool: 'callout', page_id: 'sessA-p1', st, block: st.block,
                   bRect: { left: 0, top: 0 }, startX: _g.sx, startY: _g.sy,
                   isLine: false };
      if (_g.ex != null) _annotMouseMove({ clientX: _g.ex, clientY: _g.ey });
      _annotMouseUp({ clientX: _g.ex != null ? _g.ex : _g.sx,
                      clientY: _g.ey != null ? _g.ey : _g.sy });
    })()`);
}
const D = (h) => h.run('({ x:_draft.x, y:_draft.y, w:_draft.w, h:_draft.h, tx:_draft.tx, ty:_draft.ty, scale:_draft.scale, pageH:_draft.st.pageH, tool:_draft.tool, hasInput: !!_draft.textInput })');

// ── the leader rule matches the server ──────────────────────────────────────
const BOX = [300, 600, 450, 640];
[
  [[100, 620], [300, 620], [288, 620]],
  [[600, 620], [450, 620], [462, 620]],
  [[375, 700], [375, 640], [375, 652]],
  [[375, 500], [375, 600], [375, 588]],
].forEach(([tip, end, knee]) => {
  test(`leader for tip ${tip} matches viewer.py`, async () => {
    const h = await open();
    h.ctx._b = BOX; h.ctx._t = tip;
    eq(h.run('_calloutLeader(_b, _t)'), { end, knee });
    h.dispose();
  });
});

// ── drawing ──────────────────────────────────────────────────────────────────
test('press = tip, release = the box (to the right of the tip)', async () => {
  const h = await open();
  drawCallout(h, 100, 300, 260, 200);
  const d = D(h);
  eq(d.tool, 'callout');
  eq([d.tx, d.ty], [100, 300], 'tip where the press was');
  eq(d.x, 260, 'box starts at the pointer');
  near(d.y + d.h / 2, 200, 1, 'centred on the pointer');
  near(d.w, 150 * d.scale, 0.01, '150pt wide');
  assert(d.hasInput, 'text entry opened');
  h.dispose();
});

test('released LEFT of the tip, the box ends at the pointer', async () => {
  const h = await open();
  drawCallout(h, 400, 300, 250, 200);
  const d = D(h);
  near(d.x + d.w, 250, 0.01);
  h.dispose();
});

test('a bare click makes no callout (it is how a draft is accepted)', async () => {
  const h = await open();
  drawCallout(h, 100, 300);
  eq(h.run('_draft'), null, 'no draft');
  h.dispose();
});

test('commit sends a FreeText callout with its tip in PDF points', async () => {
  const h = await open();
  drawCallout(h, 100, 300, 260, 200);
  const d = D(h);
  h.run("_draft.textInput.value = 'Check this'; commitDraft()");
  await h.flush();
  const calls = h.callsTo('/api/viewer/add_annotation');
  eq(calls.length, 1);
  const b = calls[0].body;
  eq(b.subtype, 'FreeText');
  eq(b.text, 'Check this');
  eq(b.is_comment, false);
  near(b.callout_tip[0], 100 / d.scale, 1e-6, 'tip x');
  near(b.callout_tip[1], d.pageH - 300 / d.scale, 1e-6, 'tip y (PDF y-up)');
  near(b.rect[0], 260 / d.scale, 1e-6, 'box left');
  assert(b.border_width >= 0.5, 'the box and leader are always drawn');
  h.dispose();
});

test('an empty callout is not written', async () => {
  const h = await open();
  drawCallout(h, 100, 300, 260, 200);
  h.run('commitDraft()');
  await h.flush();
  eq(h.callsTo('/api/viewer/add_annotation').length, 0);
  h.dispose();
});

// ── editing ──────────────────────────────────────────────────────────────────
test('the tip handle moves only the tip; a move leaves the tip put', async () => {
  const h = await open();
  drawCallout(h, 100, 300, 260, 200);
  const before = D(h);
  h.ctx._s = { x: before.x, y: before.y, w: before.w, h: before.h,
               tx: before.tx, ty: before.ty };
  h.run("_applyDrag('handle', 'tip', _s, 20, -10)");
  let d = D(h);
  eq([d.tx, d.ty], [120, 290]);
  eq([d.x, d.y, d.w], [before.x, before.y, before.w], 'box unchanged');
  h.ctx._s = { x: d.x, y: d.y, w: d.w, h: d.h, tx: d.tx, ty: d.ty };
  h.run("_applyDrag('move', null, _s, 30, 40)");
  d = D(h);
  eq([d.x, d.y], [before.x + 30, before.y + 40], 'box moved');
  eq([d.tx, d.ty], [120, 290], 'tip stayed');
  h.dispose();
});

const LIFTED = { annot_idx: 2, tool: 'callout', rect: [200, 600, 350, 640],
                 tip: [100, 500], text: 'From the PDF', text_color: [0, 0, 0],
                 font_size: 11, font: 'Helvetica', border_color: [0, 0, 1],
                 border_width: 1, is_comment: false, author: 'GF', replies: [] };

test('a lifted callout keeps its tip, and re-commits it', async () => {
  const h = await open();
  h.ctx._i = LIFTED;
  h.run("_buildLiftedDraft(_i, 'sessA-p1')");
  const d = D(h);
  near(d.tx, 100 * d.scale, 1e-6);
  near(d.ty, (d.pageH - 500) * d.scale, 1e-6);
  h.run('commitDraft()');
  await h.flush();
  const b = h.callsTo('/api/viewer/add_annotation')[0].body;
  near(b.callout_tip[0], 100, 1e-6); near(b.callout_tip[1], 500, 1e-6);
  eq(b.text, 'From the PDF');
  eq(b.no_snapshot, true, 'a lift is one undo step');
  h.dispose();
});

test('a pasted callout carries its tip with the box', async () => {
  const h = await open();
  h.ctx._i = LIFTED;
  const body = h.run('_annotInfoToAddBody(_i)');
  eq(body.callout_tip, [100, 500]);
  h.ctx._b = body;
  h.run(`_annotClipboard = { items: [_b], bbox: { x1: 200, y1: 600, x2: 350, y2: 640 } };
         _lastMouse = null; _pasteAnnotClipboard()`);
  await h.flush();
  const sent = h.calls.filter(c => c.url.startsWith('/api/viewer/add_annotation')).map(c => c.body);
  assert(sent.length >= 1, 'pasted');
  const p = sent[sent.length - 1];
  const dx = p.rect[0] - 200, dy = p.rect[1] - 600;
  near(p.callout_tip[0], 100 + dx, 1e-6, 'tip x moved with the box');
  near(p.callout_tip[1], 500 + dy, 1e-6, 'tip y moved with the box');
  h.dispose();
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failed++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}
console.log(failed ? `${failed} failing` : `${tests.length} passing`);
process.exit(failed ? 1 : 0);
