/* ═══════════════════════════════════════════════════════
   LAYOUT JS — drives the #panel-layout view in viewer.html
════════════════════════════════════════════════════════ */

let sessionId = null;
let pages = []; // [{id, filename, page_num, total_pages, thumbnail, rotation}]
let contextPageId = null;
let dragSrcId = null;
let saveDir = '';
let selectedIds = new Set(); // page ids checkbox-selected (in scope for Save/Rotate/Delete/Compress)
let knownIds = new Set();    // page ids we've already rendered — new pages default to checked

// \u2500\u2500\u2500 Upload \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// Full-page file drop — mirrors the viewer's whole-canvas behaviour so dragging
// a file anywhere over the Layout page highlights it and adds the pages. The
// compact bar at the top stays as a visual cue (and its own drop target).
function handleMainDragOver(e) {
  if (e.dataTransfer.types.includes('Files')) {
    e.preventDefault();
    document.getElementById('layout-main').classList.add('drag-over');
  }
}

function handleMainDragLeave(e) {
  // Ignore moves between child cards; only clear when the cursor truly leaves.
  if (!e.currentTarget.contains(e.relatedTarget))
    document.getElementById('layout-main').classList.remove('drag-over');
}

function handleCompactDragOver(e) {
  e.preventDefault();
  if ([...e.dataTransfer.items].some(i => i.kind === 'file')) {
    document.getElementById('compact-dropbar').classList.add('drag-over');
  }
}

function handleCompactDragLeave(e) {
  document.getElementById('compact-dropbar').classList.remove('drag-over');
}

function handleCompactDrop(e) {
  e.preventDefault();
  document.getElementById('compact-dropbar').classList.remove('drag-over');
  const files = [...e.dataTransfer.files].filter(f => {
    const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
    return ACCEPTED_EXTS.has(ext);
  });
  if (!files.length) return;
  dispatchFileDrop(files, openFilesByPath, () => uploadFiles(files));
}

const ACCEPTED_EXTS = new Set([
  '.pdf', '.doc', '.docx', '.odt', '.rtf',
  '.xls', '.xlsx', '.ods', '.csv', '.txt',
  // Images — converted to one document page each (multi-page TIFFs expand).
  '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.webp'
]);

function handleMainDrop(e) {
  e.preventDefault();
  document.getElementById('layout-main').classList.remove('drag-over');
  const files = [...e.dataTransfer.files].filter(f => {
    const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
    return ACCEPTED_EXTS.has(ext);
  });
  if (!files.length) return;
  dispatchFileDrop(files, openFilesByPath, () => uploadFiles(files));
}

function handleFileSelect(e) {
  const files = [...e.target.files].filter(f => {
    const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
    return ACCEPTED_EXTS.has(ext);
  });
  if (files.length) uploadFiles(files);
  e.target.value = '';
}

// "Add Files" entry point. In the native app it opens a real OS file dialog and
// loads by path (so the save dialog can default to the source folder); in a
// browser it falls back to the hidden <input type=file>.
async function addFiles() {
  const paths = await nativePickOpenFiles(sessionId);
  if (paths === undefined) {                 // not the native app
    document.getElementById('file-input').click();
    return;
  }
  if (!paths || !paths.length) return;       // dialog cancelled
  await openFilesByPath(paths);
}

async function openFilesByPath(paths) {
  // Adding to a grid that already holds pages MERGES into the open document —
  // an unsaved change.  Opening into an empty grid is just opening a file.
  const hadPages = pages.length > 0;
  showLoading(window.t('toast.opening_files', {count: paths.length}));
  const jobId = newProgressId();
  const stopPoll = startProgressPoll(jobId);
  try {
    const res = await apiFetch('/api/open_paths', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session_id: sessionId, paths, progress_id: jobId })
    });
    const data = await res.json();
    sessionId = data.session_id;
    if (data.save_dir) saveDir = data.save_dir;
    data.pages.forEach(p => pages.push(p));
    renderGrid();
    if (data.pages.length > 0) {
      if (hadPages) _layoutDirty();
      showToast(window.t('toast.added_pages', {count: data.pages.length}), 'success');
    }
    if (data.errors && data.errors.length > 0) {
      setTimeout(() => showToast(window.t('toast.could_not_open') + data.errors[0], 'error'), 600);
    }
  } catch (err) {
    showToast(window.t('toast.open_failed'), 'error');
  } finally {
    stopPoll();
    hideLoading();
  }
}

async function uploadFiles(files) {
  const hadPages = pages.length > 0;   // see openFilesByPath
  const hasDocs = files.some(f => !f.name.toLowerCase().endsWith('.pdf'));
  const loadMsg = hasDocs
    ? window.t('toast.converting_uploading', {count: files.length})
    : window.t('toast.uploading_files', {count: files.length});
  showLoading(loadMsg);
  const jobId = newProgressId();
  const stopPoll = startProgressPoll(jobId);
  const formData = new FormData();
  files.forEach(f => formData.append('files', f));
  if (sessionId) formData.append('session_id', sessionId);
  formData.append('progress_id', jobId);

  try {
    const res = await apiFetch('/api/upload', { method: 'POST', body: formData });
    const data = await res.json();
    sessionId = data.session_id;
    if (data.save_dir) saveDir = data.save_dir;
    data.pages.forEach(p => pages.push(p));
    renderGrid();
    if (data.pages.length > 0) {
      if (hadPages) _layoutDirty();
      showToast(window.t('toast.added_pages', {count: data.pages.length}), 'success');
    }
    if (data.errors && data.errors.length > 0) {
      setTimeout(() => showToast(window.t('toast.could_not_convert') + data.errors[0], 'error'), 600);
    }
  } catch (err) {
    showToast(window.t('toast.upload_failed'), 'error');
  } finally {
    stopPoll();
    hideLoading();
  }
}

