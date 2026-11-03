"""
pdf_core.py — shared document/session core for Patchwork PDF.

Holds everything that is not specific to one UI panel: the in-memory session
store, document ingestion/conversion, thumbnail rendering, metadata/outlines,
encryption, save/merge helpers, the recent-files (MRU) list, and the shared
document API routes (upload/open/save/rotate/compress/metadata/session
lifecycle/encryption).  entry.py imports the state and helpers it needs and
calls register_core_routes(); layout.py builds on these helpers too.

This module imports nothing from entry.py (or layout/viewer/editor), so the
dependency graph stays acyclic: entry -> {pdf_core, layout, viewer, editor}.
"""

import os
import io
import base64
import uuid
import tempfile
import time
import sys
import threading
import json as _json
import logging

import pypdf
from pypdf.constants import UserAccessPermissions as _AccessPermissions
from pdf import form_utils as _form_utils
from bootstrap import recovery as _recovery
from flask import request, jsonify, send_file

logger = logging.getLogger(__name__)

# ── Shared mutable state ──────────────────────────────────────────────────────
# In-memory session storage (session_id -> {pages, first_file_path, ...}).
sessions = {}

# Scratch dir for browser uploads.  Files here are copies, not the user's real
# file, so _is_temp_path() treats them as having no source folder.
UPLOAD_FOLDER = tempfile.mkdtemp()

# Last directory used in a native open/save dialog.  One-element list so both
# entry.py's WindowApi and get_save_dir() can read/write it without `global`.
LAST_DIR = ['']

# Temporary storage for encrypted files awaiting a password from the client.
# Maps ref_id -> {'path': str, 'filename': str}
_encrypted_refs = {}

# ── Progress reporting ────────────────────────────────────────────────────────
# Long multi-page operations (loading documents, exporting) write per-page
# progress here under a client-supplied job id; the client polls /api/progress
# to drive a real "page X of Y" indicator.  Flask runs threaded, so the poll
# requests are served while the worker request is still processing.
_PROGRESS = {}
_PROGRESS_LOCK = threading.Lock()


def set_progress(pid, current, total, label=''):
    """Record progress for job *pid* (no-op when pid is falsy)."""
    if not pid:
        return
    with _PROGRESS_LOCK:
        _PROGRESS[pid] = {'current': int(current), 'total': int(total),
                          'label': label}


def get_progress(pid):
    """Return the current progress dict for *pid*, or {} if none."""
    with _PROGRESS_LOCK:
        return dict(_PROGRESS.get(pid) or {})


def clear_progress(pid):
    """Drop any progress recorded for job *pid*."""
    if not pid:
        return
    with _PROGRESS_LOCK:
        _PROGRESS.pop(pid, None)


# ── Session lifecycle: per-session locks + LRU memory backstop ────────────────
# Flask runs threaded, so several request handlers can touch the SAME session
# concurrently (e.g. the viewer firing per-page renders while an edit is applied).
# `sessions` is a plain dict with no coordination, so multi-step read-modify-write
# sequences on session['pages'] and the annotation undo/redo stacks can interleave
# and lose or corrupt updates.  get_session_lock() hands out a reentrant per-
# session lock that mutating handlers hold around those sequences.
#
# Separately, a session holds all of a document's page bytes, thumbnails, undo
# history and (for scans) an OCR-source copy, and nothing frees it unless the tab
# is explicitly closed.  touch_session() keeps an access-ordered list and evicts
# the least-recently-used *saved* sessions once the resident count exceeds a
# generous cap — a backstop against unbounded growth from tabs that were never
# closed (or whose close cleanup never reached the server).
_SESSIONS_LOCK = threading.RLock()      # guards sessions-dict structure + order
_SESSION_ORDER = []                     # session_ids, most-recently-used LAST
_MAX_SESSIONS  = 32                     # resident-document backstop cap


def get_session_lock(session):
    """Return the session's reentrant lock, creating it on first use.  Hold it
    around any multi-step read-modify-write of session['pages'] or the annotation
    undo/redo stacks.  Reentrant, so nested acquisitions within one request (and
    the native-render lock acquired *inside* the critical section) are safe — no
    code path acquires these in the opposite order, so there is no deadlock."""
    lk = session.get('_lock')
    if lk is None:
        with _SESSIONS_LOCK:
            lk = session.get('_lock')
            if lk is None:
                lk = threading.RLock()
                session['_lock'] = lk
    return lk


def touch_session(session_id):
    """Mark *session_id* most-recently-used and evict the least-recently-used
    resident sessions beyond _MAX_SESSIONS.  Never evicts a session with unsaved
    working changes (recovery.has_pending), so an actively-edited tab is safe."""
    if not session_id or session_id not in sessions:
        return
    with _SESSIONS_LOCK:
        try:
            _SESSION_ORDER.remove(session_id)
        except ValueError:
            pass
        _SESSION_ORDER.append(session_id)
        while len(_SESSION_ORDER) > _MAX_SESSIONS:
            victim = None
            for sid in list(_SESSION_ORDER):
                sess = sessions.get(sid)
                if sess is None:
                    _SESSION_ORDER.remove(sid)          # already gone
                    continue
                if sid == session_id:
                    continue                            # never the one just used
                if _recovery.has_pending(sess):
                    continue                            # unsaved — keep it
                victim = sid
                break
            if victim is None:
                break                                   # nothing safe to drop
            _SESSION_ORDER.remove(victim)
            sessions.pop(victim, None)
            logger.info(f"[session LRU] evicted saved session {victim} "
                        f"(resident cap {_MAX_SESSIONS})")


def forget_session(session_id):
    """Drop a session's slot in the LRU order (call when a session is cleared)."""
    with _SESSIONS_LOCK:
        try:
            _SESSION_ORDER.remove(session_id)
        except ValueError:
            pass


