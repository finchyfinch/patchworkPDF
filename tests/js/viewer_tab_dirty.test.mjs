/* Regression tests: unsaved-changes state survives tab juggling, and closing a
 * tab says so before throwing the document away.
 *
 * The report these come from: auto-add bookmarks to document A, drop in
 * document B, go back to A, close A's tab with the × — no warning, bookmarks
 * gone.  Two separate faults produced that:
 *
 *   • the dirty flag was ONE global describing whatever was on screen, so
 *     opening B carried A's dirtiness over to B, and a Save in either one
 *     cleared the flag for both;
 *   • nothing consulted it on tab close.  `beforeunload` only fires for the
 *     whole window, and _switchTab asked the *editor* about pending text edits
 *     — never the document about unsaved changes.
 *
 * So the flag is parked per tab (_saveActiveTabMeta / _restoreTabDirty) and
 * _closeTab confirms.  These drive the real viewer-panel.js over the headless
 * DOM, with the harness modelling shared.js's flag and dirty-check registry.
 *
 * Run: node tests/js/viewer_tab_dirty.test.mjs
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

// ── fixture ──────────────────────────────────────────────────────────────────
// Two documents in two tabs, tab 1 (docB) active — the state the report starts
// from, once the second file has been dropped in.
async function twoTabs() {
  const h = loadViewer({
    sessA: { pages: makePages('sessA', 4, 'alpha.pdf') },
    sessB: { pages: makePages('sessB', 3, 'beta.pdf') },
  });
  h.ctx.showToast = () => {};
  // Whether the close confirmation was raised, and what it was answered.
  const asked = [];
  h.confirmWith = (answer) => h.run(
    `showConfirm = (msg, title, ok) => { __asked.push({msg, title, ok});
       return Promise.resolve(${answer ? 'true' : 'false'}); };`);
  h.run('var __asked = [];');
  h.asked = () => h.run('__asked');
  h.confirmWith(true);

  h.run("openNewTab('sessA','')");
  await h.flush();
  h.run("openNewTab('sessB','')");
  await h.flush();
  return h;
}

const dirtyOf   = (h, i) => h.run(`_tabs[${i}].dirty === true`);
const liveDirty = (h)    => h.run('pwIsDocDirty()');
const unsaved   = (h)    => h.run('pwHasUnsavedWork()');

// ── the flag belongs to the document, not the screen ─────────────────────────

test('opening a second document does not inherit the first one\'s dirtiness', async () => {
  const h = loadViewer({
    sessA: { pages: makePages('sessA', 4, 'alpha.pdf') },
    sessB: { pages: makePages('sessB', 3, 'beta.pdf') },
  });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')");
  await h.flush();
  h.run('pwSetDocDirty(true)');          // e.g. auto-added bookmarks in A

  h.run("openNewTab('sessB','')");       // drop in the second document
  await h.flush();
  eq(liveDirty(h), false, 'the new document starts clean');
  eq(dirtyOf(h, 0), true, "but A's unsaved changes are remembered against its tab");
  eq(unsaved(h), true, 'and the window still knows there is unsaved work somewhere');
});

test('switching tabs carries each document\'s own state with it', async () => {
  const h = await twoTabs();
  h.run('pwSetDocDirty(true)');          // edit B
  await h.run('_switchTab(0)'); await h.flush();
  eq(liveDirty(h), false, 'A is untouched, so it reads clean');
  eq(dirtyOf(h, 1), true, "while B's edit is parked on B's tab");

  h.run('pwSetDocDirty(true)');          // now edit A too
  await h.run('_switchTab(1)'); await h.flush();
  eq(liveDirty(h), true, 'back on B, its own edit is still there');
  eq(dirtyOf(h, 0), true, 'and A stays dirty in the background');
});

test('saving one document does not mark the other one saved', async () => {
  const h = await twoTabs();
  h.run('pwSetDocDirty(true)');          // B is dirty
  await h.run('_switchTab(0)'); await h.flush();
  h.run('pwSetDocDirty(true)');          // A is dirty too
  h.run('pwSetDocDirty(false)');         // ...and A gets saved

  eq(liveDirty(h), false, 'A is clean after its save');
  await h.run('_switchTab(1)'); await h.flush();
  eq(liveDirty(h), true, "B's unsaved changes survived A's save");
});

// ── closing a tab asks first ─────────────────────────────────────────────────

test('closing a tab with unsaved changes warns, and cancelling keeps it', async () => {
  const h = await twoTabs();
  h.run('pwSetDocDirty(true)');
  h.confirmWith(false);                  // user says "no, don't close"

  await h.run('_closeTab(1)'); await h.flush();
  eq(h.run('_tabs.length'), 2, 'the tab is still open');
  eq(h.asked().length, 1, 'the warning was raised');
  eq(h.asked()[0].title, 'confirm.close_tab_unsaved_title', 'and it is the close-tab one');
  eq(liveDirty(h), true, 'the document is still flagged unsaved');
});

test('closing a tab with unsaved changes proceeds once confirmed', async () => {
  const h = await twoTabs();
  h.run('pwSetDocDirty(true)');
  h.confirmWith(true);

  await h.run('_closeTab(1)'); await h.flush();
  eq(h.run('_tabs.length'), 1, 'the tab closed');
  eq(h.asked().length, 1, 'after asking exactly once');
});

test('closing a clean tab does not nag', async () => {
  const h = await twoTabs();
  await h.run('_closeTab(1)'); await h.flush();
  eq(h.run('_tabs.length'), 1, 'closed straight away');
  eq(h.asked().length, 0, 'with no confirmation at all');
});

test('closing a dirty BACKGROUND tab warns about that tab, not the active one', async () => {
  const h = await twoTabs();
  h.run('pwSetDocDirty(true)');          // dirty B (active, index 1)
  await h.run('_switchTab(0)'); await h.flush();   // A is active and clean
  h.confirmWith(false);

  await h.run('_closeTab(1)'); await h.flush();    // close B from A
  eq(h.asked().length, 1, 'the background tab\'s state is what got checked');
  eq(h.run('_tabs.length'), 2, 'and cancelling kept it');

  // The clean active tab still closes silently.
  h.run('__asked.length = 0');
  await h.run('_closeTab(0)'); await h.flush();
  eq(h.asked().length, 0, 'no warning for the clean one');
});

test('the promoted tab adopts its own state when the active tab is closed', async () => {
  const h = await twoTabs();
  h.run('pwSetDocDirty(true)');          // B dirty
  await h.run('_switchTab(0)'); await h.flush();
  h.run('pwSetDocDirty(false)');         // A clean, active

  await h.run('_closeTab(0)'); await h.flush();    // close clean A → B promoted
  eq(h.run('_tabs.length'), 1, 'A closed without a prompt');
  eq(h.asked().length, 0, 'because A was clean');
  eq(liveDirty(h), true, 'and B, now on screen, reports its own unsaved changes');
});

test('detaching a tab does not warn — the session lives on', async () => {
  const h = await twoTabs();
  h.run('pwSetDocDirty(true)');
  // skipCleanup=true is what _detachTab / pop-out pass.
  await h.run('_closeTab(1, true)'); await h.flush();
  eq(h.asked().length, 0, 'nothing is being discarded, so nothing to warn about');
  eq(h.run('_tabs.length'), 1, 'and the tab still leaves this window');
});

// ── the window-close guard sees background tabs ──────────────────────────────

test('unsaved work in a background tab still guards the window close', async () => {
  const h = await twoTabs();
  h.run('pwSetDocDirty(true)');          // dirty B
  await h.run('_switchTab(0)'); await h.flush();   // sit on clean A

  eq(liveDirty(h), false, 'the document on screen is clean');
  eq(unsaved(h), true, 'but the window guard still sees B and would warn');
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
