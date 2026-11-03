"""Bundled-font registry and PDF embedding (Option B — font matching).

This module widens the editor's font gamut beyond the PDF base-14 set by
bundling metric-compatible open fonts and embedding them into output PDFs:

    Calibri      -> Carlito              (SIL OFL)
    Arial        -> Liberation Sans      (SIL OFL)  [metric-identical to Arial]
    Times        -> Liberation Serif     (SIL OFL)  [metric-identical to Times NR]
    Courier      -> Liberation Mono      (SIL OFL)  [metric-identical to Courier NW]

Design notes
------------
* Fonts are embedded as **simple /TrueType fonts with /WinAnsiEncoding** so the
  existing ``emit_text`` path (which writes ``(latin1 text) Tj``) works unchanged.
  The full face is embedded (no subsetting) but **de-duplicated to one copy per
  face per output document** via a cache attached to the pypdf writer, so the
  size cost is bounded by the number of distinct faces actually used.
* The descriptor is marked **Nonsymbolic** (flag bit 6) — mandatory for WinAnsi
  simple fonts, otherwise viewers ignore /Encoding and use the font's own cmap.
* Metrics (/Widths, FontBBox, ascent/descent/capHeight/italicAngle/stemV) are
  read with reportlab's TTFontFile parser, so there is no new dependency.

The base-14 logical names ('Helvetica', 'Times-Roman', 'Courier', ...) are
deliberately NOT in this registry — they keep using the existing base-14 path
(no embedding, no size growth) for back-compat.
"""

import os
import logging

from pypdf.generic import (
    DictionaryObject, ArrayObject, NameObject, NumberObject,
    DecodedStreamObject, TextStringObject,
)

logger = logging.getLogger(__name__)

# Private /Font dict key stamped on every face we embed, so a later pass can
# tell our embedding apart from a same-named font the document already had.
_PW_FACE_KEY = '/PatchworkFace'

# ── Font directory resolution (dev + cx_Freeze frozen build) ──────────────────
# dev: this file is <root>/src/backend/platform_io/font_assets.py — climb to <root>.
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def _font_dir():
    """Directory holding the bundled .ttf files (assets/fonts)."""
    import sys
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = _APP_ROOT
    return os.path.join(base, 'assets', 'fonts')


# ── Registry ──────────────────────────────────────────────────────────────────
# logical rl_font key -> record describing the bundled face.
#   file       : ttf filename under assets/fonts
#   css_family : CSS font-family the @font-face declares (for the preview)
#   css_weight : 'normal' | 'bold'
#   css_style  : 'normal' | 'italic'
class _Face:
    __slots__ = ('key', 'file', 'css_family', 'css_weight', 'css_style')

    def __init__(self, key, file, css_family, css_weight, css_style):
        self.key = key
        self.file = file
        self.css_family = css_family
        self.css_weight = css_weight
        self.css_style = css_style

    @property
    def path(self):
        return os.path.join(_font_dir(), self.file)


REGISTRY = {
    # Calibri (Carlito)
    'Calibri':            _Face('Calibri',            'Carlito-Regular.ttf',          'PWCalibri', 'normal', 'normal'),
    'Calibri-Bold':       _Face('Calibri-Bold',       'Carlito-Bold.ttf',             'PWCalibri', 'bold',   'normal'),
    'Calibri-Italic':     _Face('Calibri-Italic',     'Carlito-Italic.ttf',           'PWCalibri', 'normal', 'italic'),
    'Calibri-BoldItalic': _Face('Calibri-BoldItalic', 'Carlito-BoldItalic.ttf',       'PWCalibri', 'bold',   'italic'),
    # Arial (Liberation Sans) — metric-identical to Arial
    'Arial':              _Face('Arial',              'LiberationSans-Regular.ttf',    'PWArial', 'normal', 'normal'),
    'Arial-Bold':         _Face('Arial-Bold',         'LiberationSans-Bold.ttf',       'PWArial', 'bold',   'normal'),
    'Arial-Italic':       _Face('Arial-Italic',       'LiberationSans-Italic.ttf',     'PWArial', 'normal', 'italic'),
    'Arial-BoldItalic':   _Face('Arial-BoldItalic',   'LiberationSans-BoldItalic.ttf', 'PWArial', 'bold',   'italic'),
    # Times New Roman (Liberation Serif)
    'TimesNR':            _Face('TimesNR',            'LiberationSerif-Regular.ttf',    'PWTimes', 'normal', 'normal'),
    'TimesNR-Bold':       _Face('TimesNR-Bold',       'LiberationSerif-Bold.ttf',       'PWTimes', 'bold',   'normal'),
    'TimesNR-Italic':     _Face('TimesNR-Italic',     'LiberationSerif-Italic.ttf',     'PWTimes', 'normal', 'italic'),
    'TimesNR-BoldItalic': _Face('TimesNR-BoldItalic', 'LiberationSerif-BoldItalic.ttf', 'PWTimes', 'bold',   'italic'),
    # Courier New (Liberation Mono)
    'CourierNew':         _Face('CourierNew',         'LiberationMono-Regular.ttf',     'PWCourier', 'normal', 'normal'),
    'CourierNew-Bold':    _Face('CourierNew-Bold',    'LiberationMono-Bold.ttf',        'PWCourier', 'bold',   'normal'),
    'CourierNew-Italic':  _Face('CourierNew-Italic',  'LiberationMono-Italic.ttf',      'PWCourier', 'normal', 'italic'),
    'CourierNew-BoldItalic': _Face('CourierNew-BoldItalic', 'LiberationMono-BoldItalic.ttf', 'PWCourier', 'bold', 'italic'),
}


