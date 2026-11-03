/* Regression tests: the Pointer's one-word editor, and editing a row / area.
 *
 * Reported against v3.1 (the release that merged the editor into the viewer):
 *
 *   1. "When I select the text and type the replacement you can't actually see
 *      the word I typed."  OCR now samples the real ink colour, so a white
 *      header run arrived with color '#ffffff' — and the entry painted that on
 *      its hard-coded white background.  White on white.
 *   2. "Edit box is a bit long."  The field had only a min-width, so it took
 *      the <input>'s default ~20-character width; and it was sized from the
 *      run's cap height (`size`) rather than its em (`font_size`), too short
 *      for the line it holds.
 *   3. "Multi word / line text editing was not brought through."  v2.24's
 *      editor had "Merge row": an adjustable box seeded on the selected word's
 *      row, resizable across rows, edited as one paragraph.  It is back as
 *      "Row / area" (right-click, or the panel button).
 *
 * And one found on the way: the runs come in absolute user space, which on a
 * CROPPED page is the display frame plus the CropBox origin.  The hit-areas
 * ignored the page_left/page_top the server sends, so they were drawn off by
 * the crop and a drag moved text twice as far.
 *
 * Run: node tests/js/viewer_edit_block.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || 'not equal'}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}
const near = (a, b, tol = 0.01) => Math.abs(a - b) < tol;
// The stub does not parse cssText into properties, so read it from there.
const cssPx = (css, prop) => {
  for (const decl of String(css || '').split(';')) {
    const m = /^\s*([a-z-]+)\s*:\s*(-?[\d.]+)px\s*$/.exec(decl);
    if (m && m[1] === prop) return parseFloat(m[2]);
  }
  return NaN;
};

async function viewer() {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 2, 'doc.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 1);
  h.run('rightCollapsed = false;');
  h.run("selectAnnotTool('edittext')");
  await h.flush(20);
  return h;
}

const blocks = (h) => h.callsTo('/api/viewer/edit_block');
const edits  = (h) => h.callsTo('/api/viewer/edit_text');
const scale  = (h) => h.run('(() => { const st=_pageBlocks.get(_etPage);'
                          + ' return st.renderScale * zoomLevel; })()');

/** A two-row cell ("Line Amount" / "Excl. VAT") plus a far-right value on the
 *  first row's baseline, and a word on an unrelated line further down. */
function tableItems(h) {
  h.run(`_etItems = [
    { id:'a', text:'Line',   x:486, y:423, x2:502, y2:430, size:7, font_size:10.8, color:'#ffffff', rl_font:'Calibri' },
    { id:'b', text:'Amount', x:505, y:423, x2:539, y2:430, size:7, font_size:10.8, color:'#ffffff', rl_font:'Calibri' },
    { id:'c', text:'Excl.',  x:502, y:414, x2:519, y2:421, size:7, font_size:10.8, color:'#ffffff', rl_font:'Calibri' },
    { id:'d', text:'VAT',    x:522, y:414, x2:539, y2:421, size:7, font_size:10.8, color:'#ffffff', rl_font:'Calibri' },
    { id:'e', text:'9,999',  x:580, y:423, x2:600, y2:430, size:7, font_size:10.8, color:'#000000', rl_font:'Calibri' },
    { id:'f', text:'Total',  x:486, y:300, x2:510, y2:308, size:7, font_size:10.8, color:'#000000', rl_font:'Calibri' },
  ]; _etRenderOverlays();`);
}

// ── the one-word editor ─────────────────────────────────────────────────────

test('white text is edited on a dark field, not white on white', async () => {
  const h = await viewer();
  h.run("_etItems[0].color = '#ffffff'; _etOpenEntry(_etItems[0]);");
  const st = h.run('({ c: _etEntry.el.style.color, bg: _etEntry.el.style.background })');
  eq(st.c, '#ffffff');
  assert(st.bg && st.bg !== '#ffffff' && st.bg !== '#fff',
         `typed text would be invisible: background ${st.bg}`);
  h.dispose();
});

test('dark text keeps the white field', async () => {
  const h = await viewer();
  h.run("_etOpenEntry(_etItems[0]);");
  eq(h.run('_etEntry.el.style.background'), '#ffffff');
  h.dispose();
});

