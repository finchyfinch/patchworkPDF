"""
ocr_utils.py — shared OCR helper for PatchworkPDF.

Provides word-level text extraction from scanned/image-only PDF pages via
Tesseract.  Used by both viewer.py (search & selection) and editor.py.

Public API:
    init_ocr()            — locate Tesseract; call once at startup or lazily.
    is_ocr_available()    — True if Tesseract was found and is working.
    ocr_unavailable_reason() — human-readable reason when unavailable.
    ocr_page_words(pdf_bytes, scale=2.0)
                          — run OCR on a single-page PDF; return word boxes
                            in PDF user-space (y=0 at bottom).
"""

import io
import os
import sys
import shutil
import subprocess
import threading
import logging
import contextlib

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def hidden_console():
    """Suppress the console window a child process (Tesseract) flashes on Windows.

    Patches subprocess.Popen — the common denominator behind subprocess.run(),
    subprocess.check_output() and direct Popen() calls — so every child spawned
    inside the block is created hidden.  A narrower patch of check_output alone
    misses pytesseract.get_languages(), which shells out via subprocess.run(),
    and any pytesseract build whose Popen call omits a hidden startupinfo.
    No-op off Windows.  (Restores the original Popen on exit.)
    """
    if sys.platform != 'win32':
        yield
        return
    _orig_popen = subprocess.Popen
    _CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)

    class _HiddenPopen(_orig_popen):
        def __init__(self, *args, **kwargs):
            if kwargs.get('startupinfo') is None:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                kwargs['startupinfo'] = si
            kwargs['creationflags'] = kwargs.get('creationflags', 0) | _CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _HiddenPopen
    try:
        yield
    finally:
        subprocess.Popen = _orig_popen

# Lazy-loaded heavy deps
_pytesseract = None
_pdfium      = None
_PILImage    = None
_np          = None

_OCR_AVAILABLE          = False
_OCR_UNAVAILABLE_REASON = None
_INIT_DONE              = False
_INIT_LOCK              = threading.Lock()
_TESS_MAJOR             = None   # tesseract major version; decides TESSDATA_PREFIX

# Serialises all calls into pdfium and pikepdf/QPDF across the whole app.
# Defined here so both viewer.py and editor.py can import the same lock object.
# See the "IMPORTANT — NATIVE LIBRARY THREAD SAFETY" section in viewer.py for
# the full explanation of why this lock is necessary.
#
# Reentrant, so a helper can take it itself and still be called by code that
# already holds it (pdf_core.render_page_thumbnail is both).  With a plain Lock
# the helpers had to rely on every caller remembering, and several did not.
_NATIVE_LOCK = threading.RLock()

_TESSERACT_WIN_PATHS = [
    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
    r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
    r'C:\Users\Public\Tesseract-OCR\tesseract.exe',
]

# Common install locations to check when tesseract isn't on PATH.
_TESSERACT_MAC_PATHS = [
    '/opt/homebrew/bin/tesseract',      # Apple-silicon Homebrew
    '/usr/local/bin/tesseract',         # Intel Homebrew / manual
    '/opt/local/bin/tesseract',         # MacPorts
]
_TESSERACT_NIX_PATHS = [
    '/usr/bin/tesseract',
    '/usr/local/bin/tesseract',
    '/app/bin/tesseract',               # Flatpak (built as a manifest module)
]


def _find_tesseract():
    if shutil.which('tesseract'):
        return shutil.which('tesseract')
    if sys.platform == 'win32':
        candidates = _TESSERACT_WIN_PATHS
    elif sys.platform == 'darwin':
        candidates = _TESSERACT_MAC_PATHS
    else:
        candidates = _TESSERACT_NIX_PATHS
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def init_ocr():
    """Locate Tesseract and load heavy deps.  Safe to call multiple times."""
    global _pytesseract, _pdfium, _PILImage, _np
    global _OCR_AVAILABLE, _OCR_UNAVAILABLE_REASON, _INIT_DONE, _TESS_MAJOR

    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return

        try:
            import pypdfium2 as _pd
            _pdfium = _pd
        except ImportError as e:
            _OCR_UNAVAILABLE_REASON = f"pypdfium2 not available: {e}"
            _INIT_DONE = True
            return

        try:
            from PIL import Image as _PI
            _PILImage = _PI
        except ImportError as e:
            _OCR_UNAVAILABLE_REASON = f"Pillow not available: {e}"
            _INIT_DONE = True
            return

        try:
            import numpy as _numpy
            _np = _numpy
        except ImportError as e:
            _OCR_UNAVAILABLE_REASON = f"numpy not available: {e}"
            _INIT_DONE = True
            return

        try:
            import pytesseract as _pt
            _pytesseract = _pt

            tess_path = _find_tesseract()
            if tess_path:
                _pytesseract.pytesseract.tesseract_cmd = tess_path

            # Probe version (suppress console flash on Windows)
            with hidden_console():
                ver = _pytesseract.get_tesseract_version()

            # Remember the major version: it decides what TESSDATA_PREFIX must
            # point at (see _apply_tessdata_env).
            try:
                _TESS_MAJOR = int(str(ver).lstrip('v').split('.')[0])
            except (ValueError, IndexError):
                _TESS_MAJOR = 5          # assume current; 4 is long superseded

            _OCR_AVAILABLE = True
            logger.info(f"Tesseract {ver} at: {_pytesseract.pytesseract.tesseract_cmd}")

        except Exception as e:
            _OCR_UNAVAILABLE_REASON = str(e)

        _INIT_DONE = True