# ── Robust per-character boxes (pdfium glyph-bbox repair) ─────────────────────
def corrected_char_boxes(textpage):
    """Return ``[(x1, y1, x2, y2, eff_size), …]`` — one tight box per character
    on a pdfium text page, in PDF user space (y=0 at the bottom).

    Why this exists.  Selection, hit-testing and area-select-copy all depend on
    per-glyph boxes that actually hug the glyph.  pdfium's ``get_charbox`` /
    ``FPDFText_GetCharBox`` normally does, but for some fonts it reports garbage:

      • **Type3 fonts with a ``/FontMatrix`` of ``[1 0 0 1 0 0]``** (instead of
        the usual ``[0.001 …]``).  pdfium then measures every glyph as the full
        ``/FontBBox`` em-square — e.g. a 24pt glyph as a ~480×480pt box *centred
        on the pen origin* — so hundreds of glyph boxes overlap the whole page.
        See ``docs/edit_problem.pdf``.
      • Occasional zero-size boxes with a mis-read font size of 1.0.

    Either way the pen **origin** (``FPDFText_GetCharOrigin``) and the glyph
    **advance** stay correct, so we rebuild a bad box from the origin plus the
    effective font size (Tf size × text-matrix scale):

        left = origin_x   right = next glyph's origin_x on the same baseline
        bottom = baseline (origin_y)   top = baseline + eff_size

    Boxes that already look sane (height ≲ 2.2× the font size) are returned
    untouched, so ordinary PDFs are completely unaffected.
    """
    import pypdfium2.raw as C
    import ctypes as ct
    import math

    raw_tp = getattr(textpage, 'raw', textpage)
    n = C.FPDFText_CountChars(raw_tp)

    boxes   = []   # [x1, y1, x2, y2] or None
    sizes   = []   # effective font size in pts (0 if unknown)
    origins = []   # (ox, oy) or None
    for i in range(n):
        L = ct.c_double(); R = ct.c_double(); B = ct.c_double(); T = ct.c_double()
        if C.FPDFText_GetCharBox(raw_tp, i, ct.byref(L), ct.byref(R),
                                 ct.byref(B), ct.byref(T)):
            boxes.append([L.value, B.value, R.value, T.value])
        else:
            boxes.append(None)

        fs = float(C.FPDFText_GetFontSize(raw_tp, i) or 0.0)
        m  = C.FS_MATRIX()
        if C.FPDFText_GetMatrix(raw_tp, i, ct.byref(m)):
            sc = math.hypot(m.a, m.b)
            if sc > 0:
                fs *= sc
        sizes.append(fs)

        ox = ct.c_double(); oy = ct.c_double()
        if C.FPDFText_GetCharOrigin(raw_tp, i, ct.byref(ox), ct.byref(oy)):
            origins.append((ox.value, oy.value))
        else:
            origins.append(None)

    # A size ≤ 1.5pt is almost always a pdfium mis-read (not a real 1pt glyph);
    # the page median is a safe stand-in for those glyphs' vertical extent.
    good = sorted(s for s in sizes if s > 1.5)
    median_size = good[len(good) // 2] if good else 0.0

    def _is_bad(box, fs):
        if box is None:
            return True
        x1, y1, x2, y2 = box
        if x2 - x1 <= 0 or y2 - y1 <= 0:
            return True
        # A single glyph's box is never much taller than its font size; a box
        # more than ~2.2× the size is the giant em-square pathology above.
        if fs > 0 and (y2 - y1) > fs * 2.2:
            return True
        return False

    out = []
    run_size = median_size          # last reliable size seen (per reading order)
    for i in range(n):
        box, fs, origin = boxes[i], sizes[i], origins[i]
        eff = fs if fs > 1.5 else (run_size or median_size)
        if fs > 1.5:
            run_size = fs
        if _is_bad(box, fs) and origin and eff > 0:
            ox, oy = origin
            rx2 = None
            nxt = origins[i + 1] if i + 1 < n else None
            # Advance to the next glyph on the same baseline — but only if it is a
            # plausible single-glyph step.  A large jump means the next glyph is a
            # different column / line-wrapped run, not this glyph's width, so we
            # fall back to a size-based estimate instead of a page-spanning box.
            if (nxt is not None and abs(nxt[1] - oy) < eff * 0.5
                    and ox < nxt[0] <= ox + eff * 3.0):
                rx2 = nxt[0]
            if rx2 is None:
                rx2 = ox + eff * 0.5              # size-based estimate
            out.append((ox, oy, rx2, oy + eff, eff))
        elif box is None:
            out.append((0.0, 0.0, 0.0, 0.0, eff))
        else:
            x1, y1, x2, y2 = box
            out.append((x1, y1, x2, y2, eff))
    return out


def text_is_unmappable(text):
    """Decide whether a page's *native* text is unusable because its font
    encoding is broken — pdfium hands back control codes / replacement glyphs
    instead of real characters.

    This happens with subsetted Type3 / CID fonts that ship no usable
    ``/ToUnicode`` and use arbitrary glyph names (see ``docs/edit_problem.pdf``,
    whose "text" extracts as ``\\x04\\x05\\x06…``).  Such text can't be searched,
    copied or edited, so callers OCR the page instead.

    Correctly-encoded text contains essentially *zero* C0 control characters
    (tab / newline / form-feed aside), so even a modest fraction of them is a
    reliable broken-font signal.  ``text`` is the raw string returned by pdfium's
    ``get_text_range`` for the page.
    """
    considered = bad = 0
    for ch in text or '':
        if ch in ' \t\r\n\f\v\xa0':
            continue                                  # ignore whitespace
        considered += 1
        o = ord(ch)
        # C0 controls, C1 controls, U+FFFD replacement, and the Private Use Area
        # (where broken CID fonts commonly dump unmapped glyphs).
        if o < 0x20 or 0x7f <= o <= 0x9f or o == 0xfffd or 0xe000 <= o <= 0xf8ff:
            bad += 1
    if considered < 8:
        return False                                  # too little text to judge
    return (bad / considered) >= 0.15


def native_text_is_unmappable(pdf_bytes):
    """True when page 0 carries native text whose encoding is unusable (see
    :func:`text_is_unmappable`).  False for image-only pages (no native text —
    those are handled by the existing scanned-page path) and for normal text."""
    try:
        import pypdfium2 as pdfium
        from pdf.ocr_utils import _NATIVE_LOCK
        with _NATIVE_LOCK:
            doc = pdfium.PdfDocument(pdf_bytes)
            try:
                tp = doc[0].get_textpage()
                if tp.count_chars() == 0:
                    return False
                txt = tp.get_text_range()
            finally:
                doc.close()
    except Exception:
        return False
    return text_is_unmappable(txt)


# ── Recent files (MRU) ────────────────────────────────────────────────────────
# Persisted to %LOCALAPPDATA%\PatchworkPDF\recent_files.json so the viewer's
# empty state can offer one-click reopening of recently used documents.
_MRU_MAX = 8

# ── Document conversion ───────────────────────────────────────────────────────
# Handled purely in Python — no office application needed
TEXT_EXTENSIONS   = {'.txt', '.csv'}
# Require an installed office application — converted via COM automation (Word/Excel)
OFFICE_EXTENSIONS = {'.doc', '.docx', '.odt', '.rtf', '.xls', '.xlsx', '.ods'}
# Raster images — converted to PDF pages with Pillow (one page per image frame,
# so multi-page TIFFs become multi-page PDFs).  All natively supported by Pillow.
IMAGE_EXTENSIONS  = {'.png', '.jpg', '.jpeg', '.gif', '.bmp',
                     '.tif', '.tiff', '.webp'}

# Every extension that is imported by converting it to PDF first.
NON_PDF_EXTENSIONS = (
    {'.txt', '.csv'} | OFFICE_EXTENSIONS | IMAGE_EXTENSIONS
)

MANUAL_CONVERT_MSG = (
    "No compatible office application was found to convert this file automatically. "
    "Please open it in your office application (Word, Excel, LibreOffice, etc.) "
    "and use File \u2192 Save As / Export to save it as a PDF first, "
    "then import that PDF into Patchwork PDF."
)


def _get_decrypted_pdf_bytes(reader):
    """Write all pages from an already-decrypted PdfReader into fresh, unencrypted bytes.
    Used after reader.decrypt() so downstream code (pypdfium2, form_utils) can
    work with the plain PDF without needing a password."""
    w = pypdf.PdfWriter()
    for page in reader.pages:
        w.add_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _open_encrypted_reader(reader, password):
    """Try to open an encrypted PdfReader with `password` (may be '').

    Returns (opened, restricted, is_open_password):
      opened           — False when the password was rejected and the caller must
                         ask the operator for one.
      restricted       — True when the file opened with user rights only, and its
                         permissions deny modification.  Viewing is fine; editing
                         stays off until the owner password is supplied.
      is_open_password — True when `password` is the document's open (user)
                         password, and so must be preserved and re-applied on save.

    Many PDFs (manuals, forms, datasheets) carry an owner password only: the user
    password is empty, so they open without any prompt and merely restrict what
    may be changed.  Those must never trigger a password prompt on open."""
    result = reader.decrypt(password)
    if result == 0:
        return False, False, False
    if result == pypdf.PasswordType.OWNER_PASSWORD:
        # Owner rights — every restriction is lifted.
        return True, False, False
    perms = reader.user_access_permissions
    restricted = perms is not None and not (perms & _AccessPermissions.MODIFY)
    return True, restricted, bool(password)


def _encrypt_pdf_bytes(pdf_bytes, password):
    """Apply AES-256 encryption to already-written PDF bytes.
    Returns encrypted bytes, or original on failure."""
    if not password:
        return pdf_bytes
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        writer = pypdf.PdfWriter()
        writer.append(reader)
        writer.encrypt(user_password=password, owner_password=password,
                       algorithm='AES-256-R5')
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception as e:
        logger.info(f'[encrypt] AES-256 failed ({e}), trying 128-bit RC4')
        try:
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            writer = pypdf.PdfWriter()
            writer.append(reader)
            writer.encrypt(user_password=password, owner_password=password,
                           use_128bit=True)
            buf = io.BytesIO()
            writer.write(buf)
            return buf.getvalue()
        except Exception as e2:
            logger.info(f'[encrypt] fallback also failed: {e2}')
            return pdf_bytes


def _create_blank_page_bytes(width_pts, height_pts):
    """Return a single blank white PDF page as bytes using pypdf."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=width_pts, height=height_pts)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf.read()


def _rotated_dims(w, h, rotate):
    """Swap the axes for a page displayed at /Rotate 90 or 270."""
    return (h, w) if (int(rotate) % 360) in (90, 270) else (w, h)


def _page_rotate(page):
    """A page's effective /Rotate, honouring inheritance from the /Pages tree."""
    try:
        rot = page.get_inherited('/Rotate', 0)
    except Exception:
        rot = page.get('/Rotate', 0)
    try:
        return int(rot or 0) % 360
    except (TypeError, ValueError):
        return 0


def _page_dimensions(reader, page_index=0):
    """Return (width_pts, height_pts) for a page *as displayed*.

    /Rotate 90 or 270 swaps the axes: a portrait MediaBox with /Rotate 90 is a
    landscape page on screen.  These numbers have to agree with pdfium's
    page.get_width()/get_height() — which are rotation-aware — because the
    viewer mixes the two: it fits the zoom to the widest page in the document
    using this metadata, then lays each page out at the size the renderer
    reports.  A portrait number for a landscape page opens the document zoomed
    past the viewport, the wide page overflowing to the right.
    """
    try:
        page = reader.pages[page_index]
        # The CROPBOX, not the MediaBox: pdfium renders the CropBox, so that is
        # what "as displayed" means and what these numbers have to agree with.
        # They are the same on the overwhelming majority of pages (CropBox
        # defaults to MediaBox), which is why the difference went unnoticed — but
        # on a cropped page the MediaBox over-reports, and after the viewer's own
        # Crop tool runs it would disagree with the page the user is looking at.
        box  = page.cropbox
        w, h = _rotated_dims(float(box.width), float(box.height), _page_rotate(page))
        return round(w, 2), round(h, 2)
    except Exception:
        return 595.28, 841.89  # A4 fallback


def _store_metadata(session, reader):
    """Read PDF document metadata from a PdfReader and cache it in the session.
    Only populates keys that are not already set (preserves user edits)."""
    if 'metadata' in session:
        return   # user may have already edited — don't overwrite
    try:
        m = reader.metadata or {}
        session['metadata'] = {
            'title':    str(m.get('/Title',    '') or ''),
            'author':   str(m.get('/Author',   '') or ''),
            'subject':  str(m.get('/Subject',  '') or ''),
            'keywords': str(m.get('/Keywords', '') or ''),
        }
    except Exception:
        session.setdefault('metadata', {'title':'','author':'','subject':'','keywords':''})


def _apply_metadata(writer, session):
    """Write session metadata dict into a PdfWriter before saving."""
    meta = session.get('metadata', {})
    if not any(meta.values()):
        return
    writer.add_metadata({
        '/Title':    meta.get('title',    ''),
        '/Author':   meta.get('author',   ''),
        '/Subject':  meta.get('subject',  ''),
        '/Keywords': meta.get('keywords', ''),
    })


def _read_outline_bookmarks(reader, idx_to_pid):
    """Flatten a PdfReader's /Outlines into session bookmark dicts.

    idx_to_pid maps a page index in *reader* → the page_id assigned to that page
    when the file was split into per-page records.  Returns a flat list of
    {id, title, page_id, y, level} preserving nesting depth as 'level'.
    """
    bookmarks = []

    def walk(items, level):
        for it in items:
            if isinstance(it, list):
                walk(it, level + 1)      # nested children of the previous item
                continue
            try:
                title = str(getattr(it, 'title', '') or '').strip() or 'Bookmark'
                pidx  = reader.get_destination_page_number(it)
                if pidx is None:
                    continue
                pid = idx_to_pid.get(pidx)
                if not pid:
                    continue
                y = None
                try:
                    t = getattr(it, 'top', None)
                    if t is not None:
                        y = float(t)
                except Exception:
                    y = None
                bookmarks.append({'id': str(uuid.uuid4()), 'title': title,
                                  'page_id': pid, 'y': y, 'level': level})
            except Exception:
                continue

    try:
        walk(reader.outline, 0)
    except Exception:
        pass
    return bookmarks


def _write_outlines(writer, session):
    """Write the session's bookmarks into the output PDF's /Outlines tree.

    Bookmarks reference page_ids (stable across reordering); we resolve each to
    its current index in session['pages'] (== writer page order) at save time.
    Nesting is reconstructed from each bookmark's 'level'.
    """
    bookmarks = session.get('bookmarks') or []
    if not bookmarks:
        return
    try:
        from pypdf.generic import Fit
    except Exception:
        Fit = None
    idx_by_pid = {p['id']: i for i, p in enumerate(session['pages'])}
    last_at_level = {}                      # level → outline-item ref (parent lookup)
    for bm in bookmarks:
        pi = idx_by_pid.get(bm.get('page_id'))
        if pi is None:
            continue
        lvl = int(bm.get('level', 0) or 0)
        parent = None
        for L in range(lvl - 1, -1, -1):
            if L in last_at_level:
                parent = last_at_level[L]; break
        title = bm.get('title') or 'Bookmark'
        y = bm.get('y')
        try:
            if Fit is not None and y is not None:
                ref = writer.add_outline_item(title, pi, parent=parent,
                                              fit=Fit.xyz(top=float(y)))
            else:
                ref = writer.add_outline_item(title, pi, parent=parent)
        except Exception:
            try:
                ref = writer.add_outline_item(title, pi, parent=parent)
            except Exception:
                continue
        last_at_level[lvl] = ref
        for L in [k for k in last_at_level if k > lvl]:
            last_at_level.pop(L, None)


def _is_temp_path(p):
    """True if p lives inside our upload tempdir (i.e. not a real source path)."""
    try:
        return os.path.abspath(p).startswith(os.path.abspath(UPLOAD_FOLDER))
    except Exception:
        return False


def get_save_dir(session):
    """Best directory to save into: the document's real source folder when known,
    else the last directory used in a native dialog, else home.  Files added by
    browser upload / drag-drop live in the temp upload folder and have no real
    source path, so those are ignored here."""
    first_path = session.get('first_file_path')
    if first_path and not _is_temp_path(first_path):
        d = os.path.dirname(os.path.abspath(first_path))
        # Probe through _fs_path (a deep folder is invisible to a plain isdir)
        # but hand back the plain path — this one goes to the save dialog.
        if os.path.isdir(_fs_path(d)) and os.access(_fs_path(d), os.W_OK):
            return d
    if LAST_DIR[0] and os.path.isdir(_fs_path(LAST_DIR[0])):
        return LAST_DIR[0]
    return os.path.expanduser('~')


def get_save_filename(session):
    """Return a suggested save filename.  Once the user has saved the document
    under a chosen name, reuse that name so a subsequent save keeps it; otherwise
    derive '<source>_edited.pdf' from the first page's source file."""
    saved = session.get('save_name')
    if saved:
        return saved
    first = next(
        (p['filename'] for p in session.get('pages', []) if p.get('filename')),
        'document.pdf',
    )
    base = os.path.splitext(first)[0]
    return base + '_edited.pdf'


def _resolve_save_pages(session, page_ids):
    """Return the list of session pages to write, in layout order.

    When page_ids is provided (a subset selected in the Layout view) only those
    pages are kept — letting the user save any combination of pages. The order
    always follows the current layout, never the order ids arrive in.
    Returns None when page_ids names no existing pages."""
    if not page_ids:
        return session['pages']
    wanted = set(page_ids)
    subset = [p for p in session['pages'] if p['id'] in wanted]
    return subset or None


def _build_merged_pdf(session, pages_to_save, include_outlines=True):
    """Merge the given session pages into one PDF and return the bytes.

    Each page is added as-is (its original PDF page object), so the page
    structure — text, vectors, form fields — is preserved, never flattened to an
    image. Outlines/bookmarks are written only for full-document saves: their
    page indices are resolved against the full session, so they don't line up
    with a selected subset and are skipped there."""
    writer = pypdf.PdfWriter()
    for page in pages_to_save:
        reader = pypdf.PdfReader(io.BytesIO(page['pdf_bytes']))
        writer.add_page(reader.pages[0])

    # Deduplicating objects is a size optimisation, never a requirement.  It has
    # to decode every stream to hash it, which fails on images whose filter needs
    # an external binary we may not ship (e.g. JBIG2 needs jbig2dec).  A document
    # we cannot shrink must still save, so treat any failure here as "skip it".
    try:
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    except Exception as exc:
        logger.info(f'[save] object compression skipped ({exc.__class__.__name__}: {exc})')

    _apply_metadata(writer, session)
    if include_outlines:
        _write_outlines(writer, session)

    out_buf = io.BytesIO()
    writer.write(out_buf)
    merged_bytes = _form_utils.build_acroform_on_save(out_buf.getvalue(), pages_to_save)
    return _encrypt_pdf_bytes(merged_bytes, session.get('password', ''))


def _fs_path(path):
    """Filesystem-callable form of a user-supplied path (see platform_paths)."""
    from platform_io.platform_paths import fs_path
    return fs_path(path)


def _mru_file():
    from platform_io import platform_paths
    return os.path.join(platform_paths.user_data_dir(), 'recent_files.json')


def _mru_load():
    try:
        with open(_mru_file(), 'r', encoding='utf-8') as f:
            data = _json.load(f)
        return [e for e in data if isinstance(e, dict) and e.get('path')]
    except Exception:
        return []


def _mru_record(path):
    """Remember a successfully opened real file (most recent first).  Browser
    uploads land in the temp folder and are copies, not the user's file — skip."""
    try:
        if not path or _is_temp_path(path) or not os.path.isfile(_fs_path(path)):
            return
        path  = os.path.abspath(path)
        items = [e for e in _mru_load()
                 if os.path.normcase(e['path']) != os.path.normcase(path)]
        items.insert(0, {'path': path, 'name': os.path.basename(path),
                         'when': time.time()})
        with open(_mru_file(), 'w', encoding='utf-8') as f:
            _json.dump(items[:_MRU_MAX], f, indent=1)
    except Exception as e:
        logger.info(f"mru: could not record {path}: {e}")


def render_page_thumbnail(pdf_bytes, page_index, width=200):
    """Render a single page thumbnail — opens and closes its own document.
    Used for one-off renders (e.g. after rotation).  For bulk rendering of
    many pages use _render_thumbnail_from_doc() with a shared document.

    Takes _NATIVE_LOCK itself: upload, open-paths and page edits call this from
    Flask threads while the viewer renders on others (routes/viewer.py)."""
    import pypdfium2 as pdfium
    from pdf.ocr_utils import _NATIVE_LOCK
    with _NATIVE_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            thumb = _render_thumbnail_from_doc(doc, page_index, width)
        finally:
            doc.close()
    return thumb


def _render_thumbnail_from_doc(doc, page_index, width=200):
    """Render one thumbnail from an already-open pypdfium2 PdfDocument."""
    page = doc[page_index]
    scale = width / page.get_width()
    bitmap = page.render(scale=scale, rotation=0)
    pil_image = bitmap.to_pil()
    buf = io.BytesIO()
    pil_image.save(buf, format='PNG')
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('utf-8')


def _preload_file(file_path, session_id, password=''):
    """Load a PDF (or convertible file) into an existing session dict.
    Called before Flask starts so the browser can open the viewer directly.
    Only a file that cannot be opened at all without a password sets
    session['needs_password'] = True and returns without loading pages; a file
    that opens but forbids editing loads normally with session['restricted']."""
    import pypdfium2 as _pdfium
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == '.pdf':
            with open(_fs_path(file_path), 'rb') as fh:
                pdf_bytes = fh.read()
        else:
            pdf_bytes = _convert_to_pdf_bytes(file_path)

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        sessions[session_id]['restricted'] = False
        if reader.is_encrypted:
            opened, restricted, is_open_password = _open_encrypted_reader(reader, password)
            if not opened:
                sessions[session_id]['needs_password']      = True
                sessions[session_id]['encrypted_file_path'] = file_path
                if password:
                    sessions[session_id]['wrong_password']  = True
                return
            if is_open_password:
                # Preserve the open password so saving re-encrypts automatically
                sessions[session_id]['password'] = password
            sessions[session_id]['restricted'] = restricted
            sessions[session_id].pop('needs_password', None)
            sessions[session_id].pop('wrong_password', None)
            # A restricted file keeps its path so the operator can still supply
            # the owner password later and unlock editing.
            if restricted:
                sessions[session_id]['encrypted_file_path'] = file_path
            else:
                sessions[session_id].pop('encrypted_file_path', None)
            # Re-read with decryption applied (pypdf mutates the reader state),
            # then work from plain bytes so pypdfium2 and form_utils can read them
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            reader.decrypt(password)
            pdf_bytes = _get_decrypted_pdf_bytes(reader)
            reader    = pypdf.PdfReader(io.BytesIO(pdf_bytes))

        # Read and store document metadata from the source file
        _store_metadata(sessions[session_id], reader)

        n_pages  = len(reader.pages)
        session  = sessions[session_id]

        # Extract form fields from the full PDF before splitting into pages
        page_form_fields = _form_utils.extract_all_form_fields(pdf_bytes)

        # Open the pypdfium2 document once and reuse it for all thumbnails.
        # Opening a new PdfDocument per page leaks resources and can segfault
        # when processing large files with many pages.
        #
        # Every pdfium call holds _NATIVE_LOCK (see routes/viewer.py): this runs
        # on Flask threads for a second-instance hand-off, the bundled manual and
        # a password unlock, while the viewer may be rendering.  Taken per call,
        # not around the loop, so a long document doesn't stall those renders.
        from pdf.ocr_utils import _NATIVE_LOCK
        with _NATIVE_LOCK:
            pdfium_doc = _pdfium.PdfDocument(pdf_bytes)
        idx_to_pid = {}
        try:
            for i in range(n_pages):
                page_id    = str(uuid.uuid4())
                idx_to_pid[i] = page_id
                with _NATIVE_LOCK:
                    thumbnail = _render_thumbnail_from_doc(pdfium_doc, i)
                w_pts, h_pts = _page_dimensions(reader, i)
                writer     = pypdf.PdfWriter()
                writer.add_page(reader.pages[i])
                buf        = io.BytesIO()
                writer.write(buf)
                page_bytes = buf.getvalue()
                session['pages'].append({
                    'id':          page_id,
                    'filename':    os.path.basename(file_path),
                    'page_num':    i + 1,
                    'total_pages': n_pages,
                    'rotation':    0,
                    'thumbnail':   thumbnail,
                    'pdf_bytes':   page_bytes,
                    'form_fields': page_form_fields.get(i, []),
                    'width_pts':   w_pts,
                    'height_pts':  h_pts,
                })
        finally:
            with _NATIVE_LOCK:
                pdfium_doc.close()
        # Import existing /Outlines bookmarks from the source document
        try:
            session.setdefault('bookmarks', [])
            session['bookmarks'].extend(_read_outline_bookmarks(reader, idx_to_pid))
        except Exception:
            pass
    except Exception as exc:
        logger.info(f'[preload] Failed to load {file_path}: {exc}')


def _ingest_document(session, src_path, safe_name, password='', progress_id=None):
    """Load one PDF/office file already on disk at src_path into the session.

    Shared by /api/upload (file saved to a temp path) and /api/open_paths (file
    opened by its real path via a native dialog).  Returns
    (new_pages:list, error:str|None, pw_required:dict|None)."""
    ext = os.path.splitext(safe_name.lower())[1]
    is_pdf = ext == '.pdf'
    is_convertible = ext in NON_PDF_EXTENSIONS
    if not is_pdf and not is_convertible:
        return [], None, None

    if is_pdf:
        try:
            with open(_fs_path(src_path), 'rb') as f:
                pdf_bytes = f.read()
        except OSError as read_err:
            # Locked by another app, a cloud placeholder that could not be
            # hydrated, permissions, a dropped network share.  Report it as the
            # open error it is — an uncaught OSError here becomes a 500 the
            # front end can only describe as a parse failure.
            logger.info(f"open: cannot read {src_path}: {read_err}")
            return [], f"{safe_name}: {read_err.strerror or read_err}", None
    else:
        try:
            pdf_bytes = _convert_to_pdf_bytes(src_path)
            safe_name = os.path.splitext(safe_name)[0] + '.pdf'
        except Exception as conv_err:
            return [], f"{safe_name}: {conv_err}", None

    # ── Encryption check ────────────────────────────────────────────────────
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        opened, restricted, is_open_password = _open_encrypted_reader(reader, password)
        if not opened:
            if password:
                return [], f"{safe_name}: Incorrect password.", None
            ref = str(uuid.uuid4())
            _encrypted_refs[ref] = {'path': src_path, 'filename': safe_name}
            return [], None, {'ref': ref, 'filename': safe_name}
        if is_open_password:
            # Preserve password so saving re-encrypts automatically
            session['password'] = password
        if restricted:
            session['restricted']          = True
            session['encrypted_file_path'] = src_path
        # Re-open with decryption applied
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        reader.decrypt(password)
        # Replace pdf_bytes with a decrypted copy so thumbnail rendering
        # and form-field extraction (both use pypdfium2/pikepdf) don't fail
        pdf_bytes = _get_decrypted_pdf_bytes(reader)
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))  # re-open from clean bytes

    num_pages = len(reader.pages)
    upload_form_fields = _form_utils.extract_all_form_fields(pdf_bytes)
    _store_metadata(session, reader)

    new_pages = []
    idx_to_pid = {}
    for i in range(num_pages):
        if progress_id:
            set_progress(progress_id, i + 1, num_pages,
                         f"Loading page {i + 1} of {num_pages}…")
        page_id      = str(uuid.uuid4())
        idx_to_pid[i] = page_id
        w_pts, h_pts = _page_dimensions(reader, i)
        thumbnail    = render_page_thumbnail(pdf_bytes, i)

        writer = pypdf.PdfWriter()
        writer.add_page(reader.pages[i])
        page_buf = io.BytesIO()
        writer.write(page_buf)
        page_bytes = page_buf.getvalue()

        page_data = {
            'id':          page_id,
            'filename':    safe_name,
            'page_num':    i + 1,
            'total_pages': num_pages,
            'rotation':    0,
            'thumbnail':   thumbnail,
            'pdf_bytes':   page_bytes,
            'form_fields': upload_form_fields.get(i, []),
            'width_pts':   w_pts,
            'height_pts':  h_pts,
        }
        session['pages'].append(page_data)
        new_pages.append({k: v for k, v in page_data.items() if k != 'pdf_bytes'})

    # Import existing /Outlines bookmarks from the document
    try:
        session.setdefault('bookmarks', [])
        session['bookmarks'].extend(_read_outline_bookmarks(reader, idx_to_pid))
    except Exception:
        pass

    return new_pages, None, None


