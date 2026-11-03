/* Regression test: committing a drawn annotation must not report a failure.
 *
 * commitDraft's success message read `subtype`, a variable that only exists
 * inside _draftToAddBody.  The annotation saved, then the success handler threw
 * a ReferenceError, and the chain's catch turned it into a red
 * "Failed: subtype is not defined" toast — on every newly drawn shape (reported
 * against clouds).  The other suites replace showToast with a no-op, which is
 * why nothing noticed.
 *
 * The harness sandbox resolves an unknown identifier to `undefined` instead of
 * throwing, so the ReferenceError itself cannot surface here.  What CAN is its
 * behaviour: with `subtype` undefined a text note was announced as
 * "Annotation added" instead of "Text added" (now the locale keys
 * toast.added_listed and toast.text_added).
 *
 * Run: node tests/js/viewer_annot_commit_toast.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }

async function viewer() {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 1, 'doc.pdf') } });
  h.toasts = [];
  h.ctx.showToast = (msg, kind) => h.toasts.push([String(msg), kind]);
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 0);
  return h;
}

function draw(h, tool) {
  h.run(`
    _draft = { tool: '${tool}', page_id: 'sessA-p1', x: 10, y: 10, w: 50, h: 40,
               scale: 1.5, st: _pageBlocks.get('sessA-p1'),
               style: { lineColor: [1,0,0], lineWidth: 2, lineStyle: 'solid',
                        fillOn: false, fillColor: [1,1,1], fontSize: 11, textBox: true },
               svg: null, textInput: ${tool === 'text' ? "{ value: 'Approved', remove() {} }" : 'null'}, block: null };
  `);
}

test('a text note is announced as text — the success message reads the subtype', async () => {
  const h = await viewer();
  draw(h, 'text');
  h.run('commitDraft()');
  await h.flush(30);
  // Since v3.2 the message is a locale key (the harness's window.t returns the
  // key itself): toast.text_added is "Text added" in en.json.
  assert(h.toasts.some(([m, k]) => m === 'toast.text_added' && k === 'success'),
         `expected "toast.text_added", got ${JSON.stringify(h.toasts)}`);
  h.dispose();
});

for (const tool of ['cloud', 'rect', 'ellipse', 'highlight']) {
  test(`committing a ${tool} reports success, not a failure`, async () => {
    const h = await viewer();
    draw(h, tool);
    h.run('commitDraft()');
    await h.flush(30);
    const bad = h.toasts.filter(([m, k]) => k === 'error' || /not defined|failed/i.test(m));
    assert(!bad.length, `error toast after a successful commit: ${JSON.stringify(bad)}`);
    assert(h.toasts.some(([m, k]) => k === 'success'), 'no success message');
    h.dispose();
  });
}

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`PASS  ${name}`); }
  catch (e) { failed++; console.log(`FAIL  ${name}\n      ${e.message}`); }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
