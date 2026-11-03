"""
viewer.py — read-only PDF viewer routes for Patchwork PDF.

Registered into the Flask app via register_viewer_routes() called from entry.py.
Shares the same sessions dict as the main app so pages loaded on the entry page
are immediately visible in the viewer.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT — NATIVE LIBRARY THREAD SAFETY (_NATIVE_LOCK)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Flask's development server (and production threaded WSGI servers) process
requests on concurrent threads.  When a multi-page PDF is opened, the viewer
frontend fires several /api/viewer/render requests simultaneously — one per
page — and Flask handles them on separate threads.

Two native C libraries are used here:

  • pypdfium2 / pdfium  — PDF rendering (render, text extraction, search)
  • pikepdf / QPDF      — PDF stream normalisation (strips image filter chains
                          that crash pdfium; see _normalize_page_for_pdfium)

The problem
-----------
When pikepdf is installed, loading it for the first time calls pikepdf's
delvewheel initialisation patch, which calls os.add_dll_directory() and loads
qpdf30.dll into the process.  Once qpdf30.dll is resident it shares the
Windows CRT heap (via msvcp140.dll) with pdfium's native binary.

Concurrent calls to pdfium from multiple render threads then cause Windows CRT
heap corruption: pdfium's FPDF_CloseDocument (doc.close()) writes to heap
metadata that was already corrupted by an earlier concurrent render, and the
CRT heap checker raises STATUS_BREAKPOINT / exit code 0x80000003 (-2147483645),
killing the entire Python process.

Note: before pikepdf was added to the project this worked fine because qpdf was
never loaded and pdfium's concurrent use of the heap was stable in isolation.
Adding qpdf changes the heap layout in a way that makes concurrent pdfium
unsafe, even though the two libraries never share data directly.

The fix
-------
_NATIVE_LOCK is defined in ocr_utils.py and imported here (and by editor.py)
so that all pdfium/pikepdf calls across the whole app share the same lock.
It must be acquired by EVERY function that calls into either library:

  _render_page()        — pdfium: PdfDocument → render → to_pil → close
  _get_page_chars()     — pdfium: PdfDocument → textpage → charbox → close
  _is_scanned_page()    — pdfium: PdfDocument → textpage → close
  _search_page()        — pdfium: PdfDocument → textpage → close
  _normalize_page_for_pdfium()  — pikepdf: open → scan/modify → save → close
  pdf_core.render_page_thumbnail() / _preload_file()  — pdfium thumbnails
      (both used to skip it; the loads they serve run on Flask threads)

The lock is an RLock, so a helper that takes it may be called by code already
holding it.

Only pure-Python work (PIL image encoding, base64, Python dicts) runs outside
the lock.  All C-level operations are serialised.

Rules for future maintenance
-----------------------------
1. If you add any new function that calls `import pypdfium2` or `import pikepdf`,
   wrap the entire native block with `with _NATIVE_LOCK:`.

2. PIL operations (bitmap.to_pil(), img.save()) may run outside the lock —
   to_pil() copies pixel data into a Python-owned PIL buffer and has no
   further dependency on pdfium memory.

3. Do NOT split acquire/release across try/finally manually; always use the
   `with _NATIVE_LOCK:` context manager so the lock is always released even
   if an exception is raised inside the native call.

4. The lock only serialises OUR threads.  Native start-up work we don't own —
   the .NET runtime and WinForms loading while pywebview creates the window —
   races pdfium just the same, so nothing touches pdfium off the main thread
   until the window is up (entry.main pre-loads a launch document before it).

5. If pikepdf is ever removed from the project, test whether the lock can be
   relaxed.  Without qpdf30.dll in the process, concurrent pdfium use is
   likely safe again — but measure before removing the lock.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import sys
import io
import base64
import re as _re
import uuid
import logging

import pypdf as _pypdf
from flask import request, jsonify, Response

from pdf import ocr_utils as _ocr_utils
from pdf.ocr_utils import _NATIVE_LOCK
from pdf import pdf_core as _pdf_core
from pdf import form_utils as _form_utils
from pdf.pdf_core import set_progress, clear_progress
from bootstrap import recovery as _recovery

logger = logging.getLogger(__name__)


def _t(msg):
    logger.debug(msg)

# ── Native library lock (see module docstring for full explanation) ───────────
# _NATIVE_LOCK is imported from ocr_utils so editor.py shares the same lock
# object.  Serialises ALL pdfium and pikepdf/QPDF calls across the whole app.
_PIKEPDF_LOCK = _NATIVE_LOCK  # alias used inside _normalize_page_for_pdfium
# ─────────────────────────────────────────────────────────────────────────────

# Render scale for the viewer (points → pixels).  2.0 (≈144 DPI) gives crisp
# display at fit/100% without blowing up memory.  This is the *base* (logical)
# scale: overlay coordinate math on the client is calibrated to it.  When the
# user zooms in, the client requests a fresh, higher-scale render of the focused
# page (see _render_page's `scale` arg and the /api/viewer/render `scale` param)
# so text stays sharp instead of being upscaled from this base raster.
VIEWER_SCALE = 2.0

# Cap the rendered output at this many pixels (w × h).  Pages larger than this
# limit are rendered at a proportionally reduced scale so pdfium never attempts
# a very large bitmap allocation.  6 MP keeps A2/A3/letter sharp at the base
# scale; larger engineering drawings render at a slightly lower base scale.
_MAX_RENDER_PIXELS = 6_000_000

# Larger budget for zoom-in re-renders of a *single* focused page.  Only one
# page is held at high resolution at a time, so a bigger transient allocation is
# acceptable.  ~30 MP lets a D-size (22×34") sheet reach ≈200 DPI (vs ≈89 DPI
# under the base cap) and keeps letter/A-size pages crisp to the top zoom level.
# Tunable: raise for sharper large-format zoom at the cost of memory/render time.
_MAX_RENDER_PIXELS_HI = 30_000_000

# Never ask pdfium for a scale beyond this, regardless of zoom — an upper guard
# on render time and bitmap size for the focused page.
_MAX_RENDER_SCALE = 8.0

# Pages with fewer than this many non-whitespace native characters are treated
# as scanned (image-only) and will be processed with OCR for search/selection.
_SCANNED_CHAR_THRESHOLD = 5


def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # dev: this file is <root>/src/backend/routes/viewer.py — climb to <root>.
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))


def _pdf_bytes_from_session(session, page_id):
    for p in session.get('pages', []):
        if p['id'] == page_id:
            return p['pdf_bytes'], p
    return None, None


# ── Annotation undo / redo history ───────────────────────────────────────────
# Annotations are baked into each page's PDF bytes server-side as they are drawn,
# so undo/redo works by snapshotting a page's bytes *before* each mutation onto a
# per-session stack and restoring them on undo.  The stack is global across the
# document (one entry = one page's pre-action bytes) so Ctrl+Z reverses the last
# annotation action regardless of which page it was on.  Stored on the session as
# 'annot_undo' / 'annot_redo'.  Bytes are immutable, and page_rec['pdf_bytes'] is
# *reassigned* (not mutated) on each change, so storing the reference is a true
# snapshot — no copy needed.
_ANNOT_HISTORY_CAP = 30
# Also bound the history by TOTAL bytes: each undo entry holds a full snapshot of
# a page's PDF bytes, so 30 entries of large scanned pages could reach hundreds of
# MB per session.  Drop the oldest entries once the stack exceeds this budget
# (always keeping at least the most recent, so one undo is always possible).
_ANNOT_HISTORY_BYTES = 64 * 1024 * 1024


# Page-record fields that DESCRIBE a particular set of page bytes rather than
# the page in general.  They have to travel with a snapshot: restoring the bytes
# without them leaves the record describing the page that was just replaced.
#
# `covered_text_bboxes` is the one that bites.  Editing the page's own text
# COVERS the original glyphs rather than deleting them, and this list is what
# stops them being offered for editing again.  Undo puts the uncovered original
# back — so if the list did not come back with it, the restored text would stay
# invisible to the Pointer and could never be edited a second time.
_PAGE_EDIT_META = ('covered_text_bboxes', 'merge_regions', 'excluded_xobj_names')


def _snapshot_meta(rec):
    import copy as _copy
    return {k: _copy.deepcopy(rec[k]) for k in _PAGE_EDIT_META if k in rec}


def _restore_meta(rec, meta):
    for k in _PAGE_EDIT_META:
        if meta and k in meta:
            rec[k] = meta[k]
        else:
            rec.pop(k, None)


def _annot_trim_undo(stack):
    """Trim an undo/redo stack to the count cap and the byte budget, dropping the
    OLDEST entries first.  Always leaves at least the newest entry intact."""
    if len(stack) > _ANNOT_HISTORY_CAP:
        del stack[0:len(stack) - _ANNOT_HISTORY_CAP]
    # Entries are (page_id, bytes) or (page_id, bytes, meta) — index rather than
    # unpack, so an entry pushed by either shape is handled.
    total = sum(len(e[1]) for e in stack)
    while len(stack) > 1 and total > _ANNOT_HISTORY_BYTES:
        e = stack.pop(0)
        total -= len(e[1])


def _annot_push_undo(session, page_id):
    """Snapshot a page's current bytes before a mutating op; clears redo.

    Held under the per-session lock so concurrent annotation mutations can't
    interleave and corrupt the shared undo/redo stacks.  Bounded by both a count
    cap and a byte budget (see _annot_trim_undo) so a document of large scanned
    pages can't grow the history to hundreds of MB."""
    with _pdf_core.get_session_lock(session):
        cur, _rec = _pdf_bytes_from_session(session, page_id)
        if cur is None:
            return
        stack = session.setdefault('annot_undo', [])
        stack.append((page_id, cur, _snapshot_meta(_rec or {})))
        _annot_trim_undo(stack)
        session['annot_redo'] = []   # a new action invalidates the redo history


def _annot_counts(session):
    return len(session.get('annot_undo', [])), len(session.get('annot_redo', []))


def _annot_apply(session, redo):
    """Pop a snapshot from the undo (or redo) stack, push the page's current bytes
    onto the opposite stack, and restore the snapshot.  Skips no-op snapshots that
    equal the current bytes (e.g. from a lift-edit that was cancelled unchanged).
    Returns the restored page_id, or None if there was nothing to apply."""
    with _pdf_core.get_session_lock(session):
        src = 'annot_redo' if redo else 'annot_undo'
        dst = 'annot_undo' if redo else 'annot_redo'
        stack = session.get(src, [])
        while stack:
            entry = stack.pop()
            page_id, snap = entry[0], entry[1]
            meta = entry[2] if len(entry) > 2 else None
            cur, rec = _pdf_bytes_from_session(session, page_id)
            if rec is None:
                continue                      # page no longer exists — skip
            if snap == cur:
                continue                      # no-op snapshot — discard and continue
            dst_stack = session.setdefault(dst, [])
            dst_stack.append((page_id, cur, _snapshot_meta(rec)))
            _annot_trim_undo(dst_stack)
            rec['pdf_bytes'] = snap
            _restore_meta(rec, meta)
            thumb = _render_thumb_b64(snap)
            if thumb:
                rec['thumbnail'] = thumb
            _recovery.mark_dirty(session, page_id)
            return page_id
        return None


def _refresh_page_size(page_rec, pdf_bytes):
    """Rewrite a page record's width_pts / height_pts from its current bytes.

    Every page-dict site has to carry the page's *displayed* size (CLAUDE.md,
    "Viewer request budget"), and the Crop tool changes it.  Anything that
    replaces a page's bytes has to re-derive this or the record keeps describing
    the page that used to be there — including UNDO, which restores a snapshot
    whose size may differ from the current one.

    Returns (width_pts, height_pts).
    """
    try:
        w, h = _pdf_core._page_dimensions(
            _pypdf.PdfReader(io.BytesIO(pdf_bytes)), 0)
        page_rec['width_pts'], page_rec['height_pts'] = w, h
        return w, h
    except Exception:
        logger.warning('[_refresh_page_size] could not measure page')
        return page_rec.get('width_pts'), page_rec.get('height_pts')


def _annot_history_payload(session, page_id):
    """Build the JSON body returned by the undo/redo endpoints."""
    u, r = _annot_counts(session)
    if page_id is None:
        return {'page_id': None, 'undo_count': u, 'redo_count': r}
    cur, rec = _pdf_bytes_from_session(session, page_id)
    # A restored snapshot can be a DIFFERENT SIZE — undoing a crop is exactly
    # that — so re-derive the recorded size here rather than leaving the record
    # describing the page the undo just replaced.
    w = h = None
    if rec is not None and cur:
        w, h = _refresh_page_size(rec, cur)
    return {
        'page_id':     page_id,
        'annotations': _get_annotations(cur),
        'editable':    _get_editable_annotations(cur),
        'thumbnail':   rec.get('thumbnail') if rec else None,
        'width_pts':   w,
        'height_pts':  h,
        'undo_count':  u,
        'redo_count':  r,
    }


# Device spaces we can re-label an Indexed palette into: the palette entries
# already hold device values, so mapped pixels tagged with these render the same
# colours the Indexed image did.
_NCOMP_TO_CS = {1: '/DeviceGray', 3: '/DeviceRGB', 4: '/DeviceCMYK'}

# Base colorspace names that carry device values, and how many components each
# palette entry occupies.
_BASE_CS_NCOMP = {
    '/DeviceGray': 1, '/CalGray': 1, '/G': 1,
    '/DeviceRGB':  3, '/CalRGB':  3, '/RGB': 3,
    '/DeviceCMYK': 4, '/CMYK':    4,
}


def _indexed_base_ncomp(base_cs):
    """Components per palette entry for an Indexed colorspace's base.

    Returns None when the base isn't something we can faithfully re-label —
    /Lab, /Separation and /DeviceN store values that only mean anything after a
    tint transform or conversion we don't perform, so those images are left
    alone rather than rewritten into wrong colours.

    Getting this wrong is not a subtle matter of shade: an ICCBased-CMYK palette
    (N 4) read as one component per entry slices the palette apart mid-entry and
    renders the image near-black.
    """
    import pikepdf
    if isinstance(base_cs, pikepdf.Name):
        return _BASE_CS_NCOMP.get(str(base_cs))
    if isinstance(base_cs, pikepdf.Array) and len(base_cs) > 0:
        family = str(base_cs[0])
        if family == '/ICCBased' and len(base_cs) > 1:
            try:
                n = int(base_cs[1].stream_dict.get('/N', 0))
            except Exception:
                return None
            return n if n in _NCOMP_TO_CS else None
        # /CalRGB and /CalGray are arrays too: [/CalRGB <<dict>>]
        return _BASE_CS_NCOMP.get(family)
    return None


def _normalize_page_for_pdfium(pdf_bytes):
    """Normalize PDF image streams that crash pdfium on Windows rendering.

    Two distinct bugs are addressed:

    Strategy A — [/FlateDecode /DCTDecode] dual-filter JPEG images:
      The outer zlib wrapper is decoded with Python's zlib and the stream is
      re-stored with only /DCTDecode.  pdfium's internal buffer management
      makes incorrect size assumptions about the intermediate inflate output
      at full render scale, causing 0xC0000374 heap corruption.

    Strategy B — Indexed colorspace images, or [/FlateDecode /CCITTFaxDecode]:
      These are fully decoded through QPDF (via pikepdf read_bytes) and
      re-encoded as a plain FlateDecode-compressed DeviceGray / DeviceRGB
      image.  Pdfium has a null-pointer dereference (access violation at
      0x2C) when rendering 1-bpc Indexed/DeviceRGB images, regardless of
      the filter chain.

    Returns the original bytes unchanged if no fix was needed or on error.
    """
    _t('normalize: entry')
    try:
        _t('normalize: importing pikepdf')
        import pikepdf, zlib
        import numpy as np
        _t('normalize: pikepdf imported OK')

        # Serialise all QPDF operations — QPDF global init is not thread-safe;
        # concurrent first-open calls corrupt the CRT heap causing pdfium to
        # crash in doc.close() with STATUS_BREAKPOINT.
        _t('normalize: acquiring PIKEPDF_LOCK')
        with _PIKEPDF_LOCK:
            _t('normalize: lock acquired, opening PDF with pikepdf')
            with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
                _t(f'normalize: open OK, {len(pdf.pages)} page(s)')
                changed = False
                for page in pdf.pages:
                    xobjs = page.resources.get('/XObject')
                    if xobjs is None:
                        continue
                    for key in list(xobjs.keys()):
                        xobj = xobjs[key]
                        if not isinstance(xobj, pikepdf.Stream):
                            continue
                        if xobj.stream_dict.get('/Subtype') != pikepdf.Name('/Image'):
                            continue

                        f  = xobj.stream_dict.get('/Filter')
                        cs = xobj.stream_dict.get('/ColorSpace')

                        flist   = list(f)  if isinstance(f,  pikepdf.Array) else ([f]  if f  is not None else [])
                        cs_list = list(cs) if isinstance(cs, pikepdf.Array) else None

                        is_dual_flate = (len(flist) >= 2 and flist[0] == pikepdf.Name('/FlateDecode'))
                        is_indexed    = (cs_list is not None and str(cs_list[0]) == '/Indexed')

                        _t(f'normalize: image xobj {key} filters={flist} indexed={is_indexed}')

                        if not is_dual_flate and not is_indexed:
                            _t(f'normalize: xobj {key} skipped (no match)')
                            continue

                        # ── Strategy A: [/FlateDecode /DCTDecode], no Indexed CS ──
                        if (is_dual_flate and not is_indexed
                                and len(flist) == 2
                                and flist[1] == pikepdf.Name('/DCTDecode')):
                            _t(f'normalize: xobj {key} strategy A (strip outer FlateDecode)')
                            raw = xobj.read_raw_bytes()
                            try:
                                inflated = zlib.decompress(raw)
                            except zlib.error:
                                try:
                                    inflated = zlib.decompress(raw, wbits=-15)
                                except zlib.error:
                                    continue
                            dp = xobj.stream_dict.get('/DecodeParms')
                            dct_dp = None
                            if dp is not None:
                                dplist = list(dp) if isinstance(dp, pikepdf.Array) else [dp]
                                if len(dplist) > 1 and dplist[1] != pikepdf.Name('/Null'):
                                    dct_dp = dplist[1]
                            if dct_dp is not None:
                                xobj.write(inflated, filter=pikepdf.Name('/DCTDecode'),
                                           decode_parms=dct_dp)
                            else:
                                xobj.write(inflated, filter=pikepdf.Name('/DCTDecode'))
                            changed = True
                            continue

                        # ── Strategy B: full decode → plain FlateDecode re-encode ──
                        _t(f'normalize: xobj {key} strategy B (full decode)')
                        try:
                            decoded = xobj.read_bytes()   # QPDF decodes all filters
                        except Exception:
                            continue

                        width  = int(xobj.stream_dict['/Width'])
                        height = int(xobj.stream_dict['/Height'])
                        bpc    = int(xobj.stream_dict.get('/BitsPerComponent', 8))

                        try:
                            if is_indexed:
                                base_cs  = cs_list[1]
                                hival    = int(cs_list[2])
                                lut_obj  = cs_list[3]
                                if isinstance(lut_obj, pikepdf.Stream):
                                    lut_bytes = bytes(lut_obj.read_bytes())
                                else:
                                    lut_bytes = bytes(lut_obj)
                                n_comp = _indexed_base_ncomp(base_cs)
                                if n_comp is None:
                                    _t(f'normalize: xobj {key} left alone '
                                       f'(unmappable Indexed base {base_cs})')
                                    continue
                                if len(lut_bytes) < n_comp * (hival + 1):
                                    _t(f'normalize: xobj {key} left alone (short palette)')
                                    continue
                                lut = np.frombuffer(lut_bytes[:n_comp * (hival + 1)],
                                                    dtype=np.uint8).reshape(hival + 1, n_comp)

                                if bpc == 1:
                                    row_bytes = (width + 7) // 8
                                    arr     = np.frombuffer(bytes(decoded)[:row_bytes * height],
                                                            dtype=np.uint8)
                                    indices = np.unpackbits(arr).reshape(
                                        height, row_bytes * 8)[:, :width]
                                elif bpc == 8:
                                    indices = np.frombuffer(bytes(decoded)[:width * height],
                                                            dtype=np.uint8).reshape(height, width)
                                else:
                                    continue

                                indices = np.clip(indices, 0, hival)
                                mapped  = lut[indices]    # (height, width, n_comp)

                                # Prefer DeviceGray when an RGB palette is greyscale
                                if (n_comp == 3
                                        and np.all(lut[:, 0] == lut[:, 1])
                                        and np.all(lut[:, 0] == lut[:, 2])):
                                    raw_pixels = mapped[:, :, 0].tobytes()
                                    new_cs     = '/DeviceGray'
                                else:
                                    raw_pixels = mapped.tobytes()
                                    new_cs     = _NCOMP_TO_CS[n_comp]
                            else:
                                # Dual non-JPEG filter, no Indexed CS
                                raw_pixels = bytes(decoded)
                                cur_cs = xobj.stream_dict.get('/ColorSpace')
                                if cur_cs is None:
                                    continue
                                new_cs = (str(cur_cs) if isinstance(cur_cs, pikepdf.Name)
                                          else str(list(cur_cs)[0]))
                        except Exception:
                            continue

                        compressed = zlib.compress(raw_pixels, level=1)
                        xobj.write(compressed, filter=pikepdf.Name('/FlateDecode'))
                        xobj.stream_dict['/ColorSpace']       = pikepdf.Name(new_cs)
                        xobj.stream_dict['/BitsPerComponent'] = 8
                        if '/DecodeParms' in xobj.stream_dict:
                            del xobj.stream_dict['/DecodeParms']
                        # Drop any /Decode array carried over from the source image.
                        # An Indexed source has a 2-entry Decode ([0 hival], mapping
                        # samples to palette indices); the pixels are now fully-mapped
                        # DeviceRGB/DeviceGray values, so that array is invalid for the
                        # new colorspace.  pdfium reads the stale 2-entry array on a
                        # 3-component DeviceRGB image as "keep R, zero G and B", turning
                        # the whole image pure red.  The default decode is correct here.
                        if '/Decode' in xobj.stream_dict:
                            del xobj.stream_dict['/Decode']
                        changed = True

                if not changed:
                    _t('normalize: no changes needed, returning original bytes')
                    return pdf_bytes
                _t('normalize: saving modified PDF')
                buf = io.BytesIO()
                pdf.save(buf)
                result = buf.getvalue()
                _t(f'normalize: done, returning {len(result)} bytes')
                return result

    except Exception as _ex:
        _t(f'normalize: exception caught: {type(_ex).__name__}: {_ex}')
        return pdf_bytes


def _render_page(pdf_bytes, scale=VIEWER_SCALE, max_pixels=_MAX_RENDER_PIXELS):
    """Render a PDF page to a base64 PNG.

    Returns (image_b64, page_w_pts, page_h_pts, actual_scale).
    actual_scale may be less than the requested scale if the page is large
    enough to exceed *max_pixels*; callers must use it for coordinate
    transforms rather than assuming the requested scale.

    Zoom-in re-renders pass a higher `scale` (and `max_pixels=_MAX_RENDER_PIXELS_HI`)
    so the focused page is rasterized sharp at the current zoom rather than the
    base VIEWER_SCALE raster being upscaled by the client.
    """
    scale = min(scale, _MAX_RENDER_SCALE)
    _t('render: entry')
    import pypdfium2 as pdfium
    _t('render: normalizing')
    pdf_bytes = _normalize_page_for_pdfium(pdf_bytes)  # pikepdf lock held inside
    _t('render: acquiring NATIVE_LOCK for pdfium')
    with _NATIVE_LOCK:
        _t('render: creating pdfium PdfDocument')
        doc = pdfium.PdfDocument(pdf_bytes)
        _t('render: PdfDocument created')
        try:
            _t('render: getting page 0')
            page   = doc[0]
            page_w = page.get_width()
            page_h = page.get_height()
            # Cap render size so very large pages don't trigger pdfium edge cases.
            actual_scale = min(scale, (max_pixels / (page_w * page_h)) ** 0.5)
            _t(f'render: page {page_w:.1f}x{page_h:.1f}pt, scale={actual_scale:.3f}')
            _t('render: calling page.render()')
            bitmap = page.render(scale=actual_scale)
            _t('render: render done, converting to PIL')
            img = bitmap.to_pil()   # copies pixel data into PIL; no longer needs pdfium
        finally:
            _t('render: closing doc')
            doc.close()
            _t('render: doc closed')
    # PIL/PNG encoding is pure Python — runs outside the native lock
    _t('render: encoding PNG')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    result = base64.b64encode(buf.read()).decode('utf-8')
    _t('render: done')
    return result, page_w, page_h, actual_scale


# Review markups other than comments.  They are listed in the Comments panel so
# a reviewer's highlight or cloud is not invisible there, but they are NOT
# comments: they carry no `idx` and the overlay gives them no popup — the viewer
# edits them by lift-to-draft (_get_editable_annotations) instead.  Stamps are
# left out on purpose: the viewer writes one for every inserted image and
# signature, which are content, not review.
_LISTED_MARKUP = ('/Square', '/Circle', '/Polygon', '/PolyLine', '/Line',
                  '/Highlight', '/Underline', '/StrikeOut', '/Squiggly',
                  '/Ink', '/Caret')


def _line_kind(ann):
    """'line' | 'arrow' | 'darrow' for a /Line annotation, from its /LE."""
    le = ann.get('/LE')
    if hasattr(le, 'get_object'):
        le = le.get_object()
    le = list(le or [])
    heads = sum(1 for e in le[:2] if 'Arrow' in str(e))
    return 'darrow' if heads == 2 else 'arrow' if heads == 1 else 'line'


# ── Review threads: author, dates, replies and status ─────────────────────────
# A reply is the standard PDF shape — a /Text annotation whose /IRT ("in reply
# to") points at the annotation it answers — so a thread written here reads as
# a thread in Acrobat, and one written there reads as a thread here.  A review
# STATUS is the same thing plus /StateModel /Review and /State.  Both carry an
# empty appearance so pdfium never draws them as note icons on the page.
_REVIEW_STATES = ('Accepted', 'Rejected', 'Cancelled', 'Completed', 'None')


def _TS_mod(value):
    from pypdf.generic import TextStringObject
    return TextStringObject(value)


def _pdf_date_now():
    """Now, as a PDF date string: D:YYYYMMDDHHmmSS+HH'mm'."""
    import datetime as _dt
    now = _dt.datetime.now().astimezone()
    off = now.utcoffset() or _dt.timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = '+' if mins >= 0 else '-'
    mins = abs(mins)
    return now.strftime('D:%Y%m%d%H%M%S') + f"{sign}{mins // 60:02d}'{mins % 60:02d}'"


def _iso_from_pdf_date(value):
    """A PDF date string → ISO 8601 (with offset when the PDF gives one), or
    None.  Only the year is mandatory in a PDF date; everything else defaults."""
    import re
    import datetime as _dt
    m = re.match(r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
                 r"(?:([Zz+\-])(\d{2})?'?(\d{2})?'?)?", str(value or '').strip())
    if not m:
        return None
    try:
        y = int(m.group(1))
        mo, d = int(m.group(2) or 1), int(m.group(3) or 1)
        hh, mi, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
        tz = None
        if m.group(7):
            if m.group(7) in 'Zz':
                tz = _dt.timezone.utc
            else:
                delta = _dt.timedelta(hours=int(m.group(8) or 0),
                                      minutes=int(m.group(9) or 0))
                tz = _dt.timezone(delta if m.group(7) == '+' else -delta)
        return _dt.datetime(y, mo, d, hh, mi, ss, tzinfo=tz).isoformat()
    except (ValueError, OverflowError):
        return None