def _txt_to_pdf_bytes(src_path):
    """Convert a plain-text or CSV file to PDF bytes using reportlab."""
    from reportlab.pdfgen import canvas as _rlc
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm

    with open(src_path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()

    buf     = io.BytesIO()
    pw, ph  = A4
    margin  = 20 * mm
    font_sz = 9
    line_h  = font_sz * 1.4

    # Wrap at the number of characters that actually fit the usable page width,
    # rather than a fixed 120 (which overflows the right margin — Courier's glyph
    # advance is 0.6 em, so 120 chars at 9pt is ~648pt, wider than A4's ~555pt
    # usable width).  Courier is monospaced, so char count maps directly to width.
    usable_w  = pw - 2 * margin
    char_adv  = font_sz * 0.6
    wrap_at   = max(16, int(usable_w / char_adv))

    c = _rlc.Canvas(buf, pagesize=A4)
    c.setFont('Courier', font_sz)
    y = ph - margin

    for line in lines:
        # Simple character wrap at the computed column width.
        while len(line) > wrap_at:
            c.drawString(margin, y, line[:wrap_at])
            y -= line_h
            line = '    ' + line[wrap_at:]
            if y < margin:
                c.showPage()
                c.setFont('Courier', font_sz)
                y = ph - margin
        c.drawString(margin, y, line)
        y -= line_h
        if y < margin:
            c.showPage()
            c.setFont('Courier', font_sz)
            y = ph - margin

    c.save()
    buf.seek(0)
    return buf.read()


def _office_to_pdf_via_com(src_path):
    """Convert a Word or Excel file to PDF by driving the installed Office app via COM.

    Each conversion runs in a dedicated thread with its own CoInitialize/CoUninitialize
    so that consecutive Word→Excel (or any mixed) conversions don't conflict.
    Works with all Office versions including Microsoft 365 Click-to-Run.
    Windows-only — callers fall back to LibreOffice elsewhere.
    """
    if sys.platform != 'win32':
        raise RuntimeError(MANUAL_CONVERT_MSG)

    try:
        import win32com.client as _wc
        import pythoncom as _pc
    except ImportError:
        raise RuntimeError(MANUAL_CONVERT_MSG)

    ext      = os.path.splitext(src_path)[1].lower()
    out_dir  = tempfile.mkdtemp()
    out_path = os.path.join(out_dir, 'converted.pdf')
    abs_src  = os.path.abspath(src_path)
    abs_out  = os.path.abspath(out_path)

    # Result carrier for the worker thread
    result = {'bytes': None, 'error': None}

    def _worker():
        _pc.CoInitialize()
        try:
            if ext in {'.doc', '.docx', '.odt', '.rtf'}:
                app = _wc.Dispatch('Word.Application')
                app.Visible = False
                try:
                    doc = app.Documents.Open(abs_src, ReadOnly=True)
                    doc.SaveAs(abs_out, FileFormat=17)  # 17 = wdFormatPDF
                    doc.Close(False)
                finally:
                    app.Quit()

            elif ext in {'.xls', '.xlsx', '.ods'}:
                app = _wc.Dispatch('Excel.Application')
                app.Visible = False
                app.DisplayAlerts = False
                try:
                    wb = app.Workbooks.Open(abs_src, ReadOnly=True)
                    # ExportAsFixedFormat(xlTypePDF=0, filename, xlQualityStandard=0,
                    #   IncludeDocProperties=True, IgnorePrintAreas=False,
                    #   OpenAfterPublish=False)
                    wb.ExportAsFixedFormat(0, abs_out, 0, True, False)
                    wb.Close(False)
                finally:
                    app.Quit()

            else:
                result['error'] = MANUAL_CONVERT_MSG
                return

            if not os.path.isfile(abs_out):
                result['error'] = MANUAL_CONVERT_MSG
                return

            with open(abs_out, 'rb') as f:
                result['bytes'] = f.read()

        except Exception as e:
            result['error'] = MANUAL_CONVERT_MSG
            logger.info(f'[COM conversion] {type(e).__name__}: {e}')
        finally:
            _pc.CoUninitialize()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=120)   # 2-minute ceiling per file

    if t.is_alive():
        raise RuntimeError('Office conversion timed out. Please convert the file to PDF manually.')

    if result['error']:
        raise RuntimeError(result['error'])

    if result['bytes'] is None:
        raise RuntimeError(MANUAL_CONVERT_MSG)

    import shutil as _shutil
    _shutil.rmtree(out_dir, ignore_errors=True)
    return result['bytes']


