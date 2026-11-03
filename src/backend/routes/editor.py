"""
editor.py  —  Patchwork PDF  —  Page Editor Backend
Gareth Finch 2026

Handles:
  - Rendering a page at editor resolution
  - Extracting text & graphic elements with positions
  - Applying edits (move elements, edit text, add markup) back into the PDF
"""

import io
import base64
import random
import re
import hashlib
import copy as _copy
import pypdf
from bootstrap import recovery as _recovery
import math
import pypdf as _pypdf
import threading as _editor_threading
import logging
from pdf import ocr_utils as _ocr_utils
from platform_io import font_assets as _font_assets
from pdf.ocr_utils import _NATIVE_LOCK
from pdf import pdf_core as _pdf_core
from pypdf.generic import NameObject, DecodedStreamObject, ArrayObject, DictionaryObject
from flask import request, jsonify

logger = logging.getLogger(__name__)

# ── Heavy deps — loaded lazily in _heavy_init() to avoid blocking startup ─────
np           = None   # numpy
PILImage     = None   # PIL.Image
_pytesseract = None   # pytesseract
pdfium       = None   # pypdfium2

_OCR_AVAILABLE          = False
_OCR_UNAVAILABLE_REASON = None

_INIT_DONE = False
_INIT_LOCK = _editor_threading.Lock()

GHOST_OVERLAP_THRESH = 0.4

# ── OCR result cache ──────────────────────────────────────────────────────────
# Tesseract OCR is deterministic for a given page image, but it's re-run every
# time a scanned page is opened or an edit is applied (Apply re-extracts text
# from the unchanged source scan). Cache results keyed by a hash of the source
# bytes + render scale so the expensive two-pass OCR runs once per page. Callers
# always receive a deep copy, so the cached canonical list is never mutated.
_OCR_CACHE       = {}
_OCR_CACHE_ORDER = []
_OCR_CACHE_MAX   = 32
_OCR_CACHE_LOCK  = _editor_threading.Lock()

def _heavy_init():
    """Load numpy, PIL, pypdfium2 and probe Tesseract — deferred from startup.
    Kicked off via start_heavy_init_background() once the main window has
    loaded, with a safety net on the first editor route, so heavy deps are
    never None when route code runs."""
    global np, PILImage, _pytesseract, pdfium
    global _OCR_AVAILABLE, _OCR_UNAVAILABLE_REASON, _INIT_DONE

    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return

        try:
            from bootstrap import boot_timing as _boot
            _boot.mark('heavy deps: load start (numpy/pdfium/PIL/OCR)')
        except Exception:
            _boot = None

        import numpy as _np;         np      = _np
        import pypdfium2 as _pdfium; pdfium  = _pdfium
        from PIL import Image as _PI; PILImage = _PI       # noqa: E702

        _ocr_utils.init_ocr()
        _OCR_AVAILABLE          = _ocr_utils.is_ocr_available()
        _OCR_UNAVAILABLE_REASON = _ocr_utils.ocr_unavailable_reason()
        _pytesseract            = _ocr_utils._pytesseract

        _INIT_DONE = True
        if _boot:
            _boot.mark('heavy deps: loaded')
        logger.info("Heavy dependencies loaded.")


_INIT_THREAD_STARTED = False


def start_heavy_init_background():
    """Begin loading the heavy deps on a background thread.  Idempotent.

    entry.py calls this once the main window has finished loading (and right
    away in headless mode), so the import I/O and the Tesseract probe never
    compete with WebView2 startup.  Anything hitting an editor route earlier
    is covered by the _ensure_editor_ready safety net."""
    global _INIT_THREAD_STARTED
    if _INIT_THREAD_STARTED or _INIT_DONE:
        return
    _INIT_THREAD_STARTED = True
    _editor_threading.Thread(target=_heavy_init, daemon=True,
                             name='editor-init').start()


# ── Resolution ────────────────────────────────────────────────────────────────
EDITOR_SCALE = 2.0          # render scale for the editor canvas (≈144 dpi)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pdf_bytes_from_session(session, page_id):
    for p in session.get('pages', []):
        if p['id'] == page_id:
            return p['pdf_bytes'], p
    return None, None


def _render_page_hires(pdf_bytes, scale=EDITOR_SCALE):
    """Render page[0] of pdf_bytes to base64 PNG at given scale."""
    with _NATIVE_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            page   = doc[0]
            w      = page.get_width()
            h      = page.get_height()
            bitmap = page.render(scale=scale)
            img    = bitmap.to_pil()
        finally:
            doc.close()
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('utf-8'), w, h


# ── Rotated-page coordinate orientation for the editor canvas ─────────────────
# pdfium renders a /Rotate≠0 page upright (display frame) but reports text/OCR
# boxes in the unrotated MediaBox frame.  The JS editor canvas works in the
# display frame, so extracted items must be mapped raw→display before being sent
# (else they land elsewhere and can't be hovered/selected — exactly the symptom
# on rotated scans).  apply_edits() inverts this with display_to_raw() on save.
# All of this is a no-op for /Rotate 0, so unrotated pages are unaffected.

def _rot_pt_raw_to_display(x, y, rotate, raw_W, raw_H):
    """Map a point (y-up) from raw content space to the displayed (post-/Rotate)
    frame pdfium renders.  Exact inverse of apply_edits' display_to_raw()."""
    if rotate == 90:
        return y, raw_W - x
    if rotate == 180:
        return raw_W - x, raw_H - y
    if rotate == 270:
        return raw_H - y, x
    return x, y


def _orient_text_items_for_display(items, rotate, raw_W, raw_H):
    """Map each text item's coords from raw content space into the upright
    display frame.  Box corners are transformed and re-normalised; original_x/y
    track the new bottom-left.  Identity for /Rotate 0; non-text items pass
    through untouched."""
    if not rotate:
        return items
    out = []
    for it in items:
        if it.get('type') != 'text':
            out.append(it)
            continue
        n = dict(it)
        ax, ay = _rot_pt_raw_to_display(float(it['x']),  float(it['y']),  rotate, raw_W, raw_H)
        bx, by = _rot_pt_raw_to_display(float(it['x2']), float(it['y2']), rotate, raw_W, raw_H)
        n['x'],  n['y']  = min(ax, bx), min(ay, by)
        n['x2'], n['y2'] = max(ax, bx), max(ay, by)
        if 'original_x' in n: n['original_x'] = n['x']
        if 'original_y' in n: n['original_y'] = n['y']
        if 'baseline_y' in n: n['baseline_y'] = n['y']   # box-bottom ≈ baseline
        out.append(n)
    return out


def _editor_display_orientation(pdf_bytes, page_h, text_items, page_top, page_left):
    """Orient extracted items + canvas origin for the editor on a rotated page.

    Returns (text_items, page_top, page_left).  On a rotated, uncropped page the
    items are mapped raw→display and the canvas origin is moved to the display
    frame (pageTop = display height, pageLeft = 0).  Unchanged for /Rotate 0 and
    for cropped pages (CropBox != MediaBox), which keep the existing behaviour."""
    try:
        pg = _pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0]
        rotate = int(pg.get('/Rotate', 0)) % 360
        if not rotate:
            return text_items, page_top, page_left
        mb, cb = pg.mediabox, pg.cropbox
        cropped = (abs(float(cb.left)   - float(mb.left))   > 0.5 or
                   abs(float(cb.bottom) - float(mb.bottom)) > 0.5 or
                   abs(float(cb.width)  - float(mb.width))  > 0.5 or
                   abs(float(cb.height) - float(mb.height)) > 0.5)
        if cropped:
            logger.warning('[editor_rotate] cropped+rotated page; leaving coords '
                           'in raw space (unsupported combination)')
            return text_items, page_top, page_left
        ti = _orient_text_items_for_display(
            text_items, rotate, float(mb.width), float(mb.height))
        return ti, float(page_h), 0.0
    except Exception:
        logger.exception('[editor_rotate] orientation failed; using raw coords')
        return text_items, page_top, page_left


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# Cap-height ratios (capHeight / em) for the base-14 standard fonts.  Bundled
# fonts are looked up via font_assets.  Used to convert a measured visual glyph
# height (e.g. from OCR) into the em size that renders at that height.
_BASE14_CAP_H = {
    'Helvetica': 0.718, 'Helvetica-Bold': 0.718,
    'Helvetica-Oblique': 0.718, 'Helvetica-BoldOblique': 0.718,
    'Times-Roman': 0.662, 'Times-Bold': 0.676,
    'Times-Italic': 0.653, 'Times-BoldItalic': 0.676,
    'Courier': 0.571, 'Courier-Bold': 0.571,
    'Courier-Oblique': 0.571, 'Courier-BoldOblique': 0.571,
}


def _cap_ratio(rl_font):
    """Cap-height/em ratio for any logical font (bundled or base-14)."""
    r = _font_assets.cap_height_ratio(rl_font)
    if r is not None:
        return r
    return _BASE14_CAP_H.get(rl_font, 0.718)


def _ocr_box_ratio(text, rl_font):
    """Expected ink-box height of *text* in em units for *rl_font*: tallest
    glyph top minus deepest glyph bottom, measured from the real font file.

    An OCR word box spans the word's ink, and that span depends on which
    characters are present: "2026" spans cap height only (~0.64 em in Carlito)
    while "Prepayment" runs from the cap of 'P' to the descenders of 'p'/'y'
    (~0.80 em).  Dividing the measured box height by THIS ratio gives both
    words the same em size; dividing by cap height alone (the old behaviour)
    inflated any word with descenders by ~25-30%.

    Returns None when nothing is measurable (non-bundled face, all-symbol
    text) or the span is degenerate — caller falls back to cap height."""
    top = bot = None
    for ch in set(text):
        ext = _font_assets.char_ink_extents(rl_font, ch)
        if ext is None:
            continue
        top = ext[0] if top is None else max(top, ext[0])
        bot = ext[1] if bot is None else min(bot, ext[1])
    if top is None or (top - bot) < 0.05:
        return None
    return top - bot


# PDF font-descriptor /Flags bits (PDF 32000-1 §9.8.2) — passed through from
# pdfium's per-char FPDFText_GetFontInfo so style/family detection no longer
# depends on the font NAME alone (subset fonts often have meaningless names).
_FD_FIXED_PITCH = 1 << 0
_FD_SERIF       = 1 << 1
_FD_ITALIC      = 1 << 6
_FD_FORCE_BOLD  = 1 << 18


def _safe_rl_font(name, flags=0, weight=0):
    """Map a native PDF font name to a logical editor font.

    Phase C — native font matching: recognised typeface families are mapped to
    the *bundled* faces (Calibri/Arial/TimesNR/CourierNew via Carlito/Liberation)
    so an edited native run embeds and previews as a close match to the original
    instead of collapsing to base-14 Helvetica.  This is deterministic — we know
    the real /BaseFont name, no glyph-image guessing.

    *flags* (font-descriptor /Flags) and *weight* (OS/2 weight class, 400=regular
    700=bold) refine the match when available: bold/italic faces whose name does
    not say so (common in subsetted fonts) are still detected, and unrecognised
    names with the Serif/FixedPitch descriptor bits map to the matching bundled
    family rather than collapsing to Helvetica.

    Liberation Sans/Serif/Mono are metric-identical to Arial/Times/Courier, so
    mapping those to the bundled faces is visually lossless and adds true WYSIWYG
    + portability (the font travels with the file).  Genuinely unrecognised fonts
    fall back to base-14 Helvetica (unchanged behaviour, no embedding).
    """
    n = (name or '').lower()
    if '+' in n:                       # strip subset prefix (ABCDEF+Arial-Bold)
        n = n.split('+', 1)[1]
    flags  = flags or 0
    weight = weight or 0
    is_bold   = (any(x in n for x in ('bold', 'black', 'heavy', 'semibold', 'demibold', 'demi'))
                 or weight >= 600 or bool(flags & _FD_FORCE_BOLD))
    is_italic = (any(x in n for x in ('italic', 'oblique', 'slant'))
                 or bool(flags & _FD_ITALIC))

    def _variant(base):
        if is_bold and is_italic: return base + '-BoldItalic'
        if is_bold:               return base + '-Bold'
        if is_italic:             return base + '-Italic'
        return base

    # Monospace / typewriter
    if any(x in n for x in ('courier', 'mono', 'consol', 'inconsolata', 'typewriter')):
        return _variant('CourierNew')
    # Serif
    if any(x in n for x in ('times', 'roman', 'serif', 'georgia', 'garamond', 'cambria',
                            'minion', 'palatino', 'antiqua', 'century', 'plantin',
                            'caslon', 'baskerville', 'sabon', 'constantia')):
        return _variant('TimesNR')
    # Calibri family
    if any(x in n for x in ('calibri', 'carlito')):
        return _variant('Calibri')
    # Other common humanist/grotesque sans → Arial (metric-identical to Arial/Helvetica)
    if any(x in n for x in ('arial', 'helvetica', 'liberation sans', 'segoe', 'verdana',
                            'tahoma', 'trebuchet', 'franklin', 'open sans', 'roboto',
                            'lato', 'corbel', 'candara', 'sans')):
        return _variant('Arial')

    # Name unrecognised — classify by descriptor flags when present
    if flags & _FD_FIXED_PITCH:
        return _variant('CourierNew')
    if flags & _FD_SERIF:
        return _variant('TimesNR')

    # Unrecognised → base-14 Helvetica (no embedding, unchanged behaviour)
    if is_bold and is_italic: return 'Helvetica-BoldOblique'
    if is_bold:               return 'Helvetica-Bold'
    if is_italic:             return 'Helvetica-Oblique'
    return 'Helvetica'



def is_image_only_page(pdf_bytes):
    """Return True if the page has no extractable text (image-only / scanned PDF)."""
    with _NATIVE_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            return doc[0].get_textpage().count_chars() == 0
        finally:
            doc.close()


# ── Mixed pages: a scan with some native text written on top ──────────────────
# Editing a scan injects real text, and saving writes that page out as-is.  On
# reopen there is no pristine copy in memory any more (`ocr_source_bytes` lives
# on the session, not in the file), the page has native characters, and every
# check that asks "is this a scan?" by counting characters says no — so only the
# edited words were offered for editing, and text selection stopped OCRing.
# v2.24 left the user a manual "Force OCR" button for this; recognising the page
# makes the button unnecessary.
_MIXED_MIN_IMAGE_COVER = 0.60   # one image over this share of the page = a scan
_MIXED_MAX_NATIVE_CHARS = 300   # a real text layer on a page is far denser
_PW_INJECT_MARK = b'% pw_inj'   # emit_text's marker; survives our own Save


_PW_INJECT_BLOCK = re.compile(rb'% pw_inj [0-9a-f]+\s(.*?)\bET\b', re.S)
_PDF_LITERAL_TJ  = re.compile(rb'\(((?:\\.|[^\\)])*)\)\s*Tj', re.S)
_PDF_ESCAPE      = re.compile(rb'\\(?:[0-7]{1,3}|.)', re.S)


def _injected_visible_chars(content):
    """Non-whitespace characters emit_text wrote into *content* — the text of
    every `(...) Tj` inside a `% pw_inj` block, escapes counted as one."""
    n = 0
    for block in _PW_INJECT_BLOCK.findall(content):
        for lit in _PDF_LITERAL_TJ.findall(block):
            n += len(re.sub(rb'\s', b'', _PDF_ESCAPE.sub(b'x', lit)))
    return n


def is_mixed_scan_page(pdf_bytes):
    """True for a page whose words are mostly NOT in its text layer, yet which
    has some native text — so the character count calls it a text page.

    Two ways to be one:
      * every native character on it is one we injected.  That is a scanned
        (or outlined-glyph) page that was edited and saved: its real words
        were only ever reachable through OCR.  The sample invoice draws its
        glyphs as vector outlines with no image at all, so this test — not
        image coverage — is the one that catches our own saves.
      * a single image covers most of the page and the native text is too
        sparse to be the page's own text layer (someone else's scan with a
        stamp or typed note on it).  The sparseness condition is what keeps a
        searchable PDF — a scan under a complete invisible text layer — on the
        native path, where it already works and OCR would add nothing.

    Never raises: this is a routing hint, and "no" is always a safe answer.
    """
    try:
        with _NATIVE_LOCK:
            doc = pdfium.PdfDocument(pdf_bytes)
            try:
                page = doc[0]
                tp = page.get_textpage()
                n_chars = tp.count_chars()
                if n_chars == 0:
                    return False          # image-only: the ordinary scan path
                native_visible = len(re.sub(r'\s', '', tp.get_text_range() or ''))
                pw, ph = page.get_width(), page.get_height()
                cover = 0.0
                if n_chars <= _MIXED_MAX_NATIVE_CHARS:
                    area = max(pw * ph, 1.0)
                    import pypdfium2.raw as _pc
                    for obj in page.get_objects(max_depth=2):
                        if obj.type != _pc.FPDF_PAGEOBJ_IMAGE:
                            continue
                        l, b, r, t = obj.get_bounds()
                        cover = max(cover, max(0.0, min(r, pw) - max(l, 0)) *
                                           max(0.0, min(t, ph) - max(b, 0)) / area)
            finally:
                doc.close()
        if cover >= _MIXED_MIN_IMAGE_COVER:
            return True
        content = _pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].get_contents()
        content = content.get_data() if content is not None else b''
        if _PW_INJECT_MARK not in content:
            return False
        # A little slack for characters pdfium reports that we did not write
        # (a stray ligature split, a hyphen it synthesises at a line end).
        return native_visible <= _injected_visible_chars(content) + 2
    except Exception:
        logger.debug('[mixed] detection failed', exc_info=True)
        return False


def _drop_ocr_under_native(ocr_items, native_items, frac=0.5):
    """OCR words whose box is mostly covered by a native run.

    On a mixed page the raster the OCR reads already shows the native text
    (the edits are drawn into it), so without this every edited word would be
    offered twice — once as the native run, once as OCR of its own pixels.
    """
    boxes = [(float(n['x']), float(n['y']), float(n['x2']), float(n['y2']))
             for n in native_items
             if n.get('x') is not None and n.get('x2') is not None]
    if not boxes:
        return ocr_items
    kept = []
    for it in ocr_items:
        if it.get('x') is None or it.get('x2') is None:
            kept.append(it)            # sentinels (__ocr_notice__) pass through
            continue
        x1, y1 = float(it['x']), float(it['y'])
        x2, y2 = float(it['x2']), float(it['y2'])
        a = max((x2 - x1) * (y2 - y1), 1e-6)
        under = any(max(0.0, min(x2, bx2) - max(x1, bx1)) *
                    max(0.0, min(y2, by2) - max(y1, by1)) / a >= frac
                    for bx1, by1, bx2, by2 in boxes)
        if not under:
            kept.append(it)
    return kept


def _image_is_fully_transparent(img_obj):
    """Return True only when we can PROVE an image XObject renders nothing —
    its soft mask is alpha 0 everywhere — so it is safe to drop from the editor's
    selectable-image list (it is invisible: contributes no pixels to the page).

    Deliberately keys on TRANSPARENCY, never on colour: a solid white (or any
    opaque) image is a legitimate cover/hide and is always kept.  Conservative by
    design — any uncertainty (no/odd soft mask, non-8-bit, non-default /Decode,
    decode failure) returns False so a real image is never hidden from the user.
    """
    try:
        sm = img_obj.get('/SMask')
        if sm is None:
            return False                      # no soft mask → assume visible
        sm = sm.get_object() if hasattr(sm, 'get_object') else sm
        if int(sm.get('/BitsPerComponent', 8)) != 8:
            return False                      # only reason about the 8-bit case
        dec = sm.get('/Decode')
        if dec is not None:
            try:
                d = [float(v) for v in dec]
            except Exception:
                return False
            if d[:2] != [0.0, 1.0]:           # a non-default decode can invert 0→opaque
                return False
        data = sm.get_data()                  # FlateDecode etc. applied by pypdf
        if not data:
            return False
        return max(data) == 0                 # every alpha byte 0 → fully transparent
    except Exception:
        return False


