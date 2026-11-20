/* Regression tests (v3.4): a far thumbnail click lands on the page clicked.
 *
 * goToPage pins a far-jump target to the top of the viewport until the pages
 * above it have rendered to their real heights.  The pin let go as soon as the
 * PRERENDER_MARGIN pages directly above were in — but the lazy observer's 400px
 * margin reaches the page above the target, and ITS render-around queues pages
 * further up still.  When one of those landed after the pin had let go, its
 * real height replaced the placeholder estimate, pushed the target down, and
 * the view showed the page BEFORE the one clicked.
 *
 * Also: the viewer's rail does not reorder pages, so its thumbnails must not
 * offer the browser's native image drag (a ghost that read as a broken feature).
 *
 * Run: node tests/js/viewer_thumb_jump.test.mjs
 */
import { loadViewer, makePages } from './viewer_harness.mjs';

const tests = [];
const test  = (name, fn) => tests.push([name, fn]);
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }

async function viewer() {
  const h = loadViewer({ big: { pages: makePages('big', 60, 'long.pdf') } });
  h.ctx.showToast = () => {};
  h.run("openNewTab('big','')");
  await h.flush();
  return h;
}

test('the jump pin holds while a page further above the target is still rendering', async () => {
  const h = await viewer();
  const target = 40;
  const m = h.run('PRERENDER_MARGIN');
  // Every page from the margin up to the target is in...
  h.run(`for (let i = ${target - m}; i <= ${target}; i++) {
           const st = _pageBlocks.get(allPages[i].id); st.rendered = true; st.rendering = false; }`);
  // ...but one above the margin is still in flight.
  const above = target - m - 1;
  h.run(`_pageBlocks.get(allPages[${above}].id).rendering = true`);
  h.run(`_pendingPin = { pageId: allPages[${target}].id, idx: ${target}, gen: ++_navGen }`);
  h.run('_applyPendingPin()');
  assert(h.run('_pendingPin') !== null,
         'pin released while a page above the target could still change height');
  // Once it lands the pin may go.
  h.run(`{ const st = _pageBlocks.get(allPages[${above}].id); st.rendering = false; st.rendered = true; }`);
  h.run('_applyPendingPin()');
  assert(h.run('_pendingPin') === null, 'pin should release once everything above has settled');
  h.dispose();
});

test("a far jump's own scroll event does not drop the pin", async () => {
  const h = await viewer();
  h.run('goToPage(40)');
  assert(h.run('_pendingPin') !== null, 'far jump should pin its target');
  // The scroll event scrollIntoView produces arrives after goToPage returns.
  h.run('_onAreaScroll()');
  assert(h.run('_pendingPin') !== null,
         "the jump's own scroll was taken for the user scrolling away");
  // A real user scroll still lets go.
  h.run("document.getElementById('canvas-area').scrollTop += 500");
  h.run('_onAreaScroll()');
  assert(h.run('_pendingPin') === null, 'a user scroll should cancel the pin');
  h.dispose();
});

test('viewer thumbnails are not natively draggable', async () => {
  const h = await viewer();
  h.run('renderThumbList()');
  const html = h.run("document.getElementById('thumb-list').innerHTML");
  const items = (html.match(/class="thumb-item/g) || []).length;
  const noDrag = (html.match(/draggable="false"/g) || []).length;
  assert(items > 0, 'no thumbnails rendered');
  assert(noDrag >= items * 2, `expected item AND image to be draggable=false (${noDrag} for ${items})`);
  assert(/ondragstart="return false"/.test(html), 'dragstart is not cancelled');
  h.dispose();
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log('ok   ' + name); }
  catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + e.message); }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
if (failed) process.exit(1);