def _find_soffice():
    """Locate the LibreOffice (or OpenOffice) headless binary, or None.

    Checks PATH first, then the usual per-platform install locations."""
    import shutil as _shutil
    for name in ('soffice', 'libreoffice'):
        found = _shutil.which(name)
        if found:
            return found

    candidates = []
    if sys.platform == 'win32':
        for base in (os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                     os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')):
            if base:
                candidates.append(os.path.join(base, 'LibreOffice', 'program', 'soffice.exe'))
    elif sys.platform == 'darwin':
        candidates.append('/Applications/LibreOffice.app/Contents/MacOS/soffice')
    else:  # Linux / other Unix
        candidates += [
            '/usr/bin/soffice', '/usr/local/bin/soffice',
            '/opt/libreoffice/program/soffice', '/snap/bin/libreoffice',
        ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _libreoffice_host_binary():
    """Under Flatpak the sandbox cannot see host binaries, so probe the HOST for
    LibreOffice via ``flatpak-spawn --host``.  Returns the host command name
    ('soffice' or 'libreoffice') or None.  Requires the Flatpak to hold the
    ``--talk-name=org.freedesktop.Flatpak`` permission."""
    import subprocess as _subprocess
    for name in ('soffice', 'libreoffice'):
        try:
            r = _subprocess.run(
                ['flatpak-spawn', '--host', 'sh', '-c', f'command -v {name}'],
                capture_output=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return name
        except Exception:
            return None
    return None


def _office_to_pdf_via_libreoffice(src_path):
    """Convert a Word/Excel/ODF file to PDF using a headless LibreOffice process.

    Cross-platform, and the only office converter available on macOS/Linux.
    Requires LibreOffice (or OpenOffice) to be installed.  Under Flatpak the
    host's LibreOffice is driven via ``flatpak-spawn --host``; because the host
    and sandbox do not share ``/tmp``, the work files are staged under the real
    home directory (bind-mounted at the same absolute path in both) instead."""
    import subprocess as _subprocess
    import shutil as _shutil
    import pathlib as _pathlib
    from platform_io import platform_paths as _pp

    flatpak = _pp.is_flatpak()
    if flatpak:
        soffice = _libreoffice_host_binary()
        if not soffice:
            raise RuntimeError(MANUAL_CONVERT_MSG)
        cmd_prefix = ['flatpak-spawn', '--host']
        # A location visible at the SAME absolute path from both the sandbox and
        # the host (the real home is bind-mounted at /home/<user>), unlike the
        # sandbox-private /tmp that tempfile.mkdtemp() would otherwise use.
        shared = os.path.expanduser('~/.cache')
        os.makedirs(shared, exist_ok=True)
        out_dir     = tempfile.mkdtemp(dir=shared)
        profile_dir = tempfile.mkdtemp(dir=shared)
        # The source may live in the sandbox-private temp area; stage a copy in
        # the shared dir so the host LibreOffice process can read it.
        src_arg = os.path.join(out_dir, 'source' + os.path.splitext(src_path)[1])
        _shutil.copyfile(src_path, src_arg)
    else:
        soffice = _find_soffice()
        if not soffice:
            raise RuntimeError(MANUAL_CONVERT_MSG)
        cmd_prefix = []
        out_dir     = tempfile.mkdtemp()
        profile_dir = tempfile.mkdtemp()
        src_arg     = os.path.abspath(src_path)

    # Suppress the console-window flash on Windows.
    creationflags = getattr(_subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0
    try:
        cmd = cmd_prefix + [
            soffice, '--headless', '--norestore', '--nolockcheck', '--nodefault',
            f'-env:UserInstallation={_pathlib.Path(profile_dir).as_uri()}',
            '--convert-to', 'pdf', '--outdir', out_dir, src_arg,
        ]
        proc = _subprocess.run(cmd, capture_output=True, timeout=120,
                               creationflags=creationflags)
        # LibreOffice writes "<stem>.pdf" into out_dir; pick whatever PDF it made
        # (out_dir is fresh) rather than guessing the exact sanitised name.
        produced = [f for f in os.listdir(out_dir) if f.lower().endswith('.pdf')]
        if not produced:
            stderr = (proc.stderr or b'').decode('utf-8', 'replace').strip()
            raise RuntimeError(
                f'LibreOffice produced no PDF (rc={proc.returncode}). {stderr}'.strip())
        with open(os.path.join(out_dir, produced[0]), 'rb') as f:
            return f.read()
    except _subprocess.TimeoutExpired:
        raise RuntimeError('LibreOffice conversion timed out. '
                           'Please convert the file to PDF manually.')
    finally:
        _shutil.rmtree(out_dir, ignore_errors=True)
        _shutil.rmtree(profile_dir, ignore_errors=True)


# Office conversion is NOT re-entrant.  Word and Excel are single-instance COM
# servers per user session: two threads that Dispatch('Word.Application') get the
# SAME application object, so the first one to finish calls app.Quit() and rips
# the server out from under the other, which then fails with an RPC error and is
# reported to the user as MANUAL_CONVERT_MSG ("no compatible office
# application") even though Word is installed and fine.  LibreOffice has the
# same shape of problem via its profile lock.  Serialise every conversion.
_OFFICE_LOCK = threading.Lock()


def _office_to_pdf_bytes(src_path):
    """Convert a Word/Excel/ODF file to PDF.

    On Windows, prefer the installed Office app via COM (best fidelity); if that
    is unavailable or fails, fall back to LibreOffice.  On macOS/Linux, LibreOffice
    is the only option.  Raises RuntimeError(MANUAL_CONVERT_MSG) if no converter
    succeeds.

    Serialised on _OFFICE_LOCK — concurrent conversions corrupt each other (see
    the note above)."""
    with _OFFICE_LOCK:
        return _office_to_pdf_bytes_locked(src_path)


def _office_to_pdf_bytes_locked(src_path):
    if sys.platform == 'win32':
        try:
            return _office_to_pdf_via_com(src_path)
        except Exception as com_err:
            logger.info(f'[office] COM conversion unavailable/failed ({com_err}); '
                 f'trying LibreOffice')
    try:
        return _office_to_pdf_via_libreoffice(src_path)
    except Exception as lo_err:
        logger.info(f'[office] LibreOffice conversion failed: {lo_err}')
        raise RuntimeError(MANUAL_CONVERT_MSG)


def _flatten_image(im):
    """Return an RGB copy of a PIL image, compositing any transparency onto a
    white background so it renders correctly on an opaque PDF page."""
    from PIL import Image as _PImage
    if im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info):
        rgba = im.convert('RGBA')
        bg   = _PImage.new('RGB', rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if im.mode != 'RGB':
        return im.convert('RGB')
    return im.copy()


def _image_to_pdf_bytes(src_path):
    """Convert an image file to PDF bytes — one page per image frame (so a
    multi-page TIFF becomes a multi-page PDF).  Each page takes the image's own
    aspect ratio, scaled to fit within an A4 envelope, with the image full-bleed
    (no margins)."""
    from reportlab.pdfgen import canvas as _rlc
    from reportlab.lib.utils import ImageReader
    from reportlab.lib.pagesizes import A4
    from PIL import Image as _PImage, ImageSequence as _ImageSequence

    max_w, max_h = A4   # 595.27 × 841.89 pt
    buf    = io.BytesIO()
    c      = _rlc.Canvas(buf)
    frames = 0

    with _PImage.open(src_path) as im:
        for frame in _ImageSequence.Iterator(im):
            rgb    = _flatten_image(frame)
            iw, ih = rgb.size
            if iw <= 0 or ih <= 0:
                continue
            ar = iw / ih
            if ar >= (max_w / max_h):      # wide → constrain by width
                pw, ph = max_w, max_w / ar
            else:                          # tall → constrain by height
                pw, ph = max_h * ar, max_h
            c.setPageSize((pw, ph))
            c.drawImage(ImageReader(rgb), 0, 0, width=pw, height=ph)
            c.showPage()
            frames += 1

    if not frames:
        raise RuntimeError('The image file contained no readable image data.')

    c.save()
    buf.seek(0)
    return buf.read()


def _convert_to_pdf_bytes(src_path):
    """Dispatch to the right converter based on file extension."""
    ext = os.path.splitext(src_path)[1].lower()
    if ext in TEXT_EXTENSIONS:
        return _txt_to_pdf_bytes(src_path)
    if ext in OFFICE_EXTENSIONS:
        return _office_to_pdf_bytes(src_path)
    if ext in IMAGE_EXTENSIONS:
        return _image_to_pdf_bytes(src_path)
    raise RuntimeError(MANUAL_CONVERT_MSG)


def _reencode_image_xobject(obj, quality, max_w, max_h):
    """Downsample + re-encode a single image XObject in place, leaving the page's
    content stream (text, vectors) untouched.  Returns True if the image was
    replaced with a smaller version, False if it was left as-is.

    Conservative by design — images we can't shrink without risking quality or
    correctness are skipped: transparency/masks, 1-bit (bilevel/stencil) images,
    and anything we can't decode.  This preserves searchable text and vector art,
    unlike a full-page rasterise."""
    import pikepdf
    from PIL import Image as _PILImage

    # Skip stencil masks and any image that carries transparency — re-encoding
    # these as opaque JPEG would corrupt the page.
    if obj.get('/ImageMask') or '/SMask' in obj or '/Mask' in obj:
        return False
    if int(obj.get('/BitsPerComponent', 8) or 8) < 8:
        return False   # 1-/2-/4-bit (bilevel CCITT scans, palettes) — leave alone

    try:
        pil = pikepdf.PdfImage(obj).as_pil_image()
    except Exception:
        return False   # exotic colourspace/filter we can't decode — skip safely

    mode = pil.mode
    if 'A' in mode and mode != 'RGB':        # RGBA / LA / PA — has an alpha channel
        return False
    if mode in ('L', 'I', 'I;16'):
        pil, colorspace = pil.convert('L'), '/DeviceGray'
    else:
        pil, colorspace = pil.convert('RGB'), '/DeviceRGB'

    # Re-encoding an already-JPEG image that doesn't need downsampling just adds
    # generational artifacts for no gain, so skip that case to protect quality.
    filt    = str(obj.get('/Filter') or '')
    is_jpeg = 'DCTDecode' in filt

    w, h  = pil.size
    scale = min(1.0, max_w / w, max_h / h)
    if scale >= 1.0 and is_jpeg:
        return False
    if scale < 1.0:
        pil = pil.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                         _PILImage.LANCZOS)

    buf = io.BytesIO()
    pil.save(buf, 'JPEG', quality=quality, optimize=True)
    new_data = buf.getvalue()

    # Don't replace if the re-encode isn't actually smaller than the stored stream.
    try:
        if len(new_data) >= len(obj.read_raw_bytes()):
            return False
    except Exception:
        pass

    obj.write(new_data, filter=pikepdf.Name('/DCTDecode'))
    obj.ColorSpace      = pikepdf.Name(colorspace)
    obj.BitsPerComponent = 8
    obj.Width, obj.Height = pil.size
    for k in ('/Decode', '/DecodeParms', '/SMask'):
        if k in obj:
            del obj[k]
    return True


def _compress_page_data(page_data, dpi, quality):
    """Shrink one session page by downsampling/re-encoding only its embedded
    image XObjects, leaving the content stream (text, vector art) intact so the
    page stays searchable — the Adobe-style "reduce file size" behaviour rather
    than a full-page rasterise.  Whichever is smaller (re-encoded vs. original)
    is kept.  Returns (old_size, new_size, replaced)."""
    import pikepdf
    from pdf.ocr_utils import _NATIVE_LOCK

    pdf_bytes = page_data['pdf_bytes']
    old_size  = len(pdf_bytes)

    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    try:
        page = pdf.pages[0]
        try:
            mb     = [float(v) for v in page.mediabox]
            pw, ph = mb[2] - mb[0], mb[3] - mb[1]
        except Exception:
            pw, ph = 612.0, 792.0
        # Target pixel caps assume an image could span the page, so a full-page
        # scan downsamples to ~`dpi` while small inline images are left at their
        # native resolution (scale >= 1.0 → re-encode only, no upscaling).
        max_w = max(1, round(pw / 72.0 * dpi))
        max_h = max(1, round(ph / 72.0 * dpi))

        seen = set()
        for name in list(page.images.keys()):
            obj = page.images[name]
            key = getattr(obj, 'objgen', None)
            if key in seen:
                continue
            if key is not None:
                seen.add(key)
            try:
                _reencode_image_xobject(obj, quality, max_w, max_h)
            except Exception as e:
                logger.warning(f"[compress] image {name} skipped: {e}")

        buf = io.BytesIO()
        pdf.save(buf, compress_streams=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
    finally:
        pdf.close()

    new_bytes = buf.getvalue()
    # Lossless object-stream packing above can shrink even image-free pages, so
    # let the size comparison (not the presence of images) decide.
    if len(new_bytes) >= old_size:
        return old_size, old_size, False

    page_data['pdf_bytes'] = new_bytes
    with _NATIVE_LOCK:
        page_data['thumbnail'] = render_page_thumbnail(new_bytes, 0)
    return old_size, len(new_bytes), True


def _page_is_scanned(pdf_bytes):
    """True if page 0 has essentially no extractable text (image-only / scanned).
    Used to warn before compression that text sharpness may be reduced.  Errs
    toward False so a detection hiccup never shows a spurious warning."""
    import pypdfium2 as _pdfium
    from pdf.ocr_utils import _NATIVE_LOCK
    try:
        with _NATIVE_LOCK:
            doc = _pdfium.PdfDocument(pdf_bytes)
            try:
                return doc[0].get_textpage().count_chars() < 5
            finally:
                doc.close()
    except Exception:
        return False


def register_core_routes(app, sessions, inject_token=None):
    """Register the shared document/session API routes (ingestion, save, rotate,
    compress, metadata, session lifecycle, encryption).  Called from entry.py
    after the Flask app is created.  The app-wide @app.before_request security
    hook in entry.py enforces the token/origin checks on all of these."""

    @app.route('/api/upload', methods=['POST'])
    def upload_pdf():
        session_id = request.form.get('session_id') or str(uuid.uuid4())
        if session_id not in sessions:
            sessions[session_id] = {'pages': [], 'first_file_path': None}
        session = sessions[session_id]

        files    = request.files.getlist('files')
        password = request.form.get('password', '')   # password for encrypted PDFs
        progress_id = request.form.get('progress_id', '')
        new_pages = []
        errors    = []
        password_required = []   # [{ref, filename}] for encrypted files needing a password

        try:
            for file in files:
                safe_name = os.path.basename(file.filename)
                ext = os.path.splitext(safe_name.lower())[1]
                if ext != '.pdf' and ext not in NON_PDF_EXTENSIONS:
                    continue

                tmp_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_{safe_name}")
                file.save(tmp_path)
                if session['first_file_path'] is None:
                    session['first_file_path'] = tmp_path

                pages_, err, pw = _ingest_document(session, tmp_path, safe_name,
                                                   password, progress_id)
                if err:
                    errors.append(err)
                if pw:
                    password_required.append(pw)
                new_pages.extend(pages_)
        finally:
            clear_progress(progress_id)

        logger.info(f"[TRANSITION] upload complete: session={session_id} "
                    f"pages_added={len(new_pages)} errors={len(errors)}")
        return jsonify({
            'session_id':       session_id,
            'pages':            new_pages,
            'save_dir':         get_save_dir(session),
            'errors':           errors,
            'password_required': password_required,
        })

    @app.route('/api/open_paths', methods=['POST'])
    def open_paths():
        """Load files by their real OS path (from the native open dialog).  Unlike
        /api/upload, first_file_path is the genuine source path, so the save dialog
        can default to the folder the document was opened from."""
        data       = request.json or {}
        session_id = data.get('session_id') or str(uuid.uuid4())
        paths      = data.get('paths', []) or []
        password   = data.get('password', '')
        progress_id = data.get('progress_id', '')
        if session_id not in sessions:
            sessions[session_id] = {'pages': [], 'first_file_path': None}
        session = sessions[session_id]

        new_pages, errors, password_required = [], [], []
        try:
            for p in paths:
                if not p or not os.path.isfile(_fs_path(p)):
                    errors.append(f"{os.path.basename(p or '')}: file not found")
                    continue
                safe_name = os.path.basename(p)
                if session['first_file_path'] is None:
                    session['first_file_path'] = p          # real path → save_dir works
                pages_, err, pw = _ingest_document(session, p, safe_name,
                                                   password, progress_id)
                if err:
                    errors.append(err)
                if pw:
                    password_required.append(pw)
                if pages_:
                    _mru_record(p)
                new_pages.extend(pages_)
        finally:
            clear_progress(progress_id)

        logger.info(f"[TRANSITION] open_paths complete: session={session_id} "
                    f"pages_added={len(new_pages)} errors={len(errors)}")
        return jsonify({
            'session_id':       session_id,
            'pages':            new_pages,
            'save_dir':         get_save_dir(session),
            'errors':           errors,
            'password_required': password_required,
        })

    @app.route('/api/progress', methods=['GET'])
    def progress():
        """Poll endpoint for long multi-page operations.  Returns the current
        {current, total, label} for the given job id, or {} when nothing is
        in flight (operation not started yet, or already finished)."""
        return jsonify(get_progress(request.args.get('id', '')))

    @app.route('/api/recent_files', methods=['POST'])
    def recent_files():
        """Recently opened documents (existing files only) for the empty state."""
        items = [e for e in _mru_load() if os.path.isfile(_fs_path(e.get('path', '')))]
        return jsonify({'files': items[:_MRU_MAX]})

    @app.route('/api/rotate', methods=['POST'])
    def rotate_page():
        data = request.json
        session_id = data['session_id']
        page_id = data['page_id']
        direction = data.get('direction', 90)

        session = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        with get_session_lock(session):
            for page in session['pages']:
                if page['id'] == page_id:
                    page['rotation'] = (page['rotation'] + direction) % 360
                    # The recorded dimensions describe the page as displayed, so
                    # a quarter turn swaps them — otherwise the viewer keeps
                    # fitting the document to the pre-rotation width.
                    if direction % 180:
                        page['width_pts'], page['height_pts'] = _rotated_dims(
                            page.get('width_pts', 595.28), page.get('height_pts', 841.89), 90)
                    pdf_bytes = page['pdf_bytes']
                    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
                    writer = pypdf.PdfWriter()
                    p = reader.pages[0]
                    p.rotate(direction % 360 if direction > 0 else 360 + direction)
                    writer.add_page(p)
                    page_buf = io.BytesIO()
                    writer.write(page_buf)
                    new_bytes = page_buf.getvalue()
                    page['pdf_bytes'] = new_bytes
                    thumbnail = render_page_thumbnail(new_bytes, 0)
                    page['thumbnail'] = thumbnail
                    _recovery.mark_dirty(session, page_id)
                    return jsonify({'thumbnail': thumbnail, 'rotation': page['rotation']})

        return jsonify({'error': 'Page not found'}), 404

    @app.route('/api/new_document', methods=['POST'])
    def new_document():
        """Create a fresh session containing a single blank A4 page.
        Used by the viewer's 'New Doc' toolbar button (opens in a new tab)."""
        A4_W, A4_H = 595.28, 841.89
        new_session_id = str(uuid.uuid4())
        page_bytes = _create_blank_page_bytes(A4_W, A4_H)
        try:
            thumbnail = render_page_thumbnail(page_bytes, 0)
        except Exception:
            thumbnail = None
        page_id = str(uuid.uuid4())
        page_data = {
            'id':          page_id,
            'filename':    'New Document',
            'page_num':    1,
            'total_pages': 1,
            'rotation':    0,
            'thumbnail':   thumbnail,
            'pdf_bytes':   page_bytes,
            'form_fields': [],
            'width_pts':   A4_W,
            'height_pts':  A4_H,
        }
        sessions[new_session_id] = {'pages': [page_data], 'first_file_path': None,
                                    'comments': {}}
        return jsonify({
            'session_id': new_session_id,
            'page_id':    page_id,
            'pages':      [{k: v for k, v in page_data.items() if k != 'pdf_bytes'}],
        })

    @app.route('/api/save', methods=['POST'])
    def save_pdf():
        data = request.json
        session_id = data['session_id']
        requested_path = data.get('save_path', '').strip()
        page_ids = data.get('page_ids')          # optional subset (Save Selected Pages)

        session = sessions.get(session_id)
        if not session or not session['pages']:
            return jsonify({'error': 'No pages to save', 'error_key': 'err.no_pages_to_save'}), 400

        pages_to_save = _resolve_save_pages(session, page_ids)
        if not pages_to_save:
            return jsonify({'error': 'No pages to save', 'error_key': 'err.no_pages_to_save'}), 400

        # A full selection (every page checked) is a normal full-document save:
        # keep outlines/bookmarks and remember the chosen name.  Only a genuine
        # subset skips outlines (page indices wouldn't line up) and must not
        # hijack the document's save name.
        is_subset = len(pages_to_save) != len(session['pages'])
        # Building the PDF can fail on an unusual document.  Report why instead of
        # letting Flask return an HTML 500 the client can only read as "failed".
        try:
            merged_bytes = _build_merged_pdf(session, pages_to_save, include_outlines=not is_subset)
        except Exception as exc:
            logger.exception('[save] could not build the PDF')
            return jsonify({'error': f'Could not build the PDF: {exc}'}), 500
        out_buf = io.BytesIO(merged_bytes)
        out_buf.seek(0)

        if requested_path:
            save_path = requested_path
        else:
            save_dir = get_save_dir(session)
            save_path = os.path.join(save_dir, get_save_filename(session))

        try:
            os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
            with open(_fs_path(save_path), 'wb') as f:
                f.write(out_buf.getvalue())
            # Remember the chosen name so a later save (in any panel) defaults to it —
            # but only for full-document saves, so a one-off selection export doesn't
            # hijack the document's save name.
            if not is_subset:
                session['save_name'] = os.path.basename(save_path)
                _recovery.clear(session)   # working state is now on disk
            return jsonify({'success': True, 'saved_to': save_path})
        except Exception as exc:
            # The bytes are fine, the disk write is not (permissions, a locked
            # file, a path that has gone away).  Log why — the streamed download
            # below is a silent fallback and would otherwise hide the reason.
            logger.exception(f'[save] could not write {save_path} — streaming a download instead')
            out_buf.seek(0)
            resp = send_file(out_buf, mimetype='application/pdf',
                             as_attachment=True, download_name=get_save_filename(session))
            # Let a client that asked for a specific path tell the operator why it
            # did not get one, rather than reporting a bare failure.
            resp.headers['X-Save-Error'] = str(exc).replace('\n', ' ')[:200]
            return resp

    @app.route('/api/save_blob', methods=['POST'])
    def save_blob():
        """Return the merged PDF as a raw binary download (used by File System Access API)."""
        data = request.json
        session_id = data['session_id']
        page_ids = data.get('page_ids')          # optional subset (Save Selected Pages)
        session = sessions.get(session_id)
        if not session or not session['pages']:
            return jsonify({'error': 'No pages to save', 'error_key': 'err.no_pages_to_save'}), 400

        pages_to_save = _resolve_save_pages(session, page_ids)
        if not pages_to_save:
            return jsonify({'error': 'No pages to save', 'error_key': 'err.no_pages_to_save'}), 400

        # Full selection => full-document save (keep outlines); subset skips them.
        is_subset = len(pages_to_save) != len(session['pages'])
        merged_bytes = _build_merged_pdf(session, pages_to_save, include_outlines=not is_subset)
        out_buf = io.BytesIO(merged_bytes)
        out_buf.seek(0)
        return send_file(out_buf, mimetype='application/pdf',
                         as_attachment=True, download_name=get_save_filename(session))

    @app.route('/api/compress', methods=['POST'])
    def compress_pdf():
        """Re-render pages at reduced DPI/quality to shrink file size.
        With a page_id the request compresses that single page — the layout view
        calls per page so it can show progress between requests."""
        data       = request.json
        session_id = data['session_id']
        dpi        = max(72, min(300, int(data.get('dpi', 150))))
        quality    = max(10, min(95, int(data.get('quality', 60))))
        page_id    = data.get('page_id')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        targets = [p for p in session['pages']
                   if page_id is None or p['id'] == page_id]
        if page_id is not None and not targets:
            return jsonify({'error': 'Page not found'}), 404

        results, old_total, new_total, failed = [], 0, 0, 0
        with get_session_lock(session):
            for page_data in targets:
                try:
                    old_size, new_size, replaced = _compress_page_data(page_data, dpi, quality)
                except Exception as e:
                    logger.warning(f"[compress] Page {page_data.get('id')} failed: {e}")
                    failed += 1
                    old_size = new_size = len(page_data['pdf_bytes'])
                    replaced = False
                old_total += old_size
                new_total += new_size
                if replaced:
                    _recovery.mark_dirty(session, page_data['id'])
                results.append({
                    'id':        page_data['id'],
                    'thumbnail': page_data.get('thumbnail'),
                    'replaced':  replaced,
                })

        return jsonify({'pages': results, 'old_size': old_total, 'new_size': new_total,
                        'failed': failed, 'success': failed == 0})

    @app.route('/api/compress/scan_check', methods=['POST'])
    def compress_scan_check():
        """Report whether any of the given (or all) session pages are scanned/
        image-only, so the layout view can show a 'reduces text sharpness'
        warning before compressing.  Stops at the first scanned page found."""
        data    = request.json or {}
        session = sessions.get(data.get('session_id', ''))
        if not session:
            return jsonify({'scanned': False})
        ids = set(data.get('page_ids') or [])
        for page_data in session['pages']:
            if ids and page_data['id'] not in ids:
                continue
            if _page_is_scanned(page_data['pdf_bytes']):
                return jsonify({'scanned': True})
        return jsonify({'scanned': False})

    @app.route('/api/session/pages', methods=['POST'])
    def session_pages():
        """Return existing session pages for entry.html to restore layout view."""
        data = request.json
        session_id = data.get('session_id', '')
        session = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        touch_session(session_id)
        pages = [{k: v for k, v in p.items() if k != 'pdf_bytes'}
                 for p in session['pages']]
        return jsonify({
            'pages': pages,
            'save_dir': get_save_dir(session),
        })

    @app.route('/api/clear', methods=['POST'])
    def clear_session():
        data = request.json
        session_id = data.get('session_id')
        sessions.pop(session_id, None)
        forget_session(session_id)
        return jsonify({'success': True})

    @app.route('/api/metadata', methods=['POST'])
    def metadata_endpoint():
        """Get or set PDF document metadata (title, author, subject, keywords).

        action='get' → return current session metadata
        action='set' → update session metadata (applied at save time)
        """
        data       = request.json
        session_id = data.get('session_id', '')
        action     = data.get('action', 'get')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        if action == 'get':
            return jsonify({'metadata': session.get('metadata', {
                'title': '', 'author': '', 'subject': '', 'keywords': ''
            })})

        if action == 'set':
            incoming = data.get('metadata', {})
            session['metadata'] = {
                'title':    str(incoming.get('title',    '') or ''),
                'author':   str(incoming.get('author',   '') or ''),
                'subject':  str(incoming.get('subject',  '') or ''),
                'keywords': str(incoming.get('keywords', '') or ''),
            }
            # Applied at save time, so until then the session differs from disk.
            _recovery.mark_meta_dirty(session)
            return jsonify({'success': True})

        return jsonify({'error': 'Unknown action'}), 400

    @app.route('/api/retry_encrypted', methods=['POST'])
    def retry_encrypted():
        """Process an encrypted file that was deferred during upload.
        Body: {ref, password, session_id}
        Returns the same shape as /api/upload for the single file.
        """
        data       = request.json
        ref        = data.get('ref', '')
        password   = data.get('password', '')
        session_id = data.get('session_id') or str(uuid.uuid4())

        enc = _encrypted_refs.pop(ref, None)
        if not enc:
            return jsonify({'error': 'Reference expired or invalid'}), 400

        tmp_path  = enc['path']
        safe_name = enc['filename']

        try:
            with open(tmp_path, 'rb') as fh:
                pdf_bytes = fh.read()
        except OSError as e:
            return jsonify({'error': f'Could not read file: {e}'}), 500

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            result = reader.decrypt(password)
            if result == 0:
                return jsonify({'error': 'Incorrect password', 'error_key': 'err.incorrect_password'}), 400
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            reader.decrypt(password)

        if session_id not in sessions:
            sessions[session_id] = {'pages': [], 'first_file_path': tmp_path}
        session = sessions[session_id]
        if session.get('first_file_path') is None:
            session['first_file_path'] = tmp_path
        session['password'] = password   # preserve so saving re-encrypts

        # Write decrypted bytes for thumbnail rendering and form-field extraction —
        # pypdfium2 cannot render an encrypted PDF and would raise, returning HTML.
        dec_pdf_bytes = _get_decrypted_pdf_bytes(reader)

        num_pages          = len(reader.pages)
        upload_form_fields = _form_utils.extract_all_form_fields(dec_pdf_bytes)
        new_pages          = []

        for i in range(num_pages):
            page_id = str(uuid.uuid4())
            w_pts, h_pts = _page_dimensions(reader, i)
            # Build single-page bytes from the decrypted reader first
            pw = pypdf.PdfWriter()
            pw.add_page(reader.pages[i])
            pb = io.BytesIO()
            pw.write(pb)
            page_bytes = pb.getvalue()
            # Render thumbnail from the single unencrypted page (index 0)
            thumbnail = render_page_thumbnail(page_bytes, 0)
            page_data  = {
                'id':          page_id,
                'filename':    safe_name,
                'page_num':    i + 1,
                'total_pages': num_pages,
                'rotation':    0,
                'thumbnail':   thumbnail,
                'pdf_bytes':   page_bytes,
                'form_fields': upload_form_fields.get(i, []),
                'width_pts':   w_pts,
                'height_pts':  h_pts,
            }
            session['pages'].append(page_data)
            new_pages.append({k: v for k, v in page_data.items() if k != 'pdf_bytes'})

        return jsonify({
            'session_id': session_id,
            'pages':      new_pages,
            'save_dir':   get_save_dir(session),
            'errors':     [],
        })

    @app.route('/api/unlock_session', methods=['POST'])
    def unlock_session():
        """Unlock a session whose file is encrypted: either one that could not be
        opened at all without a password, or one already open read-only whose
        owner password lifts the editing restrictions.
        Body: {session_id, password}
        """
        data       = request.json
        session_id = data.get('session_id', '')
        password   = data.get('password', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        file_path = session.get('encrypted_file_path', '')
        if not file_path:
            return jsonify({'error': 'No encrypted file pending for this session'}), 400

        # A restricted document already has its pages loaded — clear them so the
        # reload does not append a second copy of every page, but keep a copy so
        # a rejected password leaves the operator's view exactly as it was.
        prev_pages       = session['pages']
        prev_restricted  = session.get('restricted', False)
        session['pages'] = []
        _preload_file(file_path, session_id, password=password)

        # Still restricted means the password opened the file with user rights
        # only, so it unlocked nothing — report that rather than do nothing.
        if not session['pages'] or session.get('restricted'):
            hard_failure     = not session['pages'] and not session.get('needs_password')
            session['pages'] = prev_pages
            if prev_pages:
                session['restricted']          = prev_restricted
                session['encrypted_file_path'] = file_path
                session.pop('needs_password', None)
                session.pop('wrong_password', None)
            if hard_failure and not prev_pages:
                return jsonify({'error': 'Could not load file',
                                'error_key': 'err.could_not_load_file'}), 500
            return jsonify({'error': 'Incorrect password',
                            'error_key': 'err.incorrect_password'}), 400

        first_page = session['pages'][0]
        return jsonify({
            'success':    True,
            'page_id':    first_page['id'],
            'filename':   first_page['filename'],
            'restricted': False,
        })

    @app.route('/api/set_password', methods=['POST'])
    def set_password():
        """Set or remove the save-time encryption password for a session.
        Body: {session_id, password}  — empty string removes protection.
        """
        data       = request.json
        session_id = data.get('session_id', '')
        password   = data.get('password', '')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        if password:
            session['password'] = password
            _recovery.clear(session)   # never keep decrypted bytes for a protected doc
        else:
            was_protected = bool(session.pop('password', None))
            if was_protected:
                # Snapshots were suppressed while the document was protected, so
                # there is nothing on disk for it — re-take the whole thing now
                # that plaintext is allowed again.
                _recovery.mark_dirty(session)
        return jsonify({'success': True, 'protected': bool(password)})

    @app.route('/api/diag_office', methods=['GET'])
    def diag_office():
        """Diagnostic endpoint — returns everything we can find about Office on this machine."""
        import traceback
        result = {}

        # 1. Platform
        result['platform'] = sys.platform

        # 2. Can we import win32com?
        try:
            import win32com.client as _wc
            result['win32com'] = 'ok'
        except ImportError as e:
            result['win32com'] = f'IMPORT ERROR: {e}'
            return jsonify(result)

        # 3. Try dispatching Word.Application
        try:
            app_w = _wc.Dispatch('Word.Application')
            result['Word.Application'] = 'dispatched OK'
            try:
                app_w.Quit()
            except Exception:
                pass
        except Exception as e:
            result['Word.Application'] = f'FAILED: {type(e).__name__}: {e}'

        # 4. Try dispatching Excel.Application
        try:
            app_e = _wc.Dispatch('Excel.Application')
            result['Excel.Application'] = 'dispatched OK'
            try:
                app_e.Quit()
            except Exception:
                pass
        except Exception as e:
            result['Excel.Application'] = f'FAILED: {type(e).__name__}: {e}'

        # 5. Check registry for Office install paths
        result['registry'] = {}
        try:
            import winreg
            keys_to_check = [
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Office\ClickToRun\Configuration'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WINWORD.EXE'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\EXCEL.EXE'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Classes\Word.Application\CLSID'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Classes\Excel.Application\CLSID'),
            ]
            for hive, path in keys_to_check:
                try:
                    key = winreg.OpenKey(hive, path)
                    val, _ = winreg.QueryValueEx(key, '')
                    result['registry'][path] = val
                except Exception as e:
                    result['registry'][path] = f'NOT FOUND: {e}'
        except ImportError:
            result['registry'] = 'winreg not available (not Windows)'

        # 6. Live conversion test with a tiny docx
        try:
            import win32com.client as _wc2, tempfile as _tf, os as _os
            from docx import Document as _D
            doc = _D()
            doc.add_paragraph('test')
            tmp_docx = _os.path.join(_tf.mkdtemp(), 'test.docx')
            tmp_pdf  = _os.path.join(_tf.mkdtemp(), 'test.pdf')
            doc.save(tmp_docx)
            app_w2 = _wc2.Dispatch('Word.Application')
            app_w2.Visible = False
            d = app_w2.Documents.Open(_os.path.abspath(tmp_docx), ReadOnly=True)
            d.SaveAs(_os.path.abspath(tmp_pdf), FileFormat=17)
            d.Close(False)
            app_w2.Quit()
            result['live_docx_test'] = f'OK - pdf size {_os.path.getsize(tmp_pdf)} bytes'
        except Exception as e:
            result['live_docx_test'] = f'FAILED: {type(e).__name__}: {e}\n{traceback.format_exc()}'

        return jsonify(result)