def extract_pdf_images(pdf_bytes, exclude_xobj_names=None):
    """
    Detect discrete embedded images (XObject /Subtype /Image) on page 0.
    exclude_xobj_names: optional set of XObject resource names to skip
    (used after apply to hide images that were moved or deleted).

    For native PDFs: walks the content stream for 'Do' operators, resolves
    the XObject, and recovers the image's position + dimensions from the CTM
    that was active at the time of the Do call.

    For OCR/scanned PDFs (image-only pages): the entire page is one big image
    — we deliberately return nothing, since moving/deleting the whole-page scan
    is not useful. Only images that occupy clearly less than 80% of page area
    are returned.

    Returns a list of dicts:
        id, type='pdf_image', x, y, w, h, dataUrl (data:image/png;base64,...)
    All coordinates are in PDF user-space (y=0 at bottom), matching the
    display coordinate system used by pdfium/the editor.
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        page   = reader.pages[0]
        mb     = page.mediabox
        page_w = float(mb.width)
        page_h = float(mb.height)
        rotate = int(page.get('/Rotate', 0)) % 360
    except Exception:
        return []

    # For a scanned page (full-page image) we return nothing.
    # We detect this via is_image_only_page — if the whole page is an image,
    # skip (no discrete sub-images to offer).
    if is_image_only_page(pdf_bytes):
        return []

    # Collect all XObject /Image entries from page /Resources
    resources = page.get('/Resources')
    if resources is None:
        return []
    if hasattr(resources, 'get_object'):
        resources = resources.get_object()
    xobjects = resources.get('/XObject', {})
    if hasattr(xobjects, 'get_object'):
        xobjects = xobjects.get_object()

    image_xobjs = {}  # resource_name (no /) -> stream object
    for name, ref in xobjects.items():
        try:
            obj = ref.get_object() if hasattr(ref, 'get_object') else ref
            if str(obj.get('/Subtype', '')) != '/Image':
                continue
            # Skip fully-transparent (invisible) images — authoring artifacts that
            # render nothing yet would otherwise surface as phantom selectable
            # elements over the page (e.g. an empty title-block image placeholder).
            if _image_is_fully_transparent(obj):
                logger.debug(f"[PDF_IMG] skipping fully-transparent image '{name}'")
                continue
            image_xobjs[str(name).lstrip('/')] = obj
        except Exception:
            pass

    if not image_xobjs:
        return []

    # Walk content stream to find 'Do' calls and the CTM active at that moment.
    # We tokenise the raw stream ourselves so we capture the stack properly.
    try:
        contents = page.get('/Contents')
        if contents is None:
            return []
        obj = contents.get_object() if hasattr(contents, 'get_object') else contents
        if isinstance(obj, pypdf.generic.ArrayObject):
            raw = b''.join(
                o.get_object().get_data() for o in obj
                if hasattr(o.get_object(), 'get_data')
            )
        elif hasattr(obj, 'get_data'):
            raw = obj.get_data()
        else:
            return []
    except Exception:
        return []

    # Simple matrix multiply for 2D affine: result = A * B
    def _mat_mul(A, B):
        a1,b1,c1,d1,e1,f1 = A
        a2,b2,c2,d2,e2,f2 = B
        return (
            a1*a2 + b1*c2,
            a1*b2 + b1*d2,
            c1*a2 + d1*c2,
            c1*b2 + d1*d2,
            e1*a2 + f1*c2 + e2,
            e1*b2 + f1*d2 + f2,
        )

    # Transform the unit square [0,1]x[0,1] through a matrix
    def _mat_to_rect(mat):
        a, b, c, d, e, f = mat
        corners = [(0,0),(1,0),(1,1),(0,1)]
        xs = [a*cx + c*cy + e for cx,cy in corners]
        ys = [b*cx + d*cy + f for cx,cy in corners]
        return min(xs), min(ys), max(xs), max(ys)

    # Render the whole page ONCE, lazily, and reuse it to crop every image.
    # Previously the page was re-rendered (pdfium, 2×) inside the loop on every
    # qualifying image `Do` — catastrophic on CAD pages that place a small image
    # hundreds of times (measured: 162 Do calls → 126 s).  Memoised here it is a
    # single render shared by all crops (and never runs if no image qualifies).
    _scale = 2.0
    _page_img_box = []   # memo: [(pil_rgba, px_w, px_h)]
    def _get_page_img():
        if not _page_img_box:
            with _NATIVE_LOCK:
                _doc = pdfium.PdfDocument(pdf_bytes)
                try:
                    _pg = _doc[0].render(scale=_scale).to_pil().convert('RGBA')
                finally:
                    _doc.close()
            _page_img_box.append((_pg, _pg.size[0], _pg.size[1]))
        return _page_img_box[0]

    # Tokenise the content stream
    tokens = raw.split()
    ctm_stack = [(1,0,0,1,0,0)]   # identity
    q_stack   = []
    buf       = []
    results   = []
    elem_id   = 0

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == b'q':
            q_stack.append(ctm_stack[-1])
        elif tok == b'Q':
            if q_stack:
                ctm_stack = [q_stack.pop()]
        elif tok == b'cm' and len(buf) >= 6:
            try:
                nums = [float(buf[-6+j]) for j in range(6)]
                new_ctm = _mat_mul(ctm_stack[-1], tuple(nums))
                ctm_stack = [new_ctm]
            except Exception:
                pass
            buf = []
            i += 1
            continue
        elif tok == b'Do' and len(buf) >= 1:
            # buf[-1] is the XObject name (e.g. b'/Im1')
            name = buf[-1].decode('latin-1', errors='replace').lstrip('/')
            if name in image_xobjs:
                # Skip XObjects that were moved/deleted in a previous apply
                if exclude_xobj_names and name in exclude_xobj_names:
                    buf = []
                    i += 1
                    continue
                ctm = ctm_stack[-1]
                x1, y1, x2, y2 = _mat_to_rect(ctm)
                w = abs(x2 - x1)
                h = abs(y2 - y1)
                # Skip images that cover ≥80% of page area (full-page background)
                page_area = page_w * page_h
                img_area  = w * h
                if page_area > 0 and (img_area / page_area) >= 0.80:
                    buf = []
                    i += 1
                    continue
                # Also skip degenerate (zero-size) images
                if w < 2 or h < 2:
                    buf = []
                    i += 1
                    continue

                # Crop this image's region from the single shared page render
                # (rendered once at 2x for good fidelity; see _get_page_img).
                try:
                    _pg_img, _pw, _ph = _get_page_img()

                    # Convert PDF coords to pixel coords:
                    # pdfium renders with y=0 at top, pdf y=0 at bottom.
                    # Need to account for page rotation — pdfium renders upright.
                    if rotate in (0, 180):
                        _ref_W, _ref_H = page_w, page_h
                    else:  # 90, 270 — pdfium display is transposed
                        _ref_W, _ref_H = page_h, page_w

                    def _pdf_x_to_px(px_pdf):
                        return int(px_pdf / _ref_W * _pw)
                    def _pdf_y_to_px(py_pdf):
                        return int((1.0 - py_pdf / _ref_H) * _ph)

                    px1 = _pdf_x_to_px(min(x1, x2))
                    px2 = _pdf_x_to_px(max(x1, x2))
                    py1 = _pdf_y_to_px(max(y1, y2))   # top in pixel space
                    py2 = _pdf_y_to_px(min(y1, y2))   # bottom in pixel space

                    # Clamp to image bounds
                    px1 = max(0, px1); py1 = max(0, py1)
                    px2 = min(_pw, px2); py2 = min(_ph, py2)

                    if px2 <= px1 or py2 <= py1:
                        buf = []
                        i += 1
                        continue

                    _crop  = _pg_img.crop((px1, py1, px2, py2))
                    _buf   = io.BytesIO()
                    _crop.save(_buf, format='PNG')
                    _b64   = base64.b64encode(_buf.getvalue()).decode('utf-8')
                    data_url = 'data:image/png;base64,' + _b64
                except Exception:
                    data_url = ''

                elem_id += 1

                # Store in display (pdfium) coords — x1,y1 = bottom-left in PDF space
                results.append({
                    'id':      f'pdfimg_{elem_id}',
                    'type':    'pdf_image',
                    'x':       round(min(x1, x2), 2),
                    'y':       round(min(y1, y2), 2),
                    'w':       round(w, 2),
                    'h':       round(h, 2),
                    'dataUrl': data_url,
                    'xobj_name': name,
                })
                logger.debug(f"[PDF_IMG] Found image '{name}' at x={x1:.1f} y={y1:.1f} w={w:.1f} h={h:.1f}")

            buf = []
            i += 1
            continue

        buf.append(tok)
        i += 1

    return results



def ocr_extract_text_items(pdf_bytes, scale=None, progress_cb=None):
    """
    Run Tesseract OCR on an image-only PDF page and return text_items
    in the same format as extract_page_elements.

    *progress_cb*, when supplied, is called (current, total, label) at each
    coarse OCR stage (render → preprocess → two Tesseract passes → finalise) so
    the loading overlay can show a real progress bar.  A cache hit returns
    instantly and reports nothing.

    Words are grouped into lines using Tesseract's own block/par/line
    hierarchy (from image_to_data). This produces one item per visual
    line of text rather than one item per word, making sentences and
    labels editable as a unit in the editor.

    Pass-2 (psm 11) words that don't overlap any pass-1 word are kept
    as individual items — they represent isolated labels/stamps that
    Tesseract's block layout didn't group, so merging them would risk
    incorrect associations.

    Coordinates: pypdfium2 renders the page upright. Tesseract gives
    pixel coords (y=0 at top). We convert to PDF user space (y=0 at bottom).
    """
    if not _OCR_AVAILABLE:
        return []

    # Pick a ~300 DPI render scale (essential for legible OCR on dense, large-
    # format drawings; the old fixed 2.0× produced garbage on small text).
    # Resolved before the cache key so the key reflects the actual scale used.
    if scale is None:
        try:
            _m = _pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].mediabox
            scale = _ocr_utils._auto_ocr_scale(float(_m.width), float(_m.height))
        except Exception:
            scale = 2.0

    # Cache hit? OCR is deterministic per (source bytes, scale). Return a copy so
    # the caller can't mutate the cached canonical list.
    _ck = (hashlib.sha256(pdf_bytes).hexdigest(), round(float(scale), 3))
    with _OCR_CACHE_LOCK:
        _hit = _OCR_CACHE.get(_ck)
        if _hit is not None:
            # Mark as most-recently-used so active pages aren't evicted.
            try: _OCR_CACHE_ORDER.remove(_ck)
            except ValueError: pass
            _OCR_CACHE_ORDER.append(_ck)
    if _hit is not None:
        logger.debug(f"[OCR_CACHE] hit ({len(_hit)} items) key={_ck[0][:12]} scale={_ck[1]}")
        return _copy.deepcopy(_hit)

    # Get the page rotation from the PDF dictionary (pure pypdf — no lock needed)
    _pypdf_reader = _pypdf.PdfReader(io.BytesIO(pdf_bytes))
    _pypdf_page   = _pypdf_reader.pages[0]
    _rotate       = int(_pypdf_page.get('/Rotate', 0)) % 360
    _mb    = _pypdf_page.mediabox
    _raw_W = float(_mb.width)
    _raw_H = float(_mb.height)

    _ocr_utils.report_ocr_progress(progress_cb, _ocr_utils.OCR_STAGE_RENDER)
    with _NATIVE_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            page = doc[0]
            logger.debug(f"[OCR_COORDS] /Rotate={_rotate} raw_mediabox={_raw_W:.0f}x{_raw_H:.0f} "
                         f"pdfium_display={page.get_width():.0f}x{page.get_height():.0f}")
            bmp = page.render(scale=scale)
            img = bmp.to_pil()
        finally:
            doc.close()

    # ── Grayscale array for bold-weight detection ─────────────────────────────
    # Bold strokes are thicker, so a word's bounding-box has a higher dark-pixel
    # density than the same word in regular weight.  We compute this once and
    # reuse it per word in the item-building loop below.
    _img_gray = np.array(img.convert('L'))   # shape: (H, W), dtype uint8, y=0 at top
    # RGB copy in the SAME (original, un-deskewed) frame, used per word to sample
    # the real ink colour so the editor panel and re-injection don't fall back to
    # a hardcoded black.  Word boxes are mapped back to this frame below, so the
    # gray (bold) and rgb (colour) lookups share one coordinate system.
    _img_rgb = np.array(img.convert('RGB'))  # shape: (H, W, 3), uint8

    # ── Preprocess the image fed to OCR (grayscale, contrast, denoise, deskew) ──
    # _img_gray above stays in the ORIGINAL frame for bold detection.  OCR runs on
    # the cleaned/deskewed copy; word boxes are mapped back to the original frame
    # right after collection so all the coordinate maths below is unchanged.
    _ocr_utils.report_ocr_progress(progress_cb, _ocr_utils.OCR_STAGE_PREP)
    ocr_img, _skew = _ocr_utils.preprocess_image_for_ocr(img, deskew=True, scale=scale)
    _cx, _cy = img.width / 2.0, img.height / 2.0
    if _skew:
        logger.debug(f"[OCR] deskew applied: {_skew:+.2f}°")

    # ── Run two OCR passes ────────────────────────────────────────────────────
    # Pass 1: --psm 6  (uniform block) — layout-aware, returns block/par/line
    # Pass 2: --psm 11 (sparse text)   — catches isolated words psm 6 misses,
    #                                    but has no reliable layout hierarchy
    with _ocr_utils.hidden_console():
        _olang, _ocfg = _ocr_utils.ocr_lang_config()
        _ocr_utils.report_ocr_progress(progress_cb, _ocr_utils.OCR_STAGE_PASS1)
        data1 = _pytesseract.image_to_data(
            ocr_img, output_type=_pytesseract.Output.DICT,
            lang=_olang, config='--psm 6' + _ocfg)
        _ocr_utils.report_ocr_progress(progress_cb, _ocr_utils.OCR_STAGE_PASS2)
        data2 = _pytesseract.image_to_data(
            ocr_img, output_type=_pytesseract.Output.DICT,
            lang=_olang, config='--psm 11' + _ocfg)

    _ocr_utils.report_ocr_progress(progress_cb, _ocr_utils.OCR_STAGE_FINALISE)

    # ── Collect words, preserving Tesseract layout keys ───────────────────────
    def _collect_words(data, pass_num):
        words = []
        for i, text in enumerate(data['text']):
            if not text or not text.strip():
                continue
            conf = int(data['conf'][i])
            if conf < 10:
                continue
            words.append({
                'text':      text.strip(),
                'conf':      conf,
                'left':      data['left'][i],
                'top':       data['top'][i],
                'width':     data['width'][i],
                'height':    data['height'][i],
                # Layout hierarchy — only reliable for pass 1 (psm 6)
                'block_num': data['block_num'][i],
                'par_num':   data['par_num'][i],
                'line_num':  data['line_num'][i],
                'pass':      pass_num,
            })
        return words

    words1 = _collect_words(data1, pass_num=1)
    words2 = _collect_words(data2, pass_num=2)

    # Deskew was applied to the OCR image — bring boxes back to the original
    # render frame so the pixel→PDF conversion below stays correct, and so the
    # bold-detection region indexes line up with the (un-deskewed) _img_gray.
    _ocr_utils.deskew_unmap_boxes(words1, _skew, _cx, _cy)
    _ocr_utils.deskew_unmap_boxes(words2, _skew, _cx, _cy)

    # ── Cluster-based dedup across the two passes ────────────────────────────
    # Clusters overlapping detections from both passes and keeps the pass that
    # transcribes each region best (highest sum of conf×char_count).  This stops
    # a large title that psm 6 fragments into single letters ('FO','ST','R'…)
    # from crowding out the correct psm-11 whole word ('FOR CONSTRUCTION'), while
    # still preferring psm 11 where psm 6 merges a wrapped cell into a low-conf
    # blob.  See ocr_utils.dedup_two_pass_words for the full rationale.
    all_words = _ocr_utils.dedup_two_pass_words(words1, words2)
    # Both passes can agree on a misread of white-on-band text ("VAT" → "WAT"
    # at 96%), so give those words a second, polarity-corrected read.  Boxes
    # are in the original frame here, which is what _img_gray is.
    _ocr_utils.reread_light_on_dark(_img_gray, all_words, _olang, _ocfg)

    # ── One item per word (matches native PDF behaviour) ─────────────────────
    # Each word from both passes becomes its own independent item.
    # This is consistent with how native text elements are extracted and avoids
    # fragile grouping heuristics that break on table headers and column layouts.
    line_groups = [[w] for w in all_words]

    # ── Pixel → PDF coordinate conversion ────────────────────────────────────
    def _px_to_pdf(px, py, pw, ph):
        """Convert a Tesseract pixel bbox to raw PDF content stream coords."""
        if _rotate == 0:
            x1 = px / scale
            x2 = (px + pw) / scale
            y2 = _raw_H - py / scale
            y1 = _raw_H - (py + ph) / scale
        elif _rotate == 90:
            # pdfium renders /Rotate=90 with content-space x running DOWN the
            # page: Tesseract row (py) → raw x, column (px) → raw y.  (The x term
            # previously carried a spurious `_raw_W -` mirror — the 90° and 270°
            # x-formulas were swapped — flipping OCR'd text to the wrong side.)
            x1 =  py       / scale
            x2 = (py + ph) / scale
            y1 =  px       / scale
            y2 = (px + pw) / scale
        elif _rotate == 180:
            x1 = _raw_W - (px + pw) / scale
            x2 = _raw_W -  px       / scale
            y2 = (py + ph) / scale
            y1 =  py       / scale
        elif _rotate == 270:
            # Mirror of the 90° case: raw x measured from the far edge.
            x1 = _raw_W - (py + ph) / scale
            x2 = _raw_W -  py       / scale
            y1 = _raw_H - (px + pw) / scale
            y2 = _raw_H -  px       / scale
        else:
            x1 = px / scale
            x2 = (px + pw) / scale
            y2 = _raw_H - py / scale
            y1 = _raw_H - (py + ph) / scale
        return x1, y1, x2, y2

    # ── Build items, one per line group ──────────────────────────────────────
    items   = []
    elem_id = 0

    for group in line_groups:
        if not group:
            continue

        # Merge bounding box across all words in the line
        g_left   = min(w['left']              for w in group)
        g_top    = min(w['top']               for w in group)
        g_right  = max(w['left'] + w['width'] for w in group)
        g_bottom = max(w['top']  + w['height'] for w in group)
        g_pw     = g_right  - g_left
        g_ph     = g_bottom - g_top

        # Join words with a single space; words are already in left→right order
        line_text = ' '.join(w['text'] for w in group)

        # Average confidence across the line
        line_conf = round(sum(w['conf'] for w in group) / len(group))

        # x1, y1, x2, y2 = _px_to_pdf(g_left, g_top, g_pw, g_ph)
        #
        # vis_size  = abs(y2 - y1)
        # if vis_size < 0.5:
        #     vis_size = 8.0
        # font_size = round(vis_size * 1.35, 3)

        x1, y1, x2, y2 = _px_to_pdf(g_left, g_top, g_pw, g_ph)

        # Use median word height for font sizing rather than the merged bbox height.
        # The merged bbox can be taller than any individual word if baselines are
        # slightly misaligned across the line, which inflates font_size on apply.
        # Median is robust against the occasional oversized cap or descender.
        heights = sorted(w['height'] for w in group)
        med_h_px = heights[len(heights) // 2]
        vis_size = med_h_px / scale  # convert pixels → PDF points
        if vis_size < 0.5:
            vis_size = 8.0
        # font_size (em) is computed below, after the typeface is chosen, using
        # that face's cap-height ratio so the rendered cap height ≈ vis_size.

        # ── Bold detection via size-normalised stroke width ───────────────────
        # We measure stroke *thickness* directly (character-shape independent, so
        # "Adam" doesn't read bold just because compact letters pack more ink per
        # bbox), then normalise it by the word's height so the result is invariant
        # to both font size and render scale.
        #
        # The previous metric thresholded the raw erosion-survival ratio, which is
        # purely a function of stroke width *in pixels* — and pixel stroke width
        # grows with font size AND render DPI.  When the OCR render scale moved
        # from a fixed 2× to auto ~300 DPI (≈4×), regular strokes doubled from
        # ~2px to ~4px and sailed past the old 0.10 ratio, so ordinary text (and
        # large headings) started reading as bold.  Dividing the estimated stroke
        # width by the word height fixes that: regular sans ≈ 0.08–0.12, bold
        # ≈ 0.15–0.20, across every DPI and point size.
        _px1 = max(0, g_left)
        _py1 = max(0, g_top)
        _px2 = min(_img_gray.shape[1], g_right)
        _py2 = min(_img_gray.shape[0], g_bottom)
        _region = _img_gray[_py1:_py2, _px1:_px2]
        _sw_ratio  = 0.0
        _ink_mask  = None     # bool mask of ink pixels within _region (reused for colour)
        if _region.size > 20:
            # Polarity-aware ink mask.  The strokes are the MINORITY class; the
            # paper-or-fill behind them is the majority.  A fixed "dark = ink"
            # test wrongly selects the dark fill of light-on-dark text (e.g. a
            # white header on a blue band), which then survives erosion as a big
            # solid block and reads as bold.  Splitting at the mid-tone and
            # taking whichever side is the minority makes the measurement
            # polarity-agnostic: dark-on-light and light-on-dark both reduce to
            # "thin minority strokes".
            _lo  = float(np.percentile(_region, 5))
            _hi  = float(np.percentile(_region, 95))
            _mid = (_lo + _hi) / 2.0
            _dark_mask = _region < _mid
            _dark_n    = int(_dark_mask.sum())
            _light_n   = _region.size - _dark_n
            if _dark_n == 0 or _light_n == 0:
                _ink_mask = (_region < 128)                  # degenerate region
            elif _dark_n <= _light_n:
                _ink_mask = _dark_mask                       # dark ink on light
            else:
                _ink_mask = ~_dark_mask                      # light ink on dark
            _b = _ink_mask.astype(np.uint8)
            _ink_total = int(np.sum(_b))
            if _ink_total >= 10 and _b.shape[0] >= 3 and _b.shape[1] >= 3:
                # All-9-neighbour erosion via elementwise AND of the 9 shifts
                _e = (_b[:-2,:-2] & _b[:-2,1:-1] & _b[:-2,2:] &
                      _b[1:-1,:-2] & _b[1:-1,1:-1] & _b[1:-1,2:] &
                      _b[2:,:-2]  & _b[2:,1:-1]  & _b[2:,2:])
                _eroded  = int(np.sum(_e))
                # One 3×3 erosion peels ~1px off each side of every stroke, so the
                # ink it removes (_ink_total − _eroded) ≈ 2px × total stroke length.
                # Hence stroke_length ≈ removed/2 and stroke_width ≈ ink/length =
                # 2·ink / removed — which recovers the true width in pixels
                # (w=2→2, 3→3, 4→4…).  Divide by word height for size-invariance.
                _removed = _ink_total - _eroded
                if _removed > 0:
                    _stroke_w = 2.0 * _ink_total / _removed
                    _sw_ratio = _stroke_w / float(_region.shape[0])
        # 0.135 sits in the gap between regular (≈0.08–0.12) and bold (≈0.15–0.20)
        # stroke-width-to-height ratios.  Operator can override via the Font dropdown.
        _is_bold = _sw_ratio > 0.135
        # Default OCR typeface is Calibri (Carlito) — a far closer match to the
        # humanist sans used by most scanned office documents than base-14
        # Helvetica.  The operator can override via the Font dropdown; serif/sans
        # auto-classification is layered on in a later phase (C).
        detected_rl_font = 'Calibri-Bold' if _is_bold else 'Calibri'

        # ── Ink colour detection ──────────────────────────────────────────────
        # Previously every OCR word was emitted as #000000, so the editor panel's
        # colour swatch always showed black; editing white-on-band or coloured
        # text (or even just changing its font) then wrote that black back over
        # the real ink.  Take the median RGB of the very ink pixels the bold test
        # already isolated, then snap near-neutral ink to pure black/white (a
        # scanned "black" samples to a washed-out mid-grey) while leaving genuinely
        # coloured ink — navy headings, red stamps, white-on-blue bands — intact.
        detected_color = '#000000'
        if _ink_mask is not None and int(_ink_mask.sum()) >= 5:
            _ink_rgb = _img_rgb[_py1:_py2, _px1:_px2][_ink_mask]
            _med = np.median(_ink_rgb, axis=0) / 255.0
            _cr, _cg, _cb = float(_med[0]), float(_med[1]), float(_med[2])
            _lum = 0.299 * _cr + 0.587 * _cg + 0.114 * _cb
            _sat = max(_cr, _cg, _cb) - min(_cr, _cg, _cb)   # 0 = perfectly neutral
            if _sat < 0.20:
                if _lum < 0.55:
                    _cr = _cg = _cb = 0.0
                elif _lum > 0.80:
                    _cr = _cg = _cb = 1.0
            detected_color = '#{:02x}{:02x}{:02x}'.format(
                int(round(_cr * 255)), int(round(_cg * 255)), int(round(_cb * 255)))

        # Em size from the measured ink box.  The box height is divided by the
        # text's expected ink span (per-character, measured from the real font
        # glyphs) so words with descenders ("Prepayment") and cap-only words
        # ("2026") on the same line land on the same em size.  Cap height is
        # the fallback when nothing in the text is measurable.
        _box_ratio = (_ocr_box_ratio(line_text, detected_rl_font)
                      or _cap_ratio(detected_rl_font))
        font_size = round(vis_size / _box_ratio, 3)
        logger.debug(f"[OCR_BOLD] '{line_text[:30]}' sw_ratio={_sw_ratio:.3f} "
                     f"→ {detected_rl_font} color={detected_color} font_size={font_size:.3f}")

        # ── Row grouping (for the manual row-merge / reflow feature) ──────────
        # Persist Tesseract's layout hierarchy so the front end can group the
        # words that share a visual line.  Only pass-1 (psm 6) has a reliable
        # block/par/line hierarchy; pass-2 (psm 11, sparse) numbering is its own
        # namespace, so we qualify the row key by pass to avoid cross-pass
        # collisions.  Phase-2 grouping uses ocr_row (and falls back to baseline
        # proximity for the rare cross-pass case).
        _w0      = group[0]
        _ocr_pass = _w0.get('pass', 1)
        _ocr_blk  = _w0.get('block_num', 0)
        _ocr_par  = _w0.get('par_num', 0)
        _ocr_ln   = _w0.get('line_num', 0)

        elem_id += 1
        items.append({
            'id':         f'ocr_{elem_id}',
            'type':       'text',
            'x':          round(x1, 3),
            'y':          round(y1, 3),
            'x2':         round(x2, 3),
            'y2':         round(y2, 3),
            'text':       line_text,
            'size':       round(vis_size, 3),
            'font_size':  font_size,
            'font':       'unknown',
            'rl_font':    detected_rl_font,
            'color':      detected_color,
            'original_x': round(x1, 3),
            'original_y': round(y1, 3),
            'deleted':    False,
            'ocr':        True,
            'ocr_conf':   line_conf,
            # Row identity: words sharing this key are on the same Tesseract line.
            'ocr_row':    f'{_ocr_pass}:{_ocr_blk}:{_ocr_par}:{_ocr_ln}',
            'ocr_pass':   _ocr_pass,
        })

    # ── Per-line median smoothing of font sizes ──────────────────────────────
    # A wrong Tesseract box (a 1-2px sliver, or two stacked words merged into
    # one tall box) survives the ink-ratio conversion above as an absurd em
    # size while every neighbour on the same visual line is sized correctly.
    # Words on a line share a font size far more often than not, so snap clear
    # outliers (>35% off) to the line consensus.
    #
    # The consensus must be robust to lines where the garbage outnumbers the
    # good words (seen in real table headers: two merged double-height boxes +
    # one sliver vs a single good word — a plain median votes for the garbage
    # and *breaks* the good word).  Hence:
    #   • voters need ≥2 alphanumeric chars AND a non-sliver box (≥3 pt) —
    #     punctuation and sliver boxes are pure noise;
    #   • the largest cluster of mutually-close votes (within 35%) must hold a
    #     strict >2/3 supermajority, otherwise the line is left untouched —
    #     a no-consensus line gives no evidence of which size is right;
    #   • 'size' (the raw ink box) is never modified, only the derived em size.
    def _consensus_median(votes, rel=0.35):
        """Median of the largest cluster of mutually-close votes; None when
        the cluster lacks a >2/3 supermajority (no consensus)."""
        sv = sorted(votes)
        best = []
        for _i, _v in enumerate(sv):
            _hi = _v * (1.0 + rel)
            _cl = sv[_i:next((j for j in range(_i, len(sv)) if sv[j] > _hi),
                             len(sv))]
            if len(_cl) > len(best):
                best = _cl
        if len(best) >= 2 and 3 * len(best) > 2 * len(votes):
            return best[len(best) // 2]
        return None

    def _is_voter(it):
        return (sum(c.isalnum() for c in it['text']) >= 2
                and it['size'] >= 3.0 and it['font_size'] > 0)

    _rows = {}
    for _it in items:
        _rows.setdefault(_it['ocr_row'], []).append(_it)

    _row_consensus = {}
    for _row_key, _row_items in _rows.items():
        _votes = [_it['font_size'] for _it in _row_items if _is_voter(_it)]
        if len(_votes) < 2:
            continue
        _med = _consensus_median(_votes)
        if _med is None:
            continue
        _row_consensus[_row_key] = _med
        for _it in _row_items:
            _fs = _it['font_size']
            if abs(_fs - _med) / _med <= 0.35:
                continue
            # Magnitude guard: a sliver box (<3 pt) is garbage by construction
            # and may be corrected arbitrarily, but an intact-looking box more
            # than 2× off the consensus is more likely genuinely different
            # text than a bad measurement — psm 6 sometimes files a large
            # logo/title under the same "line" as unrelated small words (seen
            # in practice: a 28 pt logo sharing a row with 10 pt body text),
            # and flattening it would be worse than leaving it alone.
            if _it['size'] >= 3.0 and not (0.5 <= _fs / _med <= 2.0):
                continue
            logger.debug(f"[OCR_SMOOTH] '{_it['text'][:24]}' font_size "
                         f"{_fs:.2f} → row consensus {_med:.2f}")
            _it['font_size'] = _med

    # Degenerate boxes (sliver < 3 pt, or an em size below any readable text)
    # whose own line produced no consensus get the page's body size: their
    # current value is garbage by construction, and the dominant text size on
    # the page is the best remaining guess.  Needs ≥5 voters page-wide so a
    # sparse page can't produce a meaningless "body size".
    _page_votes = [_it['font_size'] for _it in items if _is_voter(_it)]
    if len(_page_votes) >= 5:
        _page_votes.sort()
        _page_med = _page_votes[len(_page_votes) // 2]
        for _it in items:
            if ((_it['size'] < 3.0 or _it['font_size'] < 4.5)
                    and _it['ocr_row'] not in _row_consensus):
                logger.debug(f"[OCR_SMOOTH] '{_it['text'][:24]}' degenerate box "
                             f"(size={_it['size']:.2f} fs={_it['font_size']:.2f})"
                             f" → page median {_page_med:.2f}")
                _it['font_size'] = _page_med

    logger.debug(f"[OCR_COORDS] Total items: {len(items)}")
    for _it in items[:3]:
        logger.debug(f"[OCR_COORDS]   '{_it['text'][:40]}': x={_it['x']:.1f} y={_it['y']:.1f} "
                     f"x2={_it['x2']:.1f} y2={_it['y2']:.1f}")

    # Store the canonical result (bounded LRU) and hand back a fresh copy.
    with _OCR_CACHE_LOCK:
        _OCR_CACHE[_ck] = items
        _OCR_CACHE_ORDER.append(_ck)
        while len(_OCR_CACHE_ORDER) > _OCR_CACHE_MAX:
            _OCR_CACHE.pop(_OCR_CACHE_ORDER.pop(0), None)
    logger.debug(f"[OCR_CACHE] store ({len(items)} items) key={_ck[0][:12]} scale={_ck[1]}")
    return _copy.deepcopy(items)

def _extract_text_object_color_bboxes(pdf_bytes):
    """For every character on the page, return (x1,y1,x2,y2,'#rrggbb') using
    pdfium's FPDFText_GetFillColor. The textpage flattens Form XObjects, so
    this avoids the form-recursion problem that page.get_objects() runs into
    on PDFs where content is wrapped in a form container (Word, Excel, many
    invoice generators). Coordinates are page space — same as get_charbox() —
    so no transform is needed when matching words.
    """
    try:
        import pypdfium2.raw as pdfium_c
        import ctypes
    except Exception as e:
        logger.error(f"[pdfium char color] import failed: {e}")
        return []

    out = []
    try:
        with _NATIVE_LOCK:
            doc = pdfium.PdfDocument(pdf_bytes)
            try:
                page      = doc[0]
                tp        = page.get_textpage()
                n         = pdfium_c.FPDFText_CountChars(tp.raw)
                got_color = 0
                for i in range(n):
                    try:
                        left   = ctypes.c_double(0); right = ctypes.c_double(0)
                        bottom = ctypes.c_double(0); top   = ctypes.c_double(0)
                        if not pdfium_c.FPDFText_GetCharBox(
                            tp.raw, i,
                            ctypes.byref(left), ctypes.byref(right),
                            ctypes.byref(bottom), ctypes.byref(top),
                        ):
                            continue
                        r = ctypes.c_uint(0); g = ctypes.c_uint(0)
                        b = ctypes.c_uint(0); a = ctypes.c_uint(0)
                        if not pdfium_c.FPDFText_GetFillColor(
                            tp.raw, i,
                            ctypes.byref(r), ctypes.byref(g),
                            ctypes.byref(b), ctypes.byref(a),
                        ):
                            continue
                        got_color += 1
                        out.append((
                            float(left.value),  float(bottom.value),
                            float(right.value), float(top.value),
                            '#{:02x}{:02x}{:02x}'.format(r.value, g.value, b.value),
                        ))
                    except Exception:
                        continue
                logger.debug(f"[COLOR] pdfium char-level: {n} chars, {got_color} with fill colour")
            finally:
                doc.close()
    except Exception as e:
        logger.error(f"[pdfium char color] failed: {e}")
    return out


def _lookup_color_from_text_objs(wx1, wy1, wx2, wy2, char_colors):
    """Majority colour of all characters whose bbox centre falls inside the word bbox.
    Char-level matching is robust: rotated/italic text, overlapping runs, kerned pairs
    all behave predictably because each char has its own tight bbox."""
    if not char_colors:
        return None
    from collections import Counter
    matches = []
    for cx1, cy1, cx2, cy2, col in char_colors:
        ccx = (cx1 + cx2) * 0.5
        ccy = (cy1 + cy2) * 0.5
        if wx1 - 1 <= ccx <= wx2 + 1 and wy1 - 1 <= ccy <= wy2 + 1:
            matches.append(col)
    if not matches:
        return None
    return Counter(matches).most_common(1)[0][0]

def _extract_text_and_graphics(pdf_bytes, ocr_source_bytes=None, progress_cb=None):
    """Extract text + graphic items for the editor.

    Native PDFs: just pdf_bytes.

    OCR pages: OCR items come from the pristine scan (so unchanged scanned
    text stays editable even after repeated applies) AND native text items
    come from the CURRENT pdf_bytes (so text that previous edits injected
    via emit_text — which writes real Tj operators — is also editable).
    Merging both sources is the OCR equivalent of what a single pass gives
    us on a native PDF.
    """
    if ocr_source_bytes is None:
        return extract_page_elements(pdf_bytes, progress_cb=progress_cb)

    # The source is either a pristine scan (no native text at all) or, after a
    # save and reopen, a MIXED page — a scan with our earlier edits written into
    # it.  extract_page_elements only OCRs a page with no characters, so a mixed
    # source has to be OCR'd explicitly or its scanned words are never seen.
    src_is_scan = is_image_only_page(ocr_source_bytes)
    if src_is_scan and ocr_source_bytes == pdf_bytes:
        # An unedited scan: one pass does everything.
        return extract_page_elements(pdf_bytes, progress_cb=progress_cb)

    # Native pass on the current PDF. Suppress OCR fallback: if pdfium's
    # textpage can't see the injected runs (happens when the injection CTM
    # pushes text into a space the textpage indexer skips), we must NOT
    # re-OCR the current PDF — that would pick up "GST" AND "VAT" because
    # the raster under our cover rect still says VAT.
    page_w, page_h, pt, pb, native_items, native_gfx = extract_page_elements(
        pdf_bytes, allow_ocr_fallback=False)
    native_only = [it for it in native_items if not it.get('ocr')]

    if src_is_scan:
        _, _, _, _, ocr_items, graphic_items = extract_page_elements(
            ocr_source_bytes, progress_cb=progress_cb)
    else:
        ocr_items = ocr_extract_text_items(ocr_source_bytes,
                                           progress_cb=progress_cb) or []
        graphic_items = native_gfx
    # The OCR'd raster can already show native text (a mixed source draws its
    # edits into the page), so an OCR word sitting under a native run is that
    # run read a second time.  Offer the native one.
    ocr_items = _drop_ocr_under_native(ocr_items, native_only)

    return page_w, page_h, pt, pb, ocr_items + native_only, graphic_items

def extract_page_elements(pdf_bytes, allow_ocr_fallback=True, progress_cb=None):
    """
    Extract text items and graphic elements using pypdfium2.
    pypdfium2 uses the SAME coordinate system as the renderer so positions
    are guaranteed to match the rendered image exactly — no coordinate transform issues.

    Returns:
        page_w, page_h, page_top, page_bottom — page dimensions in PDF points
        text_items    — list of {id, type, x, y, x2, y2, text, size, font, ...}
        graphic_items — list of {id, type, x, y, w, h, ...} (rects only)
    """
    # ── Page dimensions from pypdf (for mediabox info) ────────────────────────
    pypdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    pypdf_page   = pypdf_reader.pages[0]

    # ── Page dimensions via pdfium (authoritative for coordinate matching) ─────
    with _NATIVE_LOCK:
        _dim_doc = pdfium.PdfDocument(pdf_bytes)
        try:
            pw = _dim_doc[0].get_width()
            ph = _dim_doc[0].get_height()
        finally:
            _dim_doc.close()
    page_w = pw; page_h = ph; page_top = ph; page_bottom = 0.0

    text_items    = []
    graphic_items = []
    elem_id = 0

    MARGIN  = max(pw, ph) * 0.15
    MAX_GFX = 300

    # ── Fast path for image-only (scanned) pages ──────────────────────────────
    # The two pypdf extract_text() passes below cost ~2s EACH on some PDFs, but
    # an image-only page has no native text/graphics/colours to extract — those
    # passes return nothing. Detect that up front with a cheap pdfium char count
    # and skip straight to OCR (cached). No quality change: there is nothing
    # native to find. This is the dominant cost on every open/apply of a scan.
    with _NATIVE_LOCK:
        _io_doc = pdfium.PdfDocument(pdf_bytes)
        try:
            _img_only = _io_doc[0].get_textpage().count_chars() == 0
        finally:
            _io_doc.close()
    if _img_only:
        if allow_ocr_fallback and _OCR_AVAILABLE:
            ocr_items = ocr_extract_text_items(pdf_bytes, progress_cb=progress_cb)
            if ocr_items:
                logger.info(f"[OCR] Extracted {len(ocr_items)} words via Tesseract from scanned page")
                return page_w, page_h, page_top, page_bottom, ocr_items, []
            return (page_w, page_h, page_top, page_bottom,
                    [{'__ocr_notice__': 'ocr_no_text'}], [])
        if allow_ocr_fallback:
            return (page_w, page_h, page_top, page_bottom,
                    [{'__ocr_notice__': _OCR_UNAVAILABLE_REASON or 'ocr_unavailable'}], [])
        # Native pass with OCR suppressed (e.g. the edited-PDF pass) — nothing
        # native to extract on a truly image-only page.
        return page_w, page_h, page_top, page_bottom, [], []

    def in_bounds(x, y):
        return -MARGIN <= x <= pw + MARGIN and -MARGIN <= y <= ph + MARGIN

    # ── Authoritative colour source via pdfium's page-object API ──────────────
    # Handles all colour spaces (rg/g/k + scn/sc over DeviceRGB/ICCBased/CMYK).
    # We prefer this over the visitor below because the visitor only catches
    # rg/g/k and misses scn/sc colour-space sets common in Word/Excel/invoice PDFs.
    _text_obj_bboxes = _extract_text_object_color_bboxes(pdf_bytes)
    logger.debug(f"[COLOR] pdfium text objects with colour: {len(_text_obj_bboxes)}")

    # ── Extract Tf font sizes AND font names via pypdf visitor ────────────────
    # Kept as the source for font size & font name (pdfium's page-object API
    # could give these too, but the existing path works fine — no need to churn).
    # Also kept as a colour FALLBACK in case pdfium ever returns 0 text objects.
    _color_by_pos   = {}  # (round_x, round_y) -> hex color string
    _tfsize_by_pos  = {}  # (round_x, round_y) -> actual Tf font size in pts
    _font_by_pos    = {}  # (round_x, round_y) -> rl_font string
    _cur_color      = ['#000000']
    _cur_tf_size    = [12.0]
    _cur_rl_font    = ['Helvetica']

    # Build a map from font resource names (/F1, /F2 …) to base font names
    _font_res_map = {}  # e.g. 'F1' -> 'Helvetica-Bold'
    try:
        _res = pypdf_page.get('/Resources') or {}
        _fonts = _res.get('/Font') or {}
        if hasattr(_fonts, 'get_object'):
            _fonts = _fonts.get_object()
        for _fkey in _fonts:
            try:
                _fobj = _fonts[_fkey]
                if hasattr(_fobj, 'get_object'):
                    _fobj = _fobj.get_object()
                _bfn = str(_fobj.get('/BaseFont', ''))
                if _bfn.startswith('/'):
                    _bfn = _bfn[1:]
                if '+' in _bfn:
                    _bfn = _bfn.split('+', 1)[1]
                _clean_key = str(_fkey).lstrip('/')
                _font_res_map[_clean_key] = _bfn
            except Exception:
                pass
    except Exception:
        pass

    # Graphic-rect state, collected in the SAME content-stream walk as the
    # text/colour visitor below — avoids a second ~2s pypdf extract_text() pass.
    # Rect ids are assigned AFTER the text loop (see below) so element numbering
    # stays byte-identical to the previous two-pass version.
    state = {'fill': (0., 0., 0.), 'stroke': (0., 0., 0.)}
    _gfx_raw = []

    def _apply_cm(x, y, cm_mat):
        if not cm_mat: return x, y
        a, b, c, d, e, f = [_to_float(v) for v in cm_mat]
        return a*x + c*y + e, b*x + d*y + f

    def _op_color(op, operands, cm, tm):
        op_s = op.decode() if isinstance(op, bytes) else str(op)
        try:
            ops = []
            for o in operands:
                try: ops.append(float(o))
                except: pass
            if op_s == 'rg' and len(ops) >= 3:
                r,g,b = ops[0], ops[1], ops[2]
                _cur_color[0] = '#{:02x}{:02x}{:02x}'.format(int(r*255),int(g*255),int(b*255))
            elif op_s == 'g' and len(ops) >= 1:
                v = int(ops[0]*255)
                _cur_color[0] = '#{:02x}{:02x}{:02x}'.format(v,v,v)
            elif op_s == 'k' and len(ops) >= 4:  # CMYK
                r = int((1-ops[0])*(1-ops[3])*255)
                g = int((1-ops[1])*(1-ops[3])*255)
                b = int((1-ops[2])*(1-ops[3])*255)
                _cur_color[0] = '#{:02x}{:02x}{:02x}'.format(r,g,b)
            elif op_s == 'Tf':
                # Tf operands: [font_resource_name, font_size]
                # Extract font size from numeric operands
                if len(ops) >= 1:
                    _cur_tf_size[0] = ops[0]
                # Extract font name from the first (non-numeric) operand
                if operands:
                    _raw_name = str(operands[0]).lstrip('/')
                    _base = _font_res_map.get(_raw_name, '')
                    if _base:
                        _cur_rl_font[0] = _safe_rl_font(_base)
                    else:
                        # Fallback: try mapping the raw name directly
                        _cur_rl_font[0] = _safe_rl_font(_raw_name)
            if tm and op_s in ('Tj', 'TJ', "'", '"'):
                key = (round(float(tm[4]), 0), round(float(tm[5]), 0))
                _color_by_pos[key]  = _cur_color[0]
                # Tm[0]/Tm[1] form the first column of the text matrix. Their
                # magnitude is the actual scale applied on top of the Tf size.
                # Without this, text set via a scaled Tm (e.g. [0.5 0 0 0.5 x y])
                # would store tf_size raw and emit_text would render it 2× too large.
                _tm_scale = math.hypot(float(tm[0]), float(tm[1]))
                _actual_size = _cur_tf_size[0] * (_tm_scale if _tm_scale > 0 else 1.0)
                _tfsize_by_pos[key] = _actual_size
                _font_by_pos[key]   = _cur_rl_font[0]
                logger.debug(f"[DBG-FONT] key={key} tf={_cur_tf_size[0]:.3f} tm_scale={_tm_scale:.4f} "
                             f"actual={_actual_size:.3f} font={_cur_rl_font[0]}")
        except Exception:
            pass
        # ---- graphic rect collection (merged from the old visitor_ops pass) ----
        # Colour tracking above always runs first, so it stays complete even past
        # the MAX_GFX cap (which only stops collecting more rects).
        if len(_gfx_raw) >= MAX_GFX:
            return
        ops2 = [_to_float(o) for o in operands]
        if op_s == 'rg' and len(ops2) >= 3:
            state['fill'] = tuple(ops2[:3])
        elif op_s == 'RG' and len(ops2) >= 3:
            state['stroke'] = tuple(ops2[:3])
        elif op_s == 'g' and len(ops2) >= 1:
            state['fill'] = (ops2[0], ops2[0], ops2[0])
        elif op_s == 'G' and len(ops2) >= 1:
            state['stroke'] = (ops2[0], ops2[0], ops2[0])
        elif op_s == 're' and len(ops2) >= 4:
            x, y, w, h = ops2[0], ops2[1], ops2[2], ops2[3]
            x, y = _apply_cm(x, y, cm)
            if (abs(w) > 5 and abs(h) > 5
                    and abs(w) < pw * 0.9 and abs(h) < ph * 0.9
                    and in_bounds(x, y) and in_bounds(x+w, y+h)):
                fc = state['fill']
                sc = state['stroke']
                _gfx_raw.append({
                    'type':       'rect',
                    'x':          round(x,  2),
                    'y':          round(y,  2),
                    'w':          round(w,  2),
                    'h':          round(h,  2),
                    'fill_color':   '#{:02x}{:02x}{:02x}'.format(
                                      int(fc[0]*255), int(fc[1]*255), int(fc[2]*255)),
                    'stroke_color': '#{:02x}{:02x}{:02x}'.format(
                                      int(sc[0]*255), int(sc[1]*255), int(sc[2]*255)),
                    'original_x': round(x, 2),
                    'original_y': round(y, 2),
                    'deleted':    False,
                })

    try:
        pypdf_page.extract_text(visitor_operand_before=_op_color)
    except Exception:
        pass

    # ── Extract character bounding boxes via pdfium ───────────────────────────
    # Walk every character via get_charbox(). Space/newline characters mark real
    # word boundaries as defined by the PDF author — no gap heuristics needed.
    # Characters within a word are merged into one item per word.
    # Each char also carries its font identity straight from pdfium:
    #   eff_size — Tf size × the effective text-matrix scale (FPDFText_GetMatrix),
    #              so text drawn through a scaled Tm/cm reports its TRUE size
    #   fname    — the real /BaseFont name for this char (no position matching)
    #   fflags   — font-descriptor /Flags (serif / fixed-pitch / italic / bold)
    #   fweight  — OS/2 weight class (700 = bold) where the font provides one
    # This is authoritative per-glyph data — unlike the pypdf visitor maps above,
    # it can never be associated with the wrong word.
    import pypdfium2.raw as _pdfium_c
    import ctypes as _ct
    char_data = []   # list of (char, x1, y1, x2, y2, eff_size, fname, fflags, fweight)
    _text_unmappable = False
    with _NATIVE_LOCK:
        _char_doc = pdfium.PdfDocument(pdf_bytes)
        try:
            _textpage = _char_doc[0].get_textpage()
            n_chars   = _textpage.count_chars()
            full_text = _textpage.get_text_range()
            # Native text present but its font encoding is broken (control codes,
            # no usable ToUnicode — see docs/edit_problem.pdf)?  Then the native
            # "text" is garbage for search/copy/edit and we OCR the page below,
            # keeping the real native graphics.
            _text_unmappable = _pdf_core.text_is_unmappable(full_text)
            # Tight per-glyph boxes + effective size, with pdfium's giant-em-square
            # / zero-size glyph bboxes repaired from the pen origin (see
            # pdf_core.corrected_char_boxes — fixes Type3 fonts like the one in
            # docs/edit_problem.pdf whose boxes would otherwise blanket the page
            # and make every glyph unselectable).
            _cboxes = _pdf_core.corrected_char_boxes(_textpage)
            for ci in range(n_chars):
                ch = full_text[ci] if ci < len(full_text) else ''
                if ci < len(_cboxes):
                    x1, y1, x2, y2, eff_size = _cboxes[ci]
                else:
                    char_data.append((ch, None, None, None, None, 0.0, '', 0, 0))
                    continue
                fname, fflags, fweight = '', 0, 0
                try:
                    _fl  = _ct.c_int(0)
                    _ln  = _pdfium_c.FPDFText_GetFontInfo(_textpage, ci, None, 0,
                                                          _ct.byref(_fl))
                    if _ln > 0:
                        _nb = _ct.create_string_buffer(_ln)
                        _pdfium_c.FPDFText_GetFontInfo(_textpage, ci, _nb, _ln,
                                                       _ct.byref(_fl))
                        fname = _nb.raw[:max(0, _ln - 1)].decode('utf-8', 'replace')
                    fflags  = int(_fl.value or 0)
                    _w = _pdfium_c.FPDFText_GetFontWeight(_textpage, ci)
                    fweight = int(_w) if _w and _w > 0 else 0
                except Exception:
                    pass
                char_data.append((ch, x1, y1, x2, y2, eff_size, fname, fflags, fweight))
        finally:
            _char_doc.close()

    # Group characters into words: split on whitespace
    words = []      # list of {'text', 'x1', 'y1', 'x2', 'y2', 'pd_*' font info}
    current_chars = []
    current_boxes = []
    current_fonts = []   # per-char (eff_size, fname, fflags, fweight)

    def _flush_word():
        if not current_chars:
            return
        word_text = ''.join(current_chars).strip()
        if not word_text:
            current_chars.clear(); current_boxes.clear(); current_fonts.clear()
            return
        valid = [(x1,y1,x2,y2) for (x1,y1,x2,y2) in current_boxes
                 if x1 is not None and not (x1==0 and y1==0)]
        if not valid:
            current_chars.clear(); current_boxes.clear(); current_fonts.clear()
            return
        wx1 = min(b[0] for b in valid)
        wy1 = min(b[1] for b in valid)
        wx2 = max(b[2] for b in valid)
        wy2 = max(b[3] for b in valid)
        # Median of per-char bottom y values: chars without descenders sit at the
        # baseline, so the median is a robust baseline estimate for words like
        # "designs" where 'g' pulls min(y1) below the true baseline.
        y1_sorted = sorted(b[1] for b in valid)
        wy1_baseline = y1_sorted[len(y1_sorted) // 2]
        # Word-level font from the per-char pdfium info: median size (robust to
        # the odd mis-sized glyph) and the first named char's font identity.
        sizes = sorted(s for (s, _, _, _) in current_fonts if s and s > 0)
        pd_size = sizes[len(sizes) // 2] if sizes else 0.0
        pd_name, pd_flags, pd_weight = '', 0, 0
        for (_, nm, fl, w) in current_fonts:
            if nm:
                pd_name, pd_flags, pd_weight = nm, fl, w
                break
        if in_bounds(wx1, wy1):
            words.append({'text': word_text,
                          'x1': wx1, 'y1': wy1, 'x2': wx2, 'y2': wy2,
                          'baseline_y': wy1_baseline,
                          'pd_size': pd_size, 'pd_name': pd_name,
                          'pd_flags': pd_flags, 'pd_weight': pd_weight})
        current_chars.clear(); current_boxes.clear(); current_fonts.clear()

    for ch, x1, y1, x2, y2, eff_size, fname, fflags, fweight in char_data:
        if ch in (' ', '\t', '\r', '\n', '\x0c', '\xa0'):
            _flush_word()
        else:
            current_chars.append(ch)
            current_boxes.append((x1, y1, x2, y2))
            current_fonts.append((eff_size, fname, fflags, fweight))
    _flush_word()

    # Build text_items from words
    for word in words:
        wx1, wy1, wx2, wy2 = word['x1'], word['y1'], word['x2'], word['y2']
        wy1_baseline = word.get('baseline_y', wy1)
        vis_size = abs(wy2 - wy1)
        if vis_size < 0.5:
            vis_size = 8.0

        # ── Colour lookup: pdfium first, visitor as fallback ─────────────────
        # 1) Preferred: pdfium text object (handles every colour space)
        text_color = _lookup_color_from_text_objs(wx1, wy1, wx2, wy2, _text_obj_bboxes)

        # ── Size & font: per-char pdfium info is authoritative ───────────────
        # Exact per-glyph font name / flags / weight / Tm-scaled size — no
        # position matching, so it can't drift onto the wrong word.  The pypdf
        # visitor maps below remain as fallback for chars pdfium can't describe.
        pd_name = word.get('pd_name') or ''
        pd_size = word.get('pd_size') or 0.0
        rl_font = (_safe_rl_font(pd_name, word.get('pd_flags', 0),
                                 word.get('pd_weight', 0))
                   if pd_name else None)
        color_key = (round(wx1, 0), round(wy1, 0))
        tf_size   = pd_size if pd_size > 0 else _tfsize_by_pos.get(color_key)
        if rl_font is None:
            rl_font = _font_by_pos.get(color_key)

        if text_color is None or tf_size is None or rl_font is None:
            # 2) Visitor direct hit (also fills in colour if pdfium gave nothing)
            if text_color is None:
                text_color = _color_by_pos.get(color_key)
            # 3) Fuzzy fallback with ±3pt tolerance
            if text_color is None or tf_size is None or rl_font is None:
                rx, ry = round(wx1, 0), round(wy1, 0)
                for (kx, ky) in _color_by_pos:
                    if abs(kx - rx) <= 3 and abs(ky - ry) <= 3:
                        if text_color is None: text_color = _color_by_pos[(kx, ky)]
                        if tf_size    is None: tf_size    = _tfsize_by_pos.get((kx, ky))
                        if rl_font    is None: rl_font    = _font_by_pos.get((kx, ky))
                        break

            # 4) Same-line propagation for FONT & SIZE.  A Tf state is set once per
            #    text-showing operator, but pdfium splits a run into several words;
            #    words past the first have no key of their own.  Font and size
            #    persist along a line, so inherit them from the nearest Tf state to
            #    the left on the same baseline.  (Colour can change mid-line, so it
            #    is left to the pdfium-first lookup above — not propagated here.)
            if rl_font is None or tf_size is None:
                rx, ry = round(wx1, 0), round(wy1, 0)
                _best_kx = None
                _best_key = None
                for (kx, ky) in _font_by_pos:
                    if abs(ky - ry) <= 4 and kx <= rx + 3:
                        if _best_kx is None or kx > _best_kx:
                            _best_kx, _best_key = kx, (kx, ky)
                if _best_key is not None:
                    if rl_font is None: rl_font = _font_by_pos.get(_best_key)
                    if tf_size is None: tf_size = _tfsize_by_pos.get(_best_key)

        if text_color is None:
            text_color = '#000000'
        if rl_font is None:
            rl_font = 'Helvetica'

        if tf_size and tf_size > 0:
            font_size = round(tf_size, 3)
        else:
            font_size = round(vis_size * 1.35, 3)

        elem_id += 1
        text_items.append({
            'id':         f'txt_{elem_id}',
            'type':       'text',
            'x':          round(wx1, 3),
            'y':          round(wy1, 3),
            'baseline_y': round(wy1_baseline, 3),
            'x2':         round(wx2, 3),
            'y2':         round(wy2, 3),
            'text':       word['text'],
            'size':       round(vis_size, 3),
            'font_size':  round(font_size, 3),
            'font':       pd_name or 'unknown',   # real /BaseFont for reference
            'rl_font':    rl_font,
            'color':      text_color,
            'original_x': round(wx1, 3),
            'original_y': round(wy1, 3),
            'deleted':    False,
        })
        logger.debug(f"[DBG-ITEM] \"{word['text']}\" "
                     f"vis_size={vis_size:.3f} font_size={font_size:.3f} rl_font={rl_font} "
                     f"y={wy1:.3f} baseline_y={wy1_baseline:.3f} color={text_color}")

    # ── Materialise graphic rects collected during the combined walk above ─────
    # Assigned here (after the text items) so element numbering is identical to
    # the previous two-pass version: text = txt_1..N, then graphics = gfx_(N+1).. .
    for _g in _gfx_raw:
        elem_id += 1
        _g['id'] = f'gfx_{elem_id}'
        graphic_items.append(_g)

    # ── Unmappable native text → OCR (keep the native graphics) ───────────────
    # The page HAS native text, but its font encoding is broken so the extracted
    # characters are control codes, not real text (broken Type3/subset font with
    # no usable ToUnicode — docs/edit_problem.pdf).  That text is useless for
    # search / copy / editing and would also merge into line-spanning blobs (the
    # font's space is a control glyph, so word splitting fails), so treat the
    # page like a scan: swap the garbage native text for OCR words, but keep the
    # real native shapes.  Word-level, correctly-positioned, editable text.
    if (_text_unmappable and allow_ocr_fallback and _OCR_AVAILABLE
            and text_items and not any('__ocr_notice__' in t for t in text_items
                                       if isinstance(t, dict))):
        logger.info("[OCR] native text unmappable (control-code font); OCR-ing "
                    f"page, keeping {len(graphic_items)} native shapes")
        ocr_items = ocr_extract_text_items(pdf_bytes, progress_cb=progress_cb)
        if ocr_items:
            logger.info(f"[OCR] replaced {len(text_items)} unmappable native "
                        f"items with {len(ocr_items)} OCR words")
            text_items = ocr_items
        # If OCR finds nothing, keep the native items rather than blanking text.

    # If no text found, check whether this is a scanned/image-only page
    if len(text_items) == 0 and allow_ocr_fallback:
        with _NATIVE_LOCK:
            _ck_doc = pdfium.PdfDocument(pdf_bytes)
            try:
                _ck_empty = _ck_doc[0].get_textpage().count_chars() == 0
            finally:
                _ck_doc.close()
        if _ck_empty:
            # Definitely image-only — try OCR or report why we can't
            if _OCR_AVAILABLE:
                ocr_items = ocr_extract_text_items(pdf_bytes, progress_cb=progress_cb)
                if ocr_items:
                    logger.info(f"[OCR] Extracted {len(ocr_items)} words via Tesseract from scanned page")
                    text_items = ocr_items
                else:
                    # OCR ran but found nothing (blank page or very low quality scan)
                    text_items = [{'__ocr_notice__': 'ocr_no_text'}]
            else:
                # Tesseract not available — tell the user what to do
                text_items = [{'__ocr_notice__': _OCR_UNAVAILABLE_REASON or 'ocr_unavailable'}]

    return page_w, page_h, page_top, page_bottom, text_items, graphic_items

def _get_injection_ctm(pypdf_page):
    """
    Detect the effective CTM at the END of the content stream.
    This is what newly appended content will see as its starting CTM.

    Only cm operators at q/Q nesting depth 0 persist to end of stream.
    Operators inside q/Q pairs are undone by Q and do NOT affect appended content.
    Returns (a,b,c,d,e,f) or None if identity.

    Uses raw content stream tokenisation rather than extract_text() visitor,
    because extract_text() fires no callbacks on image-only (scanned) pages.
    """

    def _is_nontrivial(a, b, c, d, e, f):
        # Only the identity matrix is trivial; any scaling/rotation/translation
        # must be detected so its inverse is pre-applied before injected markup.
        # Previously this returned False for any positive-scaling matrix, which
        # caused markup on scanner-generated PDFs (depth-0 scaling CTM such as
        # "0.24 0 0 0.24 0 0 cm") to be rendered at the wrong size/position.
        return not (abs(a - 1.0) < 0.001 and abs(d - 1.0) < 0.001 and
                    abs(b) < 0.001 and abs(c) < 0.001 and
                    abs(e) < 0.001 and abs(f) < 0.001)

    # Read raw content stream bytes
    try:
        contents = pypdf_page.get('/Contents')
        if contents is None:
            return None

        obj = contents.get_object() if hasattr(contents, 'get_object') else contents
        if isinstance(obj, ArrayObject):
            raw = b'\n'.join(
                o.get_object().get_data() for o in obj
                if hasattr(o.get_object(), 'get_data')
            )
        elif hasattr(obj, 'get_data'):
            raw = obj.get_data()
        else:
            return None
    except Exception:
        return None

    # Tokenise: split on whitespace, collect runs of 6 numbers followed by 'cm'
    tokens = raw.split()
    depth = 0
    top_level_ctms = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == b'q':
            depth += 1
        elif tok == b'Q':
            depth = max(0, depth - 1)
        elif tok == b'cm' and i >= 6 and depth == 0:
            try:
                nums = [float(tokens[i-6+j]) for j in range(6)]
                a,b,c,d,e,f = nums
                if _is_nontrivial(a,b,c,d,e,f):
                    top_level_ctms.append((round(a,6),round(b,6),round(c,6),
                                           round(d,6),round(e,6),round(f,6)))
            except Exception:
                pass
        i += 1

    if top_level_ctms:
        # Return the LAST top-level cm (cumulative effect at end of stream)
        return top_level_ctms[-1]
    return None

def _invert_matrix(a,b,c,d,e,f):
    """Compute inverse of 2D affine matrix."""
    det = a*d - b*c
    if abs(det) < 1e-10:
        return None
    return d/det, -b/det, -c/det, a/det, (c*f-d*e)/det, (b*e-a*f)/det

# ── Fitting a replacement into the space its original occupied ───────────────
# PDF is fixed-layout: injected text cannot push its neighbours along, so a
# replacement longer than the run it replaces simply draws over the next word.
# ("Invoice No: 106688" edited to "Statement" rendered as "StatementNo: 106688".)
# The only honest fix is to measure the gap and make the text fit it.
#
# Condense first (Tz), shrink only if condensing is not enough: Tz keeps the
# baseline, the cap height and the line's optical rhythm, so a few percent is
# invisible, whereas a smaller font size is immediately obvious next to its
# unchanged neighbours.
_FIT_MIN_TZ    = 80.0   # below this, condensing starts to look like a defect
_FIT_MIN_SCALE = 0.55   # and below this the text is too small to be worth having
_FIT_GAP_PTS   = 0.25   # hairline of clearance once we are reflowing anyway
_FIT_BAND      = 0.35   # share of height that must overlap to count as same-line
# Adjacent runs on a justified or kerned line sit a point or two apart, and
# extraction rounds their boxes, so a sub-point overrun is inside the noise.
# Without this every other edit would be condensed for an overlap nobody could
# see, which is its own kind of wrong.
_FIT_SLACK_PTS = 1.0


def _same_line_neighbour_x(orig, elements, skip_ids):
    """Left edge of the nearest thing to the RIGHT of *orig* on its own line,
    or None when the rest of the line is free.

    None means "do not constrain".  That is deliberate: with no measured
    obstacle there is nothing to collide with, and inventing a page-edge limit
    would mean guessing at a coordinate frame for no benefit.
    """
    try:
        ox, oy  = float(orig['x']), float(orig['y'])
        ox2     = float(orig['x2'])
        oy2     = float(orig['y2'])
    except (KeyError, TypeError, ValueError):
        return None
    oh = max(oy2 - oy, 0.1)
    best = None
    for el in elements:
        if el is orig or el.get('type') != 'text' or el.get('deleted'):
            continue
        if el.get('id') in skip_ids:      # being deleted or moved away: not an obstacle
            continue
        try:
            ex, ey, ey2 = float(el['x']), float(el['y']), float(el['y2'])
        except (KeyError, TypeError, ValueError):
            continue
        # Same line? Measure vertical overlap rather than comparing baselines:
        # OCR boxes on one line vary with ascenders and descenders.
        ov = min(oy2, ey2) - max(oy, ey)
        if ov < _FIT_BAND * min(oh, max(ey2 - ey, 0.1)):
            continue
        # To the right? Compare against our own START, so a neighbour whose box
        # slightly overlaps ours (common in OCR) still counts.
        if ex <= ox + 0.5:
            continue
        if best is None or ex < best:
            best = ex
    return best


def _fit_to_available(text, size, rl_font, x, limit_x, min_x2=None):
    """(size, tz, overflows) for rendering *text* inside limit_x.

    Returns the original size and no scaling whenever the text already fits —
    the overwhelmingly common case — so a normal edit is byte-for-byte what it
    was before this existed.

    `overflows` is True when even the floors leave the text wider than the gap.
    The floors are not a failure to try harder: replacing a word with a sentence
    cannot be made to fit AND stay readable, and silently rendering 3pt text
    loses the content just as surely as overlapping does.  So this fits as far
    as it can and says when that was not far enough, which is the one thing the
    caller cannot work out for itself.
    """
    # Never eat the word space.  Stopping just short of the next word's ink is
    # technically not an overlap, but "Invoice No:" edited to "Statement" then
    # reads "StatementNo:" — the complaint in a different costume.  So keep as
    # much of the gap the original left as a space in this font would occupy.
    keep = _FIT_GAP_PTS
    space = _font_assets.string_width(rl_font, ' ', size) or size * 0.25
    if min_x2 is not None:
        trailing = limit_x - float(min_x2)
        if trailing > 0:
            keep = max(keep, min(trailing, space))
    avail = (limit_x - keep) - x
    # And the replacement is ALWAYS allowed at least the room the original had.
    # Extraction routinely reports a neighbouring run starting inside the
    # previous one's box — adjacent runs, kerned pairs, slack OCR boxes — and
    # taking that at face value condensed a replacement that was SHORTER than
    # what it replaced.  Nothing that fits where the original sat can make the
    # page worse than it already was.
    if min_x2 is not None:
        avail = max(avail, float(min_x2) - x)
    natural = _font_assets.string_width(rl_font, text, size)
    if avail <= 0:
        # Nowhere to put it at all; squashing to nothing would be worse.
        return size, 100.0, natural > 0
    if natural <= 0 or natural <= avail + _FIT_SLACK_PTS:
        return size, 100.0, False
    # Acting at all, so aim for a hairline of clearance rather than dead level.
    target = max(avail - _FIT_GAP_PTS, 0.1)
    tz = max(_FIT_MIN_TZ, (target / natural) * 100.0)
    if natural * (tz / 100.0) <= target:
        return size, tz, False
    # Condensing alone is not enough — take the rest out of the font size.
    scale = max(_FIT_MIN_SCALE, target / (natural * (tz / 100.0)))
    return size * scale, tz, (natural * (tz / 100.0) * scale) > avail + 0.5


def _rl_to_basefont(rl_font):
    return {
        'Helvetica':            'Helvetica',
        'Helvetica-Bold':       'Helvetica-Bold',
        'Helvetica-Oblique':    'Helvetica-Oblique',
        'Helvetica-BoldOblique':'Helvetica-BoldOblique',
        'Times-Roman':          'Times-Roman',
        'Times-Bold':           'Times-Bold',
        'Times-Italic':         'Times-Italic',
        'Courier':              'Courier',
        'Courier-Bold':         'Courier-Bold',
    }.get(rl_font, 'Helvetica')


def _ensure_font(writer, page, base_font_name):
    """Ensure font is in page /Resources and return its resource name string."""

    # Resolve existing resources (may be an indirect ref after writer.append())
    res_ref = page.get('/Resources')
    old_resources = res_ref.get_object() if hasattr(res_ref, 'get_object') else res_ref
    if old_resources is None:
        old_resources = DictionaryObject()

    # Resolve existing /Font sub-dict
    fonts_ref = old_resources.get('/Font')
    if hasattr(fonts_ref, 'get_object'):
        fonts = fonts_ref.get_object()
    elif fonts_ref is not None:
        fonts = fonts_ref
    else:
        fonts = DictionaryObject()

    # Check if already present — early return, no resource rebuild needed.
    #
    # The match must be strict: callers emit one-byte WinAnsi strings via
    # `(text) Tj`, so reusing a /Type0 composite (/Identity-H consumes bytes in
    # PAIRS) renders the text as CJK CIDs or as nothing at all — the edited word
    # silently disappears.  A subset-prefixed font (BCDEEE+Helvetica) is unsafe
    # for the same reason plus a second one: it carries only the glyphs the
    # document already used, so newly typed characters come out blank.  Hence
    # exact base name, simple subtype, explicit WinAnsiEncoding — which a font
    # we created on an earlier pass always satisfies.  (font_assets.
    # ensure_embedded_font applies the same rule for bundled faces.)
    for name, ref in fonts.items():
        try:
            obj  = ref.get_object() if hasattr(ref, 'get_object') else ref
            base = str(obj.get('/BaseFont', '')).lstrip('/')
            if (base == base_font_name
                    and str(obj.get('/Subtype', '')) in ('/Type1', '/TrueType')
                    and str(obj.get('/Encoding', '')) == '/WinAnsiEncoding'):
                return str(name)
        except Exception:
            pass

    # Add new standard Type1 font as an indirect object
    nums = []
    for k in fonts.keys():
        try:
            nums.append(int(str(k).lstrip('/F').lstrip('F')))
        except Exception:
            pass
    next_num  = max(nums, default=0) + 1
    new_name  = '/F' + str(next_num)
    font_dict = DictionaryObject({
        NameObject('/Type'):     NameObject('/Font'),
        NameObject('/Subtype'):  NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/' + base_font_name),
        NameObject('/Encoding'): NameObject('/WinAnsiEncoding'),
    })
    fonts[NameObject(new_name)] = writer._add_object(font_dict)

    # Build a brand-new DictionaryObject for resources.  In-place mutation of
    # the object returned by get_object() is not reliable in pypdf 5.x after
    # writer.append(): the writer may have cloned the reader's object into its
    # pool but still serialise the original, so mutations are silently lost.
    # Assigning a fresh dict directly to the page guarantees the change is
    # written regardless of pypdf's internal object-identity tracking.
    new_resources = DictionaryObject()
    for k, v in old_resources.items():
        new_resources[NameObject(str(k))] = v
    new_resources[NameObject('/Font')] = fonts
    page[NameObject('/Resources')] = new_resources
    return new_name


def _ensure_extgstate(writer, page, alpha):
    """Ensure an ExtGState with the given non-stroke and stroke alpha is in the
    page /Resources. Returns the resource name (e.g. '/GS1')."""
    resources = page.get('/Resources')
    if hasattr(resources, 'get_object'):
        resources = resources.get_object()
    if resources is None:
        resources = DictionaryObject()
        page[NameObject('/Resources')] = resources

    ext_gs = resources.get('/ExtGState')
    if hasattr(ext_gs, 'get_object'):
        ext_gs = ext_gs.get_object()
    if ext_gs is None:
        ext_gs = DictionaryObject()
        resources[NameObject('/ExtGState')] = ext_gs

    # Reuse an existing entry with matching alpha if present
    for k, v in ext_gs.items():
        try:
            gs_obj = v.get_object() if hasattr(v, 'get_object') else v
            ca = gs_obj.get('/ca')
            CA = gs_obj.get('/CA')
            if ca is not None and abs(float(ca) - alpha) < 1e-4 and \
               CA is not None and abs(float(CA) - alpha) < 1e-4:
                return str(k)
        except Exception:
            pass

    # Pick next /GSn name
    nums = []
    for k in ext_gs.keys():
        try:
            nums.append(int(str(k).lstrip('/GS').lstrip('GS')))
        except Exception:
            pass
    next_num = max(nums, default=0) + 1
    new_name = '/GS' + str(next_num)

    from pypdf.generic import FloatObject
    gs_dict = DictionaryObject({
        NameObject('/Type'): NameObject('/ExtGState'),
        NameObject('/ca'):   FloatObject(alpha),
        NameObject('/CA'):   FloatObject(alpha),
    })
    ext_gs[NameObject(new_name)] = writer._add_object(gs_dict)
    return new_name


def _safe_pdf_str(text):
    """Encode text as a PDF string literal (latin-1 with escape sequences)."""
    parens = {'(', ')', '\\'}
    result = []
    for ch in text:
        o = ord(ch)
        if ch in parens:
            result.append('\\' + ch)
        elif 32 <= o <= 126:
            result.append(ch)
        elif o <= 255:
            result.append('\\%03o' % o)
        else:
            result.append('?')
    return ''.join(result)


def _filter_covered_text_items(text_items, covered_bboxes, merge_regions=None):
    """Remove text items whose bbox overlaps a covered (whited-out) position.
    Kept items are either outside all covered bboxes, or match the new_text
    of a text-content edit at that bbox.

    merge_regions: list of (x1,y1,x2,y2) footprints of row-merges. Re-extracting
    the scan can re-segment words differently than the ids we deleted, so we drop
    any OCR text whose centre falls inside a merge footprint (the originals are
    gone) while keeping the native merged run injected on top (ocr=false)."""
    merge_regions = merge_regions or []
    if not covered_bboxes and not merge_regions:
        return text_items

    THRESH = GHOST_OVERLAP_THRESH
    kept, removed = [], []

    def _centre_in_merge_region(cx, cy):
        for rx1, ry1, rx2, ry2 in merge_regions:
            if min(rx1, rx2) <= cx <= max(rx1, rx2) and \
               min(ry1, ry2) <= cy <= max(ry1, ry2):
                return True
        return False

    for item in text_items:
        ix1 = float(item.get('x',  0))
        iy1 = float(item.get('y',  0))
        ix2 = float(item.get('x2', ix1))
        iy2 = float(item.get('y2', iy1))
        item_text = item.get('text', '')
        item_is_ocr = bool(item.get('ocr'))

        # Row-merge footprint takes priority: drop the old OCR words inside it
        # (whatever the current segmentation), keep the native merged run.
        if merge_regions and _centre_in_merge_region((ix1 + ix2) / 2, (iy1 + iy2) / 2):
            if item_is_ocr:
                removed.append(item_text)
                logger.debug(f"[FILTER] DROP '{item_text}' (in merge region)")
                continue
            kept.append(item)
            logger.debug(f"[FILTER] KEEP merged run '{item_text}' (in merge region)")
            continue

        matching = []
        for b in covered_bboxes:
            pad = 2.0
            bx1 = float(b.get('x',  0)) - pad
            by1 = float(b.get('y',  0)) - pad
            bx2 = float(b.get('x2', b.get('x', 0))) + pad
            by2 = float(b.get('y2', b.get('y', 0))) + pad
            bw  = max(bx2 - bx1, 1.0)
            bh  = max(by2 - by1, 1.0)
            olap_x = max(0.0, min(ix2, bx2) - max(ix1, bx1))
            olap_y = max(0.0, min(iy2, by2) - max(iy1, by1))
            frac   = (olap_x * olap_y) / (bw * bh)
            if frac >= THRESH:
                matching.append((b, frac))

        if not matching:
            kept.append(item)
            continue

        is_injection = False
        for b, _f in matching:
            nt = b.get('new_text', '')
            if nt and (item_text == nt or item_text in nt.split()):
                is_injection = True
                break

        if is_injection:
            kept.append(item)
            logger.debug(f"[FILTER] KEEP injection '{item_text}' "
                         f"fracs={[round(f,3) for _,f in matching]}")
        else:
            has_edit = any(b.get('new_text') for b, _ in matching)
            tag = (f"ghost of pre-edit text "
                   f"(current={matching[0][0].get('new_text')!r})"
                   if has_edit else 'covered (moved/deleted)')
            removed.append(item_text)
            logger.debug(f"[FILTER] DROP '{item_text}' at "
                         f"({item.get('x',0):.1f},{item.get('y',0):.1f}) "
                         f"fracs={[round(f,3) for _,f in matching]} reason={tag}")

    logger.debug(f"[FILTER] done: {len(text_items)} → {len(kept)} "
                 f"(removed {len(removed)}: {removed})")
    return kept


def _tokenize_pdf_stream(data):
    """Tokenize raw PDF content stream bytes into (start, end, type, value) tuples.
    Types: 'op', 'num', 'str', 'name', 'arr_open', 'arr_close', 'dict_open', 'dict_close'.
    Correctly skips string literals and comments so operator tokens are unambiguous.
    """
    tokens = []
    i = 0
    n = len(data)
    DELIMITERS = frozenset(b' \t\r\n\x00\x0c()[]<>{}/%')

    while i < n:
        b = data[i]
        # Whitespace
        if b in (0x00, 0x09, 0x0A, 0x0C, 0x0D, 0x20):
            i += 1
            continue
        # Comment
        if b == 0x25:  # '%'
            while i < n and data[i] not in (0x0A, 0x0D):
                i += 1
            continue
        # Literal string (...)
        if b == 0x28:
            start = i
            i += 1
            depth = 1
            while i < n and depth > 0:
                ch = data[i]
                if ch == 0x5C:   # backslash escape
                    i += 2
                elif ch == 0x28:
                    depth += 1; i += 1
                elif ch == 0x29:
                    depth -= 1; i += 1
                else:
                    i += 1
            tokens.append((start, i, 'str', bytes(data[start:i])))
            continue
        # Dictionary << or hex string <...>
        if b == 0x3C:
            if i + 1 < n and data[i + 1] == 0x3C:
                tokens.append((i, i + 2, 'dict_open', b'<<'))
                i += 2
            else:
                start = i
                i += 1
                while i < n and data[i] != 0x3E:
                    i += 1
                i += 1
                tokens.append((start, i, 'str', bytes(data[start:i])))
            continue
        # >> or stray >
        if b == 0x3E:
            if i + 1 < n and data[i + 1] == 0x3E:
                tokens.append((i, i + 2, 'dict_close', b'>>'))
                i += 2
            else:
                i += 1
            continue
        # Array brackets
        if b == 0x5B:
            tokens.append((i, i + 1, 'arr_open', b'['))
            i += 1; continue
        if b == 0x5D:
            tokens.append((i, i + 1, 'arr_close', b']'))
            i += 1; continue
        # Name
        if b == 0x2F:
            start = i
            i += 1
            while i < n and data[i] not in DELIMITERS:
                i += 1
            tokens.append((start, i, 'name', bytes(data[start:i])))
            continue
        # Number or operator
        start = i
        while i < n and data[i] not in DELIMITERS:
            i += 1
        tok = bytes(data[start:i])
        if tok:
            try:
                float(tok)
                tokens.append((start, i, 'num', tok))
            except ValueError:
                tokens.append((start, i, 'op', tok))

    return tokens


def _scrub_injections_at_positions(stream_bytes, target_xys, tolerance=3.0):
    """Remove emit_text BT..ET blocks (tagged '% pw_inj') whose Tm x,y position
    matches any of the given target coordinates within tolerance.

    Works cross-session: every block written by emit_text carries a '% pw_inj'
    marker in the stream itself, so we can find and remove old injections even
    when covered_text_bboxes is empty (new server session).

    target_xys  : list of (x, y) floats — the emission positions of words
                  that are about to be re-injected this pass.
    tolerance   : max distance in PDF points for a position to be considered
                  a match (default 3.0 — word bboxes are typically 5+ pt apart).
    """
    if not target_xys or not stream_bytes:
        return stream_bytes

    marker_pat = re.compile(rb'% pw_inj [0-9a-f]{8}')
    # Matches our '%.6f %.6f %.6f %.6f %.3f %.3f Tm' format
    tm_pat = re.compile(
        rb'[-+]?\d+\.\d+\s+[-+]?\d+\.\d+\s+[-+]?\d+\.\d+\s+[-+]?\d+\.\d+\s+'
        rb'([-+]?\d+\.?\d*)\s+([-+]?\d+\.?\d*)\s+Tm'
    )

    matches = list(marker_pat.finditer(stream_bytes))
    removed = 0
    # Process in reverse so removals don't shift earlier offsets
    for m in reversed(matches):
        marker_start = m.start()
        et_pos = stream_bytes.find(b'ET', marker_start)
        if et_pos == -1:
            continue
        block = stream_bytes[marker_start:et_pos + 2]
        tm_m = tm_pat.search(block)
        if not tm_m:
            continue
        try:
            inj_x = float(tm_m.group(1))
            inj_y = float(tm_m.group(2))
        except (ValueError, IndexError):
            continue
        for tx, ty in target_xys:
            if abs(inj_x - tx) <= tolerance and abs(inj_y - ty) <= tolerance:
                et_end = et_pos + 2
                while et_end < len(stream_bytes) and stream_bytes[et_end:et_end+1] in (b'\n', b'\r', b' '):
                    et_end += 1
                logger.debug(f"[SCRUB-POS] removed old injection ({inj_x:.3f},{inj_y:.3f}) "
                             f"→ target ({tx:.3f},{ty:.3f})")
                stream_bytes = stream_bytes[:marker_start] + stream_bytes[et_end:]
                removed += 1
                break
    if removed:
        logger.debug(f"[SCRUB-POS] total old injections removed: {removed}")
    return stream_bytes


def _scrub_text_under_redactions(stream_bytes, redact_rects, ctm=None):
    """Surgically remove text-show operators (Tj/TJ/'/\") whose cursor position
    falls within a redaction rectangle, leaving the rest of each BT..ET block intact.

    redact_rects : list of (x1, y1, x2, y2) in raw PDF page space (y-up).
    ctm          : (a,b,c,d,e,f) matrix active at stream start, or None for identity.
                   Applied to text-matrix positions to convert them to page space.

    Per-operator removal: only the matching text-show op and its preceding operand
    tokens are skipped.  Unaffected runs in the same BT..ET block are preserved,
    which avoids stripping whole paragraphs or pages when the PDF generator places
    many text runs inside a single BT..ET block (e.g. LaTeX, InDesign).
    """
    if not redact_rects or not stream_bytes:
        return stream_bytes

    pad = 4.0  # tolerance in points

    def page_pos(x, y):
        if ctm is None:
            return x, y
        a, b, c, d, e, f = ctm
        return a * x + c * y + e, b * x + d * y + f

    def in_redact(x, y):
        px, py = page_pos(x, y)
        for rx1, ry1, rx2, ry2 in redact_rects:
            if rx1 - pad <= px <= rx2 + pad and ry1 - pad <= py <= ry2 + pad:
                return True
        return False

    data = bytearray(stream_bytes)
    tokens = _tokenize_pdf_stream(data)

    output_parts = []
    prev_end = 0
    j = 0
    total = len(tokens)

    while j < total:
        tok_start, tok_end, tok_type, tok_val = tokens[j]

        if tok_type == 'op' and tok_val == b'BT':
            # Flush content before this BT block
            output_parts.append(bytes(data[prev_end:tok_start]))

            bt_start = tok_start
            j += 1

            cur_x = cur_y = 0.0
            line_x = line_y = 0.0
            text_leading = 0.0
            num_stack = []
            et_end = tok_end  # fallback if no ET found

            # skip_ranges: list of (byte_start, byte_end) to omit from the block.
            # Each entry covers a redacted text-show operator and its operand(s).
            # pending_start tracks where the current operand sequence begins (in bytes)
            # — reset to the end of every operator so it always points to the first
            # byte of the operands for the *next* upcoming operator.
            skip_ranges = []
            pending_start = tok_end  # operands for first inner op start after BT

            while j < total:
                ts, te, tt, tv = tokens[j]

                if tt == 'op' and tv == b'ET':
                    et_end = te
                    j += 1
                    break

                if tt == 'op':
                    op = tv
                    if op in (b'Tj', b'TJ', b"'", b'"'):
                        if in_redact(cur_x, cur_y):
                            # Mark the operand(s) + operator as a skip range
                            skip_ranges.append((pending_start, te))
                            logger.debug(f"[REDACT SCRUB] Removed {tv.decode()} at "
                                         f"stream ({cur_x:.1f},{cur_y:.1f})")
                        # ' and " also advance the text line
                        if op in (b"'", b'"'):
                            line_y -= text_leading
                            cur_x, cur_y = line_x, line_y
                    elif op == b'Tm' and len(num_stack) >= 6:
                        cur_x = num_stack[-2]
                        cur_y = num_stack[-1]
                        line_x, line_y = cur_x, cur_y
                    elif op in (b'Td', b'TD') and len(num_stack) >= 2:
                        line_x += num_stack[-2]
                        line_y += num_stack[-1]
                        cur_x, cur_y = line_x, line_y
                    elif op == b'TL' and len(num_stack) >= 1:
                        text_leading = num_stack[-1]
                    elif op == b'T*':
                        line_y -= text_leading
                        cur_x, cur_y = line_x, line_y

                    num_stack = []
                    pending_start = te  # next operator's operands start here
                else:
                    # Operand token — update num_stack for position tracking
                    if tt == 'num':
                        try:
                            num_stack.append(float(tv))
                        except ValueError:
                            num_stack = []
                    else:
                        num_stack = []

                j += 1

            # Emit the block, skipping the collected redaction ranges
            pos = bt_start
            for skip_s, skip_e in skip_ranges:
                output_parts.append(bytes(data[pos:skip_s]))
                pos = skip_e
            output_parts.append(bytes(data[pos:et_end]))

            prev_end = et_end
            continue

        j += 1

    output_parts.append(bytes(data[prev_end:]))
    result = b''.join(output_parts)
    logger.debug(f"[REDACT SCRUB] stream {len(stream_bytes)} → {len(result)} bytes "
                 f"({len(stream_bytes)-len(result)} removed)")
    return result


def _burn_redactions_into_images(pdf_bytes, redact_rects):
    """Destroy image pixels that fall under a redaction rectangle.

    Complements _scrub_text_under_redactions (which removes native *vector*
    text): here we paint solid black over the region of every image XObject that
    lies beneath a redaction box, so a scanned page or an embedded photo cannot
    be recovered by simply deleting the black cover rectangle drawn on top of it.
    Without this, redaction over image content is purely cosmetic — the sensitive
    pixels remain in the file — which breaks the tool's "permanently baked in"
    promise for the common scanned-document case.

    redact_rects : list of (x1, y1, x2, y2) in raw content-stream (MediaBox)
                   space, y-up — the same space extract_pdf_images resolves image
                   placements in, so placement CTMs and redaction rects compare
                   directly.

    Returns modified PDF bytes (or the original when nothing intersects / on a
    fatal error).  Fails SAFE: an intersecting image that cannot be decoded to
    paint precisely is blacked out in full rather than left recoverable.

    Known limitation: text painted via a Form XObject (rather than the page
    content stream) is scrubbed by _scrub_text_under_redactions only if it lives
    in the page stream; text inside nested XObjects is not reached.  Image content
    — the primary leak for scanned pages — is handled here regardless of nesting
    depth of the *image*, because we resolve placements from the page stream CTM.
    """
    if not redact_rects or not pdf_bytes:
        return pdf_bytes
    try:
        import pikepdf
        import zlib
        from PIL import ImageDraw as _PDraw
    except Exception as exc:
        logger.error(f"[REDACT] imaging libs unavailable, cannot burn images: {exc}")
        return pdf_bytes

    def _mat_mul(A, B):
        a1, b1, c1, d1, e1, f1 = A
        a2, b2, c2, d2, e2, f2 = B
        return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
                c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
                e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)

    with _NATIVE_LOCK:
        try:
            pdf = pikepdf.open(io.BytesIO(pdf_bytes))
        except Exception as exc:
            logger.error(f"[REDACT] could not open PDF to burn images: {exc}")
            return pdf_bytes
        try:
            page = pdf.pages[0]
            res = page.get('/Resources')
            xobjs = res.get('/XObject') if res is not None else None
            if not xobjs:
                return pdf_bytes

            # Concatenate the decoded page content stream operators.
            contents = page.get('/Contents')
            if isinstance(contents, pikepdf.Array):
                raw = b'\n'.join(bytes(s.read_bytes()) for s in contents
                                 if isinstance(s, pikepdf.Stream))
            elif isinstance(contents, pikepdf.Stream):
                raw = bytes(contents.read_bytes())
            else:
                return pdf_bytes

            # Walk operators tracking the CTM stack; record each image Do
            # placement (an XObject may be placed more than once).
            ctm = (1, 0, 0, 1, 0, 0)
            q_stack = []
            buf = []
            placements = {}   # xobj_name -> [ctm, ...]
            for tok in raw.split():
                if tok == b'q':
                    q_stack.append(ctm); buf = []
                elif tok == b'Q':
                    if q_stack:
                        ctm = q_stack.pop()
                    buf = []
                elif tok == b'cm' and len(buf) >= 6:
                    try:
                        ctm = _mat_mul(ctm, tuple(float(buf[-6 + j]) for j in range(6)))
                    except Exception:
                        pass
                    buf = []
                elif tok == b'Do' and buf:
                    name = buf[-1].decode('latin-1', 'replace').lstrip('/')
                    placements.setdefault(name, []).append(ctm)
                    buf = []
                else:
                    buf.append(tok)

            changed = False
            for name, ctms in placements.items():
                try:
                    obj = xobjs.get('/' + name)
                except Exception:
                    obj = None
                if not isinstance(obj, pikepdf.Stream):
                    continue
                if str(obj.get('/Subtype', '')) != '/Image':
                    continue
                W = int(obj.get('/Width', 0) or 0)
                H = int(obj.get('/Height', 0) or 0)
                if W <= 0 or H <= 0:
                    continue

                # Collect the pixel boxes (per placement × redaction rect) to
                # black out.  Redaction rect corners are mapped through the
                # inverse placement CTM into image unit space, then to pixels
                # (image row 0 = top, i.e. v = 1).
                boxes = []
                for M in ctms:
                    a, b, c, d, e, f = M
                    det = a * d - b * c
                    if abs(det) < 1e-9:
                        continue
                    pbx = [a * u + c * v + e for u, v in ((0, 0), (1, 0), (1, 1), (0, 1))]
                    pby = [b * u + d * v + f for u, v in ((0, 0), (1, 0), (1, 1), (0, 1))]
                    p_x1, p_x2 = min(pbx), max(pbx)
                    p_y1, p_y2 = min(pby), max(pby)
                    for (rx1, ry1, rx2, ry2) in redact_rects:
                        if rx2 <= p_x1 or rx1 >= p_x2 or ry2 <= p_y1 or ry1 >= p_y2:
                            continue   # no overlap with this placement
                        us, vs = [], []
                        for (px, py) in ((rx1, ry1), (rx2, ry1), (rx2, ry2), (rx1, ry2)):
                            us.append(( d * (px - e) - c * (py - f)) / det)
                            vs.append((-b * (px - e) + a * (py - f)) / det)
                        umin, umax = max(0.0, min(us)), min(1.0, max(us))
                        vmin, vmax = max(0.0, min(vs)), min(1.0, max(vs))
                        if umax <= umin or vmax <= vmin:
                            continue
                        col1 = max(0, min(W, int(umin * W)))
                        col2 = max(0, min(W, int(round(umax * W))))
                        row1 = max(0, min(H, int((1.0 - vmax) * H)))
                        row2 = max(0, min(H, int(round((1.0 - vmin) * H))))
                        if col2 > col1 and row2 > row1:
                            boxes.append((col1, row1, col2, row2))

                if not boxes:
                    continue

                try:
                    pil = pikepdf.PdfImage(obj).as_pil_image()
                    if pil.mode == 'L':
                        pil, ncomp, new_cs = pil.convert('L'), 1, '/DeviceGray'
                        fill = 0
                    else:
                        pil, ncomp, new_cs = pil.convert('RGB'), 3, '/DeviceRGB'
                        fill = (0, 0, 0)
                    draw = _PDraw.Draw(pil)
                    for (c1, r1, c2, r2) in boxes:
                        draw.rectangle([c1, r1, c2 - 1, r2 - 1], fill=fill)
                    obj.write(zlib.compress(pil.tobytes(), 6),
                              filter=pikepdf.Name('/FlateDecode'))
                    obj.ColorSpace = pikepdf.Name(new_cs)
                    obj.BitsPerComponent = 8
                    obj.Width, obj.Height = pil.size
                    for k in ('/Decode', '/DecodeParms', '/SMask', '/Mask'):
                        if k in obj:
                            del obj[k]
                    changed = True
                except Exception as dec_exc:
                    # Undecodable (exotic filter/colourspace).  We cannot paint a
                    # precise box, so black the whole image out — over-redaction,
                    # but never a recoverable leak.
                    logger.warning(f"[REDACT] image '{name}' undecodable ({dec_exc}); "
                                   f"blacking out the entire image to prevent a leak")
                    try:
                        cs = obj.get('/ColorSpace')
                        ncomp = 3 if (cs is not None and 'RGB' in str(cs)) else 1
                        obj.write(zlib.compress(b'\x00' * (W * H * ncomp), 6),
                                  filter=pikepdf.Name('/FlateDecode'))
                        obj.ColorSpace = pikepdf.Name('/DeviceRGB' if ncomp == 3 else '/DeviceGray')
                        obj.BitsPerComponent = 8
                        for k in ('/Decode', '/DecodeParms', '/SMask', '/Mask'):
                            if k in obj:
                                del obj[k]
                        changed = True
                    except Exception as blk_exc:
                        logger.error(f"[REDACT] could not black out '{name}': {blk_exc}")

            if not changed:
                return pdf_bytes
            out = io.BytesIO()
            pdf.save(out)
            logger.info(f"[REDACT] burned redactions into image content "
                        f"({len(placements)} placement group(s))")
            return out.getvalue()
        finally:
            pdf.close()


def apply_edits(pdf_bytes, edits, page_w, page_h, img_counter=0, covered_text_bboxes=None):
    """
    Apply edits by appending raw PDF operators to the page content stream.
    Avoids merge_page overlay compositing which caused transparency artefacts.
    Page remains fully text-extractable and re-editable after saving.
    """


    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    writer = pypdf.PdfWriter()
    writer.append(reader)
    page = writer.pages[0]

    deleted_ids  = set(edits.get('deleted_ids', []))
    moved_map    = {m['id']: m for m in edits.get('moved', [])}
    text_changed = {t['id']: t for t in edits.get('text_changed', [])}
    markup_items = edits.get('markup', [])
    orig_map     = {e['id']: e for e in edits.get('original_elements', [])}
    redact_ids   = deleted_ids | set(moved_map.keys()) | set(text_changed.keys())

    # ── Rotation-aware coordinate conversion (display space → raw content stream) ──
    # The JS editor works in DISPLAY space: pdfium renders the page upright
    # (applying /Rotate), so pageW/pageH/pageTop sent to the JS are the DISPLAY
    # dimensions.  c2pdf() stores markup/shape coords in display space.
    # The raw content stream uses the MEDIABOX dimensions (before rotation).
    # For /Rotate 0 and 180 the axes are the same; for /Rotate 90 and 270
    # the x/y axes are swapped, so we must remap before injecting PDF operators.
    #
    # OCR items and native text items already live in raw space (ocr_extract_text_items
    # converts them); only JS-originated coords (markup, shapes, stickies, etc.) need
    # this transform.
    _ae_pypdf  = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    _ae_page   = _ae_pypdf.pages[0]
    _ae_mb     = _ae_page.mediabox
    _ae_raw_W  = float(_ae_mb.width)
    _ae_raw_H  = float(_ae_mb.height)
    _ae_rotate = int(_ae_page.get('/Rotate', 0)) % 360

    # page_w / page_h coming in are DISPLAY dims (from _render_page_hires / pdfium).
    # We use them as the display reference for the inversion formulas.
    _disp_W = float(page_w)   # display width  (== raw_H for /Rotate 90/270)
    _disp_H = float(page_h)   # display height (== raw_W for /Rotate 90/270)

    def display_to_raw(dx, dy):
        """Convert a point from JS display space (y-up, display dims) to raw
        content stream space (y-up, raw mediabox dims).  Identity for /Rotate 0.

        For /Rotate 90 (CCW): raw mediabox is landscape (raw_W x raw_H),
        display is portrait (disp_W x disp_H = raw_H x raw_W).
        Corner verification (raw_W=842, raw_H=595, disp_H=842):
          display(0,   0)   bottom-left  → raw(842, 0)   right-bottom ✓
          display(595, 0)   bottom-right → raw(842, 595) right-top    ✓
          display(0,   842) top-left     → raw(0,   0)   left-bottom  ✓
          display(595, 842) top-right    → raw(0,   595) left-top     ✓
        """
        if _ae_rotate == 0:
            return dx, dy
        elif _ae_rotate == 90:
            # raw_x = raw_W - dy,  raw_y = dx
            raw_x = _ae_raw_W - dy
            raw_y = dx
            return raw_x, raw_y
        elif _ae_rotate == 180:
            return _ae_raw_W - dx, _ae_raw_H - dy
        elif _ae_rotate == 270:
            # raw_x = dy,  raw_y = raw_H - dx
            raw_x = dy
            raw_y = _ae_raw_H - dx
            return raw_x, raw_y
        return dx, dy

    # On a rotated page the editor sent text-item coords in DISPLAY space (the
    # extract path oriented them with _orient_text_items_for_display).  Convert
    # them back to raw content space here so every injection/cover/scrub step
    # below — all of which expect raw coords — is unchanged.  No-op for /Rotate 0.
    if _ae_rotate:
        def _txt_display_to_raw(it):
            if 'x' not in it or 'y' not in it:
                return
            rx, ry = display_to_raw(float(it['x']), float(it['y']))
            if 'x2' in it and 'y2' in it:
                rx2, ry2 = display_to_raw(float(it['x2']), float(it['y2']))
                it['x'],  it['y']  = min(rx, rx2), min(ry, ry2)
                it['x2'], it['y2'] = max(rx, rx2), max(ry, ry2)
            else:
                it['x'], it['y'] = rx, ry
            if 'original_x' in it: it['original_x'] = it['x']
            if 'original_y' in it: it['original_y'] = it['y']
            if 'baseline_y' in it: it['baseline_y'] = it['y']
        for _mid, _mv in moved_map.items():
            if orig_map.get(_mid, {}).get('type') == 'text':
                _txt_display_to_raw(_mv)
        for _og in orig_map.values():
            if _og.get('type') == 'text':
                _txt_display_to_raw(_og)

    logger.debug(f"[apply_edits] /Rotate={_ae_rotate} raw={_ae_raw_W:.0f}x{_ae_raw_H:.0f} display={_disp_W:.0f}x{_disp_H:.0f}")
    # Detect the CTM that will be active when our appended content runs.
    # Only depth-0 cm operators persist to end of stream — those inside
    # q/Q pairs are undone by Q and don't affect our appended content.
    inject_ctm = _get_injection_ctm(reader.pages[0])
    inv_ctm    = _invert_matrix(*inject_ctm) if inject_ctm else None
    if inv_ctm:
        ctm_op = ('%.6f %.6f %.6f %.6f %.6f %.6f cm' % inv_ctm).encode()
        logger.debug(f"[apply_edits] Injection CTM: {inject_ctm}, applying inverse")
    else:
        ctm_op = None
        logger.debug("[apply_edits] Injection CTM: identity, using coords directly")

    lines = [b'q']
    if ctm_op:
        lines.append(ctm_op)  # Inverse of injection CTM → puts us in raw content stream space
        # display_to_raw() then converts JS display coords → raw space

    # 0. Redaction boxes — solid black, drawn first so they permanently cover
    #    original page content before any other markup is applied on top.
    redact_boxes = edits.get('redacts', [])
    _raw_redact_rects = []   # collected for native-text stream scrubbing below
    for rb in redact_boxes:
        rx1, ry1 = display_to_raw(float(rb.get('x', 0)),  float(rb.get('y', 0)))
        rx2, ry2 = display_to_raw(float(rb.get('x2', 0)), float(rb.get('y2', 0)))
        rlx1, rly1 = min(rx1, rx2), min(ry1, ry2)
        rw,   rh   = abs(rx2 - rx1), abs(ry2 - ry1)
        _raw_redact_rects.append((rlx1, rly1, rlx1 + rw, rly1 + rh))
        lines.append(b'q')
        lines.append(b'0 0 0 rg')   # black fill
        lines.append(b'0 0 0 RG')   # black stroke
        lines.append(('%.3f %.3f %.3f %.3f re' % (rlx1, rly1, rw, rh)).encode())
        lines.append(b'f')
        lines.append(b'Q')

    # 1. Cover original text.
    # For native text: white rect erases it cleanly.
    # For OCR text: sample the background colour from beside the word
    # in the rendered page image, use that colour for the cover rect.

    # Render page to image for background colour sampling.
    # Used for ALL text types — native and OCR alike.
    # Sampling above/below the text bbox avoids glyph pixels.
    _bg_img   = None
    _bg_scale = 1.5
    try:
        with _NATIVE_LOCK:
            _bdoc = pdfium.PdfDocument(pdf_bytes)
            try:
                _bbmp   = _bdoc[0].render(scale=_bg_scale)
                _bg_img = _bbmp.to_pil().convert('RGB')
            finally:
                _bdoc.close()
    except Exception:
        pass

    # OCR coordinates are ALWAYS in standard PDF space (y=0 at bottom)
    # because ocr_extract_text_items converts pixel coords to standard PDF space.
    # Native text coords also arrive in standard space after CTM correction.
    # So always use standard conversion: img_y = (page_h - pdf_y) * scale.
    def _pdf_y_to_img_y(pdf_y):
        return int((page_h - pdf_y) * _bg_scale)  # standard: y=0 at bottom

    def _sample_bg_and_fg(x1, y1, x2, y2):
        """
        Universal Sampler: Directional Peak Detection.
        Optimized for noisy scans to find the 'True Ink' by looking for the
        most extreme brightness opposite to the background.
        """
        if _bg_img is None:
            return (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)

        iw, ih = _bg_img.size
        ix1, ix2 = int(x1 * _bg_scale), int(x2 * _bg_scale)
        iy1, iy2 = _pdf_y_to_img_y(y1), _pdf_y_to_img_y(y2)
        itop, ibot = min(iy1, iy2), max(iy1, iy2)
        ileft, iright = min(ix1, ix2), max(ix1, ix2)

        pad = 6
        sample_box = (max(0, ileft - pad), max(0, itop - pad), min(iw, iright + pad), min(ih, ibot + pad))

        patch = np.array(_bg_img.crop(sample_box))
        if patch.size == 0:
            return (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)

        pixels = patch.reshape(-1, 3)
        brightness = np.dot(pixels, [0.299, 0.587, 0.114])

        # 1. FIND BACKGROUND (The Majority Peak)
        counts, bins = np.histogram(brightness, bins=32, range=(0, 256))
        bg_bin_idx = np.argmax(counts)
        bg_val = (bins[bg_bin_idx] + bins[bg_bin_idx + 1]) / 2

        bg_mask = (brightness >= bins[bg_bin_idx]) & (brightness <= bins[bg_bin_idx + 1])
        bg_raw = np.mean(pixels[bg_mask], axis=0) if np.any(bg_mask) else np.mean(pixels, axis=0)
        bg_color = tuple(c / 255.0 for c in bg_raw)

        # 2. FIND FOREGROUND (The Extreme Contrast)
        # Determine if we are on a Light or Dark background
        is_light_bg = bg_val > 128

        # We look for 'Ink' in the opposite half of the brightness spectrum
        if is_light_bg:
            # On light paper, look for the darkest 10% of pixels in the whole patch
            ink_threshold = np.percentile(brightness, 10)
            ink_mask = brightness <= ink_threshold
        else:
            # On dark paper, look for the brightest 10% (white text)
            ink_threshold = np.percentile(brightness, 90)
            ink_mask = brightness >= ink_threshold

        ink_pixels = pixels[ink_mask]

        if len(ink_pixels) > 5:
            # Use Median for ink to avoid capturing the 'fuzzy' anti-aliased edges
            fg_raw = np.median(ink_pixels, axis=0)
            fg_color = tuple(c / 255.0 for c in fg_raw)
        else:
            # Safety fallback
            contrasting = 0.0 if is_light_bg else 1.0
            fg_color = (contrasting, contrasting, contrasting)

        # Snap near-grey ink to pure black/white.  Scanned "black" text samples
        # to a neutral mid-grey (~0.33 luminance) with anti-aliased edges, so
        # re-injecting it as vector text looks washed-out.  Users expect a clean
        # black (or white) like commercial editors and never set a colour.
        # Guard with saturation so genuinely COLOURED ink (red/blue stamps etc.)
        # is left untouched — only near-neutral ink is snapped.
        _fr, _fg, _fb = fg_color
        _fg_lum = 0.299 * _fr + 0.587 * _fg + 0.114 * _fb
        _fg_sat = max(_fr, _fg, _fb) - min(_fr, _fg, _fb)   # 0 = perfectly neutral
        if _fg_sat < 0.20:
            if _fg_lum < 0.55:
                fg_color = (0.0, 0.0, 0.0)
            elif _fg_lum > 0.80:
                fg_color = (1.0, 1.0, 1.0)

        return bg_color, fg_color

    for eid in redact_ids:
        orig = orig_map.get(eid)
        if not orig or orig.get('type') != 'text':
            continue

        x1 = float(orig['x'])
        y1 = float(orig['y'])
        x2 = float(orig.get('x2', x1 + float(orig['size']) * len(orig['text']) * 0.5))
        y2 = float(orig.get('y2', y1 + float(orig['size'])))
        pad_x, pad_y = 1.5, 1.0

        # Sample background and foreground from the rendered page
        (cr,cg,cb), (fr,fg,fb) = _sample_bg_and_fg(x1, y1, x2, y2)

        # Store sampled fg colour on orig so emit_text uses it
        if orig.get('ocr'):
            _sc = '#{:02x}{:02x}{:02x}'.format(
                int(fr*255), int(fg*255), int(fb*255))
            orig['_sampled_color'] = _sc
        logger.debug(f"[BG_SAMPLE] id={eid} ocr={orig.get('ocr')} "
                     f"bg=({cr:.3f},{cg:.3f},{cb:.3f}) "
                     f"fg=({fr:.3f},{fg:.3f},{fb:.3f}) "
                     f"sampled_color={orig.get('_sampled_color','n/a')}")

        # Draw cover rect in background colour
        lines.append(('%.3f %.3f %.3f rg %.3f %.3f %.3f RG' %
                      (cr,cg,cb,cr,cg,cb)).encode())
        lines.append(('%.3f %.3f %.3f %.3f re f' % (
            x1-pad_x, y1-pad_y, (x2-x1)+pad_x*2, (y2-y1)+pad_y*2
        )).encode())

    # 1b. Row-merge footprint (_coverRect) handling. Two cases:
    #   OCR merge   → paint a sampled-background raster fill over the WHOLE
    #                 footprint. Re-extracting the scan can re-segment words
    #                 differently, leaving uncaptured ink (e.g. a "Gr" fragment
    #                 OCR never read) exposed between per-word patches; one region
    #                 fill wipes everything the merged text replaces.
    #   native merge → the originals are real vector text, so SCRUB them out of
    #                 the content stream (collected here, removed below) — no
    #                 raster patch, which also avoids a flat fill on coloured bg.
    # (_coverRect comes from text-item coords — raw page space, used directly.)
    _merge_scrub_rects = []
    for _m in markup_items:
        _cr = _m.get('_coverRect')
        if not _cr:
            continue
        _cx1 = float(_cr.get('x1', 0)); _cy1 = float(_cr.get('y1', 0))
        _cx2 = float(_cr.get('x2', 0)); _cy2 = float(_cr.get('y2', 0))
        _rlx, _rly = min(_cx1, _cx2), min(_cy1, _cy2)
        _rw,  _rh  = abs(_cx2 - _cx1), abs(_cy2 - _cy1)
        # Default True (older OCR merges have no _srcOcr key).
        if _m.get('_srcOcr', True):
            (_mcr, _mcg, _mcb), _ = _sample_bg_and_fg(_rlx, _rly, _rlx + _rw, _rly + _rh)
            lines.append(('%.3f %.3f %.3f rg %.3f %.3f %.3f RG' %
                          (_mcr, _mcg, _mcb, _mcr, _mcg, _mcb)).encode())
            lines.append(('%.3f %.3f %.3f %.3f re f' % (_rlx, _rly, _rw, _rh)).encode())
        else:
            _merge_scrub_rects.append((_rlx, _rly, _rlx + _rw, _rly + _rh))

    # Reset colour state after all cover rects
    lines.append(b'1 1 1 rg 1 1 1 RG')

    # 2. Draw new/moved/markup text
    # For /Rotate != 0 pages we use Tm (absolute text matrix) instead of Td.
    # Tm = [a b c d x y] sets text position AND orientation.
    # We pre-rotate the text matrix to cancel the viewer's /Rotate transform,
    # so the text appears upright after the viewer applies /Rotate.
    # Derivation: viewer_matrix * our_Tm_rotation = identity
    #   /Rotate 90  (viewer CCW 90°):  Tm rotation = 0 -1 1 0
    #   /Rotate 180 (viewer 180°):      Tm rotation = -1 0 0 -1
    #   /Rotate 270 (viewer CW 90°):    Tm rotation = 0 1 -1 0
    # Tm rotation matrices to counteract the viewer's /Rotate transform.
    # The viewer for /Rotate 90 uses CTM [[0,1],[-1,0]] (maps raw→display).
    # We need Tm such that viewer_ctm * [[a,c],[b,d]] = identity.
    # Solution: [[a,c],[b,d]] = viewer_ctm^-1
    # /Rotate 90:  viewer=[[0,1],[-1,0]] → inv=[[0,-1],[1,0]] → a=0,b=1,c=-1,d=0
    # /Rotate 270: viewer=[[0,-1],[1,0]] → inv=[[0,1],[-1,0]] → a=0,b=-1,c=1,d=0
    # /Rotate 180: viewer=[[-1,0],[0,-1]] → inv=same (self-inverse) → a=-1,b=0,c=0,d=-1
    _TM_ROTATION = {
        0:   (1,  0,  0,  1),
        90:  (0,  1, -1,  0),
        180: (-1, 0,  0, -1),
        270: (0, -1,  1,  0),
    }
    _tm_a, _tm_b, _tm_c, _tm_d = _TM_ROTATION.get(_ae_rotate, (1, 0, 0, 1))

    def emit_text(x, y, text, size, rl_font, color='#000000', tz=100.0):
        inject_id = '%08x' % random.randint(0, 0xFFFFFFFF)
        logger.debug(f"[DBG-EMIT] \"{text}\" size={size:.3f} rl_font={rl_font} "
                     f"x={x:.3f} y={y:.3f} color={color} inject_id={inject_id}")
        # Bundled fonts (Calibri/Arial/Times/Courier via Carlito/Liberation) are
        # embedded as simple TrueType + WinAnsiEncoding so this (text) Tj path is
        # unchanged.  Base-14 logical names fall through to the standard path.
        fname = _font_assets.ensure_embedded_font(writer, page, rl_font)
        if fname is None:
            base  = _rl_to_basefont(rl_font)
            fname = _ensure_font(writer, page, base)
        r, g, b = _hex_to_rgb(color)
        s = _safe_pdf_str(text)
        # Tm sets position and text orientation. The (a,b,c,d) components are a
        # pure rotation matrix (not scaled by size) — Tf handles the font size.
        # This cancels the viewer's /Rotate so text appears upright.
        # The '% pw_inj' comment is a PDF stream comment (ignored by renderers)
        # used to locate and remove this specific block on subsequent apply passes.
        out = [
            ('% pw_inj ' + inject_id).encode(),
            b'BT',
            b'0 Tr',   # reset text rendering mode to fill — searchable-PDF layers
                       # use Tr=3 (invisible) so their OCR text doesn't show; that
                       # state persists into any appended BT block unless we reset it
            ('%.3f %.3f %.3f rg' % (r, g, b)).encode(),
            ('%s %.3f Tf' % (fname, size)).encode(),
        ]
        # Tz is horizontal scaling, in PERCENT.  It belongs to the text state,
        # which q/Q saves — but every block we append lives inside the SAME
        # outer q, so a condensed run would otherwise squash every run emitted
        # after it.  Set it and put it back, in the block that uses it.
        if abs(tz - 100.0) > 0.05:
            out.append(('%.2f Tz' % tz).encode())
        out += [
            ('%.6f %.6f %.6f %.6f %.3f %.3f Tm' % (_tm_a, _tm_b, _tm_c, _tm_d, x, y)).encode(),
            ('(%s) Tj' % s).encode(),
        ]
        if abs(tz - 100.0) > 0.05:
            out.append(b'100 Tz')
        out.append(b'ET')
        return out, inject_id

    _emit_inject_ids = {}   # {eid: new inject_id} — populated as emit_text is called

    # Collect (x, y) emit positions for all items about to be re-injected.
    # _scrub_injections_at_positions uses these to remove old pw_inj blocks from
    # the existing stream — works cross-session because it scans the stream itself.
    _target_xys = []
    for _pre_eid, _pre_move in moved_map.items():
        if _pre_eid not in deleted_ids:
            _target_xys.append((float(_pre_move['x']), float(_pre_move['y'])))
    for _pre_eid in text_changed:
        if _pre_eid in deleted_ids or _pre_eid in moved_map:
            continue
        _pre_orig = orig_map.get(_pre_eid)
        if _pre_orig and _pre_orig.get('type') == 'text':
            _target_xys.append((
                float(_pre_orig['x']),
                float(_pre_orig.get('baseline_y', _pre_orig['y']))
            ))

    for eid, move in moved_map.items():
        if eid in deleted_ids:
            continue
        orig   = orig_map.get(eid)
        if not orig or orig.get('type') != 'text':
            continue
        change = text_changed.get(eid, {})
        _use_font_moved = move.get('rl_font', '') or change.get('rl_font', orig.get('rl_font', 'Helvetica'))
        render_size = float(orig.get('font_size') or float(orig.get('size', 8)) * 1.35)
        # move entry may carry an explicit pixel-sampled size override
        _move_color  = move.get('color', '')
        _move_size   = move.get('size')
        _move_font   = move.get('rl_font', '')
        if _move_size:
            render_size = float(_move_size)
        _sampled    = orig.get('_sampled_color', '')
        _user_color = change.get('color', '') or _move_color
        _orig_color = orig.get('color', '#000000')
        if orig.get('ocr'):
            _use_color = _sampled or '#000000'
        elif _user_color and _user_color not in ('#000000', ''):
            _use_color = _user_color
        elif _orig_color and _orig_color not in ('#000000', ''):
            _use_color = _orig_color
        else:
            _use_color = '#000000'
        _use_font = _move_font or change.get('rl_font', orig.get('rl_font', 'Helvetica'))
        logger.debug(f"[TEXT_COLOR_MOVED] id={eid} ocr={orig.get('ocr')} "
                     f"move_color={_move_color!r} → using {_use_color!r}")
        _et_lines, _et_id = emit_text(
            float(move['x']), float(move['y']),
            change.get('text', orig['text']),
            render_size,
            _use_font,
            _use_color,
        )
        lines += _et_lines
        _emit_inject_ids[eid] = _et_id

    for eid, change in text_changed.items():
        if eid in deleted_ids or eid in moved_map:
            continue
        orig = orig_map.get(eid)
        if not orig or orig.get('type') != 'text':
            continue
        _use_font_tc = change.get('rl_font', orig.get('rl_font', 'Helvetica'))
        # Size priority: explicit user override from the panel → original Tf em-size → fallback.
        # change['size'] is only present when the user explicitly set it via the size control;
        # when absent the key is omitted from JSON so we fall back to orig font_size.
        _user_size  = change.get('size')
        render_size = float(_user_size) if _user_size else \
                      float(orig.get('font_size') or float(orig.get('size', 8)) * 1.35)
        # Colour priority:
        # 1. Explicit colour set by user in editor (if not default black)
        # 2. _sampled_color derived from background brightness (for OCR items)
        # 3. Original item colour
        # 4. Black fallback
        _sampled    = orig.get('_sampled_color', '')
        _user_color = change.get('color', '')
        _orig_color = orig.get('color', '#000000')
        if orig.get('ocr'):
            # OCR text: always use sampled foreground colour — it was
            # measured directly from the ink pixels in the original scan.
            # Ignore whatever colour the user had selected in the panel.
            _use_color = _sampled or '#000000'
            _color_src = 'sampled_ocr'
        elif _user_color and _user_color not in ('#000000', ''):
            _use_color = _user_color
            _color_src = 'user'
        elif _orig_color and _orig_color not in ('#000000', ''):
            _use_color = _orig_color
            _color_src = 'original'
        else:
            _use_color = '#000000'
            _color_src = 'default_black'
        logger.debug(f"[TEXT_COLOR] id={eid} ocr={orig.get('ocr')} "
                     f"sampled={_sampled!r} → using {_use_color!r} (src={_color_src})")
        # Fit the replacement into the space the original occupied.  Nothing
        # in a PDF reflows, so a longer word would otherwise draw straight over
        # its neighbour.  Only a MEASURED neighbour constrains it; where the
        # rest of the line is free the text is emitted exactly as before.
        _new_text = change.get('text', orig['text'])
        _limit_x  = _same_line_neighbour_x(orig, orig_map.values(),
                                           deleted_ids | set(moved_map))
        _fit_size, _fit_tz, _fit_over = (render_size, 100.0, False)
        if _limit_x is not None:
            _fit_size, _fit_tz, _fit_over = _fit_to_available(
                _new_text, render_size, _use_font_tc, float(orig['x']),
                _limit_x, orig.get('x2'))
            if _fit_size != render_size or _fit_tz != 100.0 or _fit_over:
                logger.info('[FIT] id=%s "%s" %.1fpt → %.1fpt @ %.0f%% '
                            '(x=%.1f limit=%.1f)%s', eid, _new_text[:24],
                            render_size, _fit_size, _fit_tz,
                            float(orig['x']), _limit_x,
                            ' STILL OVERFLOWS' if _fit_over else '')
                # Let the caller tell the user why the words look condensed —
                # or that they could not be made to fit at all.
                change['_fitted'] = {'size': round(_fit_size, 2),
                                     'tz': round(_fit_tz, 1),
                                     'orig_size': round(render_size, 2),
                                     'overflows': bool(_fit_over)}
        _et_lines, _et_id = emit_text(
            float(orig['x']), float(orig.get('baseline_y', orig['y'])),
            _new_text,
            _fit_size,
            change.get('rl_font', orig.get('rl_font', 'Helvetica')),
            _use_color,
            _fit_tz,
        )
        lines += _et_lines
        _emit_inject_ids[eid] = _et_id

    for item in markup_items:
        if not item.get('text'):
            continue
        # Size resolution for markup items:
        #   _vis_size is set on items pasted from native text.  Convert to em size using
        #   the cap-height ratio so the injected text matches the original's visual height.
        #   This is correct even for PDFs that use cm-operator scaling (where font_size
        #   would be inflated).  Manually-typed annotations have no _vis_size so they use
        #   font_size / size directly (user-specified values need no conversion).
        _mvis = float(item.get('_vis_size') or 0)
        if _mvis > 1.0:
            # _cap_ratio handles both bundled fonts (via font_assets) and base-14.
            _cap_h = _cap_ratio(item.get('rl_font', 'Helvetica'))
            _markup_size = round(_mvis / _cap_h, 3)
            logger.debug(f"[DBG-MARKUP] paste vis_size={_mvis:.3f} → markup_size={_markup_size:.3f} "
                         f"rl_font={item.get('rl_font','Helvetica')}")
        else:
            _markup_size = float(item.get('font_size') or item.get('size', 12))
        _mx, _my = display_to_raw(float(item['x']), float(item['y']))

        # If this markup item was previously baked into the PDF (has _bakedAt)
        # and has since been moved, white out the old baked position first.
        baked_at = item.get('bakedAt') or item.get('_bakedAt')
        if baked_at:
            _bx = float(baked_at.get('x', item['x']))
            _by = float(baked_at.get('y', item['y']))
            if abs(_bx - float(item['x'])) > 0.5 or abs(_by - float(item['y'])) > 0.5:
                _obx, _oby = display_to_raw(_bx, _by)
                # Estimate bbox from font size: width ~= len*size*0.6, height ~= size*1.4
                _est_w = len(item['text']) * _markup_size * 0.6
                _est_h = _markup_size * 1.4
                _pad   = _markup_size * 0.3
                lines.append(b'q')
                lines.append(b'1 1 1 rg 1 1 1 RG')
                lines.append(('%.3f %.3f %.3f %.3f re f' % (
                    _obx - _pad, _oby - _pad, _est_w + _pad*2, _est_h + _pad*2
                )).encode())
                lines.append(b'Q')

        if item.get('multiline') and item.get('lines'):
            # Multiline text box: emit each pre-wrapped line at successive Y positions.
            # x,y is the top-left of the box in display space (y is the top edge).
            # First baseline sits one em below the top; each successive line drops by lineH.
            # A row/area edit carries the pitch of the rows it replaced, so a
            # tight table-header cell does not come back double-spaced.
            try:
                _line_h = float(item.get('lineH') or 0) or _markup_size * 1.35
            except (TypeError, ValueError):
                _line_h = _markup_size * 1.35
            _rl_font = item.get('rl_font', 'Helvetica')
            _color   = item.get('color', '#000000')
            # First-baseline anchor.  A row-merge carries _capAnchor: the block must
            # sit where the original/scanned glyphs actually were, so anchor the top
            # line's baseline a cap-height below the true glyph top (item.x/y is only
            # the padded, user-adjustable box).  _anchorD{X,Y} are offsets from the
            # box origin, so they track any later move of the block.  Manually-typed
            # text boxes have no anchor and keep the box-top-minus-one-em convention.
            if item.get('_capAnchor'):
                _eff_top  = float(item['y']) + float(item.get('_anchorDY', 0.0))
                _eff_left = float(item['x']) + float(item.get('_anchorDX', 0.0))
                _first_baseline = _eff_top - _cap_ratio(_rl_font) * _markup_size
                _x_disp = _eff_left
            else:
                # top-left of box is (item['x'], item['y']) in display space.
                _first_baseline = float(item['y']) - _markup_size
                _x_disp = float(item['x'])
            # Alignment (row/area edits): 'right' hangs each line off the
            # original rows' right edge, 'center' centres it on their centre.
            # The anchor is an offset from the box's x, so it rides along with
            # a later move.  Measured with the embedded font's own widths, so
            # the edge lands exactly where the originals' did.
            _align = item.get('align')
            _align_x = None
            if _align in ('right', 'center'):
                try:
                    _align_x = float(item['x']) + float(item.get('_alignDX', 0.0))
                except (TypeError, ValueError):
                    _align_x = None
            for _li, _ln in enumerate(item['lines']):
                if not _ln.strip():
                    continue
                # Baseline of line i = first_baseline - i*lineH  (PDF y-up, so subtract)
                _line_display_y = _first_baseline - _li * _line_h
                _line_x = _x_disp
                if _align_x is not None:
                    _w = _font_assets.string_width(_rl_font, _ln.rstrip(), _markup_size)
                    _line_x = _align_x - (_w if _align == 'right' else _w / 2.0)
                _lx, _ly = display_to_raw(_line_x, _line_display_y)
                _el, _ = emit_text(_lx, _ly, _ln, _markup_size, _rl_font, _color)
                lines += _el
        else:
            _el, _ = emit_text(
                _mx, _my,
                item['text'],
                _markup_size,
                item.get('rl_font', 'Helvetica'),
                item.get('color', '#000000'),
            )
            lines += _el

    # ── PDF image moves and deletes ─────────────────────────────────────────────
    # pdf_image items are detected embedded XObjects.  The user can:
    #   delete → cover the image area with a background-coloured rect
    #   move   → cover original position + re-embed the cropped PNG at new position
    pdf_image_edits = edits.get('pdf_image_edits', [])
    # Collect the ORIGINAL XObject names that were acted on.
    # These are the names already present in the stream from previous applies or
    # the original PDF. We exclude them from future detection because their Do
    # call stays in the stream (we cover them visually) but we don't want them
    # re-detected as interactive handles.
    # IMPORTANT: we only exclude names that came FROM the PDF (the xobj_name the
    # JS sent back). We do NOT exclude the new names we create here (PdfImgMvN)
    # because those are the live copies after a move and must remain detectable.
    _excluded_xobj_names = set()
    for _pe in pdf_image_edits:
        _xn = _pe.get('xobj_name', '')
        if _xn:
            _excluded_xobj_names.add(_xn)

    # img_counter is a global-per-page counter so moved XObject names are unique
    # across every apply pass. This prevents the new copy from sharing a name with
    # a previously excluded XObject.
    _mv_counter = img_counter

    for idx_pe, pe in enumerate(pdf_image_edits):
        action = pe.get('action')          # 'delete' or 'move'
        # These coords are in raw content stream space (same space extract_pdf_images uses)
        ix = float(pe.get('orig_x', 0))
        iy = float(pe.get('orig_y', 0))
        iw = float(pe.get('orig_w', 0))
        ih = float(pe.get('orig_h', 0))

        # Sample background colour from strips OUTSIDE the image bbox.
        # Sampling inside the image gives the image's own dominant colour (e.g. dark
        # blue logo), not the page background.  Instead we sample thin pixel strips
        # just beyond each edge of the image and take the majority colour.
        _strip_px = 20   # pixel width of each border strip
        _sc       = _bg_scale
        _img_W, _img_H = (_bg_img.size if _bg_img else (1, 1))

        # Convert image bbox to pixel coords (y-flipped: pdf y=0 at bottom)
        _px1 = int(ix * _sc)
        _px2 = int((ix + iw) * _sc)
        _py2 = int((page_h - iy) * _sc)          # top of image in pixel space
        _py1 = int((page_h - (iy + ih)) * _sc)   # bottom of image in pixel space
        _px1, _px2 = min(_px1, _px2), max(_px1, _px2)
        _py1, _py2 = min(_py1, _py2), max(_py1, _py2)

        cr, cg, cb = 1.0, 1.0, 1.0   # white fallback
        if _bg_img is not None:
            _strips = []
            # Top strip (above image)
            if _py1 - _strip_px >= 0:
                _strips.append(_bg_img.crop((max(0,_px1), max(0,_py1-_strip_px), min(_img_W,_px2), _py1)))
            # Bottom strip (below image)
            if _py2 + _strip_px <= _img_H:
                _strips.append(_bg_img.crop((max(0,_px1), _py2, min(_img_W,_px2), min(_img_H,_py2+_strip_px))))
            # Left strip
            if _px1 - _strip_px >= 0:
                _strips.append(_bg_img.crop((max(0,_px1-_strip_px), max(0,_py1), _px1, min(_img_H,_py2))))
            # Right strip
            if _px2 + _strip_px <= _img_W:
                _strips.append(_bg_img.crop((_px2, max(0,_py1), min(_img_W,_px2+_strip_px), min(_img_H,_py2))))

            _strip_arrs = [
                np.array(s.convert('RGB')).reshape(-1, 3)
                for s in _strips
                if np.array(s).size > 0
            ]
            if _strip_arrs:                       # guard: vstack([]) raises
                _pixels = np.vstack(_strip_arrs)
                if _pixels.size > 0:
                    _brightness = np.dot(_pixels, [0.299, 0.587, 0.114])
                    _counts, _bins = np.histogram(_brightness, bins=32, range=(0, 256))
                    _bg_bin = np.argmax(_counts)
                    _mask   = (_brightness >= _bins[_bg_bin]) & (_brightness <= _bins[_bg_bin + 1])
                    _bg_raw = np.mean(_pixels[_mask], axis=0) if np.any(_mask) else np.mean(_pixels, axis=0)
                    cr, cg, cb = float(_bg_raw[0]/255), float(_bg_raw[1]/255), float(_bg_raw[2]/255)

        # Cover the original position with the sampled page background colour.
        # Use a generous pad so the cover fully masks anti-aliased edges.
        pad = 3.0
        lines.append(('%.3f %.3f %.3f rg %.3f %.3f %.3f RG' % (cr,cg,cb,cr,cg,cb)).encode())
        lines.append(('%.3f %.3f %.3f %.3f re f' % (ix - pad, iy - pad, iw + pad*2, ih + pad*2)).encode())
        logger.debug(f'[PDF_IMG_COVER] covering {action} at x={ix:.1f} y={iy:.1f} w={iw:.1f} h={ih:.1f} '
                     f'bg=({cr:.2f},{cg:.2f},{cb:.2f})')

        if action == 'move':
            # Re-embed the image at its new position using the dataUrl stored on the edit
            data_url = pe.get('dataUrl', '')
            if data_url and ',' in data_url:
                nx = float(pe.get('new_x', ix))
                ny = float(pe.get('new_y', iy))
                nw = float(pe.get('new_w', iw))
                nh = float(pe.get('new_h', ih))
                b64data   = data_url.split(',', 1)[1]
                img_bytes = base64.b64decode(b64data)
                pil_img   = PILImage.open(io.BytesIO(img_bytes)).convert('RGB')
                jpeg_buf  = io.BytesIO()
                pil_img.save(jpeg_buf, format='JPEG', quality=92)
                jpeg_bytes = jpeg_buf.getvalue()

                # Each moved image gets a globally unique XObject name (unique per page
                # across all apply passes) so it can never collide with an excluded name.
                xobj_name = f'PdfImgMv{_mv_counter}'
                _mv_counter += 1
                img_stream = pypdf.generic.DecodedStreamObject()
                img_stream._data = jpeg_bytes
                img_stream.update({
                    pypdf.generic.NameObject('/Type'):    pypdf.generic.NameObject('/XObject'),
                    pypdf.generic.NameObject('/Subtype'): pypdf.generic.NameObject('/Image'),
                    pypdf.generic.NameObject('/Width'):   pypdf.generic.NumberObject(pil_img.width),
                    pypdf.generic.NameObject('/Height'):  pypdf.generic.NumberObject(pil_img.height),
                    pypdf.generic.NameObject('/ColorSpace'): pypdf.generic.NameObject('/DeviceRGB'),
                    pypdf.generic.NameObject('/BitsPerComponent'): pypdf.generic.NumberObject(8),
                    pypdf.generic.NameObject('/Filter'): pypdf.generic.NameObject('/DCTDecode'),
                })
                img_ref = writer._add_object(img_stream)

                page_res = writer.pages[0]
                if '/Resources' not in page_res:
                    page_res[pypdf.generic.NameObject('/Resources')] = pypdf.generic.DictionaryObject()
                res = page_res['/Resources'].get_object()
                if '/XObject' not in res:
                    res[pypdf.generic.NameObject('/XObject')] = pypdf.generic.DictionaryObject()
                xobj_dict = res['/XObject'].get_object()
                xobj_dict[pypdf.generic.NameObject('/' + xobj_name)] = img_ref

                lines.append(b'q')
                lines.append(('%.3f 0 0 %.3f %.3f %.3f cm' % (nw, nh, nx, ny)).encode())
                lines.append(('/%s Do' % xobj_name).encode())
                lines.append(b'Q')
                logger.debug(f"[PDF_IMG_MOVE] idx={idx_pe} moved image to x={nx:.1f} y={ny:.1f} w={nw:.1f} h={nh:.1f}")

    lines.append(b'Q')  # close outer q

    # ── Watermark ─────────────────────────────────────────────────────────────
    wm = edits.get('watermark')
    if wm and wm.get('text', '').strip():
        _wm_text    = wm['text'].strip()
        _wm_size    = float(wm.get('size', 60))
        _wm_opacity = max(0.05, min(0.95, float(wm.get('opacity', 0.25))))
        _wm_angle   = float(wm.get('angleDeg', 45))
        _wm_layout  = wm.get('layout', 'center')
        _wm_hex     = wm.get('color', '#808080')

        # Use the actual watermark colour (no white-blending hack).
        # Real opacity is applied via an ExtGState so the watermark works on
        # any background (coloured fills, images, etc.) rather than relying on
        # the page being white.
        _wr, _wg, _wb = _hex_to_rgb(_wm_hex)

        # Ensure Helvetica-Bold font and a translucent ExtGState are on the page
        _wm_fname = _ensure_font(writer, page, 'Helvetica-Bold')
        _wm_gs    = _ensure_extgstate(writer, page, _wm_opacity)

        # Wrap watermark in its own q/Q with the same CTM inversion used for
        # markup. Scanned PDFs often have a persistent net CTM in their content
        # stream (e.g. a y-flip for image placement). The markup outer q/Q
        # corrects for this, but the watermark was previously appended outside
        # that block — causing it to run under the raw stream CTM and appear
        # rotated/mirrored. The inversion brings us into raw PDF space where
        # the cropbox centre coordinates and angle calculation are correct.
        lines.append(b'q')
        if ctm_op:
            lines.append(ctm_op)

        # Negate angle: canvas uses y-down clockwise; PDF uses y-up counter-clockwise.
        # Also offset by /Rotate so the watermark appears at the user-set angle
        # in display space regardless of how the page is physically stored.
        _angle_rad  = math.radians(-_wm_angle + _ae_rotate)
        _cos, _sin  = math.cos(_angle_rad), math.sin(_angle_rad)
        # Helvetica-Bold AFM character widths (1/1000 units per em).
        # Used instead of a fixed 0.55 factor so that uppercase-heavy watermark
        # text (DRAFT, CONFIDENTIAL, etc.) doesn't produce a spurious vertical
        # shift at non-zero angles — the canvas preview uses textAlign='center'
        # with actual glyph metrics, so we must too.
        _HB_W = {
            ' ':278,'!':333,'"':474,'#':556,'$':556,'%':889,'&':722,"'":278,
            '(':333,')':333,'*':389,'+':584,',':278,'-':333,'.':278,'/':278,
            '0':556,'1':556,'2':556,'3':556,'4':556,'5':556,'6':556,'7':556,
            '8':556,'9':556,':':333,';':333,'<':584,'=':584,'>':584,'?':611,
            '@':975,
            'A':722,'B':722,'C':667,'D':778,'E':667,'F':611,'G':722,'H':778,
            'I':278,'J':500,'K':722,'L':611,'M':833,'N':722,'O':778,'P':667,
            'Q':778,'R':722,'S':667,'T':611,'U':722,'V':667,'W':944,'X':667,
            'Y':611,'Z':611,
            'a':556,'b':611,'c':556,'d':611,'e':556,'f':333,'g':611,'h':611,
            'i':278,'j':278,'k':556,'l':278,'m':889,'n':611,'o':611,'p':611,
            'q':611,'r':389,'s':556,'t':333,'u':611,'v':556,'w':778,'x':556,
            'y':556,'z':500,
        }
        _text_w = sum(_HB_W.get(c, 556) for c in _wm_text) / 1000.0 * _wm_size
        _half_w = _text_w / 2
        _half_h = _wm_size * 0.35  # approximate cap height / 2

        def _emit_wm_at(cx, cy):
            # Combine translate-to-center + rotate into one CTM
            # Local origin is at (cx, cy); text is offset by -halfW in local x
            tx = cx - _half_w * _cos + _half_h * _sin
            ty = cy - _half_w * _sin - _half_h * _cos

            out = [
                b'q',
                # Apply translucency via ExtGState
                ('%s gs' % _wm_gs).encode(),
                ('%.4f %.4f %.4f %.4f %.3f %.3f cm' % (_cos, _sin, -_sin, _cos, tx, ty)).encode(),
                b'BT',
                ('/%s %.3f Tf' % (_wm_fname, _wm_size)).encode(),
                ('%.4f %.4f %.4f rg' % (_wr, _wg, _wb)).encode(),
                '0 0 Td'.encode(),  # Cleaned up to be a direct byte literal
                ('(%s) Tj' % _safe_pdf_str(_wm_text)).encode(),
                b'ET',
                b'Q'
            ]
            return out

        # Page centre in raw PDF content-stream space.
        # Use the CropBox (which pdfium renders) rather than the raw MediaBox
        # half-dimensions, so the watermark aligns with what the canvas preview
        # shows even when mb.bottom != 0 or a CropBox smaller than MediaBox exists.
        _wm_cb  = _ae_page.cropbox   # falls back to mediabox if no cropbox
        _pcx    = (float(_wm_cb.left) + float(_wm_cb.right))  / 2
        _pcy    = (float(_wm_cb.bottom) + float(_wm_cb.top))  / 2

        if _wm_layout == 'tiled':
            _spacing = max(len(_wm_text) * _wm_size * 0.6 * 1.8, _wm_size * 4)
            _steps = int(math.ceil(max(_ae_raw_W, _ae_raw_H) / _spacing)) + 2
            for _ti in range(-_steps, _steps + 1):
                for _tj in range(-_steps, _steps + 1):
                    lines.extend(_emit_wm_at(
                        _pcx + _ti * _spacing,
                        _pcy + _tj * _spacing
                    ))
        else:
            lines.extend(_emit_wm_at(_pcx, _pcy))

        lines.append(b'Q')  # close watermark CTM wrapper

    # Append to existing content stream. The watermark uses an ExtGState with
    # /ca + /CA set to the configured opacity, so it draws translucently over
    # whatever is underneath — text, colour fills, images all remain visible.
    new_ops  = b'\n'.join(lines) + b'\n'
    contents = page.get('/Contents')
    if contents is None:
        existing = b''
    else:
        obj = contents.get_object() if hasattr(contents, 'get_object') else contents
        if isinstance(obj, ArrayObject):
            existing = b'\n'.join(
                o.get_object().get_data() for o in obj
                if hasattr(o.get_object(), 'get_data')
            )
        elif hasattr(obj, 'get_data'):
            existing = obj.get_data()
        else:
            existing = b''

    # Remove previous emit_text injections for items being re-applied this pass.
    # Scans the stream for all '% pw_inj' tagged blocks and removes those whose
    # Tm position matches a target word position — works across server sessions
    # because the marker is embedded in the stream, not stored in memory.
    if _target_xys:
        existing = _scrub_injections_at_positions(existing, _target_xys)

    # For native text PDFs with redactions: remove text operators from the existing
    # content stream that fall under any redaction box.  Scanned/OCR PDFs don't need
    # this — their text was injected by us and simply won't be re-injected for
    # redacted areas.  The black rectangle drawn in new_ops provides the visual cover;
    # this scrub ensures the underlying text is gone from the stream too.
    if _raw_redact_rects:
        existing = _scrub_text_under_redactions(
            existing, _raw_redact_rects, ctm=inject_ctm
        )

    # Native row-merge: scrub the original vector text under each merged-row
    # footprint so it's gone from the stream (the merged run is injected fresh in
    # new_ops). Both old and new are native here, so we can't tell them apart on
    # re-extraction — removing the originals is what keeps it clean and editable.
    if _merge_scrub_rects:
        existing = _scrub_text_under_redactions(
            existing, _merge_scrub_rects, ctm=inject_ctm
        )

    new_stream = DecodedStreamObject()
    new_stream.set_data(existing + b'\n' + new_ops)
    page[NameObject('/Contents')] = writer._add_object(new_stream)

    # ── Crop box ─────────────────────────────────────────────────────────────
    # cropItem coords are in JS display space (y-up, display dims).
    # Convert both corners to raw content-stream space, then set /CropBox.
    crop = edits.get('crop')
    if crop:
        from pypdf.generic import FloatObject
        cx1_d = float(crop.get('x',  0))
        cy1_d = float(crop.get('y',  0))
        cx2_d = float(crop.get('x2', _ae_raw_W))
        cy2_d = float(crop.get('y2', _ae_raw_H))
        rx1, ry1 = display_to_raw(cx1_d, cy1_d)
        rx2, ry2 = display_to_raw(cx2_d, cy2_d)
        llx = min(rx1, rx2)
        lly = min(ry1, ry2)
        urx = max(rx1, rx2)
        ury = max(ry1, ry2)
        writer.pages[0][NameObject('/CropBox')] = ArrayObject([
            FloatObject(llx), FloatObject(lly), FloatObject(urx), FloatObject(ury)
        ])
        logger.info(f"[apply_edits] CropBox set: [{llx:.1f},{lly:.1f},{urx:.1f},{ury:.1f}]")

    out = io.BytesIO()
    writer.write(out)
    result_bytes = out.getvalue()

    # True redaction: after scrubbing native vector text (above) and drawing the
    # black cover rectangles (in new_ops), also destroy the pixels under each
    # redaction box inside any image XObject — otherwise a scanned page or photo
    # remains fully recoverable beneath the cover.  Only runs when redactions
    # exist; never lets a failure abort the user's edit.
    if _raw_redact_rects:
        try:
            result_bytes = _burn_redactions_into_images(result_bytes, _raw_redact_rects)
        except Exception as _burn_exc:
            logger.error(f"[REDACT] image burn pass failed: {_burn_exc}")

    return result_bytes, _emit_inject_ids


def _hex_to_rgb(hex_color):
    """Convert #rrggbb to (r, g, b) floats 0–1."""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join(c*2 for c in hex_color)
    try:
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
        return r, g, b
    except Exception:
        return 0.0, 0.0, 0.0


# ── Flask route registration ──────────────────────────────────────────────────


def register_editor_routes(app, sessions, inject_token=None):
    """Register the routes this module still serves itself.

    The Editor panel was merged into the viewer in v3.1 and its /api/editor/*
    routes went with it; what remains here is the text/graphics extraction and
    apply_edits machinery the viewer's page tools call, plus the start-up
    guard below and Layout's header/footer stamp.
    """

    # ── Lazy heavy deps: make sure they are loaded before a route needs them ──
    # pdfium and the OCR stack load in the background once the window is up
    # (start_heavy_init_background).  A request that reaches this module's
    # extraction / apply_edits before that finishes found `pdfium` still None.
    # The guard used to cover only /api/editor/* — the editor's own routes,
    # which are gone — while the VIEWER's editing routes, which call straight
    # into this module, had none: using Edit text in the first moments after
    # launch failed, and the mixed-page check quietly answered "no".
    _NEEDS_HEAVY = ('/api/viewer/page_elements', '/api/viewer/page_text',
                    '/api/viewer/edit_text', '/api/viewer/edit_block',
                    '/api/viewer/add_text', '/api/viewer/redact',
                    '/api/viewer/watermark', '/api/viewer/crop',
                    '/api/header_footer')

    @app.before_request
    def _ensure_heavy_ready():
        if not _INIT_DONE and request.path.startswith(_NEEDS_HEAVY):
            _heavy_init()

    @app.route('/api/header_footer', methods=['POST'])
    def header_footer_apply():
        """Stamp header/footer text onto every page in the session."""
        data       = request.json
        session_id = data.get('session_id')
        session    = sessions.get(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        hdr    = data.get('header', {})
        ftr    = data.get('footer', {})
        font   = data.get('font', 'Helvetica')
        size   = max(6, min(24, int(data.get('size', 10))))
        color  = data.get('color', '#000000')
        margin = max(4, min(80, int(data.get('margin', 18))))
        total  = int(data.get('total_pages', len(session['pages'])))
        filenames = data.get('filenames', [])

        # Parse hex color → 0-1 floats
        def _hex_to_rgb(h):
            h = h.lstrip('#')
            r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
            return r/255, g/255, b/255
        cr, cg, cb = _hex_to_rgb(color)

        def _resolve(text, page_num, total_pages, filename):
            import datetime
            return (text
                .replace('{page}',     str(page_num))
                .replace('{total}',    str(total_pages))
                .replace('{date}',     datetime.date.today().strftime('%d/%m/%Y'))
                .replace('{filename}', filename))

        def _pdf_str(s):
            """Escape a string for PDF literal string syntax."""
            return s.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')

        def _hf_stream(pw, ph, llx, lly, page_num, total_pages, filename):
            """Build a content stream snippet that draws header/footer."""
            lines = [
                'BT',
                f'/{font} {size} Tf',
                f'{cr:.4f} {cg:.4f} {cb:.4f} rg'
            ]

            def _text_width_pts(text):
                # Measure actual advance width via PIL against a BUNDLED
                # metric-compatible face, so header/footer centring/right-align is
                # correct on every OS.  (The old code loaded Windows font files
                # like arial.ttf, which are absent on macOS/Linux, silently forcing
                # the crude fallback there.)  PIL TrueType size in px == points at
                # 72 DPI, which matches PDF units.  getlength() gives the pen
                # advance width — the right measure for positioning, unlike
                # getbbox() which returns only the inked extent.
                try:
                    from PIL import ImageFont as _IF
                    _pth = _font_assets.measurement_ttf_path(font)
                    if not _pth:
                        raise RuntimeError('no bundled measurement face')
                    _pf = _IF.truetype(_pth, size)
                    return float(_pf.getlength(text))
                except Exception:
                    return len(text) * size * 0.45

            # Each item uses Tm (absolute text matrix) so positions are always
            # relative to the page coordinate origin (llx, lly), unaffected by
            # any preceding Td or text-matrix state.
            def _draw_row_abs(positions, y):
                left   = _resolve(positions.get('left',   ''), page_num, total_pages, filename)
                centre = _resolve(positions.get('centre', ''), page_num, total_pages, filename)
                right  = _resolve(positions.get('right',  ''), page_num, total_pages, filename)
                row = []
                if left.strip():
                    lx = llx + margin
                    row.append(f'1 0 0 1 {lx:.1f} {y:.1f} Tm ({_pdf_str(left)}) Tj')
                if centre.strip():
                    cw = _text_width_pts(centre)
                    cx = llx + max(0.0, (pw - cw) / 2.0)
                    row.append(f'1 0 0 1 {cx:.1f} {y:.1f} Tm ({_pdf_str(centre)}) Tj')
                if right.strip():
                    rw = _text_width_pts(right)
                    rx = llx + max(0.0, pw - margin - rw)
                    row.append(f'1 0 0 1 {rx:.1f} {y:.1f} Tm ({_pdf_str(right)}) Tj')
                return row

            # y coordinates are absolute in page space: origin at (llx, lly).
            h_y = lly + ph - margin - size
            f_y = lly + margin

            lines += _draw_row_abs(hdr, h_y)
            lines += _draw_row_abs(ftr, f_y)
            lines.append('ET')
            return '\n'.join(lines).encode('latin-1', errors='replace')

        def _inject_hf(pdf_bytes, page_num, total_pages, filename):
            """Stamp header/footer onto the first page of a single-page PDF."""
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            writer = pypdf.PdfWriter()
            writer.add_page(reader.pages[0])

            pg = writer.pages[0]
            pw  = float(pg.mediabox.width)
            ph  = float(pg.mediabox.height)
            try:
                llx = float(pg.mediabox.lower_left[0])
                lly = float(pg.mediabox.lower_left[1])
            except Exception:
                llx, lly = 0.0, 0.0

            snippet = _hf_stream(pw, ph, llx, lly, page_num, total_pages, filename)

            # Ensure font resource exists on the page
            res = pg.get('/Resources')
            if res is None:
                pg[pypdf.generic.NameObject('/Resources')] = pypdf.generic.DictionaryObject()
                res = pg['/Resources']
            else:
                res = res.get_object()

            font_dict_obj = pypdf.generic.DictionaryObject({
                pypdf.generic.NameObject('/Type'):     pypdf.generic.NameObject('/Font'),
                pypdf.generic.NameObject('/Subtype'):  pypdf.generic.NameObject('/Type1'),
                pypdf.generic.NameObject('/BaseFont'): pypdf.generic.NameObject(f'/{font}'),
            })
            font_key = pypdf.generic.NameObject(f'/{font}')
            font_res = res.get('/Font')
            if font_res is None:
                res[pypdf.generic.NameObject('/Font')] = pypdf.generic.DictionaryObject(
                    {font_key: writer._add_object(font_dict_obj)}
                )
            else:
                font_res = font_res.get_object()
                if font_key not in font_res:
                    font_res[font_key] = writer._add_object(font_dict_obj)

            # Build the HF stream object
            hf_stm = pypdf.generic.DecodedStreamObject()
            hf_stm.set_data(snippet)
            hf_ref = writer._add_object(hf_stm)

            existing = pg.get('/Contents')
            if existing is None:
                pg[pypdf.generic.NameObject('/Contents')] = hf_ref
            else:
                # Wrap existing content in q/Q so any CTM transforms the page
                # applied are fully restored before our HF stream runs.
                # This guarantees our absolute Tm coordinates land in the true
                # page coordinate space, regardless of what the page content did.
                ec = existing.get_object()
                if isinstance(ec, pypdf.generic.ArrayObject):
                    raw_parts = []
                    for item in ec:
                        try:
                            raw_parts.append(item.get_object().get_data())
                        except Exception:
                            pass
                    existing_data = b'\n'.join(raw_parts)
                else:
                    try:
                        existing_data = ec.get_data()
                    except Exception:
                        existing_data = b''

                wrapped_stm = pypdf.generic.DecodedStreamObject()
                wrapped_stm.set_data(b'q\n' + existing_data + b'\nQ')
                wrapped_ref = writer._add_object(wrapped_stm)

                pg[pypdf.generic.NameObject('/Contents')] = pypdf.generic.ArrayObject(
                    [wrapped_ref, hf_ref]
                )

            out = io.BytesIO()
            writer.write(out)
            return out.getvalue()

        updated = []
        for idx, page_rec in enumerate(session.get('pages', [])):
            pdf_bytes = page_rec.get('pdf_bytes', b'')
            if not pdf_bytes:
                continue
            fname = filenames[idx] if idx < len(filenames) else (page_rec.get('filename') or '')
            try:
                new_pdf = _inject_hf(pdf_bytes, idx + 1, total, fname)
                page_rec['pdf_bytes'] = new_pdf
                _recovery.mark_dirty(session, page_rec['id'])
                with _NATIVE_LOCK:
                    _doc = pdfium.PdfDocument(new_pdf)
                    try:
                        _sc     = 200 / _doc[0].get_width()
                        _bmp    = _doc[0].render(scale=_sc)
                        _hf_img = _bmp.to_pil()
                    finally:
                        _doc.close()
                _tb = io.BytesIO()
                _hf_img.save(_tb, format='PNG')
                _tb.seek(0)
                page_rec['thumbnail'] = base64.b64encode(_tb.read()).decode('utf-8')
                updated.append({'id': page_rec['id'], 'thumbnail': page_rec['thumbnail']})
            except Exception as exc:
                import traceback; traceback.print_exc()
                logger.warning(f'[header_footer] page {page_rec.get("id")} failed: {exc}')

        return jsonify({'success': True, 'updated': updated})

    # ── Form field routes ────────────────────────────────────────────────────────
