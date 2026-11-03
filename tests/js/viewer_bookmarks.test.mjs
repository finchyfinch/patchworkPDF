/* Regression tests: bulk-deleting bookmarks from the left panel.
 *
 * Merging a stack of documents leaves an outline that is mostly noise, so the
 * bookmark right-click menu grew "Delete selected" and "Delete all".  Both are
 * destructive and there is no undo for bookmarks, which makes three things
 * worth pinning:
 *
 *   • the selection a delete acts on is exactly what the user marked — a
 *     shift-click takes the contiguous block, a ctrl-click toggles one entry,
 *     and neither navigates away mid-selection;
 *   • right-clicking an entry *outside* the selection collapses to that entry,
 *     so the menu never offers to delete a block that has scrolled out of view;
 *   • declining the confirmation sends nothing at all, and ids that no longer
 *     exist can never widen a later delete.
 *
 * These drive the real src/frontend/js/viewer-panel.js over the headless DOM.
 *
 * Run: node tests/js/viewer_bookmarks.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

// ── tiny test runner ─────────────────────────────────────────────────────────
const tests = [];
const test  = (name, fn) => tests.push([name, fn]);

function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`${msg || 'values differ'}: expected ${expected}, got ${actual}`);
  }
}
function deepEq(actual, expected, msg) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${msg || 'values differ'}: expected ${b}, got ${a}`);
}

// ── fixture ──────────────────────────────────────────────────────────────────
// Six bookmarks, one per page, named bm1..bm6 so assertions read plainly.
async function viewerWithBookmarks(n = 6) {
  const h = loadViewer({ sessA: { pages: makePages('sessA', n, 'alpha.pdf') } });
  const toasts = [];
  h.ctx.showToast = (m, type) => toasts.push({ m, type });
  h.run("openNewTab('sessA','')");
  await h.flush();

  // Every bookmark call the panel made, newest last.  `fail` makes the next
  // bookmark request come back as an API error instead of a new outline.
  const sent = [];
  const state = { fail: false };
  h.ctx.apiFetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    if (!url.startsWith('/api/viewer/') || !/bookmark/.test(url)) {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    sent.push(Object.assign({ url }, body));
    if (state.fail) {
      return { ok: true, status: 200, json: async () => ({ error: 'session gone' }) };
    }
    const live = h.run('_bookmarks');
    let out = live;
    if (url.endsWith('/delete_bookmarks')) {
      out = body.all ? [] : live.filter(b => !body.bookmark_ids.includes(b.id));
    } else if (url.endsWith('/delete_bookmark')) {
      out = live.filter(b => b.id !== body.bookmark_id);
    } else if (url.endsWith('/add_bookmark')) {
      out = live.concat([{ id: 'new' + (live.length + 1), title: body.title,
                           page_id: body.page_id, y: body.y, level: 0 }]);
    } else if (url.endsWith('/add_bookmarks')) {
      out = live.concat(body.bookmarks.map((b, i) => Object.assign({ id: 'auto' + i }, b)));
    } else if (url.endsWith('/rename_bookmark')) {
      out = live.map(b => (b.id === body.bookmark_id ? Object.assign({}, b, { title: body.title }) : b));
    } else if (url.endsWith('/move_bookmark')) {
      out = live.slice();
      const i = out.findIndex(b => b.id === body.bookmark_id);
      const j = body.direction === 'up' ? i - 1 : i + 1;
      if (i >= 0 && j >= 0 && j < out.length) { const t = out[i]; out[i] = out[j]; out[j] = t; }
    }
    return { ok: true, status: 200,
             json: async () => ({ bookmarks: out, deleted: live.length - out.length }) };
  };

  h.run(`
    _leftMode  = 'bookmarks';
    _bookmarks = allPages.map((p, i) => ({
      id: 'bm' + (i + 1), title: 'Chapter ' + (i + 1),
      page_id: p.id, y: null, level: 0,
    }));
    renderBookmarks();
  `);
  return Object.assign(h, { toasts, sent, state });
}

// The events have to be built inside the viewer's own vm context, so these
// helpers hand it the source of one rather than an object from out here.
const clickWith = (h, id, mods) =>
  h.run(`bmItemClick({${mods}preventDefault(){},stopPropagation(){}}, '${id}')`);
const click      = (h, id) => clickWith(h, id, '');
const clickShift = (h, id) => clickWith(h, id, 'shiftKey:true,');
const clickCtrl  = (h, id) => clickWith(h, id, 'ctrlKey:true,');
const rightClick = (h, id) =>
  h.run(`showBookmarkCtx({clientX:10,clientY:10,preventDefault(){},stopPropagation(){}}, '${id}')`);

/** The ids the panel currently holds as selected, in list order. */
const selected = (h) => h.run('_bookmarks.filter(b => _bmSelected.has(b.id)).map(b => b.id)');
/** The ids marked `selected` in the rendered markup — what the user actually sees. */
function selectedInMarkup(h) {
  const html = h.doc.getElementById('bookmark-list').innerHTML;
  const out  = [];
  const re   = /<div class="bm-item( selected)?"[\s\S]*?bmItemClick\(event,'([^']+)'\)/g;
  let m;
  while ((m = re.exec(html))) if (m[1]) out.push(m[2]);
  return out;
}