def resolve(rl_font):
    """Return the _Face for a logical font key, or None if it is not a bundled
    font (base-14 names return None → caller uses the base-14 path)."""
    if not rl_font:
        return None
    return REGISTRY.get(rl_font)


# Base-14 logical names → bundled, metric-compatible face file.  Used for
# cross-platform text-width measurement (e.g. header/footer centring): these
# faces ship with the app, so measurement is identical on every OS instead of
# depending on a platform font like arial.ttf that is absent on macOS/Linux.
_BASE14_TO_FILE = {
    'Helvetica':             'LiberationSans-Regular.ttf',
    'Helvetica-Bold':        'LiberationSans-Bold.ttf',
    'Helvetica-Oblique':     'LiberationSans-Italic.ttf',
    'Helvetica-BoldOblique': 'LiberationSans-BoldItalic.ttf',
    'Times-Roman':           'LiberationSerif-Regular.ttf',
    'Times-Bold':            'LiberationSerif-Bold.ttf',
    'Times-Italic':          'LiberationSerif-Italic.ttf',
    'Times-BoldItalic':      'LiberationSerif-BoldItalic.ttf',
    'Courier':               'LiberationMono-Regular.ttf',
    'Courier-Bold':          'LiberationMono-Bold.ttf',
    'Courier-Oblique':       'LiberationMono-Italic.ttf',
    'Courier-BoldOblique':   'LiberationMono-BoldItalic.ttf',
}


def measurement_ttf_path(font_name):
    """Absolute path to a bundled TTF whose metrics match *font_name* (a base-14
    or a bundled logical name), for cross-platform text-width measurement.
    Returns None when nothing suitable is bundled (caller falls back to a
    width heuristic)."""
    face = resolve(font_name)
    if face is not None:
        return face.path if os.path.isfile(face.path) else None
    fn = _BASE14_TO_FILE.get(font_name)
    if not fn:
        return None
    p = os.path.join(_font_dir(), fn)
    return p if os.path.isfile(p) else None


# ── Cap-height ratios (capHeight / unitsPerEm) ────────────────────────────────
# Used by both the cap-height-correct sizing on apply and the live preview.
# Computed once per face from the TTF and cached.
_CAP_CACHE = {}


def cap_height_ratio(rl_font):
    face = resolve(rl_font)
    if face is None:
        return None
    if face.key in _CAP_CACHE:
        return _CAP_CACHE[face.key]
    try:
        from reportlab.pdfbase.ttfonts import TTFontFile
        ttf = TTFontFile(face.path, charInfo=1, validate=0)
        ratio = float(ttf.capHeight) / 1000.0   # reportlab scales metrics to /1000
    except Exception as exc:
        logger.warning(f"[FONT] cap-height read failed for {face.key}: {exc}")
        ratio = 0.66
    _CAP_CACHE[face.key] = ratio
    return ratio