test('the field is sized from the em, not the cap height', async () => {
  const h = await viewer();
  const s = scale(h);
  h.run("_etOpenEntry(_etItems[0]);");            // size 10, font_size 11
  const font = h.run('_etEntry.el.style.font');
  const px = parseFloat(/([\d.]+)px/.exec(font)[1]);
  assert(near(px, Math.max(9, 11 * s), 0.05), `font ${font} is not the em size`);
  const hgt = cssPx(h.run('_etEntry.wrap.style.cssText'), 'height');
  assert(hgt >= px * 1.2, `height ${hgt}px cannot hold ${px}px text`);
  h.dispose();
});

test('the field is as wide as the words and grows as you type', async () => {
  const h = await viewer();
  h.run("_etOpenEntry(_etItems[0]);");            // 'Alpha', run 48pt wide
  const w0 = parseFloat(h.run('_etEntry.wrap.style.width'));
  assert(w0 > 0, 'no explicit width: the <input> default decides it');
  h.run(`_etEntry.el.value = 'Alpha and a great deal more besides';
         _etEntry.el.dispatch('input', {});`);
  const w1 = parseFloat(h.run('_etEntry.wrap.style.width'));
  assert(w1 > w0, `did not grow: ${w0} -> ${w1}`);
  h.dispose();
});

// ── cropped pages ───────────────────────────────────────────────────────────

test('on a cropped page the hit-areas come off the CropBox origin', async () => {
  const h = await viewer();
  const s = scale(h);
  const pageH = h.run('_pageBlocks.get(_etPage).pageH');
  // CropBox lower-left at (50, 60): items are absolute, the page is not.
  h.run(`_etPageLeft = 50; _etPageTop = ${pageH} + 60; _etRenderOverlays();`);
  const left = cssPx(h.run(`(() => { const ov = document.getElementById('et-ov-' + _etPage);
    return ov.children[0].style.cssText; })()`), 'left');
  assert(near(left, (72 - 50) * s, 0.2), `left ${left}, expected ${(72 - 50) * s}`);
  const top = cssPx(h.run(`(() => { const ov = document.getElementById('et-ov-' + _etPage);
    return ov.children[0].style.cssText; })()`), 'top');
  assert(near(top, (pageH - (712 - 60)) * s, 0.2), `top ${top}`);
  h.dispose();
});

test('on a cropped page a drag sends DISPLAY-frame coordinates', async () => {
  const h = await viewer();
  const s = scale(h);
  const pageH = h.run('_pageBlocks.get(_etPage).pageH');
  h.run(`_etPageLeft = 50; _etPageTop = ${pageH} + 60; _etRenderOverlays();`);
  h.run(`(() => {
    const ov = document.getElementById('et-ov-' + _etPage);
    ov.children[0].dispatch('mousedown', { button: 0, clientX: 0, clientY: 0,
                                            preventDefault(){}, stopPropagation(){} });
    _etDragMove({ clientX: 30, clientY: 15 });
    _etDragEnd();
  })()`);
  await h.flush(20);
  const b = edits(h)[0].body;
  assert(near(b.x, 72 - 50 + 30 / s), `x=${b.x}: the server adds the origin back`);
  assert(near(b.y, 700 - 60 - 15 / s), `y=${b.y}`);
  h.dispose();
});

// ── right-click ─────────────────────────────────────────────────────────────

test('right-clicking a run selects it and offers Row / area', async () => {
  const h = await viewer();
  h.run(`(() => {
    const ov = document.getElementById('et-ov-' + _etPage);
    ov.children[1].dispatch('contextmenu', { clientX: 40, clientY: 40,
                                             preventDefault(){}, stopPropagation(){} });
  })()`);
  eq(h.run('_etSel'), 'txt_2');
  eq(h.run("document.getElementById('et-ctx-menu').style.display"), 'block');
  h.run('etCtxBlock()');
  eq(h.run("document.getElementById('et-ctx-menu').style.display"), 'none');
  assert(h.run('!!_etBlock'), 'Row / area did not open the box');
  h.dispose();
});

// ── row / area ──────────────────────────────────────────────────────────────

test('the box starts on the selected word\'s row and stops at a column gap',
     async () => {
  const h = await viewer();
  tableItems(h);
  h.run("_etStartBlock(_etItems.find(i => i.id === 'b'))");
  const ids = h.run('_etBlockItems().map(i => i.id)');
  eq(JSON.stringify(ids), JSON.stringify(['a', 'b']),
     'it must take the words either side, not the value a column away');
  h.dispose();
});