// ── selection ────────────────────────────────────────────────────────────────

test('a plain click navigates and makes that entry the sole selection', async () => {
  const h = await viewerWithBookmarks();
  click(h, 'bm3');
  await h.flush();
  deepEq(selected(h), ['bm3'], 'sole selection');
  deepEq(selectedInMarkup(h), ['bm3'], 'markup marks the clicked entry');
  eq(h.run('_bmAnchorId'), 'bm3', 'the click sets the range anchor');
  eq(h.run('currentIdx'), 2, 'and it still navigates to the bookmarked page');
});

test('shift-click takes the whole block between the anchor and the target', async () => {
  const h = await viewerWithBookmarks();
  click(h, 'bm2');
  await h.flush();
  clickShift(h, 'bm5');
  deepEq(selected(h), ['bm2', 'bm3', 'bm4', 'bm5'], 'contiguous block');
  deepEq(selectedInMarkup(h), ['bm2', 'bm3', 'bm4', 'bm5'], 'markup matches');
});

test('shift-click backwards takes the same block, and does not navigate', async () => {
  const h = await viewerWithBookmarks();
  click(h, 'bm5');
  await h.flush();
  const idxBefore = h.run('currentIdx');
  clickShift(h, 'bm2');
  await h.flush();
  deepEq(selected(h), ['bm2', 'bm3', 'bm4', 'bm5'], 'block regardless of direction');
  eq(h.run('currentIdx'), idxBefore, 'shift-click leaves the page where it was');
});

test('shift-click with no anchor yet selects just that entry', async () => {
  const h = await viewerWithBookmarks();
  clickShift(h, 'bm4');
  deepEq(selected(h), ['bm4'], 'no anchor → no runaway range');
  eq(h.run('_bmAnchorId'), 'bm4', 'and it becomes the anchor for the next one');
});

test('ctrl-click toggles one entry in and out without navigating', async () => {
  const h = await viewerWithBookmarks();
  click(h, 'bm1');
  await h.flush();
  const idxBefore = h.run('currentIdx');
  clickCtrl(h, 'bm4');
  clickCtrl(h, 'bm6');
  await h.flush();
  deepEq(selected(h), ['bm1', 'bm4', 'bm6'], 'three discontiguous entries');
  eq(h.run('currentIdx'), idxBefore, 'ctrl-click leaves the page where it was');

  clickCtrl(h, 'bm4');
  deepEq(selected(h), ['bm1', 'bm6'], 'a second ctrl-click removes it again');
});

// ── the menu acts on what the user can see ───────────────────────────────────

test('right-clicking inside the selection keeps it; outside collapses to one', async () => {
  const h = await viewerWithBookmarks();
  click(h, 'bm2');
  await h.flush();
  clickShift(h, 'bm4');

  rightClick(h, 'bm3');
  deepEq(selected(h), ['bm2', 'bm3', 'bm4'], 'right-click within the block keeps it');
  eq(h.doc.getElementById('bmctx-delsel-label').textContent,
     'bmctx.delete_selected', 'the label is re-translated each time the menu opens');
  eq(h.doc.getElementById('bmctx-delete-selected').style.pointerEvents, '',
     '"Delete selected" is live with a block selected');

  rightClick(h, 'bm6');
  deepEq(selected(h), ['bm6'], 'right-click outside the block collapses to that entry');
  eq(h.doc.getElementById('bmctx-delete-selected').style.pointerEvents, 'none',
     '"Delete selected" is greyed out below two entries');
});