# ── Glyph ink extents (for OCR box→em conversion) ─────────────────────────────
# A word's OCR bounding box spans its actual ink, so converting box height to an
# em size needs the ink extents of the characters present — cap-only words
# ("2026") and descender words ("Prepayment") have very different box heights at
# the same em size.  Extents are measured once per (face, char) by rasterising
# the glyph with Pillow's FreeType binding (already a dependency) and cached.
_INK_CACHE        = {}   # face.key -> {char: (top_em, bot_em) | None}
_INK_FONT_CACHE   = {}   # face.key -> (ImageFont, ascent_px)
_INK_MEASURE_SIZE = 1000


def char_ink_extents(rl_font, ch):
    """Ink extents of glyph *ch* in the bundled face for ``rl_font``:
    (top, bottom) in em units relative to the baseline.  E.g. Carlito 'H' →
    (+0.642, 0.0), 'p' → (+0.486, -0.162), and a floating '-' → (+0.319, +0.246).
    Returns None for non-bundled faces, whitespace, or unmeasurable glyphs."""
    face = resolve(rl_font)
    if face is None or not ch or ch.isspace():
        return None
    per_face = _INK_CACHE.setdefault(face.key, {})
    if ch in per_face:
        return per_face[ch]
    ext = None
    try:
        from PIL import ImageFont
        cached = _INK_FONT_CACHE.get(face.key)
        if cached is None:
            f = ImageFont.truetype(face.path, size=_INK_MEASURE_SIZE)
            cached = (f, f.getmetrics()[0])
            _INK_FONT_CACHE[face.key] = cached
        f, asc = cached
        # getmask2 gives the glyph bitmap plus its offset in the layout box;
        # the mask's own bbox is needed on top because the bitmap can be padded
        # down to the baseline (e.g. '-').  getbbox(anchor='ls') is NOT usable
        # here: it clamps the box to include the baseline origin, hiding the
        # true bottom of floating glyphs like '-' and "'".
        mask, (_dx, dy) = f.getmask2(ch)
        bb = mask.getbbox() if (mask.size[0] and mask.size[1]) else None
        if bb is not None:
            _x0, y0, _x1, y1 = bb
            ext = ((asc - dy - y0) / float(_INK_MEASURE_SIZE),
                   (asc - dy - y1) / float(_INK_MEASURE_SIZE))
    except Exception as exc:
        logger.debug(f"[FONT] ink-extent measure failed for {face.key} {ch!r}: {exc}")
    per_face[ch] = ext
    return ext


# ── Advance widths (for fitting injected text into a fixed space) ────────────
# PDF has no reflow: text injected over an edited run cannot push its neighbours
# along, so the only way to keep a longer replacement off the word next to it is
# to measure it first.  The measurement must match what will actually RENDER,
# which is the bundled face — the same TTF whose charWidths become the embedded
# font's /Widths — so measuring it is exact rather than approximate.
_CHARW_CACHE = {}         # ttf path -> ({codepoint: width/1000}, default_width)


def _char_widths(rl_font):
    """({codepoint: advance in /1000 em}, default) for the face *rl_font* will
    actually be drawn with, or None when nothing suitable is bundled."""
    path = measurement_ttf_path(rl_font)
    if not path:
        return None
    hit = _CHARW_CACHE.get(path)
    if hit is None:
        try:
            from reportlab.pdfbase.ttfonts import TTFontFile
            ttf = TTFontFile(path, charInfo=1, validate=0)
            hit = (dict(ttf.charWidths), float(ttf.defaultWidth or 500))
        except Exception as exc:
            logger.warning(f"[FONT] width table read failed for {path}: {exc}")
            hit = ({}, 500.0)
        _CHARW_CACHE[path] = hit
    return hit


def string_width(rl_font, text, size):
    """Advance width of *text* in points, as the injected PDF text will render.

    Read from the BUNDLED face's own width table — the same `charWidths` that
    `_metrics` turns into the embedded font's /Widths — so this is not an
    approximation of the output, it IS the output's arithmetic.  For a base-14
    name the bundled face is the metric-compatible Liberation equivalent, which
    is the mapping the rest of this module already relies on.

    Two things this deliberately does NOT do.  It does not measure with Pillow:
    `getlength` returns HINTED, pixel-quantised advances, so 'Invoice' came out
    39.0pt at 12pt and 75.0pt at 24pt — a 4% error against a renderer that
    applies no hinting at all, which is enough to mis-fit text at the boundary.
    And it does not kern, because a PDF `Tj` string does not kern either.

    Characters outside WinAnsi are drawn as '?' by `_safe_pdf_str`, so they are
    measured as '?' too.  Falls back to a heuristic for an unbundled face; the
    fallback must never return 0, because 0 reads as "it fits".
    """
    if not text:
        return 0.0
    try:
        size = float(size)
    except (TypeError, ValueError):
        return 0.0
    if size <= 0:
        return 0.0
    table = _char_widths(rl_font)
    if table is None:
        return len(text) * size * 0.45
    widths, default = table
    total = 0.0
    for ch in text:
        o = ord(ch)
        if o > 255:
            o = ord('?')          # what _safe_pdf_str will actually emit
        total += widths.get(o, default)
    return total * size / 1000.0