// \u2500\u2500\u2500 Grid Render \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
function renderGrid() {
  const grid = document.getElementById('pages-grid');
  const dropZone = document.getElementById('drop-zone');
  const emptyState = document.getElementById('empty-state');

  const compactBar = document.getElementById('compact-dropbar');
  if (pages.length === 0) {
    grid.innerHTML = '';
    selectedIds.clear();
    knownIds.clear();
    document.body.classList.remove('subset-selected');
    dropZone.classList.remove('has-pages');
    compactBar.classList.remove('show');
    emptyState.classList.add('show');
    ['save-btn', 'compress-btn', 'export-btn', 'hf-btn', 'rotate-cw-btn',
     'rotate-ccw-btn', 'delete-sel-btn', 'selectall-btn', 'deselectall-btn',
     'clear-all-btn']
      .forEach(id => { const el = document.getElementById(id); if (el) el.disabled = true; });
    document.getElementById('page-count-badge').textContent = window.t('badge.count_pages', {count: 0});
    return;
  }

  emptyState.classList.remove('show');
  dropZone.classList.add('has-pages');
  compactBar.classList.add('show');
  // Export, Header/Footer and Clear All are whole-document — enabled whenever
  // pages exist, regardless of what is checked.
  ['export-btn', 'hf-btn', 'clear-all-btn']
    .forEach(id => { const el = document.getElementById(id); if (el) el.disabled = false; });

  // New pages default to checked (in scope). Pages the user explicitly unchecked
  // stay unchecked, because they're already "known".
  const live = new Set(pages.map(p => p.id));
  pages.forEach(p => {
    if (!knownIds.has(p.id)) { knownIds.add(p.id); selectedIds.add(p.id); }
  });
  // Drop ids for pages that no longer exist (e.g. after a delete).
  selectedIds.forEach(id => { if (!live.has(id)) selectedIds.delete(id); });
  knownIds.forEach(id => { if (!live.has(id)) knownIds.delete(id); });

  grid.innerHTML = pages.map((p, i) => `
    <div class="page-card${selectedIds.has(p.id) ? ' selected' : ''}"
      id="card-${p.id}"
      draggable="true"
      ondragstart="onDragStart(event, '${p.id}')"
      ondragover="onDragOver(event, '${p.id}')"
      ondragleave="onDragLeave(event, '${p.id}')"
      ondrop="onDrop(event, '${p.id}')"
      ondragend="onDragEnd(event)"
      oncontextmenu="showContextMenu(event, '${p.id}')">
      <label class="page-select" title="Select page"
        onclick="event.stopPropagation()"
        ondragstart="event.preventDefault();event.stopPropagation()">
        <input type="checkbox" ${selectedIds.has(p.id) ? 'checked' : ''}
          onchange="togglePageSelect('${p.id}', this.checked)">
      </label>
      <div class="page-thumbnail">
        <span class="page-index-badge">${i + 1}</span>
        ${p.thumbnail
          ? `<img src="data:image/png;base64,${p.thumbnail}" alt="Page ${i+1}">`
          : '<div class="loading-spinner"></div>'}
      </div>
      <div class="page-info">
        <div class="page-filename">${escHtml(p.filename)}</div>
        <div class="page-number">p.${p.page_num}${p.total_pages > 1 ? ` of ${p.total_pages}` : ''}</div>
      </div>
    </div>
  `).join('');

  updateSelectionUI();
}

// ─── Selection ────────────────────────────────────────────────────────
function togglePageSelect(id, checked) {
  if (checked) selectedIds.add(id);
  else selectedIds.delete(id);
  document.getElementById('card-' + id)?.classList.toggle('selected', checked);
  updateSelectionUI();
}

function selectAllPages() {
  pages.forEach(p => selectedIds.add(p.id));
  renderGrid();
}

function deselectAllPages() {
  selectedIds.clear();
  renderGrid();
}

// Reflect the selection in the badge, the selection-scoped button states, and
// the grid's subset styling.
function updateSelectionUI() {
  const n = selectedIds.size;
  const total = pages.length;

  const badge = document.getElementById('page-count-badge');
  if (badge) {
    if (total === 0)      badge.textContent = window.t('badge.count_pages', {count: 0});
    else if (n === 0)     badge.textContent = window.t('badge.n_of_total_selected', {n: 0, total: total});
    else if (n === total) badge.textContent = window.t('badge.count_pages', {count: total});
    else                  badge.textContent = window.t('badge.n_of_total_selected', {n: n, total: total});
  }

  // Save / Rotate / Delete / Compress act on the checked pages — need ≥1.
  const has = n > 0;
  ['save-btn', 'rotate-cw-btn', 'rotate-ccw-btn', 'delete-sel-btn', 'compress-btn']
    .forEach(id => { const el = document.getElementById(id); if (el) el.disabled = !has; });

  // Move to Front / Back only make sense for a strict subset — moving all (or
  // none) of the pages is a no-op, so keep those buttons disabled then.
  const canMove = n > 0 && n < total;
  ['move-front-btn', 'move-back-btn']
    .forEach(id => { const el = document.getElementById(id); if (el) el.disabled = !canMove; });
  const deselectBtn = document.getElementById('deselectall-btn');
  if (deselectBtn) deselectBtn.disabled = !has;
  const selectBtn = document.getElementById('selectall-btn');
  if (selectBtn) selectBtn.disabled = (total === 0 || n === total);

  // Only dim/expose checkboxes when a strict subset is chosen, so the default
  // "all included" view stays clean.
  document.body.classList.toggle('subset-selected', total > 0 && n < total);
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// \u2500\u2500\u2500 Drag & Drop Reorder \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
function onDragStart(e, id) {
  // Only activate card drag if not from drop-zone
  dragSrcId = id;
  setTimeout(() => document.getElementById('card-' + id)?.classList.add('dragging'), 0);
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/card-id', id);
}

function onDragOver(e, id) {
  if (!dragSrcId || dragSrcId === id) return;
  e.preventDefault();
  e.stopPropagation();
  document.querySelectorAll('.page-card').forEach(c => c.classList.remove('drag-target'));
  document.getElementById('card-' + id)?.classList.add('drag-target');
}

function onDragLeave(e, id) {
  document.getElementById('card-' + id)?.classList.remove('drag-target');
}

function onDrop(e, targetId) {
  // External file drop onto a card → let it bubble up to the page-level handler
  // (handleMainDrop) so the files are added, instead of being swallowed here.
  if (!dragSrcId) return;
  e.preventDefault();
  e.stopPropagation();
  document.querySelectorAll('.page-card').forEach(c => c.classList.remove('drag-target', 'dragging'));
  if (dragSrcId === targetId) return;

  const srcIdx = pages.findIndex(p => p.id === dragSrcId);
  const tgtIdx = pages.findIndex(p => p.id === targetId);
  if (srcIdx === -1 || tgtIdx === -1) return;

  const [moved] = pages.splice(srcIdx, 1);
  pages.splice(tgtIdx, 0, moved);
  renderGrid();
  syncOrder();
  dragSrcId = null;
}

function onDragEnd(e) {
  document.querySelectorAll('.page-card').forEach(c => c.classList.remove('dragging', 'drag-target'));
  dragSrcId = null;
}

async function syncOrder() {
  if (!sessionId) return;
  _layoutDirty();
  await apiFetch('/api/reorder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ session_id: sessionId, order: pages.map(p => p.id) })
  });
}

// ─── Move selected to front / back ──────────────────────────────────────
// Toolbar bulk action: jump every checked page to the start (front) or end
// (back) of the document, preserving the checked pages' current relative order.
// Pure reorder of the local `pages` array, persisted through /api/reorder like a
// drag. No-op when nothing (or everything) is checked.
// NB: named moveSelectedPages, not moveSelected — editor.js defines a global
// moveSelected(dxPts, dyPts) (nudge selected element) and loads after layout.js,
// so a bare moveSelected here would be shadowed and the toolbar button would
// silently call the editor's function instead.
function moveSelectedPages(toFront) {
  if (!sessionId || selectedIds.size === 0) return;
  const sel  = pages.filter(p => selectedIds.has(p.id));
  const rest = pages.filter(p => !selectedIds.has(p.id));
  if (sel.length === 0 || rest.length === 0) return;   // all/none selected → no move
  pages = toFront ? [...sel, ...rest] : [...rest, ...sel];
  renderGrid();
  syncOrder();
  showToast(window.t(toFront ? 'toast.moved_to_front' : 'toast.moved_to_back',
                     {count: sel.length}), 'success');
}
function moveSelectedToFront() { moveSelectedPages(true); }
function moveSelectedToBack()  { moveSelectedPages(false); }

