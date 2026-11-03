/* Regression tests: the Pointer — editing the document's own text in the viewer.
 *
 * The gate is the point of this tool, and it is deliberately two conditions:
 * the panel OPEN and the tool ARMED.  There is no separate editing mode, but a
 * click on the page must not start rewriting the document unless the user asked
 * for it — otherwise someone trying to select text to copy it would find
 * themselves editing instead.  Collapsing the panel is how the toolset is put
 * away, so it ends editing too.
 *
 * The rest of what needs pinning:
 *
 *   1. Elements load PER PAGE, on demand, and follow the scroll.  The
 *      extraction behind them runs OCR on a scanned page, so loading every page
 *      on screen would be ruinous on a long scan.
 *   2. Every edit is committed immediately — one request, one Ctrl+Z — and the
 *      element model is RELOADED afterwards, because rewriting the page
 *      invalidates every id from the previous load.
 *   3. A press that does not move is a selection, not a move.
 *   4. An unchanged editor commits nothing.
 *
 * Run: node tests/js/viewer_edit_text.test.mjs
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
  h.run('rightCollapsed = false;');     // the panel is open
  return h;
}

const loads = (h) => h.callsTo('/api/viewer/page_elements');
const edits = (h) => h.callsTo('/api/viewer/edit_text');
// Count overlays actually attached to page blocks. The stub keeps a detached
// node answering to getElementById, so an id lookup would not prove removal.
const overlaysOnBlocks = (h) => h.run(`(() => {
  let n = 0;
  _pageBlocks.forEach(st => {
    if (st && st.block && st.block.children) {
      n += st.block.children.filter(
        c => c.classList && c.classList.contains('page-edittext-overlay')).length;
    }
  });
  return n;
})()`);
const runs  = (h) => h.run(`(() => {
  const ov = document.getElementById('et-ov-' + _etPage);
  return ov ? ov.children.filter(c => (c.className||'').includes('et-run')).length : 0;
})()`);

async function arm(h) {
  h.run("selectAnnotTool('edittext')");
  await h.flush(20);
}

/** Press (and optionally drag) the Nth run's hit-area. */
function grab(h, idx, dx = 0, dy = 0) {
  h.run(`(() => {
    const ov = document.getElementById('et-ov-' + _etPage);
    const el = ov.children.filter(c => (c.className||'').includes('et-run'))[${idx}];
    el.dispatch('mousedown', { button: 0, clientX: 0, clientY: 0,
                               preventDefault(){}, stopPropagation(){} });
    if (${dx} !== 0 || ${dy} !== 0) _etDragMove({ clientX: ${dx}, clientY: ${dy} });
    _etDragEnd();
  })()`);
}

// ── the gate ────────────────────────────────────────────────────────────────

test('arming with the panel open loads the page and shows hit-areas', async () => {
  const h = await viewer();
  await arm(h);
  eq(loads(h).length, 1, 'the element model was not loaded');
  eq(h.run('_etPage'), 'sessA-p1');
  eq(runs(h), 2, 'no hit-areas drawn');
  h.dispose();
});

test('a collapsed panel means no editing, even with the tool armed', async () => {
  const h = await viewer();
  h.run('rightCollapsed = true;');
  await arm(h);
  eq(h.run('_etActive()'), false, 'the gate let editing through a closed panel');
  eq(loads(h).length, 0, 'it loaded elements it cannot use');
  eq(h.run('_etPage'), null);
  h.dispose();
});

test('collapsing the panel mid-edit ends editing and clears the overlay',
     async () => {
  const h = await viewer();
  await arm(h);
  eq(runs(h), 2, 'precondition');
  h.run('toggleRightPanel()');          // closes it
  await h.flush(10);
  eq(h.run('_etActive()'), false);
  eq(h.run('_etPage'), null, 'the loaded page survived the panel closing');
  eq(overlaysOnBlocks(h), 0, 'the hit-areas outlived the panel');
  h.dispose();
});

test('disarming the tool clears everything', async () => {
  const h = await viewer();
  await arm(h);
  h.run("selectAnnotTool('edittext')");  // toggle off
  await h.flush(10);
  eq(h.run('_activeAnnotTool'), null);
  eq(h.run('_etPage'), null);
  eq(overlaysOnBlocks(h), 0);
  h.dispose();
});

