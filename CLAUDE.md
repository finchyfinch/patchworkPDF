# PatchworkPDF — notes for Claude Code

A lightweight PDF editor (Flask backend + WebView2 frontend) published on
SourceForge as PatchworkPDF. This file travels with every versioned folder
copy — keep it updated when project-wide facts change.

## Versioning workflow (no git)

- There is **no git repository**. Each release is a full folder copy:
  `C:\workspace\patchworkPDF\Source\patchworkPDF_source_v<X.YY>\`, with older
  versions moved to `Source\Old\`.
- Folders named `... - Copy` are throwaway backups taken before a change
  session. Never fix, reference, or seed anything from them.
- Consequence: any fix to config files (`.claude/launch.json`, this file, etc.)
  only persists if the *next* version folder is copied from the folder that
  received the fix.
- The running app's version comes from `version.json` in the project root.

## Every change set: changelog + What's New

- **Update `docs\changelog.docx`** with each user-facing change: a version
  heading paragraph (the bare number, `Normal`), one `List Paragraph` bullet per
  change, a blank paragraph; newest first. The update notifier downloads this
  file and parses it (`check_for_updates._notes_for_version`) — clone the
  previous release's paragraphs and check the parser reads the new entry.
- **Add the release's key changes to the What's New popup**
  (`#whats-new-modal` in `viewer.html`), every string in all five `locales\`
  files. Keep the previous release's note below the new one for users who skip
  a version.
- The Flatpak metainfo needs a `<release>` for the new version too, or
  `build_flatpak.sh` refuses to build.

## Layout (since the v2.15 restructure)

- **Entry point: `src\backend\entry.py`** — it moved here from the project
  root in v2.15. If a launch config or doc says plain `entry.py`, it is stale;
  fix it rather than working around it.
