"""Edit text's "Read text in images" (v3.4) — the old Editor's Force OCR,
redone so it cannot lose anything.

Force OCR replaced a page's real text with OCR words.  This keeps every native
run untouched and only ADDS the OCR words found where there is no real text —
the words inside a screenshot on an otherwise native page (the manual's
"Riverton Office Fit-out" cover).  It is opt-in per page, session-only, and
refused cleanly when Tesseract is not installed (an optional install).
"""
import io
import os

import _bootstrap  # noqa: F401

import pytest

import entry
from routes import editor as _editor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module', autouse=True)
def _heavy():
    _editor._heavy_init()


def _page_with_picture():
    """Native heading + a raster picture that says 'RIVERTON FITOUT'."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    font = ImageFont.truetype(os.path.join(ROOT, 'assets', 'fonts',
                                           'LiberationSans-Bold.ttf'), 64)
    im = Image.new('RGB', (900, 160), (255, 255, 255))
    ImageDraw.Draw(im).text((30, 40), 'RIVERTON FITOUT', fill=(20, 40, 90), font=font)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.setFont('Helvetica', 18)
    c.drawString(72, 760, 'Native heading text')
    c.drawImage(ImageReader(im), 72, 500, width=450, height=80)
    c.showPage(); c.save()
    return buf.getvalue()


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _session(sid):
    entry.sessions[sid] = {'pages': [{'id': 'pg1', 'filename': 't.pdf', 'page_num': 1,
                                      'total_pages': 1, 'thumbnail': '',
                                      'pdf_bytes': _page_with_picture()}],
                           'comments': {}}
    return sid


def _load(client, sid, **kw):
    return _post(client, '/api/viewer/page_elements',
                 dict(session_id=sid, page_id='pg1', **kw)).get_json()


def test_reading_images_adds_words_and_keeps_native_text(client):
    if not _editor._OCR_AVAILABLE:
        pytest.skip('Tesseract not installed')
    sid = _session('readimgA')
    before = _load(client, sid)
    native = sorted(i['text'] for i in before['items'])
    assert native == ['Native', 'heading', 'text']
    assert before['can_read_images'] is True and before['ocr_words'] == 0
    after = _load(client, sid, read_images=True)
    texts = [i['text'] for i in after['items']]
    for w in native:                                   # real text untouched…
        assert texts.count(w) == 1, texts              # …and not duplicated
    assert 'RIVERTON' in texts and 'FITOUT' in texts, texts
    assert after['ocr_words'] >= 2 and after['can_read_images'] is False
    # Stays on for the session: an ordinary reload still has the words.
    again = [i['text'] for i in _load(client, sid)['items']]
    assert 'RIVERTON' in again


def test_a_word_from_a_picture_can_be_edited(client):
    if not _editor._OCR_AVAILABLE:
        pytest.skip('Tesseract not installed')
    sid = _session('readimgB')
    items = _load(client, sid, read_images=True)['items']
    w = next(i for i in items if i['text'] == 'FITOUT')
    r = _post(client, '/api/viewer/edit_text',
              {'session_id': sid, 'page_id': 'pg1', 'id': w['id'],
               'op': 'text', 'text': 'REFIT', 'expect': 'FITOUT'})
    assert r.status_code == 200, r.get_json()
    texts = [i['text'] for i in _load(client, sid)['items']]
    assert 'REFIT' in texts and 'FITOUT' not in texts, texts
    assert 'heading' in texts and 'RIVERTON' in texts


def test_refused_without_tesseract(client, monkeypatch):
    monkeypatch.setattr(_editor, '_OCR_AVAILABLE', False)
    sid = _session('readimgC')
    assert _load(client, sid)['can_read_images'] is False
    r = _post(client, '/api/viewer/page_elements',
              {'session_id': sid, 'page_id': 'pg1', 'read_images': True})
    assert r.status_code == 400
    assert r.get_json()['error_key'] == 'err.ocr_unavailable'
    assert not entry.sessions[sid]['pages'][0].get('ocr_source_bytes')