// \u2500\u2500\u2500 Context Menu \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// The right-click menu acts on the single page under the cursor (quick rotate,
// edit, insert, delete). Bulk actions on the checkbox selection live in the
// toolbar.
function showContextMenu(e, pageId) {
  e.preventDefault();
  contextPageId = pageId;

  const menu = document.getElementById('context-menu');
  menu.style.display = 'block';
  // Clamp using the menu's real height so it never spills past the viewport.
  const x = Math.min(e.clientX, window.innerWidth - menu.offsetWidth - 8);
  const y = Math.min(e.clientY, window.innerHeight - menu.offsetHeight - 8);
  menu.style.left = Math.max(8, x) + 'px';
  menu.style.top = Math.max(8, y) + 'px';
}

document.addEventListener('click', () => {
  document.getElementById('context-menu').style.display = 'none';
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.getElementById('context-menu').style.display = 'none';
});

async function contextRotate(deg) {
  if (!contextPageId || !sessionId) return;
  document.getElementById('context-menu').style.display = 'none';

  const card = document.getElementById('card-' + contextPageId);
  if (card) card.style.opacity = '0.5';

  try {
    const res = await apiFetch('/api/rotate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session_id: sessionId, page_id: contextPageId, direction: deg })
    });
    const data = await res.json();
    const page = pages.find(p => p.id === contextPageId);
    if (page) {
      page.thumbnail = data.thumbnail;
      page.rotation = data.rotation;
    }
    _layoutDirty();
    renderGrid();
    showToast(deg > 0 ? window.t('toast.rotated_cw') : window.t('toast.rotated_ccw'), 'success');
  } catch (err) {
    showToast(window.t('toast.rotation_failed'), 'error');
    if (card) card.style.opacity = '1';
  }
}

async function contextDelete() {
  if (!contextPageId || !sessionId) return;
  document.getElementById('context-menu').style.display = 'none';

  try {
    await apiFetch('/api/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session_id: sessionId, page_id: contextPageId })
    });
    _layoutDirty();
    pages = pages.filter(p => p.id !== contextPageId);
    renderGrid();
    showToast(window.t('toast.page_deleted'), 'success');
  } catch (err) {
    showToast(window.t('toast.delete_failed'), 'error');
  }
  contextPageId = null;
}

// ─── Move (context) ────────────────────────────────────────────────────
// Right-click action on the single page under the cursor: move it to the very
// front or back of the document. Same reorder-and-sync path as the toolbar
// bulk move, one page.
function contextMove(toFront) {
  if (!contextPageId || !sessionId) return;
  document.getElementById('context-menu').style.display = 'none';

  const idx = pages.findIndex(p => p.id === contextPageId);
  if (idx === -1) { contextPageId = null; return; }
  if (toFront ? idx === 0 : idx === pages.length - 1) {   // already there
    contextPageId = null;
    return;
  }
  const [moved] = pages.splice(idx, 1);
  if (toFront) pages.unshift(moved); else pages.push(moved);
  renderGrid();
  syncOrder();
  showToast(window.t(toFront ? 'toast.moved_to_front' : 'toast.moved_to_back',
                     {count: 1}), 'success');
  contextPageId = null;
}
function contextMoveToFront() { contextMove(true); }
function contextMoveToBack()  { contextMove(false); }

// ─── Rotate selected ──────────────────────────────────────────────────
async function rotateSelected(deg) {
  if (!sessionId || selectedIds.size === 0) return;
  // Rotate in current layout order for predictable progress.
  const targets = pages.filter(p => selectedIds.has(p.id));
  showLoading(window.t('toast.rotating_pages', {count: targets.length}));
  try {
    for (let i = 0; i < targets.length; i++) {
      const page = targets[i];
      showLoading(loadingProgressHtml(i + 1, targets.length,
        window.t('toast.rotating_page_n', {n: i + 1, total: targets.length})));
      const res = await apiFetch('/api/rotate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ session_id: sessionId, page_id: page.id, direction: deg })
      });
      const data = await res.json();
      page.thumbnail = data.thumbnail;
      page.rotation = data.rotation;
    }
    _layoutDirty();
    renderGrid();
    showToast(window.t(deg > 0 ? 'toast.rotated_pages_cw' : 'toast.rotated_pages_ccw',
              {count: targets.length}), 'success');
  } catch (err) {
    showToast(window.t('toast.rotation_failed'), 'error');
  } finally {
    hideLoading();
  }
}

// ─── Delete selected ──────────────────────────────────────────────────
async function deleteSelectedPages() {
  if (!sessionId || selectedIds.size === 0) return;
  const ids = pages.filter(p => selectedIds.has(p.id)).map(p => p.id);
  const msg = ids.length === pages.length
    ? window.t('confirm.delete_all_pages', {count: ids.length})
    : window.t('confirm.delete_selected_pages', {count: ids.length});
  if (!await showConfirm(msg, window.t('confirm.delete_pages_title'),
                         window.t('confirm.delete'), 'btn-danger')) return;

  showLoading(window.t('toast.deleting_pages', {count: ids.length}));
  try {
    for (let i = 0; i < ids.length; i++) {
      showLoading(loadingProgressHtml(i + 1, ids.length,
        window.t('toast.deleting_page_n', {n: i + 1, total: ids.length})));
      await apiFetch('/api/delete', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ session_id: sessionId, page_id: ids[i] })
      });
    }
    _layoutDirty();
    const del = new Set(ids);
    pages = pages.filter(p => !del.has(p.id));
    renderGrid();
    showToast(window.t('toast.deleted_pages', {count: ids.length}), 'success');
  } catch (err) {
    showToast(window.t('toast.delete_failed'), 'error');
  } finally {
    hideLoading();
  }
}

// ─── Unsaved-changes tracking ─────────────────────────────────────────
// Layout mutates the document as directly as the editor does — reorder, rotate,
// delete, compress, insert, header/footer — but every one of those changes only
// ever lived in the server session.  Nothing told shared.js's doc-dirty flag, so
// the close guard, the close-tab warning and the per-tab parking in
// _saveActiveTabMeta all reported a reshuffled document as clean and threw the
// work away without asking.  Every mutating path below calls this.
function _layoutDirty() {
  if (typeof pwSetDocDirty === 'function') pwSetDocDirty(true);
}

// ─── Save ─────────────────────────────────────────────────────────────
// Toolbar Save writes the checked pages (every page by default) as a new PDF,
// preserving page structure. A full selection is a normal full-document save
// (keeps bookmarks, remembers the name); a strict subset saves just those pages.
const hasNativePicker = typeof window.showSaveFilePicker === 'function';

// Only a full-document save means "this document is now on disk".  Exporting a
// strict subset of the checked pages writes a DIFFERENT document to a new file
// and leaves this one exactly as unsaved as it was — clearing the flag there
// would silently license throwing the rest of the pages away.
function _layoutMarkSaved(isSubset) {
  if (isSubset) return;
  if (typeof pwMarkSaved === 'function') pwMarkSaved(sessionId);
}