test('dragging the bottom handle takes in the next row, in reading order',
     async () => {
  const h = await viewer();
  tableItems(h);
  const s = scale(h);
  h.run("_etStartBlock(_etItems.find(i => i.id === 'a'))");
  // 12pt further down the page = +12pt of screen y.
  h.run(`_etBlockGrab({ button: 0, clientX: 0, clientY: 0,
                        preventDefault(){}, stopPropagation(){} }, 's');
         _etBlockDragMove({ clientX: 0, clientY: ${12 * s} });
         _etBlockDragEnd();`);
  const ids = h.run('_etBlockItems().map(i => i.id)');
  eq(JSON.stringify(ids), JSON.stringify(['a', 'b', 'c', 'd']));
  // And the words it will take are marked on the page.
  const marked = h.run(`(() => { const ov = document.getElementById('et-ov-' + _etPage);
    return ov.children.filter(c => c.classList && c.classList.contains('inblk')).length; })()`);
  eq(marked, 4, 'the captured words are not highlighted');
  h.dispose();
});

test('dragging the west handle leaves a word out', async () => {
  const h = await viewer();
  tableItems(h);
  const s = scale(h);
  h.run("_etStartBlock(_etItems.find(i => i.id === 'a'))");
  h.run(`_etBlockGrab({ button: 0, clientX: 0, clientY: 0,
                        preventDefault(){}, stopPropagation(){} }, 'w');
         _etBlockDragMove({ clientX: ${16 * s}, clientY: 0 });
         _etBlockDragEnd();`);
  eq(JSON.stringify(h.run('_etBlockItems().map(i => i.id)')), JSON.stringify(['b']));
  h.dispose();
});

test('Enter edits the captured words as one text, in their own colour',
     async () => {
  const h = await viewer();
  tableItems(h);
  h.run("_etBlock = { x1: 484, y1: 412, x2: 541, y2: 432 }; _etRenderOverlays();");
  h.run("_etKey({ key: 'Enter', target: { tagName: 'DIV' }, preventDefault(){} })");
  eq(h.run('_etEntry && _etEntry.el.tagName'), 'TEXTAREA');
  eq(h.run('_etEntry.el.value'), 'Line Amount Excl. VAT');
  eq(h.run('_etEntry.el.style.color'), '#ffffff');
  assert(h.run('_etEntry.el.style.background') !== '#ffffff', 'white on white again');
  h.dispose();
});

test('committing sends ONE edit_block with the ids, text, wrapped lines and box',
     async () => {
  const h = await viewer();
  tableItems(h);
  h.run("_etBlock = { x1: 484, y1: 412, x2: 541, y2: 432 }; _etRenderOverlays();");
  h.run("_etOpenBlockEntry(); _etEntry.el.value = 'Line Amount Excl. GST';");
  h.run("_etEntry.el.onkeydown({ key: 'Enter', shiftKey: false, preventDefault(){} })");
  await h.flush(30);
  eq(blocks(h).length, 1);
  const b = blocks(h)[0].body;
  eq(JSON.stringify(b.ids), JSON.stringify(['a', 'b', 'c', 'd']));
  eq(b.text, 'Line Amount Excl. GST');
  assert(Array.isArray(b.lines) && b.lines.length >= 1, 'no client-side wrap');
  eq(b.lines.join(' '), 'Line Amount Excl. GST', 'wrapping lost words');
  eq(b.box.x1, 484); eq(b.box.y2, 432);
  eq(h.run('_etBlock'), null, 'the box outlived the commit');
  eq(edits(h).length, 0, 'it must not ALSO edit the runs one by one');
  h.dispose();
});

test('a multi-row block keeps its rows and wraps new text', async () => {
  const h = await viewer();
  tableItems(h);
  // The stub measures 7px per character at any size, so a 57pt-wide box holds
  // 8 characters a line — "Line Amount Excl. GST" must break.
  h.run("_etBlock = { x1: 484, y1: 412, x2: 541, y2: 432 }; _etRenderOverlays();");
  h.run("_etOpenBlockEntry(); _etEntry.el.value = 'Line Amount Excl. GST'; _etCommitEntry();");
  await h.flush(30);
  assert(blocks(h)[0].body.lines.length > 1, 'a paragraph came back as one line');
  h.dispose();
});

