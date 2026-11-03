/* Regression tests: what makes a document "unsaved", and what makes it saved again.
 *
 * The report these come from: two scanned PDFs opened, edited, Applied and
 * Saved from the EDITOR toolbar — then closing the app still warned about
 * unsaved changes, every launch, forever.  Nothing was unsaved; the flag simply
 * never came back down.
 *
 * Two halves of one mechanism, and each half was incomplete:
 *
 *   • Clearing.  Only the viewer's Save called pwSetDocDirty(false) /
 *     pwRecoveryClear.  Layout's Save wrote the same bytes to
 *     the same disk and told nobody, so the document stayed flagged.  Worse, it
 *     stayed flagged PER TAB: _saveActiveTabMeta parks the live flag against the
 *     tab you are leaving, so one editor-save baked a permanent phantom into
 *     that tab, and the window guard (which counts background tabs) went on
 *     warning about it no matter what the user did afterwards.
 *
 *   • Setting.  The mirror-image fault, silent instead of noisy: Layout's
 *     reorder / rotate / delete / compress / insert / header-footer, the
 *     viewer's page-rail menu, form filling, document properties, the save
 *     password, and the editor's form fields ALL changed the document and none
 *     of them flagged it.  Close the tab and the work went without a word.
 *
 * So the rule these pin is symmetric, and it is the reason the suite loads the
 * real shared.js next to the real panels rather than a shim of either: every
 * change flags the document, and every full-document save — from whichever
 * panel — unflags it and drops the crash-recovery snapshot.
 *
 * Run: node tests/js/doc_dirty.test.mjs
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

// ── fixtures ─────────────────────────────────────────────────────────────────
/** One document open in the viewer, with the real shared.js + all three panels
 *  and a native bridge — i.e. the app the report came from. */
async function app(opts) {
  const h = loadViewer({
    sessA: { pages: makePages('sessA', 4, 'alpha.pdf') },
    sessB: { pages: makePages('sessB', 3, 'beta.pdf') },
  }, Object.assign({ shared: true, layout: true, native: true }, opts));
  h.run('showConfirm = () => Promise.resolve(true);');
  h.run("openNewTab('sessA','')");
  await h.flush();
  return h;
}

const dirty    = (h) => h.run('pwIsDocDirty()');
const unsaved  = (h) => h.run('pwHasUnsavedWork()');
/** The last value the page pushed to the native close guard (entry.py reads
 *  nothing else), or null if it never spoke. */
const pushed   = (h) => (h.bridge.unsaved.length ? h.bridge.unsaved[h.bridge.unsaved.length - 1] : null);
const snapshotDropped = (h) => h.callsTo('/api/recovery/clear').length > 0;

/** Point Layout at the open document with every page checked (its default). */
function inLayout(h, sessionId, ids) {
  h.run(`sessionId = ${JSON.stringify(sessionId)};
         pages = ${JSON.stringify(ids.map((id, i) => ({
           id, filename: 'alpha.pdf', page_num: i + 1, total_pages: ids.length,
           rotation: 0, thumbnail: '', width_pts: 612, height_pts: 792 })))};
         selectedIds = new Set(pages.map(p => p.id));
         contextPageId = pages.length ? pages[0].id : null;
         renderGrid = () => {};`);
}

// ═══ 1. Every panel's Save clears the flag ═══════════════════════════════════

test('the viewer Save clears the flag and drops the snapshot', async () => {
  const h = await app();
  h.run('pwSetDocDirty(true)');
  await h.run('viewerSaveToFile()'); await h.flush();
  eq(dirty(h), false, 'the document is no longer unsaved');
  assert(snapshotDropped(h), 'and the crash-recovery snapshot went with it');
});

test('the LAYOUT Save clears the flag when the whole document is written', async () => {
  const h = await app();
  inLayout(h, 'sessA', ['sessA-p1', 'sessA-p2', 'sessA-p3', 'sessA-p4']);
  h.run('pwSetDocDirty(true)');
  await h.run('saveDocument()'); await h.flush();
  eq(dirty(h), false, 'every page was written, so the document is on disk');
  assert(snapshotDropped(h), 'and the snapshot is dropped');
});

test('exporting a SUBSET of pages does not count as saving the document', async () => {
  // Saving the two checked pages writes a different document to a different
  // file; this one is exactly as unsaved as it was.  Clearing here would license
  // throwing the other pages away without a word.
  const h = await app();
  inLayout(h, 'sessA', ['sessA-p1', 'sessA-p2', 'sessA-p3', 'sessA-p4']);
  h.run("selectedIds = new Set(['sessA-p1','sessA-p2']);");
  h.run('pwSetDocDirty(true)');
  await h.run('saveDocument()'); await h.flush();
  eq(dirty(h), true, 'the document still holds unsaved changes');
  eq(snapshotDropped(h), false, 'so its crash-recovery snapshot must stay');
});