async function saveDocument() {
  if (!sessionId || selectedIds.size === 0) return;
  const ids       = pages.filter(p => selectedIds.has(p.id)).map(p => p.id);
  const isSubset  = ids.length !== pages.length;
  const suggested = isSubset ? suggestedSelectionFilename() : suggestedFilename();
  const label     = isSubset
    ? `Saving ${ids.length} selected page${ids.length !== 1 ? 's' : ''}…`
    : 'Saving PDF…';

  // Native app: OS save dialog defaulting to the document's source folder,
  // then write server-side to the chosen path.
  const np = await nativePickSavePath(sessionId, suggested);
  if (np !== undefined) {                 // native app
    if (!np) return;                      // dialog cancelled
    showLoading(label);
    try {
      await saveSessionToPath(sessionId, np, ids);   // throws with the reason
      _layoutMarkSaved(isSubset);
      showToast(window.t('toast.saved') + np, 'success');
    } catch (e) {
      showToast(window.t('toast.save_failed') + e.message, 'error');
    } finally {
      hideLoading();
    }
    return;
  }

  // Browser with File System Access API: native OS save picker + blob write.
  if (hasNativePicker) {
    let handle;
    try {
      handle = await window.showSaveFilePicker({
        suggestedName: suggested,
        types: [{ description: 'PDF File', accept: { 'application/pdf': ['.pdf'] } }],
      });
    } catch (e) {
      if (e.name !== 'AbortError') showToast(window.t('toast.could_not_open_file_dialog'), 'error');
      return;
    }
    showLoading(label);
    try {
      const blobRes = await apiFetch('/api/save_blob', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ session_id: sessionId, page_ids: ids })
      });
      const blob = await blobRes.blob();
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      _layoutMarkSaved(isSubset);
      showToast(window.t('toast.saved') + handle.name, 'success');
    } catch (e) {
      showToast(window.t('toast.save_failed') + e.message, 'error');
    } finally {
      hideLoading();
    }
    return;
  }

  // Fallback: download the PDF via the browser.
  showLoading(label);
  try {
    const blobRes = await apiFetch('/api/save_blob', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session_id: sessionId, page_ids: ids })
    });
    const blob = await blobRes.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = suggested; a.click();
    URL.revokeObjectURL(url);
    _layoutMarkSaved(isSubset);
    showToast(window.t('toast.downloaded') + suggested, 'success');
  } catch (e) {
    showToast(window.t('toast.save_failed') + e.message, 'error');
  } finally {
    hideLoading();
  }
}

function suggestedSelectionFilename() {
  const first = pages.find(p => selectedIds.has(p.id));
  const base = first && first.filename ? first.filename.replace(/\.pdf$/i, '') : 'selection';
  return base + '_selection.pdf';
}

async function showCompress() {
  const warn = document.getElementById('compress-scan-warn');
  warn.style.display = 'none';
  document.getElementById('compress-modal').style.display = 'flex';
  updateCompressPreset();
  // Asynchronously flag scanned pages so the modal isn't blocked on the check.
  try {
    const ids = pages.filter(p => selectedIds.has(p.id)).map(p => p.id);
    if (!ids.length) return;
    const res  = await apiFetch('/api/compress/scan_check', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: sessionId, page_ids: ids})
    });
    const data = await res.json();
    // Only reveal if the modal is still open (user may have cancelled meanwhile).
    if (data.scanned &&
        document.getElementById('compress-modal').style.display !== 'none') {
      warn.style.display = 'block';
    }
  } catch (_) { /* warning is advisory — ignore check failures */ }
}
function hideCompress() {
  document.getElementById('compress-modal').style.display = 'none';
}
function updateCompressPreset() {
  const v = document.getElementById('compress-preset').value;
  document.getElementById('compress-custom').style.display = (v==='custom') ? 'flex' : 'none';
  const map = {screen:[96,45], ebook:[150,60], print:[200,75]};
  if (v !== 'custom') {
    const [dpi,q] = map[v];
    document.getElementById('compress-dpi').value = dpi;
    document.getElementById('compress-dpi-val').textContent = dpi;
    document.getElementById('compress-quality').value = q;
    document.getElementById('compress-q-val').textContent = q;
  }
}
function fmtBytes(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return Math.round(n / 1024) + ' KB';
  return n + ' B';
}

function compressProgressHtml(done, total) {
  const pct = Math.round(done / total * 100);
  return `Compressing page ${Math.min(done + 1, total)} of ${total}…` +
    `<div style="margin:10px auto 0;width:220px;height:6px;background:var(--surface2);` +
    `border:1px solid var(--border);border-radius:3px;overflow:hidden">` +
    `<div style="width:${pct}%;height:100%;background:var(--accent);transition:width .15s"></div></div>`;
}

async function doCompress() {
  hideCompress();
  const dpi     = parseInt(document.getElementById('compress-dpi').value);
  const quality = parseInt(document.getElementById('compress-quality').value);
  // Compress only the checked pages (every page by default).
  const targets = pages.filter(p => selectedIds.has(p.id));
  const total   = targets.length;
  if (total === 0) return;
  let oldTotal = 0, newTotal = 0, failed = 0;
  try {
    // One request per page so the overlay can show real progress and
    // thumbnails refresh as each page finishes.
    for (let i = 0; i < total; i++) {
      const pg = targets[i];
      showLoading(compressProgressHtml(i, total));
      const res  = await apiFetch('/api/compress', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({session_id: sessionId, page_id: pg.id, dpi, quality})
      });
      const data = await res.json();
      if (data.error) throw apiErr(data);
      oldTotal += data.old_size || 0;
      newTotal += data.new_size || 0;
      failed   += data.failed   || 0;
      const result = data.pages && data.pages[0];
      if (result && result.thumbnail) {
        pg.thumbnail = result.thumbnail;
        const img = document.getElementById('card-' + pg.id)?.querySelector('.page-thumbnail img');
        if (img) img.src = 'data:image/png;base64,' + result.thumbnail;
      }
    }
    _layoutDirty();
    renderGrid();
    const saved = oldTotal - newTotal;
    const pct   = oldTotal > 0 ? Math.round(saved / oldTotal * 100) : 0;
    if (failed > 0) {
      showToast(window.t('toast.compression_finished_failed', {count: failed}), 'error');
    } else if (saved <= 0) {
      showToast(window.t('toast.pages_are_already_smaller_than_the'), 'info');
    } else {
      showToast(window.t('toast.compression_complete',
        {old: fmtBytes(oldTotal), new: fmtBytes(newTotal), pct: pct}), 'success');
    }
  } catch(e) {
    showToast(window.t('toast.compression_failed') + e.message, 'error');
  } finally {
    hideLoading();
  }
}

function suggestedFilename() {
  // Use the first page's source filename with an "_edited" suffix
  if (pages.length > 0 && pages[0].filename) {
    return pages[0].filename.replace(/\.pdf$/i, '') + '_edited.pdf';
  }
  return 'edited.pdf';
}