test('the editor previews the rows\' alignment', async () => {
  const h = await viewer();
  tableItems(h);                     // both rows end at x2 = 539: right-aligned
  h.run("_etBlock = { x1: 484, y1: 412, x2: 541, y2: 432 }; _etRenderOverlays();");
  h.run('_etOpenBlockEntry()');
  eq(h.run('_etEntry.el.style.textAlign'), 'right');
  eq(h.run("_etAlign(_etItems.slice(0, 4), 10.8).align"), 'right');
  // A ragged-right pair (same left edge) stays left.
  eq(h.run(`_etAlign([
    { x: 72, x2: 150, y: 500, y2: 507 }, { x: 72, x2: 110, y: 490, y2: 497 }], 10.8).align`),
     'left');
  h.dispose();
});

test('an unchanged block commits nothing', async () => {
  const h = await viewer();
  tableItems(h);
  h.run("_etBlock = { x1: 484, y1: 412, x2: 541, y2: 432 }; _etRenderOverlays();");
  h.run("_etOpenBlockEntry(); _etCommitEntry();");
  await h.flush(30);
  eq(blocks(h).length, 0, 'an untouched block would still be re-set in the app font');
  h.dispose();
});

test('Escape in the block editor discards the edit AND the box', async () => {
  const h = await viewer();
  tableItems(h);
  h.run("_etBlock = { x1: 484, y1: 412, x2: 541, y2: 432 }; _etRenderOverlays();");
  h.run(`_etOpenBlockEntry(); _etEntry.el.value = 'nope';
         _etEntry.el.onkeydown({ key: 'Escape', preventDefault(){} });`);
  await h.flush(20);
  eq(blocks(h).length, 0);
  eq(h.run('_etEntry'), null);
  eq(h.run('_etBlock'), null);
  h.dispose();
});

test('an empty box says so rather than opening an empty editor', async () => {
  const h = await viewer();
  tableItems(h);
  let msg = null;
  h.ctx.showToast = (m) => { msg = m; };
  h.run("_etBlock = { x1: 10, y1: 10, x2: 20, y2: 20 }; _etOpenBlockEntry();");
  eq(h.run('_etEntry'), null);
  assert(msg, 'no feedback for an empty box');
  h.dispose();
});

test('while the box is up, Del does not delete the selected run', async () => {
  const h = await viewer();
  tableItems(h);
  h.run("_etSelect(_etItems[0]); _etStartBlock(_etItems[0]);");
  h.run("_etKey({ key: 'Delete', target: { tagName: 'DIV' }, preventDefault(){} })");
  await h.flush(20);
  eq(edits(h).length, 0);
  h.dispose();
});

test('closing the panel puts the box away', async () => {
  const h = await viewer();
  tableItems(h);
  h.run("_etStartBlock(_etItems[0]);");
  h.run('toggleRightPanel()');
  eq(h.run('_etBlock'), null);
  h.dispose();
});

test('the panel button needs a selection', async () => {
  const h = await viewer();
  h.run('_etSelect(_etItems[0])');
  eq(h.run("document.getElementById('at-et-block').disabled"), false);
  h.run('_etSelect(null)');
  eq(h.run("document.getElementById('at-et-block').disabled"), true);
  h.dispose();
});

// ── stale ids (found in the review after the merge) ─────────────────────────
// Run ids are POSITIONAL (`txt_12`, `ocr_90`).  After Undo/Redo or a rotate the
// page bytes differ, so an id the Pointer still holds may name a different run
// and an edit through its hit-area would rewrite the wrong words.

/** Wrap the harness's fetch so a test can script one route's answer. */
function answer(h, prefix, fn) {
  const real = h.ctx.apiFetch;
  h.ctx.apiFetch = async (url, opts) => {
    if (!url.startsWith(prefix)) return real(url, opts);
    await real(url, opts);                          // still recorded
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    return { ok: true, status: 200, headers: { get: () => 'application/json' },
             json: async () => fn(body) };
  };
}

test('an edit says which words it means', async () => {
  const h = await viewer();
  h.run("_etOpenEntry(_etItems[0]); _etEntry.el.value = 'Gamma'; _etCommitEntry();");
  await h.flush(20);
  eq(edits(h)[0].body.expect, 'Alpha', 'the server cannot catch a stale id');
  h.dispose();
});