- `src\backend\` — Flask app: `routes\`, `services\` (e.g.
  `check_for_updates.py`), `platform_io\` (paths per OS), `bootstrap\`, `i18n\`,
  `pdf\`.
- `src\frontend\` — `viewer.html` (the single SPA shell since v2.00; there is
  no `editor.html`), `css\`, `js\`. In `js\`: `viewer-panel.js` holds the
  annotation tools AND the page-editing tools (the Editor panel was merged into
  it in v3.1 — there is no `editor.js`), `layout.js` the page/word layout
  model, `shared.js` common helpers.
- `routes\editor.py` has no Editor behind it any more: it is the extraction /
  OCR / `apply_edits` library the viewer's page tools call, plus the lazy
  heavy-deps guard (`_NEEDS_HEAVY`) and Layout's `/api/header_footer`. Its
  `/api/editor/*` routes were removed with the Editor. **A new viewer route
  that calls into it must be added to `_NEEDS_HEAVY`**, or it fails in the
  moments after launch before the background load finishes.
- `locales\` — UI strings; keep `data-i18n` keys in sync when editing HTML.
- `assets\tessdata\*.traineddata` — ~11 MB of OCR language data (eng/spa/fra/
  deu/ita). **Part of the shipped payload, not optional and not git-ignored** —
  a folder copy that loses them yields an installer that only OCRs in English.
  Restore/verify on any platform with `python tools\fetch_tessdata.py`
  (`--verify` to check only). Windows/macOS bundle them via `setup.py`; the
  Flatpak downloads its own pinned copies. See `assets\tessdata\README.md`.
- User state (update prefs, license, recents) lives in
  `%LOCALAPPDATA%\PatchworkPDF\`, not in the project.

## Native-window traps (pywebview / WebView2)

- **pywebview serialises the whole DOM into every DOM event it sends to Python.**
  `webview/js/api.js` runs any `Node`-valued event property through
  `domJSON.toJSON` with its default `deep: true`, so a `drop` on `#layout-main`
  used to ship the entire page grid — every card's inline
  `data:image/png;base64,...` thumbnail included — across the bridge. At ~100
  pages that froze the UI thread for seconds. `entry.py`'s
  `_shallow_dom_serialization()` forces `deep: false` at page load; keep it, and
  never assume a Python-side DOM handler is cheap.
- **A native modal steals keyboard focus from the page and does not give it
  back.** After the OS Save/Open dialog (or the print preview iframe) closes,
  focus sits on the WinForms host, not the WebView2 child, so document-level
  shortcuts go dead while toolbar clicks keep working — the "Ctrl+P does nothing
  after a Layout save" report. Every path that opens a native modal must call
  `win_chrome.focus_web_content()` (Python) / `restoreWebFocus()` (JS) on the way
  out.
  - **One push is not enough, and `SetFocus` can silently no-op.** Windows moves
    focus again while the modal is destroyed, so a push made a moment too early
    is undone; and the first `Chrome_RenderWidgetHostHWND` under the window is
    not necessarily the page's (the print preview spins up its own, and a
    torn-down one outlives its frame). `focus_web_content` therefore tries every
    visible candidate and CHECKS `GetFocus()` rather than hoping, and callers
    re-assert across the teardown (`_reclaimNativeFocus` in JS).
  - **For printing, the JS signals are all unreliable, so the watcher is native.**
    `afterprint` is flaky in WebView2's PDF viewer; the window `focus` EVENT only
    fires if focus returns to the web content, which is precisely what fails; the
    `mousedown` fallback needs the click that would have fixed it anyway.
    `WindowApi.begin_print_focus_guard()` (armed before `print()`) watches
    `GetForegroundWindow` instead — the dialog is modal and ours, so our window
    regaining the foreground IS the dialog closing — then re-asserts focus until
    it verifiably sticks. Guarded by `tests/test_print_focus_guard.py`.
- **Foreground rights do not belong to the process that owns the window.**
  Windows lets only the foreground process (or one it has blessed) call
  `SetForegroundWindow`; everyone else is denied and the taskbar button flashes
  instead. Explorer grants those rights to the process it *launches* — the
  short-lived second instance — while the window belongs to the primary, so a
  double-clicked file opened its tab behind a minimised window. The second
  instance therefore calls `AllowSetForegroundWindow(primary_pid)`
  (`single_instance._grant_foreground`) before it POSTs, and
  `win_chrome.bring_to_front` drives the un-minimise from `GetWindowPlacement`
  (honouring `WPF_RESTORETOMAXIMIZED`) with an `AttachThreadInput` fallback, so
  it still works when no grant was made. Front the window BEFORE `_preload_file`
  — a big scan takes seconds, and the grant is only good while the forwarding
  process is still blocked on the POST. Guarded by
  `tests/test_file_handoff_window.py`.
- **On Linux, pywebview's own JS bridge needs eval, and the CSP forbids it.**
  WebKitGTK blocks `new Function` in pywebview's `js/api.js` (so
  `window.pywebview.api` never exists, `isNativeApp()` is false, and Save
  silently downloads instead) and the `eval()` wrapper `evaluate_js` puts round
  every js_api return value (so a native dialog's promise never resolves).
  `entry._csp_safe_js_bridge()` patches both at startup — don't grant
  `'unsafe-eval'` instead. The replies end on `void 0`: pywebview's gtk
  backend converts an OBJECT result with a JSC call the GNOME 50 runtime
  lacks, and logged a traceback per call. It is written against pywebview 5.4 and logs
  `[gtk-csp]` if a new release changes shape; re-run
  `tests/test_gtk_js_bridge_csp.py` after any pywebview bump. Up to v3.2 every
  Flatpak shipped with this broken.
- **Nothing touches pdfium/pikepdf off the main thread until the window is
  up.** v3.3 briefly pre-loaded the double-clicked document on a worker thread
  to overlap the WebView start-up; it ran while the main thread loaded .NET and
  WinForms, the CRT heap was corrupted, and pdfium aborted the app
  (`0x80000003`, faulting module `pdfium.dll`) on about half of all
  double-click launches. `_NATIVE_LOCK` cannot prevent that — the CLR start-up
  is not ours to lock. After start-up every pdfium/pikepdf call holds
  `_NATIVE_LOCK` (an RLock), `pdf_core`'s thumbnail/preload paths included.
  Guarded by `tests/test_launch_document_native_safety.py`.
- **A closing instance still holds the single-instance lock for seconds**
  (WebView2 tear-down + the clean-exit purge; far longer on a slow laptop). Once
  the last window closes the app is `_APP_CLOSING`: `/api/ping` reports it and
  `/api/open_in_new_tab` answers 503, and a launching process re-tries the lock
  while it waits (`single_instance._defer_to_running_instance`, up to
  `_HANDOFF_WAIT`) and starts fresh when it frees. Before this, opening a PDF
  within a few seconds of closing said "already running" (or handed the file to
  the dying process, which lost it). Guarded by
  `tests/test_relaunch_while_closing.py`.
- **Never keep a setting only in localStorage.** The page's origin includes the
  launch port (49371, then 49372–49375, then any free one) and WebView2's data
  sits under the roaming `%APPDATA%\pywebview`, so either changing makes
  localStorage come back empty — a work PC reverted to dark mode on every
  restart. Theme lives in `ui_prefs.json` and language in `lang.txt` (user data
  dir), handed to the page as `window.PW_PREFS` at the top of `<head>`, before
  `i18n.js` runs. Guarded by `tests/test_ui_prefs.py` and
  `tests/js/theme_persist.test.mjs`.
- **Office conversion is single-instance.** Word/Excel COM hands every thread the
  SAME `Application` object, so two concurrent conversions kill each other and
  report "no compatible office application". `pdf_core._OFFICE_LOCK` serialises
  them — don't route around it.

## Unsaved changes ("is this tab stale?")

One rule, and it is symmetric — both halves have been broken in the field:

- **Every change flags the document**: `pwSetDocDirty(true)` (shared.js) on any
  path that alters the session — annotations, every page tool (Edit text,
  Row / area, Add text, Redact, Crop, Watermark), form fields, the viewer's
  page-rail rotate/delete/insert, form filling, bookmarks, document
  properties, the save password, and *every* Layout page operation. Layout had
  none of these until v2.22 and silently discarded reshuffled documents.
- **Every full-document Save unflags it**: `pwMarkSaved(sessionId)` (shared.js),
  which clears the flag and drops the crash-recovery snapshot. There are two
  Saves (viewer / layout) × three write paths (OS dialog, `showSaveFilePicker`,
  download) = **six endings, and all six must call it** (there were nine while
  the Editor had its own Save). When only the viewer's did, an editor Save left
  the document flagged
  forever — and because `_saveActiveTabMeta` parks the live flag against the tab
  you leave, that phantom followed the tab and the app warned about unsaved
  changes on every close, every launch. Saving a strict SUBSET of pages (Layout's
  checked-pages export) writes a *different* document and must NOT clear.
- **The native close guard is push-only.** `beforeunload` never fires in the
  host and the CSP forbids eval, so `entry.py` can only read what the page last
  pushed via `pwPublishUnsaved()`. A dirty-check whose answer can change without
  `pwSetDocDirty` running (a background tab's parked flag) must publish for
  itself — see `_renderTabBar`.
- **Server side, `recovery.mark_dirty(session, page_id)` is for a byte rewrite;
  `recovery.mark_meta_dirty(session)` for a change that touches no page bytes**
  (page order, deletion, filled values, bookmarks, metadata) — it rewrites the
  manifest and prunes orphans without re-writing unchanged pages. Marking is not
  optional bookkeeping: `has_pending()` is also what stops the session LRU
  evicting a tab the user is still editing.
- Guarded by `tests/js/doc_dirty.test.mjs`, `tests/test_unsaved_marking.py`,
  `tests/test_recovery_snapshot.py` and `tests/test_close_guard.py`.

## Viewer annotation mutations

- **Every annotation mutation is a read-modify-write of one page's PDF bytes
  PLUS a push onto the shared undo stack, and Flask serves concurrently.** The
  whole handler must run under `get_session_lock` — taking it only for the
  snapshot push (which `add`/`update`/`delete_annotation` used to do) leaves the
  read and the write outside it. Two overlapping mutations then both read the
  same bytes, the second write is built from a page that never saw the first,
  and an annotation silently disappears; the undo stack is left holding two
  snapshots of the SAME state, and `_annot_apply` discards a snapshot equal to
  the current bytes, so one Ctrl+Z reaches past the action the user meant. Six
  concurrent draws used to leave ONE annotation on the page. The
  `_serialised_on_session` decorator in `register_viewer_routes` is what enforces
  it; the lock is reentrant, and the native render lock is always taken inside
  it, never the reverse.
- **The client must queue them too, or the order is not the user's order.** The
  server records one undo step per mutation in ARRIVAL order, so anything fired
  without queueing overtakes a request still in flight. `commitDraft()` only
  QUEUES its POST, so "draw a shape, then Ctrl+Z" sent the undo FIRST and
  reversed the previous annotation while the new one landed on top — the
  "removed annotation lines out of order" report. Every mutation goes through
  `_queueAnnotOp` (viewer-panel.js): draws, pastes, sticky notes, signatures,
  popup edits, lift deletes — and undo/redo themselves. Never call one of these
  endpoints with a bare `_vFetch`, and never nest `_queueAnnotOp` inside a queued
  op (`_doPasteBatch` deliberately uses `_vFetch` because it already runs inside
  one).
- Guarded by `tests/test_annot_undo_ordering.py` and
  `tests/js/viewer_annot_order.test.mjs`.

## Viewer page coordinates (the CropBox origin)

- **One wire convention: the client speaks the VIEWER DISPLAY FRAME, and the
  server translates. Never the reverse.** The frontend measures everything from
  the top-left of the rendered raster; every coordinate the PDF itself carries
  (an annotation `/Rect`, a pdfium charbox, an OCR word box) is in absolute user
  space. Those are the same frame only when the page's CropBox starts at the
  origin — which is almost every page, and is why the two were conflated for
  years.
- **`_page_display_frame(pdf_bytes)` returns everything a conversion needs**:
  `(rotate, raw_W, raw_H, off_x, off_y)`, the last two being the CropBox
  lower-left from `_page_crop_origin`. Use it at any site that moves geometry;
  `_page_rotation_and_raw_dims` alone is what let the offset be forgotten.
- **Order matters, and it is asymmetric.** `_raw_rect_to_display` translates
  (absolute → CropBox-relative) and THEN rotates: the rotation maths works in an
  extent of `raw_W x raw_H` anchored at (0,0), which is only the page's real
  frame once the CropBox origin is off. `_display_rect_to_raw` un-rotates and
  THEN translates back, and must not pass the offset into the inner
  `_raw_rect_to_display` call or it translates twice.
- **`if rotate:` is not a sufficient guard** — a cropped page needs the
  translation whether or not it is rotated. Three sites guarded on rotation
  alone (`add_annotation`, search highlights, `page_text` chars) and so skipped
  the work entirely on an unrotated cropped page. Guard on
  `rotate or off_x or off_y`.
- On a cropped page this displaced **everything the viewer read or wrote** by
  exactly (CropBox.left, CropBox.bottom) — annotations, links, search highlights
  and text selection alike. It needed no unusual file: cropping a page with the
  editor's own Crop tool and then annotating it in the viewer reproduced it.
- A site that only takes WIDTH/HEIGHT from a transform (the FreeText `/AP`
  layout boxes) does not need the offset — a translation does not change an
  extent. Passing it there is harmless but pointless.
- **Form fields were the third instance and the worst.** `form_utils` stores a
  widget's geometry off its `/Rect` (absolute user space) with `page_h` from the
  MEDIABOX, while the viewer renders the CropBox and maps `x1 * s` /
  `(page_h - y2) * s`. On a cropped page that put the overlay *off the page
  entirely* — invisible and unreachable, not merely displaced — and on a rotated
  page `page_h` was the wrong axis. `/api/viewer/form_fields` now translates on
  the way out via `_field_rect_to_display`, returning COPIES: the stored dict
  stays in user space, which is what form_utils and Save work in.
- **The viewer's `/api/viewer/*_form_field` routes speak the display frame and
  convert at the boundary.** The Editor had user-space twins (removed in v3.1);
  one route with two conventions is what caused all three instances of this
  bug. They are serialised — a field write can race an annotation on the same
  page bytes, and `tests/test_form_field_coords.py` fails without it.

## Viewer page tools (Redact, and the rest of Phase 1)

A *page tool* rewrites the page's content instead of adding an annotation
object. See `docs/EDITOR_VIEWER_MERGE_PLAN.md` for the whole programme.

- **They are undoable for free.** `_annot_push_undo` snapshots whole page BYTES,
  so pushing one before the rewrite gives a content edit full Undo/Redo through
  the existing toolbar buttons. Push it BEFORE the mutation and skip it on any
  path that refuses, or a no-op spends an undo slot the user then has to press
  through.
- **`_page_tool_offset` converts the viewer's display frame to what
  `apply_edits` wants**, which is *not* the same thing as user space: it is the
  CropBox origin on an unrotated page and nothing on a rotated one, because
  `_editor_display_orientation` moves the editor's own origin to the display
  frame there. Reproduce that decision; don't re-derive it.
- **A page that is cropped AND rotated is refused** (`_page_tool_refusal`).
  `apply_edits` cannot place content on it — measured up to 100px out — and for
  redaction that is a *security* failure, not a cosmetic one: the box lands
  elsewhere, nothing is covered, and the operation reports success while the
  text stays readable. The viewer's own annotation path handles that geometry
  correctly, so this is specifically about what `apply_edits` can be trusted
  with. A refusal must leave the page byte-identical.
- **Redaction destroys rather than covers**: `apply_edits` scrubs the native
  text and `_burn_redactions_into_images` destroys the pixels inside any image
  XObject. Verified end to end — redacting a word removes it from
  `/api/viewer/page_text` while the rest of the page survives, and Undo brings
  it back. The panel says so, because "Undo works until you save" is the part
  users need to know.
- Client side the DRAG tools share `_pageToolDrag*` (one drag, per-tool
  `commit`), never the draft/`commitDraft` machinery — there is no annotation
  object to commit — and still go through `_queueAnnotOp` like every other
  mutation.
- **Watermark is a page tool with NO geometry**, and therefore no drag, no
  `_page_tool_offset` and no refusal: `apply_edits` derives the page centre from
  the CropBox itself, so it is correct on all eight
  {plain,cropped}x{0,90,180,270} combinations including the one Redact has to
  refuse. That was measured before the route was written; don't copy Redact's
  shape onto it. It previews live over the page in view (an SVG overlay the
  viewer keeps a direct reference to in `_wmPreviewEl`) and a button stamps it.
  Its settings are clamped server-side because they reach a content stream.
- **All-pages watermarking is a File-menu command, not a panel tool.** N pages
  means N byte snapshots against a 30-entry / 64 MB history, so "undo" would
  half-revert a long document — worse than none. `/api/viewer/watermark_all` is
  therefore outside the undo stack, and **clears it**: leaving earlier entries
  would let one Ctrl+Z restore a single page to a pre-watermark snapshot,
  removing the mark from that page alone. It confirms first and says all three
  things (every page, not undoable, history cleared).
- **The server owns the per-page loop; the client sends ONE request.** The
  shipped editor path had the client loop over the pages calling
  `/api/watermark/apply_all` once per page — while that route loops over every
  page itself and reads `exclude_page_id`, which the client never sent. An
  N-page document got **N stacked watermarks on every page**, measurably darker
  each pass (3 pages: mean ink 187 vs 225 for one). The viewer route does not
  repeat it; pinned by `tests/test_viewer_watermark_all.py`. (The editor route
  and its looping caller were removed with the Editor.)
- **`routes/editor` loads `pdfium` lazily** in `_heavy_init()` — kicked off in
  the background at startup. Anything calling into that module in the first
  moments of a launch sees `pdfium is None`; the `_NEEDS_HEAVY` before_request
  guard is what forces the load for the viewer routes that need it.
- **Crop is the only tool that changes the page's SIZE**, and everything that
  records a size has to move together: `allPages[i].width_pts/height_pts` (what
  the opening zoom is fitted to), `_pageBlocks.get(id).pageW/pageH` (what every
  overlay's maths is calibrated against), and `pageW`/`pageH` for the page in
  view. `_applyPageSize` does all three; `_refresh_page_size` is the server
  half. **Undo needs it too** — a restored snapshot can be a different size, so
  `_annot_history_payload` re-derives and reports it.
  - The page BLOCK needs no help: it has no explicit height, so it reflows to
    the new canvas. Measured end to end — cropping 612×792 → 440×440 took the
    block's own rect from 306×396 to 220×220 with no stale gap. This was the
    plan's one open question (§3.4) and the answer is that it already works.
  - `_page_dimensions` measured the **MediaBox** while pdfium renders the
    CropBox, so a cropped page was listed at its uncropped size — a fifth
    instance of the same conflation, and a violation of the "as displayed"
    contract its own docstring states. Now reads the CropBox.
- **Add text is not the annotation Text tool, and the difference matters.**
  `at-tool-text` writes a FreeText annotation — an object on top of the page
  that can be selected and deleted. `at-tool-pagetext` ("Add text") writes into
  the page CONTENT via `apply_edits`'s `markup` key: afterwards the words are
  indistinguishable from the document's own and cannot be selected. Keep them
  named and grouped apart; two tools called "Text" in a 260px panel was flagged
  as a hazard before either existed.
  - v3.2 merged Page text and Text Box into this one tool: a press that moves
    less than `_PAGE_TOOL_MIN_PX` is a click and opens a one-line entry (the
    `click` hook on its `_PAGE_TOOL_DRAG` spec), a real drag opens a wrapped
    box. There is no `pagetextbox` tool any more.
  - The text box is wrapped BY THE CLIENT, because only the client can measure
    the font it is previewing. The viewer must not call the editor's
    `wrapTextLines`: that uses the editor's live canvas `ctx`, which does not
    exist until the editor panel has been opened. `_ptWrap` has its own
    measure-only canvas.
  - Clicking away commits, only Escape discards, and the style controls are
    excluded from "away" — changing the size mid-edit is part of the edit.

## The right panel: three tabs and the comments list (v3.2)

- **Three tabs, split by what a tool does to the document**: Comment (objects
  on top of the page — listed, removable), Edit (changes the document itself;
  Undo until you save — plus Image), Fill & Sign (Field, Sign). The comments
  list sits under the Comment tools, and shows on that tab only.
  - **A tool's tab is recorded twice** — where its button sits
    (`#rp-group-<tab>` in viewer.html) and `_TOOL_TAB` in viewer-panel.js —
    and `tests/test_panel_tabs.py` checks they agree. A new tool needs both.
  - `_armTool` brings the tool's tab forward; `_updateAnnotDeleteBtn` brings
    forward the tab of a lifted draft (an image lifted while Comment shows);
    `setRightMode` puts away a tool or draft that belongs to the tab being
    left, so nothing is ever armed with its settings out of sight.
  - `setRightMode` still accepts the pre-3.2 names: `'annots'` → `'comment'`,
    `'tools'` → leave the tab alone (the manual's scene scripts used both).
  - `_RESTRICTED_CONTROLS` locks `#annot-tools` (every tool on every tab) but
    NOT `#rp-comments`, so a read-only document still lists and jumps to its
    comments.
- **The Add comment box is gone.** The Text tool is the text comment; the
  Add-Note path (`addComment`, `_textIsComment`, `_textPrefill`) was removed.
  Comment FreeText written by older versions still opens its popup.
- **One word for the review objects: "Comments".** The list, its empty state
  and the toasts say it; before v3.2 the same thing was "Notes",
  "Annotations", "comments" and "Add Note" depending on where you looked. The
  collapsed tab says **Tools** because it opens all three tabs.
- **Tool labels may wrap to two lines** (`.at-tool-lbl`), and single words too
  long for a ~43px tile carry soft hyphens (U+00AD) in `locales/*.json` —
  German "Wasser­zeichen". There is no `overflow-wrap:anywhere`: it broke
  "Rectangle" mid-word. Measure every locale when adding or renaming a tool.
  The tab names are sentence case because uppercase German does not fit three
  abreast.
- **Review threads** (author, dates, replies, status) use the standard PDF
  shapes so they interoperate with Acrobat: a reply is a `/Text` with `/IRT`
  → its parent; a status is the same plus `/StateModel /Review` and `/State`.
  Both carry an EMPTY `/AP` — pdfium generates a note icon for a `/Text` with
  none, and a reply must not appear on the page.
  - **Anything with `/IRT` is never an item**: `_get_annotations` hangs it on
    its parent (`replies`, `status`), `_get_editable_annotations` skips it,
    and it takes NO comment index — `update_annotation` skips it too. Get
    either side wrong and the sticky popup edits a reply. Pinned by
    `tests/test_comment_threads.py`.
  - **The thread goes where its parent goes.** `delete_annotation` and the
    popup's delete cascade (`_thread_positions`). Lifting deletes the parent,
    so the editable info carries `replies` + `created`, the draft keeps them,
    and `_draftToAddBody` sends them back for `add_annotation` to re-attach —
    moving a shape must not drop its discussion. A COPY (`_copyLiveDraft`)
    strips both: a pasted shape is a new item.
  - Thread edits go through `/api/viewer/comment_thread` (reply / status /
    delete_reply, by raw `annot_idx`), queued via `_queueAnnotOp`, one undo
    step each. One status per item; `'None'` clears it.
  - Everything the viewer writes gets `/CreationDate`, `/M` and `/NM`. The
    author is the Comment tab's **Name** field, persisted by
    `/api/reviewer_name` in the user data dir (not localStorage: the page's
    origin includes a port that can change between launches).
- **Toasts go through the locale files.** Tool-arming hints are
  `toast.arm_<tool>`; commit toasts are `toast.updated` / `text_added` /
  `added_listed`. Don't add a `showToast('literal')`.
- **The list shows every review markup, not just comments.**
  `_get_annotations` returns sticky notes and comment FreeText (`markup:
  False`, with `idx`) AND shapes, lines, highlights, Text-tool FreeText and
  foreign text markup (`markup: True`, `idx: None`). Stamps are left out: the
  viewer writes one for every inserted image and signature.
  - **`idx` is the index basis `update_annotation` walks** — it counts comments
    only. Markup must never take a comment index, or the popup's Save/Delete
    lands on a different comment. Pinned by `tests/test_comments_list.py`.
  - **Markup gets no overlay hit area** (`drawPageAnnotations`) — lift-to-edit
    owns its clicks. It only gets the ring when its card is clicked.
- **The shared Text / Line / Fill rows follow the tool** (`_STYLE_ROWS` /
  `_refreshStyleRows`): the armed tool, or with nothing armed the draft being
  edited, else hidden. A new drawing tool must be added to `_STYLE_ROWS` or it
  shows no style controls. Called from `_armTool`, `_disarmTool` and
  `_updateAnnotDeleteBtn` (which every draft create/teardown already calls).
- The session-only notes store (`/api/viewer/comments`) was removed in v3.2 —
  nothing had written to it since Add Note became a FreeText comment. The
  `'comments': {}` key on sessions and in the recovery manifest is inert.
- Guarded by `tests/test_comments_list.py`, `tests/test_comment_threads.py`,
  `tests/test_panel_tabs.py`, `tests/test_locales.py` and
  `tests/js/viewer_comment_panel.test.mjs`.

## Editing the page's own text from the viewer (the Pointer)

- **The gate is two conditions: the Tools panel OPEN and the tool ARMED**
  (`_etActive`). There is no separate editing mode by design, but a bare click
  on the page must never start rewriting the document — otherwise someone
  selecting text to copy it ends up editing. Collapsing the panel is how the
  toolset is put away, so `toggleRightPanel` ends editing too.
- **Elements load ONE PAGE at a time, on demand, and follow the scroll.**
  `/api/viewer/page_elements` runs the editor's extraction, which OCRs a
  scanned page — loading every page on screen would be ruinous on a long scan.
  It deliberately omits the editor's hi-res render (~250 KB) because the viewer
  already has the page.
  - With several pages on screen only one has hit-areas, so a click on text on
    ANOTHER page moves the Pointer there (`_etPageClick` → `_etLoadPage`) and
    selects the clicked word once it loads. `_etWant` is the page asked for;
    edits and invalidations reload that page, not the one in view.
  - The hit-areas are positioned in screen pixels: `redrawPageBlock` (every
    zoom) must re-render them, or after a zoom clicks miss the words.
  - Changing tab puts any armed tool away (`_putToolAway`, called before
    SESSION_ID moves so a half-drawn shape commits to its own document).
- **Each edit commits immediately: one request, one byte snapshot, one Ctrl+Z.**
  The editor stages edits and bakes them at Apply; the viewer does not, so there
  is one undo system rather than two. The cost is that a rewrite invalidates
  every element id, so a successful edit RELOADS the element model.
- **Editing text does not remove it.** `apply_edits` paints a
  background-coloured rectangle over the original glyphs and injects the
  replacement on top; the old words stay in the content stream.
  `covered_text_bboxes` on the page record is what stops them being offered for
  editing again.
  - **So the undo snapshot has to carry that list.** Undo restores the
    *uncovered* original bytes, and without its metadata the restored text stays
    filtered out — visible on the page and impossible to edit ever again. Undo
    entries are therefore `(page_id, bytes, meta)`; `_PAGE_EDIT_META` says what
    travels. Anything reading those stacks must INDEX rather than unpack, since
    older entries may be 2-tuples.
- **Selecting a run must NOT rebuild the overlay.** Selection happens on
  MOUSEDOWN, and a browser only fires `dblclick` when both halves of the gesture
  land on the SAME element — so re-rendering the hit-areas from `_etSelect`
  destroys the node under the pointer between click one and click two and the
  double-click that opens the editor never arrives. That shipped: the run turned
  yellow and then refused to open, on every document. `_etSelect` therefore
  toggles the `sel` class on the existing children. No stub can model the
  browser's click bookkeeping, so the test pins the invariant it rests on —
  node identity survives a selection.
- **A PDF does not reflow, so a longer replacement has to be made to fit.**
  `apply_edits` injects text at the original run's x; nothing pushes the next
  word along, so "Invoice No: 106688" edited to "Statement" rendered as
  "StatementNo: 106688". `_fit_to_available` measures and condenses. The rules,
  each of which was arrived at by getting it wrong first:
  - **Only a MEASURED neighbour constrains it.** `_same_line_neighbour_x`
    returns None when the rest of the line is free, and None means *do not
    constrain* — inventing a page-edge limit would mean guessing at a
    coordinate frame to solve a problem that is not there.
  - **The original's own footprint is always available** (`min_x2`). Extraction
    routinely reports a neighbour starting *inside* the previous run's box, and
    taking that at face value condensed replacements SHORTER than what they
    replaced.
  - **Never eat the word space.** Stopping a hair short of the next word's ink
    is not an overlap but reads as one. As much of the original's trailing gap
    as a space in that font occupies is reserved — and only that much, or a run
    at the end of a sentence would be condensed with inches of room beside it.
  - **Sub-point overruns are left alone** (`_FIT_SLACK_PTS`). Adjacent runs on a
    justified line sit a point or two apart and their boxes are rounded.
  - **Condense (`Tz`) before shrinking.** Tz keeps the baseline and cap height,
    so a few percent is invisible; a smaller font size sits obviously wrong
    beside unchanged neighbours. **Tz is text STATE and every injected block
    lives inside the same outer `q`**, so a block that sets it must also put it
    back or it squashes everything emitted after it.
  - **Stop at the floors and SAY SO.** A word replaced by a sentence cannot both
    fit and stay readable, and 3pt text loses the content as surely as an
    overlap does. `too_long_for_space` comes back on the response and the viewer
    warns; `shrunk_to_fit` covers the milder case.
- **Measure widths from the font's own width table, never from Pillow.**
  `font_assets.string_width` reads the same `charWidths` that become the
  embedded font's `/Widths`, so it is not an approximation of the output, it is
  the output's arithmetic. `ImageFont.getlength` returns HINTED, pixel-quantised
  advances — 'Invoice' measured 39.0pt at 12pt and 75.0pt at 24pt, a 4% error
  against a renderer that does no hinting, which is enough to mis-fit at the
  boundary. It also must not kern, because a `Tj` string does not.
- **A scanned page stops looking scanned the moment you edit it.**
  `_extract_text_and_graphics` decides whether to OCR from a char count, and
  `apply_edits` injects real text operators, so after one edit the next
  extraction takes the native path, sees only the run just injected, and every
  other word becomes uneditable — 178 items became 1 on a scanned invoice.
  `_preserve_ocr_source` stashes the untouched scan on the page record as
  `ocr_source_bytes` BEFORE the first rewrite (the editor's own fix, same key),
  and `_page_elements` passes it so OCR runs on the pristine bytes while native
  runs come from the current ones. It is called from every viewer route that can
  inject text — redact, watermark, watermark-all, add_text, edit_text — and
  stores `None` for a native page as the "already checked" marker. Crop is
  deliberately excluded: it changes the page geometry, so pre-crop OCR boxes
  would not line up.
- **`className` is not a string on an SVG element.** A page block holds SVG
  overlays, so walking its children with `(c.className||'').indexOf(...)` throws
  `indexOf is not a function` in a browser — and passes in the DOM stub, which
  models every className as a string. Use `classList.contains`.
- Guarded by `tests/test_viewer_edit_text.py`, `tests/test_text_fit.py`,
  `tests/test_viewer_scanned_edit.py` and `tests/js/viewer_edit_text.test.mjs`.
- **The stash does not survive Save.** `ocr_source_bytes` lives on the session,
  so a scan that was edited, saved and reopened has native chars and nothing
  saying it was a scan: only the edited words were editable and selection
  stopped OCRing (v2.24 had a manual Force OCR button; v3.1 had nothing).
  `editor.is_mixed_scan_page` recognises such a page — every native char is
  inside our `% pw_inj` blocks (the sample invoice draws glyphs as vector
  OUTLINES with no image at all, so image coverage alone misses it), or one
  image covers ≥60% of the page with sparse native text. `_ocr_source_for`
  (viewer) then OCRs the current bytes; `_extract_text_and_graphics` force-OCRs
  a source that has native text, and `_drop_ocr_under_native` /
  `_drop_words_under_chars` stop an edit being offered or copied twice (the
  raster shows it too). `_ocr_source_for` also ignores a stash whose page SIZE
  differs from the current page (a later crop). Guarded by
  `tests/test_viewer_mixed_scan.py`.
- **Runs are in ABSOLUTE user space, the overlay is the display frame.** On a
  cropped page they differ by the CropBox origin; `/api/viewer/page_elements`
  returns `page_left`/`page_top` and `_etBoxPx` / `_etOff` apply them. The
  single-run `move` op sends DISPLAY coords (server adds `_page_tool_offset`);
  `edit_block` sends its box in the ITEM frame, as the editor did.
- **Run ids are POSITIONAL (`txt_12`, `ocr_90`), so they go stale whenever the
  page bytes change** — not only after the Pointer's own edits but after
  Undo/Redo and page-rail reloads (rotate), where the same id can name a
  DIFFERENT run and an edit would rewrite the wrong words. Two guards, keep
  both: `_etInvalidate` drops the model on those paths (`_viewerHistoryNow`,
  `_resetViewerState`, `_pgCtxReload`), and the client sends `expect` (the
  text it means; a list for `edit_block`) which the server checks, answering
  409 `err.text_run_gone` and changing nothing on a mismatch.
- **Row / area editing** (v2.24's "Merge row"): right-click a run (or the panel
  button) → an 8-handle box seeded on the x-contiguous run of the word's line;
  drag to take words in/out across rows; Enter/double-click opens a textarea;
  Enter applies, Shift+Enter is a newline. `/api/viewer/edit_block` deletes the
  runs and injects ONE multiline markup item (`_coverRect`, `_capAnchor`, and
  `lineH` = the original rows' pitch — 1.35 em double-spaced a header cell).
  It records the footprint in `merge_regions`, NOT per-word covered boxes: a
  covered box would also hide the new block's own line where it runs past the
  words it replaced. Style comes from the runs (median em, modal font and
  colour), so white header text stays white.
  - **Alignment is read from the rows** (`_block_alignment`): whichever of
    their left edges, right edges or centres agree (tolerance max(2pt, 0.3 em),
    bias to left so justified/ragged text stays left; one row = left). The
    markup carries `align` + `_alignDX` (anchor as an offset from the box x),
    and apply_edits places each line with `font_assets.string_width` — the
    embedded font's own widths — so right-aligned lines end exactly on the
    original edge. The client's `_etAlign` mirrors the rule for the preview
    only; the server decides.
  - **The size is calibrated to the rows** it replaces: OCR's em estimate ran
    ~5% large on the sample header ("Line Amount" 56.0pt vs 53.3pt). Rows are
    re-set in our font and the size scaled to match, clamped 0.85–1.05; the
    client mirrors it (`_etCalibratedEm`) so its wrap agrees.
  - The client wraps at max(box width, widest ORIGINAL row set in our font):
    the box hugs scanned glyphs, and re-set at the estimated em a row can be a
    hair wider than its own footprint, turning 2 lines into 3.
- **The inline entry is painted in the run's colour on a contrasting
  background** (`_etEntryColours`). OCR samples real ink colour, so a white
  header run came in `#ffffff` and was typed white-on-white. It is sized from
  `font_size` (the em), not `size` (cap/ink height, ~70% of it), and its width
  is measured, not left to the `<input>` default.
- **Light-on-dark OCR gets a second read** (`ocr_utils.reread_light_on_dark`,
  both OCR paths). "VAT" against a band edge read "WAT" at 96% in BOTH passes,
  so neither confidence nor dedup can catch it. The crop re-read is only
  accepted for a same-length substitution or a one-letter insertion at much
  higher confidence (`_accept_reread`) — unrestricted, it turned "GROUP" into
  "GR". Guarded by `tests/test_viewer_mixed_scan.py` and
  `tests/js/viewer_edit_block.test.mjs`.

- Guarded by `tests/test_viewer_redact.py`, `tests/test_viewer_watermark.py`,
  `tests/test_viewer_watermark_all.py`, `tests/test_viewer_crop.py`,
  `tests/test_viewer_add_text.py`, `tests/test_viewer_edit_text.py` and the
  matching `tests/js/viewer_*.test.mjs`.
- **The editor's `apply_edits` pipeline is a different convention** (it works in
  MediaBox dims and does its own rotation via `display_to_raw`) and is still
  wrong for a page that is cropped AND rotated. The viewer's own path handles
  all eight {plain,cropped}x{0,90,180,270} cases; anything feeding `apply_edits`
  must refuse that combination rather than misfire — a redaction that lands
  elsewhere leaves the content readable while reporting success.
- Guarded by `tests/test_crop_coords.py`.

## Viewer request budget

- **Every API error on an `/api/` path answers in JSON** (`_json_http_errors` in
  `entry.py`). The front end parses replies with `res.json()`, so a Flask HTML
  error page surfaces as `Unexpected token '<', "<!doctype"...` and tells the
  user nothing. Don't add a route that returns an HTML error.
- **The rate limit is a burst guard, not access control** — that is the origin
  check plus the per-launch token. It has to clear what the viewer legitimately
  does, which is far more than a human clicking: at 120 req/10 s a fast scroll
  tripped it after ~40 pages.
- **A page's recorded size is its *displayed* size.** `width_pts`/`height_pts`
  in `/api/viewer/pages` come from `_page_dimensions`, which applies `/Rotate`
  (inherited `/Rotate` included) so they agree with pdfium's rotation-aware
  `page.get_width()`. The viewer fits its opening zoom to the widest page in the
  document from that metadata and then lays each page out at the size the
  renderer reports — list a landscape page as portrait and the document opens
  zoomed past the viewport, page 1 centred in an over-wide column with its right
  edge clipped (`Mixed_orientation.pdf`). Any new page-dict site must record
  them; `/api/rotate` swaps them on a quarter turn.
- **Base page renders go through a bounded queue** (`_renderQueue` /
  `RENDER_CONCURRENCY` in `viewer-panel.js`), and a queued render is re-checked
  against the current viewport before it is sent. All pdfium work is serialised
  on one process-wide lock (`routes/viewer.py`), so firing a render per page
  crossed put the page the user stopped on behind everything they had scrolled
  past. Keep the staleness band at least as wide as `PRERENDER_MARGIN` pages, or
  pre-rendering ahead of the scroll is undone as fast as it is requested.

## Document links

- **A /Link annotation with a /URI action is clickable in the viewer** — a
  `.pdf-link-hit` div per link in a `page-link-overlay`, hover-only styling, and
  a click that hands the URL to the OS browser. `/GoTo` and bare `/Dest` links
  are internal jumps and deliberately get no hit area.
- **The page sends a link INDEX, never a URL.** `/api/viewer/open_link` re-reads
  the destination from the open document (`_get_links`, the same function and
  the same ordering the page was given), so the only thing the route can be made
  to open is a link that is really in the PDF. The far end is
  `os.startfile`/`xdg-open`, which resolve `file:`, `javascript:` and every
  registered protocol handler on the machine, so `_LINK_SCHEMES` is an
  ALLOW-list — http/https/mailto — applied before the rect is ever emitted.
  entry.py's `/api/open_external` is the same concern from the other end and
  stays pinned to SourceForge; don't relax it into a general-purpose opener.
- **Ten call sites load a document into the viewer, and every one of them must
  call `loadAllLinks()`** alongside `loadAllAnnotations()`/`loadFormFields()`:
  seven in `viewer-panel.js` (tab switch, tab open, boot, page-rail reload,
  two password paths, recovery) and three in `shared.js` (layout adoption and
  the two panel-return paths). Wiring only the tab path and missing boot is
  exactly how this shipped broken the first time — the file opened from the
  command line had no links at all. `_resetViewerState()` clears `_allLinks`,
  so a missed site is a document with silently no links, not a stale one.
- Guarded by `tests/test_viewer_links.py` and `tests/js/viewer_links.test.mjs`.

## Running / testing

- `pytest` from the project root runs everything; each `tests\test_*.py` also
  runs standalone. `tests\js\*.test.mjs` holds front-end regression tests — they
  load the real `src\frontend\js\*.js` into a Node `vm` over a headless DOM stub
  (see `tests\README.md`). `tests\test_frontend.py` discovers and runs them all,
  so a new suite needs no wiring. Node is dev-only; without it on PATH they skip.
- **Tests must not read the shipped manual.** `docs/PatchworkPDF_Manual.pdf` is
  rewritten whenever the manual is updated; four suites used its page 2 as a
  "page with real text" fixture and would have silently changed what they
  test. They use the frozen `tests/fixtures/text_page.pdf` (see its README).
- **The user manual is generated.** `Documentation/Manual/manual_tools/` drives
  the real app headless (Playwright, throwaway settings folder) to capture
  every screenshot from the fictional documents in `sample_docs/`, then builds
  the .docx and exports the PDF through Word. Its README has the steps; update
  `build_manual.js` (all the manual's text) when a feature changes.
- Note when verifying in the Browser pane: if the pane isn't displayed the page
  stops compositing, so `scroll` events and `requestAnimationFrame` never fire.
  Scroll-tracking behaviour can't be checked that way — assert it in `tests\js\`.
- Dev servers are defined in `.claude/launch.json` (use the Browser pane's
  preview_start, e.g. `patchwork-verify` or `patchwork-verify-doc`). All
  configs must point at `src\backend\entry.py`.
- **The app does NOT serve on port 5000.** It picks its own port at runtime:
  `49371`, then `49372`–`49375`, then an OS-assigned ephemeral port if all five
  are busy (`bootstrap\single_instance.py`). The IANA dynamic range was chosen
  deliberately to stay clear of the crowded 5000/8000/8080 space, and the
  single-instance guarantee is an OS file lock, not the port — so a foreign
  process on a port no longer blocks startup or fakes "already running".
  - `PATCHWORK_PORT=<n>` overrides the list with a single port. Any
    `.claude/launch.json` entry that declares `"port": 5000` **must** also set
    `PATCHWORK_PORT=5000` in its `env`, or the app serves on 49371 while the
    Browser pane watches 5000 and sees nothing.
  - The live port is published in `instance.json` in the per-user state dir, and
    `/api/ping` identifies the server as `patchwork-pdf` (that is how a second
    launch finds the first rather than trusting the port alone).
- `PATCHWORK_NO_BROWSER=1` stops it opening a WebView window during headless
  testing.
- The right-hand annotation panel is a fixed 260px wide (`--right-w` in
  `css\viewer.css`) — check new controls fit at that width.