// ─── Clear ────────────────────────────────────────────────────────────
async function clearAll() {
  if (pages.length === 0) return;
  if (!await showConfirm(window.t('confirm.clear_all_msg'), window.t('confirm.clear_all_title'),
                         window.t('confirm.clear_all'), 'btn-danger')) return;
  if (sessionId) {
    await apiFetch('/api/clear', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session_id: sessionId })
    });
  }
  pages = [];
  renderGrid();
  showToast(window.t('toast.cleared'), 'success');
}

// ─── Grid zoom ────────────────────────────────────────────────────────
// Layout was the only mode without a zoom control, and it is the one whose
// content is a grid whose density matters most: four pages float in an empty
// canvas, two hundred take forever to scroll.  The pill drives --card-min, the
// minimum track width of the auto-fill grid, so the column count follows.
const LAY_CARD_STEPS = [110, 140, 180, 230, 300, 390];
const LAY_CARD_DEFAULT = 180;
let _layCardMin = LAY_CARD_DEFAULT;

function _layApplyCardMin(px) {
  _layCardMin = Math.max(LAY_CARD_STEPS[0],
                         Math.min(LAY_CARD_STEPS[LAY_CARD_STEPS.length - 1], px));
  const grid = document.getElementById('pages-grid');
  if (grid) grid.style.setProperty('--card-min', _layCardMin + 'px');
  const lbl = document.getElementById('lay-zoom-label');
  if (lbl) lbl.textContent = Math.round(_layCardMin / LAY_CARD_DEFAULT * 100) + '%';
  try { localStorage.setItem('pw_layout_card_min', String(_layCardMin)); } catch (_) {}
}

function layZoomIn() {
  const next = LAY_CARD_STEPS.find(s => s > _layCardMin + 1);
  _layApplyCardMin(next !== undefined ? next : LAY_CARD_STEPS[LAY_CARD_STEPS.length - 1]);
}
function layZoomOut() {
  const prev = [...LAY_CARD_STEPS].reverse().find(s => s < _layCardMin - 1);
  _layApplyCardMin(prev !== undefined ? prev : LAY_CARD_STEPS[0]);
}
function layZoomReset() { _layApplyCardMin(LAY_CARD_DEFAULT); }

// Ctrl+plus / minus / 0, matching the editor's bindings. Layout-only: the other
// panels drive their own zoom off the same keys.
document.addEventListener('keydown', e => {
  if (typeof _curPanel !== 'function' || _curPanel() !== 'panel-layout') return;
  if (!e.ctrlKey && !e.metaKey) return;
  if (e.key === '=' || e.key === '+') { e.preventDefault(); layZoomIn(); }
  else if (e.key === '-')             { e.preventDefault(); layZoomOut(); }
  else if (e.key === '0')             { e.preventDefault(); layZoomReset(); }
});

(function _layRestoreCardMin() {
  let saved = LAY_CARD_DEFAULT;
  try { saved = parseInt(localStorage.getItem('pw_layout_card_min'), 10) || LAY_CARD_DEFAULT; } catch (_) {}
  _layApplyCardMin(saved);
})();

// The selection chip is built at runtime, so i18n's [data-i18n] sweep cannot
// reach it — recompute it whenever the language changes.
window.addEventListener('pw-lang-changed', () => updateSelectionUI());

// ─── Init ─────────────────────────────────────────────────────────────
renderGrid();

// ─── Window lifecycle ─────────────────────────────────────────────────────────
// Each window has a unique ID so the server tracks them individually.
// The app only shuts down when ALL windows have closed.

// cache:'no-store' bypasses the WebView cache entirely, so this reads the
// version of the build that is actually running.  Without it an upgraded
// install can keep reporting the PREVIOUS version here — the WebView serves a
// cached version.json from the old build (see _NO_STORE_TYPES in entry.py).
// The server now sends no-store too; this also covers a cache already poisoned
// by an older build, where the stale entry would otherwise still be used.
fetch('version.json', { cache: 'no-store' })
  .then(r => r.json())
  .then(v => {
    document.getElementById('about-version').textContent =
      (window.t ? window.t('about_version.version') : 'Version') + ' ' + v.version + ' — ' + v.year;
    document.getElementById('about-publisher').textContent = v.publisher;
    document.getElementById('about-year').textContent = v.year;
  })
  .catch(() => {});

// About dialog — shared across all three views.  Keyboard model: Tab / Shift+Tab
// cycle its buttons (focus trapped inside), Esc closes, and focus returns to
// whatever opened it.
let _aboutPrevFocus = null;

function _aboutFocusables() {
  const box = document.querySelector('#about-modal .about-box');
  if (!box) return [];
  return Array.from(box.querySelectorAll('button, a[href], [tabindex]:not([tabindex="-1"])'))
    .filter(el => el.offsetParent !== null && !el.disabled);
}

function _aboutKey(e) {
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeAbout(); return; }
  if (e.key !== 'Tab') return;
  const f = _aboutFocusables();
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1], active = document.activeElement;
  if (e.shiftKey) {
    if (active === first || !f.includes(active)) { e.preventDefault(); last.focus(); }
  } else {
    if (active === last || !f.includes(active)) { e.preventDefault(); first.focus(); }
  }
}

function showAbout() {
  const m = document.getElementById('about-modal');
  if (!m) return;
  _aboutPrevFocus = document.activeElement;
  m.style.display = 'flex';
  document.addEventListener('keydown', _aboutKey, true);
  // Land focus on the primary action (Close, the last button) so Enter dismisses.
  const f = _aboutFocusables();
  if (f.length) f[f.length - 1].focus();
}

function closeAbout() {
  const m = document.getElementById('about-modal');
  if (!m) return;
  m.style.display = 'none';
  document.removeEventListener('keydown', _aboutKey, true);
  if (_aboutPrevFocus && _aboutPrevFocus.focus) _aboutPrevFocus.focus();
  _aboutPrevFocus = null;
}

// ─── Usage license ────────────────────────────────────────────────────────────
// Shown from the About box (Close button) and as a blocking first-run
// acceptance dialog (Accept button, no other way to dismiss).

let _licenseFirstRun = false;
let _licenseLoaded   = false;

async function _loadLicenseText() {
  if (_licenseLoaded) return;
  try {
    const res = await fetch('/license.txt');
    if (!res.ok) throw new Error(res.status);
    document.getElementById('license-text').textContent = await res.text();
    _licenseLoaded = true;
  } catch (e) {
    document.getElementById('license-text').textContent =
      'Could not load the license text. A copy is included as LICENSE.txt in the application folder.';
  }
}

function showLicense(firstRun) {
  _licenseFirstRun = !!firstRun;
  document.getElementById('license-accept-btn').style.display   = firstRun ? '' : 'none';
  document.getElementById('license-close-btn').style.display    = firstRun ? 'none' : '';
  document.getElementById('license-firstrun-note').style.display = firstRun ? '' : 'none';
  document.getElementById('license-modal').style.display = 'flex';
  _loadLicenseText();
}

function closeLicense() {
  if (_licenseFirstRun) return;   // first run must be acknowledged via Accept
  document.getElementById('license-modal').style.display = 'none';
}

async function acceptLicense() {
  try { await apiFetch('/api/accept_license', { method: 'POST' }); } catch (e) {}
  _licenseFirstRun = false;
  document.getElementById('license-modal').style.display = 'none';
}