test('"Delete all" is greyed out when there are no bookmarks left', async () => {
  const h = await viewerWithBookmarks();
  h.run('_bookmarks = []; renderBookmarks();');
  rightClick(h, 'bm1');
  eq(h.doc.getElementById('bmctx-delete-all').style.pointerEvents, 'none',
     'nothing to delete');
});

// ── deleting ─────────────────────────────────────────────────────────────────

test('"Delete selected" sends exactly the marked ids and clears the selection', async () => {
  const h = await viewerWithBookmarks();
  h.run('showConfirm = () => Promise.resolve(true);');
  click(h, 'bm2');
  await h.flush();
  clickShift(h, 'bm4');
  h.run('deleteSelectedBookmarksCtx()');
  await h.flush();

  eq(h.sent.length, 1, 'one request');
  deepEq(h.sent[0].bookmark_ids, ['bm2', 'bm3', 'bm4'], 'exactly the marked block');
  eq(h.sent[0].all, false, 'not a delete-all');
  deepEq(h.run('_bookmarks.map(b => b.id)'), ['bm1', 'bm5', 'bm6'], 'the rest survive');
  eq(h.run('_bmSelected.size'), 0, 'selection cleared after the delete');
  eq(h.run('_bmAnchorId'), null, 'anchor cleared with it');
});

test('"Delete all" sends the all flag rather than a list of ids', async () => {
  const h = await viewerWithBookmarks();
  h.run('showConfirm = () => Promise.resolve(true);');
  rightClick(h, 'bm3');
  h.run('deleteAllBookmarksCtx()');
  await h.flush();

  eq(h.sent.length, 1, 'one request');
  eq(h.sent[0].all, true, 'the server clears the list itself');
  eq(h.run('_bookmarks.length'), 0, 'panel empty afterwards');
  assert(h.doc.getElementById('bookmark-list').innerHTML.includes('bm-empty'),
         'the empty-state hint comes back');
});

test('declining either confirmation sends nothing and changes nothing', async () => {
  const h = await viewerWithBookmarks();
  h.run('showConfirm = () => Promise.resolve(false);');
  click(h, 'bm2');
  await h.flush();
  clickShift(h, 'bm4');

  h.run('deleteSelectedBookmarksCtx()');
  await h.flush();
  h.run('deleteAllBookmarksCtx()');
  await h.flush();

  eq(h.sent.length, 0, 'cancel means no request at all');
  eq(h.run('_bookmarks.length'), 6, 'every bookmark still there');
  deepEq(selected(h), ['bm2', 'bm3', 'bm4'], 'and the selection is left intact');
});

test('a bookmark that no longer exists cannot widen a later delete', async () => {
  const h = await viewerWithBookmarks();
  h.run('showConfirm = () => Promise.resolve(true);');
  click(h, 'bm1');
  await h.flush();
  clickShift(h, 'bm4');

  // Another path (a reload, a single delete elsewhere) drops two underneath us.
  h.run("_bookmarks = _bookmarks.filter(b => b.id !== 'bm2' && b.id !== 'bm3'); renderBookmarks();");
  // The Set itself has to shed them, not just the views built from it: the
  // count on "Delete selected" is read straight off _bmSelected.size.
  deepEq(h.run('[..._bmSelected].sort()'), ['bm1', 'bm4'],
         'the vanished ids are pruned out of the selection itself');
  rightClick(h, 'bm4');
  eq(h.doc.getElementById('bmctx-delete-selected').style.pointerEvents, '',
     'the menu still counts the two that remain, not the original four');

  h.run('deleteSelectedBookmarksCtx()');
  await h.flush();
  deepEq(h.sent[0].bookmark_ids, ['bm1', 'bm4'], 'only ids the server still has are sent');
});

test('a read-only document refuses both bulk deletes', async () => {
  const h = await viewerWithBookmarks();
  h.run('showConfirm = () => Promise.resolve(true);');
  h.run('_applyRestrictedMode(true)');
  click(h, 'bm2');
  await h.flush();
  clickShift(h, 'bm4');

  h.run('deleteSelectedBookmarksCtx()');
  h.run('deleteAllBookmarksCtx()');
  await h.flush();
  eq(h.sent.length, 0, 'no request from a restricted document');
  eq(h.run('_bookmarks.length'), 6, 'nothing deleted');
});

