"""OCR'd words get a typeface, weight, size and baseline that match (v3.4).

Every OCR word used to be Calibri (bold by stroke thickness alone, size from
box height, baseline on the box bottom), so an edit to a word in Times, Arial
or Courier — or one with a descender — visibly did not match.  pdf/font_guess
draws the OCR'd letters in each bundled face and compares width, shape and
stroke with the pixels; lines vote, the page's family is a prior.

Benchmarked on real Windows fonts printed to a blurred 300 dpi 'scan' (15
faces, 116 words): family 45% -> 96%, serif/sans/mono class 59% -> 100%,
weight 78% -> 100%.  This test uses only the bundled faces (via reportlab +
pdfium, so the drawing engine differs from the matcher's) to stay portable.
"""
import io
import os

import _bootstrap  # noqa: F401

import pytest

from routes import editor as _editor
from pdf import font_guess as FG

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = os.path.join(ROOT, 'assets', 'fonts')
CASES = [('Carlito-Regular.ttf', 'Calibri'), ('Carlito-Bold.ttf', 'Calibri-Bold'),
         ('LiberationSans-Regular.ttf', 'Arial'), ('LiberationSans-Bold.ttf', 'Arial-Bold'),
         ('LiberationSerif-Regular.ttf', 'TimesNR'), ('LiberationSerif-Bold.ttf', 'TimesNR-Bold'),
         ('LiberationMono-Regular.ttf', 'CourierNew'), ('LiberationMono-Bold.ttf', 'CourierNew-Bold')]


@pytest.fixture(scope='module', autouse=True)
def _heavy():
    _editor._heavy_init()


def _scan(lines_by_case):
    """An image-only page: each case's lines set in its face, rasterised and
    softened like a scan."""
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import pypdfium2 as pdfium
    from PIL import ImageFilter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    y, spans = 790, []
    for fn, _ in CASES:
        name = 'T_' + fn
        pdfmetrics.registerFont(TTFont(name, os.path.join(FONTS, fn)))
        c.setFont(name, 12)
        top = y
        for t in lines_by_case:
            c.drawString(60, y, t); y -= 20
        spans.append((y + 10, top + 14))
        y -= 14
    c.showPage(); c.save()
    doc = pdfium.PdfDocument(buf.getvalue())
    img = doc[0].render(scale=300 / 72).to_pil().convert('L').filter(ImageFilter.GaussianBlur(0.6))
    doc.close()
    out = io.BytesIO(); img.convert('RGB').save(out, 'PDF', resolution=300)
    return out.getvalue(), spans


def test_ocr_words_get_the_right_face_and_weight():
    if not _editor._OCR_AVAILABLE:
        pytest.skip('Tesseract not installed')
    pdf, spans = _scan(['Invoice number dated Monday', 'Project Completion Report'])
    items = _editor.ocr_extract_text_items(pdf)
    ok = n = 0
    for it in items:
        if sum(ch.isalnum() for ch in it['text']) < 3:
            continue
        mid = (it['y'] + it['y2']) / 2
        for (lo, hi), (_, want) in zip(spans, CASES):
            if lo <= mid <= hi:
                n += 1; ok += (it['rl_font'] == want)
    assert n >= 40, n
    assert ok / n >= 0.9, (ok, n)


def test_descender_lifts_the_baseline_off_the_box_bottom():
    if not _editor._OCR_AVAILABLE:
        pytest.skip('Tesseract not installed')
    pdf, _ = _scan(['Prepared Studio'])
    items = _editor.ocr_extract_text_items(pdf)
    prep = [i for i in items if i['text'] == 'Prepared']
    stud = [i for i in items if i['text'] == 'Studio']
    assert prep and stud
    assert all(i.get('baseline_y', i['y']) > i['y'] + 0.5 for i in prep)
    assert all(abs(i.get('baseline_y', i['y']) - i['y']) < 0.6 for i in stud)


def test_page_family_wins_a_close_line_vote():
    close = [(5, {'TimesNR': 0.62, 'Calibri': 0.60})]
    assert FG.choose_family(close, 'Calibri') == 'Calibri'
    clear = [(10, {'TimesNR': 0.80, 'Calibri': 0.40})]
    assert FG.choose_family(clear, 'Calibri') == 'TimesNR'
    assert FG.choose_family([], 'Arial') == 'Arial'