// First-run check: if this user has never accepted the license, show the
// blocking acceptance dialog on top of whatever panel is active.
//
// Fails CLOSED — only an explicit `accepted: false` from a successful reply
// opens the dialog.  fetch does not reject on 4xx/5xx, so `r.json()` happily
// parses an error body; that body has no `accepted` field, and treating it as
// "never accepted" put the first-run licence wall in front of the app on every
// single launch when /api/license_status started answering 403.  A status the
// app cannot read is not evidence the user never accepted.
apiFetch('/api/license_status')
  .then(r => (r.ok ? r.json() : null))
  .then(s => { if (s && s.accepted === false) showLicense(true); })
  .catch(() => {});

// ─── Update notifications ───────────────────────────────────────────────────────
// After the page has rendered, ask the backend whether a newer release is
// published on SourceForge.  The fetch is async and the backend does the network
// round-trip on a worker thread, so this never blocks startup.  If a new release
// is found (and the user hasn't opted out) a popup is shown with the release
// notes and a button to open the download page in the browser.

// ─── What's New (run once per version) ──────────────────────────────────────
// v3.1 moved the editor's tools into the viewer and removed the Editor button.
// Someone who used that button every day needs telling once, or they reasonably
// conclude the app is broken.
//
// Queued behind the licence dialog for the same reason the update check is, and
// the update check is in turn queued behind THIS: three dialogs stacking on a
// first launch would be worse than any of them.

function _startupDialogBlocked() {
  const lic = document.getElementById('license-modal');
  return !!(lic && lic.style.display === 'flex');
}

function showWhatsNew() {
  const m = document.getElementById('whats-new-modal');
  if (m) m.style.display = 'flex';
}

function closeWhatsNew() {
  const m = document.getElementById('whats-new-modal');
  if (m) m.style.display = 'none';
  // Acknowledge on dismissal, not on display: a user who never got to read it
  // (a crash, a force-quit) should be told again rather than silently skipped.
  apiFetch('/api/whats_new_ack', { method: 'POST' }).catch(() => {});
  if (typeof _checkForUpdates === 'function') _checkForUpdates();
}

function _maybeShowWhatsNew() {
  if (_startupDialogBlocked()) { setTimeout(_maybeShowWhatsNew, 4000); return; }
  apiFetch('/api/whats_new_status')
    .then(r => (r.ok ? r.json() : null))
    .then(s => {
      // Fails CLOSED: only an explicit `show: true` opens it.
      if (s && s.show === true) showWhatsNew();
      else if (typeof _checkForUpdates === 'function') _checkForUpdates();
    })
    .catch(() => { if (typeof _checkForUpdates === 'function') _checkForUpdates(); });
}

let _updateResult = null;

function _checkForUpdates() {
  // Don't stack on top of the first-run license acceptance dialog, or on the
  // What's New note — wait until the user has dealt with those.
  const wn = document.getElementById('whats-new-modal');
  if (_startupDialogBlocked() || (wn && wn.style.display === 'flex')) {
    setTimeout(_checkForUpdates, 4000);
    return;
  }
  apiFetch('/api/update_check')
    .then(r => r.json())
    .then(res => {
      if (res && res.new_release) {
        _updateResult = res;
        _showUpdateModal(res);
      }
    })
    .catch(() => {});            // fail soft — an update check must never disturb the app
}

// Manual "Check for Updates" from the About box.  Forces a live check
// (?force=1) that bypasses the opt-out and the once-a-day throttle, and — unlike
// the silent startup check — always reports back: the update dialog when there's
// a newer release, an "up to date" dialog when there isn't, or a toast on error.
// Both dialogs carry the notify toggle, so this is also the path by which an
// opted-out user turns update notifications back on.
let _checkingUpdates = false;
async function checkForUpdatesManual() {
  if (_checkingUpdates) return;
  _checkingUpdates = true;
  const btn = document.getElementById('about-check-updates-btn');
  if (btn) btn.disabled = true;
  const failMsg = window.t ? window.t('update_modal.check_failed')
                           : "Couldn't check for updates right now.";
  showToast(window.t ? window.t('update_modal.checking') : 'Checking for updates…', 'info');
  try {
    const res = await apiFetch('/api/update_check?force=1').then(r => r.json());
    _updateResult = res;
    closeAbout();
    if (!res || res.error) {
      showToast(failMsg, 'error');
    } else {
      _showUpdateModal(res);
    }
  } catch (e) {
    showToast(failMsg, 'error');
  } finally {
    _checkingUpdates = false;
    if (btn) btn.disabled = false;
  }
}

// Renders the update dialog for both outcomes: a newer release ("Update
// available", with notes + Download) or, from a manual check, "You're up to
// date" (notes and Download hidden).  Either way the notify checkbox is shown,
// pre-set from the server's current preference, so it can be flipped both ways.
function _showUpdateModal(res) {
  const isNew = !!(res && res.new_release);
  const T = (k, p) => (window.t ? window.t(k, p) : k);

  document.getElementById('update-title').textContent =
    isNew ? T('update_title.update_available') : T('update_title.up_to_date');

  document.getElementById('update-sub').textContent = isNew
    ? 'Version ' + (res.release_version || '?') + ' is available' +
        (res.current_version ? ' — you have ' + res.current_version : '') + '.'
    : T('update_modal.latest_version', { version: (res && res.current_version) || '?' });

  // Release notes only mean anything when there's something new to install.
  const notesLabel = document.getElementById('update-notes-label');
  const notesEl    = document.getElementById('update-notes');
  if (isNew) {
    const notes = res.release_notes || [];
    notesEl.textContent = notes.length
      ? notes.map(n => '• ' + n).join('\n')
      : 'A newer version of Patchwork PDF is available.';
    notesEl.style.display = '';
    if (notesLabel) notesLabel.style.display = '';
  } else {
    notesEl.textContent = '';
    notesEl.style.display = 'none';
    if (notesLabel) notesLabel.style.display = 'none';
  }

  // Two-way notify toggle: checked = "don't notify" (opted out).  Pre-set from
  // the server's current preference so the dialog reflects — and can flip —
  // whichever way the user currently stands.
  document.getElementById('update-optout-cb').checked = !(res && res.notify);

  // Download only applies when there's a release to fetch; otherwise "Later"
  // becomes a plain "Close".
  const dlBtn    = document.getElementById('update-download-btn');
  const laterBtn = document.getElementById('update-later-btn');
  if (dlBtn) dlBtn.style.display = isNew ? '' : 'none';
  if (laterBtn) laterBtn.textContent = isNew ? T('update_modal.later') : T('update_modal.close');

  document.getElementById('update-modal').style.display = 'flex';
}

// Persist the notify preference in whichever direction the checkbox now sits
// (checked = don't notify).  Always synced — this two-way write is what lets a
// previously opted-out user turn notifications back on.
async function _applyUpdateNotifyPref() {
  const optedOut = document.getElementById('update-optout-cb').checked;
  try {
    await apiFetch('/api/update_set_notify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notify: !optedOut }),
    });
  } catch (e) {}
}

async function closeUpdateModal() {
  await _applyUpdateNotifyPref();
  document.getElementById('update-modal').style.display = 'none';
}