test('switching documents drops the selection with the bookmarks', async () => {
  const h = await viewerWithBookmarks();
  click(h, 'bm2');
  await h.flush();
  clickShift(h, 'bm4');
  eq(h.run('_bmSelected.size'), 3, 'selected before the switch');

  h.run('_resetViewerState()');
  eq(h.run('_bmSelected.size'), 0, 'a fresh document starts with nothing selected');
  eq(h.run('_bmAnchorId'), null, 'and no anchor');
});

// ── the unsaved-changes flag ─────────────────────────────────────────────────
// Bookmarks are only written into the PDF at save time (entry.py
// _write_outlines).  An edit that does not flag the document dirty is offered
// no save prompt when the tab closes, and goes in the bin without a word.

/** Record pwSetDocDirty calls, and return a reader for them. */
function watchDirty(h) {
  h.run('var __dirty = []; pwSetDocDirty = (v) => { __dirty.push(v !== false); _pwDocDirty = (v !== false); };');
  return () => h.run('__dirty');
}

test('every edit to the outline flags the document dirty', async () => {
  const h = await viewerWithBookmarks();
  h.run('showConfirm = () => Promise.resolve(true);');
  const dirty = watchDirty(h);

  const edits = {
    add:        "_addBookmark(allPages[0].id, null, 'Added')",
    rename:     "_renameBookmark('bm1', 'Renamed')",
    del:        "deleteBookmark('bm6')",
    delBulk:    'deleteAllBookmarksCtx()',
  };
  for (const [name, expr] of Object.entries(edits)) {
    h.run('__dirty.length = 0');
    h.run(expr);
    await h.flush();
    deepEq(dirty(), [true], name + ' flags the document dirty');
    // Put the outline back for the next one.
    h.run(`_bookmarks = [1,2,3,4,5,6].map(i => ({id:'bm'+i, title:'Chapter '+i,
             page_id: allPages[i-1].id, y:null, level:0})); renderBookmarks();`);
  }

  // Move goes through the context menu, which is where it reads its target.
  h.run('__dirty.length = 0');
  rightClick(h, 'bm3');
  h.run("moveBookmark('up')");
  await h.flush();
  deepEq(dirty(), [true], 'move flags the document dirty');
});

test('committing auto-detected bookmarks flags the document dirty', async () => {
  const h = await viewerWithBookmarks();
  const dirty = watchDirty(h);
  // Stand in for the modal's ticked checkboxes.
  h.run(`_abProposed = [{title:'Intro', page_id: allPages[0].id, y:null, level:0},
                        {title:'Body',  page_id: allPages[1].id, y:null, level:1}];`);
  h.doc.querySelectorAll = (sel) => (/ab-cb/.test(sel)
    ? [{ dataset: { i: '0' } }, { dataset: { i: '1' } }] : []);

  h.run('confirmAutoBookmarks()');
  await h.flush();
  assert(h.sent.some(r => r.url.endsWith('/add_bookmarks')), 'the batch was committed');
  deepEq(dirty(), [true], 'a committed batch counts as a change');
});

test('merely loading the outline does not flag the document dirty', async () => {
  const h = await viewerWithBookmarks();
  const dirty = watchDirty(h);
  h.run('loadBookmarks()');
  await h.flush();
  deepEq(dirty(), [], 'reading what is already in the file changes nothing');

  // Nor does selecting, or opening the menu over a selection.
  click(h, 'bm2');
  await h.flush();
  clickShift(h, 'bm4');
  rightClick(h, 'bm3');
  deepEq(dirty(), [], 'and neither does selecting');
});

test('a failed delete neither blanks the panel nor claims a change', async () => {
  const h = await viewerWithBookmarks();
  const dirty = watchDirty(h);
  h.state.fail = true;

  h.run("deleteBookmark('bm3')");
  await h.flush();
  eq(h.run('_bookmarks.length'), 6, 'the outline is left alone on an API error');
  deepEq(dirty(), [], 'a change that did not happen is not flagged');
  assert(h.toasts.some(t => t.type === 'error'), 'and the failure is reported');
});

// ── run ──────────────────────────────────────────────────────────────────────
let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log('  ok   ' + name);
  } catch (e) {
    failed++;
    console.log('  FAIL ' + name + '\n       ' + e.message);
  }
}
console.log(failed ? `\n${failed} of ${tests.length} failed` : `\n${tests.length} passed`);
process.exit(failed ? 1 : 0);
