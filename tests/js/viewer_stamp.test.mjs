/* Review stamps (v3.5): APPROVED, REJECTED, ... with name and date.
 *
 *   * a click stamps a default-size stamp centred on it, straight away, and
 *     the tool stays armed (stamp page after page);
 *   * a drag stamps the dragged box;
 *   * the label is the UI language's word, upper-cased; Custom takes the typed
 *     text and refuses to stamp nothing;
 *   * line 2 is the Name field and today's date, or nothing when unticked;
 *   * a lifted stamp shows its kind, and picking another preset restyles THAT
 *     stamp while keeping its original name/date line — and re-commits as one
 *     undo step.
 *
 * Run: node tests/js/viewer_stamp.test.mjs
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
  h.toasts = [];
  h.ctx.showToast = (m, kind) => h.toasts.push([m, kind]);
  // The panel controls the stamp reads (the stub DOM hands out a fresh element
  // per lookup unless one is registered under that id).
  const mk = (id, props) => { const el = h.doc.createElement('input'); el.id = id; Object.assign(el, props); return el; };
  h.sub    = mk('at-stamp-sub',    { checked: true });
  h.color  = mk('at-stamp-color',  { value: '#1e8c3a' });
  h.custom = mk('at-stamp-custom', { value: '' });
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 0);
  h.run("_reviewerName = 'GF'");
  h.ctx.window.t = (k) => ({ 'stamp.approved': 'Approved', 'stamp.rejected': 'Rejected' }[k] || k);
  return h;
}

function place(h, sx, sy, ex, ey) {
  h.run("_armTool('stamp')");
  h.ctx._g = { sx, sy, ex, ey };
  h.run(`
    (() => {
      const st = _pageBlocks.get('sessA-p1');
      _drawing = { tool: 'stamp', page_id: 'sessA-p1', st, block: st.block,
                   bRect: { left: 0, top: 0 }, startX: _g.sx, startY: _g.sy, isLine: false };
      if (_g.ex != null) _annotMouseMove({ clientX: _g.ex, clientY: _g.ey });
      _annotMouseUp({ clientX: _g.ex != null ? _g.ex : _g.sx, clientY: _g.ey != null ? _g.ey : _g.sy });
    })()`);
}
const sent = (h) => h.callsTo('/api/viewer/add_annotation').map(c => c.body);

test('a click stamps a default-size stamp centred on it, and the tool stays armed', async () => {
  const h = await open();
  place(h, 300, 200);
  await h.flush();
  const b = sent(h);
  eq(b.length, 1, 'written straight away');
  eq(b[0].subtype, 'Stamp');
  eq(b[0].stamp.key, 'approved');
  eq(b[0].stamp.label, 'APPROVED', 'the UI word, upper-cased');
  assert(/^GF · /.test(b[0].stamp.sub), 'name and date: ' + b[0].stamp.sub);
  const sc = h.run("_pageBlocks.get('sessA-p1').renderScale * zoomLevel");
  const H = h.run("_pageBlocks.get('sessA-p1').pageH");
  const [x1, y1, x2, y2] = b[0].rect;
  near((x1 + x2) / 2, 300 / sc, 1e-6, 'centred x');
  near((y1 + y2) / 2, H - 200 / sc, 1e-6, 'centred y');
  near(x2 - x1, 'APPROVED'.length * 13 + 40, 1e-6, 'default width');
  near(y2 - y1, 46, 1e-6, 'two-line height');
  eq(h.run('_activeAnnotTool'), 'stamp', 'still armed');
  eq(h.run('_draft'), null, 'no draft left behind');
  h.dispose();
});

test('a drag stamps the dragged box', async () => {
  const h = await open();
  place(h, 100, 100, 340, 180);
  await h.flush();
  const b = sent(h)[0];
  const sc = h.run("_pageBlocks.get('sessA-p1').renderScale * zoomLevel");
  near(b.rect[2] - b.rect[0], 240 / sc, 1e-6);
  near(b.rect[3] - b.rect[1], 80 / sc, 1e-6);
  h.dispose();
});

test('Name & date unticked: no second line', async () => {
  const h = await open();
  h.sub.checked = false;
  place(h, 300, 200);
  await h.flush();
  eq(sent(h)[0].stamp.sub, '');
  h.dispose();
});

test('Custom stamps the typed text, and refuses to stamp nothing', async () => {
  const h = await open();
  h.run("selectStampPreset('custom')");
  place(h, 300, 200);
  await h.flush();
  eq(sent(h).length, 0, 'nothing written');
  assert(h.toasts.some(t => t[0] === 'toast.type_stamp_text'), 'told why');
  h.custom.value = 'checked on site';
  place(h, 300, 200);
  await h.flush();
  eq(sent(h)[0].stamp.label, 'CHECKED ON SITE');
  eq(sent(h)[0].stamp.key, 'custom');
  h.dispose();
});

test('a lifted stamp is restyled in place, keeping its own name/date line', async () => {
  const h = await open();
  h.ctx._i = { annot_idx: 1, tool: 'stamp', rect: [100, 600, 260, 646],
               stamp: { key: 'approved', label: 'APPROVED', sub: 'AB · 1 Jan 2025',
                        color: [0.12, 0.55, 0.23] },
               author: 'AB', replies: [] };
  h.run("_buildLiftedDraft(_i, 'sessA-p1')");
  eq(h.run('_stampKey'), 'approved', 'panel shows the lifted kind');
  h.run("selectStampPreset('rejected')");
  h.run('commitDraft()');
  await h.flush();
  const b = sent(h)[0];
  eq(b.stamp.key, 'rejected');
  eq(b.stamp.label, 'REJECTED');
  eq(b.stamp.sub, 'AB · 1 Jan 2025', 'an old approval is not re-dated');
  eq(b.no_snapshot, true, 'one undo step');
  near(b.rect[0], 100, 1e-6); near(b.rect[3], 646, 1e-6);
  h.dispose();
});

test('a copied stamp pastes with its stamp text', async () => {
  const h = await open();
  h.ctx._i = { tool: 'stamp', rect: [100, 600, 260, 646], border_color: [0, 0, 0],
               stamp: { key: 'void', label: 'VOID', sub: '', color: [0.78, 0.16, 0.16] } };
  const body = h.run('_annotInfoToAddBody(_i)');
  eq(body.subtype, 'Stamp');
  eq(body.stamp.label, 'VOID');
  h.dispose();
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`  ok   ${name}`); }
  catch (e) { failed++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}
console.log(failed ? `${failed} failing` : `${tests.length} passing`);
process.exit(failed ? 1 : 0);