async function openUpdateDownload() {
  await _applyUpdateNotifyPref();
  const url = (_updateResult && _updateResult.download_url)
    || 'https://sourceforge.net/projects/patchworkpdf/files/';
  try {
    await apiFetch('/api/open_external', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
  } catch (e) {
    window.open(url, '_blank');  // fallback: WebView2 routes window.open to the OS browser
  }
  document.getElementById('update-modal').style.display = 'none';
}

// Kick off the check shortly after the window has finished loading, so it never
// competes with first paint or the heavy editor init.  The backend runs the
// actual network check only once per process, so this is safe in every window.
// What's New runs first and hands off to the update check when it is done,
// so the two can never be on screen together.
window.addEventListener('load', () => { setTimeout(_maybeShowWhatsNew, 2000); });

async function openBundledFile(filename) {
  closeAbout();
  showLoading('Opening ' + filename + '…');
  try {
    const res = await apiFetch('/api/open_bundled_file', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ filename }),
    });
    const data = await res.json();
    if (data.error) throw apiErr(data);
    if (SESSION_ID && typeof openNewTab === 'function') {
      openNewTab(data.session_id, data.page_id);   // openNewTab shows the viewer
    } else {
      // No document loaded yet — replace the empty placeholder tab
      _setActivePanel('panel-viewer');
      SESSION_ID = data.session_id;
      currentPageId = data.page_id;
      if (_tabs[_activeTabIdx]) _tabs[_activeTabIdx].session_id = SESSION_ID;
      if (typeof loadPageList === 'function')
        await loadPageList().then(() => { if (typeof loadAllAnnotations === 'function') loadAllAnnotations(); });
    }
  } catch(e) {
    showToast(window.t('toast.could_not_open_file') + e.message, 'error');
    hideLoading();
  }
}


// ─── Export As ───────────────────────────────────────────────────────
function showExportModal() {
  selectExportFmt('word');  // reset to default
  document.getElementById('export-modal').style.display = 'flex';
}

function hideExportModal() {
  document.getElementById('export-modal').style.display = 'none';
}

function selectExportFmt(fmt) {
  const wordOpt  = document.getElementById('export-opt-word');
  const excelOpt = document.getElementById('export-opt-excel');
  const editRow  = document.getElementById('export-editable-row');
  if (editRow) editRow.style.display = (fmt === 'word') ? 'flex' : 'none';
  const accent   = 'var(--accent)';
  const border   = 'var(--border)';
  if (fmt === 'word') {
    wordOpt.style.borderColor  = accent;
    excelOpt.style.borderColor = border;
    wordOpt.querySelector('input').checked  = true;
    excelOpt.querySelector('input').checked = false;
  } else {
    excelOpt.style.borderColor = accent;
    wordOpt.style.borderColor  = border;
    excelOpt.querySelector('input').checked = true;
    wordOpt.querySelector('input').checked  = false;
  }
}

async function doExport() {
  hideExportModal();
  if (!sessionId || pages.length === 0) return;

  const fmt      = document.querySelector('input[name="export-fmt"]:checked').value;
  if (fmt === 'excel') { doExportExcelTables(); return; }
  const isWord   = fmt === 'word';
  const editable = isWord && document.getElementById('export-editable').checked;
  const ext      = isWord ? '.docx' : '.xlsx';
  const desc     = isWord ? 'Word Document' : 'Excel Spreadsheet';
  const mimeType = isWord
    ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

  const baseName = (pages.length > 0 && pages[0].filename)
    ? pages[0].filename.replace(/\.[^.]+$/, '') + '_export' + ext
    : 'export' + ext;

  // Try native OS save picker first
  let savePath = null;
  let nativeHandle = null;

  if (typeof window.showSaveFilePicker === 'function') {
    try {
      nativeHandle = await window.showSaveFilePicker({
        suggestedName: baseName,
        types: [{ description: desc, accept: { [mimeType]: [ext] } }],
      });
    } catch (e) {
      if (e.name === 'AbortError') return;
      showToast(window.t('toast.could_not_open_file_dialog'), 'error');
      return;
    }
  }

  showLoading(window.t('toast.exporting_as', {desc: desc}));
  const jobId = newProgressId();
  const stopPoll = startProgressPoll(jobId);
  try {
    const res = await apiFetch('/api/export_doc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, format: fmt, dpi: 150,
                             editable: editable, progress_id: jobId }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `Server error ${res.status}`);
    }

    const blob = await res.blob();

    if (nativeHandle) {
      // Write via File System Access API
      const writable = await nativeHandle.createWritable();
      await writable.write(blob);
      await writable.close();
      showToast(window.t('toast.saved') + nativeHandle.name, 'success');
    } else {
      // Fallback: browser download
      const url = URL.createObjectURL(blob);
      const a   = document.createElement('a');
      a.href = url; a.download = baseName; a.click();
      URL.revokeObjectURL(url);
      showToast(window.t('toast.downloaded') + baseName, 'success');
    }
  } catch (e) {
    showToast(window.t('toast.export_failed') + e.message, 'error');
  } finally {
    stopPoll();
    hideLoading();
  }
}

