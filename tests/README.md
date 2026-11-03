# PatchworkPDF regression tests

Fast, dependency-light tests that pin the behaviours most prone to silent
regression (coordinate/rotation math, version comparison, encryption, true
redaction, and the memory/form changes).

## Running

With pytest (preferred):

```
pip install pytest
pytest                 # from the project root
```

Without pytest — every file is also a standalone script:

```
python tests/test_version.py
python tests/test_rotation.py
python tests/test_encryption.py
python tests/test_redaction.py
python tests/test_session_and_forms.py
python tests/test_image_normalize.py
python tests/test_long_paths.py
python tests/test_ocr_tessdata.py
python tests/test_packaging_scripts.py
python tests/test_source_distribution.py
python tests/test_recovery_snapshot.py
python tests/test_unsaved_marking.py
python tests/test_frontend.py
```

The front-end suites additionally need Node on PATH (dev-only — it is not part
of the shipped app).  Without it `test_frontend.py` skips.  Each suite also runs
directly:

```
node tests/js/viewer_zoom.test.mjs
```

`tests/conftest.py` (pytest) and `tests/_bootstrap.py` (standalone) both put
`src/backend` on `sys.path`, so no install/packaging step is needed.

## Coverage

| File | What it guards |
|------|----------------|
| `test_version.py` | `_version_key` / `_is_newer` zero-padded-minor ordering (2.11 > 2.9); changelog note extraction |
| `test_rotation.py` | `_raw_rect_to_display` ↔ `_display_rect_to_raw` round-trip + concrete 90° mapping |
| `test_encryption.py` | encrypt→open round-trip, wrong-password rejection, decrypted-bytes readability |
| `test_redaction.py` | image pixels under a redaction box are truly destroyed (not just covered) |
| `test_session_and_forms.py` | byte/count-bounded annotation undo history; checkbox/radio export-state helpers |
| `test_image_normalize.py` | the pre-render Indexed-image rewrite reproduces the original colours — palette entry width per base colorspace (ICCBased CMYK is 4 bytes, not 1), stale `/Decode` dropped, unmappable palettes left alone |
| `test_long_paths.py` | documents on paths over Windows' 260-char limit (deep work shares) are found at launch, open, save back to their own folder, and stay in recents; an unreadable file reports an error instead of raising |
| `test_ocr_tessdata.py` | the bundled tessdata dir reaches Tesseract via TESSDATA_PREFIX, never through pytesseract's config string (quotes and spaces do not survive Windows argument splitting); prefix matches the Tesseract major version |
| `test_packaging_scripts.py` | build scripts keep the line endings and encoding their interpreter needs — shell scripts LF (CRLF breaks them in a Linux VM), `.bat` files CRLF and ASCII (cmd.exe resumes mid-line otherwise and skips commands) |
| `test_api_guards.py` | the /api/ origin+token guard serves our own page's GETs — a browser sends no Origin on a same-origin GET and `Referrer-Policy: no-referrer` (2.17) strips the Referer, so rejecting on a missing header 403'd every GET route the app itself called (the first-run licence dialog on every launch, and update checks, dead since 2.17); a header that IS present must still be ours, a missing/wrong token is still refused, /api/ping stays open, and every API error answers in JSON with no `accepted` field |
| `test_close_guard.py` | closing a native window cannot silently discard unsaved work — the guard reads the state the page pushes (the CSP forbids eval, so it cannot ask at close time), refuses the close and asks the page to prompt, and lets the re-close through once the user agrees; saving re-arms it; and it can never trap the user in the app — the ask goes out on a worker (calling pywebview's blocking run_js from the UI thread inside FormClosing is a deadlock, not a delay), a second X while the prompt is up closes, and an unreachable page stands the guard down after one refusal instead of looping |
| `test_frontend.py` → `js/drop_dispatch.test.mjs` | a dropped file is added exactly once — the native path push and the byte-upload fallback race each other, and whichever gets there first CLAIMS the drop (a push that lands after the fallback already ran is ignored, so a slow bridge on a big Layout grid can no longer add the file twice / collide inside Word's single-instance COM server) |
| `test_frontend.py` → `js/viewer_render_queue.test.mjs` | a fast scroll through a long document does not flood the server — base renders go through a bounded queue, pages the user has already scrolled past are dropped instead of rendered, whatever is actually on screen is swept back in afterwards, and a page whose annotations the bulk load already fetched does not fetch them again |
| `test_frontend.py` → `js/viewer_tab_scroll.test.mjs` | each open document keeps its own scroll position across tab switches (restored mid-page, holds as page heights settle, yields to the user); the editor opens at the top rather than another tab's offset |
| `test_frontend.py` → `js/viewer_zoom.test.mjs` | zoom holds the point under the cursor (or the viewport centre) still; a trackpad pinch zooms continuously, one application per frame, clamped to the zoom range |
| `test_frontend.py` → `js/viewer_bookmarks.test.mjs` | bulk-deleting bookmarks removes exactly what the user marked — shift-click takes the block between the anchor and the target, ctrl-click toggles one, and neither navigates away mid-selection; right-clicking outside the selection collapses to that entry, so the menu never offers to delete a block that has scrolled out of view; declining the confirmation sends no request at all, and ids the server no longer has are pruned instead of widening a later delete |
| `test_frontend.py` → `js/viewer_tab_dirty.test.mjs` | unsaved-changes state is per document, not per screen — opening a second file does not inherit the first's dirtiness, switching tabs carries each document's own state, and saving one does not mark the other saved; closing a tab with unsaved changes warns first (background tabs judged on their own parked state, detach/pop-out exempt because the session lives on), and work in a background tab still guards the window close |
| `test_frontend.py` → `js/quit_guard.test.mjs` | the page half of that guard — the pushed answer counts work in background tabs and not just the document on screen, crosses the bridge only when it changes, is re-sent rather than assumed delivered if the push fails, no-ops in a plain browser, and the prompt reports the user's answer back exactly once even if the backend asks twice |
| `test_source_distribution.py` | `build_source_distribution.py`'s payload is a hand-kept list that nothing checked against the tree, so a file added to the repo was simply absent from every release — and the failure landed on whoever downloaded it. `tools/run_tests.py` was one: the pre-flight gate all three platform build scripts run before packaging, so a missing script exited 2 and every build from a distribution aborted at step 1 reporting "tests failed" with the tests themselves fine. These check the payload against what the tree and the shipped scripts actually need rather than a second hand-kept list — every entry names a real file, the whole `tests/` tree ships (front-end `.test.mjs` suites included), the gate and `pytest.ini` ship, every repo-relative path the shipped build scripts reference also ships, and build residue (`__pycache__`, stale `.pyc`, egg-info) does not |
| `test_recovery_snapshot.py` | the working-bytes snapshot store, which is what makes "unsaved" survivable — a change that touches no page bytes (reorder, delete, a filled field, a bookmark, document properties) still marks the session pending, so the LRU cannot evict a tab the user is editing, and rewrites the manifest without rewriting a single unchanged page; a deleted page's file is pruned rather than orphaned; bookmarks, filled form values, properties and comments all round-trip through save→load (a restore that recovers every page but loses the outline is still lost work); a manifest written by the previous version still loads; a successful Save drops both the snapshot and the pending flag; and a protected session is purged and never re-snapshotted, so decrypted plaintext never reaches disk |
| `test_unsaved_marking.py` | every route that changes a document says so, driven through the real Flask app — reorder / delete / insert / rotate, form fill, every bookmark verb, document properties; reads (page list, outline, metadata, form fields) and a fill that matched no field mark nothing, or every freshly opened document would claim unsaved changes; setting a save password purges the snapshot and removing it re-arms a full one |
| `test_frontend.py` → `js/doc_dirty.test.mjs` | the symmetric rule the "saved but still warned about unsaved changes" report broke both halves of: every full-document Save — viewer, **editor**, Layout — clears the doc-dirty flag and drops the crash-recovery snapshot (only the viewer's used to, so an editor Save left a phantom flag parked on that tab forever by `_saveActiveTabMeta`), while exporting a strict SUBSET of pages does not; and every change flags the document — the viewer's page rail, form filling, properties, the save password, all of Layout's page operations, merging a file into a loaded grid (but not opening one into an empty grid), and the editor's form fields.  Also: un-applied editor edits reach the native close guard through the push (nothing polls them), and closing a tab mid-edit asks about them |
| `test_comments_list.py` | the Comments panel lists every review markup (shapes, lines, highlights, Text-tool text) as well as comments, while only comments carry a comment index — so the popup's Save/Delete still lands on the comment it was opened on when markups sit before it on the page; inserted images and signatures are not listed |
| `test_frontend.py` → `js/viewer_comment_panel.test.mjs` | the right panel's three tabs (Comment / Edit / Fill & Sign): arming a tool brings its tab forward, leaving a tab puts its tool away, a lifted draft surfaces its own tab, the comments list shows on the Comment tab only, and the pre-3.2 mode names still work; the shared Text / Line / Fill style rows follow the armed tool (or the annotation being edited) and are hidden otherwise — Add text never shows a second Font row, a highlight shows no weight or dash; tool hints are locale keys; every listed item gets a typed card; markup gets no overlay hit area, only the ring its card click draws |
| `test_comment_threads.py` | review threads in the standard PDF shape (`/IRT` replies, `/StateModel /Review` statuses): everything added carries author, `/CreationDate`, `/M`, `/NM`; replies ride on their parent, draw nothing on the page and take no comment index (so the sticky popup still edits the right note); one status per item, `None` clears it; deleting a parent deletes its thread, and lifting one to move it brings the thread and creation date back; one undo step per change; an Acrobat-style thread is read and its private Marked ticks ignored; the reviewer name persists, cleaned |
| `test_locales.py` | every locale has exactly the English keys, keeps every `{placeholder}`, and actually translates EVERY string — about 60 per language (toasts, errors, confirm dialogs, update checks, print navigation) had shipped in English; names, units and words a language spells the same are listed as exemptions, and a stale exemption fails too |
| `test_panel_tabs.py` | every tool button sits on the tab `_TOOL_TAB` says it belongs to — if they disagree, arming a tool brings forward a tab that does not contain it, and its settings are out of sight |
| `test_frontend.py` → `js/license_gate.test.mjs` | the first-run licence gate fails CLOSED — fetch does not reject on 4xx/5xx, so a 403/429/500 error body parses fine and has no `accepted` field; only an explicit `accepted: false` on a successful reply may raise the blocking dialog, while a genuine first run still is |

The dependencies used (`pypdf`, `pikepdf`, `Pillow`, `python-docx`) are already
in `requirements.txt`.

## Front-end tests

`tests/js/` runs the real `src/frontend/js/*.js` in a Node `vm` context on top
of `dom_stub.mjs`, a headless DOM that models only what the viewer reads: a
scrollable `#canvas-area`, a `#canvas-column` whose children stack vertically
(so `offsetTop` and heights behave), and inert stubs for every other element.
`viewer_harness.mjs` loads `viewer-panel.js` into that context along with the
globals `shared.js` normally provides (and `editor.js` on request), and fakes
the viewer's API calls — so the tests drive the actual code paths, not a copy of
them.  Page blocks lay out at their canvas size, so a zoom really does change the
geometry the scroll math has to compensate for, and window-level listeners are
recorded so a test can fire a real ctrl+wheel gesture.

`loadViewer(docs, opts)` takes `editor` / `layout` to load those panels too, and:

* `shared: true` loads the **real** `shared.js` in place of the harness's shims
  for the globals it owns.  Anything testing unsaved-changes behaviour needs it,
  because that mechanism lives in the seam between the two files — the flag and
  the bridge push are `shared.js`'s, the per-tab parking and each panel's Save
  are the panels' — and a shim of either half cannot catch a panel that never
  calls the other.  (The prelude splits in two for this: `shared.js` and the
  shims both declare the same `let`s, and two `let`s of one name in the context's
  lexical scope is a `SyntaxError`.)
* `native: true` installs a `window.pywebview` bridge, so `isNativeApp()` is true
  and the OS-save-dialog branch is the one that runs.  `h.bridge.unsaved` is then
  exactly what `entry.py`'s close guard would read, and `h.callsTo(url)` shows
  side effects that leave no trace in the page (the recovery snapshot being
  dropped after a save is one).

`test_frontend.py` discovers and runs every `tests/js/*.test.mjs`, so a new suite
needs no wiring beyond the filename.