def is_ocr_available():
    if not _INIT_DONE:
        init_ocr()
    return _OCR_AVAILABLE


def ocr_unavailable_reason():
    if not _INIT_DONE:
        init_ocr()
    return _OCR_UNAVAILABLE_REASON


# ── OCR image preprocessing (numpy + Pillow only — no extra dependencies) ──────
#
# All steps below run on the rendered page image *before* it is handed to
# Tesseract.  Three of them (grayscale, contrast stretch, median denoise) only
# change pixel *values*, so they never affect coordinates.  Deskew rotates the
# image, so the OCR pixel boxes come back in the rotated ("deskewed") frame and
# must be mapped back to the original render frame with deskew_unmap_boxes()
# before any pixel→PDF conversion.  Shared by editor.py and viewer (via this
# module) so both OCR paths get identical treatment.

def _estimate_skew_angle(gray_img, max_angle=6.0, step=0.5):
    """Return the rotation (degrees, CCW positive) that best horizontalises the
    text lines, found by maximising the variance of the horizontal dark-pixel
    projection.  Straight, well-separated lines give alternating high/low row
    sums → high variance; skewed lines smear the projection → low variance.

    Pure numpy/PIL.  Runs on a downscaled copy (angle is scale-invariant) so the
    ~25 trial rotations cost only ~100-200 ms even on a large page.
    """
    w, h = gray_img.size
    sc = 1000.0 / max(w, h)
    small = (gray_img.resize((max(1, int(w * sc)), max(1, int(h * sc))))
             if sc < 1 else gray_img)
    # Threshold to ink mask; mean-based so it adapts to the (already stretched)
    # tonal range without a fixed magic constant.
    thr = max(1, int(_np.asarray(small).mean()) - 10)

    best_angle, best_score, zero_score = 0.0, -1.0, 0.0
    steps = int(round(max_angle / step))
    for k in range(-steps, steps + 1):
        a = k * step
        rot = (small if a == 0
               else small.rotate(a, resample=_PILImage.BILINEAR,
                                 expand=False, fillcolor=255))
        ink = (_np.asarray(rot, dtype=_np.uint8) < thr)
        rowsum = ink.sum(axis=1).astype(_np.float64)
        score = float(_np.var(rowsum))
        if a == 0:
            zero_score = score
        if score > best_score:
            best_score, best_angle = score, a
    # Require a clear improvement over "no rotation" so a sparse page or stray
    # diagonal graphic can't nudge us into a spurious large rotation.
    if zero_score > 0 and best_score < zero_score * 1.05:
        return 0.0
    return best_angle


# ── Rule / gridline / underline removal (Phase 1 structure removal) ───────────
# Tesseract garbles text that touches long thin strokes: table gridlines,
# underlines, and cell/box borders fuse with glyphs and break character
# segmentation (measured: removing them ~doubled high-confidence words on a
# gridded title block).  We whiten out long horizontal/vertical runs before OCR.
# Pure numpy (sliding-window morphology) — no new dependency, <1 s on an A3 page.
# Pixel-value only: coordinates are unaffected (deskew remains the sole geometry
# change).  Kernel lengths scale with the render factor (≈DPI/72) and sit above
# glyph stroke dimensions so text — including large title fonts — survives.
_RULE_H_FACTOR     = 12.0   # min horizontal-rule length ≈ 12 pt
_RULE_V_FACTOR     = 16.0   # min vertical-rule length ≈ 16 pt
_RULE_THICK_FACTOR = 2.0    # a rule is thinner than ≈ 2 pt across its width


