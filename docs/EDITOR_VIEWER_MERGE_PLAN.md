# Editor → Viewer Merge — Upgrade Plan

**Status:** W0-W6 complete (2026-09-21). All four tools (Field, Redact,
Watermark, Crop) are in the viewer's Tools panel, all-pages watermarking is a
File-menu command, and five instances of the CropBox/MediaBox conflation are
fixed — four of them shipped bugs.
**Phase 1 is complete, including W7.** The Editor panel is down to its
text-editing set (Pointer, Hand, Text, Text Box); the four moved tools now live
only in the viewer.
**Phase 2 in progress.** Text and Text Box have moved (as *Page text* and *Text
Box*), and the Editor panel is down to **Pointer + Hand**.
**P2b done too:** the Pointer is in the viewer. The Editor panel now holds
nothing the viewer cannot do.
**Next:** retire the Editor button and panel, and ship the run-once "What's New".
**Author context:** Claude Code session, 2026-09-20, against `patchworkPDF_source_v3.1`.
**Goal:** retire the separate Editor panel by moving its tools into the viewer's
right-hand Tools panel, so there is one document surface instead of two.

This document is the durable reference for the whole merge. It is split into two
phases; **work stops at the end of Phase 1 for review before Phase 2 begins.**

> **Phase 1 (this document's subject):** move the four editor tools that fit the
> viewer's existing architecture — Redact, Crop, Watermark, Field — into the
> Tools panel. **The Editor button stays.** No "What's New" popup, because
> nothing the user relies on disappears.
>
> **Phase 2 (specified separately, later):** Pointer / real text editing in the
> viewer, then remove the Editor button and ship the run-once "What's New".

---

## 0. Why the split

The two panels look alike, and some of their code genuinely *is* duplicated —
`#at-text-font` (`viewer.html:375`) and `#ml-font` (`viewer.html:1512`) are
character-for-character identical 25-font `<select>`s, both panels share the
`at-tool` / `at-tool-row` / `at-ctrl-row` CSS vocabulary, and the viewer already
has its own Pointer and Hand tools (`viewerTool`, `viewer-panel.js:5523`).

But "the editor" is not one thing. Its tools divide cleanly by whether they need
the editor's *extracted element model* — the `text_items` / `graphic_items` /
`pdf_image_items` that `/api/editor/open` builds from a hi-res render plus
(on scanned or unmappable-font pages) a full OCR pass.

- Tools that **only need a rectangle on a page** can move now.
- Tools that **need the element model** cannot move without bringing the whole
  editor — its client-side staged-override model, its own `undoStack`, and its
  Apply/Discard commit boundary — across with them.

Phase 1 is the first group. It is worth doing on its own even if Phase 2 never
happens.

---

## 1. The enabling discovery

**The viewer's undo stack already snapshots whole page bytes, not annotation
objects.**

```python
# routes/viewer.py:176
def _annot_push_undo(session, page_id):
    ...
    stack.append((page_id, cur))   # cur = the page's full PDF bytes
```

This is the fact that makes Phase 1 cheap. A redaction, a crop and a watermark
are destructive content-stream edits — they cannot be represented as PDF
annotation objects and cannot be reversed by deleting one. But they do not need
to be: pushing a byte snapshot before the mutation and letting `_annot_apply`
restore it gives them full Undo/Redo through the **existing** toolbar buttons
(`v-btn-undo` / `v-btn-redo`) with **no new undo machinery at all**.

In the editor these same operations are staged client-side (`redactItems`,
`cropItem`, `watermarkConfig`) and only baked in at Apply. In the viewer they
become immediate, individually-undoable operations. That is a genuine
improvement, not just a relocation.

Bounds to respect: `_ANNOT_HISTORY_CAP = 30` entries and
`_ANNOT_HISTORY_BYTES = 64 MB` (`routes/viewer.py:157`). See §3.5 — this is why
"watermark all pages" is handled differently from the rest.

---

## 2. Tool-by-tool triage

### 2.1 Moving in Phase 1

| Tool | Why it fits | Main work |
|---|---|---|
| **Redact** | Drag a rect → black box burned into content stream + image XObjects. No element model. | New live endpoint; coordinate conversion |
| **Crop** | Drag a rect → `/CropBox`. No element model. | New live endpoint; **page-dimension refresh** (§3.4) |
| **Watermark** | Page-level stamp from a config object. No element model. Already has an all-pages sibling route. | New single-page endpoint; preview overlay |
| **Field** | **Already live-committed** via `/api/editor/add_form_field`; viewer already renders field overlays via `loadFormFields()` (`viewer-panel.js:6545`). | Least work of the four |

Note on Field: `applyEdits` says so explicitly —

> *"Form fields are intentionally not counted: they're committed live via their
> own endpoints"* (`editor.js:2657`)

**Corrected by W2.** That made Field the natural first tool, but the conclusion
drawn from it — "no new backend route at all, only arming + drag + the existing
POST" — was wrong. The existing route speaks absolute user space and is
unserialised, and the viewer's field *overlays* turned out to be a fourth
instance of the CropBox bug. Field needed three viewer-namespaced routes and a
geometry translation on the way out. See the W2 entry in §4.

### 2.2 Staying behind — correction to the initial assessment

**Force OCR does *not* move in Phase 1.** My first read put it in the "fits
today" group; reading `forceOCR()` (`editor.js:2537`) shows that is wrong. The
route returns `text_items` / `graphic_items` and the function's entire body
reloads the editor's element model:

```js
textItems    = data.text_items    || [];
graphicItems = data.graphic_items || [];
overrides    = {};
```

Outside the Pointer there is nothing for it to refresh. It belongs with Phase 2.

Also staying: **Pointer** (text editing), **Hand** (viewer already has one),
**Reset / Apply / Discard** (they exist to bound the staged-edit model, which
Phase 1 does not touch).

---

## 3. Cross-cutting work — do these once, before or alongside the tools

These items are not per-tool. Getting any of them wrong reproduces a bug class
the project has already shipped once.

### 3.1 Session locking — mandatory

The editor's mutating routes are **not** serialised. `editor_add_form_field`
(`routes/editor.py:4282`) does a bare read-modify-write of `p['pdf_bytes']`, and
`watermark_apply_all` (`routes/editor.py:3762`) does the same in a loop. That is
safe *today* only because the editor is a modal panel where one edit happens at
a time.

In the viewer these operations sit alongside concurrent annotation mutations on
the same page bytes. Per CLAUDE.md ("Viewer annotation mutations"), that is
exactly the read-modify-write race that once left **one** annotation on a page
after six concurrent draws.

**Every new or re-exposed mutating route in Phase 1 must carry
`@_serialised_on_session`** (`routes/viewer.py:2641`). The lock is reentrant, so
`_annot_push_undo`'s own acquisition nests harmlessly, and the native render
lock is always taken *inside* it.

Client side, the matching rule: **every mutation goes through `_queueAnnotOp`**
(`viewer-panel.js:3753`), never a bare `_vFetch`. The server records one undo
step per mutation in *arrival* order, so an unqueued POST overtakes one still in
flight and the user's Ctrl+Z reaches past the action they meant.

### 3.2 The coordinate contract — the one real trap

The two panels do not agree on where a page's origin is.

**Editor** (`editor.js:200`):
```js
function c2pdf(cx, cy) { return { px: cx/editorScale + pageLeft,
                                  py: pageTop - (cy/editorScale) }; }
```
`pageLeft` / `pageTop` come from the page's **CropBox** (`routes/editor.py:3670`),
so `c2pdf` yields **absolute PDF user-space** coordinates.

**Viewer** (`viewer-panel.js:4208`):
```js
const scale = st.renderScale * zoomLevel;
const px = startX / scale;
const py = st.pageH - startY / scale;
```
No CropBox term. `st.pageW` / `st.pageH` come from the render response, which is
pdfium's `get_width()` / `get_height()` — i.e. **CropBox-relative, origin (0,0)**.

And the backend's `display_to_raw` (`routes/editor.py:2599`) is *identity for
`/Rotate 0`* and works in **MediaBox** dims.

So: on an uncropped page (MediaBox == CropBox) the two agree and nothing breaks.
On a **cropped** page the viewer's numbers are short by the CropBox offset, and a
redaction drawn in the viewer would land displaced by exactly that offset.

**SUPERSEDED BY W1 — read this box, not the paragraph the plan originally had
here.** The plan proposed shipping `page_left` / `page_top` to the client and
adding a `_vToPdfPt()` helper so the frontend could compute absolute PDF
coordinates. **Do not do that.** Implementing the fix showed the opposite
invariant is the right one, and it is now the rule:

> **One wire convention: the client speaks the viewer display frame, and the
> server translates. Never the reverse.**

That is what eliminated the bug — the frontend was never changed at all. Giving
the page tools their own client-side convention would put two conventions on the
wire and recreate exactly the drift §3.2 exists to prevent. So:

- `_page_crop_origin(pdf_bytes)` → the CropBox lower-left corner.
- `_page_display_frame(pdf_bytes)` → `(rotate, raw_W, raw_H, off_x, off_y)`,
  used at every site that moves geometry.
- `_raw_rect_to_display` translates **then** rotates; `_display_rect_to_raw`
  un-rotates **then** translates. The inner call in the latter must not receive
  the offset, or it translates twice.
- `if rotate:` is not a sufficient guard — three sites skipped the work entirely
  on an unrotated cropped page. Guard on `rotate or off_x or off_y`.

The new page-tool routes (W3–W6) therefore accept **viewer-frame** coordinates
and convert server-side before calling `apply_edits`. Since cropped+rotated is
refused (§3.2b), that conversion is just `+= _page_crop_origin()` on an
unrotated page and identity otherwise.

### 3.2a The CropBox bug is already shipped, and reachable without a crop tool

W0 did not just confirm a gap in the plan; it found a live bug in v3.1.

`add_annotation` converts incoming geometry with `_display_rect_to_raw`
(`routes/viewer.py:3058`), which handles **rotation only** and whose `raw_W` /
`raw_H` come from pdfium — i.e. CropBox dims. There is no CropBox **offset**
term anywhere in the viewer's write path. So on a cropped page every annotation
the viewer writes is displaced by `(CropBox.left, CropBox.bottom)`. Measured
through the real route: **(−50, +80) pt on a page cropped by (50, 80)**.

And the app creates that condition itself. Crop a plain page with the editor's
Crop tool, return to the viewer, drop an annotation in the dead centre — it
lands 50pt left and 80pt up. Every step is a supported v3.1 action. Pinned by
`test_cropping_then_annotating_misplaces_the_annotation_today`.

This widens W1 from plumbing to a **bug fix**, and it is the strongest argument
for doing Phase 1 at all: the same change that enables the four new tools also
fixes annotations, stickies, signatures, images and highlights on cropped pages.

Scope note: the *read* direction (search highlights, link hit-areas, which go
through `_raw_rect_to_display`) has the same shape and is likely offset too.
W0 did not measure it. Check it during W1 — it is probably the same one-line
origin term, and it would be perverse to fix writing and leave reading wrong.

### 3.2b Cropped **and** rotated: refuse, do not misfire

Cropped+rotated is flagged as unsupported in the editor (`routes/editor.py:209`)
and W0 confirmed by measurement that **both panels misplace content on it**, by
varying amounts (editor: 0–100px out depending on the angle; viewer: worse).
Phase 1 inherits that; it must not pretend otherwise.

For most tools a misplacement is a visible cosmetic error the user can undo.
**For Redact it is a security failure**: on the cropped test page the viewer's
box landed entirely outside the visible area, so *nothing was covered* while the
operation reported success and the text stayed fully readable.

So Phase 1 adds a guard: **on a page that is both cropped and rotated, the
page-tools refuse with a clear message** rather than writing to a coordinate
frame neither panel gets right. `test_cropped_and_rotated_is_unsupported_in_both_panels_today`
pins the trigger condition for that check.

### 3.3 Dirty marking

One rule, symmetric, and both halves have been broken in the field:

- Every new tool calls `pwSetDocDirty(true)` on success — as `commitDraft` does
  (`viewer-panel.js:4800`).
- Server side, `recovery.mark_dirty(session, page_id)` for a byte rewrite.
  All four tools rewrite bytes, so all four use `mark_dirty`, not
  `mark_meta_dirty`.
- Nothing in Phase 1 adds a save path, so the "nine endings" rule is untouched.

### 3.4 Crop changes page dimensions — the one tool with a layout consequence

`/api/editor/apply` sets `/CropBox` (`routes/editor.py:3399`) but **never
updates `width_pts` / `height_pts` on the page record** — there is no
`width_pts` write anywhere in `routes/editor.py`. Today that is survivable
because the viewer only trusts `width_pts` for its *opening* fit and takes live
dimensions from the render response.

Once Crop is a viewer tool the user crops a page and watches it reflow
immediately, so this must be correct:

1. The crop route updates `page_rec['width_pts']` / `['height_pts']` — per
   CLAUDE.md, *"Any new page-dict site must record them."*
2. The response carries the new dims; the client updates `allPages[i]` and
   `st.pageW` / `st.pageH`.
3. `_invalidateViewerPage(page_id)` (`shared.js:641`) forces the re-render.
4. **Verify** the page block relayouts rather than leaving a stale-height gap —
   `_buildPageBlockDOM` deliberately replaces the canvas in place to avoid
   scroll jumps, which is the opposite of what a size change needs.

Item 4 is a genuine unknown; treat it as a spike at the start of the Crop work
item, not an assumption.

### 3.5 Watermark: single page vs all pages

Single-page watermark: normal tool, one undo snapshot, fine.

All-pages watermark is different. Pushing N snapshots for an N-page document
blows both history bounds (30 entries / 64 MB) — on a 100-page scan the stack
would trim to the newest entries and "undo" would half-revert the document.
That is worse than no undo at all.

Two options:

- **(a) Recommended for Phase 1.** All-pages watermark is a *document-level
  command*, not a panel tool: it goes in the File menu (`viewer.html:1862`)
  alongside Print, is outside the undo stack, and asks for confirmation first.
  The panel keeps the single-page tool only. Zero risk to the undo stack.
- **(b) Deferred.** Make an undo entry a *list* of `(page_id, bytes)` so a
  multi-page action is one step. Contained in principle — three functions —
  but it is the most bug-prone code in the app. Not worth it for one feature in
  Phase 1.

Go with (a); revisit (b) if Phase 2 needs multi-page steps anyway.

### 3.6 Panel layout and naming

Layout is a non-issue: `.at-tool` is `flex:0 0 calc((100% - 20px) / 5)`
(`viewer.css:550`) so tools lay out five per row, and `#annot-tools` is already
`overflow-y:auto` (`viewer.css:539`). A new row of four fits; the panel just
scrolls a little more at the 260px `--right-w`.

Naming is the real UI work. The panel would otherwise hold **two tools called
Text**: the annotation Text (a FreeText overlay object) and — in Phase 2 — the
editor's Text (content stamped into the page). Fix the grouping *now*, while
only four tools are arriving, so Phase 2 slots in:

```
Tools
  ── Annotate ──          (existing 11: text, ellipse, rect, line, arrow,
                           darrow, cloud, highlight, sticky, image, signature)
  ── Change the page ──   (new: redact, crop, watermark, field)
```

Use the existing `role="group"` + `aria-label` pattern the rows already carry,
and add a visible section heading (`.at-title` already exists — see
`#ed-wm-props`). The distinction the heading carries is real and worth making
explicit in the tooltip text: **annotations sit on top of the page and can be
deleted individually; "change the page" tools alter the page itself.**

Redact in particular should say so. It is irreversible once saved — the backend
scrubs native text *and* burns pixels in image XObjects
(`_burn_redactions_into_images`, `routes/editor.py:3411`). Undo restores it only
while the snapshot is still in the session history.

### 3.7 i18n

All five locales are at **exact key parity — 863 keys each**. Keep it that way,
and do not hand-edit five files:

```bash
python tools/i18n_extract.py            # dry report
python tools/i18n_extract.py --apply    # inject markers + merge into en.json
python tools/i18n_translate.py          # rebuild es/fr/de/it from en.json
python tools/i18n_translate.py --check  # verify coverage
```

Many of the strings already exist under editor-namespaced keys
(`tool_redact.redact`, `tool_crop.crop`, `ed_wm_props.*`). Decide once: either
reuse those keys from the new markup, or retire them with the editor panel in
Phase 2. **Reuse them** — it keeps the translated values and avoids 5 × N new
strings for text that is already translated.

---

## 4. Work order

Each item is independently shippable and independently testable. Do them in
this order; the early ones de-risk the later ones.

**W0 — Spike: cropped-page coordinates. ✅ COMPLETE (2026-09-20).**
Deliverable: `tests/test_crop_coords.py` — 8 tests, green, and verified to
discriminate (mutating the origin logic fails 3 of them). Findings:

| case | viewer today | editor today |
|---|---|---|
| plain, `/Rotate` 0 / 90 / 180 / 270 | **correct** | correct |
| cropped, `/Rotate 0` | **wrong** — box lands off-page, nothing covered | correct |
| cropped + rotated | wrong | **wrong** (documented unsupported) |

1. The viewer's origin-free conversion is **already correct for plain pages at
   every rotation** — the case essentially every real document is in. W1 must
   not regress it, which is why the origin has to come from the shared helper
   (§3.2) rather than a direct CropBox read.
2. The editor's origin is `(CropBox.left, CropBox.top)`, **except** rotated +
   uncropped, where `_editor_display_orientation` overrides it to `(0, page_h)`.
3. Feeding viewer-frame coordinates to `apply_edits` on a cropped page put the
   redaction wholly outside the visible page: nothing covered, success reported.
4. **Annotations are already broken on cropped pages in shipped v3.1** (§3.2a),
   and the app can reach that state through its own Crop tool.
5. Cropped + rotated is broken in *both* panels (§3.2b) → refuse, don't misfire.

Scratch scripts used to establish this are not part of the repo; everything
worth keeping is in the test file.

**W1 — The coordinate contract. ✅ COMPLETE (2026-09-20).**
Shipped as a pure backend change; `src/frontend/` was not touched.

- Added `_page_crop_origin` and `_page_display_frame`; gave
  `_raw_rect_to_display`, `_display_rect_to_raw`, `_xform_pdf_rect` and
  `_xform_pdf_points` an `off_x` / `off_y` term (default 0, so an unmigrated
  site keeps its old behaviour rather than changing silently).
- Migrated six call sites; three of them also needed their `if rotate:` guard
  widened. One site (FreeText `/AP` layout, `routes/viewer.py:3001`) was
  deliberately left alone — it takes only width/height from the transform.
- **The read direction was affected too**, which W0 had not measured: all three
  paths were off by the full CropBox origin. pdfium is the trap — it reports
  CropBox *dimensions* but absolute-user-space *charboxes*.

  | path | before | after |
  |---|---|---|
  | `_get_annotations` | +50, +80 | 0, 0 |
  | `_get_links` | +50, +80 | 0, 0 |
  | `/api/viewer/page_text` | +50, +80 | 0, 0 |

- **Unplanned win: the whole matrix now works.** W0 measured cropped+rotated as
  broken in both panels and the plan expected Phase 1 to inherit that. Because
  the translation is applied *before* the rotation, all eight
  {plain, cropped} × {0, 90, 180, 270} cases are now correct for the viewer's
  annotation path — pinned by
  `test_viewer_annotations_land_correctly_across_the_whole_matrix`.
  `apply_edits` is unchanged and still wrong on cropped+rotated, so the §3.2b
  refusal is still required for the page tools.
- Four characterisation tests inverted; `tests/test_crop_coords.py` is 9 tests.
  Full suite green (166).
- CLAUDE.md gained a "Viewer page coordinates (the CropBox origin)" section.

**Moved to W2:** the inactive panel scaffolding (the "Change the page" section,
`_TOOL_IDS`, `_armTool`). Shipping dead buttons on their own has no value and no
way to be verified; they arrive with the first tool that uses them.
- Add the "Change the page" section to `#annot-tools` with the four buttons,
  inactive.
- Extend `_TOOL_IDS` (`viewer-panel.js:3759`) and the props-visibility switch in
  `_armTool` (`viewer-panel.js:3964`).
- No behaviour yet — the buttons arm and disarm and nothing else.

**W2 — Field + panel scaffolding. ✅ COMPLETE (2026-09-20).**

- Panel: an `Annotate` heading over the existing 11 tools, then a rule and
  `Change the page` with the Field tool (`.at-section` in `viewer.css`). The
  grouping is the only thing telling the user which kind of tool they are
  picking, so it went in now rather than when the panel is fuller.
- `_TOOL_IDS` gained `formfield`, plus `_PAGE_TOOL_IDS` / `_isPageTool`:
  `_annotMouseDown` hands a page tool off to its own drag **before** any draft
  is created, because a widget is not an annotation and `commitDraft` cannot
  write one.
- **The plan was wrong that this needed no backend work.** Field placement was
  the cheapest tool only in the sense that `add_field_to_page_pdf` already
  existed. Three things were still required:
  1. A **fourth** instance of the CropBox bug, and the worst yet: on a cropped
     page a field overlay rendered *off the page entirely*. Fixed by
     `_field_rect_to_display` / `_field_rect_to_raw`.
  2. Viewer-namespaced `add`/`update`/`delete_form_field` routes, because the
     editor's speak user space and the viewer speaks the display frame.
  3. `@_serialised_on_session` on all three. Removing the decorator makes
     `test_field_write_and_annotation_write_do_not_lose_each_other` fail with
     the widget silently gone — the race is real, not theoretical.
- Ported from the editor's `ffDrag`: a press that does not move places nothing.
- 9 Python tests (`tests/test_form_field_coords.py`) + 10 front-end tests
  (`tests/js/viewer_field_tool.test.mjs`). Locales back to exact parity at 867
  keys; 4 new keys translated in all five languages via `tools/i18n_translate.py`.
- Verified in the running app, including the self-inflicted path: crop page 1
  with the editor's Crop tool, return to the viewer, place a field — it lands
  under the cursor.

**Still open for W6:** the §3.4 page-relayout question. Cropping a page and
returning to the viewer looked correct in manual testing, but that was an
observation, not a measurement — W6 still owns proving it. Arm → drag → existing
`/api/editor/add_form_field` (now `@_serialised_on_session`) → `loadFormFields()`
refresh. Port select/move/resize from `_updateFFDrag` (`editor.js:3632`), keeping
its deliberate design: form-field drags are **outside** undo and only POST when
`ffDrag._moved`, so a press that moved nothing neither spends a round trip nor
turns a saved document unsaved.

**W3 — Redact. ✅ COMPLETE (2026-09-20).**

- `/api/viewer/redact`, serialised, reusing `apply_edits` through a minimal
  edits dict exactly as sketched below. `_annot_push_undo` before the rewrite —
  so a redaction is one Ctrl+Z through the existing toolbar, with no new undo
  machinery. **The §1 premise holds in practice**, which was the main thing W3
  was there to find out.
- `_page_tool_offset` and `_page_tool_refusal` are shared helpers, not
  redaction-specific: W4–W6 use them as they land.
- **The refusal is implemented and is byte-safe**: a cropped+rotated page
  returns 400 with `err.cropped_and_rotated`, leaves the page unchanged, and
  does not spend an undo slot. Verified in the app with a real cropped+rotated
  page; the message is localised through `apiErr`'s `error_key` path.
- Client: `_ffDraw*` generalised into `_pageToolDrag*` — one drag shape, a
  per-tool `previewClass` and `commit`. Redact's preview is solid black rather
  than an outline, so it looks like what it is about to do. The W2 suite was
  updated to the shared names in the same pass.
- The panel carries a permanence note while the tool is armed, because "Undo
  works until you save" is the part users need and it is not guessable.
- 11 Python + 11 front-end tests. Mutation-checked: zeroing the CropBox offset
  fails the cropped-page test, and dropping `_annot_push_undo` fails four.
- **Verified end to end in the app**: redacting the word "Introduction" removed
  it from `/api/viewer/page_text` while "Contents" elsewhere on the page
  survived, and Undo brought it back. That is the security promise, measured
  rather than assumed.

Original sketch, kept because it is what the route does:

```python
minimal_edits = {'deleted_ids': [], 'moved': [], 'text_changed': [],
                 'markup': [], 'redacts': [box],
                 'pdf_image_edits': [], 'original_elements': [],
                 'watermark': None}
new_pdf, _ = apply_edits(pdf_bytes, minimal_edits, page_w, page_h)
```

Plus `_annot_push_undo` before, `mark_dirty` after, thumbnail refresh, and
`_invalidateViewerPage` client-side. Live drag preview reuses the existing draft
overlay machinery (`_makeDraftFrom` / `_draftRender`).

**W4 — Watermark (single page). ✅ COMPLETE (2026-09-20).**

- **The plan's "same shape as W3" was wrong, and measuring first is what caught
  it.** The watermark takes no client geometry: `apply_edits` derives the page
  centre from the CropBox itself. Measured across all eight
  {plain, cropped} × {0, 90, 180, 270} combinations *before* the route was
  written, it is centred on every one — including the cropped+rotated case
  Redact must refuse. So it has **no `_page_tool_offset` and no refusal**, and
  copying Redact's shape would have added a refusal that wasn't needed.
- It is therefore a page tool with no drag: `_PAGE_TOOL_IDS` but not
  `_PAGE_TOOL_DRAG`. `_pageToolDragStart` no-ops for it, pinned by a test.
- Preview is an SVG overlay on the page block (matching every other viewer
  overlay), tracked in `_wmPreviewEl` rather than looked up by class — the code
  that creates it owns the reference, and it has to be cleared on disarm, on
  tool switch, and after stamping, where leaving it would show the mark twice.
  It follows `currentPageId` as the user scrolls, because that is also what
  Stamp applies to; letting those drift apart silently was the main hazard.
- Settings are clamped server-side (`_clean_watermark`) — they reach a content
  stream, so size, opacity, angle, layout and colour are all bounded rather
  than trusted.
- 12 Python + 14 front-end tests. Verified in the app: live preview, stamp,
  Undo, and the panel's `DRAFT` default (which the DOM stub cannot exercise).

**Two test-quality notes from this step, both worth keeping in mind:**

1. A test asserting the colour was "sanitised against injection" passed with the
   validation removed — `_hex_to_rgb` already fails safe. It was rewritten to
   pin what the validation actually buys: a bad colour degrades to the default
   grey rather than to black. It now fails when the validation is removed.
2. `_invalidateViewerPage` was missing from the harness shims, so **every**
   viewer mutation's `.then()` tail threw and everything after the call was
   silently skipped. Added as a no-op; all 17 JS suites still pass, and tests
   asserting on that tail now mean what they say.

**W5 — Watermark (all pages). ✅ COMPLETE (2026-09-20).**

Shipped as §3.5(a) said — File menu, confirm, outside undo, serialised — plus
two things the plan had not anticipated.

- **It found a shipped bug.** `layout_handleWatermarkAllPages` looped over the
  pages calling `/api/watermark/apply_all` once per page, while that route loops
  over every page *itself* and skips `exclude_page_id` — a key the client never
  sent. So nothing was excluded and every page was stamped once per call: an
  N-page document got **N stacked watermarks on every page**. Measured on 3
  pages: mean ink darkness 187 vs 225 for a single application; the 24-page
  manual would come out nearly opaque. The editor's client was fixed to make one
  call, and `test_the_editor_route_applies_once_and_honours_exclude_page_id`
  pins the contract it now depends on.
- **It clears the undo history**, which §3.5 had not called for. Being outside
  the stack is not enough: an earlier per-page entry left in place would let one
  Ctrl+Z restore that page to a pre-watermark snapshot, silently removing the
  mark from one page while the rest kept it. The confirmation says so.
- The route lives in `routes/viewer.py` and takes page dimensions through
  `_page_display_frame` rather than `pdfium` directly. That matters: the editor's
  sibling route reaches the module-level `pdfium` that `_heavy_init()` fills in
  lazily, and outside the `/api/editor/*` `before_request` guard it can be
  `None` — in which case the per-page `except` swallows the failure and the
  route answers `200 {'updated': []}` having done nothing at all. A File-menu
  command is reachable without ever opening the editor, so it must not depend on
  that.
- 10 Python + 9 front-end tests. Mutation-checked: reintroducing the stacking
  fails the once-per-page test, and leaving the history in place fails two.
- Verified in the app on the 24-page manual: one mark per page (page ink +8.5k
  px, mean darkness unchanged at 184.9 — 24 stacked would have been far darker),
  Undo greyed out afterwards.

**Correction to something I claimed mid-task:** I first reported this route as
outright broken in v3.1 because a test client got `200 {'updated': []}`. That
was my harness, not the app — heavy deps load in the background at startup, so
the real app works. The narrower fragility above is what is actually true.

**W6 — Crop. ✅ COMPLETE (2026-09-21).**

- `/api/viewer/crop`, serialised, display-frame coordinates, `_page_tool_refusal`
  (Crop takes client coordinates, so unlike the watermark it does need it), and
  a `clear: true` mode that restores the CropBox to the MediaBox.
- **§3.4's open question is answered: the block already relayouts correctly.**
  It has no explicit height — `buildPageColumn` sizes a placeholder and
  `_buildPageBlockDOM` then replaces the block's contents wholesale — so it
  reflows to whatever the new canvas measures. Verified against the real server:
  cropping 612×792 → 440×440 took the block's own rect from 306×396 to 220×220,
  and undo took it back, with no stale-height gap at any point.
- **A fifth instance of the CropBox conflation, in `_page_dimensions`.** It
  measured the MediaBox while pdfium renders the CropBox, so a cropped page was
  listed at its uncropped size. Its own docstring says "as displayed" and
  `test_rotation.py` says the two "have to agree", so this was a contract
  violation rather than a design choice. Fixed; the existing rotation tests use
  uncropped pages (CropBox defaults to MediaBox) and were unaffected.
- **My own test caught a real gap in this work:** undo restored a page's bytes
  but nothing re-derived its size, so after undoing a crop the record still
  claimed the cropped dimensions. Fixed in `_annot_history_payload`, which is
  where both undo and redo answer from, with `_applyPageSize` as the client half.
- 12 Python + 12 front-end tests. Mutation-checked: dropping the undo size
  handling fails the undo test.

**W7 — De-duplicate the Editor panel. ✅ COMPLETE (2026-09-21).**

Reversed from the earlier "leave both" decision after review: the duplication
was the first thing that stood out in the running app, and it reads as an
unfinished job rather than a deliberate transition.

- The Editor panel is now the text-editing set only: **Pointer, Hand, Text,
  Text Box**. Redact / Crop / Field / Watermark and their option panels
  (`#ed-crop-props`, `#ed-ff-section`, `#ed-wm-props`) are gone from it.
- **Field parity had to be built first.** The viewer could only PLACE a field;
  the editor could also select, move, resize and delete one. Removing the
  editor's tool without that would have been a regression, not a
  de-duplication. The viewer's form overlay now switches to an edit mode while
  the Field tool is armed — widgets go `pointer-events:none` and a hit-area per
  field sits on top, because otherwise a click lands in the input and types.
  Move and resize were verified exact against the running server.
- Redact, Crop and Watermark were already at parity or better: the editor's are
  staged-until-Apply, the viewer's are immediate with real undo.
- 7 new front-end tests (17 in `viewer_field_tool.test.mjs`).

> **Incident during this step.** A scripted edit using `str.index` on
> `<!-- Tool selector (row 2) -->` matched the VIEWER's copy of that comment
> instead of the editor's — the search string was indent-insensitive and the
> viewer's occurrence came first — and deleted ~1,300 lines between them,
> taking `panel-layout` and `panel-editor` with it. There is no git here, so
> recovery came from `build/exe.win-amd64-3.14/src/frontend/viewer.html`, which
> was byte-identical to the pre-session file; the Phase 1 markup was then
> re-applied and the de-duplication redone with line-anchored edits that assert
> what they are about to delete and what they must not touch.
>
> Two lessons worth keeping: **HTML edits in this file need unique anchors**
> (several comments are duplicated between panels now that the viewer has its
> own tool rows), and **the `build/` tree is the only pre-session copy of the
> frontend** in a project with no version control.

### P2a — Page text / Text Box. ✅ COMPLETE (2026-09-21)

- `/api/viewer/add_text`, serialised, `_page_tool_refusal`, `_page_tool_offset` —
  the same shape as Redact, feeding `apply_edits`'s `markup` key.
- **Named apart from the annotation Text tool**, which was flagged as a hazard in
  the very first assessment: two tools called "Text" in a 260px panel. They are
  *Text* (a note on top, selectable and deletable) and *Page text* (written into
  the page, indistinguishable from the document's own words afterwards). The
  panel note spells that out, because it is not guessable and not reversible.
- Text places on a CLICK, Text Box on a DRAG. Clicking away commits and only
  Escape discards — the rule every text entry in this app follows — and the
  style controls are excluded from "away", so adjusting the size mid-edit is
  part of the edit rather than the end of it.
- **The client wraps the text box**, because only the client can measure the
  font it is previewing with. The viewer must NOT borrow the editor's
  `wrapTextLines`: it uses the editor's live canvas `ctx`, which does not exist
  until the editor panel has been opened. The viewer has its own measure-only
  canvas. The route falls back to the raw text if a client sends no wrap, so a
  bad client places something rather than silently nothing.
- 14 Python + 13 front-end tests. Mutation-checked: dropping the CropBox offset
  fails the cropped-page test; dropping the style-panel guard fails the
  mid-edit test.
- Verified in the app: three placements land as real, extractable page text
  (checked through `/api/viewer/page_text`, not just pixels), and the box wraps
  onto two lines.
- The editor's `Text` / `Text Box` and their `ml-*` style controls were removed
  in the same step, so no duplication was re-introduced. **Editor panel is now
  Pointer + Hand.**

> Two notes for whoever does the Pointer next. `placeMarkup` and the multiline
> opener in `editor.js` are now unreachable dead code (nothing calls
> `setMode('markup'|'multiline')`), and they still reference the deleted `ml-*`
> controls — they go with `editor.js`. And the browser pane's synthetic key
> events arrive with `key: ""`, so Enter/Ctrl+Enter cannot be exercised by
> driving the real app; they are covered in `tests/js/viewer_page_text.test.mjs`
> instead.

### P2b — The Pointer (editing the document's own text). ✅ COMPLETE (2026-09-21)

The hardest piece, and the one the original assessment said would need the
whole editor brought across. It did not, because of one decision:

- **Edits commit immediately — one request, one snapshot, one Ctrl+Z — instead
  of staging behind an Apply button.** That avoids running two undo systems side
  by side, which was the single biggest risk identified when this was planned.
  The price is that rewriting a page invalidates every element id, so each
  successful edit reloads the element model. On a native-text page the reload
  costs ~0.2s; on a scan it costs an OCR pass, which is why only the page in
  view is ever loaded.
- **The gate is the panel plus the tool**, exactly as originally asked for: no
  separate editing mode, but a bare click never starts rewriting the document.
- Two new routes: `/api/viewer/page_elements` (read-only, unserialised, no page
  image) and `/api/viewer/edit_text` (serialised; text / style / move / delete).
  The server re-extracts to build `original_elements` rather than trusting
  client geometry — those bboxes are what gets painted over to hide the old
  glyphs, so a wrong one leaves the original showing through its replacement.

**Two defects found and fixed while building it:**

1. **Undo made text permanently uneditable.** Editing COVERS the original
   glyphs rather than deleting them, and `covered_text_bboxes` is what stops
   them being offered again — but it lives on the page record, and undo only
   restored the bytes. So undoing an edit left the restored text visible and
   impossible to ever edit again. Undo entries now carry page metadata
   (`_PAGE_EDIT_META`); readers must index rather than unpack, and one existing
   test did unpack, which is how it was caught.
2. **`className` is not a string on an SVG element.** Walking a page block's
   children with `(c.className||'').indexOf(...)` throws in a browser, because
   the block holds SVG overlays. The DOM stub models every className as a
   string, so all 18 front-end tests passed while the real app threw on the
   first click. Only driving the actual app found it — worth remembering for
   anything that walks block children.

- 14 Python + 18 front-end tests. Mutation-checked: dropping the metadata
  restore fails two; dropping the CropBox offset fails the cropped-page test.
- Verified in the app end to end: the gate (armed + panel closed does nothing),
  select, edit, reload, and undo restoring both the words and their editability.

**Out of scope, deliberately:** the editor's image and vector-graphic editing,
and Force OCR. Those still need the editor's element model in a way the text
tools do not, and no one has asked for them in the viewer.

---

## 5. Tests

New front-end suites in `tests/js/` (they load the real JS into a Node `vm` over
the DOM stub; `tests/test_frontend.py` discovers them, so no wiring):

- `viewer_page_tools_coords.test.mjs` — `_vToPdfPt` against cropped, rotated and
  plain pages. The regression guard for §3.2.
- `viewer_page_tools_queue.test.mjs` — every new tool's mutation goes through
  `_queueAnnotOp`; arm/disarm doesn't leak listeners. Guards §3.1.
- `viewer_ff_drag.test.mjs` — a press that doesn't move sends nothing; guards the
  ported `ffDrag._moved` behaviour.

New / extended Python tests:

- extend `tests/test_redaction.py` to cover the new route, including that the
  image-burn pass still runs.
- `tests/test_page_tools_locking.py` — concurrent redact + annotation on one page
  leaves both, and the undo stack holds two distinct snapshots. This is the
  direct analogue of `test_annot_undo_ordering.py` and the single most valuable
  new test.
- extend `tests/test_session_and_forms.py` for field-add via the viewer path.
- `tests/test_crop_dimensions.py` — after a crop, the page record's `width_pts` /
  `height_pts` match the new CropBox (§3.4).

Full run: `pytest` from the project root. Node is dev-only; the `.mjs` suites
skip without it on PATH.

---

## 6. Explicitly out of scope for Phase 1

- Pointer / text editing in the viewer, and with it Force OCR, Apply, Discard,
  Reset, and the editor's client-side `undoStack`.
- Removing the Editor button (`#v-btn-edit`, `viewer.html:115`) or the
  `panel-editor` markup.
- The run-once "What's New" popup. It is Phase 2's, because Phase 1 removes
  nothing the user can see — the Editor button still works and still does
  everything it did. Shipping the popup now would announce a change that hasn't
  happened.
- Any change to the crash-recovery draft model, which is keyed to editor pages
  (`_clearDraft`, `scheduleDraftSave`) and only becomes a question when the
  editor session boundary goes away.

---

## 7. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| ~~Cropped-page coordinate drift~~ | ~~High~~ → **characterised** | W0 done: measured, understood, pinned by `tests/test_crop_coords.py`. Residual risk is in W1 *applying* the fix without regressing plain pages — which the same file guards |
| Origin fix regresses rotated pages | High — would break the common case to fix the rare one | Reuse `editor_open`'s origin logic, never a raw CropBox read (§3.2); `test_plain_pages_agree_at_every_rotation` |
| Redact silently misfires on unsupported geometry | High — **security**, not cosmetic | Refuse on cropped+rotated (§3.2b) |
| Read-modify-write race with annotations | High — silent data loss, precedent in project history | `@_serialised_on_session` on every route; `_queueAnnotOp` on every call |
| Crop relayout leaves a stale block height | Medium — visible glitch | Spike in W6 before committing to the approach |
| Watermark-all trims the undo stack to nonsense | Medium | §3.5(a): outside undo, in the File menu, with a confirm |
| Redact feels reversible because Undo is right there | Medium — user-facing, irreversible | Section heading + tooltip wording; toast on commit |
| Panel gets crowded at 260px | Low | Already scrollable; section headings help |

---

## 8. Definition of done for Phase 1

- [ ] The four tools work from the viewer's Tools panel on a plain page.
- [ ] They work on a **cropped** page and a **rotated** page; on a
      cropped **and** rotated page they refuse with a clear message rather than
      writing to the wrong place (§3.2b).
- [ ] The four `*_today` tests in `tests/test_crop_coords.py` have been
      **inverted**, not loosened — cropped-page annotations now land where the
      user put them (§3.2a).
- [ ] Redact, Crop and single-page Watermark are each one Ctrl+Z.
- [ ] All-pages Watermark is in the File menu, confirms, and says it can't be undone.
- [ ] Concurrent annotation + page-tool mutation on one page loses neither.
- [ ] Every mutation sets the document dirty; no save path changed.
- [ ] `pytest` green, including the new suites.
- [ ] All five locales still at exact key parity.
- [ ] The Editor button still opens the editor and the Pointer still works there.
- [ ] CLAUDE.md updated: a "Viewer page tools" section stating the coordinate
      contract (§3.2) and the locking rule (§3.1), since both are the kind of
      invariant that file exists to record.

---

## W8 — deep stale-code scan (post-merge)

Ran after the merge was complete and green. The rule used throughout: **delete
only what is provably unreachable AND whose reachability was removed by this
work**; report, don't delete, anything substantial that predates it.

### Removed

| What | Where | Size |
|---|---|---|
| `#ed-*`, `#editor-canvas`, `#ml-*`, `#sl-*`, `.mk-*`, `.ed-stack`, `.ed-sep`, `#ocr-notice*` rules | `css/viewer.css` | 54 rules |
| `#ed-toolbar` selector in the shared scrollbar rule; a comment naming `#ed-tools` | `css/viewer.css` | 2 residues |
| Debug Panel block (`#debug-bar/-panel/-toolbar/-log`, `.dbg-*`) — no markup anywhere creates these; predates the merge | `css/viewer.css` | 1,644 chars |
| `pwDraftSave/Load/Delete/Key`, `layout_handleWatermarkAllPages`, the empty draft loop | `js/shared.js`, `js/layout.js` | — |
| Editor-only locale keys (`panel_editor.*`, `ed_*`, `ml_*`, `sl_*`, `tool_select/grab/markup/multiline`, `ctx_*`, `ff_label/ff_name/ff_options`, `viewer.header.edit*`, `toast.ocr_*`, `confirm.close_editor_*`) | `locales/*.json` | 97 keys |
| `p_font.*` — 25 font NAMES, zero references anywhere, and not translatable content in the first place | `locales/*.json` | 25 keys |

Locales went **913 → 769** keys, still at exact 5-way parity.
`pwDraftClear()` now runs at boot, so a pre-upgrade install's leftover editor
drafts are disposed of rather than orphaned in storage.

### Reported, not removed — needs a decision

1. **Ten `/api/editor/*` routes have no client caller** (`open`, `pages`,
   `apply`, `force_ocr`, `ocr_status`, `page_diag`, `dump_coords`, and the three
   `*_form_field`), plus `/api/watermark/apply_all`. But **`routes/editor.py`
   itself is load-bearing**: the viewer imports `apply_edits`,
   `_extract_text_and_graphics`, `_editor_display_orientation`,
   `_filter_covered_text_items` and `GHOST_OVERLAP_THRESH` from it at eight
   sites. The module stays either way; only the route decorators are dead.
   Deleting them is safe but is a separate, testable change.
2. **`/api/watermark/apply_all` is now unreachable from the UI.** The W5 client
   fix lived in `layout_handleWatermarkAllPages`, which this scan deleted as
   dead. `tests/test_viewer_watermark_all.py` still pins the route's behaviour;
   its docstring now says so honestly, and says to delete the test with the
   route.
3. **Nine JS functions defined and never called** — `switchToViewer`,
   `themeAlpha` (shared.js); `_splitIntoColumns`, `_syncSelectionOverlay`,
   `_viewerPanelActive`, `clearAnnotations`, `clearSelectionOverlay`,
   `drawAnnotations`, `goReturn` (viewer-panel.js). All predate this work; four
   are no-op SPA shims. `switchToViewer` is substantive and looks like it was
   meant to be wired to the layout→viewer return path that
   `switchToViewerFromLayout` now serves — worth a look before deleting.
4. **~48 unreferenced locale keys remain** (`toast.*` ~30, `confirm.*` ~13, a
   few singles). All predate the merge. Left alone: they are cheap, and the
   merge is not the right change to audit them in.

### Verified after every step
255 Python tests, all `tests/js/*.test.mjs` suites, CSS brace balance 0,
locales 5 x 769 exact parity.


---

## W9 — two Pointer bugs found in user testing (scanned invoice)

Reported: "I selected the Edit text tool, clicked on some text, it went yellow,
but I cannot edit it."  Reproduced in the real app on `cant edit.pdf`, a scanned
invoice, and both bugs were real.

### 1. Double-click could never fire

`_etSelect` re-rendered the whole overlay, and it runs on MOUSEDOWN.  So the
node the pointer was over was destroyed between click one and click two, and a
browser only dispatches `dblclick` when both clicks land on the same element.
Selection worked (yellow, panel label, Delete enabled); opening the editor was
impossible.  Enter still worked, which is why the panel hint did not look wrong.

Fixed by toggling the `sel` class on the existing children instead.  Verified in
the browser both ways: with the shipped code `{sel:'ocr_1', entryOpen:false}`;
with the fix `{entryOpen:true, entryValue:'Invoice'}`.

**Why no test caught it.**  There was none — the 18 JS tests covered the commit
paths and the gate, not the gesture that reaches them.  A stub cannot model a
browser's click bookkeeping, so the new test pins the invariant that bookkeeping
depends on: node identity survives a selection.  Mutation-checked.

### 2. The first edit on a scan killed the rest of the page

Found while verifying the fix end to end.  After changing one word the element
model reloaded with **1 item instead of 178**.  `_extract_text_and_graphics`
decides whether to OCR from a char count; `apply_edits` writes real text
operators; so one edit and the page is no longer a scan, the native path runs,
and the other 177 OCR'd words are gone.

The editor solved this years ago with `ocr_source_bytes` — the viewer simply
never set it.  `_preserve_ocr_source` now stashes the pristine scan before the
first rewrite, on every route that can inject text, and `_page_elements` passes
it through.  Measured after the fix: 178 items still, "Receipt" present as a
native run, the covered OCR original correctly filtered, the *other* "Invoice"
(the heading) untouched, and Undo restoring all 178 with both originals back.

Guarded by `tests/test_viewer_scanned_edit.py` (6) and the new JS case.
Both mutants killed.  261 Python tests green.


---

## W10 — replacement text overlapping the next word

The last thing left from user testing: "Invoice No: 106688" edited to
"Statement" rendered as "StatementNo: 106688".  A PDF does not reflow — the
injection goes down at the original run's x and nothing moves the next word
along — so the only honest fix is to measure and fit.

### What was rejected

* **Pushing the neighbours right.**  Needs a layout model the extraction does
  not produce, changes the whole line, and the last word then overflows instead.
* **Shrinking the font size first.**  A smaller word sits obviously wrong beside
  unchanged neighbours; horizontal scaling keeps the baseline and cap height, so
  a few percent is invisible.  Size is the fallback, not the first move.
* **Wrapping to a second line.**  One run has one baseline; a second line lands
  on the line below.
* **Truncating.**  Silently discards what the user typed.

### What it does

Condense with `Tz` to fit the gap; shrink the size only when condensing hits its
floor; stop at both floors and TELL the user when even that was not enough.

### Four corrections found by measuring, not reasoning

1. **Pillow measures the wrong thing.**  `getlength` returns hinted,
   pixel-quantised advances: 'Invoice' came out 39.0pt at 12pt and 75.0pt at
   24pt — not even linear, a 4% error against an unhinted renderer.  Switched to
   the face's own `charWidths`, which is literally what becomes the embedded
   `/Widths`.
2. **A neighbour can start inside the previous run's box.**  Adjacent runs,
   kerned pairs and slack OCR boxes all do it.  Taken at face value, replacing a
   run with a SHORTER string condensed it to 80%.  The original's own footprint
   is now always available.
3. **Sub-point overruns are noise.**  Runs on a justified line sit a point or
   two apart and their boxes are rounded; without a tolerance every other edit
   was condensed for an overlap nobody could see.
4. **Clearing the ink is not enough — the word space has to survive.**  The
   first working version produced "StatementNo:" with 0.37pt of clearance:
   no overlap, same complaint.  As much of the original's trailing gap as a
   space in that font occupies is now reserved.  Measured on the real page:
   "Invoice" left 4.0pt, "Statement" now leaves 2.89pt, and the line reads
   "Statement No: 106688".

Guarded by `tests/test_text_fit.py` (18) and six end-to-end cases in
`tests/test_viewer_edit_text.py`.  Six mutants killed, including one aimed at
each of the four corrections.  284 Python tests green.