// ═══ 2. The reported scenario, end to end ════════════════════════════════════

test('two documents edited and saved close silently', async () => {
  const h = await app();
  h.run('pwSetDocDirty(true)');
  await h.run('viewerSaveToFile()'); await h.flush();

  h.run("openNewTab('sessB','')"); await h.flush();
  h.run('pwSetDocDirty(true)');
  await h.run('viewerSaveToFile()'); await h.flush();

  eq(dirty(h), false, 'B is saved');
  eq(h.run('_tabs[0].dirty === true'), false,
     "A's tab did not have a phantom unsaved flag parked on it");
  eq(unsaved(h), false, 'so the window holds no unsaved work at all');
  eq(pushed(h), false, 'and that is what the native close guard was told');
});

test('a genuinely unsaved second tab still warns after the first one is saved', async () => {
  // The other side of the same coin: clearing must not over-reach.
  const h = await app();
  h.run('pwSetDocDirty(true)');            // A is edited and NOT saved
  h.run("openNewTab('sessB','')"); await h.flush();
  await h.run('viewerSaveToFile()'); await h.flush();   // B is saved

  eq(dirty(h), false, 'B itself is clean');
  eq(h.run('_tabs[0].dirty === true'), true, "but A's edit is still parked on its tab");
  eq(unsaved(h), true, 'so closing the window must still ask');
  eq(pushed(h), true, 'and the close guard was told so');
});

// ═══ 3. Every change flags the document ══════════════════════════════════════

test('the viewer page rail flags the document on rotate, delete and insert', async () => {
  for (const [label, call] of [
    ['rotate',       "pgCtxRotate(90)"],
    ['delete',       "pgCtxDelete()"],
    ['insert blank', "pgCtxInsertBlank(false)"],
  ]) {
    const h = await app();
    h.run("_ctxPageId = 'sessA-p2';");
    eq(dirty(h), false, `${label}: clean to begin with`);
    await h.run(call); await h.flush();
    eq(dirty(h), true, `${label} from the page rail is an unsaved change`);
  }
});

test('filling a form field flags the document', async () => {
  const h = await app();
  h.run("_ffPending.f1 = 'typed';");   // the pending map is a const the UI mutates
  await h.run('_flushFF()'); await h.flush();
  eq(dirty(h), true, 'filled values only reach the PDF at save time');
});

test('document properties and the save password flag the document', async () => {
  let h = await app();
  await h.run('saveDocProperties()'); await h.flush();
  eq(dirty(h), true, 'metadata is written at save time, so the session differs from disk');

  h = await app();
  h.run("document.getElementById('pp-password').value = 'secret';\n" +
        "document.getElementById('pp-confirm').value  = 'secret';");
  await h.run('savePasswordProtection()'); await h.flush();
  eq(dirty(h), true, 'encryption is applied at save time too');

  h = await app();
  await h.run('removePasswordProtection()'); await h.flush();
  eq(dirty(h), true, 'and so is removing it');
});

test('every Layout page operation flags the document', async () => {
  const ids = ['sessA-p1', 'sessA-p2', 'sessA-p3', 'sessA-p4'];
  for (const [label, call] of [
    ['reorder',        'syncOrder()'],
    ['rotate one',     'contextRotate(90)'],
    ['delete one',     'contextDelete()'],
    ['rotate checked', 'rotateSelected(90)'],
    ['delete checked', 'deleteSelectedPages()'],
    ['compress',       'doCompress()'],
  ]) {
    const h = await app();
    inLayout(h, 'sessA', ids);
    eq(dirty(h), false, `${label}: clean to begin with`);
    await h.run(call); await h.flush();
    eq(dirty(h), true, `${label} in Layout is an unsaved change`);
  }
});

test('merging another file into a loaded document flags it — opening one does not', async () => {
  const h = await app();
  inLayout(h, 'sessA', []);                      // empty grid: nothing open yet
  await h.run("openFilesByPath(['C:/docs/first.pdf'])"); await h.flush();
  eq(dirty(h), false, 'opening a file into an empty grid is not a change');

  await h.run("openFilesByPath(['C:/docs/second.pdf'])"); await h.flush();
  eq(dirty(h), true, 'adding to a grid that already holds pages merges — and that is');
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
