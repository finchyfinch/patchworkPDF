"""Editing a run keeps the document's own font, weight and baseline (v3.4).

Field report (a drawing-office title block, A1 sheet with /Rotate 90, set in
Century Gothic): editing "5 SHEETS" to "6" came back in Times BOLD, one glyph
height too HIGH and nudged right, and the panel showed no size.  Four causes:

* `_safe_rl_font` listed "century" as a serif hint, so Century Gothic (a
  geometric sans) mapped to Times;
* the bold cut-off was weight >= 600, and pdfium ESTIMATES the weight from
  StemV when the descriptor has no /FontWeight — the regular face read 600;
* on a quarter-turned page apply_edits anchored the replacement at the
  min/max-normalised raw box corner, which is the glyphs' TOP in display;
* `size` was never re-measured after orienting, so it held the run's width.

And the replacement is now set in the document's OWN embedded font when that
font has every glyph needed (`_NativeFont`), falling back to the mapped bundled
face only when it does not — what Acrobat does.

The fixture is synthetic: the field document is a client's drawing.
"""
import io
import os

import _bootstrap  # noqa: F401

import pypdf
import pypdfium2 as pdfium
import pytest

import entry
from routes import editor as _editor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, 'assets', 'fonts', 'Carlito-Regular.ttf')


@pytest.fixture(scope='module', autouse=True)
def _heavy():
    _editor._heavy_init()


def _rotated_title_block():
    """A landscape-reading page stored portrait with /Rotate 90, its text drawn
    rotated in content space so it reads upright on screen — how CAD plots
    come out.  Carlito is embedded as a SUBSET (reportlab), with ToUnicode."""
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    pdfmetrics.registerFont(TTFont('PWTestCarlito', FONT))
    buf = io.BytesIO()
    W, H = 600, 800                       # raw (portrait) MediaBox
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setFont('PWTestCarlito', 20)
    for x, txt in ((100, 'FOOTING - 5 SHEETS'), (160, '0123456789 ABC')):
        c.saveState()
        c.translate(x, 100)
        c.rotate(90)                      # reads upright once /Rotate 90 applies
        c.drawString(0, 0, txt)
        c.restoreState()
    c.showPage(); c.save()
    r = pypdf.PdfReader(io.BytesIO(buf.getvalue()))
    w = pypdf.PdfWriter(); w.add_page(r.pages[0])
    w.pages[0].rotate(90)
    out = io.BytesIO(); w.write(out)
    return out.getvalue()


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _session(pdf):
    entry.sessions['nativefont'] = {
        'pages': [{'id': 'pg1', 'filename': 't.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': pdf}],
        'comments': {}}
    return 'nativefont'


def _items(client, sid):
    return _post(client, '/api/viewer/page_elements',
                 {'session_id': sid, 'page_id': 'pg1'}).get_json()['items']


def _page_fonts(pdf):
    page = pypdf.PdfReader(io.BytesIO(pdf)).pages[0]
    return sorted(str(f.get_object()['/BaseFont'])
                  for f in page['/Resources']['/Font'].values())


def _fonts_used_for(pdf, ch):
    doc = pdfium.PdfDocument(pdf)
    try:
        tp = doc[0].get_textpage(); t = tp.get_text_range()
        import ctypes, pypdfium2.raw as raw
        names = []
        for i, c in enumerate(t):
            if c != ch:
                continue
            fl = ctypes.c_int(0)
            n = raw.FPDFText_GetFontInfo(tp, i, None, 0, ctypes.byref(fl))
            b = ctypes.create_string_buffer(n)
            raw.FPDFText_GetFontInfo(tp, i, b, n, ctypes.byref(fl))
            names.append(b.raw[:n - 1].decode())
        return names
    finally:
        doc.close()


# ── font mapping ─────────────────────────────────────────────────────────────

def test_century_gothic_is_a_sans_not_times():
    assert _editor._safe_rl_font('GMNPDC+CenturyGothic', 131078, 600) == 'Arial'
    assert _editor._safe_rl_font('CenturyGothic,Bold', 6, 765) == 'Arial-Bold'
    # Century Schoolbook really is a serif.
    assert _editor._safe_rl_font('CenturySchoolbook', 0, 400) == 'TimesNR'


def test_an_estimated_600_weight_is_not_bold():
    assert _editor._safe_rl_font('SomeFont', 0, 600) == 'Helvetica'
    assert _editor._safe_rl_font('SomeFont', 0, 700) == 'Helvetica-Bold'
    assert _editor._safe_rl_font('Arial-SemiBold', 0, 600) == 'Arial-Bold'


# ── the edit itself ──────────────────────────────────────────────────────────

