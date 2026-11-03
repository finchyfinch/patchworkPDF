/* Regression tests: the right panel's tabs, style rows and Comments list (v3.2).
 *
 * Before v3.2 the shared Font / Text / Line / Fill rows were always on show,
 * whatever tool was armed — so arming Add text (which has its own font
 * controls) put TWO Font rows in the panel, and Edit text / Crop / Redact sat
 * above Line and Fill controls that did nothing.  And the Comments list showed
 * only sticky notes and text comments, so a reviewer's highlight or cloud was
 * invisible there.
 *
 * What has to hold:
 *
 *   1. The style rows follow the armed tool — or, with nothing armed, the
 *      annotation being edited — and are hidden otherwise.
 *   2. Every listed item (comment or markup) gets a card, labelled by type.
 *   3. Three tabs — Comment, Edit, Fill & Sign — and every tool on exactly one.
 *      Arming a tool brings its tab forward; leaving a tab puts its tool away;
 *      the comments list shows on the Comment tab only.
 *   4. Review threads: each card shows who and when, its replies and status;
 *      replies and statuses post through the annotation queue as the reviewer;
 *      filters narrow the list; a lifted shape keeps its thread, a copy does not.
 *   5. Markup is listed but is not a comment: the overlay gives it no hit area
 *      or popup (lift-to-edit owns its clicks), only the ring shown when its
 *      card is clicked.
 *
 * Run: node tests/js/viewer_comment_panel.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || 'not equal'}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}

async function viewer() {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 1, 'doc.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')");
  await h.flush();
  await h.renderPages(0, 0);
  return h;
}

const shown = (h, id) => h.run(`document.getElementById('${id}').style.display`) !== 'none';
const rows  = (h) => ({
  text: shown(h, 'at-text-props'),
  line: shown(h, 'at-line-props'),
  fill: shown(h, 'at-fill-props'),
});

// ── style rows ──────────────────────────────────────────────────────────────

test('with nothing armed the style rows are hidden and the hint shows', async () => {
  const h = await viewer();
  h.run("setRightMode('comment')");
  eq(JSON.stringify(rows(h)), JSON.stringify({ text: false, line: false, fill: false }));
  assert(shown(h, 'at-idle-hint'), 'the idle hint should explain what to do');
  h.dispose();
});

test('each drawing tool shows only the rows it uses', async () => {
  const h = await viewer();
  const want = {
    text:    { text: true,  line: true,  fill: false },   // Box borrows Line
    rect:    { text: false, line: true,  fill: true  },
    ellipse: { text: false, line: true,  fill: true  },
    arrow:   { text: false, line: true,  fill: false },
    cloud:   { text: false, line: true,  fill: false },
    sticky:  { text: false, line: false, fill: false },
  };
  for (const [tool, r] of Object.entries(want)) {
    h.run(`selectAnnotTool('${tool}')`);
    eq(JSON.stringify(rows(h)), JSON.stringify(r), tool);
    assert(!shown(h, 'at-idle-hint'), `${tool}: hint should hide while a tool is armed`);
  }
  h.dispose();
});

test('page tools show none of the shared rows', async () => {
  const h = await viewer();
  for (const tool of ['pagetext', 'edittext', 'crop', 'redact', 'formfield']) {
    h.run(`selectAnnotTool('${tool}')`);
    eq(JSON.stringify(rows(h)), JSON.stringify({ text: false, line: false, fill: false }), tool);
  }
  h.dispose();
});

test('a highlight has a colour but no weight or dash style', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('highlight')");
  assert(shown(h, 'at-line-props'));
  assert(!shown(h, 'at-line-weight-fld'), 'weight means nothing for a highlight');
  assert(!shown(h, 'at-line-style-fld'), 'nor does dash style');
  h.run("selectAnnotTool('line')");
  assert(shown(h, 'at-line-weight-fld') && shown(h, 'at-line-style-fld'),
         'switching away must bring them back');
  h.dispose();
});

test('disarming hides the rows again', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('rect')");
  h.run("selectAnnotTool('rect')");     // toggle off
  eq(JSON.stringify(rows(h)), JSON.stringify({ text: false, line: false, fill: false }));
  h.dispose();
});

test('with nothing armed, the rows follow the annotation being edited', async () => {
  const h = await viewer();
  h.run("_draft = { tool: 'ellipse' }; _refreshStyleRows()");
  eq(JSON.stringify(rows(h)), JSON.stringify({ text: false, line: true, fill: true }));
  h.run('_draft = null; _refreshStyleRows()');
  eq(JSON.stringify(rows(h)), JSON.stringify({ text: false, line: false, fill: false }));
  h.dispose();
});

// ── the three tabs ──────────────────────────────────────────────────────────

const mode = (h) => h.run('_rightMode');

test('the panel opens on the Comment tab, with the comments list showing', async () => {
  const h = await viewer();
  eq(mode(h), 'comment');
  h.run("setRightMode('comment')");
  assert(shown(h, 'rp-group-comment') && shown(h, 'rp-comments'));
  assert(!shown(h, 'rp-group-edit') && !shown(h, 'rp-group-sign'));
  h.dispose();
});

test('the comments list shows on the Comment tab only', async () => {
  const h = await viewer();
  for (const [m, list] of [['edit', false], ['sign', false], ['comment', true]]) {
    h.run(`setRightMode('${m}')`);
    eq(shown(h, 'rp-comments'), list, m);
    assert(h.run(`document.getElementById('rp-seg-${m}').classList.contains('active')`),
           `${m} tab button should be marked active`);
  }
  h.dispose();
});

test('arming a tool brings its tab forward', async () => {
  const h = await viewer();
  for (const [tool, tab] of [['crop', 'edit'], ['signature', 'sign'],
                             ['highlight', 'comment'], ['pagetext', 'edit'],
                             ['formfield', 'sign'], ['sticky', 'comment']]) {
    h.run(`selectAnnotTool('${tool}')`);
    eq(mode(h), tab, tool);
    assert(shown(h, 'rp-group-' + tab), `${tool}: its tab's tools should show`);
  }
  h.dispose();
});

test('switching tab puts away a tool that lives on another tab', async () => {
  const h = await viewer();
  h.run("selectAnnotTool('rect')");
  h.run("setRightMode('edit')");
  eq(h.run('_activeAnnotTool'), null, 'a hidden tool must not stay armed');
  h.run("selectAnnotTool('redact')");
  h.run("setRightMode('edit')");
  eq(h.run('_activeAnnotTool'), 'redact', 'staying on the tab keeps it');
  h.dispose();
});

test('an annotation being edited brings forward the tab that owns it', async () => {
  const h = await viewer();
  h.run("setRightMode('comment'); _draft = { tool: 'image' }; _updateAnnotDeleteBtn()");
  eq(mode(h), 'edit', 'an image is on the Edit tab');
  h.run("_draft = null; _updateAnnotDeleteBtn()");
  h.dispose();
});

test('the pre-3.2 mode names still work', async () => {
  const h = await viewer();
  h.run("setRightMode('edit'); setRightMode('annots')");
  eq(mode(h), 'comment', "'annots' was the list, which is on the Comment tab");
  h.run("setRightMode('sign'); setRightMode('tools')");
  eq(mode(h), 'sign', "'tools' meant 'not the list' — leave the tab alone");
  h.dispose();
});

test('the idle hint belongs to the Comment tab', async () => {
  const h = await viewer();
  h.run("setRightMode('comment')");
  assert(shown(h, 'at-idle-hint'));
  h.run("setRightMode('edit')");
  assert(!shown(h, 'at-idle-hint'), 'it talks about comments, so not on Edit');
  h.dispose();
});

test('tool hints go through the locale files', async () => {
  const h = await viewer();
  const toasts = [];
  h.ctx.showToast = (m, k) => toasts.push([m, k]);
  h.run("selectAnnotTool('text')");
  eq(toasts.pop()[0], 'toast.arm_draw', 'arming toasts are translated keys now');
  h.run("selectAnnotTool('crop')");
  eq(toasts.pop()[0], 'toast.arm_crop');
  h.dispose();
});

// ── the Comments list ───────────────────────────────────────────────────────

const LISTED = JSON.stringify([
  { idx: 0,    x: 10, y: 10, w: 18, h: 18, text: 'Check this', author: 'GF',
    color: [1, 0.92, 0], subtype: 'Text', markup: false },
  { idx: null, x: 50, y: 50, w: 80, h: 40, text: '', author: '',
    color: [1, 0, 0], subtype: 'Square', markup: true },
  { idx: null, x: 50, y: 90, w: 80, h: 2, text: '', author: '',
    color: [1, 0, 0], subtype: 'Line', kind: 'darrow', markup: true },
  { idx: 1,    x: 90, y: 90, w: 80, h: 20, text: '', author: '',
    color: [1, 1, 1], subtype: 'FreeText', markup: false },
]);

test('every listed item gets a card, labelled by its type', async () => {
  const h = await viewer();
  h.run(`allAnnotations = { 'sessA-p1': ${LISTED} }; renderComments()`);
  const html = h.run("document.getElementById('comment-list').innerHTML");
  eq((html.match(/class="comment-card/g) || []).length, 4, 'one card per item');
  for (const k of ['comments.type_sticky', 'comments.type_rect',
                   'comments.type_darrow', 'comments.type_text']) {
    assert(html.includes(k), `missing label ${k}`);
  }
  eq(h.run("document.getElementById('rp-badge').textContent"), 4);
  h.dispose();
});

test('a markup with no text shows none; an empty comment says so', async () => {
  const h = await viewer();
  h.run(`allAnnotations = { 'sessA-p1': ${LISTED} }; renderComments()`);
  const html = h.run("document.getElementById('comment-list').innerHTML");
  eq((html.match(/comments\.no_text/g) || []).length, 1,
     'only the empty FreeText comment, not the two shapes');
  h.dispose();
});

test('an empty list says so and hides the badge', async () => {
  const h = await viewer();
  h.run('allAnnotations = {}; renderComments()');
  assert(h.run("document.getElementById('comment-list').innerHTML").includes('comments.empty'));
  eq(h.run("document.getElementById('rp-badge').style.display"), 'none');
  h.dispose();
});

// ── review threads: who, when, replies, status, filters ─────────────────────

const THREADED = JSON.stringify([
  { idx: null, annot_idx: 0, x: 50, y: 50, w: 80, h: 40, text: '', author: 'Ana',
    created: '2026-09-27T10:00:00+00:00', modified: '2026-09-27T10:00:00+00:00',
    color: [1, 0, 0], subtype: 'Square', markup: true,
    replies: [{ annot_idx: 2, text: 'Why here?', author: 'Ben',
                created: '2026-09-27T11:00:00+00:00' }],
    status: { annot_idx: 3, state: 'Accepted', author: 'Cy',
              modified: '2026-09-27T12:00:00+00:00' } },
  { idx: 0, annot_idx: 1, x: 10, y: 10, w: 18, h: 18, text: 'Check totals', author: 'Ben',
    color: [1, 0.92, 0], subtype: 'Text', markup: false, replies: [], status: null },
]);
const listHtml = (h) => h.run("document.getElementById('comment-list').innerHTML");
const cards = (h) => (listHtml(h).match(/class="comment-card/g) || []).length;

test('a card shows its author, its replies and its status', async () => {
  const h = await viewer();
  h.run(`allAnnotations = { 'sessA-p1': ${THREADED} }; renderComments()`);
  const html = listHtml(h);
  assert(html.includes('Ana'), 'author');
  assert(html.includes('Why here?') && html.includes('Ben'), 'reply and its author');
  assert(html.includes('comments.status_accepted'), 'status chip');
  assert(html.includes('st-Accepted'), 'status styled by state');
  h.dispose();
});

test('filters narrow the list and say how much is hidden', async () => {
  const h = await viewer();
  h.run(`allAnnotations = { 'sessA-p1': ${THREADED} }; renderComments()`);
  eq(cards(h), 2);
  h.run("setCommentFilter('author', 'Ben')");
  eq(cards(h), 1, "only Ben's own item: his reply does not make Ana's shape his");
  assert(listHtml(h).includes('Check totals'));
  assert(h.run("document.getElementById('comment-filters').innerHTML").includes('comments.showing'));
  h.run("setCommentFilter('author', ''); setCommentFilter('status', 'Accepted')");
  eq(cards(h), 1);
  h.run("setCommentFilter('status', ''); setCommentFilter('type', 'Text')");
  eq(cards(h), 1);
  eq(h.run("document.getElementById('rp-badge').textContent"), 2, 'the badge counts everything');
  h.dispose();
});

test('a filter on something no longer in the document is dropped', async () => {
  const h = await viewer();
  h.run(`allAnnotations = { 'sessA-p1': ${THREADED} }; setCommentFilter('author', 'Ana')`);
  eq(cards(h), 1);
  h.run(`allAnnotations = { 'sessA-p1': [${JSON.stringify(JSON.parse(THREADED)[1])}] }; renderComments()`);
  eq(h.run('_commentFilter.author'), '', 'a filter nobody can see would leave the list looking empty');
  eq(cards(h), 1);
  h.dispose();
});

test('a reply is posted through the queue, as the reviewer', async () => {
  const h = await viewer();
  h.run(`allAnnotations = { 'sessA-p1': ${THREADED} }; _reviewerName = 'Dee'`);
  h.run("commentReplyStart('sessA-p1', 0)");
  assert(listHtml(h).includes('comment-reply-text'), 'the reply box opens under the card');
  h.run("_replyDraft = '  Agreed  '; commentReplyPost()");
  await h.flush(20);
  const calls = h.callsTo('/api/viewer/comment_thread');
  eq(calls.length, 1);
  const b = calls[0].body;
  eq(b.action, 'reply'); eq(b.annot_idx, 0); eq(b.text, 'Agreed'); eq(b.author, 'Dee');
  h.dispose();
});

test('an empty reply sends nothing', async () => {
  const h = await viewer();
  h.ctx.showToast = () => {};
  h.run(`allAnnotations = { 'sessA-p1': ${THREADED} }`);
  h.run("commentReplyStart('sessA-p1', 0); _replyDraft = '   '; commentReplyPost()");
  await h.flush(10);
  eq(h.callsTo('/api/viewer/comment_thread').length, 0);
  h.dispose();
});

test('a half-typed reply survives the list being re-rendered', async () => {
  const h = await viewer();
  h.run(`allAnnotations = { 'sessA-p1': ${THREADED} }; commentReplyStart('sessA-p1', 0)`);
  h.run("_replyDraft = 'half a thought'; renderComments()");
  eq(h.run("document.getElementById('comment-reply-text').value"), 'half a thought');
  h.dispose();
});

test('setting a status posts it', async () => {
  const h = await viewer();
  h.run(`allAnnotations = { 'sessA-p1': ${THREADED} }; _reviewerName = 'Dee'`);
  h.run("commentSetStatus('sessA-p1', 1, 'Rejected')");
  await h.flush(20);
  const b = h.callsTo('/api/viewer/comment_thread')[0].body;
  eq(b.action, 'status'); eq(b.state, 'Rejected'); eq(b.annot_idx, 1); eq(b.author, 'Dee');
  h.dispose();
});

test('a read-only document lists its comments but takes no replies', async () => {
  const h = await viewer();
  h.ctx.showToast = () => {};
  h.run(`allAnnotations = { 'sessA-p1': ${THREADED} }; _applyRestrictedMode(true)`);
  h.run("commentReplyStart('sessA-p1', 0); commentSetStatus('sessA-p1', 0, 'Accepted')");
  await h.flush(10);
  eq(h.callsTo('/api/viewer/comment_thread').length, 0);
  eq(cards(h), 2, 'the list itself stays readable');
  h.dispose();
});

test('what you draw is authored by you; a lifted shape keeps its author, date and thread', async () => {
  const h = await viewer();
  h.run("_reviewerName = 'Dee'");
  const author = h.run(`(() => { const st = _pageBlocks.get('sessA-p1');
    return _makeDraftFrom({ tool: 'rect', page_id: 'sessA-p1', st, block: st.block, isLine: false }).author; })()`);
  eq(author, 'Dee');
  const body = h.run(`(() => { const st = _pageBlocks.get('sessA-p1');
    return _draftToAddBody({ tool: 'rect', st, scale: 1, x: 0, y: 0, w: 20, h: 20,
      style: _toolStyle(), author: 'Ana', created: '2026-01-01T00:00:00',
      replies: [{ text: 'r', author: 'Ben' }] }); })()`);
  eq(body.author, 'Ana');
  eq(body.created, '2026-01-01T00:00:00');
  eq(body.replies.length, 1);
  h.dispose();
});

test("a copied shape does not take the original's thread with it", async () => {
  const h = await viewer();
  h.ctx.showToast = () => {};
  h.run(`(() => { const st = _pageBlocks.get('sessA-p1');
    _draft = { tool: 'rect', st, scale: 1, x: 0, y: 0, w: 20, h: 20, style: _toolStyle(),
               author: 'Ana', created: '2026-01-01T00:00:00', replies: [{ text: 'r' }] };
    _copyLiveDraft(); _draft = null; })()`);
  const item = h.run('_annotClipboard.items[0]');
  assert(!('replies' in item) && !('created' in item), JSON.stringify(item));
  h.dispose();
});

// ── the overlay ─────────────────────────────────────────────────────────────

test('markup gets no hit area on the page, only a ring when shown', async () => {
  const h = await viewer();
  const count = () => h.run("_pageBlocks.get('sessA-p1').annotOv.children.length");
  // The DOM stub has no firstChild, so drawPageAnnotations cannot clear the
  // overlay itself here — empty it before each draw.
  const clear = () => h.run(`(() => { const ov = _pageBlocks.get('sessA-p1').annotOv;
                                     while (ov.children.length) ov.removeChild(ov.children[0]); })()`);
  const markupOnly = JSON.stringify(JSON.parse(LISTED).filter(a => a.markup));
  clear(); h.run(`allAnnotations = { 'sessA-p1': ${markupOnly} }; drawPageAnnotations('sessA-p1')`);
  eq(count(), 0, 'a hit area would steal the click lift-to-edit needs');
  clear(); h.run(`_annotHighlight = { page_id: 'sessA-p1', annIdx: 0 }; drawPageAnnotations('sessA-p1')`);
  eq(count(), 1, 'the card click should ring it');
  clear(); h.run(`_annotHighlight = null; allAnnotations = { 'sessA-p1': ${LISTED} };
         drawPageAnnotations('sessA-p1')`);
  eq(count(), 2, 'the two comments keep their popups; the two shapes add nothing');
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
