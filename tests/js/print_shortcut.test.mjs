/* Ctrl+P arrives by up to two paths on Windows (v3.4): the page's own keydown
 * listener, and the app window (platform_io/win_shortcuts.py) calling
 * pwAppShortcut('print', ...) when the page could not hear the key.  If both
 * fire, the dialog must open ONCE; and every call is reported for the log.
 *
 * Run: node tests/js/print_shortcut.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function eq(a, b, msg) { if (a !== b) throw new Error(`${msg}: expected ${b}, got ${a}`); }

async function viewer() {
  const h = loadViewer({ s: { pages: makePages('s', 3, 'doc.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('s','')");
  await h.flush();
  h.run(`window.__prints = 0;
         printDocument = function () { window.__prints++; };`);
  return h;
}

test('the window path opens the print dialog', async () => {
  const h = await viewer();
  h.run("pwAppShortcut('print', 'window')");
  await h.flush();
  eq(h.run('window.__prints'), 1, 'printDocument calls');
  const ev = h.callsTo('/api/ui_event');
  eq(ev.length, 1, 'reported to the log');
  eq(ev[0].body.via, 'window', 'path');
  h.dispose();
});

test('page and window both firing for ONE keypress open it once', async () => {
  const h = await viewer();
  h.run("pwAppShortcut('print', 'page'); pwAppShortcut('print', 'window')");
  await h.flush();
  eq(h.run('window.__prints'), 1, 'printDocument calls');
  h.dispose();
});

test('Ctrl+P with the dialog already open does not rebuild it', async () => {
  const h = await viewer();
  h.run("document.getElementById('print-modal-overlay').classList.add('show')");
  h.run("pwAppShortcut('print', 'window')");
  await h.flush();
  eq(h.run('window.__prints'), 0, 'printDocument calls');
  eq(h.callsTo('/api/ui_event')[0].body.outcome, 'alreadyopen', 'outcome');
  h.dispose();
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log('ok   ' + name); }
  catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + e.message); }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
if (failed) process.exit(1);