def test_rotated_edit_keeps_baseline_size_and_own_font(client):
    pdf = _rotated_title_block()
    sid = _session(pdf)
    items = _items(client, sid)
    five = next(i for i in items if i['text'] == '5')
    # Size is the upright glyph HEIGHT, not the run's width.
    assert five['size'] > (five['x2'] - five['x']), five
    r = _post(client, '/api/viewer/edit_text',
              {'session_id': sid, 'page_id': 'pg1', 'id': five['id'],
               'op': 'text', 'text': '6', 'expect': '5'})
    assert r.status_code == 200, r.get_json()
    six = next(i for i in _items(client, sid) if i['text'] == '6'
               and abs(i['x'] - five['x']) < 20 and abs(i['y'] - five['y']) < 20)
    # Same line: was one glyph height too high before the fix.
    assert abs(six['y'] - five['y']) < 1.5, (five, six)
    assert abs(six['x'] - five['x']) < 2.0, (five, six)
    # Set in the document's own (embedded subset) font: same font object, and
    # nothing new embedded alongside it.
    pdf2 = entry.sessions[sid]['pages'][0]['pdf_bytes']
    assert _page_fonts(pdf2) == _page_fonts(pdf), (_page_fonts(pdf), _page_fonts(pdf2))
    assert six['font'] == five['font'], (five['font'], six['font'])


def test_missing_glyph_falls_back_to_the_mapped_face(client):
    pdf = _rotated_title_block()
    sid = _session(pdf)
    five = next(i for i in _items(client, sid) if i['text'] == '5')
    # reportlab's subset carries ASCII only, so it has no 'é'.
    r = _post(client, '/api/viewer/edit_text',
              {'session_id': sid, 'page_id': 'pg1', 'id': five['id'],
               'op': 'text', 'text': 'é', 'expect': '5'})
    assert r.status_code == 200, r.get_json()
    pdf2 = entry.sessions[sid]['pages'][0]['pdf_bytes']
    assert len(_page_fonts(pdf2)) == len(_page_fonts(pdf)) + 1, _page_fonts(pdf2)
    e = next(i for i in _items(client, sid) if i['text'] == 'é')
    assert abs(e['y'] - five['y']) < 3.0, (five, e)


def test_choosing_a_font_opts_out_of_the_document_font(client):
    pdf = _rotated_title_block()
    sid = _session(pdf)
    five = next(i for i in _items(client, sid) if i['text'] == '5')
    r = _post(client, '/api/viewer/edit_text',
              {'session_id': sid, 'page_id': 'pg1', 'id': five['id'],
               'op': 'style', 'rl_font': 'TimesNR-Bold', 'expect': '5'})
    assert r.status_code == 200, r.get_json()
    pdf2 = entry.sessions[sid]['pages'][0]['pdf_bytes']
    assert any('Serif' in n or 'Times' in n for n in _fonts_used_for(pdf2, '5')), \
        _fonts_used_for(pdf2, '5')


# ── restyling (v3.4) ─────────────────────────────────────────────────────────

def test_restyle_leaves_one_run_and_it_is_the_new_one(client):
    """A restyle keeps the words, so the painted-over original also matched
    the replacement's text and BOTH were offered at the same spot — clicking
    could pick the hidden original and the panel showed its old weight."""
    pdf = _rotated_title_block()
    sid = _session(pdf)
    five = next(i for i in _items(client, sid) if i['text'] == '5')
    r = _post(client, '/api/viewer/edit_text',
              {'session_id': sid, 'page_id': 'pg1', 'id': five['id'],
               'op': 'style', 'rl_font': 'Arial-Bold', 'expect': '5'})
    assert r.status_code == 200, r.get_json()
    fives = [i for i in _items(client, sid) if i['text'] == '5'
             and abs(i['x'] - five['x']) < 10 and abs(i['y'] - five['y']) < 10]
    assert len(fives) == 1, fives
    assert fives[0]['rl_font'] == 'Arial-Bold', fives


def test_an_explicit_size_is_not_shrunk_to_fit(client):
    pdf = _rotated_title_block()
    sid = _session(pdf)
    five = next(i for i in _items(client, sid) if i['text'] == '5')
    r = _post(client, '/api/viewer/edit_text',
              {'session_id': sid, 'page_id': 'pg1', 'id': five['id'],
               'op': 'style', 'size': 30, 'expect': '5'})
    body = r.get_json()
    assert r.status_code == 200, body
    assert 'shrunk_to_fit' not in body and 'too_long_for_space' not in body, body
    big = [i for i in _items(client, sid) if i['text'] == '5'
           and abs(i['x'] - five['x']) < 10]
    assert big and abs(big[0]['font_size'] - 30) < 0.5, big