// Excel export finds ruled tables across the document and writes each to its own
// sheet tab.  Detection runs FIRST (before any save dialog) so a document with no
// tables just tells the user, without leaving an empty file behind.  The probe
// builds and caches the workbook server-side; the save step reuses that cache.
async function doExportExcelTables() {
  const baseName = (pages.length > 0 && pages[0].filename)
    ? pages[0].filename.replace(/\.[^.]+$/, '') + '_tables.xlsx'
    : 'tables.xlsx';

  // 1. Detect + build (no save dialog yet).
  showLoading(window.t('toast.exporting_tables'));
  const jobId = newProgressId();
  const stopPoll = startProgressPoll(jobId);
  let count = 0;
  try {
    const res = await apiFetch('/api/export_doc', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, format: 'excel',
                             probe: true, progress_id: jobId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Server error ${res.status}`);
    if (data.no_tables || !data.count) {
      stopPoll(); hideLoading();
      showToast(window.t('toast.no_tables_found'), 'info');
      return;
    }
    count = data.count;
  } catch (e) {
    stopPoll(); hideLoading();
    showToast(window.t('toast.export_failed') + e.message, 'error');
    return;
  }
  stopPoll();
  hideLoading();   // don't spin behind the save dialog

  // 2. Choose destination and save the cached workbook.
  const mimeType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  const nativePath = await nativePickSavePath(sessionId, baseName);

  if (nativePath === null) return;                 // native dialog cancelled

  if (nativePath) {
    // Native desktop app: backend writes the cached workbook to the chosen path.
    showLoading(window.t('toast.exporting_as', {desc: 'Excel Spreadsheet'}));
    try {
      const res = await apiFetch('/api/export_doc', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, format: 'excel',
                               save_path: nativePath }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || `Server error ${res.status}`);
      showToast(window.t('toast.saved') + nativePath.split(/[\\/]/).pop(), 'success');
    } catch (e) {
      showToast(window.t('toast.export_failed') + e.message, 'error');
    } finally {
      hideLoading();
    }
    return;
  }

  // 3. Browser fallback: fetch the workbook bytes and save via picker / download.
  let nativeHandle = null;
  if (typeof window.showSaveFilePicker === 'function') {
    try {
      nativeHandle = await window.showSaveFilePicker({
        suggestedName: baseName,
        types: [{ description: 'Excel Spreadsheet', accept: { [mimeType]: ['.xlsx'] } }],
      });
    } catch (e) {
      if (e.name === 'AbortError') return;
    }
  }
  showLoading(window.t('toast.exporting_as', {desc: 'Excel Spreadsheet'}));
  try {
    const res = await apiFetch('/api/export_doc', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, format: 'excel' }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `Server error ${res.status}`);
    }
    const blob = await res.blob();
    if (nativeHandle) {
      const writable = await nativeHandle.createWritable();
      await writable.write(blob);
      await writable.close();
      showToast(window.t('toast.saved') + nativeHandle.name, 'success');
    } else {
      const url = URL.createObjectURL(blob);
      const a   = document.createElement('a');
      a.href = url; a.download = baseName; a.click();
      URL.revokeObjectURL(url);
      showToast(window.t('toast.downloaded') + baseName, 'success');
    }
  } catch (e) {
    showToast(window.t('toast.export_failed') + e.message, 'error');
  } finally {
    hideLoading();
  }
}

// ─── Header / Footer ─────────────────────────────────────────────────────
function showHFModal() {
  document.getElementById('hf-modal').style.display = 'flex';
}

function hideHFModal() {
  document.getElementById('hf-modal').style.display = 'none';
}

async function applyHeaderFooter() {
  if (!sessionId || pages.length === 0) return;

  const config = {
    session_id: sessionId,
    header: {
      left:   document.getElementById('hf-h-left').value.trim(),
      centre: document.getElementById('hf-h-centre').value.trim(),
      right:  document.getElementById('hf-h-right').value.trim(),
    },
    footer: {
      left:   document.getElementById('hf-f-left').value.trim(),
      centre: document.getElementById('hf-f-centre').value.trim(),
      right:  document.getElementById('hf-f-right').value.trim(),
    },
    font:   document.getElementById('hf-font').value,
    size:   parseInt(document.getElementById('hf-size').value, 10) || 10,
    color:  document.getElementById('hf-color').value,
    margin: parseInt(document.getElementById('hf-margin').value, 10) || 18,
    total_pages: pages.length,
    filenames: pages.map(p => p.filename || ''),
  };

  // Skip if all fields are empty
  const allEmpty = Object.values(config.header).every(v => !v) &&
                   Object.values(config.footer).every(v => !v);
  if (allEmpty) { hideHFModal(); return; }

  hideHFModal();
  showLoading('Applying header/footer…');
  try {
    const res = await apiFetch('/api/header_footer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `Server error ${res.status}`);
    }
    const data = await res.json();
    if (data.updated) {
      _layoutDirty();
      data.updated.forEach(u => {
        const pg = pages.find(p => p.id === u.id);
        if (pg && u.thumbnail) pg.thumbnail = u.thumbnail;
      });
      renderGrid();
      showToast(window.t('toast.hf_applied_pages', {count: data.updated.length}), 'success');
    }
  } catch (e) {
    showToast(window.t('toast.header_footer_failed') + e.message, 'error');
  } finally {
    hideLoading();
  }
}

// ── Insert Blank Page ─────────────────────────────────────────────────────────

// Standard page sizes in points
const _BLANK_SIZES = {
  match: null,                          // resolved at insert time
  a4p:   [595.28, 841.89],
  a4l:   [841.89, 595.28],
  a3p:   [841.89, 1190.55],
  ltp:   [612.0,  792.0],
  ltl:   [792.0,  612.0],
  legal: [612.0,  1008.0],
};

let _insertBlankBefore = false;  // true = before ref page, false = after

function _ptToMm(pts) { return Math.round(pts * 0.3528); }

function contextInsertBlank(insertBefore) {
  if (!contextPageId || !sessionId) return;
  document.getElementById('context-menu').style.display = 'none';
  _insertBlankBefore = insertBefore;

  // Find the reference page to show its dimensions
  const refPage = pages.find(p => p.id === contextPageId);
  const wMm = refPage ? _ptToMm(refPage.width_pts  || 595.28) : 210;
  const hMm = refPage ? _ptToMm(refPage.height_pts || 841.89) : 297;
  const refIdx = pages.findIndex(p => p.id === contextPageId) + 1;

  document.getElementById('insert-blank-title').textContent =
    insertBefore ? `Insert Blank Before Page ${refIdx}` : `Insert Blank After Page ${refIdx}`;
  document.getElementById('insert-match-dims').textContent =
    `${wMm} × ${hMm} mm`;

  // Reset to default selection
  const matchRadio = document.querySelector('input[name="insert-size"][value="match"]');
  if (matchRadio) matchRadio.checked = true;
  document.getElementById('insert-custom-row').style.display = 'none';

  // Wire up radio change to show/hide custom inputs
  document.querySelectorAll('input[name="insert-size"]').forEach(r => {
    r.onchange = () => {
      document.getElementById('insert-custom-row').style.display =
        r.value === 'custom' && r.checked ? 'flex' : 'none';
    };
  });

  document.getElementById('insert-blank-modal').style.display = 'flex';
}

function closeInsertBlankModal() {
  document.getElementById('insert-blank-modal').style.display = 'none';
}

async function doInsertBlank() {
  const selected = document.querySelector('input[name="insert-size"]:checked');
  if (!selected) return;

  let width_pts, height_pts;

  if (selected.value === 'match') {
    const refPage = pages.find(p => p.id === contextPageId);
    width_pts  = refPage ? (refPage.width_pts  || 595.28) : 595.28;
    height_pts = refPage ? (refPage.height_pts || 841.89) : 841.89;
  } else if (selected.value === 'custom') {
    const wMm = parseFloat(document.getElementById('insert-custom-w').value) || 210;
    const hMm = parseFloat(document.getElementById('insert-custom-h').value) || 297;
    width_pts  = wMm / 0.3528;
    height_pts = hMm / 0.3528;
  } else {
    [width_pts, height_pts] = _BLANK_SIZES[selected.value] || [595.28, 841.89];
  }

  closeInsertBlankModal();
  showLoading('Inserting blank page…');

  try {
    const res  = await apiFetch('/api/insert_blank_page', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        session_id:   sessionId,
        ref_page_id:  contextPageId,
        insert_before: _insertBlankBefore,
        width_pts,
        height_pts,
      })
    });
    const data = await res.json();
    if (!data.success) throw new Error(data.error || 'Insert failed');

    _layoutDirty();
    // Splice the new page into the local array at the correct position
    const refIdx = pages.findIndex(p => p.id === contextPageId);
    const insertAt = _insertBlankBefore ? refIdx : refIdx + 1;
    if (insertAt < 0 || refIdx < 0) {
      pages.push(data.page);
    } else {
      pages.splice(insertAt, 0, data.page);
    }
    renderGrid();
    showToast(window.t('toast.blank_page_inserted'), 'success');
  } catch(err) {
    showToast(window.t('toast.could_not_insert_page') + err.message, 'error');
  } finally {
    hideLoading();
  }
}

