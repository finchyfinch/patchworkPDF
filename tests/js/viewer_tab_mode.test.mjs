/* Regression test: a tab left in the Viewer must come back in the Viewer.
 *
 * _saveActiveTabMeta records which panel a tab was showing.  It tested the
 * Layout panel with `style.display !== 'none'` — but the Layout panel is hidden
 * by CSS until it is first opened, so its inline display is EMPTY, and '' is
 * not 'none'.  Every tab left before Layout had ever been visited was parked as
 * a Layout tab, so closing the tab in front of it (or clicking back to it)
 * dropped the user into Layout.  Found while capturing the manual's screenshots:
 * open three documents, close two, and the app switched to Layout.
 *
 * Run: node tests/js/viewer_tab_mode.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || 'not equal'}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}

async function twoTabs() {
  const h = loadViewer({ sessA: { pages: makePages('sessA', 1, 'a.pdf') },
                         sessB: { pages: makePages('sessB', 1, 'b.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('sessA','')"); await h.flush();
  h.run("openNewTab('sessB','')"); await h.flush();
  return h;
}

test('a tab left in the Viewer is parked as a Viewer tab', async () => {
  const h = await twoTabs();
  h.run("document.getElementById('panel-layout').style.display = ''");   // the real default
  h.run('_saveActiveTabMeta()');
  eq(h.run('_tabs[_activeTabIdx].mode'), 'viewer');
  h.dispose();
});

test('a tab left in Layout is still parked as a Layout tab', async () => {
  const h = await twoTabs();
  h.run("document.getElementById('panel-layout').style.display = 'flex'");
  h.run('_saveActiveTabMeta()');
  eq(h.run('_tabs[_activeTabIdx].mode'), 'layout');
  h.dispose();
});

test('an empty inline display is not an active panel', async () => {
  const h = await twoTabs();
  h.run(`document.getElementById('panel-viewer').style.display = 'flex';
         document.getElementById('panel-layout').style.display = ''`);
  eq(h.run('_activePanelId()'), 'panel-viewer');
  h.run(`document.getElementById('panel-viewer').style.display = 'none';
         document.getElementById('panel-layout').style.display = 'flex'`);
  eq(h.run('_activePanelId()'), 'panel-layout');
  h.dispose();
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`PASS  ${name}`); }
  catch (e) { failed++; console.log(`FAIL  ${name}\n      ${e.message}`); }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