def _pdf_date_from_iso(value):
    """ISO 8601 → PDF date string, or None if it does not parse."""
    import datetime as _dt
    try:
        t = _dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        return t.strftime('D:%Y%m%d%H%M%S')
    mins = int(t.utcoffset().total_seconds() // 60)
    sign = '+' if mins >= 0 else '-'
    mins = abs(mins)
    return t.strftime('D:%Y%m%d%H%M%S') + f"{sign}{mins // 60:02d}'{mins % 60:02d}'"


def _ann_dates(ann):
    """(created_iso, modified_iso) for an annotation dictionary."""
    return (_iso_from_pdf_date(ann.get('/CreationDate')),
            _iso_from_pdf_date(ann.get('/M')))


def _irt_idnum(ann):
    """The object number an annotation replies to, or None."""
    irt = ann.get('/IRT')
    return getattr(irt, 'idnum', None)


def _is_review_state(ann):
    return str(ann.get('/StateModel', '')) == '/Review' and ann.get('/State') is not None


def _thread_positions(annots, root_pos):
    """/Annots positions of every reply (and status) under the annotation at
    root_pos — replies to replies included.  Empty for a direct object, which
    nothing can reply to."""
    root = annots[root_pos]
    want = {getattr(root, 'idnum', None)} - {None}
    found = []
    changed = True
    while changed and want:
        changed = False
        for pos, ref in enumerate(annots):
            if pos in found or pos == root_pos:
                continue
            ann = ref.get_object() if hasattr(ref, 'get_object') else ref
            if _irt_idnum(ann) in want:
                found.append(pos)
                idn = getattr(ref, 'idnum', None)
                if idn is not None:
                    want.add(idn)
                changed = True
    return sorted(found)


def _thread_of(annots, root_pos):
    """(replies, status) for the annotation at root_pos.  replies is oldest
    first; status is the most recent Review state, or None."""
    replies, status = [], None
    for pos in _thread_positions(annots, root_pos):
        ref = annots[pos]
        ann = ref.get_object() if hasattr(ref, 'get_object') else ref
        created, modified = _ann_dates(ann)
        item = {'annot_idx': pos,
                'author':   str(ann.get('/T', '') or '').strip(),
                'created':  created, 'modified': modified}
        if _is_review_state(ann):
            item['state'] = str(ann.get('/State')).lstrip('/')
            if status is None or (item['modified'] or item['created'] or '') >= \
                    (status['modified'] or status['created'] or ''):
                status = item
        elif str(ann.get('/StateModel', '')):
            continue                       # e.g. Acrobat's Marked/Unmarked ticks
        else:
            item['text'] = str(ann.get('/Contents', '') or '').strip()
            replies.append(item)
    replies.sort(key=lambda r: r['created'] or r['modified'] or '')
    return replies, status


def _make_thread_annot(writer, parent_ref, rect, author, text='', state=None,
                       created=None, modified=None):
    """A reply (or, with *state*, a Review status) to *parent_ref*."""
    from pypdf.generic import (DictionaryObject, NameObject, ArrayObject,
                               FloatObject, NumberObject, BooleanObject,
                               TextStringObject, DecodedStreamObject)
    rv = [float(v) for v in rect]
    annot = DictionaryObject({
        NameObject('/Type'):     NameObject('/Annot'),
        NameObject('/Subtype'):  NameObject('/Text'),
        NameObject('/Rect'):     ArrayObject([FloatObject(v) for v in rv]),
        NameObject('/Contents'): TextStringObject(text),
        NameObject('/T'):        TextStringObject(author),
        NameObject('/IRT'):      parent_ref,
        NameObject('/RT'):       NameObject('/R'),
        NameObject('/Open'):     BooleanObject(False),
        NameObject('/F'):        NumberObject(4),
        NameObject('/NM'):       TextStringObject(uuid.uuid4().hex),
        NameObject('/CreationDate'): TextStringObject(
            _pdf_date_from_iso(created) or _pdf_date_now()),
        NameObject('/M'):        TextStringObject(
            _pdf_date_from_iso(modified) or _pdf_date_now()),
    })
    if state:
        annot[NameObject('/StateModel')] = NameObject('/Review')
        annot[NameObject('/State')] = NameObject('/' + state)
    # Empty appearance: pdfium generates an icon for a /Text with no /AP, and a
    # reply must not show up on the page as a second note.
    xo = DecodedStreamObject()
    xo.set_data(b'')
    xo[NameObject('/Type')] = NameObject('/XObject')
    xo[NameObject('/Subtype')] = NameObject('/Form')
    xo[NameObject('/BBox')] = ArrayObject([FloatObject(0), FloatObject(0),
                                           FloatObject(max(1.0, rv[2] - rv[0])),
                                           FloatObject(max(1.0, rv[3] - rv[1]))])
    annot[NameObject('/AP')] = writer._add_object(
        DictionaryObject({NameObject('/N'): writer._add_object(xo)}))
    return writer._add_object(annot)


def _get_annotations(pdf_bytes):
    """Everything the Comments panel lists for a single-page PDF.

    Comments — /Text (sticky note) and /FreeText — carry `idx`, their position
    among the page's comments, which is the index basis update_annotation uses.
    FreeText drawn with the Text tool (/PWKind /Markup) and the other review
    markups in _LISTED_MARKUP are returned with `markup: True` and `idx: None`.
    Replies and review statuses (anything with /IRT) are never top-level: they
    ride on the item they answer, as `replies` and `status`.  Every entry also
    carries `annot_idx` (its raw /Annots position, what the thread routes take),
    `created` and `modified` (ISO 8601, or None).
    Coordinates are in the viewer display frame (y=0 at bottom)."""
    result = []
    try:
        reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return result
        page = reader.pages[0]
        annots = page.get('/Annots')
        if not annots:
            return result
        if hasattr(annots, 'get_object'):
            annots = annots.get_object()
        # Annotation /Rects live in raw content space; the viewer overlay draws
        # in the displayed (post-/Rotate) frame, so map raw→display here.
        rotate, raw_W, raw_H, off_x, off_y = _page_display_frame(pdf_bytes)
        editable_idx = 0
        for pos, ref in enumerate(annots):
            try:
                ann = ref.get_object() if hasattr(ref, 'get_object') else ref
                subtype = str(ann.get('/Subtype', ''))
                if ann.get('/IRT') is not None:
                    continue                # a reply/status: rides on its parent
                if subtype in ('/Text', '/FreeText'):
                    # FreeText drawn with the "Text" tool is page markup, not a
                    # comment: it is listed, but has no popup and no comment
                    # index.  Comments (sticky notes, the Add-comment box, and
                    # FreeText from other producers) have no marker.
                    markup = (subtype == '/FreeText' and
                              str(ann.get('/PWKind', '')) == '/Markup')
                elif subtype in _LISTED_MARKUP:
                    markup = True
                else:
                    continue
                rect = ann.get('/Rect')
                if rect is None:
                    continue
                if hasattr(rect, 'get_object'):
                    rect = rect.get_object()
                rv = [float(v) for v in rect]
                if len(rv) < 4:
                    continue
                # FreeText: prefer /IC (interior/fill color), fall back to /C
                # (border), then to the text colour in /DA.
                if subtype == '/FreeText':
                    color = (_read_color_arr(ann.get('/IC')) or
                             _read_color_arr(ann.get('/C')) or
                             _parse_da_style(str(ann.get('/DA', '') or ''))[1])
                else:
                    color = _read_color_arr(ann.get('/C')) or [1.0, 0.92, 0.0]
                xywh = _raw_rect_to_display(
                    {'x': min(rv[0], rv[2]), 'y': min(rv[1], rv[3]),
                     'w': abs(rv[2] - rv[0]), 'h': abs(rv[3] - rv[1])},
                    rotate, raw_W, raw_H, off_x, off_y)
                created, modified = _ann_dates(ann)
                replies, status = _thread_of(annots, pos)
                entry = {
                    'idx':     None if markup else editable_idx,
                    'annot_idx': pos,
                    'created': created,
                    'modified': modified,
                    'replies': replies,
                    'status':  status,
                    'x':       xywh['x'],
                    'y':       xywh['y'],
                    'w':       xywh['w'],
                    'h':       xywh['h'],
                    'text':    str(ann.get('/Contents', '') or '').strip(),
                    'author':  str(ann.get('/T', '') or '').strip(),
                    'color':   color,
                    'subtype': subtype.lstrip('/'),
                    'markup':  markup,
                }
                if subtype == '/Line':
                    entry['kind'] = _line_kind(ann)
                result.append(entry)
                if not markup:
                    editable_idx += 1
            except Exception:
                continue
    except Exception:
        pass
    return result


# ── Link annotations (clickable URLs) ────────────────────────────────────────
# Schemes we are willing to hand to the OS.  A /URI action can name anything the
# producer likes — `file:`, `javascript:`, `ms-search:` — and the shell handlers
# these end up in will happily resolve it, so this is an allow-list rather than
# a list of things to strip.  Everything not on it simply gets no hit area,
# which is also the honest answer: the viewer cannot follow it.
_LINK_SCHEMES = ('http://', 'https://', 'mailto:')

# Long enough for any real link; short enough that a pathological /URI cannot be
# used to push megabytes through the page payload.
_LINK_URI_MAX = 2048


def _normalize_link_uri(raw):
    """A raw /URI value → a URL we will open, or None if we won't touch it."""
    try:
        uri = str(raw or '').strip()
    except Exception:
        return None
    if not uri or len(uri) > _LINK_URI_MAX:
        return None
    # Control characters are either producer sloppiness or an attempt to hide a
    # second line from the scheme check.  Either way, not a link we follow.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in uri):
        return None
    # Word and older Acrobat write typed links as bare hostnames, with no
    # scheme at all.  Every viewer treats those as https.
    if uri.lower().startswith('www.'):
        uri = 'https://' + uri
    if not uri.lower().startswith(_LINK_SCHEMES):
        return None
    return uri


def _link_uri_of(ann):
    """The URL a /Link annotation points at, or None for one we don't follow.

    Only /URI actions qualify.  A /GoTo (or a bare /Dest) is a jump inside this
    document: real, but not a browser link, and giving it a hit area that opens
    nothing would be worse than leaving it alone."""
    action = ann.get('/A')
    if action is None:
        return None
    if hasattr(action, 'get_object'):
        action = action.get_object()
    try:
        if str(action.get('/S', '')) != '/URI':
            return None
        uri = action.get('/URI')
    except AttributeError:
        return None
    if hasattr(uri, 'get_object'):
        uri = uri.get_object()
    return _normalize_link_uri(uri)


def _get_links(pdf_bytes):
    """Extract followable /Link annotations from a single-page PDF.

    Returns dicts in the same frame as _get_annotations — display (post-/Rotate)
    coordinates, PDF user space with y=0 at the bottom — so the viewer's existing
    pdfToDisplay math places them without a second convention to get wrong.

    `idx` is the position in THIS list, not in /Annots.  /api/viewer/open_link
    re-derives the list from the same function and indexes it the same way, so
    the page never names a URL: it names a link, and the document says where
    that link goes.
    """
    result = []
    try:
        reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return result
        annots = reader.pages[0].get('/Annots')
        if not annots:
            return result
        if hasattr(annots, 'get_object'):
            annots = annots.get_object()
        # Resolve the display frame lazily — it costs a pdfium open under
        # _NATIVE_LOCK, and the overwhelming majority of pages have no link at all.
        rotate = raw_W = raw_H = None
        off_x = off_y = 0.0
        for ref in annots:
            try:
                ann = ref.get_object() if hasattr(ref, 'get_object') else ref
                if str(ann.get('/Subtype', '')) != '/Link':
                    continue
                url = _link_uri_of(ann)
                if not url:
                    continue
                rect = ann.get('/Rect')
                if rect is None:
                    continue
                if hasattr(rect, 'get_object'):
                    rect = rect.get_object()
                rv = [float(v) for v in rect]
                if len(rv) < 4:
                    continue
                # /Rect corners are not required to be ordered.
                x1, x2 = sorted((rv[0], rv[2]))
                y1, y2 = sorted((rv[1], rv[3]))
                if x2 - x1 <= 0 or y2 - y1 <= 0:
                    continue
                if rotate is None:
                    rotate, raw_W, raw_H, off_x, off_y = _page_display_frame(pdf_bytes)
                xywh = _raw_rect_to_display(
                    {'x': x1, 'y': y1, 'w': x2 - x1, 'h': y2 - y1},
                    rotate, raw_W, raw_H, off_x, off_y)
                result.append({
                    'idx': len(result),
                    'x':   xywh['x'],
                    'y':   xywh['y'],
                    'w':   xywh['w'],
                    'h':   xywh['h'],
                    'url': url,
                })
            except Exception:
                continue
    except Exception:
        pass
    return result


def _read_color_arr(v):
    """Read a PDF colour array → [r,g,b] (0..1) or None. Handles gray/rgb/cmyk-ish."""
    if v is None:
        return None
    if hasattr(v, 'get_object'):
        v = v.get_object()
    try:
        c = [float(x) for x in v]
    except Exception:
        return None
    if len(c) >= 3:
        return c[:3]
    if len(c) == 1:
        return [c[0], c[0], c[0]]
    return None


def _parse_da_style(da):
    """Parse a FreeText /DA string → (font_size, text_color[r,g,b])."""
    import re
    size, color = 11.0, [0.0, 0.0, 0.0]
    m = re.search(r'([\d.]+)\s+Tf', da or '')
    if m:
        try: size = float(m.group(1))
        except ValueError: pass
    m = re.search(r'([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg', da or '')
    if m:
        color = [float(m.group(1)), float(m.group(2)), float(m.group(3))]
    else:
        m = re.search(r'(?:^|\s)([\d.]+)\s+g\b', da or '')
        if m:
            g = float(m.group(1)); color = [g, g, g]
    return size, color


def _annot_edit_info(ann, idx):
    """Parse a single annotation into a draft-rebuildable dict, or None if the
    viewer doesn't support editing it.  annot_idx is the raw /Annots position."""
    subtype = str(ann.get('/Subtype', ''))
    rect = ann.get('/Rect')
    if hasattr(rect, 'get_object'):
        rect = rect.get_object()
    try:
        rv = [float(v) for v in rect] if rect else None
    except Exception:
        rv = None

    # Border width from /BS /W (default 1 when a colour is present, else 0)
    bs = ann.get('/BS')
    if hasattr(bs, 'get_object'):
        bs = bs.get_object()
    border_w = None
    if bs is not None:
        try: border_w = float(bs.get('/W', 1.0))
        except Exception: border_w = None

    # Dash style from /BS /S + /D. We only ever write two patterns
    # (dashed = [3w,3w], dotted = [w,2w]); classify foreign dashes by whether
    # the on-length is short relative to the stroke width.
    border_style = 'solid'
    if bs is not None:
        try:
            if str(bs.get('/S', '')) == '/D':
                darr = bs.get('/D')
                if hasattr(darr, 'get_object'): darr = darr.get_object()
                on = float(darr[0]) if darr else 3.0
                w  = border_w if border_w and border_w > 0 else 1.0
                border_style = 'dotted' if on <= w * 1.5 else 'dashed'
        except Exception:
            pass

    border_color = _read_color_arr(ann.get('/C'))
    fill_color   = _read_color_arr(ann.get('/IC'))

    def _pwrect():
        v = ann.get('/PWRect')
        if hasattr(v, 'get_object'): v = v.get_object()
        try:
            f = [float(x) for x in v]
            return f if len(f) >= 4 else None
        except Exception:
            return None

    base = {'annot_idx': idx, 'subtype': subtype.lstrip('/'),
            'border_color': border_color,
            'border_width': border_w if border_w is not None else (1.0 if border_color else 0.0),
            'border_style': border_style,
            'fill_color': fill_color,
            'text': str(ann.get('/Contents', '') or ''),
            'author': str(ann.get('/T', '') or '')}

    if subtype == '/FreeText':
        import re as _re
        da = str(ann.get('/DA', '') or '')
        size, tcolor = _parse_da_style(da)
        fm = _re.search(r'/(\S+)\s+[\d.]+\s+Tf', da)
        font = fm.group(1) if fm else 'Helvetica'
        base.update({'tool': 'text', 'rect': rv, 'text_color': tcolor,
                     'font_size': size, 'font': font,
                     'is_comment': str(ann.get('/PWKind', '')) != '/Markup'})
        return base
    if subtype in ('/Square', '/Circle'):
        base.update({'tool': 'rect' if subtype == '/Square' else 'ellipse',
                     'rect': _pwrect() or rv})
        return base
    if subtype == '/Polygon':
        base.update({'tool': 'cloud', 'rect': _pwrect() or rv})
        return base
    if subtype == '/Line':
        L = ann.get('/L')
        if hasattr(L, 'get_object'): L = L.get_object()
        try: line = [float(v) for v in L][:4]
        except Exception: line = None
        le = ann.get('/LE')
        if hasattr(le, 'get_object'): le = le.get_object()
        le = list(le or [])
        head_start = len(le) > 0 and 'Arrow' in str(le[0])
        head_end   = len(le) > 1 and 'Arrow' in str(le[1])
        # A start-only arrowhead has no tool of its own, so flip the endpoints
        # and lift it as a normal end-headed arrow rather than dropping the head.
        if head_start and not head_end and line and len(line) >= 4:
            line = [line[2], line[3], line[0], line[1]]
            head_start, head_end = False, True
        tool = ('darrow' if (head_start and head_end)
                else 'arrow' if head_end else 'line')
        base.update({'tool': tool, 'line': line,
                     'arrow': head_end, 'arrow_start': head_start})
        return base
    if subtype == '/Highlight':
        base.update({'tool': 'highlight', 'rect': rv,
                     'border_color': border_color or _read_color_arr(ann.get('/C'))})
        return base
    if subtype == '/Text':
        base.update({'tool': 'sticky', 'rect': rv})
        return base
    if subtype == '/Stamp':
        # Geometry only — the image bytes are fetched on demand at lift time
        # (via /api/viewer/stamp_image) to keep editable payloads light.
        base.update({'tool': 'image', 'rect': rv})
        return base
    return None


def _get_editable_annotations(pdf_bytes):
    """Enumerate every viewer-editable annotation with geometry + style so the
    frontend can draw selection hit-areas and rebuild a draft (lift-to-edit)."""
    out = []
    try:
        reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return out
        annots = reader.pages[0].get('/Annots')
        if not annots:
            return out
        if hasattr(annots, 'get_object'):
            annots = annots.get_object()
        # Geometry from _annot_edit_info is in raw content space; the frontend
        # draws hit-areas and lifted drafts in the displayed (post-/Rotate)
        # frame, so map raw→display (identity for /Rotate 0).
        rotate, raw_W, raw_H, off_x, off_y = _page_display_frame(pdf_bytes)
        for i, ref in enumerate(annots):
            try:
                ann = ref.get_object() if hasattr(ref, 'get_object') else ref
                if ann.get('/IRT') is not None:
                    continue                # replies are not objects on the page
                info = _annot_edit_info(ann, i)
                if info and (info.get('rect') is not None or info.get('line') is not None):
                    # Lifting deletes the annotation (and, with it, its thread)
                    # and re-commits a new one, so the thread has to travel with
                    # the draft or moving a shape would silently drop its replies.
                    info['created'] = _ann_dates(ann)[0]
                    replies, status = _thread_of(annots, i)
                    info['replies'] = [
                        {k: r.get(k) for k in ('text', 'author', 'created', 'modified')}
                        for r in replies]
                    if status:
                        info['replies'].append(
                            {k: status.get(k) for k in ('state', 'author', 'created', 'modified')})
                    # NB: not `if rotate` — a cropped page needs the translation
                    # even when it is not rotated.  The transforms are a no-op
                    # when there is nothing to do.
                    if rotate or off_x or off_y:
                        if info.get('rect') is not None:
                            info['rect'] = _xform_pdf_rect(
                                info['rect'], _raw_rect_to_display,
                                rotate, raw_W, raw_H, off_x, off_y)
                        if info.get('line') is not None:
                            info['line'] = _xform_pdf_points(
                                info['line'], _raw_rect_to_display,
                                rotate, raw_W, raw_H, off_x, off_y)
                    out.append(info)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _render_thumb_b64(pdf_bytes):
    """Render page 0 to a ~200px-wide base64 PNG thumbnail."""
    try:
        import pypdfium2 as pdfium
        with _NATIVE_LOCK:
            doc = pdfium.PdfDocument(pdf_bytes)
            try:
                pg  = doc[0]
                sc  = 200 / pg.get_width()
                bmp = pg.render(scale=sc)
                img = bmp.to_pil()
            finally:
                doc.close()
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')
    except Exception:
        return None


# Helvetica glyph widths per 1000 units of em (Adobe AFM standard metrics).
# Used for word-wrap width measurement when editing FreeText annotations.
_HELVETICA_W = {
    ' ':278,'!':278,'"':355,'#':556,'$':556,'%':889,'&':667,"'":222,
    '(':333,')':333,'*':389,'+':584,',':278,'-':333,'.':278,'/':278,
    '0':556,'1':556,'2':556,'3':556,'4':556,'5':556,'6':556,'7':556,
    '8':556,'9':556,':':278,';':278,'<':584,'=':584,'>':584,'?':556,
    'A':667,'B':667,'C':722,'D':722,'E':667,'F':611,'G':778,'H':722,
    'I':278,'J':500,'K':667,'L':556,'M':833,'N':722,'O':778,'P':667,
    'Q':778,'R':722,'S':667,'T':611,'U':722,'V':667,'W':944,'X':667,
    'Y':667,'Z':611,
    'a':556,'b':611,'c':556,'d':611,'e':556,'f':333,'g':611,'h':611,
    'i':278,'j':278,'k':556,'l':278,'m':889,'n':611,'o':611,'p':611,
    'q':611,'r':389,'s':556,'t':333,'u':611,'v':556,'w':778,'x':556,
    'y':556,'z':500,
    '\xa0':278,            # non-breaking space ≈ regular space width
}


def _w_pts(ch, size):
    """Approximate character width in points (Helvetica metrics, safe default 500)."""
    return _HELVETICA_W.get(ch, 500) / 1000.0 * size


def _pdf_unescape(s):
    """Decode a PDF literal-string body (the bytes between the parens)."""
    import re
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n:
            nxt = s[i + 1]
            simple = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b',
                      'f': '\f', '(': '(', ')': ')', '\\': '\\'}
            if nxt in simple:
                out.append(simple[nxt]); i += 2; continue
            m = re.match(r'[0-7]{1,3}', s[i + 1:])
            if m:
                out.append(chr(int(m.group(0), 8) & 0xFF))
                i += 1 + len(m.group(0)); continue
            if nxt in '\r\n':            # line continuation
                i += 2; continue
            out.append(nxt); i += 2; continue
        out.append(c); i += 1
    return ''.join(out)


def _parse_text_runs(inner):
    """Walk a text block's operators in order and return styled runs.

    Returns (runs, font_name, font_size, text_color) where runs is a list of
    {'text', 'font', 'size', 'color'} preserving the font alias / size / colour
    active for each shown string.  Original line breaks (Td/T*/Tm) are ignored —
    we re-wrap from scratch — but every Tf and colour change is captured so that
    bold/italic/colour runs survive the round-trip.
    """
    import re
    token = re.compile(r"""
        (?P<str>\((?:\\.|[^\\()])*\))\s*(?P<show>Tj|')      # (string) Tj | '
      | (?P<arr>\[(?:\\.|[^[\]\\])*\])\s*TJ                 # [ ... ] TJ
      | /(?P<font>[^\s/]+)\s+(?P<size>[-\d.]+)\s+Tf         # /F0 11 Tf
      | (?P<rr>[-\d.]+)\s+(?P<gg>[-\d.]+)\s+(?P<bb>[-\d.]+)\s+rg   # r g b rg
      | (?P<gray>[-\d.]+)\s+g\b                             # gray g
      | (?P<ck>[-\d.]+)\s+(?P<mk>[-\d.]+)\s+(?P<yk>[-\d.]+)\s+(?P<kk>[-\d.]+)\s+k
    """, re.VERBOSE)

    cur_font, cur_size, cur_color = None, None, '0 g'
    first_font, first_size, first_color = None, None, None
    runs = []

    def _emit(text):
        if not text:
            return
        runs.append({'text': text,
                     'font':  cur_font or 'Helvetica',
                     'size':  cur_size if cur_size is not None else 11.0,
                     'color': cur_color})

    for m in token.finditer(inner):
        if m.group('font') is not None:
            cur_font = m.group('font')
            try: cur_size = float(m.group('size'))
            except ValueError: cur_size = 11.0
            if first_font is None:
                first_font, first_size = cur_font, cur_size
        elif m.group('rr') is not None:
            cur_color = (f"{float(m.group('rr')):.4f} {float(m.group('gg')):.4f} "
                         f"{float(m.group('bb')):.4f} rg")
            if first_color is None: first_color = cur_color
        elif m.group('gray') is not None:
            cur_color = f"{float(m.group('gray')):.4f} g"
            if first_color is None: first_color = cur_color
        elif m.group('ck') is not None:
            cur_color = (f"{float(m.group('ck')):.4f} {float(m.group('mk')):.4f} "
                         f"{float(m.group('yk')):.4f} {float(m.group('kk')):.4f} k")
            if first_color is None: first_color = cur_color
        elif m.group('str') is not None:
            _emit(_pdf_unescape(m.group('str')[1:-1]))
        elif m.group('arr') is not None:
            # Concatenate the literal-string parts of a TJ array; ignore the
            # numeric kerning adjustments (small, not worth reconstructing).
            parts = re.findall(r'\((?:\\.|[^\\()])*\)', m.group('arr'))
            _emit(''.join(_pdf_unescape(p[1:-1]) for p in parts))

    return (runs,
            first_font or 'Helvetica',
            first_size if first_size is not None else 11.0,
            first_color or '0 g')