test('the Pointer never starts an annotation draft or a page-tool drag',
     async () => {
  const h = await viewer();
  await arm(h);
  h.run(`(() => {
    const block = document.getElementById('page-block-sessA-p1');
    _annotMouseDown({ button: 0, target: block, clientX: 10, clientY: 10,
                      preventDefault(){}, stopPropagation(){} });
  })()`);
  eq(h.run('_draft ? 1 : 0'), 0, 'a draft would be written as an annotation');
  eq(h.run('_pageToolDrag ? 1 : 0'), 0);
  h.dispose();
});

// ── per-page loading ────────────────────────────────────────────────────────

test('it loads one page at a time and follows the scroll', async () => {
  const h = await viewer(2);
  await arm(h);
  eq(loads(h).length, 1);
  eq(loads(h)[0].body.page_id, 'sessA-p1');

  h.run(`currentPageId = 'sessA-p2'; if (_etActive()) _etLoadForCurrentPage();`);
  await h.flush(20);
  eq(loads(h).length, 2, 'it did not follow the scroll');
  eq(loads(h)[1].body.page_id, 'sessA-p2');
  eq(h.run('_etPage'), 'sessA-p2');
  h.dispose();
});

test('it does not reload the page it already has', async () => {
  const h = await viewer();
  await arm(h);
  h.run('_etLoadForCurrentPage(); _etLoadForCurrentPage();');
  await h.flush(20);
  eq(loads(h).length, 1, 'redundant loads cost an OCR pass on a scan');
  h.dispose();
});

// ── selecting, editing, moving, deleting ────────────────────────────────────

test('clicking a run selects it without editing anything', async () => {
  const h = await viewer();
  await arm(h);
  grab(h, 0);
  eq(h.run('_etSel'), 'txt_1');
  eq(edits(h).length, 0, 'selecting must not rewrite the page');
  eq(h.run("document.getElementById('at-et-selected').textContent"), 'Alpha');
  eq(h.run("document.getElementById('at-et-delete').disabled"), false);
  h.dispose();
});

test('selecting re-styles the hit-areas in place and never replaces them',
     async () => {
  // A browser only fires `dblclick` when both halves of the gesture land on the
  // SAME element.  Selection runs on MOUSEDOWN, so if it rebuilds the overlay
  // it destroys the node the pointer is over between click one and click two,
  // and the double-click that opens the editor can never arrive.  That shipped:
  // the run turned yellow and then refused to open.  A stub cannot model the
  // browser's dblclick bookkeeping, so what is pinned here is the invariant
  // that bookkeeping depends on — node identity survives a selection.
  const h = await viewer();
  await arm(h);
  const r = h.run(`(() => {
    const ov = document.getElementById('et-ov-' + _etPage);
    const before = ov.children.filter(c => (c.className||'').includes('et-run'));
    const target = before[0];
    target.dispatch('mousedown', { button: 0, clientX: 0, clientY: 0,
                                   preventDefault(){}, stopPropagation(){} });
    _etDragEnd();
    const ov2 = document.getElementById('et-ov-' + _etPage);
    const after = ov2.children.filter(c => (c.className||'').includes('et-run'));
    return {
      sameOverlay: ov2 === ov,
      sameNodes:   after.length === before.length &&
                   after.every((c, i) => c === before[i]),
      onSelected:  target.classList.contains('sel'),
      onOther:     after.length > 1 && after[1].classList.contains('sel'),
    };
  })()`);
  assert(r.sameOverlay, 'the overlay container was replaced');
  assert(r.sameNodes,   'the hit-area nodes were replaced — dblclick cannot fire');
  assert(r.onSelected,  'the clicked run was not marked selected');
  assert(!r.onOther,    'selection leaked onto another run');
  h.dispose();
});

