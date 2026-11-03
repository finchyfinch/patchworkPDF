/* Regression tests: annotation mutations reach the server in the order the user
 * performed them.
 *
 * The server records one undo step per mutation, in ARRIVAL order, so whatever
 * the client fires without queueing can overtake a request still in flight.
 * `commitDraft()` only QUEUES its POST — so "draw a shape, then immediately
 * Ctrl+Z" used to send the undo FIRST: the server reversed the annotation before
 * the one the user had just drawn, and the new shape then landed on top of the
 * undone state.  That is the "undo removed annotation lines out of order"
 * report, and it needs no unusual timing — just a keystroke arriving while a
 * commit is on the wire.
 *
 * The same hole let the sticky-note add, the popup edit, the lift delete and the
 * area delete overtake a draw, because only the draw and paste paths were on the
 * chain.
 *
 * The server now holds the session lock across each mutation, so nothing is
 * corrupted whatever the order (tests/test_annot_undo_ordering.py pins that).
 * What these pin is the other half: the order matches what the user did.
 *
 * Run: node tests/js/viewer_annot_order.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || 'not equal'}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}
function deepEq(a, b, msg) {
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    throw new Error(`${msg || 'not equal'}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
  }
}

const ANNOT_URLS = ['/api/viewer/add_annotation', '/api/viewer/delete_annotation',
                    '/api/viewer/update_annotation', '/api/viewer/undo',
                    '/api/viewer/redo'];
const short = (u) => u.replace('/api/viewer/', '');

/** Viewer open on one page, with annotation requests slowed and recorded. */
async function viewer(opts) {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 1, 'doc.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 0);

  // Requests ARRIVE in call order; each resolves after `lag` turns, so a caller
  // that does not queue can overtake one still in flight.  `arrivals` is what
  // the server would see.
  h.arrivals = [];
  const lag = (opts && opts.lag) || 4;
  const realFetch = h.ctx.apiFetch;
  h.ctx.apiFetch = (url, o) => {
    if (!ANNOT_URLS.some(u => url.startsWith(u))) return realFetch(url, o);
    h.arrivals.push(short(url));
    const fail = (opts && opts.failUrls || []).some(u => url.startsWith(u));
    return new Promise((resolve, reject) => {
      let n = lag;
      const tick = () => (n-- > 0 ? h.ctx.setTimeout(tick, 0)
                                  : (fail ? reject(new Error('boom'))
                                          : resolve({
                                              ok: true, status: 200,
                                              headers: { get: () => 'application/json' },
                                              json: async () => ({ page_id: 'sessA-p1',
                                                                   annotations: [],
                                                                   editable: [],
                                                                   undo_count: 1,
                                                                   redo_count: 0 }),
                                            })));
      tick();
    });
  };
  return h;
}

/** Arm a tool and draw one shape, leaving a live draft ready to commit. */
function drawShape(h) {
  h.run(`
    _draft = { tool: 'rect', page_id: 'sessA-p1', x: 10, y: 10, w: 50, h: 40,
               scale: 1.5, st: _pageBlocks.get('sessA-p1'),
               style: { lineColor: [1,0,0], lineWidth: 2, lineStyle: 'solid',
                        fillOn: false, fillColor: [1,1,1], fontSize: 11,
                        textBox: true },
               svg: null, textInput: null, block: null };
  `);
}

// ── the report ───────────────────────────────────────────────────────────────

test('a draw followed straight away by undo arrives draw-then-undo', async () => {
  const h = await viewer();
  drawShape(h);
  h.run('commitDraft()');       // only QUEUES the POST
  h.run("viewerUndo()");        // the keystroke, while the add is on the wire
  await h.flush(40);

  deepEq(h.arrivals, ['add_annotation', 'undo'],
         'the undo overtaking the add is what reverses the WRONG annotation');
  h.dispose();
});

test('a sticky note cannot overtake a shape already being committed', async () => {
  const h = await viewer();
  drawShape(h);
  h.run('commitDraft()');
  h.run("_placeSticky('sessA-p1', 100, 100)");
  await h.flush(40);
  deepEq(h.arrivals, ['add_annotation', 'add_annotation']);
  h.dispose();
});

test('a popup edit cannot overtake a shape already being committed', async () => {
  const h = await viewer();
  drawShape(h);
  h.run('commitDraft()');
  h.run(`
    _editingAnnot = { page_id: 'sessA-p1', annot_idx: 0, ann: {} };
    saveAnnotEdit();
  `);
  await h.flush(40);
  deepEq(h.arrivals, ['add_annotation', 'update_annotation']);
  h.dispose();
});

test('three drawings in a row arrive in draw order', async () => {
  const h = await viewer();
  for (let i = 0; i < 3; i++) { drawShape(h); h.run('commitDraft()'); }
  await h.flush(60);
  deepEq(h.arrivals, ['add_annotation', 'add_annotation', 'add_annotation'],
         'fast draw-draw-draw must not reorder or drop a commit');
  h.dispose();
});

test('undo then redo stay in order behind a pending draw', async () => {
  const h = await viewer();
  drawShape(h);
  h.run('commitDraft()');
  h.run('viewerUndo()');
  h.run('viewerRedo()');
  await h.flush(60);
  deepEq(h.arrivals, ['add_annotation', 'undo', 'redo']);
  h.dispose();
});

// ── the queue must not become a single point of failure ──────────────────────

test('a failed mutation does not wedge the ones behind it', async () => {
  const h = await viewer({ failUrls: ['/api/viewer/add_annotation'] });
  drawShape(h);
  h.run('commitDraft()');       // this one rejects
  h.run('viewerUndo()');
  await h.flush(60);
  deepEq(h.arrivals, ['add_annotation', 'undo'],
         'one bad request must not stop every annotation action afterwards');
  h.dispose();
});

test('the queue keeps working after a rejection', async () => {
  const h = await viewer({ failUrls: ['/api/viewer/update_annotation'] });
  h.run(`
    _editingAnnot = { page_id: 'sessA-p1', annot_idx: 0, ann: {} };
    saveAnnotEdit();
  `);
  await h.flush(40);
  drawShape(h);
  h.run('commitDraft()');
  await h.flush(40);
  deepEq(h.arrivals, ['update_annotation', 'add_annotation']);
  h.dispose();
});

// ── run ──────────────────────────────────────────────────────────────────────
let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log(`  ok    ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name}\n        ${e.message}`);
    if (process.env.PW_TEST_TRACE) console.log(e.stack);
  }
}
console.log(failed ? `\n${failed} of ${tests.length} tests failed` : `\n${tests.length} tests passed`);
process.exit(failed ? 1 : 0);