test('a block says which words it means, in order', async () => {
  const h = await viewer();
  tableItems(h);
  h.run("_etBlock = { x1: 484, y1: 412, x2: 541, y2: 432 }; _etRenderOverlays();");
  h.run("_etOpenBlockEntry(); _etEntry.el.value = 'x'; _etCommitEntry();");
  await h.flush(30);
  eq(JSON.stringify(blocks(h)[0].body.expect),
     JSON.stringify(['Line', 'Amount', 'Excl.', 'VAT']));
  h.dispose();
});

test('Undo drops the Pointer\'s ids and reloads them', async () => {
  const h = await viewer();
  const pid = h.run('_etPage');
  answer(h, '/api/viewer/undo', () => ({ page_id: pid, annotations: [], editable: [],
                                         undo_count: 0, redo_count: 1 }));
  tableItems(h);
  h.run("_etStartBlock(_etItems[0]);");
  const before = h.callsTo('/api/viewer/page_elements').length;
  h.run('viewerUndo()');
  await h.flush(30);
  eq(h.run('_etBlock'), null, 'a box over the old ids survived the undo');
  assert(h.callsTo('/api/viewer/page_elements').length > before,
         'the element model was not reloaded after the page changed');
  h.dispose();
});

test('Undo on ANOTHER page leaves the Pointer alone', async () => {
  const h = await viewer();
  answer(h, '/api/viewer/undo', () => ({ page_id: 'sessA-p2', annotations: [],
                                         editable: [], undo_count: 0, redo_count: 1 }));
  const before = h.callsTo('/api/viewer/page_elements').length;
  h.run('viewerUndo()');
  await h.flush(30);
  eq(h.callsTo('/api/viewer/page_elements').length, before);
  h.dispose();
});

test('a failed field move puts the field back', async () => {
  const h = await viewer();
  answer(h, '/api/viewer/update_form_field', () => ({ error: 'nope' }));
  const before = h.callsTo('/api/viewer/form_fields').length;
  h.run("_ffUpdate('sessA-p1', 'f1', { x1: 1, y1: 1, x2: 2, y2: 2 })");
  await h.flush(30);
  assert(h.callsTo('/api/viewer/form_fields').length > before,
         'the overlay kept showing a move the server never made');
  h.dispose();
});

// ── tools, tabs, zoom and pages (user testing, v3.1) ────────────────────────

test('switching tabs puts the armed tool away', async () => {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 1, 'a.pdf') },
                         sessB: { pages: makePages('sessB', 1, 'b.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')"); await h.flush();
  h.run("openNewTab('sessB','')"); await h.flush();
  h.run('rightCollapsed = false;');
  h.run("selectAnnotTool('edittext')"); await h.flush(20);
  eq(h.run('_activeAnnotTool'), 'edittext', 'precondition');
  await h.run('_switchTab(0)'); await h.flush(20);
  eq(h.run('_activeAnnotTool'), null, 'the tool followed the user into another document');
  eq(h.run('_etPage'), null);
  h.dispose();
});

test('a zoom redraws the hit-areas at the new size', async () => {
  const h = await viewer();
  const cssW = () => cssPx(h.run(`(() => { const ov = document.getElementById('et-ov-' + _etPage);
      return ov.children[0].style.cssText; })()`), 'width');
  const w0 = cssW();
  h.run('zoomLevel = zoomLevel * 1.5; redrawAllRendered();');
  const w1 = cssW();
  assert(near(w1, w0 * 1.5, 0.5),
         `hit-area ${w1}px after zooming 1.5x from ${w0}px — clicks would miss the words`);
  h.dispose();
});

test('clicking text on another page moves the Pointer there', async () => {
  const h = await viewer();
  eq(h.run('_etPage'), 'sessA-p1', 'precondition');
  h.run(`_etPageClick({ button: 0, clientX: 5, clientY: 5,
                        target: { closest: () => document.getElementById('page-block-sessA-p2') },
                        preventDefault(){}, stopPropagation(){} })`);
  await h.flush(20);
  eq(h.run('_etPage'), 'sessA-p2', 'the other page was not loaded');
  // …and an edit there reloads THAT page, not the one in view.
  h.run("_etOpenEntry(_etItems[0]); _etEntry.el.value = 'Gamma'; _etCommitEntry();");
  await h.flush(30);
  eq(h.callsTo('/api/viewer/page_elements').slice(-1)[0].body.page_id, 'sessA-p2');
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