# ── Embedding ─────────────────────────────────────────────────────────────────
# Flags (PDF 9.8.2): bit positions are 1-based in the spec.
_FF_FIXEDPITCH = 1 << 0    # 1
_FF_SERIF      = 1 << 1    # 2
_FF_NONSYMB    = 1 << 5    # 32  (Nonsymbolic) — REQUIRED for WinAnsi simple fonts
_FF_ITALIC     = 1 << 6    # 64


def _metrics(face):
    """Parse the TTF once, return (raw_bytes, descriptor_dict_values, widths)."""
    from reportlab.pdfbase.ttfonts import TTFontFile
    ttf = TTFontFile(face.path, charInfo=1, validate=0)
    with open(face.path, 'rb') as fh:
        raw = fh.read()

    # PostScript name for /BaseFont — make it unique & valid (no spaces).
    ps_name = ttf.name.decode('latin-1') if isinstance(ttf.name, bytes) else str(ttf.name)
    ps_name = ps_name.replace(' ', '')

    flags = _FF_NONSYMB
    if face.css_style == 'italic':
        flags |= _FF_ITALIC
    if 'Serif' in face.file or 'Times' in face.file:
        flags |= _FF_SERIF
    if 'Mono' in face.file:
        flags |= _FF_FIXEDPITCH

    # /Widths for codes 32..255, mapped through cp1252 (WinAnsi) to unicode.
    first, last = 32, 255
    widths = []
    for code in range(first, last + 1):
        try:
            uni = bytes([code]).decode('cp1252')
            w = ttf.charWidths.get(ord(uni))
        except (UnicodeDecodeError, ValueError):
            w = None
        if w is None:
            w = ttf.defaultWidth or 500
        widths.append(int(round(w)))

    desc = {
        'ps_name':      ps_name,
        'flags':        flags,
        'bbox':         [int(round(v)) for v in ttf.bbox],
        'italic_angle': int(round(ttf.italicAngle)),
        'ascent':       int(round(ttf.ascent)),
        'descent':      int(round(ttf.descent)),
        'cap_height':   int(round(ttf.capHeight)),
        'stem_v':       int(round(ttf.stemV)),
    }
    return raw, desc, (first, last, widths)


def _build_font_objects(writer, face):
    """Create the FontFile2 / FontDescriptor / Font indirect objects for a face
    and return the indirect reference to the /Font dictionary."""
    raw, desc, (first, last, widths) = _metrics(face)

    # FontFile2 stream (the embedded TTF program). /Length1 = uncompressed size.
    ff = DecodedStreamObject()
    ff.set_data(raw)
    ff[NameObject('/Length1')] = NumberObject(len(raw))
    ff_ref = writer._add_object(ff)

    descriptor = DictionaryObject({
        NameObject('/Type'):        NameObject('/FontDescriptor'),
        NameObject('/FontName'):    NameObject('/' + desc['ps_name']),
        NameObject('/Flags'):       NumberObject(desc['flags']),
        NameObject('/FontBBox'):    ArrayObject([NumberObject(v) for v in desc['bbox']]),
        NameObject('/ItalicAngle'): NumberObject(desc['italic_angle']),
        NameObject('/Ascent'):      NumberObject(desc['ascent']),
        NameObject('/Descent'):     NumberObject(desc['descent']),
        NameObject('/CapHeight'):   NumberObject(desc['cap_height']),
        NameObject('/StemV'):       NumberObject(desc['stem_v']),
        NameObject('/FontFile2'):   ff_ref,
    })
    desc_ref = writer._add_object(descriptor)

    font = DictionaryObject({
        NameObject('/Type'):           NameObject('/Font'),
        NameObject('/Subtype'):        NameObject('/TrueType'),
        NameObject('/BaseFont'):       NameObject('/' + desc['ps_name']),
        NameObject('/FirstChar'):      NumberObject(first),
        NameObject('/LastChar'):       NumberObject(last),
        NameObject('/Widths'):         ArrayObject([NumberObject(w) for w in widths]),
        NameObject('/FontDescriptor'): desc_ref,
        NameObject('/Encoding'):       NameObject('/WinAnsiEncoding'),
        # Private marker: identifies this as OUR bundled embedding of `face.key`.
        # Readers ignore unknown keys. ensure_embedded_font() reuses a page font
        # only when this matches, which is the ONLY reliable way to tell our own
        # font from a same-named one the document already carried (see there).
        NameObject(_PW_FACE_KEY):      TextStringObject(face.key),
    })
    return writer._add_object(font)