test('dragging a run moves it, in display-frame points', async () => {
  const h = await viewer();
  await arm(h);
  const s = h.run('(() => { const st=_pageBlocks.get("sessA-p1");'
                + ' return st.renderScale * zoomLevel; })()');
  grab(h, 0, 30, 15);
  await h.flush(20);
  eq(edits(h).length, 1);
  const b = edits(h)[0].body;
  eq(b.op, 'move');
  eq(b.id, 'txt_1');
  assert(Math.abs(b.x - (72 + 30 / s)) < 0.01, `x=${b.x}`);
  assert(Math.abs(b.y - (700 - 15 / s)) < 0.01, `y=${b.y} (screen y is down)`);
  h.dispose();
});

test('a press that does not move sends nothing', async () => {
  const h = await viewer();
  await arm(h);
  grab(h, 0, 1, 1);
  await h.flush(20);
  eq(edits(h).length, 0, 'a click is a selection, not a move');
  h.dispose();
});

test('changing the words sends one text edit', async () => {
  const h = await viewer();
  await arm(h);
  h.run("_etOpenEntry(_etItems[0]); _etEntry.el.value = 'Gamma'; _etCommitEntry();");
  await h.flush(20);
  eq(edits(h).length, 1);
  eq(edits(h)[0].body.op, 'text');
  eq(edits(h)[0].body.text, 'Gamma');
  h.dispose();
});

test('an unchanged editor commits nothing', async () => {
  const h = await viewer();
  await arm(h);
  h.run("_etOpenEntry(_etItems[0]); _etCommitEntry();");   // value untouched
  await h.flush(20);
  eq(edits(h).length, 0,
     're-injecting identical text still re-renders the run and costs an undo step');
  h.dispose();
});

test('Escape in the editor discards', async () => {
  const h = await viewer();
  await arm(h);
  h.run(`_etOpenEntry(_etItems[0]); _etEntry.el.value = 'nope';
         _etEntry.el.onkeydown({ key: 'Escape', preventDefault(){} });`);
  await h.flush(20);
  eq(edits(h).length, 0);
  eq(h.run('_etEntry ? 1 : 0'), 0);
  h.dispose();
});

test('Del removes the selected run', async () => {
  const h = await viewer();
  await arm(h);
  grab(h, 1);
  h.run("_etKey({ key: 'Delete', target: { tagName: 'DIV' }, preventDefault(){} })");
  await h.flush(20);
  eq(edits(h).length, 1);
  eq(edits(h)[0].body.op, 'delete');
  eq(edits(h)[0].body.id, 'txt_2');
  h.dispose();
});

test('Del while typing in the inline editor does not delete the run', async () => {
  const h = await viewer();
  await arm(h);
  grab(h, 0);
  h.run('_etOpenEntry(_etItems[0]);');
  h.run("_etKey({ key: 'Delete', target: { tagName: 'INPUT' }, preventDefault(){} })");
  await h.flush(20);
  eq(edits(h).length, 0);
  h.dispose();
});

// ── after an edit ───────────────────────────────────────────────────────────

test('an edit reloads the element model, because the ids are now stale',
     async () => {
  const h = await viewer();
  await arm(h);
  eq(loads(h).length, 1);
  h.run("_etOpenEntry(_etItems[0]); _etEntry.el.value = 'Gamma'; _etCommitEntry();");
  await h.flush(30);
  eq(loads(h).length, 2,
     'the page was rewritten, so every id from the first load is stale');
  h.dispose();
});

test('an edit flags the document, updates Undo and re-renders the page',
     async () => {
  const h = await viewer();
  let dirty = null, invalidated = null;
  h.ctx.pwSetDocDirty = (v) => { dirty = v; };
  h.ctx._invalidateViewerPage = (pid) => { invalidated = pid; };
  await arm(h);
  grab(h, 0, 30, 15);
  await h.flush(30);
  eq(dirty, true);
  eq(h.run('_vUndoCount'), 1, 'an edit is an undo step');
  eq(invalidated, 'sessA-p1');
  h.dispose();
});

test('edits are queued behind a mutation in flight', async () => {
  const h = await viewer();
  await arm(h);
  h.run(`_commitChain = new Promise(res => setTimeout(res, 0))
            .then(() => new Promise(res => setTimeout(res, 0)))`);
  grab(h, 0, 30, 15);
  eq(edits(h).length, 0, 'fired without waiting for the chain');
  await h.flush(30);
  eq(edits(h).length, 1);
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