def _binary_open_1d(mask, length, axis):
    """Binary morphological opening (erode→dilate) with a flat line structuring
    element of *length* along *axis*.  Pure numpy via sliding windows."""
    if length < 2:
        return mask
    from numpy.lib.stride_tricks import sliding_window_view
    pad = [(0, 0), (0, 0)]
    pad[axis] = (length // 2, length // 2)
    m  = _np.pad(mask, pad)
    er = sliding_window_view(m, length, axis=axis).min(axis=-1)   # erosion
    m2 = _np.pad(er, pad)
    di = sliding_window_view(m2, length, axis=axis).max(axis=-1)  # dilation
    sl = [slice(None), slice(None)]
    sl[axis] = slice(0, mask.shape[axis])
    return di[tuple(sl)]


def remove_rules(gray_arr, scale):
    """Return a copy of *gray_arr* (uint8, ink dark) with long horizontal and
    vertical rules — table gridlines, underlines, cell/box borders — whitened
    out.  *scale* is the render points→pixels factor (kernels scale with it).

    A rule is detected as a run that is BOTH long (≥ hlen/vlen) AND thin across
    its width (not part of a ≥ tthick run in the perpendicular axis).  The
    thinness test is what makes this safe for text: a capital-letter bar or stem
    is the same length as a short rule, but it connects to a perpendicular stroke
    and so fails the thinness test — validated to leave ordinary text (incl. bold
    titles) byte-identical, while ~doubling high-confidence words on gridded
    tables.  Near no-op on pages with no rules."""
    s = max(1.0, float(scale or 2.0))
    hlen   = max(10, int(round(_RULE_H_FACTOR * s)))
    vlen   = max(10, int(round(_RULE_V_FACTOR * s)))
    tthick = max(3,  int(round(_RULE_THICK_FACTOR * s)))
    thr  = max(1, int(gray_arr.mean()) - 10)
    ink  = gray_arr < thr
    if not ink.any():
        return gray_arr
    v_thick = _binary_open_1d(ink, tthick, 0)   # part of a tall vertical run
    h_thick = _binary_open_1d(ink, tthick, 1)   # part of a wide horizontal run
    h_rule  = _binary_open_1d(ink, hlen, 1) & ~v_thick   # long & vertically thin
    v_rule  = _binary_open_1d(ink, vlen, 0) & ~h_thick   # long & horizontally thin
    lines = h_rule | v_rule
    if not lines.any():
        return gray_arr
    # Grow the mask by 1 px so anti-aliased rule edges go too.
    grown = lines.copy()
    grown[:, 1:]  |= lines[:, :-1]
    grown[:, :-1] |= lines[:, 1:]
    grown[1:, :]  |= lines[:-1, :]
    grown[:-1, :] |= lines[1:, :]
    out = gray_arr.copy()
    out[grown] = 255
    return out


def _speck_fraction(gray_arr):
    """Fraction of ink pixels that are isolated specks (no 4-neighbour ink).
    Near 0 on a clean vector-ish render; high on a speckled scan.  Used to gate
    denoising so it doesn't run — and ruin thin strokes — on clean pages."""
    ink = gray_arr < (int(gray_arr.mean()) - 10)
    total = int(ink.sum())
    if total == 0:
        return 0.0
    nb = _np.zeros_like(ink)
    nb[1:, :]  |= ink[:-1, :]; nb[:-1, :] |= ink[1:, :]
    nb[:, 1:]  |= ink[:, :-1]; nb[:, :-1] |= ink[:, 1:]
    return float((ink & ~nb).sum()) / float(total)


def preprocess_image_for_ocr(pil_img, deskew=True, scale=None):
    """Return (processed_gray_img, skew_angle_deg).

    skew_angle_deg is the CCW rotation applied to the image; pass it (with the
    image centre) to deskew_unmap_boxes() to bring OCR coordinates back to the
    original frame.  0.0 means no rotation was applied.

    *scale* is the render points→pixels factor; when given, long table/box rules
    and underlines are removed before OCR (see remove_rules).

    Contrast-stretch and median-denoise are applied ONLY when the image actually
    needs them (faint / speckled scans).  On clean, high-contrast renders — e.g.
    CAD/engineering drawings with thin single-stroke fonts — both steps do pure
    harm (the median erases the strokes; the stretch, dominated by the white
    background, over-amplifies), measured to cut OCR yield by 60%+.  Gating them
    is what lets the thin-stroke text (titles, table cells) actually read.
    """
    from PIL import ImageFilter
    g = pil_img.convert('L')
    a = _np.asarray(g)

    # Contrast stretch only for genuinely faint scans (ink not already dark).
    if float(_np.percentile(a, 3)) > 120:
        arr = a.astype(_np.float32)
        lo, hi = _np.percentile(arr, (2, 98))
        if hi > lo:
            g = _PILImage.fromarray(_np.clip((arr - lo) * (255.0 / (hi - lo)), 0, 255)
                                    .astype(_np.uint8))
            a = _np.asarray(g)

    # 3x3 median denoise only when the page is actually speckled.
    if _speck_fraction(a) > 0.02:
        g = g.filter(ImageFilter.MedianFilter(size=3))

    # Remove gridlines / underlines / box borders before deskew (kernels are
    # axis-aligned, so this runs while rules are still horizontal/vertical).
    if scale:
        g = _PILImage.fromarray(remove_rules(_np.asarray(g), scale))

    angle = 0.0
    if deskew:
        angle = _estimate_skew_angle(g)
        if abs(angle) >= 0.3:   # ignore sub-pixel jitter; rotation isn't free
            g = g.rotate(angle, resample=_PILImage.BICUBIC,
                         expand=False, fillcolor=255)
        else:
            angle = 0.0
    return g, angle


def deskew_unmap_point(px, py, angle_deg, cx, cy):
    """Inverse of a CCW rotation by angle_deg about (cx, cy) in image (y-down)
    coordinates — maps a deskewed-frame point back to the original frame."""
    import math
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    dx, dy = px - cx, py - cy
    return cx + dx * ca - dy * sa, cy + dx * sa + dy * ca


def deskew_unmap_boxes(words, angle_deg, cx, cy):
    """In place: map each word's pixel box (left/top/width/height) from the
    deskewed frame back to the original render frame.  Box dimensions are kept
    as-is (exact for the small skew angles produced by a scanner bed)."""
    if not angle_deg:
        return
    for w in words:
        ccx = w['left'] + w['width']  / 2.0
        ccy = w['top']  + w['height'] / 2.0
        ox, oy = deskew_unmap_point(ccx, ccy, angle_deg, cx, cy)
        w['left'] = int(round(ox - w['width']  / 2.0))
        w['top']  = int(round(oy - w['height'] / 2.0))


def _box_overlap_frac(a, b):
    """Fraction of the SMALLER of two pixel boxes (keys left/top/width/height)
    covered by their intersection.  0 if they don't overlap."""
    ox = max(0, min(a['left'] + a['width'],  b['left'] + b['width'])
                - max(a['left'], b['left']))
    oy = max(0, min(a['top']  + a['height'], b['top']  + b['height'])
                - max(a['top'],  b['top']))
    if ox <= 0 or oy <= 0:
        return 0.0
    min_area = min(a['width'] * a['height'], b['width'] * b['height'])
    return (ox * oy) / min_area if min_area > 0 else 0.0


def dedup_two_pass_words(words1, words2):
    """Merge Tesseract psm-6 (`words1`) and psm-11 (`words2`) word lists, resolving
    overlaps so the BETTER segmentation of each region wins.  Word dicts carry
    left/top/width/height/conf/text; the originals are returned unmodified.

    psm 6 (uniform block) reads body text reliably but fragments large/sparse
    title text into single letters ('FO','R','ST','U'…) and merges wrapped cells
    into one low-confidence blob; psm 11 (sparse) is the opposite.  Comparing word
    *pairs* and keeping the higher-confidence single word loses a big title that
    psm 6 fragmented into several high-confidence pieces — each piece individually
    ties/beats the one correct psm-11 word ('CONSTRUCTION'), so the whole word was
    dropped (only 'FO'/'ST' survived).

    Instead, cluster all words from both passes by >50% box overlap (transitive),
    and for any cluster containing both passes keep the pass that transcribes the
    region best — highest sum of conf×char_count.  Ties prefer fewer tokens (less
    fragmentation), then pass 1 (its block/line hierarchy is more reliable).
    Single-pass clusters are kept as-is, so this is a no-op where the passes agree.
    """
    n1  = len(words1)
    allw   = list(words1) + list(words2)
    passes = [1] * n1 + [2] * len(words2)
    n = len(allw)
    if n <= 1:
        return allw

    parent = list(range(n))
    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    # Edges = pairs overlapping >50%.  Sort by left edge and stop scanning a box
    # once the next box starts past its right edge — direct overlaps only (the
    # union-find handles transitive clustering), which keeps this affordable on
    # dense pages with thousands of words.
    order = sorted(range(n), key=lambda i: allw[i]['left'])
    for a in range(n):
        i  = order[a]
        ri = allw[i]['left'] + allw[i]['width']
        for b in range(a + 1, n):
            j = order[b]
            if allw[j]['left'] > ri:
                break
            if _box_overlap_frac(allw[i], allw[j]) > 0.5:
                _union(i, j)

    from collections import defaultdict
    comp = defaultdict(list)
    for i in range(n):
        comp[_find(i)].append(i)

    kept = []
    for members in comp.values():
        if len(set(passes[i] for i in members)) == 1:
            kept.extend(allw[i] for i in members)
            continue
        score = {1: 0.0, 2: 0.0}
        count = {1: 0, 2: 0}
        for i in members:
            p = passes[i]
            score[p] += allw[i]['conf'] * len(allw[i]['text'])
            count[p] += 1
        if score[1] != score[2]:
            win = 1 if score[1] > score[2] else 2
        elif count[1] != count[2]:
            win = 1 if count[1] < count[2] else 2     # fewer tokens = less fragmented
        else:
            win = 1
        kept.extend(allw[i] for i in members if passes[i] == win)
    return kept


# ── Second opinion for light-on-dark words ────────────────────────────────────
# White text on a coloured band (table headers, title bars) is where the
# page-level passes misread most: the band's own edge sits against the glyphs,
# and binarisation of the whole page is tuned for dark-on-light.  Measured on an
# invoice header, "VAT" hard against the band's right edge read as "WAT" at 96%
# confidence in BOTH passes — so confidence cannot flag it and dedup cannot fix
# it.  Cropping the word, flipping it to dark-on-light and reading it as one line
# gets "VAT|" (the '|' is the band edge).
#
# The re-read is NOT trusted wholesale: on the same header it also turned
# "GROUP" into "GR" and "Quantity" into "Quantit".  It is only allowed to make a
# correction that keeps the word's shape — see _accept_reread.
_REREAD_MAX_WORDS  = 60      # a page of white-on-dark is a design, not a header
_REREAD_WORKERS    = 4       # tesseract is a subprocess, so threads parallelise
_REREAD_MIN_CONF   = 60
_REREAD_EDGE_CHARS = "|[](){}!'\"`‘’“”.,:;_-~"


def _is_light_on_dark(gray_arr, w):
    """True when the word's box is mostly dark with light strokes."""
    t, l = max(0, int(w['top'])), max(0, int(w['left']))
    r = gray_arr[t:t + int(w['height']), l:l + int(w['width'])]
    if r.size < 20:
        return False
    lo, hi = float(_np.percentile(r, 5)), float(_np.percentile(r, 95))
    if hi - lo < 40:                       # no real contrast: nothing to read
        return False
    return int((r < (lo + hi) / 2.0).sum()) > r.size / 2


def _strip_edge_artifacts(text, orig):
    """Trim bits of box edge / rule the crop picked up ('|', '[', …), except
    where the original word genuinely starts or ends with that character."""
    s = text.strip()
    while s and s[0] in _REREAD_EDGE_CHARS and not orig.startswith(s[0]):
        s = s[1:].lstrip()
    while s and s[-1] in _REREAD_EDGE_CHARS and not orig.endswith(s[-1]):
        s = s[:-1].rstrip()
    return s


def _accept_reread(orig, orig_conf, reread, reread_conf):
    """The corrected text, or None to keep the original.

    Two shapes are accepted, both of which leave the word recognisably the same
    word:
      * same length, a few letters substituted ("WAT" → "VAT") — the classic
        confusable-glyph misread;
      * exactly one letter inserted, and the re-read is far more confident
        ("Unt" 38% → "Unit" 96%) — a thin stroke the page pass lost.
    Anything shorter, longer or restructured is refused: those are the cases
    where the crop cost the re-read context rather than giving it clarity.
    """
    r = _strip_edge_artifacts(reread, orig)
    if not r or r == orig or reread_conf < _REREAD_MIN_CONF:
        return None
    if len(r) == len(orig):
        diffs = sum(1 for a, b in zip(r, orig) if a != b)
        if diffs <= max(1, len(orig) // 3):
            return r
        return None
    if (len(r) == len(orig) + 1 and reread_conf >= max(80, orig_conf + 30)
            and any(r[:i] + r[i + 1:] == orig for i in range(len(r)))):
        return r
    return None


def _reread_word(gray_arr, w, lang, cfg):
    """(text, min_conf) from a single-line read of *w*'s crop, flipped to
    dark-on-light and upscaled."""
    pad = max(4, int(w['height']) // 2)
    t0, l0 = max(0, int(w['top']) - pad), max(0, int(w['left']) - pad)
    crop = gray_arr[t0:int(w['top']) + int(w['height']) + pad,
                    l0:int(w['left']) + int(w['width']) + pad]
    if crop.size == 0:
        return '', -1
    im = _PILImage.fromarray(255 - crop)
    im = im.resize((im.width * 2, im.height * 2), _PILImage.LANCZOS)
    im = _PILImage.fromarray(_np.pad(_np.asarray(im), 20, constant_values=255))
    with hidden_console():
        d = _pytesseract.image_to_data(im, output_type=_pytesseract.Output.DICT,
                                       lang=lang, config='--psm 7' + cfg)
    toks = [(t.strip(), int(c)) for t, c in zip(d['text'], d['conf'])
            if t and t.strip() and int(c) >= 0]
    if not toks:
        return '', -1
    return ' '.join(t for t, _ in toks), min(c for _, c in toks)


def reread_light_on_dark(gray_arr, words, lang, cfg=''):
    """In place: correct misreads among the light-on-dark words of *words*
    (Tesseract word dicts, pixel boxes in *gray_arr*'s frame).  Returns the
    number of words changed.  Never raises — a failed second opinion simply
    leaves the first one standing."""
    try:
        cands = [w for w in words if _is_light_on_dark(gray_arr, w)]
    except Exception:
        return 0
    if not cands:
        return 0
    cands = cands[:_REREAD_MAX_WORDS]
    from concurrent.futures import ThreadPoolExecutor

    def _one(w):
        try:
            return _reread_word(gray_arr, w, lang, cfg)
        except Exception:
            return '', -1

    with ThreadPoolExecutor(max_workers=_REREAD_WORKERS) as ex:
        results = list(ex.map(_one, cands))
    changed = 0
    for w, (txt, conf) in zip(cands, results):
        fixed = _accept_reread(w['text'], int(w.get('conf', 0)), txt, conf)
        if fixed:
            logger.info("[OCR] light-on-dark re-read: %r -> %r (conf %s)",
                        w['text'], fixed, conf)
            w['text'] = fixed
            changed += 1
    return changed


# OCR render resolution.  Tesseract needs ~300 DPI to resolve the small text on
# dense engineering drawings; the old fixed 2.0× (~144 DPI) produced garbage
# (e.g. "EPC CONTRACTOR" → "eee CONTRACTOR") on large-format sheets.  We target
# 300 DPI but cap total pixels so an A0/A1 sheet doesn't blow up render/OCR time.
_OCR_TARGET_DPI = 300
_OCR_MAX_PIXELS = 30_000_000
_OCR_MIN_SCALE  = 2.0
_OCR_MAX_SCALE  = 4.5


def _auto_ocr_scale(page_w_pt, page_h_pt):
    """Pick a render scale that puts the page near _OCR_TARGET_DPI for OCR,
    clamped to [_OCR_MIN_SCALE, _OCR_MAX_SCALE] and to _OCR_MAX_PIXELS."""
    if not page_w_pt or not page_h_pt:
        return _OCR_MIN_SCALE
    scale = _OCR_TARGET_DPI / 72.0
    if (page_w_pt * scale) * (page_h_pt * scale) > _OCR_MAX_PIXELS:
        scale = (_OCR_MAX_PIXELS / (page_w_pt * page_h_pt)) ** 0.5
    return max(_OCR_MIN_SCALE, min(_OCR_MAX_SCALE, scale))


# ── OCR progress reporting ────────────────────────────────────────────────────
# A single page is OCR'd as one blocking operation, and Tesseract's
# image_to_data gives no progress *within* a pass — so the finest *honest*
# granularity is these coarse stages.  ocr_page_words (here) and editor.py's
# ocr_extract_text_items report through the same labels, so the loading overlay
# reads identically in the viewer (text selection) and the editor (open /
# force-OCR).  Callbacks take (current, total, label) to match set_progress.
OCR_PROGRESS_STAGES = 5

OCR_STAGE_RENDER    = (1, 'Rendering page…')
OCR_STAGE_PREP      = (2, 'Preparing image…')
OCR_STAGE_PASS1     = (3, 'Recognising text (pass 1 of 2)…')
OCR_STAGE_PASS2     = (4, 'Recognising text (pass 2 of 2)…')
OCR_STAGE_FINALISE  = (5, 'Finalising text…')


def report_ocr_progress(progress_cb, stage):
    """Invoke a (current, total, label) progress callback for an OCR *stage*
    (one of the OCR_STAGE_* tuples).  No-op when no callback is supplied; a
    misbehaving callback can never interrupt OCR."""
    if not progress_cb:
        return
    step, label = stage
    try:
        progress_cb(step, OCR_PROGRESS_STAGES, label)
    except Exception:
        pass


# ── OCR language selection ────────────────────────────────────────────────────
# OCR runs in the active UI language (combined with English so mixed-language
# documents still read). The Tesseract .traineddata packs are bundled under
# assets/tessdata/ and pointed at with TESSDATA_PREFIX (see _apply_tessdata_env
# for why not --tessdata-dir); when that directory is absent (a dev checkout
# without the packs) we fall back to whatever the installed Tesseract itself
# provides, and ultimately to English.
_UI_TO_TESS = {'en': 'eng', 'es': 'spa', 'fr': 'fra', 'de': 'deu', 'it': 'ita'}
_LANG_CFG_CACHE = {}


def _bundled_tessdata_dir():
    # frozen: next to the exe; dev: <root>/src/backend/pdf/ocr_utils.py -> <root>.
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
    d = os.path.join(base, 'assets', 'tessdata')
    return d if os.path.isdir(d) else None


def _ui_lang():
    try:
        from i18n import i18n_py
        return i18n_py.get_lang()
    except Exception:
        return 'en'


def _system_langs():
    """Languages the installed Tesseract can see (console flash suppressed on
    Windows).  Only consulted when no bundled tessdata dir is available.

    get_languages() shells out via subprocess.run(), so hidden_console() (which
    patches Popen) is required — a check_output-only patch would miss it."""
    try:
        with hidden_console():
            return set(_pytesseract.get_languages(config=''))
    except Exception:
        return {'eng'}


def _apply_tessdata_env(bundled):
    """Point Tesseract at the bundled packs through TESSDATA_PREFIX.

    NOT through ``--tessdata-dir`` in the config string.  pytesseract splits that
    string with ``shlex.split(config, posix=not_windows)``, so on Windows
    (posix=False) shlex keeps the quotes as part of the token and tesseract is
    handed a path with literal quote characters:

        Error opening data file "C:\\...\\assets\\tessdata"/eng.traineddata

    Dropping the quotes only moves the failure: an unquoted path then splits on
    its spaces, which every installed build has (``C:\\Program Files\\...``).
    The environment variable carries the path intact either way, on every
    platform.

    Tesseract 5 expects TESSDATA_PREFIX to BE the tessdata directory; Tesseract 4
    expects its parent and appends ``tessdata`` itself.  Ours is named tessdata,
    so the parent is right for 4.x.
    """
    if not bundled:
        return
    if _TESS_MAJOR and _TESS_MAJOR <= 4:
        os.environ['TESSDATA_PREFIX'] = os.path.dirname(bundled)
    else:
        os.environ['TESSDATA_PREFIX'] = bundled


def ocr_lang_config():
    """Return (lang, config_suffix) for the active UI language, e.g.
    ('deu+eng', '').  Only packs that are actually present are requested;
    degrades to 'eng'.  Cached per UI language.

    The bundled tessdata directory is passed via TESSDATA_PREFIX rather than in
    the config suffix — see _apply_tessdata_env for why — so the suffix is empty
    today.  It is still returned so callers keep working if a genuine Tesseract
    flag needs adding later."""
    ui = _ui_lang()
    if ui in _LANG_CFG_CACHE:
        # Re-assert the env var: cheap, and it keeps working if anything else in
        # the process has cleared or overwritten TESSDATA_PREFIX since.
        _apply_tessdata_env(_LANG_CFG_CACHE[ui][2])
        return _LANG_CFG_CACHE[ui][:2]
    code = _UI_TO_TESS.get(ui, 'eng')
    wanted = list(dict.fromkeys([code, 'eng']))     # [code, 'eng'] or ['eng']

    bundled = _bundled_tessdata_dir()
    result = None
    if bundled:
        have = [c for c in wanted
                if os.path.isfile(os.path.join(bundled, c + '.traineddata'))]
        if have:
            _apply_tessdata_env(bundled)
            result = ('+'.join(have), '', bundled)
    if result is None:
        avail = _system_langs()
        have = [c for c in wanted if c in avail]
        result = ('+'.join(have) if have else 'eng', '', None)

    _LANG_CFG_CACHE[ui] = result
    logger.info("OCR language: %s%s", result[0],
                " (bundled tessdata)" if result[2] else "")
    return result[:2]


def ocr_page_words(pdf_bytes, scale=None, progress_cb=None):
    """Run Tesseract on page 0 of *pdf_bytes*.

    Returns a list of word dicts, each with:
        text  — word string
        x, y  — bottom-left corner in PDF user-space points (y=0 at bottom)
        w, h  — width and height in PDF user-space points

    *scale* is the points→pixels render factor.  When None (the default) it is
    chosen automatically to target ~300 DPI based on page size — essential for
    legible OCR on large-format drawings.

    Returns an empty list if OCR is unavailable or produces no output.
    """
    if not is_ocr_available():
        return []

    try:
        import pypdf as _pypdf_local
    except ImportError:
        _pypdf_local = None

    try:
        doc  = _pdfium.PdfDocument(pdf_bytes)
        page = doc[0]

        # ── Get rotation & raw mediabox dimensions from pypdf ─────────────────
        _rotate = 0
        _raw_W  = page.get_width()
        _raw_H  = page.get_height()
        if _pypdf_local is not None:
            try:
                reader   = _pypdf_local.PdfReader(io.BytesIO(pdf_bytes))
                pypdf_pg = reader.pages[0]
                _rotate  = int(pypdf_pg.get('/Rotate', 0)) % 360
                mb       = pypdf_pg.mediabox
                _raw_W   = float(mb.width)
                _raw_H   = float(mb.height)
            except Exception:
                pass

        # Auto-pick a ~300 DPI render scale when the caller didn't force one.
        # _raw_W/_raw_H are unrotated dims; total pixel area is rotation-agnostic.
        if scale is None:
            scale = _auto_ocr_scale(_raw_W, _raw_H)

        # ── Render page to PIL image ──────────────────────────────────────────
        report_ocr_progress(progress_cb, OCR_STAGE_RENDER)
        bmp = page.render(scale=scale)
        img = bmp.to_pil()

        # ── Preprocess (grayscale, contrast, denoise, deskew) ─────────────────
        # OCR runs on the cleaned image; deskew rotates it, so we map word boxes
        # back to the original render frame (below) before any pixel→PDF maths.
        report_ocr_progress(progress_cb, OCR_STAGE_PREP)
        ocr_img, _skew = preprocess_image_for_ocr(img, deskew=True, scale=scale)
        _cx, _cy = img.width / 2.0, img.height / 2.0

        # ── Two-pass OCR ──────────────────────────────────────────────────────
        # Pass 1 (psm 6): layout-aware, reliable block/par/line hierarchy
        # Pass 2 (psm 11): sparse text, catches isolated words psm 6 misses
        with hidden_console():
            _olang, _ocfg = ocr_lang_config()
            report_ocr_progress(progress_cb, OCR_STAGE_PASS1)
            data1 = _pytesseract.image_to_data(
                ocr_img, output_type=_pytesseract.Output.DICT,
                lang=_olang, config='--psm 6' + _ocfg)
            report_ocr_progress(progress_cb, OCR_STAGE_PASS2)
            data2 = _pytesseract.image_to_data(
                ocr_img, output_type=_pytesseract.Output.DICT,
                lang=_olang, config='--psm 11' + _ocfg)

        report_ocr_progress(progress_cb, OCR_STAGE_FINALISE)

        # ── Collect words ─────────────────────────────────────────────────────
        def _collect(data, pass_num):
            words = []
            for i, text in enumerate(data['text']):
                if not text or not text.strip():
                    continue
                conf = int(data['conf'][i])
                if conf < 10:
                    continue
                words.append({
                    'text':   text.strip(),
                    'conf':   conf,
                    'left':   data['left'][i],
                    'top':    data['top'][i],
                    'width':  data['width'][i],
                    'height': data['height'][i],
                    'pass':   pass_num,
                })
            return words

        words1 = _collect(data1, 1)
        words2 = _collect(data2, 2)

        # Deskew was applied to the OCR image — bring boxes back to the original
        # render frame so the pixel→PDF conversion below stays correct.
        deskew_unmap_boxes(words1, _skew, _cx, _cy)
        deskew_unmap_boxes(words2, _skew, _cx, _cy)

        # ── Cluster-based dedup across the two passes ───────────────────────
        # See dedup_two_pass_words: clusters overlapping detections and keeps the
        # pass that transcribes each region best, so a big title fragmented by
        # psm 6 ('FO','ST',…) doesn't crowd out the correct psm-11 whole word.
        all_words = dedup_two_pass_words(words1, words2)
        # White-on-band text is where both passes misread together.
        reread_light_on_dark(_np.asarray(img.convert('L')), all_words,
                             _olang, _ocfg)

        # ── Pixel → PDF user-space coordinate conversion ──────────────────────
        def _px_to_pdf(px, py, pw, ph):
            """Convert a Tesseract pixel bbox to PDF user-space (y=0 at bottom).
            Returns (x, y, w, h) where x,y is bottom-left."""
            if _rotate == 0:
                x1 = px / scale
                x2 = (px + pw) / scale
                y1 = _raw_H - (py + ph) / scale
                y2 = _raw_H - py / scale
            elif _rotate == 90:
                # pdfium renders /Rotate=90 with the content-space x-axis running
                # DOWN the page, so a Tesseract row (py) maps to raw x and a
                # Tesseract column (px) maps to raw y.  (Previously the x term
                # carried a spurious `_raw_W -` mirror, flipping text to the wrong
                # side of the page on 90° rotations.)
                x1 =  py       / scale
                x2 = (py + ph) / scale
                y1 =  px       / scale
                y2 = (px + pw) / scale
            elif _rotate == 180:
                x1 = _raw_W - (px + pw) / scale
                x2 = _raw_W -  px       / scale
                y1 =  py       / scale
                y2 = (py + ph) / scale
            elif _rotate == 270:
                # Mirror of the 90° case: raw x is measured from the far edge.
                x1 = _raw_W - (py + ph) / scale
                x2 = _raw_W -  py       / scale
                y1 = _raw_H - (px + pw) / scale
                y2 = _raw_H -  px       / scale
            else:
                x1 = px / scale
                x2 = (px + pw) / scale
                y1 = _raw_H - (py + ph) / scale
                y2 = _raw_H - py / scale
            return round(x1, 3), round(y1, 3), round(x2 - x1, 3), round(y2 - y1, 3)

        result = []
        for w in all_words:
            x, y, ww, wh = _px_to_pdf(w['left'], w['top'], w['width'], w['height'])
            if ww > 0 and wh > 0:
                result.append({'text': w['text'], 'x': x, 'y': y, 'w': ww, 'h': wh})

        return result

    except Exception as e:
        logger.error(f"ocr_page_words failed: {e}")
        return []