def ensure_embedded_font(writer, page, rl_font):
    """Ensure the bundled face for ``rl_font`` is embedded in the document and
    referenced from this page's /Resources. Returns the resource name (e.g.
    '/F3'), or None if ``rl_font`` is not a bundled font.

    The FontFile2/descriptor/Font objects are built once per document and cached
    on the writer; subsequent pages reference the same indirect Font object.
    """
    face = resolve(rl_font)
    if face is None:
        return None

    # Per-document cache of {face.key: font indirect ref}
    cache = getattr(writer, '_pw_embedded_fonts', None)
    if cache is None:
        cache = {}
        writer._pw_embedded_fonts = cache

    font_ref = cache.get(face.key)
    if font_ref is None:
        font_ref = _build_font_objects(writer, face)
        cache[face.key] = font_ref

    # Resolve / build the page /Resources and /Font sub-dict (mirrors the
    # robust-rebuild approach used by editor._ensure_font for pypdf 5.x).
    res_ref = page.get('/Resources')
    old_resources = res_ref.get_object() if hasattr(res_ref, 'get_object') else res_ref
    if old_resources is None:
        old_resources = DictionaryObject()

    fonts_ref = old_resources.get('/Font')
    if hasattr(fonts_ref, 'get_object'):
        fonts = fonts_ref.get_object()
    elif fonts_ref is not None:
        fonts = fonts_ref
    else:
        fonts = DictionaryObject()

    # Already referenced on this page?  Reuse ONLY a font we embedded ourselves:
    # the cached indirect object, or one carrying our private marker (left by an
    # apply pass in an earlier session, so it is no longer in the writer cache).
    #
    # Deliberately NOT matched by /BaseFont name.  Callers emit one-byte
    # WinAnsi strings — `(text) Tj` — and a document's own font with the same
    # name is routinely a /Type0 composite with /Identity-H encoding, where
    # those bytes are consumed in PAIRS: "PatchworkPDF" renders as CJK CIDs, or
    # as nothing at all when the subset has no such glyphs, i.e. the edited word
    # silently vanishes.  Word-produced PDFs hit this constantly, since they
    # embed both a Type0 and a TrueType flavour of e.g. Calibri.  Foreign
    # subsets are unsafe even when simple, too: they contain only the glyphs the
    # original document used, so any character the user newly types comes out
    # blank.  Embedding our own complete face is the only correct option.
    for name, ref in fonts.items():
        try:
            if getattr(ref, 'idnum', None) is not None and getattr(font_ref, 'idnum', None) is not None:
                if ref.idnum == font_ref.idnum:
                    return str(name)
            obj = ref.get_object() if hasattr(ref, 'get_object') else ref
            if (str(obj.get(_PW_FACE_KEY, '')) == face.key
                    and str(obj.get('/Subtype', '')) == '/TrueType'
                    and str(obj.get('/Encoding', '')) == '/WinAnsiEncoding'):
                return str(name)
        except Exception:
            pass

    # Assign next /F# name and point it at the shared font object.
    nums = []
    for k in fonts.keys():
        try:
            nums.append(int(str(k).lstrip('/F').lstrip('F')))
        except Exception:
            pass
    new_name = '/F' + str(max(nums, default=0) + 1)
    fonts[NameObject(new_name)] = font_ref

    new_resources = DictionaryObject()
    for k, v in old_resources.items():
        new_resources[NameObject(str(k))] = v
    new_resources[NameObject('/Font')] = fonts
    page[NameObject('/Resources')] = new_resources
    return new_name


# ── Frontend helpers ──────────────────────────────────────────────────────────

