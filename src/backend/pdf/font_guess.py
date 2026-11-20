"""font_guess.py — estimate the typeface, weight and size of an OCR'd word.

OCR gives the WORDS and a box, nothing about how they were set, so every OCR
word used to come back as Calibri (bold decided by stroke thickness alone) and
an edit to a word in Times or Arial looked wrong next to its neighbours (v3.4
report, "Read text in images").

What OCR does give is exactly which letters are in the box.  So each bundled
face can be asked to draw those same letters, and the drawing compared with the
pixels:

* WIDTH — scaled to the word's ink height, how wide would this face set these
  letters?  The single strongest signal: Courier, Times, Arial and Calibri set
  the same word at clearly different widths.
* SHAPE — the candidate's drawing stretched onto the word's box, compared
  pixel for pixel (IoU).  Separates serif from sans where widths are close.
* WEIGHT — stroke thickness relative to height, measured the same way on both
  (regular vs bold of each family).

The candidates are only the faces the app can actually WRITE (the bundled
Calibri/Arial/Times/Courier families), because the point is that an edit
matches: the "closest drawable face", not a font name.

A single short word is weak evidence ("a", "-", "1"), so callers vote per line
(`vote_line`) and let a short word take its line's family.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Families and the logical names apply_edits can embed (platform_io.font_assets).
FAMILIES = ('Calibri', 'Arial', 'TimesNR', 'CourierNew')
_FACES = {
    ('Calibri', False): 'Calibri',     ('Calibri', True): 'Calibri-Bold',
    ('Arial', False): 'Arial',         ('Arial', True): 'Arial-Bold',
    ('TimesNR', False): 'TimesNR',     ('TimesNR', True): 'TimesNR-Bold',
    ('CourierNew', False): 'CourierNew', ('CourierNew', True): 'CourierNew-Bold',
}
_RENDER_PX = 96              # reference size the candidates are drawn at
_MIN_ALNUM = 3               # fewer letters than this: let the line decide

_font_cache = {}
_render_cache = {}


def _np():
    import numpy as np
    return np


def _pil():
    from PIL import Image, ImageDraw, ImageFont
    return Image, ImageDraw, ImageFont


def _font(rl_font):
    f = _font_cache.get(rl_font)
    if f is None:
        from platform_io import font_assets
        face = font_assets.resolve(rl_font)
        if face is None or not os.path.isfile(face.path):
            _font_cache[rl_font] = False
            return False
        _, _, ImageFont = _pil()
        f = _font_cache[rl_font] = ImageFont.truetype(face.path, _RENDER_PX)
    return f


def _trim(mask):
    np = _np()
    ys = np.where(mask.any(axis=1))[0]
    xs = np.where(mask.any(axis=0))[0]
    if not len(ys) or not len(xs):
        return None
    return mask[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]


def _render_full(rl_font, text):
    """(trimmed bool ink mask, rows of ink BELOW the baseline) for *text* in
    *rl_font*, or (None, 0)."""
    key = (rl_font, text)
    if key in _render_cache:
        return _render_cache[key]
    f = _font(rl_font)
    out = (None, 0)
    if f:
        Image, ImageDraw, _ = _pil()
        np = _np()
        w = int(f.getlength(text)) + _RENDER_PX
        base = _RENDER_PX + _RENDER_PX // 3          # baseline row
        im = Image.new('L', (max(8, w), _RENDER_PX * 2), 255)
        ImageDraw.Draw(im).text((_RENDER_PX // 4, base), text, fill=0, font=f,
                                anchor='ls')
        m = np.asarray(im) < 128
        rows = np.where(m.any(axis=1))[0]
        if len(rows):
            out = (_trim(m), max(0, int(rows[-1]) + 1 - base))
    if len(_render_cache) > 4000:
        _render_cache.clear()
    _render_cache[key] = out
    return out


def _render(rl_font, text):
    """Trimmed bool ink mask of *text* drawn in *rl_font*, or None."""
    return _render_full(rl_font, text)[0]


def stroke_ratio(mask):
    """Stroke width / height of a bool ink mask (the measure editor.py uses
    for bold): one 3x3 erosion removes ~1px from each side of every stroke."""
    np = _np()
    b = mask.astype(np.uint8)
    ink = int(b.sum())
    if ink < 10 or b.shape[0] < 3 or b.shape[1] < 3:
        return 0.0
    e = (b[:-2, :-2] & b[:-2, 1:-1] & b[:-2, 2:] &
         b[1:-1, :-2] & b[1:-1, 1:-1] & b[1:-1, 2:] &
         b[2:, :-2] & b[2:, 1:-1] & b[2:, 2:])
    removed = ink - int(e.sum())
    if removed <= 0:
        return 0.0
    return (2.0 * ink / removed) / float(b.shape[0])


def _resize_mask(mask, w, h):
    Image, _, _ = _pil()
    np = _np()
    im = Image.fromarray((mask * 255).astype('uint8'))
    return np.asarray(im.resize((max(1, w), max(1, h)), Image.BILINEAR)) >= 128


def _alnum(text):
    return sum(ch.isalnum() for ch in text)


def score_faces(ink_mask, text):
    """{rl_font: score} for one word (higher is better), or {} when the word
    gives too little to go on.  *ink_mask* is the word's bool ink mask (ink =
    True, polarity already resolved by the caller)."""
    if not text or _alnum(text) < _MIN_ALNUM or ink_mask is None:
        return {}
    word = _trim(ink_mask)
    if word is None or word.shape[0] < 6 or word.shape[1] < 6:
        return {}
    np = _np()
    H, W = word.shape
    sw_obs = stroke_ratio(word)
    out = {}
    for fam in FAMILIES:
        for bold in (False, True):
            rl = _FACES[(fam, bold)]
            ref = _render(rl, text)
            if ref is None:
                continue
            rh, rw = ref.shape
            # WIDTH: this face's width for these letters at the word's height.
            pred_w = rw * (H / float(rh))
            width_err = abs(pred_w - W) / float(W)
            # SHAPE: drawn onto the word's own box, pixel agreement.
            fit = _resize_mask(ref, W, H)
            inter = int(np.logical_and(fit, word).sum())
            union = int(np.logical_or(fit, word).sum()) or 1
            iou = inter / union
            # WEIGHT: stroke thickness of the candidate AT THE WORD'S SIZE.
            sw_ref = stroke_ratio(fit)
            weight_err = abs(sw_ref - sw_obs) / max(sw_obs, 1e-3)
            out[rl] = iou - 1.2 * width_err - 0.35 * min(weight_err, 1.5)
    return out


def best_face(scores):
    if not scores:
        return None
    return max(scores, key=scores.get)


def vote_totals(word_scores):
    """({family: letter-weighted score}, letters) for [(n_letters, scores)]."""
    tot, letters = {}, 0
    for n, sc in word_scores:
        if not sc:
            continue
        letters += n
        for fam in FAMILIES:
            fs = [v for k, v in sc.items() if k.split('-')[0] == fam]
            if fs:
                tot[fam] = tot.get(fam, 0.0) + n * max(fs)
    return tot, letters


def vote_line(word_scores):
    """Family for a set of words from their scores ([(n_letters, scores)]):
    each family's best per-word score, weighted by letter count.  Returns the
    family name or None."""
    tot, _ = vote_totals(word_scores)
    if not tot:
        return None
    return max(tot, key=tot.get)


# How much better (score per letter) a line's own favourite must do than the
# page's family before the line is allowed to differ.  Documents use one or two
# families; a short line ("3 June 2026"), an underlined link or a noisy word
# can tip a close vote the wrong way, and a lone Times line in a sans document
# is a far more visible mistake than missing a genuine change of face.
PAGE_PRIOR_MARGIN = 0.10


def choose_family(word_scores, page_family):
    tot, letters = vote_totals(word_scores)
    if not tot:
        return page_family
    best = max(tot, key=tot.get)
    if (page_family and best != page_family and page_family in tot
            and tot[best] - tot[page_family] < PAGE_PRIOR_MARGIN * max(letters, 1)):
        return page_family
    return best


def face_for(family, bold):
    return _FACES.get((family, bool(bold)), 'Calibri-Bold' if bold else 'Calibri')


def em_px(ink_mask, text, rl_font):
    """The em size (in the word's own pixels) at which *rl_font* draws *text*
    to cover the word's ink: the mean of the height estimate and the width
    estimate.  Height alone (the old measure) is thrown by faces whose caps
    sit low or high in the em (Courier ran ~10% off); width alone by a
    lookalike face that sets narrower than the original.  The mean keeps a
    replacement both level with its line and the width of what it replaces.
    None when there is too little to measure."""
    if not text or _alnum(text) < 1 or ink_mask is None:
        return None
    word = _trim(ink_mask)
    ref = _render(rl_font, text)
    if word is None or ref is None or word.shape[0] < 4:
        return None
    H, W = word.shape
    rh, rw = ref.shape
    est_h = H * _RENDER_PX / float(rh)
    est_w = W * _RENDER_PX / float(rw)
    # A wildly different width means the OCR box is not this word's ink alone
    # (a merged neighbour, a rule line): trust the height.
    if not 0.6 <= est_w / est_h <= 1.6:
        return est_h
    return (est_h + est_w) / 2.0


def descent_px(ink_mask, text, rl_font):
    """How far (in the word's pixels) its ink reaches BELOW the baseline, by
    the matched face: "Prepared" hangs its p below the line, "Studio" does not.
    An OCR box ends at the lowest ink, so placing a replacement on the box
    bottom set it a descender too low (v3.4)."""
    word = _trim(ink_mask) if ink_mask is not None else None
    ref, desc = _render_full(rl_font, text) if text else (None, 0)
    if word is None or ref is None or not desc:
        return 0.0
    return desc * word.shape[0] / float(ref.shape[0])