def _style_new_text(new_text, runs, default_style):
    """Map each character of *new_text* to a style, using the original runs.

    Aligns the visible run text against the (possibly edited) new text with
    difflib so that styling follows the words even when wrapping whitespace
    differs.  Inserted/edited characters inherit the style of the preceding
    character.  Returns a list of {'c', 'font', 'size', 'color'} dicts.
    """
    import difflib
    ap_styles = []
    for run in runs:
        for ch in run['text']:
            ap_styles.append({'font': run['font'], 'size': run['size'],
                              'color': run['color']})
    ap_plain = ''.join(ch for run in runs for ch in run['text'])

    styled = []
    sm = difflib.SequenceMatcher(None, ap_plain, new_text, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(j2 - j1):
                st = ap_styles[i1 + k]
                styled.append({'c': new_text[j1 + k], **st})
        elif tag in ('replace', 'insert'):
            if i1 > 0 and ap_styles:
                base = ap_styles[min(i1 - 1, len(ap_styles) - 1)]
            elif styled:
                base = styled[-1]
            else:
                base = default_style
            for k in range(j1, j2):
                styled.append({'c': new_text[k], 'font': base['font'],
                               'size': base['size'], 'color': base['color']})
        # tag == 'delete': original chars removed by the edit → drop
    return styled


def _wrap_styled(styled, avail_pts):
    """Word-wrap a styled-character list into lines, honouring per-char width.

    Hard breaks on '\\n'; soft-wraps words to fit avail_pts using each
    character's own font size for measurement.  Returns a list of lines, each a
    list of styled-char dicts (no trailing space chars).
    """
    def _w(ch):
        return _w_pts(ch['c'], ch['size'])

    lines = []
    # Split into paragraphs on hard newlines first.
    para = []
    paras = []
    for ch in styled:
        if ch['c'] == '\n':
            paras.append(para); para = []
        else:
            para.append(ch)
    paras.append(para)

    for pchars in paras:
        # Split paragraph into words on regular spaces (nbsp stays in-word).
        words, cur = [], []
        for ch in pchars:
            if ch['c'] == ' ':
                if cur:
                    words.append(cur); cur = []
            else:
                cur.append(ch)
        if cur:
            words.append(cur)

        if not words:
            lines.append([])               # preserve blank line
            continue

        line, line_w = [], 0.0
        for w in words:
            ww = sum(_w(c) for c in w)
            if not line:
                line, line_w = list(w), ww
            else:
                sp_size = line[-1]['size']
                sp_w = _w_pts(' ', sp_size)
                if line_w + sp_w + ww <= avail_pts:
                    line.append({'c': ' ', 'font': line[-1]['font'],
                                 'size': sp_size, 'color': line[-1]['color']})
                    line.extend(w); line_w += sp_w + ww
                else:
                    lines.append(line); line, line_w = list(w), ww
        lines.append(line)
    return lines


def _patch_freetext_ap_text(ann_obj, new_text, writer):
    """Patch a FreeText annotation's /AP with updated text.

    Reads the original /AP stream, keeps everything outside BT...ET
    (background fill, border, graphics state) and rebuilds only the text
    operators with accurate glyph-width wrapping.

    Creates a *new* XObject in the writer (rather than mutating in-place) so
    pypdf reliably writes all font resource references — this is what preserves
    bold/italic weight across saves.

    Returns True on success; caller falls back to _generate_freetext_ap.
    """
    import re
    from pypdf.generic import (DecodedStreamObject, DictionaryObject, NameObject)

    ap_ref = ann_obj.get('/AP')
    if ap_ref is None:
        return False
    if hasattr(ap_ref, 'get_object'):
        ap_ref = ap_ref.get_object()
    n_ref = ap_ref.get('/N')
    if n_ref is None:
        return False
    n_obj = n_ref.get_object() if hasattr(n_ref, 'get_object') else n_ref

    try:
        stream_str = n_obj.get_data().decode('latin-1')

        # Capture from the FIRST BT to the LAST ET so that appearances built
        # from several BT…ET blocks (one per colour/font run, as some producers
        # emit) are all re-flowed together rather than truncated.
        bt_m = re.search(r'\bBT\b', stream_str)
        et_all = list(re.finditer(r'\bET\b', stream_str))
        if not bt_m or not et_all:
            return False
        et_last = et_all[-1]

        before_bt = stream_str[:bt_m.start()]
        bt_block  = stream_str[bt_m.start():et_last.end()]
        after_et  = stream_str[et_last.end():]

        # ── Parse styled runs (font alias, size, colour per shown string) ──
        runs, font_name, font_size, text_color = _parse_text_runs(bt_block)

        # ── Text position: Td preferred, fall back to Tm e/f components ──
        td_m = re.search(r'([-\d.]+)\s+([-\d.]+)\s+Td', bt_block)
        tm_m = re.search(
            r'[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+([-\d.]+)\s+([-\d.]+)\s+Tm',
            bt_block)
        pos_m = td_m or tm_m
        pos_x = float(pos_m.group(1)) if pos_m else 3.0
        pos_y = float(pos_m.group(2)) if pos_m else 3.0

        # ── Line leading ──────────────────────────────────────────────────
        tl_m   = re.search(r'([\d.]+)\s+TL', bt_block)
        line_h = float(tl_m.group(1)) if tl_m else round(font_size * 1.35, 2)

        # ── Available line width from the XObject's own /BBox ─────────────
        # CRITICAL: the text-start X (pos_x) is in the SAME coordinate space
        # as the BBox.  Many producers (Adobe et al.) do NOT normalise the
        # BBox to the origin — it is left in page space, e.g.
        # [rx1, ry1, rx2, ry2], and the text uses absolute page coords too.
        # The left inset is therefore (pos_x - bbox_x0), NOT pos_x.  Using
        # pos_x alone (assuming bbox_x0 == 0) collapses avail_w to a fraction
        # of the real width whenever bbox_x0 is large → text wraps far too
        # early.  patchwork's own appearances use bbox_x0 == 0, so this also
        # reduces to the previous behaviour for self-created annotations.
        bbox_ref = n_obj.get('/BBox')
        if hasattr(bbox_ref, 'get_object'):
            bbox_ref = bbox_ref.get_object()
        if bbox_ref and len(bbox_ref) >= 4:
            bbox_x0 = float(bbox_ref[0])
            xobj_w  = float(bbox_ref[2]) - bbox_x0
        else:
            bbox_x0 = 0.0
            rect_ref = ann_obj.get('/Rect')
            if hasattr(rect_ref, 'get_object'):
                rect_ref = rect_ref.get_object()
            xobj_w = (float(rect_ref[2]) - float(rect_ref[0])) if rect_ref else 200.0

        # Left inset of the text relative to the BBox's own left edge.
        left_margin = abs(pos_x - bbox_x0)
        # Subtract that inset on both sides (symmetric left/right margin).
        avail_w = xobj_w - left_margin * 2
        # Defensive clamp: if the geometry still yields an implausibly narrow
        # width (< 40% of the box), the position operator was probably in a
        # different space than the BBox — fall back to a small fixed margin so
        # we never wrap dramatically tighter than the box itself.
        if avail_w < xobj_w * 0.4:
            avail_w = max(10.0, xobj_w - 6.0)
        avail_w = max(10.0, avail_w)

        n_runs   = len(runs)
        n_styles = len({(r['font'], round(r['size'], 1)) for r in runs})
        logger.info(
            "[FREETEXT-WRAP] bbox_x0=%.2f xobj_w=%.2f pos_x=%.2f pos_y=%.2f "
            "left_margin=%.2f avail_w=%.2f font=%s size=%.2f from=%s "
            "runs=%d distinct_styles=%d text_len=%d",
            bbox_x0, xobj_w, pos_x, pos_y, left_margin, avail_w,
            font_name, font_size, ('Td' if td_m else 'Tm' if tm_m else 'default'),
            n_runs, n_styles, len(new_text))

        # ── Re-flow text while preserving each character's original style ──
        default_style = {'font': font_name, 'size': font_size, 'color': text_color}
        if runs:
            styled = _style_new_text(new_text, runs, default_style)
        else:
            # No parsed runs (unusual) → treat all text as the default style.
            styled = [{'c': c, **default_style} for c in new_text]
        wrapped = _wrap_styled(styled, avail_w)
        logger.info("[FREETEXT-WRAP] produced %d line(s): %r",
                    len(wrapped),
                    [''.join(c['c'] for c in ln) for ln in wrapped][:8])

        def _esc(s):
            return (s.replace('\\', '\\\\').replace('(', '\\(')
                     .replace(')', '\\)').replace('\r', '').replace('\n', ''))

        new_bt = ['BT', f'{line_h:.3f} TL']
        cur_f, cur_s, cur_c = None, None, None
        for li, line in enumerate(wrapped):
            # Position: first line absolute Td, subsequent lines T* (uses TL).
            new_bt.append(f'{pos_x:.3f} {pos_y:.3f} Td' if li == 0 else 'T*')
            # Group consecutive chars sharing font+size+colour into one Tj.
            seg = []
            seg_f = seg_s = seg_c = None
            def _flush():
                nonlocal cur_f, cur_s, cur_c, seg, seg_f, seg_s, seg_c
                if not seg:
                    return
                if (seg_f, seg_s) != (cur_f, cur_s):
                    new_bt.append(f'/{seg_f} {seg_s:.2f} Tf')
                    cur_f, cur_s = seg_f, seg_s
                if seg_c != cur_c:
                    new_bt.append(seg_c)
                    cur_c = seg_c
                new_bt.append(f'({_esc("".join(seg))}) Tj')
                seg = []
            for ch in line:
                if (ch['font'], ch['size'], ch['color']) != (seg_f, seg_s, seg_c):
                    _flush()
                    seg_f, seg_s, seg_c = ch['font'], ch['size'], ch['color']
                seg.append(ch['c'])
            _flush()
        new_bt.append('ET')

        new_bytes = (before_bt + '\n'.join(new_bt) + '\n' + after_et
                     ).encode('latin-1', errors='replace')

        # ── Create a new XObject in the writer (not in-place mutation) ────
        # Copying /Resources by reference keeps the original font dict
        # (including bold/italic) visible to pypdf when it writes the file.
        new_xobj = DecodedStreamObject()
        new_xobj.set_data(new_bytes)
        new_xobj[NameObject('/Type')]    = NameObject('/XObject')
        new_xobj[NameObject('/Subtype')] = NameObject('/Form')
        for key in ('/BBox', '/Resources', '/Matrix'):
            v = n_obj.get(key)
            if v is not None:
                new_xobj[NameObject(key)] = v

        new_n   = writer._add_object(new_xobj)
        new_ap  = writer._add_object(DictionaryObject({NameObject('/N'): new_n}))
        ann_obj[NameObject('/AP')] = new_ap
        logger.info("[FREETEXT-WRAP] patched /AP in place (success)")
        return True

    except Exception:
        logger.exception("[FREETEXT-WRAP] patch path FAILED — "
                         "falling back to full _generate_freetext_ap")
        return False


def _ap_upright_matrix(rotate):
    """Form-XObject /Matrix that keeps appearance content upright on a page with
    /Rotate `rotate`.  The content is authored upright in a w×h box; this rotates
    it by -rotate so the page's display rotation cancels.  Per PDF spec the
    transformed BBox is then re-fitted to /Rect, so only the linear (rotation)
    part matters — translation is left at 0.  None for /Rotate 0.
    """
    if rotate == 90:
        return [0.0, 1.0, -1.0, 0.0, 0.0, 0.0]    # +90° (CCW) cancels CW display
    if rotate == 180:
        return [-1.0, 0.0, 0.0, -1.0, 0.0, 0.0]
    if rotate == 270:
        return [0.0, -1.0, 1.0, 0.0, 0.0, 0.0]    # -90° (CW)
    return None


def _generate_freetext_ap(ann_obj, text, rect_vals, writer, layout_wh=None,
                          ap_matrix=None):
    """Build and attach an /AP /N appearance XObject for a FreeText annotation.

    Reads font/size from /DA, background from /IC, border from /C so the
    regenerated appearance matches the annotation's declared style.

    layout_wh/ap_matrix: on a rotated page the text is laid out upright in the
    display-frame box dims (layout_wh) and rotated into raw space by ap_matrix,
    so it renders upright after /Rotate.  Omit both for unrotated pages.
    """
    import re
    from pypdf.generic import (DictionaryObject, NameObject, ArrayObject,
                               FloatObject, DecodedStreamObject)

    rx1, ry1, rx2, ry2 = rect_vals
    if layout_wh:
        bw, bh = layout_wh                       # author upright in display dims
    else:
        bw = rx2 - rx1
        bh = ry2 - ry1
    if bw <= 0 or bh <= 0:
        return

    # ── Parse /DA for font name, size, text-colour operators ──────────────
    da = str(ann_obj.get('/DA', '') or '/Helvetica 11 Tf 0 g')
    font_name  = 'Helvetica'
    font_size  = 11.0
    tf_m = re.search(r'/(\S+)\s+([\d.]+)\s+Tf', da)
    if tf_m:
        font_name = tf_m.group(1)
        try: font_size = float(tf_m.group(2))
        except ValueError: pass
    # Everything after "Tf" in /DA is the text-colour operator(s)
    text_color_ops = '0 g'
    after_tf = re.search(r'\bTf\b\s+(.*)', da)
    if after_tf and after_tf.group(1).strip():
        text_color_ops = after_tf.group(1).strip()

    # ── Background fill from /IC ───────────────────────────────────────────
    ops = ['q']
    ic = ann_obj.get('/IC')
    if ic is not None:
        if hasattr(ic, 'get_object'): ic = ic.get_object()
        try:
            rgb = [float(c) for c in ic]
            if len(rgb) >= 3:
                ops.append(f'{rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} rg')
                ops.append(f'0 0 {bw:.3f} {bh:.3f} re f')
        except Exception:
            pass

    # ── Border stroke from /C, width from /BS /W (skip when width<=0) ───────
    bs = ann_obj.get('/BS')
    if hasattr(bs, 'get_object'): bs = bs.get_object()
    border_w = 1.0
    if bs is not None:
        try: border_w = float(bs.get('/W', 1.0))
        except Exception: border_w = 1.0
    c_arr = ann_obj.get('/C')
    if c_arr is not None and border_w > 0:
        if hasattr(c_arr, 'get_object'): c_arr = c_arr.get_object()
        try:
            rgb = [float(c) for c in c_arr]
            if len(rgb) >= 3:
                half = border_w / 2.0
                ops += [f'{rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} RG',
                        f'{border_w:.3f} w',
                        f'{half:.3f} {half:.3f} {bw-border_w:.3f} '
                        f'{bh-border_w:.3f} re S']
        except Exception:
            pass

    # ── Word-wrap text ─────────────────────────────────────────────────────
    pad  = max(3.0, font_size * 0.35)
    lh   = font_size * 1.35
    cpl  = max(1, int((bw - 2 * pad) / (font_size * 0.55)))  # chars per line
    logger.info("[FREETEXT-GEN] fallback regen: bw=%.2f bh=%.2f pad=%.2f "
                "font=%s size=%.2f chars_per_line=%d text_len=%d",
                bw, bh, pad, font_name, font_size, cpl, len(text))

    def _wrap(para):
        if not para:
            return ['']
        words, lines, cur = para.split(' '), [], ''
        for w in words:
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= cpl:
                cur += ' ' + w
            else:
                lines.append(cur); cur = w
        lines.append(cur)
        return lines

    def _esc(s):
        return (s.replace('\\', '\\\\').replace('(', '\\(')
                 .replace(')', '\\)').replace('\r','').replace('\n',''))

    wrapped = []
    for para in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        wrapped.extend(_wrap(para))

    start_y = bh - pad - font_size
    ops += ['BT', f'/{font_name} {font_size:.2f} Tf', text_color_ops,
            f'{pad:.3f} {start_y:.3f} Td', f'{lh:.3f} TL']
    for i, ln in enumerate(wrapped):
        ops.append(f'({_esc(ln)}) Tj' if i == 0 else f'T* ({_esc(ln)}) Tj')
    ops += ['ET', 'Q']

    stream_bytes = '\n'.join(ops).encode('latin-1', errors='replace')

    # ── Build XObject ──────────────────────────────────────────────────────
    xobj = DecodedStreamObject()
    xobj.set_data(stream_bytes)
    xobj[NameObject('/Type')]    = NameObject('/XObject')
    xobj[NameObject('/Subtype')] = NameObject('/Form')
    xobj[NameObject('/BBox')]    = ArrayObject([
        FloatObject(0.0), FloatObject(0.0), FloatObject(bw), FloatObject(bh)])
    if ap_matrix:
        xobj[NameObject('/Matrix')] = ArrayObject([FloatObject(v) for v in ap_matrix])
    font_dict = DictionaryObject({
        NameObject('/Type'):     NameObject('/Font'),
        NameObject('/Subtype'):  NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject(f'/{font_name}'),
    })
    xobj[NameObject('/Resources')] = DictionaryObject({
        NameObject('/Font'): DictionaryObject({
            NameObject(f'/{font_name}'): writer._add_object(font_dict)
        })
    })
    ap_ref  = writer._add_object(xobj)
    ap_dict = DictionaryObject({NameObject('/N'): ap_ref})
    ann_obj[NameObject('/AP')] = writer._add_object(ap_dict)


def _make_image_xobject(writer, img_bytes):
    """Embed raw image bytes as a PDF image XObject (handling alpha via SMask).
    Returns (img_ref, width, height) or (None, 0, 0) on failure.  Mirrors the
    editor's image-embed logic so viewer image stamps match its output."""
    import io as _io, zlib as _zlib
    from PIL import Image as _PILImage
    from pypdf.generic import (NameObject, NumberObject,
                               DecodedStreamObject, EncodedStreamObject)
    try:
        src  = _PILImage.open(_io.BytesIO(img_bytes))
        mode = src.mode
        has_alpha = mode in ('RGBA', 'LA') or (mode == 'P' and 'transparency' in src.info)
        if has_alpha:
            rgba = _PILImage.open(_io.BytesIO(img_bytes)).convert('RGBA')
            rgb  = rgba.convert('RGB')
            a    = rgba.split()[3]
            smask = EncodedStreamObject()
            smask._data = _zlib.compress(a.tobytes(), 6)
            smask.update({
                NameObject('/Type'):            NameObject('/XObject'),
                NameObject('/Subtype'):         NameObject('/Image'),
                NameObject('/Width'):           NumberObject(rgba.width),
                NameObject('/Height'):          NumberObject(rgba.height),
                NameObject('/ColorSpace'):      NameObject('/DeviceGray'),
                NameObject('/BitsPerComponent'): NumberObject(8),
                NameObject('/Filter'):          NameObject('/FlateDecode'),
            })
            img = EncodedStreamObject()
            img._data = _zlib.compress(rgb.tobytes(), 6)
            img.update({
                NameObject('/Type'):            NameObject('/XObject'),
                NameObject('/Subtype'):         NameObject('/Image'),
                NameObject('/Width'):           NumberObject(rgba.width),
                NameObject('/Height'):          NumberObject(rgba.height),
                NameObject('/ColorSpace'):      NameObject('/DeviceRGB'),
                NameObject('/BitsPerComponent'): NumberObject(8),
                NameObject('/Filter'):          NameObject('/FlateDecode'),
                NameObject('/SMask'):           writer._add_object(smask),
            })
            return writer._add_object(img), rgba.width, rgba.height
        rgb = _PILImage.open(_io.BytesIO(img_bytes)).convert('RGB')
        buf = _io.BytesIO(); rgb.save(buf, format='JPEG', quality=92)
        img = DecodedStreamObject()
        img._data = buf.getvalue()
        img.update({
            NameObject('/Type'):            NameObject('/XObject'),
            NameObject('/Subtype'):         NameObject('/Image'),
            NameObject('/Width'):           NumberObject(rgb.width),
            NameObject('/Height'):          NumberObject(rgb.height),
            NameObject('/ColorSpace'):      NameObject('/DeviceRGB'),
            NameObject('/BitsPerComponent'): NumberObject(8),
            NameObject('/Filter'):          NameObject('/DCTDecode'),
        })
        return writer._add_object(img), rgb.width, rgb.height
    except Exception as e:
        logger.warning(f"[IMG] embed failed: {e}")
        return None, 0, 0


def _stamp_image_data_url(ann):
    """Extract a /Stamp annotation's embedded image back to a data URL so it can
    be lifted for re-editing.  Returns 'data:image/...;base64,...' or None."""
    import io as _io
    try:
        ap = ann.get('/AP')
        if hasattr(ap, 'get_object'): ap = ap.get_object()
        n = ap.get('/N') if ap is not None else None
        if hasattr(n, 'get_object'): n = n.get_object()
        res = n.get('/Resources') if n is not None else None
        if hasattr(res, 'get_object'): res = res.get_object()
        xo = res.get('/XObject') if res is not None else None
        if hasattr(xo, 'get_object'): xo = xo.get_object()
        if not xo:
            return None
        img = None
        for k in list(xo.keys()):
            cand = xo[k]
            if hasattr(cand, 'get_object'): cand = cand.get_object()
            if str(cand.get('/Subtype')) == '/Image':
                img = cand; break
        if img is None:
            return None
        filt = img.get('/Filter')
        if hasattr(filt, 'get_object'): filt = filt.get_object()
        filt = str(filt)
        w, h = int(img.get('/Width')), int(img.get('/Height'))
        if 'DCTDecode' in filt:
            return ('data:image/jpeg;base64,'
                    + base64.b64encode(img.get_data()).decode('ascii'))
        # FlateDecode RGB (+ optional grayscale SMask alpha) → rebuild a PNG.
        from PIL import Image as _PILImage
        pil = _PILImage.frombytes('RGB', (w, h), img.get_data())
        sm = img.get('/SMask')
        if sm is not None:
            if hasattr(sm, 'get_object'): sm = sm.get_object()
            a = _PILImage.frombytes('L', (int(sm.get('/Width')), int(sm.get('/Height'))),
                                    sm.get_data())
            pil = pil.convert('RGBA'); pil.putalpha(a)
        buf = _io.BytesIO(); pil.save(buf, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception as e:
        logger.warning(f"[IMG] stamp extract failed: {e}")
        return None


def _cloud_path_ops(bx1, by1, bx2, by2, arc):
    """Return content-stream ops drawing a revision-cloud (bumpy) closed path
    around the rectangle (bx1,by1)-(bx2,by2).  Bumps bulge outward.

    Mirrors the editor's cloud algorithm (gentle cubic-Bézier bumps).
    """
    import math
    cx_mid, cy_mid = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0

    segs = []                     # ('h'|'v', sx, sy, ex, ey) around the perimeter
    x = bx1
    while x < bx2:
        segs.append(('h', x, by2, min(x + arc * 2, bx2), by2)); x += arc * 2
    y = by2
    while y > by1:
        segs.append(('v', bx2, y, bx2, max(y - arc * 2, by1))); y -= arc * 2
    x = bx2
    while x > bx1:
        segs.append(('h', x, by1, max(x - arc * 2, bx1), by1)); x -= arc * 2
    y = by1
    while y < by2:
        segs.append(('v', bx1, y, bx1, min(y + arc * 2, by2))); y += arc * 2

    if not segs:
        return [f'{bx1:.3f} {by1:.3f} {bx2-bx1:.3f} {by2-by1:.3f} re']

    out = [f'{segs[0][1]:.3f} {segs[0][2]:.3f} m']
    for kind, sx, sy, ex, ey in segs:
        mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
        r = math.hypot(ex - sx, ey - sy) / 2.0
        if r < 0.5:
            out.append(f'{ex:.3f} {ey:.3f} l'); continue
        if kind == 'h':
            nx, ny = 0.0, (1.0 if my > cy_mid else -1.0)
        else:
            nx, ny = (1.0 if mx > cx_mid else -1.0), 0.0
        dl = math.hypot(ex - sx, ey - sy) or 1.0
        tx, ty = (ex - sx) / dl, (ey - sy) / dl
        bulge, tang = r * 0.75, r * 0.5
        cp1x, cp1y = sx + tx * tang + nx * bulge, sy + ty * tang + ny * bulge
        cp2x, cp2y = ex - tx * tang + nx * bulge, ey - ty * tang + ny * bulge
        out.append(f'{cp1x:.3f} {cp1y:.3f} {cp2x:.3f} {cp2y:.3f} {ex:.3f} {ey:.3f} c')
    out.append('h')
    return out


def _generate_highlight_ap(ann_obj, rect, color, writer):
    """Build a /Highlight appearance: translucent (Multiply) colour fill so the
    underlying text shows through, like a real highlighter."""
    from pypdf.generic import (DictionaryObject, NameObject, ArrayObject,
                               FloatObject, DecodedStreamObject)
    rx1, ry1, rx2, ry2 = rect
    W, H = rx2 - rx1, ry2 - ry1
    c = color or [1.0, 0.92, 0.0]
    stream = (f'q /GSmul gs {c[0]:.4f} {c[1]:.4f} {c[2]:.4f} rg '
              f'0 0 {W:.3f} {H:.3f} re f Q').encode('latin-1', 'replace')
    xobj = DecodedStreamObject(); xobj.set_data(stream)
    xobj[NameObject('/Type')]    = NameObject('/XObject')
    xobj[NameObject('/Subtype')] = NameObject('/Form')
    xobj[NameObject('/BBox')]    = ArrayObject([FloatObject(0), FloatObject(0),
                                                FloatObject(W), FloatObject(H)])
    xobj[NameObject('/Matrix')]  = ArrayObject([FloatObject(1), FloatObject(0),
        FloatObject(0), FloatObject(1), FloatObject(rx1), FloatObject(ry1)])
    gs = DictionaryObject({NameObject('/Type'): NameObject('/ExtGState'),
                           NameObject('/BM'): NameObject('/Multiply')})
    xobj[NameObject('/Resources')] = DictionaryObject({
        NameObject('/ExtGState'): DictionaryObject({
            NameObject('/GSmul'): writer._add_object(gs)})})
    ap_ref = writer._add_object(xobj)
    ann_obj[NameObject('/AP')] = writer._add_object(
        DictionaryObject({NameObject('/N'): ap_ref}))


def _generate_shape_ap(ann_obj, subtype, geom, style, writer):
    """Build and attach an /AP /N appearance for a shape annotation.

    subtype : 'Square' | 'Circle' | 'Polygon' (revision cloud) | 'Line'
    geom    : for Square/Circle/Polygon → /Rect-style [x1,y1,x2,y2] (page coords);
              for Line                  → [x1,y1,x2,y2] endpoints (page coords).
    style   : {'border_color':[r,g,b]|None, 'fill_color':[r,g,b]|None,
               'border_width':float, 'arrow':bool (head at the 2nd endpoint),
               'arrow_start':bool (head at the 1st endpoint — set with 'arrow'
               for a double-ended arrow),
               'dashes':[on,off]|None (dash pattern in points; None = solid)}
    Returns (bbox_rect) — the /Rect to assign to the annotation.
    """
    from pypdf.generic import (DictionaryObject, NameObject, ArrayObject,
                               FloatObject, DecodedStreamObject)
    import math

    bw_pt  = max(0.0, float(style.get('border_width', 1.0) or 0.0))
    bc     = style.get('border_color')
    fc     = style.get('fill_color')
    dashes = style.get('dashes') or None

    def _rg(c, stroke):           # colour-setting operator
        op = 'RG' if stroke else 'rg'
        return f'{c[0]:.4f} {c[1]:.4f} {c[2]:.4f} {op}'

    def _dash_op():               # dash-pattern operator (phase 0)
        return '[' + ' '.join(f'{v:.3f}' for v in dashes) + '] 0 d'

    ops = ['q']

    if subtype in ('Square', 'Circle', 'Polygon'):
        x1, y1, x2, y2 = [float(v) for v in geom]
        rx1, ry1 = min(x1, x2), min(y1, y2)
        rx2, ry2 = max(x1, x2), max(y1, y2)
        # Revision clouds bulge outward — pad by an arc radius so bumps aren't clipped.
        arc  = max(6.0, min(16.0, min(rx2 - rx1, ry2 - ry1) / 4.0)) if rx2 > rx1 and ry2 > ry1 else 10.0
        pad  = bw_pt / 2.0 + 1.0 + (arc if subtype == 'Polygon' else 0.0)
        rect = [rx1 - pad, ry1 - pad, rx2 + pad, ry2 + pad]
        W, H = rect[2] - rect[0], rect[3] - rect[1]
        # Inner geometry in BBox space (origin at 0,0), inset by pad.
        ix1, iy1 = pad, pad
        ix2, iy2 = W - pad, H - pad

        if bw_pt > 0 and bc:
            ops.append(_rg(bc, True)); ops.append(f'{bw_pt:.3f} w')
            if dashes:
                ops.append(_dash_op())
        if fc:
            ops.append(_rg(fc, False))
        paint = 'B' if (fc and bw_pt > 0 and bc) else ('f' if fc else 'S')

        if subtype == 'Square':
            ops.append(f'{ix1:.3f} {iy1:.3f} {ix2-ix1:.3f} {iy2-iy1:.3f} re')
            ops.append(paint)
        elif subtype == 'Polygon':       # revision cloud — bumpy perimeter, stroke only
            ops += _cloud_path_ops(ix1, iy1, ix2, iy2, arc)
            ops.append('S')
        else:  # Circle → ellipse via 4 Bézier arcs
            cx, cy = (ix1 + ix2) / 2.0, (iy1 + iy2) / 2.0
            erx, ery = (ix2 - ix1) / 2.0, (iy2 - iy1) / 2.0
            k = 0.5522847498
            ox, oy = erx * k, ery * k
            ops.append(f'{cx+erx:.3f} {cy:.3f} m')
            ops.append(f'{cx+erx:.3f} {cy+oy:.3f} {cx+ox:.3f} {cy+ery:.3f} {cx:.3f} {cy+ery:.3f} c')
            ops.append(f'{cx-ox:.3f} {cy+ery:.3f} {cx-erx:.3f} {cy+oy:.3f} {cx-erx:.3f} {cy:.3f} c')
            ops.append(f'{cx-erx:.3f} {cy-oy:.3f} {cx-ox:.3f} {cy-ery:.3f} {cx:.3f} {cy-ery:.3f} c')
            ops.append(f'{cx+ox:.3f} {cy-ery:.3f} {cx+erx:.3f} {cy-oy:.3f} {cx+erx:.3f} {cy:.3f} c')
            ops.append(paint)

    else:  # Line
        x1, y1, x2, y2 = [float(v) for v in geom]
        ah = max(8.0, bw_pt * 3.5)        # arrowhead size
        minx, miny = min(x1, x2), min(y1, y2)
        maxx, maxy = max(x1, x2), max(y1, y2)
        pad = bw_pt / 2.0 + ah + 1.0
        rect = [minx - pad, miny - pad, maxx + pad, maxy + pad]
        ox_, oy_ = rect[0], rect[1]       # BBox origin offset
        ax1, ay1 = x1 - ox_, y1 - oy_
        ax2, ay2 = x2 - ox_, y2 - oy_

        if bw_pt > 0 and bc:
            ops.append(_rg(bc, True)); ops.append(f'{bw_pt:.3f} w')
        else:
            ops.append('0 0 0 RG'); ops.append('1 w')
        if dashes:
            ops.append(_dash_op())
        ops.append(f'{ax1:.3f} {ay1:.3f} m {ax2:.3f} {ay2:.3f} l S')
        if dashes:
            ops.append('[] 0 d')          # arrowhead stays solid

        def _arrow_head(tipx, tipy, fromx, fromy):
            """Two barbs opening back from (tip) towards (from)."""
            ang = math.atan2(tipy - fromy, tipx - fromx)
            spread = math.radians(28)
            for sgn in (+1, -1):
                hx = tipx - ah * math.cos(ang + sgn * spread)
                hy = tipy - ah * math.sin(ang + sgn * spread)
                ops.append(f'{tipx:.3f} {tipy:.3f} m {hx:.3f} {hy:.3f} l S')

        if style.get('arrow'):
            _arrow_head(ax2, ay2, ax1, ay1)
        if style.get('arrow_start'):        # double-ended arrow
            _arrow_head(ax1, ay1, ax2, ay2)

    ops.append('Q')
    stream_bytes = '\n'.join(ops).encode('latin-1', errors='replace')

    W, H = rect[2] - rect[0], rect[3] - rect[1]
    xobj = DecodedStreamObject()
    xobj.set_data(stream_bytes)
    xobj[NameObject('/Type')]    = NameObject('/XObject')
    xobj[NameObject('/Subtype')] = NameObject('/Form')
    xobj[NameObject('/BBox')]    = ArrayObject([
        FloatObject(0.0), FloatObject(0.0), FloatObject(W), FloatObject(H)])
    # Map BBox(0..W,0..H) onto the page-space /Rect via /Matrix translation.
    xobj[NameObject('/Matrix')]  = ArrayObject([
        FloatObject(1), FloatObject(0), FloatObject(0), FloatObject(1),
        FloatObject(rect[0]), FloatObject(rect[1])])
    ap_ref = writer._add_object(xobj)
    ann_obj[NameObject('/AP')] = writer._add_object(
        DictionaryObject({NameObject('/N'): ap_ref}))
    return rect


def _get_page_chars(pdf_bytes):
    """Return per-character text with bounding boxes for text selection.
    Coordinates are in PDF user-space (y=0 at bottom)."""
    _t('chars: entry')
    import pypdfium2 as pdfium
    with _NATIVE_LOCK:
        _t('chars: creating PdfDocument')
        doc = pdfium.PdfDocument(pdf_bytes)
        _t('chars: PdfDocument created')
        try:
            _t('chars: getting page 0')
            page      = doc[0]
            _t('chars: getting textpage')
            textpage  = page.get_textpage()
            n_chars   = textpage.count_chars()
            _t(f'chars: {n_chars} chars found')
            full_text = textpage.get_text_range() if n_chars > 0 else ''
            # Tight per-glyph boxes with pdfium's giant-em-square / zero-size
            # bboxes repaired from the pen origin.  Without this, fonts like the
            # Type3 font in docs/edit_problem.pdf report every glyph as a huge box
            # blanketing the page, so an area-select marquee would "hit" nearly
            # every character (see pdf_core.corrected_char_boxes).
            cboxes = _pdf_core.corrected_char_boxes(textpage)
            chars = []
            for ci in range(n_chars):
                try:
                    x1, y1, x2, y2, _eff = cboxes[ci]
                    if x2 > x1 and y2 > y1 and not (x1 == 0 and y1 == 0):
                        c = full_text[ci] if ci < len(full_text) else ''
                        chars.append({'c': c, 'x': x1, 'y': y1, 'w': x2 - x1, 'h': y2 - y1})
                except Exception:
                    continue
            _t(f'chars: done, returning {len(chars)} chars')
            return chars
        finally:
            _t('chars: closing doc')
            doc.close()
            _t('chars: doc closed')


# ── Auto-bookmark detection ───────────────────────────────────────────────────
# Two fallbacks for documents that ship without an embedded /Outlines tree (which
# is already imported on load by pdf_core._read_outline_bookmarks):
#   1. parse a printed "Contents / Index" page, then
#   2. failing that, detect headings by font size.
# Both return *proposed* bookmarks; nothing is committed until the user confirms.

def _page_text_lines(pdf_bytes):
    """Group a page's native characters into text lines.

    Returns a list of {text, x, y_top, height} in PDF user space (y=0 at the
    bottom), in reading order.  `height` is the median glyph height on the line —
    a good proxy for font size.  Returns [] for image-only (scanned) pages.
    """
    import pypdfium2 as pdfium
    with _NATIVE_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            page = doc[0]
            tp   = page.get_textpage()
            n    = tp.count_chars()
            if n == 0:
                return []
            full = tp.get_text_range()
            # Group chars into lines by vertical box OVERLAP (robust to the mixed
            # glyph heights — caps, dot leaders, sub-baseline hyphen runs — that a
            # plain baseline test wrongly splits).  Chars are processed in reading
            # order so text (and its spaces) is preserved without re-sorting.
            lines, cur, lo, hi = [], None, None, None
            for i in range(n):
                c = full[i] if i < len(full) else ''
                if c in ('\r', '\n'):
                    if cur:
                        lines.append(cur); cur = None
                    continue
                try:
                    x1, y1, x2, y2 = tp.get_charbox(i)
                except Exception:
                    x1 = y1 = x2 = y2 = 0.0
                rec = {'c': c, 'x1': x1, 'x2': x2, 'y1': y1, 'y2': y2,
                       'h': (y2 - y1) if y2 > y1 else 0.0}
                if cur is None:
                    cur, lo, hi = [rec], y1, y2
                    continue
                overlap = min(hi, y2) - max(lo, y1)
                small   = min(hi - lo, y2 - y1) or 1.0
                if y2 > y1 and overlap > 0.3 * small:
                    cur.append(rec); lo, hi = min(lo, y1), max(hi, y2)
                elif y2 <= y1:           # degenerate box (e.g. space) — keep on line
                    cur.append(rec)
                else:
                    lines.append(cur); cur, lo, hi = [rec], y1, y2
            if cur:
                lines.append(cur)

            out = []
            for ln in lines:
                text = ''.join(r['c'] for r in ln).strip()
                if not text:
                    continue
                glyphs = [r for r in ln if r['h'] > 0 and not r['c'].isspace()]
                if not glyphs:
                    continue
                xs    = [r['x1'] for r in glyphs]
                tops  = [r['y2'] for r in glyphs]
                hs    = sorted(r['h'] for r in glyphs)
                med_h = hs[len(hs) // 2]
                out.append({'text': text, 'x': min(xs),
                            'y_top': max(tops), 'height': med_h})
            return out
        finally:
            doc.close()


def _toc_numbering_depth(title):
    """Depth from a leading numbering prefix like '1.2.3' → 2, else None."""
    import re
    m = re.match(r'^(\d+(?:\.\d+)+)', title)
    if m:
        return m.group(1).count('.')
    if re.match(r'^\d+[.\s]', title):
        return 0
    return None


def _looks_like_toc(lines):
    """Heuristic: does this page look like a printed table of contents?"""
    import re
    leader = re.compile(r'.+?[.…\s]{2,}\d{1,4}\s*$')
    head   = ' '.join(l['text'] for l in lines[:6]).lower()
    titled = ('table of contents' in head or 'contents' in head
              or head.strip().startswith('index'))
    leaders = sum(1 for l in lines if leader.match(l['text']))
    return (titled and leaders >= 2) or leaders >= 5


def _parse_toc_lines(lines):
    """Extract {title, printed, x, depth} entries from a contents page.

    Handles entries that wrapped onto two lines (a leading numbered fragment
    with no page number, followed by the remainder ending in the page number)
    by carrying the first fragment onto the next matched entry.
    """
    import re
    leader   = re.compile(r'^(.*?\S)[.…\s]{2,}(\d{1,4})\s*$')
    trailing = re.compile(r'^(.*\S)\s+(\d{1,4})\s*$')
    skip     = re.compile(r'^(?:table\s+of\s+)?contents$|^index$', re.I)
    numpref  = re.compile(r'^\d+(?:\.\d+)*[.\s]')
    entries  = []
    carry    = ''
    for l in lines:
        t = l['text'].strip()
        if not t:
            continue
        m = leader.match(t) or trailing.match(t)
        if not m:
            # A numbered line with no page number is likely the first visual
            # line of a wrapped entry — hold it to prepend to the next entry.
            carry = t if (numpref.match(t) and not carry) else ''
            continue
        title = m.group(1).strip(' .…\t')
        try:
            printed = int(m.group(2))
        except ValueError:
            carry = ''
            continue
        if carry and not numpref.match(title):
            title = (carry + ' ' + title).strip()
        carry = ''
        if len(title) < 2 or skip.match(title):
            continue
        entries.append({'title': title, 'printed': printed,
                        'x': l['x'], 'depth': _toc_numbering_depth(title)})
    return entries


def _assign_levels(entries):
    """Set 'level' on each entry from numbering depth, else x-indent buckets."""
    if any(e['depth'] is not None for e in entries):
        for e in entries:
            e['level'] = min(3, e['depth'] if e['depth'] is not None else 0)
        return
    xs = sorted({round(e['x'] / 6.0) * 6 for e in entries})   # ~6pt buckets
    rank = {x: min(3, i) for i, x in enumerate(xs)}
    for e in entries:
        e['level'] = rank[round(e['x'] / 6.0) * 6]


def _norm(s):
    import re
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _strip_numbering(title):
    """Drop a leading '1.2.3' / '1. ' style numbering prefix from a title."""
    import re
    return re.sub(r'^\d+(?:\.\d+)*\.?\s*', '', title)


def _cached_lines(pages, idx, cache):
    """_page_text_lines(idx) with a per-detection cache (pages get re-read a lot
    during offset anchoring and per-entry refinement)."""
    lines = cache.get(idx)
    if lines is None:
        lines = cache[idx] = _page_text_lines(pages[idx]['pdf_bytes'])
    return lines


def _toc_page_offset(pages, toc_idx, entries, cache):
    """Estimate the printed-page → physical-index offset by anchoring on titles.

    Matches several early entry titles to the page that actually starts with
    them; the most common offset wins.  This is only a starting guess —
    _refine_entry_page snaps each bookmark to the exact page afterwards.  Falls
    back to assuming content starts right after the contents page.
    """
    from collections import Counter
    n = len(pages)
    window_end = min(n, toc_idx + 1 + 80)
    anchors = []
    for e in entries[:10]:
        target = _norm(_strip_numbering(e['title']))[:40]
        if len(target) < 4:
            continue
        for pidx in range(toc_idx + 1, window_end):
            lines = _cached_lines(pages, pidx, cache)
            if _looks_like_toc(lines):          # a 2nd contents page is a magnet
                continue
            if any(_norm(l['text']).startswith(target) for l in lines):
                anchors.append(pidx - e['printed'])
                break
    if anchors:
        return Counter(anchors).most_common(1)[0][0]
    printed_min = min((e['printed'] for e in entries), default=1)
    return (toc_idx + 1) - printed_min


def _refine_entry_page(pages, guess_idx, title, cache, toc_idx, radius=6):
    """Snap an entry to the nearby page that actually begins with its heading.

    Searches ±radius pages around the offset-based guess for a line that *starts
    with* the heading text (a contents page or body mention merely *containing*
    it doesn't count), skipping any contents page, and prefers the page where the
    heading is in its largest font — which beats a running header that repeats
    the title on every page of a section.  Returns (physical_idx, y_top) so the
    bookmark lands on the heading; falls back to the offset guess.
    """
    n = len(pages)
    target  = _norm(title)
    target2 = _norm(_strip_numbering(title))
    if len(target) < 3:
        return guess_idx, None
    lo = max(toc_idx + 1, guess_idx - radius)
    hi = min(n, guess_idx + radius + 1)
    cands = []   # (height, -distance_to_guess, pidx, y_top)
    for pidx in range(lo, hi):
        lines = _cached_lines(pages, pidx, cache)
        if _looks_like_toc(lines):
            continue
        for l in lines:
            ln = _norm(l['text'])
            if not ln:
                continue
            if ((len(target) >= 4 and ln.startswith(target)) or
                    (len(target2) >= 4 and ln.startswith(target2))):
                cands.append((l['height'], -abs(pidx - guess_idx), pidx, l['y_top']))
                break
    if not cands:
        return guess_idx, None
    cands.sort(reverse=True)               # biggest font, then closest to guess
    _, _, pidx, y_top = cands[0]
    return pidx, y_top


def _detect_toc_bookmarks(session):
    """Tier 2 — parse a printed contents page into proposed bookmarks."""
    pages = session['pages']
    n = len(pages)
    scan_to = min(n, max(3, n // 8 + 2))      # only early pages can be the TOC
    cache = {}
    for toc_idx in range(scan_to):
        lines = _cached_lines(pages, toc_idx, cache)
        if not lines or not _looks_like_toc(lines):
            continue
        entries = _parse_toc_lines(lines)
        if len(entries) < 3:
            continue
        _assign_levels(entries)
        offset = _toc_page_offset(pages, toc_idx, entries, cache)
        out = []
        for e in entries:
            guess = min(max(0, e['printed'] + offset), n - 1)
            phys, y = _refine_entry_page(pages, guess, e['title'], cache, toc_idx)
            if 0 <= phys < n:
                out.append({'title': e['title'][:160], 'page_id': pages[phys]['id'],
                            'page_idx': phys, 'y': y, 'level': e['level']})
        if out:
            return out
    return []


def _detect_heading_bookmarks(session, cap=150):
    """Tier 3 — detect headings by font size across the whole document."""
    pages = session['pages']
    per_page, all_h = [], []
    for rec in pages:
        lines = _page_text_lines(rec['pdf_bytes'])
        per_page.append(lines)
        all_h.extend(l['height'] for l in lines if l['height'] > 0)
    if not all_h:
        return []
    all_h.sort()
    body = all_h[len(all_h) // 2]            # median glyph height = body text
    cand = []
    for idx, (rec, lines) in enumerate(zip(pages, per_page)):
        for l in lines:
            t = l['text'].strip()
            if not t or l['height'] <= body * 1.35:
                continue
            # Title-shape filters: real headings aren't bulleted list items or
            # full sentences (which end in sentence punctuation / start lowercase).
            if t[0] in '•◦‣·*–—-':
                continue
            if t[-1] in '.,;:':
                continue
            first = next((ch for ch in t if ch.isalpha()), '')
            if first and first.islower():
                continue
            if len(t.split()) > 12 or t.replace('.', '').replace(' ', '').isdigit():
                continue
            cand.append({'title': t[:160], 'page_id': rec['id'], 'page_idx': idx,
                         'y': l['y_top'], 'h': round(l['height'], 1)})
    if not cand:
        return []
    tiers = sorted({c['h'] for c in cand}, reverse=True)[:3]
    def level_for(h):
        for i, s in enumerate(tiers):
            if h >= s - 0.2:
                return i
        return len(tiers) - 1
    for c in cand:
        c['level'] = level_for(c['h'])
        c.pop('h', None)
    return cand[:cap]


def _auto_detect_bookmarks(session):
    """Run the fallbacks in order. Returns (proposals, source)."""
    toc = _detect_toc_bookmarks(session)
    if toc:
        return toc, 'toc'
    headings = _detect_heading_bookmarks(session)
    if headings:
        return headings, 'headings'
    return [], 'none'


def _is_scanned_page(pdf_bytes):
    """Return True if the page has too few native characters to be searchable."""
    _t('is_scanned: entry')
    import pypdfium2 as pdfium
    with _NATIVE_LOCK:
        _t('is_scanned: creating PdfDocument')
        doc = pdfium.PdfDocument(pdf_bytes)
        _t('is_scanned: PdfDocument created')
        try:
            page     = doc[0]
            textpage = page.get_textpage()
            n_chars  = textpage.count_chars()
            _t(f'is_scanned: {n_chars} chars')
            if n_chars < _SCANNED_CHAR_THRESHOLD:
                return True
            text   = textpage.get_text_range()
            non_ws = sum(1 for c in text if not c.isspace())
            result = non_ws < _SCANNED_CHAR_THRESHOLD
            _t(f'is_scanned: done, result={result}')
            return result
        finally:
            _t('is_scanned: closing doc')
            doc.close()
            _t('is_scanned: doc closed')


def _get_ocr_words_cached(session, page_id, pdf_bytes, progress_cb=None):
    """Return OCR word boxes for *page_id*, running OCR and caching on first call.
    *progress_cb* drives the loading overlay only on the (slow) first call; a
    cache hit returns instantly and reports nothing."""
    cache = session.setdefault('_ocr_cache', {})
    if page_id not in cache:
        cache[page_id] = _ocr_utils.ocr_page_words(pdf_bytes, progress_cb=progress_cb)
    return cache[page_id]


def _page_dims(pdf_bytes):
    """Return (width, height) of page 0 in PDF points (rounded), or None."""
    import pypdfium2 as pdfium
    try:
        with _NATIVE_LOCK:
            doc = pdfium.PdfDocument(pdf_bytes)
            try:
                page = doc[0]
                return (round(page.get_width(), 1), round(page.get_height(), 1))
            finally:
                doc.close()
    except Exception:
        return None


def _page_rotation_and_raw_dims(pdf_bytes):
    """Return (rotate, raw_W, raw_H) for page 0.

    rotate is the page /Rotate in {0,90,180,270}.  raw_W/raw_H are the
    *unrotated* content-space dimensions — the frame in which pdfium's
    get_charbox() and ocr_utils' word boxes live.  pdfium reports get_width/
    get_height() in the *display* (post-rotation) frame, so for 90/270 the raw
    dims are the display dims swapped.  Returns (0, None, None) on failure.
    """
    import pypdfium2 as pdfium
    try:
        with _NATIVE_LOCK:
            doc = pdfium.PdfDocument(pdf_bytes)
            try:
                page   = doc[0]
                rot    = int(page.get_rotation()) % 360
                dw, dh = page.get_width(), page.get_height()   # display dims
                if rot in (90, 270):
                    return rot, dh, dw
                return rot, dw, dh
            finally:
                doc.close()
    except Exception:
        return 0, None, None


def _page_crop_origin(pdf_bytes):
    """Return (off_x, off_y) — the page's CropBox lower-left corner in PDF user
    space, i.e. how far the VISIBLE page is displaced from the coordinate origin.

    Everything the viewer draws is measured from the top-left of the rendered
    raster, and pdfium renders the CropBox — but every coordinate the PDF itself
    carries (an annotation /Rect, a pdfium charbox, an OCR word box) is in
    absolute user space.  On the overwhelming majority of pages CropBox ==
    MediaBox and this is (0, 0), which is why the two frames were conflated for
    so long.  On a cropped page the difference is exactly this offset, and
    leaving it out displaces everything the viewer reads or writes — see
    tests/test_crop_coords.py.

    Returns (0.0, 0.0) on failure, which is the historical behaviour.
    """
    try:
        cb = _pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].cropbox
        return float(cb.left), float(cb.bottom)
    except Exception:
        return 0.0, 0.0


def _page_display_frame(pdf_bytes):
    """Return (rotate, raw_W, raw_H, off_x, off_y) — everything needed to move a
    coordinate between absolute PDF user space and the viewer's display frame.

    This is _page_rotation_and_raw_dims plus the CropBox origin; prefer it at any
    site that converts geometry, so the offset cannot be forgotten again.
    """
    rotate, raw_W, raw_H = _page_rotation_and_raw_dims(pdf_bytes)
    off_x, off_y = _page_crop_origin(pdf_bytes)
    return rotate, raw_W, raw_H, off_x, off_y


def _raw_rect_to_display(rect, rotate, raw_W, raw_H, off_x=0.0, off_y=0.0):
    """Map a rect dict (keys x,y,w,h; bottom-left, y-up) from raw content space
    to the displayed (post-/Rotate) frame that pdfium renders and the viewer
    measures the page in.  Other keys (e.g. 'c') are preserved.

    The viewer's pdfToDisplay applies only a y-flip + scale (no rotation), so
    char/word/search boxes — which pdfium and OCR produce in the raw frame —
    must be rotated here or they land in the wrong place on rotated pages.

    Two steps, in this order:
      1. translate absolute user space → CropBox-relative (off_x/off_y from
         _page_crop_origin).  This is what makes the rotation step below legal:
         it works within an extent of raw_W x raw_H anchored at (0, 0), which is
         only the page's real frame once the CropBox origin has been removed.
      2. rotate.

    Identity for an unrotated page whose CropBox starts at the origin — i.e.
    almost every page, which is why omitting step 1 went unnoticed.
    """
    if off_x or off_y:
        rect = dict(rect)
        rect['x'] = rect['x'] - off_x
        rect['y'] = rect['y'] - off_y
    if not rotate or raw_W is None:
        return rect
    x, y, w, h = rect['x'], rect['y'], rect['w'], rect['h']
    if rotate == 90:
        nx, ny, nw, nh = y, raw_W - (x + w), h, w
    elif rotate == 180:
        nx, ny, nw, nh = raw_W - (x + w), raw_H - (y + h), w, h
    elif rotate == 270:
        nx, ny, nw, nh = raw_H - (y + h), x, h, w
    else:
        return rect
    out = dict(rect)
    out['x'], out['y'], out['w'], out['h'] = nx, ny, nw, nh
    return out


def _display_rect_to_raw(rect, rotate, raw_W, raw_H, off_x=0.0, off_y=0.0):
    """Inverse of _raw_rect_to_display: map a rect dict (x,y,w,h; bottom-left,
    y-up) from the displayed (post-/Rotate) frame the viewer draws in back to raw
    content space.  Used when writing user-drawn annotations: the frontend works
    purely in the display frame, but a /Rect is interpreted in raw user space and
    re-rotated by /Rotate at render time, so it must be un-rotated here.

    The two steps of _raw_rect_to_display, inverted and in the opposite order:
      1. un-rotate, via the forward transform with the inverse rotation, run in
         the display frame (whose dims are the raw dims swapped for 90/270).
         No offset is passed to that call — it must not translate twice.
      2. translate CropBox-relative → absolute user space.

    Identity for an unrotated page whose CropBox starts at the origin.
    """
    if rotate and raw_W is not None:
        inv = (360 - rotate) % 360
        if rotate in (90, 270):
            rect = _raw_rect_to_display(rect, inv, raw_H, raw_W)
        else:
            rect = _raw_rect_to_display(rect, inv, raw_W, raw_H)
    if off_x or off_y:
        rect = dict(rect)
        rect['x'] = rect['x'] + off_x
        rect['y'] = rect['y'] + off_y
    return rect


def _xform_pdf_rect(rv, fn, rotate, raw_W, raw_H, off_x=0.0, off_y=0.0):
    """Apply a rect-dict transform (fn = _raw_rect_to_display or
    _display_rect_to_raw) to a PDF /Rect-style array [x0,y0,x1,y1] (y-up),
    returning a new [x0,y0,x1,y1].  Identity for an unrotated page with no
    CropBox offset, or for short arrays."""
    if (not rotate and not off_x and not off_y) or rv is None or len(rv) < 4:
        return rv
    if raw_W is None and rotate:
        return rv
    x0, y0, x1, y1 = float(rv[0]), float(rv[1]), float(rv[2]), float(rv[3])
    d = fn({'x': min(x0, x1), 'y': min(y0, y1),
            'w': abs(x1 - x0), 'h': abs(y1 - y0)}, rotate, raw_W, raw_H,
           off_x, off_y)
    return [d['x'], d['y'], d['x'] + d['w'], d['y'] + d['h']]


def _xform_pdf_points(pts, fn, rotate, raw_W, raw_H, off_x=0.0, off_y=0.0):
    """Apply a rect-dict transform to a flat list of points [x1,y1,x2,y2,...]
    (y-up), transforming each (x,y) independently so endpoint order/orientation
    is preserved (e.g. line direction, arrowhead end).  Identity for an unrotated
    page with no CropBox offset."""
    if (not rotate and not off_x and not off_y) or not pts:
        return pts
    if raw_W is None and rotate:
        return pts
    out = []
    for i in range(0, len(pts) - 1, 2):
        d = fn({'x': float(pts[i]), 'y': float(pts[i + 1]), 'w': 0.0, 'h': 0.0},
               rotate, raw_W, raw_H, off_x, off_y)
        out += [d['x'], d['y']]
    return out


def _display_page_size(rotate, raw_W, raw_H):
    """Display (post-/Rotate) page size from the raw dims.  Mirrors pdfium's
    rotation-aware get_width()/get_height()."""
    if raw_W is None or raw_H is None:
        return None, None
    if rotate in (90, 270):
        return raw_H, raw_W
    return raw_W, raw_H


def _field_rect_to_display(field, rotate, raw_W, raw_H, off_x, off_y):
    """Return a COPY of a form-field dict with its x1/y1/x2/y2 moved from
    absolute PDF user space into the viewer's display frame, and `page_h` set to
    the DISPLAY page height.

    form_utils stores widget geometry straight off the `/Rect` — absolute user
    space — alongside `page_h` taken from the MEDIABOX.  The viewer renders the
    CropBox and maps with `x1 * s` / `(page_h - y2) * s`, so on a cropped page a
    field was displaced by (CropBox.left, MediaBox.height - CropBox.top) and on a
    rotated page `page_h` was the wrong axis entirely.  On the 400x400 page
    cropped to (50,80)-(350,300) in tests/test_form_field_coords.py that put the
    widget at y=250 on a 220pt-tall page — off the page and unreachable.

    The stored dict is deliberately NOT mutated: it stays in absolute user
    space, which is what form_utils reads and writes (and what Save bakes).
    """
    out = dict(field)
    rv = _xform_pdf_rect(
        [field.get('x1', 0.0), field.get('y1', 0.0),
         field.get('x2', 0.0), field.get('y2', 0.0)],
        _raw_rect_to_display, rotate, raw_W, raw_H, off_x, off_y)
    out['x1'], out['y1'], out['x2'], out['y2'] = (
        min(rv[0], rv[2]), min(rv[1], rv[3]),
        max(rv[0], rv[2]), max(rv[1], rv[3]))
    _dw, dh = _display_page_size(rotate, raw_W, raw_H)
    if dh:
        out['page_h'] = dh
    return out


def _field_rect_to_raw(field, rotate, raw_W, raw_H, off_x, off_y):
    """Inverse of _field_rect_to_display: a COPY with x1/y1/x2/y2 moved from the
    viewer's display frame back into absolute PDF user space, which is what
    form_utils.add_field_to_page_pdf writes into the widget `/Rect`."""
    out = dict(field)
    rv = _xform_pdf_rect(
        [field.get('x1', 0.0), field.get('y1', 0.0),
         field.get('x2', 0.0), field.get('y2', 0.0)],
        _display_rect_to_raw, rotate, raw_W, raw_H, off_x, off_y)
    out['x1'], out['y1'], out['x2'], out['y2'] = (
        min(rv[0], rv[2]), min(rv[1], rv[3]),
        max(rv[0], rv[2]), max(rv[1], rv[3]))
    out.pop('page_h', None)      # display-frame value; never store it
    return out


def _drop_covered_words(words, covered_bboxes):
    """Drop OCR words that sit under an edited/redacted (covered) region, so a
    replaced original doesn't stay selectable/searchable.  Mirrors the editor's
    _filter_covered_text_items overlap test: fraction of the COVER box overlapped
    by the word, thresholded at 0.4 (editor GHOST_OVERLAP_THRESH)."""
    if not words or not covered_bboxes:
        return words
    THRESH = 0.4
    kept = []
    for w in words:
        wx1, wy1 = w['x'], w['y']
        wx2, wy2 = w['x'] + w['w'], w['y'] + w['h']
        covered = False
        for b in covered_bboxes:
            pad = 2.0
            bx1 = float(b.get('x', 0)) - pad
            by1 = float(b.get('y', 0)) - pad
            bx2 = float(b.get('x2', b.get('x', 0))) + pad
            by2 = float(b.get('y2', b.get('y', 0))) + pad
            bw  = max(bx2 - bx1, 1.0)
            bh  = max(by2 - by1, 1.0)
            ox  = max(0.0, min(wx2, bx2) - max(wx1, bx1))
            oy  = max(0.0, min(wy2, by2) - max(wy1, by1))
            if (ox * oy) / (bw * bh) >= THRESH:
                covered = True
                break
        if not covered:
            kept.append(w)
    return kept


def _drop_words_in_regions(words, regions):
    """Drop OCR words whose centre lies inside a row/area-edit footprint
    (page_rec['merge_regions']): the scanned words there were replaced by one
    native block, and must not stay selectable underneath it."""
    if not words or not regions:
        return words
    kept = []
    for w in words:
        cx, cy = w['x'] + w['w'] / 2.0, w['y'] + w['h'] / 2.0
        if not any(min(r[0], r[2]) <= cx <= max(r[0], r[2]) and
                   min(r[1], r[3]) <= cy <= max(r[1], r[3]) for r in regions):
            kept.append(w)
    return kept


def _drop_words_under_chars(words, chars, frac=0.5):
    """Drop OCR words that are mostly covered by native character boxes.

    On a MIXED page (a scan with native edits drawn into it) the raster shows
    the edits too, so OCR reads each edited word a second time and a selection
    would copy it twice.  The native chars are the better copy — exact text,
    exact boxes — so the OCR duplicate goes."""
    if not words or not chars:
        return words
    kept = []
    for w in words:
        wx1, wy1 = w['x'], w['y']
        wx2, wy2 = w['x'] + w['w'], w['y'] + w['h']
        area = max(w['w'] * w['h'], 1e-6)
        covered = 0.0
        for c in chars:
            ox = min(wx2, c['x'] + c['w']) - max(wx1, c['x'])
            if ox <= 0:
                continue
            oy = min(wy2, c['y'] + c['h']) - max(wy1, c['y'])
            if oy > 0:
                covered += ox * oy
        if covered / area < frac:
            kept.append(w)
    return kept


def _block_row_pitch(items, band):
    """Median distance between successive rows' tops among *items* (text runs
    in PDF y-up), or None for a single row.  Rows are clustered by top edge:
    a jump larger than *band* starts a new one."""
    tops = sorted((float(t['y2']) for t in items), reverse=True)
    rows = [[tops[0]]] if tops else []
    for y in tops[1:]:
        if rows[-1][-1] - y > band:
            rows.append([y])
        else:
            rows[-1].append(y)
    if len(rows) < 2:
        return None
    means = [sum(r) / len(r) for r in rows]
    gaps = sorted(means[i] - means[i + 1] for i in range(len(means) - 1))
    return gaps[len(gaps) // 2]


def _block_rows(items, band):
    """*items* grouped into rows by top edge (top of the page first)."""
    rows = []
    for t in sorted(items, key=lambda t: -float(t['y2'])):
        for r in rows:
            if abs(float(r[0]['y2']) - float(t['y2'])) <= band:
                r.append(t)
                break
        else:
            rows.append([t])
    return rows


def _block_alignment(items, band, em):
    """('left' | 'right' | 'center', anchor_x or None) for a row/area edit.

    Read from the rows the user replaced: whichever of their left edges, right
    edges or centres line up.  A table header like "Line Amount / Excl. VAT"
    shares its right edge; "Unit Price / Excl. VAT" its centre.  OCR boxes
    jitter by a point or two, so the tightest edge wins if it is within the
    tolerance — with a small bias to left, so a justified paragraph (both
    edges aligned) or ragged text stays left.  One row gives nothing to
    compare, so it stays left, anchored where its first word was.
    """
    rows = _block_rows(items, band)
    if len(rows) < 2:
        return 'left', None
    lefts   = [min(float(t['x'])  for t in r) for r in rows]
    rights  = [max(float(t['x2']) for t in r) for r in rows]
    centres = [(l + r) / 2.0 for l, r in zip(lefts, rights)]
    tol = max(2.0, em * 0.3)
    spread = lambda v: max(v) - min(v)
    cands = [(spread(lefts), 0.0, 'left', None),
             (spread(rights), 0.5, 'right', max(rights)),
             (spread(centres), 0.5, 'center', sum(centres) / len(centres))]
    raw, _bias, align, anchor = min(cands, key=lambda c: c[0] + c[1])
    if raw > tol:
        return 'left', None
    return align, anchor


_MIXED_CACHE = {}          # sha1(page bytes) -> bool
_MIXED_CACHE_MAX = 256


def _is_mixed_scan(pdf_bytes):
    """Cached editor.is_mixed_scan_page — the Pointer and text selection ask on
    every page they load, and the answer only changes when the bytes do."""
    import hashlib
    key = hashlib.sha1(pdf_bytes).digest()
    hit = _MIXED_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        from routes.editor import is_mixed_scan_page
        res = bool(is_mixed_scan_page(pdf_bytes))
    except Exception:
        res = False
    if len(_MIXED_CACHE) >= _MIXED_CACHE_MAX:
        _MIXED_CACHE.clear()
    _MIXED_CACHE[key] = res
    return res


def _ocr_source_words_for_mixed(session, page_rec, progress_cb=None):
    """For a scanned page that has been edited into a mixed scan+native page,
    return OCR word boxes from the PRISTINE scan (page_rec['ocr_source_bytes']),
    with words under edited/redacted regions removed.

    Returns [] when the page isn't an edited scan, or when a geometry change
    (e.g. a crop) means the source coordinates would no longer line up with the
    current page.  Mirrors the editor's _extract_text_and_graphics merge so the
    viewer can still select/search untouched scanned text on a page it now sees
    as 'native' because some native text was injected."""
    src = page_rec.get('ocr_source_bytes')
    cur = page_rec.get('pdf_bytes')
    if not src or src == cur:
        return []
    # Crop/geometry guard: source-OCR coordinates only align if the page box is
    # unchanged.  Skip the merge otherwise rather than place boxes wrongly.
    if _page_dims(src) != _page_dims(cur):
        logger.info("[OCR_MIX] skipping source-OCR merge for %s: page geometry changed",
                    page_rec.get('id'))
        return []
    words = _get_ocr_words_cached(session, page_rec['id'] + '::ocrsrc', src,
                                  progress_cb=progress_cb)
    words = _drop_covered_words(words, page_rec.get('covered_text_bboxes'))
    return _drop_words_in_regions(words, page_rec.get('merge_regions'))


def _search_ocr_words(words, query):
    """Search *query* in a list of OCR word dicts (text, x, y, w, h).

    Words are sorted into reading order and concatenated with spaces so that
    multi-word queries (including cross-line phrases) match correctly.

    Returns a list of {x, y, w, h} bounding boxes in PDF user-space.
    """
    if not words or not query:
        return []

    # Sort reading order: top of page first (largest y+h), then left-to-right.
    # Group approximate lines with a 5-point bucket so slight baseline
    # variations don't scramble left-right ordering within a line.
    sorted_words = sorted(words, key=lambda w: (-round((w['y'] + w['h']) / 5), w['x']))

    # Build concatenated text and record each word's char span
    spans = []   # (start, end, word_dict)
    text  = ''
    for w in sorted_words:
        if text:
            text += ' '
        start = len(text)
        text += w['text']
        spans.append((start, len(text), w))

    q_lower    = query.lower()
    text_lower = text.lower()
    matches    = []
    pos        = 0

    while True:
        idx = text_lower.find(q_lower, pos)
        if idx == -1:
            break
        end_idx = idx + len(query)

        hit_words = [w for (ws, we, w) in spans if we > idx and ws < end_idx]
        if hit_words:
            x0 = min(w['x']           for w in hit_words)
            y0 = min(w['y']           for w in hit_words)
            x1 = max(w['x'] + w['w'] for w in hit_words)
            y1 = max(w['y'] + w['h'] for w in hit_words)
            matches.append({'x': x0, 'y': y0, 'w': x1 - x0, 'h': y1 - y0})

        pos = idx + 1

    return matches


def _search_page(pdf_bytes, query):
    """Return a list of bounding-box dicts for every occurrence of *query* in
    the page.  Coordinates are in PDF user-space (y=0 at bottom)."""
    import pypdfium2 as pdfium
    with _NATIVE_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            page     = doc[0]
            textpage = page.get_textpage()

            n_chars   = textpage.count_chars()
            full_text = textpage.get_text_range()

            # Build per-character bounding boxes
            char_boxes = []   # (x1, y1, x2, y2) or None if unavailable
            for ci in range(n_chars):
                try:
                    x1, y1, x2, y2 = textpage.get_charbox(ci)
                    char_boxes.append((x1, y1, x2, y2))
                except Exception:
                    char_boxes.append(None)

            matches    = []
            q_lower    = query.lower()
            text_lower = full_text.lower()
            start      = 0

            while True:
                idx = text_lower.find(q_lower, start)
                if idx == -1 or idx + len(query) > len(char_boxes):
                    break
                boxes = [b for b in char_boxes[idx:idx + len(query)] if b is not None]
                # Filter out zero-area and origin-anchored chars (whitespace artefacts)
                valid = [b for b in boxes if b[2] > b[0] and b[3] > b[1]
                         and not (b[0] == 0 and b[1] == 0)]
                if valid:
                    x0 = min(b[0] for b in valid)
                    y0 = min(b[1] for b in valid)
                    x1 = max(b[2] for b in valid)
                    y1 = max(b[3] for b in valid)
                    matches.append({'x': x0, 'y': y0, 'w': x1 - x0, 'h': y1 - y0})
                start = idx + 1

            return matches
        finally:
            doc.close()


def _search_ap_text(pdf_bytes, query):
    """Char-box search like _search_page, but whitespace-tolerant: any run of
    whitespace in the extracted text (including the line breaks pdfium emits
    at a FreeText appearance's soft wraps) matches a single space in the
    query, so a phrase broken across wrapped lines is still found.

    Returns one {x, y, w, h} union rect per occurrence, in PDF user-space."""
    import pypdfium2 as pdfium
    q_norm = ' '.join(query.split()).lower()
    if not q_norm:
        return []
    with _NATIVE_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            page      = doc[0]
            textpage  = page.get_textpage()
            n_chars   = textpage.count_chars()
            full_text = textpage.get_text_range()
            char_boxes = []
            for ci in range(n_chars):
                try:
                    char_boxes.append(textpage.get_charbox(ci))
                except Exception:
                    char_boxes.append(None)
        finally:
            doc.close()

    # Whitespace-collapsed text plus a map back to original char indices.
    norm    = []     # (lower_char, orig_idx or None for collapsed whitespace)
    prev_ws = True   # drops leading whitespace too
    for ci in range(min(len(full_text), len(char_boxes))):
        ch = full_text[ci]
        if ch.isspace():
            if not prev_ws:
                norm.append((' ', None))
            prev_ws = True
        else:
            norm.append((ch.lower(), ci))
            prev_ws = False
    norm_text = ''.join(c for c, _ in norm)

    matches, start = [], 0
    while True:
        idx = norm_text.find(q_norm, start)
        if idx == -1:
            break
        idxs  = [oi for (_, oi) in norm[idx:idx + len(q_norm)] if oi is not None]
        boxes = [char_boxes[i] for i in idxs if char_boxes[i] is not None]
        valid = [b for b in boxes if b[2] > b[0] and b[3] > b[1]
                 and not (b[0] == 0 and b[1] == 0)]
        if valid:
            x0 = min(b[0] for b in valid)
            y0 = min(b[1] for b in valid)
            x1 = max(b[2] for b in valid)
            y1 = max(b[3] for b in valid)
            matches.append({'x': x0, 'y': y0, 'w': x1 - x0, 'h': y1 - y0})
        start = idx + 1
    return matches


def _annot_text_match_rects(pdf_bytes, query):
    """Search annotation text, returning the tightest highlight rects available.

    FreeText annotation text is drawn by the annotation's /AP /N appearance
    stream — pdfium bakes it into the rendered page image, but the regular
    page textpage never sees it, so _search_page cannot position matches
    inside the box.  To get real char positions we build a synthetic copy of
    the page whose CONTENT is just the matching annotations' /AP /N form
    XObjects, each mapped onto its annotation /Rect with the standard
    appearance→rect transform (PDF 32000-1 §12.5.5).  A pdfium textpage over
    that synthetic page then yields char boxes directly in page user-space.

    • /FreeText — one char-accurate rect per occurrence; falls back to the
      whole annotation /Rect when the appearance cannot be searched (missing
      /AP, foreign-producer quirks, match present in /Contents only).
    • /FreeText marked /PWKind /Markup (the Text tool's page markup) is
      included: its text is visible on the page but lives only in the
      annotation, so this is the only way search can reach it.
    • /Text (sticky note) — the note text is not drawn on the page, so the
      icon's /Rect is returned, as before.

    Returns a list of {x, y, w, h, annotation: True} rects in PDF user-space.
    """
    from pypdf.generic import (ArrayObject, DecodedStreamObject,
                               DictionaryObject, FloatObject, NameObject)

    def _norm(s):
        return ' '.join(str(s or '').split()).lower()

    q_norm = _norm(query)
    if not q_norm:
        return []

    rects = []
    try:
        reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return rects
        page   = reader.pages[0]
        annots = page.get('/Annots')
        if annots is None:
            return rects
        if hasattr(annots, 'get_object'):
            annots = annots.get_object()
    except Exception:
        return rects

    # Pass 1 — annotations whose /Contents contains the query.
    free_hits = []   # [(ann_obj, whole-box rect)] for /FreeText
    for ref in annots:
        try:
            ann = ref.get_object() if hasattr(ref, 'get_object') else ref
            subtype = str(ann.get('/Subtype', ''))
            if subtype not in ('/Text', '/FreeText'):
                continue
            if q_norm not in _norm(ann.get('/Contents', '')):
                continue
            rect = ann.get('/Rect')
            if hasattr(rect, 'get_object'):
                rect = rect.get_object()
            rv = [float(v) for v in rect]
            if len(rv) < 4:
                continue
            x0, x1 = sorted((rv[0], rv[2]))
            y0, y1 = sorted((rv[1], rv[3]))
            box = {'x': x0, 'y': y0, 'w': x1 - x0, 'h': y1 - y0,
                   'annotation': True}
            if subtype == '/Text':
                rects.append(box)   # note text is not on the page — icon box
            else:
                free_hits.append((ann, box))
        except Exception:
            continue
    if not free_hits:
        return rects

    # Pass 2 — synthetic page drawing only the matching /AP /N appearances.
    # Pure-pypdf work; the pdfium pass inside _search_ap_text takes the lock.
    word_rects = []
    try:
        writer = _pypdf.PdfWriter()
        mb     = [float(v) for v in page.mediabox]
        wpage  = writer.add_blank_page(width=mb[2] - mb[0],
                                       height=mb[3] - mb[1])
        wpage[NameObject('/MediaBox')] = ArrayObject(
            [FloatObject(v) for v in mb])

        xobjs, ops = DictionaryObject(), []
        for k, (ann, box) in enumerate(free_hits):
            try:
                ap = ann.get('/AP')
                if hasattr(ap, 'get_object'):
                    ap = ap.get_object()
                n_obj = (ap or {}).get('/N')
                if hasattr(n_obj, 'get_object'):
                    n_obj = n_obj.get_object()
                bbox = n_obj.get('/BBox') if n_obj is not None else None
                if hasattr(bbox, 'get_object'):
                    bbox = bbox.get_object()
                if not bbox or len(bbox) < 4:
                    continue
                bx  = [float(v) for v in bbox]
                mtx = n_obj.get('/Matrix')
                if hasattr(mtx, 'get_object'):
                    mtx = mtx.get_object()
                m = [float(v) for v in mtx] if mtx else [1, 0, 0, 1, 0, 0]

                # §12.5.5: BBox corners through /Matrix → bounds → fit /Rect
                corners = [(bx[0], bx[1]), (bx[2], bx[1]),
                           (bx[2], bx[3]), (bx[0], bx[3])]
                txs = [m[0]*x + m[2]*y + m[4] for x, y in corners]
                tys = [m[1]*x + m[3]*y + m[5] for x, y in corners]
                tw, th = max(txs) - min(txs), max(tys) - min(tys)
                sx = box['w'] / tw if tw > 1e-6 else 1.0
                sy = box['h'] / th if th > 1e-6 else 1.0
                ex = box['x'] - min(txs) * sx
                ey = box['y'] - min(tys) * sy

                cloned = n_obj.clone(writer)
                xobjs[NameObject(f'/PWAP{k}')] = cloned.indirect_reference
                ops.append(f'q {sx:.6f} 0 0 {sy:.6f} {ex:.4f} {ey:.4f} cm '
                           f'/PWAP{k} Do Q')
            except Exception:
                continue

        if ops:
            content = DecodedStreamObject()
            content.set_data('\n'.join(ops).encode('latin-1'))
            wpage[NameObject('/Contents')]  = writer._add_object(content)
            wpage[NameObject('/Resources')] = DictionaryObject(
                {NameObject('/XObject'): xobjs})
            buf = io.BytesIO()
            writer.write(buf)
            word_rects = _search_ap_text(buf.getvalue(), query)
    except Exception:
        logger.exception('[ANNOT-SEARCH] appearance-stream search failed; '
                         'falling back to whole-box highlights')
        word_rects = []

    # Assign each char-accurate rect to the annotation containing its centre;
    # annotations that got none keep the whole-box fallback.
    matched = [[] for _ in free_hits]
    for r in word_rects:
        cx, cy = r['x'] + r['w'] / 2, r['y'] + r['h'] / 2
        for i, (_, box) in enumerate(free_hits):
            if (box['x'] <= cx <= box['x'] + box['w'] and
                    box['y'] <= cy <= box['y'] + box['h']):
                matched[i].append({**r, 'annotation': True})
                break
    for i, (_, box) in enumerate(free_hits):
        rects.extend(matched[i] or [box])
    return rects


def register_viewer_routes(app, sessions, inject_token=None):
    """Register all viewer routes.  Call this from entry.py after creating the app."""

    def _serialised_on_session(fn):
        """Run a handler under the session lock, start to finish.

        Every annotation mutation is a read-modify-write of one page's PDF bytes
        PLUS a push onto the shared undo stack, and Flask serves concurrently.
        Taking the lock only for the snapshot push (which is all these handlers
        used to do) leaves the read and the write outside it, so two mutations of
        the same page overlap: both read the same bytes, the second write is
        built from a page that never saw the first, and one annotation silently
        disappears.  The undo stack is then holding two snapshots of the SAME
        state — and _annot_apply discards a snapshot equal to the current bytes,
        so one Ctrl+Z reaches past the action the user meant to reverse.  That is
        the "undo sometimes does nothing" / "removed lines out of order" pair.

        The viewer queues DRAWN annotations through one promise chain, but the
        lift delete, the sticky add, the popup edit and undo itself do not, so
        overlapping requests are ordinary traffic, not an edge case.  Correctness
        belongs here rather than in the client's queueing discipline.

        get_session_lock is reentrant, so _annot_push_undo's own acquisition
        nests harmlessly; the native render lock is always taken INSIDE this one,
        never the reverse.
        """
        import functools

        @functools.wraps(fn)
        def _locked(*args, **kwargs):
            body    = request.get_json(silent=True) or {}
            session = sessions.get(body.get('session_id', ''))                 if isinstance(body, dict) else None
            if session is None:
                return fn(*args, **kwargs)    # the handler reports the 404 itself
            with _pdf_core.get_session_lock(session):
                return fn(*args, **kwargs)
        return _locked

    # ── Page serving ────────────────────────────────────────────────────────────

    @app.route('/viewer')
    def viewer_page():
        base     = _get_base_dir()
        html_path = os.path.join(base, 'src', 'frontend', 'viewer.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
        if inject_token:
            html = inject_token(html)
        return html, 200, {'Content-Type': 'text/html; charset=utf-8'}

    @app.route('/viewer.html')
    def serve_viewer_html():
        base      = _get_base_dir()
        html_path = os.path.join(base, 'src', 'frontend', 'viewer.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
        if inject_token:
            html = inject_token(html)
        return html, 200, {'Content-Type': 'text/html; charset=utf-8'}

    # ── Render a single page ────────────────────────────────────────────────────

    @app.route('/api/viewer/render', methods=['POST'])
    def viewer_render():
        """Return a base64 PNG of the requested page plus its dimensions."""
        data       = request.json
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        _pdf_core.touch_session(session_id)   # keep an actively-viewed tab resident

        # Optional `scale` requests a sharper render of the focused page for
        # zoom-in (re-rasterized from vector, not upscaled).  Absent/<=base →
        # the base render; clients calibrate overlay coordinates to the base
        # scale and only use a higher returned scale to size the source bitmap.
        try:
            req_scale = float(data.get('scale') or 0)
        except (TypeError, ValueError):
            req_scale = 0.0
        if req_scale > VIEWER_SCALE:
            scale, max_px = req_scale, _MAX_RENDER_PIXELS_HI
        else:
            scale, max_px = VIEWER_SCALE, _MAX_RENDER_PIXELS

        logger.info(f"[TRANSITION] page {page_id} opened in VIEWER (session={session_id})")
        image_b64, page_w, page_h, actual_scale = _render_page(
            pdf_bytes, scale=scale, max_pixels=max_px)
        return jsonify({
            'image':  image_b64,
            'page_w': page_w,
            'page_h': page_h,
            'scale':  actual_scale,
        })

    # ── Page list (thumbnails) for the left panel ───────────────────────────────

    @app.route('/api/viewer/pages', methods=['POST'])
    def viewer_pages():
        data       = request.json
        session_id = data.get('session_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pages = [{
            'id':          p['id'],
            'filename':    p['filename'],
            'page_num':    p['page_num'],
            'total_pages': p['total_pages'],
            'thumbnail':   p['thumbnail'],
            'width_pts':   p.get('width_pts',  595.28),
            'height_pts':  p.get('height_pts', 841.89),
        } for p in session['pages']]
        enc_path = session.get('encrypted_file_path', '')
        enc_name = os.path.basename(enc_path) if enc_path else ''
        return jsonify({
            'pages':          pages,
            'needs_password': session.get('needs_password', False),
            'filename':       enc_name,
            'is_protected':   bool(session.get('password', '')),
            # Opened read-only: the document's owner password is unknown, so its
            # permissions forbid editing until the operator supplies it.
            'restricted':     session.get('restricted', False),
        })

    # ── Annotations (sticky notes) ─────────────────────────────────────────────

    @app.route('/api/viewer/annotations', methods=['POST'])
    def viewer_annotations():
        """Return /Text sticky-note annotations for a single page."""
        data       = request.json
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pdf_bytes, _ = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        return jsonify({'annotations': _get_annotations(pdf_bytes)})

    # ── All annotations (sticky notes) across all pages ───────────────────────

    @app.route('/api/viewer/all_annotations', methods=['POST'])
    def viewer_all_annotations():
        """Return /Text sticky-note annotations for every page in the session,
        keyed by page_id so the client can group and navigate by page."""
        data       = request.json
        session_id = data.get('session_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        result = {}
        for page_rec in session['pages']:
            pdf_bytes = page_rec['pdf_bytes']
            annotations = _get_annotations(pdf_bytes)
            if annotations:
                result[page_rec['id']] = annotations
        u, r = _annot_counts(session)
        return jsonify({'annotations_by_page': result,
                        'undo_count': u, 'redo_count': r})

    # ── Links (clickable URLs in the document) ────────────────────────────────

    @app.route('/api/viewer/links', methods=['POST'])
    def viewer_links():
        """Every followable link in the session, keyed by page_id.

        One call per document, like /api/viewer/all_annotations, rather than one
        per page: the viewer's request budget is already tight enough that a
        fast scroll through a long document is the thing it has to survive (see
        CLAUDE.md), and links never change while a document is open.
        """
        data       = request.json or {}
        session_id = data.get('session_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        result = {}
        for page_rec in session['pages']:
            links = _get_links(page_rec['pdf_bytes'])
            if links:
                result[page_rec['id']] = links
        return jsonify({'links_by_page': result})

    @app.route('/api/viewer/open_link', methods=['POST'])
    def viewer_open_link():
        """Open link *idx* of *page_id* in the user's default browser.

        The page sends an index, never a URL.  The destination is re-read from
        the open document here, so the only thing this route can ever be made to
        open is a link that really is in the PDF in front of the user — a page
        bug, or anything that got a foothold in the renderer, cannot point it
        somewhere of its own choosing.  (entry.py's /api/open_external is the
        same concern from the other end: it answers only for the update prompt's
        fixed SourceForge destination, and stays that narrow.)
        """
        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pdf_bytes, _ = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        try:
            idx = int(data.get('idx', -1))
        except (TypeError, ValueError):
            idx = -1
        links = _get_links(pdf_bytes)
        if idx < 0 or idx >= len(links):
            return jsonify({'error': 'Link not found',
                            'error_key': 'err.link_not_found'}), 404
        url = links[idx]['url']
        # _get_links only ever returns vetted schemes; assert it rather than
        # trust it, because this is the line where a string becomes a shell call.
        if not url.lower().startswith(_LINK_SCHEMES):
            return jsonify({'error': 'Link not allowed'}), 400
        from platform_io import platform_paths
        if not platform_paths.open_url_in_default_browser(url):
            logger.info("open_link: no handler for %s", url)
            return jsonify({'error': 'Could not open link',
                            'error_key': 'err.link_open_failed'}), 500
        logger.info("open_link: opened %s", url)
        return jsonify({'ok': True, 'url': url})

    # ── Update annotation contents ─────────────────────────────────────────────

    @app.route('/api/viewer/update_annotation', methods=['POST'])
    @_serialised_on_session
    def viewer_update_annotation():
        """Update /Contents and /T of an existing /Text or /FreeText annotation."""
        from pypdf.generic import NameObject as _N2, TextStringObject as _TS
        data       = request.json
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        annot_idx  = int(data.get('annot_idx', 0))
        new_text   = str(data.get('text',   '') or '')
        new_author = str(data.get('author', '') or '')
        do_delete  = bool(data.get('delete'))

        session = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        if not data.get('no_snapshot'):
            _annot_push_undo(session, page_id)

        try:
            reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
            writer = _pypdf.PdfWriter()
            writer.append(reader)

            page   = writer.pages[0]
            annots = page.get('/Annots')
            if annots is None:
                return jsonify({'error': 'No annotations on this page', 'error_key': 'err.no_annotations'}), 404
            if hasattr(annots, 'get_object'):
                annots = annots.get_object()

            editable_n = 0
            found = False
            for pos, item in enumerate(annots):
                ann_obj = item.get_object() if hasattr(item, 'get_object') else item
                subtype = str(ann_obj.get('/Subtype', ''))
                if subtype not in ('/Text', '/FreeText'):
                    continue
                # Must match _get_annotations' index basis (it skips dumb text
                # and replies).
                if subtype == '/FreeText' and \
                        str(ann_obj.get('/PWKind', '')) == '/Markup':
                    continue
                if ann_obj.get('/IRT') is not None:
                    continue
                if editable_n == annot_idx:
                    if do_delete:
                        # The thread goes with it: a reply to nothing is junk
                        # that other readers show as a stray note.
                        for p in sorted(_thread_positions(annots, pos) + [pos],
                                        reverse=True):
                            del annots[p]
                        found = True
                        break
                    ann_obj[_N2('/Contents')] = _TS(new_text)
                    ann_obj[_N2('/T')]        = _TS(new_author)
                    ann_obj[_N2('/M')]        = _TS(_pdf_date_now())
                    # Update appearance for FreeText: patch the BT..ET block in
                    # the existing /AP stream so font, size, bold, border, and
                    # background are all preserved.  Fall back to full generation
                    # if the annotation has no /AP (e.g. was never rendered).
                    if subtype == '/FreeText':
                        if not _patch_freetext_ap_text(ann_obj, new_text, writer):
                            rect_ref = ann_obj.get('/Rect')
                            if rect_ref is not None:
                                if hasattr(rect_ref, 'get_object'):
                                    rect_ref = rect_ref.get_object()
                                rv = [float(v) for v in rect_ref]
                                if len(rv) >= 4:
                                    # /Rect is raw; on a rotated page lay text out
                                    # upright in display dims + counter-rotate.
                                    rot, rW, rH = _page_rotation_and_raw_dims(pdf_bytes)
                                    if rot:
                                        dsp = _xform_pdf_rect(rv, _raw_rect_to_display,
                                                              rot, rW, rH)
                                        _generate_freetext_ap(
                                            ann_obj, new_text, rv, writer,
                                            layout_wh=(dsp[2] - dsp[0], dsp[3] - dsp[1]),
                                            ap_matrix=_ap_upright_matrix(rot))
                                    else:
                                        _generate_freetext_ap(ann_obj, new_text, rv, writer)
                    found = True
                    break
                editable_n += 1

            if not found:
                return jsonify({'error': 'Annotation not found'}), 404

            out = io.BytesIO()
            writer.write(out)
            page_rec['pdf_bytes'] = out.getvalue()
            thumb = _render_thumb_b64(page_rec['pdf_bytes'])
            if thumb:
                page_rec['thumbnail'] = thumb
            _recovery.mark_dirty(session, page_id)

            u, r = _annot_counts(session)
            return jsonify({
                'annotations': _get_annotations(page_rec['pdf_bytes']),
                'thumbnail':   thumb,
                'undo_count':  u,
                'redo_count':  r,
            })
        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({'error': str(exc)}), 500

    # ── Add new annotation drawn by the user (text or shape) ──────────────────

    @app.route('/api/viewer/add_annotation', methods=['POST'])
    @_serialised_on_session
    def viewer_add_annotation():
        """Create a new annotation drawn in the viewer.

        Supports:
          • FreeText        — rect + text, styled via text_color/font_size/
                              border_color/border_width.
          • Square / Circle — rect, styled via border_color/border_width and
                              optional fill_color.
          • Line (+arrow)   — line endpoints, styled via border_color/
                              border_width and the arrow / arrow_start flags
                              (both set = double-ended arrow).

        Shapes are listed in the Comments panel as markup (see
        _get_annotations) but get no comment popup — they are edited by lifting.
        """
        from pypdf.generic import (DictionaryObject, NameObject, ArrayObject,
                                   FloatObject, TextStringObject as _TS2,
                                   NumberObject)
        data       = request.json
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        subtype    = str(data.get('subtype', 'FreeText') or 'FreeText')
        rect       = data.get('rect', [0, 0, 200, 80])
        line_pts   = data.get('line')                 # [x1,y1,x2,y2] for Line
        text       = str(data.get('text', '') or '')
        author     = str(data.get('author', '') or '')

        def _norm_rgb(v, default=None):
            """Accept [r,g,b] 0..1 list or None → list or default."""
            if v is None:
                return default
            try:
                rgb = [max(0.0, min(1.0, float(c))) for c in v][:3]
                return rgb if len(rgb) == 3 else default
            except Exception:
                return default

        text_color   = _norm_rgb(data.get('text_color'),   [0.0, 0.0, 0.0])
        border_color = _norm_rgb(data.get('border_color'), [1.0, 0.0, 0.0])
        fill_color   = _norm_rgb(data.get('fill_color'),   None)
        try:    border_width = max(0.0, float(data.get('border_width', 1.0)))
        except Exception: border_width = 1.0
        # Stroke dash style (shapes/lines): 'solid' (default) | 'dashed' | 'dotted'.
        border_style = str(data.get('border_style', 'solid') or 'solid')
        if border_style not in ('solid', 'dashed', 'dotted'):
            border_style = 'solid'
        _dw = max(border_width, 0.5)
        dashes = ([3.0 * _dw, 3.0 * _dw] if border_style == 'dashed'
                  else [_dw, 2.0 * _dw]  if border_style == 'dotted' else None)
        try:    font_size = max(4.0, float(data.get('font_size', 11.0)))
        except Exception: font_size = 11.0
        arrow       = bool(data.get('arrow', False))        # head at endpoint 2
        arrow_start = bool(data.get('arrow_start', False))  # head at endpoint 1
        # FreeText drawn with the Text tool is decorative markup, not a comment.
        is_comment = bool(data.get('is_comment', True))
        # Font: restrict to the PDF base-14 families (no embedding needed).
        _BASE14 = {'Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique',
                   'Helvetica-BoldOblique', 'Times-Roman', 'Times-Bold',
                   'Times-Italic', 'Times-BoldItalic', 'Courier', 'Courier-Bold',
                   'Courier-Oblique', 'Courier-BoldOblique'}
        font = str(data.get('font', 'Helvetica') or 'Helvetica')
        if font not in _BASE14:
            font = 'Helvetica'

        session = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404

        # The frontend draws in the displayed (post-/Rotate) frame and sends
        # coords as if the page were unrotated.  A /Rect is interpreted in raw
        # user space and re-rotated by /Rotate at render time, so convert the
        # incoming geometry display→raw here (identity for /Rotate 0).  Keep the
        # display-frame box dims for FreeText/Stamp, whose appearance content is
        # laid out upright and counter-rotated via /Matrix.
        rotate, raw_W, raw_H, off_x, off_y = _page_display_frame(pdf_bytes)
        disp_rect = [float(v) for v in rect] if rect and len(rect) >= 4 else None
        # NB: not `if rotate` — on a CROPPED page the incoming coords are
        # relative to the CropBox and still need translating into user space,
        # rotated or not.  Writing them straight through is what displaced every
        # annotation on a cropped page (tests/test_crop_coords.py).  disp_rect is
        # only ever used for width/height below, which a translation leaves alone.
        if rotate or off_x or off_y:
            rect = _xform_pdf_rect(rect, _display_rect_to_raw,
                                   rotate, raw_W, raw_H, off_x, off_y)
            line_pts = _xform_pdf_points(line_pts, _display_rect_to_raw,
                                         rotate, raw_W, raw_H, off_x, off_y)

        # Lift-edit re-commits pass no_snapshot (the lift's delete already
        # snapshotted), so a move/restyle is a single undo step.
        if not data.get('no_snapshot'):
            _annot_push_undo(session, page_id)

        try:
            reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
            writer = _pypdf.PdfWriter()
            writer.append(reader)

            page = writer.pages[0]

            def _carr(rgb):
                return ArrayObject([FloatObject(c) for c in rgb])

            if subtype == 'FreeText':
                rv = [float(v) for v in rect]
                tc = text_color or [0.0, 0.0, 0.0]
                da_str = (f'/{font} {font_size:.2f} Tf '
                          f'{tc[0]:.4f} {tc[1]:.4f} {tc[2]:.4f} rg')
                annot = DictionaryObject({
                    NameObject('/Type'):     NameObject('/Annot'),
                    NameObject('/Subtype'):  NameObject('/FreeText'),
                    NameObject('/Rect'):     ArrayObject([FloatObject(v) for v in rv]),
                    NameObject('/Contents'): _TS2(text),
                    NameObject('/T'):        _TS2(author),
                    NameObject('/DA'):       _TS2(da_str),
                    NameObject('/F'):        NumberObject(4),
                    NameObject('/Q'):        NumberObject(0),
                    NameObject('/BS'): DictionaryObject({
                        NameObject('/W'): NumberObject(border_width),
                        NameObject('/S'): NameObject('/S')}),
                })
                if fill_color:
                    annot[NameObject('/IC')] = _carr(fill_color)
                if border_width > 0 and border_color:
                    annot[NameObject('/C')] = _carr(border_color)
                if not is_comment:
                    # Private marker → listed as markup, not a comment (no popup).
                    annot[NameObject('/PWKind')] = NameObject('/Markup')
                # On a rotated page, lay the text out upright in the display-frame
                # box and counter-rotate via /Matrix so it isn't rendered sideways.
                if rotate and disp_rect:
                    _generate_freetext_ap(
                        annot, text, rv, writer,
                        layout_wh=(disp_rect[2] - disp_rect[0],
                                   disp_rect[3] - disp_rect[1]),
                        ap_matrix=_ap_upright_matrix(rotate))
                else:
                    _generate_freetext_ap(annot, text, rv, writer)

            elif subtype in ('Square', 'Circle', 'Line', 'Polygon'):
                # Dash style applies to Square/Circle/Line (clouds stay solid).
                use_dashes = dashes if subtype != 'Polygon' else None
                style = {'border_color': border_color, 'fill_color': fill_color,
                         'border_width': border_width, 'arrow': arrow,
                         'arrow_start': arrow_start, 'dashes': use_dashes}
                geom = ([float(v) for v in line_pts] if subtype == 'Line'
                        else [float(v) for v in rect])
                bs = DictionaryObject({
                    NameObject('/W'): NumberObject(border_width),
                    NameObject('/S'): NameObject('/D' if use_dashes else '/S')})
                if use_dashes:
                    bs[NameObject('/D')] = ArrayObject(
                        [FloatObject(v) for v in use_dashes])
                annot = DictionaryObject({
                    NameObject('/Type'):    NameObject('/Annot'),
                    NameObject('/Subtype'): NameObject('/' + subtype),
                    NameObject('/F'):       NumberObject(4),
                    NameObject('/BS'):      bs,
                })
                if author:
                    annot[NameObject('/T')] = _TS2(author)
                if border_color:
                    annot[NameObject('/C')] = _carr(border_color)
                if fill_color and subtype in ('Square', 'Circle'):
                    annot[NameObject('/IC')] = _carr(fill_color)
                if subtype == 'Line':
                    annot[NameObject('/L')] = ArrayObject(
                        [FloatObject(v) for v in geom])
                    if arrow or arrow_start:
                        annot[NameObject('/LE')] = ArrayObject([
                            NameObject('/OpenArrow' if arrow_start else '/None'),
                            NameObject('/OpenArrow' if arrow       else '/None')])
                if subtype == 'Polygon':     # revision cloud — record vertices
                    gx1, gy1, gx2, gy2 = geom
                    annot[NameObject('/Vertices')] = ArrayObject([FloatObject(v) for v in
                        (gx1, gy1, gx2, gy1, gx2, gy2, gx1, gy2)])
                    annot[NameObject('/IT')] = NameObject('/PolygonCloud')
                ap_rect = _generate_shape_ap(annot, subtype, geom, style, writer)
                annot[NameObject('/Rect')] = ArrayObject(
                    [FloatObject(v) for v in ap_rect])
                # Store the exact user-drawn box so lift-to-edit is pixel-exact
                # (the /Rect above is padded for the stroke/bumps).
                if subtype in ('Square', 'Circle', 'Polygon'):
                    annot[NameObject('/PWRect')] = _carr(geom)

            elif subtype == 'Highlight':
                rv = [float(v) for v in rect]
                lx1, ly1 = min(rv[0], rv[2]), min(rv[1], rv[3])
                lx2, ly2 = max(rv[0], rv[2]), max(rv[1], rv[3])
                hl_color = border_color or [1.0, 0.92, 0.0]
                annot = DictionaryObject({
                    NameObject('/Type'):    NameObject('/Annot'),
                    NameObject('/Subtype'): NameObject('/Highlight'),
                    NameObject('/Rect'):    _carr([lx1, ly1, lx2, ly2]),
                    # QuadPoints corner order per PDF spec: TL, TR, BL, BR
                    NameObject('/QuadPoints'): _carr(
                        [lx1, ly2, lx2, ly2, lx1, ly1, lx2, ly1]),
                    NameObject('/C'):       _carr(hl_color),
                    NameObject('/F'):       NumberObject(4),
                })
                if author:
                    annot[NameObject('/T')] = _TS2(author)
                _generate_highlight_ap(annot, [lx1, ly1, lx2, ly2], hl_color, writer)

            elif subtype == 'Text':                 # sticky note (point annotation)
                from pypdf.generic import (BooleanObject,
                                           DecodedStreamObject as _DSO)
                rv = [float(v) for v in rect]
                col = fill_color or [1.0, 0.92, 0.0]   # default sticky yellow
                annot = DictionaryObject({
                    NameObject('/Type'):     NameObject('/Annot'),
                    NameObject('/Subtype'):  NameObject('/Text'),
                    NameObject('/Rect'):     _carr(rv),
                    NameObject('/Contents'): _TS2(text),
                    NameObject('/Name'):     NameObject('/Comment'),
                    NameObject('/Open'):     BooleanObject(False),
                    NameObject('/C'):        _carr(col),
                    NameObject('/F'):        NumberObject(4),
                })
                if author:
                    annot[NameObject('/T')] = _TS2(author)
                # Empty appearance → pdfium renders nothing, so the viewer's own
                # folded-note overlay is the single icon (no duplicate).
                _xo = _DSO(); _xo.set_data(b'')
                _xo[NameObject('/Type')]    = NameObject('/XObject')
                _xo[NameObject('/Subtype')] = NameObject('/Form')
                _xo[NameObject('/BBox')]    = _carr([0.0, 0.0,
                                                     rv[2] - rv[0], rv[3] - rv[1]])
                annot[NameObject('/AP')] = writer._add_object(
                    DictionaryObject({NameObject('/N'): writer._add_object(_xo)}))
            elif subtype == 'Stamp':                # imported image
                from pypdf.generic import DecodedStreamObject as _DSO2
                img_data = str(data.get('image', '') or '')
                if ',' in img_data:                 # strip data-URL prefix
                    img_data = img_data.split(',', 1)[1]
                try:
                    raw = base64.b64decode(img_data) if img_data else b''
                except Exception:
                    raw = b''
                if not raw:
                    return jsonify({'error': 'No image data', 'error_key': 'err.no_image_data'}), 400
                img_ref, iw, ih = _make_image_xobject(writer, raw)
                if img_ref is None:
                    return jsonify({'error': 'Image embed failed', 'error_key': 'err.image_embed_failed'}), 400
                rv = [float(v) for v in rect]
                rx1, ry1 = min(rv[0], rv[2]), min(rv[1], rv[3])
                rx2, ry2 = max(rv[0], rv[2]), max(rv[1], rv[3])
                bw, bh = rx2 - rx1, ry2 - ry1
                annot = DictionaryObject({
                    NameObject('/Type'):    NameObject('/Annot'),
                    NameObject('/Subtype'): NameObject('/Stamp'),
                    NameObject('/Rect'):    ArrayObject([FloatObject(rx1), FloatObject(ry1),
                                                         FloatObject(rx2), FloatObject(ry2)]),
                    NameObject('/F'):       NumberObject(4),
                })
                if author:
                    annot[NameObject('/T')] = _TS2(author)
                # On a rotated page, author the image upright in the display-frame
                # box and counter-rotate via /Matrix so it isn't rendered sideways
                # (the /Rect above is already in raw space).
                ap_matrix = _ap_upright_matrix(rotate) if rotate and disp_rect else None
                if ap_matrix:
                    fw, fh = (disp_rect[2] - disp_rect[0], disp_rect[3] - disp_rect[1])
                else:
                    fw, fh = bw, bh
                # Form XObject appearance draws the image scaled to the BBox.
                form = _DSO2()
                form.set_data(f'q {fw:.3f} 0 0 {fh:.3f} 0 0 cm /Im0 Do Q'.encode('latin-1'))
                form[NameObject('/Type')]    = NameObject('/XObject')
                form[NameObject('/Subtype')] = NameObject('/Form')
                form[NameObject('/BBox')]    = ArrayObject(
                    [FloatObject(0.0), FloatObject(0.0), FloatObject(fw), FloatObject(fh)])
                if ap_matrix:
                    form[NameObject('/Matrix')] = ArrayObject(
                        [FloatObject(v) for v in ap_matrix])
                form[NameObject('/Resources')] = DictionaryObject({
                    NameObject('/XObject'): DictionaryObject({
                        NameObject('/Im0'): img_ref})})
                annot[NameObject('/AP')] = writer._add_object(
                    DictionaryObject({NameObject('/N'): writer._add_object(form)}))

            else:
                return jsonify({'error': f'Unsupported subtype: {subtype}'}), 400

            # Review metadata on everything the viewer writes.  A lifted
            # annotation re-commits with its original creation date.
            now = _pdf_date_now()
            annot[NameObject('/CreationDate')] = _TS2(
                _pdf_date_from_iso(data.get('created')) or now)
            annot[NameObject('/M')] = _TS2(now)
            annot[NameObject('/NM')] = _TS2(uuid.uuid4().hex)
            if author and '/T' not in annot:
                annot[NameObject('/T')] = _TS2(author)

            annot_ref = writer._add_object(annot)

            annots = page.get('/Annots')
            if annots is None:
                page[NameObject('/Annots')] = ArrayObject([annot_ref])
                annots = page['/Annots']
                if hasattr(annots, 'get_object'):
                    annots = annots.get_object()
            else:
                if hasattr(annots, 'get_object'):
                    annots = annots.get_object()
                annots.append(annot_ref)

            # A lifted annotation carries its thread back (see
            # _get_editable_annotations) — re-attach it to the new object.
            for r in (data.get('replies') or [])[:200]:
                if not isinstance(r, dict):
                    continue
                state = str(r.get('state') or '')
                if state and state not in _REVIEW_STATES:
                    continue
                annots.append(_make_thread_annot(
                    writer, annot_ref, annot['/Rect'],
                    str(r.get('author') or '')[:200], str(r.get('text') or '')[:20000],
                    state=state or None, created=r.get('created'),
                    modified=r.get('modified')))

            out = io.BytesIO()
            writer.write(out)
            page_rec['pdf_bytes'] = out.getvalue()
            thumb = _render_thumb_b64(page_rec['pdf_bytes'])
            if thumb:
                page_rec['thumbnail'] = thumb
            _recovery.mark_dirty(session, page_id)

            u, r = _annot_counts(session)
            return jsonify({
                'annotations': _get_annotations(page_rec['pdf_bytes']),
                'thumbnail':   thumb,
                'undo_count':  u,
                'redo_count':  r,
            })
        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({'error': str(exc)}), 500

    # ── Review threads: reply, set status, delete a reply ─────────────────────

    @app.route('/api/viewer/comment_thread', methods=['POST'])
    @_serialised_on_session
    def viewer_comment_thread():
        """Reply to a listed item, set its review status, or delete a reply.

          action='reply'        annot_idx (the item), text, author
          action='status'       annot_idx (the item), state, author — replaces
                                the item's previous status; state 'None' clears
          action='delete_reply' annot_idx (the reply) — its own replies go too

        annot_idx is the raw /Annots position `_get_annotations` reports.  Each
        call is one undo step, like every other annotation mutation.
        """
        from pypdf.generic import NameObject as _N3
        data       = request.json or {}
        session    = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        page_id    = data.get('page_id', '')
        action     = str(data.get('action', ''))
        author     = str(data.get('author', '') or '').strip()[:200]
        text       = str(data.get('text', '') or '').strip()[:20000]
        state      = str(data.get('state', '') or '')
        try:    annot_idx = int(data.get('annot_idx', -1))
        except Exception: annot_idx = -1
        if action not in ('reply', 'status', 'delete_reply'):
            return jsonify({'error': 'Unknown action'}), 400
        if action == 'reply' and not text:
            return jsonify({'error': 'Empty reply', 'error_key': 'err.empty_reply'}), 400
        if action == 'status' and state not in _REVIEW_STATES:
            return jsonify({'error': 'Unknown status'}), 400
        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        try:
            reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
            writer = _pypdf.PdfWriter()
            writer.append(reader)
            page   = writer.pages[0]
            annots = page.get('/Annots')
            if hasattr(annots, 'get_object'):
                annots = annots.get_object()
            if not annots or annot_idx < 0 or annot_idx >= len(annots):
                return jsonify({'error': 'Annotation not found',
                                'error_key': 'err.comment_gone'}), 404
            target_ref = annots[annot_idx]
            target = target_ref.get_object() if hasattr(target_ref, 'get_object') else target_ref
            is_reply = target.get('/IRT') is not None
            # A thread hangs off an indirect object; a reply cannot be threaded
            # onto, and a delete_reply must name a reply.
            if (action == 'delete_reply') != is_reply or \
                    getattr(target_ref, 'idnum', None) is None:
                return jsonify({'error': 'Annotation not found',
                                'error_key': 'err.comment_gone'}), 404
            _annot_push_undo(session, page_id)

            if action == 'delete_reply':
                for p in sorted(_thread_positions(annots, annot_idx) + [annot_idx],
                                reverse=True):
                    del annots[p]
            else:
                rect = target.get('/Rect')
                if hasattr(rect, 'get_object'):
                    rect = rect.get_object()
                if action == 'status':
                    # One status per item: the new one replaces the old.
                    for p in sorted(_thread_positions(annots, annot_idx), reverse=True):
                        a = annots[p].get_object()
                        if _is_review_state(a) and _irt_idnum(a) == target_ref.idnum:
                            del annots[p]
                    if state != 'None':
                        annots.append(_make_thread_annot(
                            writer, target_ref, rect, author, state=state))
                else:
                    annots.append(_make_thread_annot(
                        writer, target_ref, rect, author, text=text))
                target[_N3('/M')] = _TS_mod(_pdf_date_now())

            out = io.BytesIO()
            writer.write(out)
            page_rec['pdf_bytes'] = out.getvalue()
            _recovery.mark_dirty(session, page_id)
            u, r = _annot_counts(session)
            return jsonify({
                'annotations': _get_annotations(page_rec['pdf_bytes']),
                'editable':    _get_editable_annotations(page_rec['pdf_bytes']),
                'undo_count':  u,
                'redo_count':  r,
            })
        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({'error': str(exc)}), 500

    # ── Editable annotations (select / lift-to-edit / delete) ──────────────────

    @app.route('/api/viewer/editable_annotations', methods=['POST'])
    def viewer_editable_annotations():
        """Enumerate all viewer-editable annotations on a page with geometry +
        style, so the frontend can draw selection hit-areas and lift-to-edit."""
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pdf_bytes, _ = _pdf_bytes_from_session(session, data.get('page_id', ''))
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        return jsonify({'annotations': _get_editable_annotations(pdf_bytes)})

    @app.route('/api/viewer/stamp_image', methods=['POST'])
    def viewer_stamp_image():
        """Return a /Stamp annotation's embedded image as a data URL, so the
        frontend can lift it into an editable image draft."""
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pdf_bytes, _ = _pdf_bytes_from_session(session, data.get('page_id', ''))
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        try:
            idx    = int(data.get('annot_idx', -1))
            reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
            annots = reader.pages[0].get('/Annots')
            if hasattr(annots, 'get_object'): annots = annots.get_object()
            ann = annots[idx].get_object()
            url = _stamp_image_data_url(ann)
            if not url:
                return jsonify({'error': 'No image found', 'error_key': 'err.no_image_found'}), 404
            return jsonify({'image': url})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 500

    @app.route('/api/viewer/delete_annotation', methods=['POST'])
    @_serialised_on_session
    def viewer_delete_annotation():
        """Delete the annotation at raw /Annots index *annot_idx* and re-render."""
        data       = request.json or {}
        session    = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        page_id    = data.get('page_id', '')
        try:    annot_idx = int(data.get('annot_idx', -1))
        except Exception: annot_idx = -1
        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        if not data.get('no_snapshot'):
            _annot_push_undo(session, page_id)
        try:
            reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
            writer = _pypdf.PdfWriter()
            writer.append(reader)
            page   = writer.pages[0]
            annots = page.get('/Annots')
            if hasattr(annots, 'get_object'):
                annots = annots.get_object()
            if not annots or annot_idx < 0 or annot_idx >= len(annots):
                return jsonify({'error': 'Annotation not found'}), 404
            # Its replies and status go too.  A lift re-commits them (they
            # travel on the draft); a real delete should not leave them behind.
            for p in sorted(_thread_positions(annots, annot_idx) + [annot_idx],
                            reverse=True):
                del annots[p]

            out = io.BytesIO()
            writer.write(out)
            page_rec['pdf_bytes'] = out.getvalue()
            thumb = _render_thumb_b64(page_rec['pdf_bytes'])
            if thumb:
                page_rec['thumbnail'] = thumb
            _recovery.mark_dirty(session, page_id)
            u, r = _annot_counts(session)
            return jsonify({
                'annotations': _get_annotations(page_rec['pdf_bytes']),
                'editable':    _get_editable_annotations(page_rec['pdf_bytes']),
                'thumbnail':   thumb,
                'undo_count':  u,
                'redo_count':  r,
            })
        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({'error': str(exc)}), 500

    # ── Undo / redo (annotation history) ────────────────────────────────────────

    @app.route('/api/viewer/undo', methods=['POST'])
    @_serialised_on_session
    def viewer_undo():
        session = sessions.get((request.json or {}).get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        page_id = _annot_apply(session, redo=False)
        return jsonify(_annot_history_payload(session, page_id))

    @app.route('/api/viewer/redo', methods=['POST'])
    @_serialised_on_session
    def viewer_redo():
        session = sessions.get((request.json or {}).get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        page_id = _annot_apply(session, redo=True)
        return jsonify(_annot_history_payload(session, page_id))

    # ── Bookmarks (document outline) ────────────────────────────────────────────
    # Stored as session metadata: {id, title, page_id, y, level}.  Written into
    # the PDF /Outlines tree at save time (see entry.py _write_outlines).

    @app.route('/api/viewer/bookmarks', methods=['POST'])
    def viewer_bookmarks():
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        return jsonify({'bookmarks': session.get('bookmarks', [])})

    @app.route('/api/viewer/add_bookmark', methods=['POST'])
    def viewer_add_bookmark():
        import uuid as _uuid
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        page_id = data.get('page_id', '')
        if not any(p['id'] == page_id for p in session.get('pages', [])):
            return jsonify({'error': 'Page not found'}), 404
        title = str(data.get('title', '') or '').strip() or 'Bookmark'
        y = data.get('y')
        try:    y = float(y) if y is not None else None
        except Exception: y = None
        try:    level = max(0, int(data.get('level', 0) or 0))
        except Exception: level = 0
        bm = {'id': str(_uuid.uuid4()), 'title': title, 'page_id': page_id,
              'y': y, 'level': level}
        session.setdefault('bookmarks', []).append(bm)
        _recovery.mark_meta_dirty(session)
        return jsonify({'bookmarks': session['bookmarks'], 'added': bm})

    @app.route('/api/viewer/delete_bookmark', methods=['POST'])
    def viewer_delete_bookmark():
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        bid = data.get('bookmark_id', '')
        session['bookmarks'] = [b for b in session.get('bookmarks', [])
                                if b.get('id') != bid]
        _recovery.mark_meta_dirty(session)
        return jsonify({'bookmarks': session['bookmarks']})

    @app.route('/api/viewer/delete_bookmarks', methods=['POST'])
    def viewer_delete_bookmarks():
        """Bulk delete: every bookmark (``all``) or a chosen set (``bookmark_ids``).

        A merge of many documents can leave hundreds of outline entries, so the
        panel offers "Delete all" and "Delete selected" rather than making the
        operator click each one away.  ``all`` clears the list outright, which
        also covers the case where the client's id list has gone stale.
        """
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        bms    = session.get('bookmarks', [])
        before = len(bms)
        if data.get('all'):
            session['bookmarks'] = []
        else:
            ids = {str(i) for i in (data.get('bookmark_ids') or [])}
            session['bookmarks'] = [b for b in bms if b.get('id') not in ids]
        _recovery.mark_meta_dirty(session)
        return jsonify({'bookmarks': session['bookmarks'],
                        'deleted': before - len(session['bookmarks'])})

    @app.route('/api/viewer/rename_bookmark', methods=['POST'])
    def viewer_rename_bookmark():
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        bid   = data.get('bookmark_id', '')
        title = str(data.get('title', '') or '').strip()
        for b in session.get('bookmarks', []):
            if b.get('id') == bid and title:
                b['title'] = title
                break
        _recovery.mark_meta_dirty(session)
        return jsonify({'bookmarks': session.get('bookmarks', [])})

    @app.route('/api/viewer/move_bookmark', methods=['POST'])
    def viewer_move_bookmark():
        data      = request.json or {}
        session   = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        bid       = data.get('bookmark_id', '')
        direction = data.get('direction', '')
        bms       = session.get('bookmarks', [])
        idx       = next((i for i, b in enumerate(bms) if b.get('id') == bid), -1)
        if idx >= 0:
            if direction == 'up' and idx > 0:
                bms[idx - 1], bms[idx] = bms[idx], bms[idx - 1]
            elif direction == 'down' and idx < len(bms) - 1:
                bms[idx + 1], bms[idx] = bms[idx], bms[idx + 1]
        _recovery.mark_meta_dirty(session)
        return jsonify({'bookmarks': bms})

    @app.route('/api/viewer/auto_bookmarks', methods=['POST'])
    def viewer_auto_bookmarks():
        """Detect proposed bookmarks (printed contents page, else headings).

        Does NOT modify the session — the frontend previews the proposals and
        commits the chosen ones via /api/viewer/add_bookmarks.
        """
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        if not session.get('pages'):
            return jsonify({'proposed': [], 'source': 'none'})
        try:
            proposed, source = _auto_detect_bookmarks(session)
        except Exception as e:
            logger.exception('auto_bookmarks failed')
            return jsonify({'error': str(e)}), 500
        return jsonify({'proposed': proposed, 'source': source})

    @app.route('/api/viewer/add_bookmarks', methods=['POST'])
    def viewer_add_bookmarks():
        """Append a batch of bookmarks (used to commit auto-detected proposals)."""
        import uuid as _uuid
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        valid_ids = {p['id'] for p in session.get('pages', [])}
        bms = session.setdefault('bookmarks', [])
        for item in (data.get('bookmarks') or []):
            page_id = item.get('page_id', '')
            if page_id not in valid_ids:
                continue
            title = str(item.get('title', '') or '').strip() or 'Bookmark'
            y = item.get('y')
            try:    y = float(y) if y is not None else None
            except Exception: y = None
            try:    level = max(0, int(item.get('level', 0) or 0))
            except Exception: level = 0
            bms.append({'id': str(_uuid.uuid4()), 'title': title[:200],
                        'page_id': page_id, 'y': y, 'level': level})
        _recovery.mark_meta_dirty(session)
        return jsonify({'bookmarks': bms})

    # ── Full-document text search ───────────────────────────────────────────────

    @app.route('/api/viewer/search', methods=['POST'])
    def viewer_search():
        """Search *query* across every page in the session.

        Returns a list of match objects, each with:
          page_idx  — 0-based index into the session pages list
          page_id   — UUID of the page
          rects     — list of {x, y, w, h} in PDF user-space points (y=0 at bottom)
        """
        data       = request.json
        session_id = data.get('session_id', '')
        query      = (data.get('query') or '').strip()
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        if not query:
            return jsonify({'matches': [], 'total_rects': 0})

        all_matches = []
        total_rects = 0
        for idx, page_rec in enumerate(session['pages']):
            pdf_bytes = page_rec['pdf_bytes']

            # Edited scan (mixed scan+native): search the native edits AND the
            # untouched scanned text via OCR of the pristine source.  Checked
            # first so a tiny native edit can't route the page to plain OCR of
            # the current image (which still shows replaced text under covers).
            src_words = _ocr_source_words_for_mixed(session, page_rec)
            if src_words:
                rects = _search_page(pdf_bytes, query) \
                      + _search_ocr_words(src_words, query)
            elif _is_scanned_page(pdf_bytes):
                # Image-only page (no native text yet) — pure OCR.
                words = _get_ocr_words_cached(session, page_rec['id'], pdf_bytes)
                rects = _search_ocr_words(words, query)
            else:
                rects = _search_page(pdf_bytes, query)

            # Also search annotation text (sticky notes, free-text, and the
            # Text tool's page markup).  FreeText matches are positioned at
            # char precision via the annotation's appearance stream; sticky
            # notes highlight the note icon.
            rects.extend(_annot_text_match_rects(pdf_bytes, query))

            if rects:
                # Match rects are in raw content space; move them into the
                # display frame so highlights land correctly on a rotated page —
                # and on a cropped one, where the CropBox origin has to come off.
                rotate, raw_W, raw_H, off_x, off_y = _page_display_frame(pdf_bytes)
                if rotate or off_x or off_y:
                    rects = [_raw_rect_to_display(r, rotate, raw_W, raw_H,
                                                  off_x, off_y)
                             for r in rects]
                all_matches.append({
                    'page_idx': idx,
                    'page_id':  page_rec['id'],
                    'rects':    rects,
                })
                total_rects += len(rects)

        return jsonify({'matches': all_matches, 'total_rects': total_rects,
                        'ocr_used': True})

    # ── Full-document PDF for printing ─────────────────────────────────────────

    @app.route('/api/viewer/pdf', methods=['POST'])
    def viewer_pdf():
        """Merge session pages into a single PDF and return it for printing.

        An optional ``pages`` list of page IDs restricts the output to that
        selection (used by the in-app print dialog so the OS print preview only
        ever receives the chosen pages).  When omitted, every page is included
        as before.  Selected pages are emitted in document order regardless of
        the order the IDs arrive in, matching normal print behaviour.
        """
        data       = request.json
        session_id = data.get('session_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        wanted = data.get('pages')
        wanted = set(wanted) if wanted else None   # None ⇒ all pages

        writer = _pypdf.PdfWriter()
        for page_rec in session.get('pages', []):
            if wanted is not None and page_rec['id'] not in wanted:
                continue
            pdf_bytes = page_rec['pdf_bytes']
            reader    = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
            for page in reader.pages:
                writer.add_page(page)

        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)

        return Response(
            buf.read(),
            mimetype='application/pdf',
            headers={'Content-Disposition': 'inline; filename="document.pdf"'},
        )

    # ── Page character data for pointer-tool text selection ───────────────────

    @app.route('/api/viewer/page_text', methods=['POST'])
    def viewer_page_text():
        """Return character-level text with positions for pointer-tool selection."""
        data       = request.json
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        progress_id = data.get('progress_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404

        chars = _get_page_chars(pdf_bytes)
        ocr_used = False

        # Is this page's native text broken-encoding garbage — control codes with
        # no usable ToUnicode (e.g. docs/edit_problem.pdf)?  Then it is useless for
        # selection / copy-to-clipboard, and OCR of the rendered page is the only
        # source of real, word-split text.
        unmappable_native = _pdf_core.native_text_is_unmappable(pdf_bytes)

        # A scanned page needs Tesseract before its text is selectable — slow, so
        # report the OCR sub-stages under progress_id for the loading overlay.  A
        # native page never OCRs, so nothing is registered and the overlay stays
        # hidden (the client poll only shows it once progress appears).
        def _ocr_pcb(cur, total, label):
            set_progress(progress_id, cur, total, label)

        try:
            if unmappable_native:
                # Discard the garbage native chars entirely and OCR the CURRENT
                # rendered page — its glyphs (including any edits baked into the
                # raster) become real, selectable words.
                words = _get_ocr_words_cached(session, page_id, pdf_bytes, progress_cb=_ocr_pcb)
                if words:
                    chars    = [{'c': w['text'], 'x': w['x'], 'y': w['y'],
                                  'w': w['w'],   'h': w['h']} for w in words]
                    ocr_used = True
                else:
                    chars = []   # OCR found nothing — better empty than gibberish
            else:
                # Edited scan (mixed scan+native): add untouched scanned text from
                # the pristine source so it stays selectable alongside native edits.
                src_words = _ocr_source_words_for_mixed(session, page_rec, progress_cb=_ocr_pcb)
                # A source that was itself reopened from a save already shows
                # the earlier edits in its pixels — don't copy those words twice.
                src_words = _drop_words_under_chars(src_words, chars)
                if src_words:
                    chars = chars + [{'c': w['text'], 'x': w['x'], 'y': w['y'],
                                      'w': w['w'],   'h': w['h']} for w in src_words]
                    ocr_used = True
                elif chars and _is_mixed_scan(pdf_bytes):
                    # A scan saved with edits in it and opened again: nothing in
                    # memory remembers it was a scan, and it has native chars, so
                    # the count test below says "text page" and only the edited
                    # words were selectable.  OCR what is on the page now and keep
                    # the native chars for the words that have them.
                    words = _get_ocr_words_cached(session, page_id, pdf_bytes, progress_cb=_ocr_pcb)
                    words = _drop_words_under_chars(words, chars)
                    words = _drop_words_in_regions(words, page_rec.get('merge_regions'))
                    if words:
                        chars = chars + [{'c': w['text'], 'x': w['x'], 'y': w['y'],
                                          'w': w['w'],   'h': w['h']} for w in words]
                        ocr_used = True
                elif len(chars) < _SCANNED_CHAR_THRESHOLD:
                    # Image-only page (no native text) — convert OCR word boxes into
                    # the char-box format the selection logic expects.
                    words = _get_ocr_words_cached(session, page_id, pdf_bytes, progress_cb=_ocr_pcb)
                    if words:
                        chars    = [{'c': w['text'], 'x': w['x'], 'y': w['y'],
                                      'w': w['w'],   'h': w['h']} for w in words]
                        ocr_used = True
        finally:
            clear_progress(progress_id)

        # Char boxes are in raw content space; the viewer renders (and measures
        # the page) in the rotated display frame.  Move the boxes so selection
        # lands on the text on /Rotate 90/180/270 pages — and on cropped pages,
        # where pdfium reports CropBox DIMENSIONS but absolute-user-space
        # charboxes, so the origin has to come off explicitly.
        rotate, raw_W, raw_H, off_x, off_y = _page_display_frame(pdf_bytes)
        if rotate or off_x or off_y:
            chars = [_raw_rect_to_display(c, rotate, raw_W, raw_H, off_x, off_y)
                     for c in chars]

        return jsonify({'chars': chars, 'has_text': len(chars) > 0,
                        'ocr_used': ocr_used})

    # ── Open a selected table in Excel ──────────────────────────────────────────

    @app.route('/api/viewer/open_table', methods=['POST'])
    def viewer_open_table():
        """Write the selected table rows to a temp spreadsheet and open it in the
        OS default handler (Excel).  The browser <a download> path that the
        client used before is silently ignored by the embedded WebView2 window,
        so the desktop app must open the file natively here instead.

        Returns {opened: True} when the file was launched (Windows desktop app),
        or {opened: False, ...} so the client can fall back to a CSV download in
        a plain browser / non-Windows session."""
        data = request.json or {}
        rows = data.get('rows') or []
        if not rows:
            return jsonify({'error': 'No table data', 'error_key': 'err.no_table_data'}), 400

        import tempfile
        try:
            try:
                from openpyxl import Workbook
                wb = Workbook()
                ws = wb.active
                for r in rows:
                    ws.append(['' if c is None else str(c) for c in r])
                fd, path = tempfile.mkstemp(prefix='patchwork_table_', suffix='.xlsx')
                os.close(fd)
                wb.save(path)
            except ImportError:
                # openpyxl missing — fall back to a CSV the OS can still open.
                import csv as _csv
                fd, path = tempfile.mkstemp(prefix='patchwork_table_', suffix='.csv')
                with os.fdopen(fd, 'w', newline='', encoding='utf-8-sig') as f:
                    _csv.writer(f).writerows(rows)
        except Exception as exc:
            logger.exception('[open_table] failed to build spreadsheet')
            return jsonify({'error': str(exc)}), 500

        # Open in the OS default handler for the extension — Excel or
        # LibreOffice/OpenOffice Calc, whatever owns .xlsx.  Cross-platform:
        # os.startfile (Windows), `open` (macOS), `xdg-open` (Linux, incl. the
        # Flatpak portal).
        from platform_io import platform_paths
        if platform_paths.open_path_in_default_app(path):
            return jsonify({'opened': True})
        # No app registered / launch failed (e.g. a PC with no spreadsheet app) —
        # drop the temp file; the client will offer a native "save as CSV" instead.
        logger.warning('[open_table] could not open %s in a default app', path)
        try:
            os.remove(path)          # drop the temp file we couldn't open
        except OSError:
            pass
        return jsonify({'opened': False})

    # ── Form fields (AcroForm) ──────────────────────────────────────────────────

    @app.route('/api/viewer/form_fields', methods=['POST'])
    def viewer_form_fields():
        """Return all AcroForm widget field data for every page, keyed by page_id."""
        data       = request.json
        session_id = data.get('session_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        fields_by_page = {}
        for page_rec in session['pages']:
            fields = page_rec.get('form_fields', [])
            if not fields:
                continue
            # Stored geometry is absolute user space with a MediaBox page_h;
            # the viewer draws in the display frame.  Translate per page — and
            # only for pages that actually have fields, so the pdfium open this
            # costs is not paid by every page of the document.
            pdf_bytes = page_rec.get('pdf_bytes', b'')
            if pdf_bytes:
                rot, rW, rH, ox, oy = _page_display_frame(pdf_bytes)
                fields = [_field_rect_to_display(f, rot, rW, rH, ox, oy)
                          for f in fields]
            fields_by_page[page_rec['id']] = fields

        return jsonify({'fields_by_page': fields_by_page})

    # ── Page tools (Redact, and the rest of Phase 1 as they land) ────────────
    #
    # A page tool rewrites the page's CONTENT rather than adding an annotation
    # object, so it cannot be undone by deleting something — but it does not
    # need to be.  The viewer's undo stack snapshots whole page bytes
    # (_annot_push_undo), so pushing one before the rewrite gives a redaction
    # full Undo/Redo through the toolbar buttons with no new machinery.
    #
    # The geometry arrives in the VIEWER DISPLAY FRAME, as everything from the
    # viewer does.  apply_edits wants the EDITOR's convention instead — which is
    # absolute user space on an unrotated page, and the display frame on a
    # rotated one, because _editor_display_orientation moves the origin there.
    # _page_tool_offset reproduces that decision rather than re-deriving it.

    def _page_tool_offset(rotate, off_x, off_y):
        """(dx, dy) to add to display-frame coords to get what apply_edits wants.

        Mirrors editor_open's page_left/page_top: the CropBox origin on an
        unrotated page, and nothing on a rotated one (where the editor moves its
        own origin to the display frame).  Only valid when the geometry is
        supported at all — see _page_tool_refusal.
        """
        if rotate:
            return 0.0, 0.0
        return off_x, off_y

    def _page_tool_refusal(pdf_bytes):
        """An error response when a page is geometry the edit pipeline cannot
        place content on, else None.

        A page that is cropped AND rotated is explicitly unsupported by
        apply_edits (routes/editor.py logs 'cropped+rotated page; leaving coords
        in raw space') and measurement puts content up to 100px out.  For most
        tools that is a visible mistake the user can undo.  For REDACTION it is a
        security failure: the box lands somewhere else, nothing is covered, and
        the operation reports success while the text stays readable.

        So refuse.  The viewer's own annotation path handles this combination
        correctly (tests/test_crop_coords.py), so this is specifically about
        what apply_edits can be trusted with.
        """
        rot, _rW, _rH, ox, oy = _page_display_frame(pdf_bytes)
        if rot and (ox or oy):
            return jsonify({
                'error': 'This page is both cropped and rotated, which this tool '
                         'cannot place accurately. Reset the rotation or the crop '
                         'and try again.',
                'error_key': 'err.cropped_and_rotated',
            }), 400
        return None

    @app.route('/api/viewer/redact', methods=['POST'])
    @_serialised_on_session
    def viewer_redact():
        """Burn a redaction box into a page.

        Body: {session_id, page_id, rect: {x, y, x2, y2}} in DISPLAY-frame
        points (y-up), i.e. exactly what the viewer drew.

        This is destructive by design: apply_edits scrubs the native text under
        the box and _burn_redactions_into_images destroys the pixels inside any
        image XObject, so the content is gone rather than covered.  Undo
        restores the snapshot pushed here, but only while it is still in the
        session's history.
        """
        from routes.editor import apply_edits

        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        rect       = data.get('rect') or {}
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404

        refusal = _page_tool_refusal(pdf_bytes)
        if refusal:
            return refusal

        try:
            x1, y1 = float(rect.get('x', 0)),  float(rect.get('y', 0))
            x2, y2 = float(rect.get('x2', 0)), float(rect.get('y2', 0))
        except (TypeError, ValueError):
            return jsonify({'error': 'Bad rect'}), 400
        if abs(x2 - x1) < 1 or abs(y2 - y1) < 1:
            return jsonify({'error': 'Redaction area too small'}), 400

        rot, rW, rH, ox, oy = _page_display_frame(pdf_bytes)
        dx, dy = _page_tool_offset(rot, ox, oy)
        disp_w, disp_h = _display_page_size(rot, rW, rH)

        box = {'x':  min(x1, x2) + dx, 'y':  min(y1, y2) + dy,
               'x2': max(x1, x2) + dx, 'y2': max(y1, y2) + dy}

        # Snapshot BEFORE the rewrite — _annot_apply restores this on Ctrl+Z.
        _preserve_ocr_source(page_rec)
        _annot_push_undo(session, page_id)
        try:
            minimal_edits = {
                'deleted_ids': [], 'moved': [], 'text_changed': [],
                'markup': [], 'redacts': [box],
                'pdf_image_edits': [], 'original_elements': [],
                'watermark': None,
            }
            new_pdf, _ = apply_edits(pdf_bytes, minimal_edits, disp_w, disp_h)
            page_rec['pdf_bytes'] = new_pdf
            thumb = _render_thumb_b64(new_pdf)
            if thumb:
                page_rec['thumbnail'] = thumb
            _recovery.mark_dirty(session, page_id)
            u, r = _annot_counts(session)
            return jsonify({'success': True, 'thumbnail': thumb,
                            'undo_count': u, 'redo_count': r})
        except Exception as exc:
            logger.exception('[viewer_redact] failed')
            return jsonify({'error': str(exc)}), 500

    _WM_LAYOUTS = ('center', 'tiled')

    def _clean_watermark(cfg):
        """Validate and clamp a watermark config, or return (None, message).

        The values reach a content stream, so every one is bounded here rather
        than trusted: an absurd size or step count is a way to make the server
        emit an enormous stream.
        """
        if not isinstance(cfg, dict):
            return None, 'No watermark supplied'
        text = str(cfg.get('text', '') or '').strip()
        if not text:
            return None, 'Enter some watermark text first'
        if len(text) > 120:
            text = text[:120]
        try:
            size = float(cfg.get('size', 60))
            opacity = float(cfg.get('opacity', 0.25))
            angle = float(cfg.get('angleDeg', 45))
        except (TypeError, ValueError):
            return None, 'Bad watermark settings'
        layout = str(cfg.get('layout', 'center'))
        if layout not in _WM_LAYOUTS:
            layout = 'center'
        colour = str(cfg.get('color', '#808080'))
        if not _re.fullmatch(r'#[0-9a-fA-F]{6}', colour):
            colour = '#808080'
        return {
            'text':     text,
            'color':    colour,
            'opacity':  max(0.05, min(0.95, opacity)),
            'size':     max(6.0, min(400.0, size)),
            'angleDeg': max(-360.0, min(360.0, angle)),
            'layout':   layout,
        }, None

    @app.route('/api/viewer/watermark', methods=['POST'])
    @_serialised_on_session
    def viewer_watermark():
        """Stamp a watermark across ONE page.

        Body: {session_id, page_id, watermark: {text, color, opacity, size,
        angleDeg, layout}}.

        Unlike the other page tools this takes NO geometry: apply_edits derives
        the page centre from the CropBox itself, which is why a watermark lands
        correctly on all eight {plain,cropped}x{0,90,180,270} combinations and
        needs neither _page_tool_offset nor _page_tool_refusal.  Measured, not
        assumed — see tests/test_viewer_watermark.py.

        All-pages watermarking is deliberately NOT here: N pages would push N
        byte snapshots against a 30-entry / 64 MB history and "undo" would
        half-revert the document.  It stays a document-level command, outside
        the undo stack and behind a confirmation (see the plan, §3.5).
        """
        from routes.editor import apply_edits

        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404

        cfg, err = _clean_watermark(data.get('watermark'))
        if err:
            return jsonify({'error': err, 'error_key': 'err.watermark_settings'}), 400

        rot, rW, rH, _ox, _oy = _page_display_frame(pdf_bytes)
        disp_w, disp_h = _display_page_size(rot, rW, rH)

        _preserve_ocr_source(page_rec)
        _annot_push_undo(session, page_id)
        try:
            minimal_edits = {
                'deleted_ids': [], 'moved': [], 'text_changed': [],
                'markup': [], 'redacts': [],
                'pdf_image_edits': [], 'original_elements': [],
                'watermark': cfg,
            }
            new_pdf, _ = apply_edits(pdf_bytes, minimal_edits, disp_w, disp_h)
            page_rec['pdf_bytes'] = new_pdf
            thumb = _render_thumb_b64(new_pdf)
            if thumb:
                page_rec['thumbnail'] = thumb
            _recovery.mark_dirty(session, page_id)
            u, r = _annot_counts(session)
            return jsonify({'success': True, 'thumbnail': thumb,
                            'undo_count': u, 'redo_count': r})
        except Exception as exc:
            logger.exception('[viewer_watermark] failed')
            return jsonify({'error': str(exc)}), 500

    _TEXT_MAX_CHARS = 4000

    # ── Editing the page's OWN text (the Pointer) ────────────────────────────
    #
    # The editor panel loads a whole element model plus a hi-res render; the
    # viewer needs only the text runs, because it already has the page on
    # screen.  Skipping the image keeps the payload ~250 KB smaller per page,
    # which matters when this is loaded on demand as the user scrolls.
    #
    # The client names an element by ID and says what changed.  The SERVER
    # re-extracts to build `original_elements` rather than trusting geometry
    # from the client: those bboxes are what apply_edits paints over to hide the
    # old glyphs, so a wrong one leaves the original text showing through the
    # replacement.  Extraction is deterministic for a given set of bytes and the
    # session lock holds across the whole handler, so the IDs the client quotes
    # still refer to the same runs.

    def _page_elements(pdf_bytes, page_rec=None):
        """(text_items, page_top, page_left) for a page, using the editor's own
        extraction and origin rules so both panels agree on what a run is.

        **Edited text leaves a ghost.** apply_edits COVERS the original glyphs
        with a background-coloured rectangle rather than deleting them, so the
        old words are still in the content stream and extraction finds them
        again.  The editor deals with that by recording each covered bbox on the
        page record and filtering them out on re-open; this must do the same, or
        a second load offers the user the text they just replaced, sitting
        invisibly underneath its replacement.
        """
        from routes.editor import (_extract_text_and_graphics,
                                   _editor_display_orientation,
                                   _filter_covered_text_items)
        # On an edited scan, OCR the PRISTINE bytes and take native runs from
        # the current ones — otherwise the first edit hides the rest of the
        # page from the Pointer.  See _preserve_ocr_source.
        _w, page_h, _pt, _pb, text_items, _gi = _extract_text_and_graphics(
            pdf_bytes, _ocr_source_for(pdf_bytes, page_rec))
        if page_rec is not None:
            text_items = _filter_covered_text_items(
                text_items,
                page_rec.get('covered_text_bboxes', []),
                page_rec.get('merge_regions', []))
        cb = _pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].cropbox
        page_top, page_left = float(cb.top), float(cb.left)
        text_items, page_top, page_left = _editor_display_orientation(
            pdf_bytes, page_h, text_items, page_top, page_left)
        return text_items, page_top, page_left

    def _preserve_ocr_source(page_rec):
        """Stash a SCANNED page's pristine bytes before we inject native text.

        `_extract_text_and_graphics` decides whether to OCR from a char count,
        so the moment `apply_edits` writes one real Tj into a scan the page
        stops looking scanned: the next extraction takes the native path, finds
        only the run we just injected, and every OTHER word on the page becomes
        uneditable.  One edit and the Pointer goes dead for the rest of the
        page — measured on a 178-word invoice, where the second load returned 1
        item.  The editor already solved this by keeping the original and OCRing
        THAT (`ocr_source_bytes`, set in editor_apply); the viewer has to do the
        same, and at the same moment — before the first rewrite, while the bytes
        are still the scan.

        Storing None for a native page is deliberate: it is the "already
        checked" marker, so this costs one pdfium open per page, not one per
        mutation.
        """
        if 'ocr_source_bytes' in page_rec:
            return
        try:
            from routes.editor import is_image_only_page
            cur = page_rec.get('pdf_bytes', b'')
            # A MIXED page (a scan reopened with earlier edits in it) is kept
            # too: its pixels are the best record of the scan we will ever get,
            # and stashing them means its OCR is cached across this session's
            # edits instead of redone on every rewrite.
            page_rec['ocr_source_bytes'] = cur if (
                cur and (is_image_only_page(cur) or _is_mixed_scan(cur))) else None
        except Exception:
            logger.exception('[preserve_ocr_source] scanned-page check failed')
            page_rec['ocr_source_bytes'] = None

    def _ocr_source_for(pdf_bytes, page_rec):
        """The bytes `_extract_text_and_graphics` should OCR for this page, or
        None for a native page.

        The stash when there is one — unless the page has changed SIZE since
        (a crop), because OCR boxes from before it would land in the wrong
        place; text selection already refuses that case for the same reason.
        Otherwise the current bytes when the page is a mixed scan that simply
        was not stashed in this session: the reopen-after-save case.
        """
        src = (page_rec or {}).get('ocr_source_bytes')
        if src and src != pdf_bytes and _page_dims(src) != _page_dims(pdf_bytes):
            logger.info('[OCR_MIX] ignoring pre-crop OCR source for %s',
                        (page_rec or {}).get('id'))
            src = None
        if src is None and _is_mixed_scan(pdf_bytes):
            src = pdf_bytes
        return src

    def _record_covered(page_rec, orig, new_text=None):
        """Remember that `orig`'s glyphs are now hidden under a cover rect.

        Mirrors what editor_apply stores, including superseding an earlier entry
        for the same spot so a run edited twice does not accumulate stale
        new_text values.
        """
        from routes.editor import GHOST_OVERLAP_THRESH

        def _overlap_frac(a, b):
            # Same measure editor_apply uses: the fraction of A covered by B.
            # (Its own copy is nested inside that handler, so not importable.)
            ax1, ay1 = float(a.get('x', 0)), float(a.get('y', 0))
            ax2, ay2 = float(a.get('x2', ax1)), float(a.get('y2', ay1))
            bx1, by1 = float(b.get('x', 0)), float(b.get('y', 0))
            bx2, by2 = float(b.get('x2', bx1)), float(b.get('y2', by1))
            aw, ah = max(ax2 - ax1, 1.0), max(ay2 - ay1, 1.0)
            ox = max(0.0, min(bx2, ax2) - max(bx1, ax1))
            oy = max(0.0, min(by2, ay2) - max(by1, ay1))
            return (ox * oy) / (aw * ah)

        entry_ = {'x': float(orig.get('x', 0)), 'y': float(orig.get('y', 0)),
                  'x2': float(orig.get('x2', orig.get('x', 0))),
                  'y2': float(orig.get('y2', orig.get('y', 0)))}
        if new_text is not None:
            entry_['new_text'] = new_text
        prev = [b for b in page_rec.get('covered_text_bboxes', [])
                if _overlap_frac(b, entry_) < GHOST_OVERLAP_THRESH]
        page_rec['covered_text_bboxes'] = prev + [entry_]

    @app.route('/api/viewer/page_elements', methods=['POST'])
    def viewer_page_elements():
        """The page's editable text runs, for the Pointer's hit-areas.

        Read-only, so it is deliberately NOT serialised — it takes no lock and
        blocks nothing.  On a scanned page the underlying extraction runs OCR,
        which is why the client loads this for one page on demand rather than
        for everything on screen.
        """
        data       = request.json or {}
        session    = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pdf_bytes, rec = _pdf_bytes_from_session(session, data.get('page_id', ''))
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        try:
            items, page_top, page_left = _page_elements(pdf_bytes, rec)
        except Exception as exc:
            logger.exception('[viewer_page_elements] extraction failed')
            return jsonify({'error': str(exc)}), 500
        # Only what the overlay needs: geometry, the words, and the style the
        # panel shows when a run is selected.
        slim = [{'id': t.get('id'), 'text': t.get('text', ''),
                 'x': t.get('x'), 'y': t.get('y'),
                 'x2': t.get('x2'), 'y2': t.get('y2'),
                 'size': t.get('size'), 'font_size': t.get('font_size'),
                 'color': t.get('color', '#000000'),
                 'rl_font': t.get('rl_font', 'Helvetica')}
                for t in items
                if t.get('type') == 'text' and not t.get('deleted')
                and t.get('x') is not None]
        return jsonify({'items': slim, 'page_top': page_top,
                        'page_left': page_left})

    @app.route('/api/viewer/edit_text', methods=['POST'])
    @_serialised_on_session
    def viewer_edit_text():
        """Change, move, restyle or delete one of the page's own text runs.

        Body: {session_id, page_id, id, op, ...} where op is
          'text'   — {text}            replace the words
          'style'  — {size?, color?, rl_font?}
          'move'   — {x, y}            new bottom-left, DISPLAY frame
          'delete' —                   remove it

        One operation, one byte snapshot, one Ctrl+Z — the same contract as
        every other page tool, rather than the editor's stage-then-Apply model.
        """
        from routes.editor import apply_edits

        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        el_id      = data.get('id', '')
        op         = data.get('op', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404

        refusal = _page_tool_refusal(pdf_bytes)
        if refusal:
            return refusal

        try:
            items, _pt, _pl = _page_elements(pdf_bytes, page_rec)
        except Exception as exc:
            logger.exception('[viewer_edit_text] extraction failed')
            return jsonify({'error': str(exc)}), 500

        target = next((t for t in items if t.get('id') == el_id), None)
        if target is None:
            return jsonify({'error': 'That text is no longer on the page',
                            'error_key': 'err.text_run_gone'}), 404
        # Ids are positional, so after anything rewrites the page (an undo, a
        # rotate) a stale id can name a DIFFERENT run.  The client says what it
        # thinks it is editing; refuse rather than rewrite the wrong words.
        if 'expect' in data and str(data.get('expect')) != str(target.get('text', '')):
            return jsonify({'error': 'That text is no longer on the page',
                            'error_key': 'err.text_run_gone'}), 409

        rot, rW, rH, ox, oy = _page_display_frame(pdf_bytes)
        disp_w, disp_h = _display_page_size(rot, rW, rH)
        dx, dy = _page_tool_offset(rot, ox, oy)

        edits = {'deleted_ids': [], 'moved': [], 'text_changed': [],
                 'markup': [], 'redacts': [], 'pdf_image_edits': [],
                 'original_elements': items, 'watermark': None}

        if op == 'delete':
            edits['deleted_ids'] = [el_id]
        elif op == 'move':
            try:
                edits['moved'] = [{'id': el_id,
                                   'x': float(data['x']) + dx,
                                   'y': float(data['y']) + dy}]
            except (KeyError, TypeError, ValueError):
                return jsonify({'error': 'Bad move'}), 400
        elif op in ('text', 'style'):
            ch = {'id': el_id,
                  'text': str(data.get('text', target.get('text', '')))[:_TEXT_MAX_CHARS],
                  'color': target.get('color') or '#000000',
                  'rl_font': target.get('rl_font') or 'Helvetica'}
            if op == 'text' and not ch['text'].strip():
                # Emptying a run is a delete; doing it as a text change would
                # leave a covered blank where the words were.
                edits['deleted_ids'] = [el_id]
            else:
                if data.get('color') and _re.fullmatch(r'#[0-9a-fA-F]{6}',
                                                       str(data['color'])):
                    ch['color'] = data['color']
                if data.get('rl_font'):
                    ch['rl_font'] = str(data['rl_font'])
                if data.get('size'):
                    try:
                        ch['size'] = max(4.0, min(400.0, float(data['size'])))
                    except (TypeError, ValueError):
                        pass
                edits['text_changed'] = [ch]
        else:
            return jsonify({'error': 'Unknown operation'}), 400

        _preserve_ocr_source(page_rec)
        _annot_push_undo(session, page_id)
        try:
            new_pdf, _inject = apply_edits(
                pdf_bytes, edits, disp_w, disp_h,
                covered_text_bboxes=page_rec.get('covered_text_bboxes'))
            page_rec['pdf_bytes'] = new_pdf
            # The originals are covered, not removed — remember where, so the
            # next page_elements does not offer them again (see _page_elements).
            if edits['text_changed']:
                _record_covered(page_rec, target, edits['text_changed'][0]['text'])
            else:
                _record_covered(page_rec, target)
            thumb = _render_thumb_b64(new_pdf)
            if thumb:
                page_rec['thumbnail'] = thumb
            _recovery.mark_dirty(session, page_id)
            u, r = _annot_counts(session)
            # apply_edits condenses a replacement that would not fit the space
            # its original occupied.  Report a SHRUNK one so the user is not
            # left wondering why the words came out smaller; mild condensing is
            # not visible and does not need saying.
            fitted = (edits['text_changed'][0].get('_fitted')
                      if edits['text_changed'] else None)
            body = {'success': True, 'thumbnail': thumb,
                    'undo_count': u, 'redo_count': r}
            if fitted and fitted.get('overflows'):
                body['too_long_for_space'] = fitted
            elif fitted and fitted['size'] < fitted['orig_size'] - 0.05:
                body['shrunk_to_fit'] = fitted
            return jsonify(body)
        except Exception as exc:
            logger.exception('[viewer_edit_text] failed')
            return jsonify({'error': str(exc)}), 500

    @app.route('/api/viewer/edit_block', methods=['POST'])
    @_serialised_on_session
    def viewer_edit_block():
        """Replace several of the page's own text runs — a row, or an area
        spanning rows — with ONE wrapped block of text.  v2.24's editor called
        this "Merge row"; the merge into the viewer dropped it.

        Body: {session_id, page_id, ids: [...], text, lines: [...],
               box: {x1, y1, x2, y2}}
          ids    the runs the user's box captured (from page_elements)
          lines  `text` already wrapped by the client, which is the only side
                 that can measure the font it previews with (same contract as
                 the Text Box tool)
          box    the user's box, in the SAME frame as the page_elements items
                 (PDF y-up).  That is the frame apply_edits takes original
                 elements in, so nothing here is offset — the reverse of the
                 single-run move, whose client coordinates are the display
                 frame.

        Style is taken from the runs, not the client: median em size, the most
        common font and the most common colour, so a white header stays white.
        One byte snapshot, so one Ctrl+Z restores every word.
        """
        from routes.editor import apply_edits

        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404
        refusal = _page_tool_refusal(pdf_bytes)
        if refusal:
            return refusal

        ids = data.get('ids')
        if not isinstance(ids, list) or not ids or len(ids) > 2000:
            return jsonify({'error': 'Nothing selected',
                            'error_key': 'err.text_run_gone'}), 400
        ids = [str(i) for i in dict.fromkeys(ids)]
        try:
            items, _pt, _pl = _page_elements(pdf_bytes, page_rec)
        except Exception as exc:
            logger.exception('[viewer_edit_block] extraction failed')
            return jsonify({'error': str(exc)}), 500
        by_id = {t.get('id'): t for t in items if t.get('type') == 'text'}
        picked = [by_id[i] for i in ids if i in by_id]
        if len(picked) != len(ids):
            return jsonify({'error': 'That text is no longer on the page',
                            'error_key': 'err.text_run_gone'}), 404
        # Positional ids again (see viewer_edit_text): check they still name
        # the words the user boxed.
        expect = data.get('expect')
        if isinstance(expect, list) and (
                len(expect) != len(picked) or
                any(str(e) != str(t.get('text', '')) for e, t in zip(expect, picked))):
            return jsonify({'error': 'That text is no longer on the page',
                            'error_key': 'err.text_run_gone'}), 409

        text = str(data.get('text', '') or '')[:_TEXT_MAX_CHARS]
        edits = {'deleted_ids': list(ids), 'moved': [], 'text_changed': [],
                 'markup': [], 'redacts': [], 'pdf_image_edits': [],
                 'original_elements': items, 'watermark': None}

        # Footprint of the captured words — what gets covered, whatever the
        # user's (padded, adjustable) box looks like.
        fx1 = min(float(t['x']) for t in picked)
        fy1 = min(float(t['y']) for t in picked)
        fx2 = max(float(t['x2']) for t in picked)
        fy2 = max(float(t['y2']) for t in picked)
        cover = {'x1': fx1 - 1.5, 'y1': fy1 - 1.5, 'x2': fx2 + 1.5, 'y2': fy2 + 1.5}

        if text.strip():
            try:
                box = data.get('box') or {}
                bx1, by1 = float(box.get('x1', fx1)), float(box.get('y1', fy1))
                bx2, by2 = float(box.get('x2', fx2)), float(box.get('y2', fy2))
            except (TypeError, ValueError):
                return jsonify({'error': 'Bad box'}), 400
            bx1, bx2 = min(bx1, bx2), max(bx1, bx2)
            by1, by2 = min(by1, by2), max(by1, by2)

            sizes = sorted(float(t.get('font_size') or float(t.get('size') or 8) * 1.35)
                           for t in picked)
            size = max(4.0, min(400.0, round(sizes[len(sizes) // 2], 2)))

            def _mode(key, default):
                counts = {}
                for t in picked:
                    v = t.get(key) or default
                    counts[v] = counts.get(v, 0) + 1
                return max(counts, key=counts.get)

            rl_font = _mode('rl_font', 'Helvetica')
            color = _mode('color', '#000000')
            if not _re.fullmatch(r'#[0-9a-fA-F]{6}', str(color)):
                color = '#000000'

            lines = data.get('lines')
            if not isinstance(lines, list) or not lines:
                lines = text.split('\n')
            lines = [str(ln) for ln in lines][:400]

            # Anchor the first baseline to where the original top row's glyphs
            # actually sat (not the padded box top) — see apply_edits'
            # _capAnchor.  Stored as offsets from the box origin.
            heights = sorted(max(1.0, float(t['y2']) - float(t['y'])) for t in picked)
            band = heights[len(heights) // 2] * 0.6
            top_row = [t for t in picked if fy2 - float(t['y2']) <= band] or picked
            src_left = min(float(t['x']) for t in top_row)

            # Calibrate the size against the rows being replaced.  The em is an
            # estimate from OCR box heights and runs a few percent large on a
            # light face ("Line Amount" set 56.0pt wide where the original was
            # 53.3pt), which is visible beside untouched neighbours and pushes a
            # right-aligned block past its column's left edge.  Re-set the
            # original rows in our font and scale so they measure what they
            # measured on the page.  Clamped: a bad OCR box must not wreck it.
            from platform_io import font_assets as _fa
            ratios = []
            for row in _block_rows(picked, band):
                row = sorted(row, key=lambda t: float(t['x']))
                natural = _fa.string_width(rl_font, ' '.join(t.get('text', '') for t in row), size)
                span = max(float(t['x2']) for t in row) - min(float(t['x']) for t in row)
                if natural > 1.0 and span > 1.0:
                    ratios.append(span / natural)
            if ratios:
                ratios.sort()
                size = round(size * max(0.85, min(1.05, ratios[len(ratios) // 2])), 2)

            # Keep the original rows' pitch.  A default 1.35 em spread a tight
            # two-line table header well below its band.
            line_h = _block_row_pitch(picked, band) or size * 1.35
            line_h = max(size * 0.8, min(size * 2.5, line_h))
            # And the way they were aligned: a right-aligned or centred header
            # cell used to come back hard left.
            align, align_x = _block_alignment(picked, band, size)

            edits['markup'] = [{
                'id': 'mk_block_' + uuid.uuid4().hex[:12],
                'text': text, 'lines': lines, 'multiline': True,
                'x': bx1, 'y': by2,
                'boxW': round(bx2 - bx1, 2),
                'boxH': round(len(lines) * line_h + 2, 2),
                'lineH': round(line_h, 3),
                'size': size, 'color': color, 'rl_font': rl_font,
                '_coverRect': cover,
                '_srcOcr': any(t.get('ocr') for t in picked),
                '_capAnchor': True,
                '_anchorDX': round(src_left - bx1, 3),
                '_anchorDY': round(fy2 - by2, 3),
                'align': align,
            }]
            if align_x is not None:
                edits['markup'][0]['_alignDX'] = round(align_x - bx1, 3)

        _preserve_ocr_source(page_rec)
        _annot_push_undo(session, page_id)
        try:
            new_pdf, _inject = apply_edits(
                pdf_bytes, edits, *_display_page_size(*_page_display_frame(pdf_bytes)[:3]),
                covered_text_bboxes=page_rec.get('covered_text_bboxes'))
            page_rec['pdf_bytes'] = new_pdf
            if edits['markup']:
                # The footprint, not per-word ghost boxes: re-OCR can segment the
                # old words differently from the ids we deleted, and a per-word
                # box would also hide the new block's own line where it runs past
                # the words it replaced.  Mirrors editor_apply's merge_regions.
                page_rec['merge_regions'] = list(page_rec.get('merge_regions', [])) + [
                    [cover['x1'], cover['y1'], cover['x2'], cover['y2']]]
            else:
                for t in picked:
                    _record_covered(page_rec, t)
            thumb = _render_thumb_b64(new_pdf)
            if thumb:
                page_rec['thumbnail'] = thumb
            _recovery.mark_dirty(session, page_id)
            u, r = _annot_counts(session)
            return jsonify({'success': True, 'thumbnail': thumb,
                            'undo_count': u, 'redo_count': r})
        except Exception as exc:
            logger.exception('[viewer_edit_block] failed')
            return jsonify({'error': str(exc)}), 500

    @app.route('/api/viewer/add_text', methods=['POST'])
    @_serialised_on_session
    def viewer_add_text():
        """Stamp text into the page — the Text and Text Box tools.

        Body: {session_id, page_id, item: {...}} where `item` is a markup item in
        DISPLAY-frame coordinates:

          single line  {text, x, y, size, color, rl_font}
                       x,y is the text BASELINE.
          text box     {..., multiline: true, boxW, boxH, lines: [...]}
                       x,y is the box's TOP-LEFT, and `lines` is the text already
                       wrapped to the box width by the client, because only the
                       client can measure the font it is previewing with.

        This is page CONTENT, not a FreeText annotation: it cannot be selected or
        deleted afterwards, which is exactly what distinguishes it from the
        viewer's own Text annotation tool.  One byte snapshot, so one Ctrl+Z.
        """
        from routes.editor import apply_edits

        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        item       = dict(data.get('item') or {})
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404

        refusal = _page_tool_refusal(pdf_bytes)
        if refusal:
            return refusal

        text = str(item.get('text', '') or '').strip()
        if not text:
            return jsonify({'error': 'No text supplied',
                            'error_key': 'err.no_text'}), 400
        item['text'] = text[:_TEXT_MAX_CHARS]

        try:
            item['size'] = max(4.0, min(400.0, float(item.get('size', 12))))
            item['x'] = float(item.get('x', 0))
            item['y'] = float(item.get('y', 0))
        except (TypeError, ValueError):
            return jsonify({'error': 'Bad text placement'}), 400
        if not _re.fullmatch(r'#[0-9a-fA-F]{6}', str(item.get('color', ''))):
            item['color'] = '#000000'
        if not isinstance(item.get('rl_font'), str) or not item['rl_font']:
            item['rl_font'] = 'Helvetica'

        if item.get('multiline'):
            lines = item.get('lines')
            if not isinstance(lines, list) or not lines:
                # No usable wrap from the client — fall back to the raw text so
                # something lands rather than nothing.
                item['lines'] = text.split('\n')
            else:
                item['lines'] = [str(ln) for ln in lines][:400]

        rot, rW, rH, ox, oy = _page_display_frame(pdf_bytes)
        disp_w, disp_h = _display_page_size(rot, rW, rH)
        dx, dy = _page_tool_offset(rot, ox, oy)
        item['x'] += dx
        item['y'] += dy
        item.setdefault('id', 'mk_' + uuid.uuid4().hex[:12])

        _preserve_ocr_source(page_rec)
        _annot_push_undo(session, page_id)
        try:
            new_pdf, _ = apply_edits(pdf_bytes, {
                'deleted_ids': [], 'moved': [], 'text_changed': [],
                'markup': [item], 'redacts': [],
                'pdf_image_edits': [], 'original_elements': [],
                'watermark': None,
            }, disp_w, disp_h)
            page_rec['pdf_bytes'] = new_pdf
            thumb = _render_thumb_b64(new_pdf)
            if thumb:
                page_rec['thumbnail'] = thumb
            _recovery.mark_dirty(session, page_id)
            u, r = _annot_counts(session)
            return jsonify({'success': True, 'thumbnail': thumb,
                            'undo_count': u, 'redo_count': r})
        except Exception as exc:
            logger.exception('[viewer_add_text] failed')
            return jsonify({'error': str(exc)}), 500

    @app.route('/api/viewer/crop', methods=['POST'])
    @_serialised_on_session
    def viewer_crop():
        """Set a page's CropBox from a box drawn in the viewer.

        Body: {session_id, page_id, rect: {x, y, x2, y2}} in DISPLAY-frame
        points (y-up), or {session_id, page_id, clear: true} to restore the full
        page.

        Unlike the other page tools this changes the page's SIZE, so the reply
        carries the new dimensions and the page record is updated with them —
        the client cannot lay the page out correctly otherwise.

        Cropping is not destructive in the PDF sense (the content outside the box
        is still in the file), but it is a byte rewrite like the rest and joins
        the same undo stack.
        """
        from routes.editor import apply_edits

        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404

        refusal = _page_tool_refusal(pdf_bytes)
        if refusal:
            return refusal

        rot, rW, rH, ox, oy = _page_display_frame(pdf_bytes)
        disp_w, disp_h = _display_page_size(rot, rW, rH)

        if data.get('clear'):
            # Restore the CropBox to the MediaBox.  Done here rather than through
            # apply_edits, which only ever narrows it.
            from pypdf.generic import NameObject, ArrayObject, FloatObject
            _annot_push_undo(session, page_id)
            try:
                reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
                writer = _pypdf.PdfWriter()
                writer.append(reader)
                pg = writer.pages[0]
                mb = pg.mediabox
                pg[NameObject('/CropBox')] = ArrayObject(
                    [FloatObject(mb.left), FloatObject(mb.bottom),
                     FloatObject(mb.right), FloatObject(mb.top)])
                out = io.BytesIO()
                writer.write(out)
                new_pdf = out.getvalue()
            except Exception as exc:
                logger.exception('[viewer_crop] clear failed')
                return jsonify({'error': str(exc)}), 500
        else:
            rect = data.get('rect') or {}
            try:
                x1, y1 = float(rect.get('x', 0)),  float(rect.get('y', 0))
                x2, y2 = float(rect.get('x2', 0)), float(rect.get('y2', 0))
            except (TypeError, ValueError):
                return jsonify({'error': 'Bad rect'}), 400
            # A crop smaller than this is almost certainly a stray drag, and a
            # near-zero CropBox makes the page unrenderable.
            if abs(x2 - x1) < 10 or abs(y2 - y1) < 10:
                return jsonify({'error': 'Crop area too small',
                                'error_key': 'err.crop_too_small'}), 400

            dx, dy = _page_tool_offset(rot, ox, oy)
            crop = {'x':  min(x1, x2) + dx, 'y':  min(y1, y2) + dy,
                    'x2': max(x1, x2) + dx, 'y2': max(y1, y2) + dy}

            _annot_push_undo(session, page_id)
            try:
                new_pdf, _ = apply_edits(pdf_bytes, {
                    'deleted_ids': [], 'moved': [], 'text_changed': [],
                    'markup': [], 'redacts': [],
                    'pdf_image_edits': [], 'original_elements': [],
                    'watermark': None, 'crop': crop,
                }, disp_w, disp_h)
            except Exception as exc:
                logger.exception('[viewer_crop] failed')
                return jsonify({'error': str(exc)}), 500

        page_rec['pdf_bytes'] = new_pdf
        w, h = _refresh_page_size(page_rec, new_pdf)
        thumb = _render_thumb_b64(new_pdf)
        if thumb:
            page_rec['thumbnail'] = thumb
        _recovery.mark_dirty(session, page_id)
        u, r = _annot_counts(session)
        return jsonify({'success': True, 'thumbnail': thumb,
                        'width_pts': w, 'height_pts': h,
                        'undo_count': u, 'redo_count': r})

    @app.route('/api/viewer/watermark_all', methods=['POST'])
    @_serialised_on_session
    def viewer_watermark_all():
        """Stamp a watermark across EVERY page — a document-level command.

        Deliberately not a panel tool and deliberately OUTSIDE the undo stack.
        One byte snapshot per page against a 30-entry / 64 MB history would be
        trimmed on any real document, so Ctrl+Z would half-revert it — worse
        than no undo at all (see the plan, §3.5).

        Because it cannot be undone it also CLEARS the history: leaving the
        earlier entries would let one Ctrl+Z restore a single page to a snapshot
        taken before the watermark, silently removing it from that page alone.
        The caller confirms first and is told both things.

        Applies ONCE per page.  The editor's sibling route was driven by a
        client loop that called it once per page while the route itself looped
        over every page, so an N-page document got N stacked watermarks on every
        page — measurably darker each time.  Do not reintroduce that shape.
        """
        from routes.editor import apply_edits

        data       = request.json or {}
        session_id = data.get('session_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        cfg, err = _clean_watermark(data.get('watermark'))
        if err:
            return jsonify({'error': err, 'error_key': 'err.watermark_settings'}), 400

        updated, failed = [], 0
        for page_rec in session.get('pages', []):
            pdf_bytes = page_rec.get('pdf_bytes', b'')
            if not pdf_bytes:
                continue
            try:
                _preserve_ocr_source(page_rec)
                rot, rW, rH, _ox, _oy = _page_display_frame(pdf_bytes)
                disp_w, disp_h = _display_page_size(rot, rW, rH)
                new_pdf, _ = apply_edits(pdf_bytes, {
                    'deleted_ids': [], 'moved': [], 'text_changed': [],
                    'markup': [], 'redacts': [],
                    'pdf_image_edits': [], 'original_elements': [],
                    'watermark': cfg,
                }, disp_w, disp_h)
                page_rec['pdf_bytes'] = new_pdf
                thumb = _render_thumb_b64(new_pdf)
                if thumb:
                    page_rec['thumbnail'] = thumb
                _recovery.mark_dirty(session, page_rec['id'])
                updated.append({'id': page_rec['id'], 'thumbnail': thumb or ''})
            except Exception as exc:
                failed += 1
                logger.warning('[viewer_watermark_all] page %s failed: %s',
                               page_rec.get('id'), exc)

        if updated:
            # Not undoable, so the history must not survive it (see docstring).
            session['annot_undo'] = []
            session['annot_redo'] = []
        u, r = _annot_counts(session)
        return jsonify({'success': True, 'updated': updated, 'failed': failed,
                        'undo_count': u, 'redo_count': r})

    # ── Form-field placement from the viewer's Field tool ────────────────────
    #
    # The stored fields are in absolute PDF user space; the viewer speaks the
    # display frame.  The conversion happens here, once, on the way in and out
    # — one wire convention, not a route that speaks two (the drift that
    # displaced every annotation on a cropped page; see CLAUDE.md, "Viewer page
    # coordinates").  The Editor's own /api/editor/*_form_field routes, which
    # spoke user space, were removed with it in v3.1.
    #
    # These are serialised: a field write can land concurrently with an
    # annotation mutation on the same page bytes, and a bare read-modify-write
    # loses one of them.

    def _page_and_frame(session, page_id):
        """(page_rec, frame) for a page, or (None, None)."""
        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return None, None
        return page_rec, _page_display_frame(pdf_bytes)

    @app.route('/api/viewer/add_form_field', methods=['POST'])
    @_serialised_on_session
    def viewer_add_form_field():
        """Place a new form-field widget from the viewer's Field tool.

        Body: {session_id, page_id, field: {type, x1, y1, x2, y2, …}} with the
        rect in DISPLAY-frame coordinates.  Echoes the stored field back in the
        same frame so the caller can draw it without a reload.
        """
        import uuid as _uuid
        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        field      = dict(data.get('field') or {})
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        if not field.get('type'):
            return jsonify({'error': 'No field type supplied'}), 400

        page_rec, frame = _page_and_frame(session, page_id)
        if page_rec is None:
            return jsonify({'error': 'Page not found'}), 404
        rot, rW, rH, ox, oy = frame

        stored = _field_rect_to_raw(field, rot, rW, rH, ox, oy)
        stored['id']     = str(_uuid.uuid4())
        stored['source'] = 'new'
        if not stored.get('name'):
            stored['name'] = f"{stored['type']}_{stored['id'][:8]}"

        page_rec['form_fields'] = page_rec.get('form_fields', [])
        page_rec['form_fields'].append(stored)
        page_rec['pdf_bytes'] = _form_utils.add_field_to_page_pdf(
            page_rec['pdf_bytes'], stored)
        _recovery.mark_dirty(session, page_id)
        return jsonify({'success': True,
                        'field': _field_rect_to_display(stored, rot, rW, rH, ox, oy)})

    @app.route('/api/viewer/update_form_field', methods=['POST'])
    @_serialised_on_session
    def viewer_update_form_field():
        """Move / resize / rename a field.  Geometry in `updates` is display-frame."""
        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        field_id   = data.get('field_id', '')
        updates    = dict(data.get('updates') or {})
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        page_rec, frame = _page_and_frame(session, page_id)
        if page_rec is None:
            return jsonify({'error': 'Page not found'}), 404
        rot, rW, rH, ox, oy = frame

        target = next((f for f in page_rec.get('form_fields', [])
                       if f['id'] == field_id), None)
        if target is None:
            return jsonify({'error': 'Field not found',
                            'error_key': 'err.field_not_found'}), 404

        moved = bool({'x1', 'y1', 'x2', 'y2'} & set(updates))
        if moved:
            # Convert only the geometry, and only when all four corners are
            # present — a partial rect cannot be transformed meaningfully.
            merged = {k: updates.get(k, target.get(k)) for k in
                      ('x1', 'y1', 'x2', 'y2')}
            raw = _field_rect_to_raw(merged, rot, rW, rH, ox, oy)
            updates.update({k: raw[k] for k in ('x1', 'y1', 'x2', 'y2')})
        updates.pop('page_h', None)

        old_name = target['name']
        target.update(updates)
        if moved or ({'name', 'type', 'options', 'font_size', 'label'} & set(updates)):
            page_rec['pdf_bytes'] = _form_utils.delete_field_from_page_pdf(
                page_rec['pdf_bytes'], old_name)
            page_rec['pdf_bytes'] = _form_utils.add_field_to_page_pdf(
                page_rec['pdf_bytes'], target)
            _recovery.mark_dirty(session, page_id)
        return jsonify({'success': True,
                        'field': _field_rect_to_display(target, rot, rW, rH, ox, oy)})

    @app.route('/api/viewer/delete_form_field', methods=['POST'])
    @_serialised_on_session
    def viewer_delete_form_field():
        """Remove a field.  No geometry, so no frame conversion."""
        data       = request.json or {}
        session_id = data.get('session_id', '')
        page_id    = data.get('page_id', '')
        field_id   = data.get('field_id', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        pdf_bytes, page_rec = _pdf_bytes_from_session(session, page_id)
        if not pdf_bytes:
            return jsonify({'error': 'Page not found'}), 404

        fields = page_rec.get('form_fields', [])
        target = next((f for f in fields if f['id'] == field_id), None)
        if target is None:
            return jsonify({'error': 'Field not found',
                            'error_key': 'err.field_not_found'}), 404
        page_rec['pdf_bytes'] = _form_utils.delete_field_from_page_pdf(
            page_rec['pdf_bytes'], target['name'])
        page_rec['form_fields'] = [f for f in fields if f['id'] != field_id]
        _recovery.mark_dirty(session, page_id)
        return jsonify({'success': True})

    @app.route('/api/viewer/fill_form', methods=['POST'])
    def viewer_fill_form():
        """Update form field values in the session.
        Body: {session_id, values: {field_id: value_string}}
        """
        data       = request.json
        session_id = data.get('session_id', '')
        values     = data.get('values', {})
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        updated = 0
        for page_rec in session['pages']:
            for field in page_rec.get('form_fields', []):
                if field['id'] in values:
                    field['value'] = values[field['id']]
                    updated += 1

        if updated:
            # Filled values are session state written into the PDF only at save
            # time — snapshot them so a crash does not lose the filled form.
            _recovery.mark_meta_dirty(session)
        return jsonify({'success': True, 'updated': updated})

