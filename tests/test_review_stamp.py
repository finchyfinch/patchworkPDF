"""Review stamps: APPROVED, REJECTED, ... with name and date (v3.5).

A vector /Stamp (rounded frame + label in Helvetica-Bold fitted to the box +
an optional second line), marked /PWKind /RubberStamp with its text in
/PWStampLabel / /PWStampSub.  These pin:

* the annotation: subtype, standard /Name where one exists, colour, the text
  in /Contents (what Acrobat shows) and our keys (what lifting reads);
* the appearance really draws frame and text inside the box, upright on a
  /Rotate 90 page too, and a long label is fitted, never spilled;
* accented labels (French, German) survive as WinAnsi;
* review stamps are LISTED (review markup) while image and signature stamps,
  which are page content, still are not;
* a review stamp lifts back as tool 'stamp' with its label, line 2 and colour.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pypdf
import pytest

import entry
from routes import viewer as V


def _page_bytes(rotate=0):
    w = pypdf.PdfWriter()
    pg = w.add_blank_page(width=612, height=792)
    if rotate:
        pg.rotate(rotate)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _session(sid, pdf):
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'a.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': pdf}],
        'comments': {},
    }
    return sid


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


@pytest.fixture()
def sid():
    s = _session('review-stamp', _page_bytes())
    yield s
    entry.sessions.pop(s, None)


def _post(client, path, body, status=200):
    r = client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})
    assert r.status_code == status, r.get_json()
    return r.get_json()


def _bytes(sid):
    return entry.sessions[sid]['pages'][0]['pdf_bytes']


def _annots(sid):
    page = pypdf.PdfReader(io.BytesIO(_bytes(sid))).pages[0]
    return [a.get_object() for a in (page.get('/Annots') or [])]


GREEN = [0.12, 0.55, 0.23]
BOX = [100, 600, 260, 648]


def _stamp(client, sid, rect=BOX, **stamp):
    st = {'key': 'approved', 'label': 'APPROVED', 'sub': 'GF - 27 Nov 2026',
          'color': GREEN}
    st.update(stamp)
    return _post(client, '/api/viewer/add_annotation', {
        'session_id': sid, 'page_id': 'pg1', 'subtype': 'Stamp',
        'rect': rect, 'stamp': st, 'author': 'GF'})


def _render(pdf, scale=2.0):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        pg = doc[0]
        img = pg.render(scale=scale, may_draw_forms=True).to_pil().convert('RGB')
        return img, pg.get_height()
    finally:
        doc.close()


def _ink(img, H, scale, x1, y1, x2, y2, green=False):
    """Fraction of inked pixels in a display-frame box; green=True counts only
    pixels whose green channel dominates (the stamp's colour)."""
    px1, px2 = int(x1 * scale), int(x2 * scale)
    py1, py2 = int((H - y2) * scale), int((H - y1) * scale)
    n = hit = 0
    for x in range(px1, px2):
        for y in range(py1, py2):
            r, g, b = img.getpixel((x, y))
            n += 1
            hit += (g > r + 30 and g > b + 30) if green else ((r + g + b) < 700)
    return hit / max(1, n)


def test_written_as_a_vector_stamp_with_its_text(client, sid):
    _stamp(client, sid)
    (a,) = _annots(sid)
    assert a['/Subtype'] == '/Stamp'
    assert a['/Name'] == '/Approved', 'standard name where one exists'
    assert a['/PWKind'] == '/RubberStamp'
    assert a['/PWStampLabel'] == 'APPROVED'
    assert a['/PWStampSub'] == 'GF - 27 Nov 2026'
    assert a['/Contents'] == 'APPROVED - GF - 27 Nov 2026'
    assert [round(float(c), 3) for c in a['/C']] == GREEN
    ap = a['/AP']['/N'].get_object()
    assert '/XObject' not in (ap.get('/Resources') or {}), 'vector, not an image'


def test_a_key_with_no_standard_name_gets_our_own(client, sid):
    _stamp(client, sid, key='for_construction', label='FOR CONSTRUCTION')
    assert _annots(sid)[0]['/Name'] == '/PWForConstruction'


def test_frame_and_text_are_drawn_inside_the_box(client, sid):
    _stamp(client, sid)
    img, H = _render(_bytes(sid))
    x1, y1, x2, y2 = BOX
    assert _ink(img, H, 2, x1 + 12, y1, x2 - 12, y1 + 1.5, green=True) > 0.5, 'bottom edge'
    assert _ink(img, H, 2, x1, y1 + 12, x1 + 1.5, y2 - 12, green=True) > 0.5, 'left edge'
    assert _ink(img, H, 2, x1 + 10, y1 + 22, x2 - 10, y2 - 6, green=True) > 0.08, 'label'
    assert _ink(img, H, 2, x1 + 30, y1 + 6, x2 - 30, y1 + 16) > 0.02, 'second line'
    assert _ink(img, H, 2, x2 + 4, y1, x2 + 60, y2) < 0.001, 'nothing outside'


def test_a_long_label_is_fitted_not_spilled(client, sid):
    _stamp(client, sid, rect=[100, 600, 180, 630], label='FOR INFORMATION ONLY', sub='')
    img, H = _render(_bytes(sid))
    assert _ink(img, H, 2, 184, 595, 260, 640) < 0.001, 'no ink past the right edge'
    assert _ink(img, H, 2, 104, 606, 176, 624, green=True) > 0.05, 'the label is there'


def test_rotated_page_draws_the_stamp_upright_in_place(client):
    s = _session('review-stamp-rot', _page_bytes(rotate=90))
    try:
        box = [300, 400, 460, 448]
        _stamp(client, s, rect=box)
        (info,) = V._get_editable_annotations(_bytes(s))
        assert [round(v, 3) for v in info['rect']] == box
        img, H = _render(_bytes(s))
        x1, y1, x2, y2 = box
        assert _ink(img, H, 2, x1 + 12, y1, x2 - 12, y1 + 1.5, green=True) > 0.5, 'bottom edge as displayed'
        assert _ink(img, H, 2, x1 + 10, y1 + 22, x2 - 10, y2 - 6, green=True) > 0.08
    finally:
        entry.sessions.pop(s, None)


def test_accented_labels_survive(client, sid):
    _stamp(client, sid, key='approved', label='APPROUVÉ', sub='Zoë · 27/11/2026')
    (a,) = _annots(sid)
    assert a['/PWStampLabel'] == 'APPROUVÉ'
    stream = a['/AP']['/N'].get_object().get_data()
    assert b'\\311' in stream, 'É as its WinAnsi code'
    fonts = a['/AP']['/N'].get_object()['/Resources']['/Font']
    assert fonts['/HelvB'].get_object()['/Encoding'] == '/WinAnsiEncoding'


def test_refuses_a_stamp_with_no_text(client, sid):
    r = _post(client, '/api/viewer/add_annotation', {
        'session_id': sid, 'page_id': 'pg1', 'subtype': 'Stamp',
        'rect': BOX, 'stamp': {'key': 'custom', 'label': '   '}}, status=400)
    assert r['error_key'] == 'err.stamp_has_no_text'
    assert _annots(sid) == []


def test_review_stamps_are_listed_image_stamps_are_not(client, sid):
    png = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
    _post(client, '/api/viewer/add_annotation', {
        'session_id': sid, 'page_id': 'pg1', 'subtype': 'Stamp',
        'rect': [50, 50, 100, 100], 'image': 'data:image/png;base64,' + png})
    data = _stamp(client, sid)
    (item,) = data['annotations']
    assert item['kind'] == 'stamp' and item['markup'] is True
    assert item['text'] == 'APPROVED - GF - 27 Nov 2026'


def test_lifts_back_as_a_stamp(client, sid):
    _stamp(client, sid, color=[0.8, 0.1, 0.1], key='rejected', label='REJECTED')
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['tool'] == 'stamp'
    assert info['rect'] == BOX
    assert info['stamp'] == {'key': 'rejected', 'label': 'REJECTED',
                             'sub': 'GF - 27 Nov 2026', 'color': [0.8, 0.1, 0.1]}
