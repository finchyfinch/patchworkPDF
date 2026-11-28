"""Text markup on selected text: Highlight / Underline / Strikethrough / Squiggly
(v3.5).

A text selection marks up one quad per selected line (/QuadPoints — the shape
Acrobat writes and reads), not one box.  These pin:

* each kind is written as the standard subtype with one quad per line, our
  /PWKind /TextMarkup marker, a default colour per kind and the marked words in
  /Contents (so the Comments list says what was marked);
* the appearance draws where the words are — the highlight over the line,
  the underline under it, the strikeout through it — on a plain page, a
  /Rotate 90 page and a cropped page (the display-frame → raw conversion);
* the viewer gets them back as `textmarkup` with display-frame quads, so it
  selects them as text markup instead of lifting them into a movable box —
  while the box Highlight tool (no quads) still lifts as a box;
* restyle (colour / kind) edits the same object in place, as one undo step,
  and refuses anything that is not text markup.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pypdf
import pytest

import entry
from routes import viewer as V


def _page_bytes(width=612, height=792, rotate=0, crop=None):
    w = pypdf.PdfWriter()
    pg = w.add_blank_page(width=width, height=height)
    if rotate:
        pg.rotate(rotate)
    if crop:
        pg.cropbox = pypdf.generic.RectangleObject(crop)
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
    s = _session('text-markup', _page_bytes())
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


# Two lines of "text": display-frame boxes, PDF y-up.
LINES = [[72, 700, 300, 714], [72, 682, 240, 696]]


def _mark(client, sid, kind, quads=LINES, **extra):
    return _post(client, '/api/viewer/add_annotation',
                 {'session_id': sid, 'page_id': 'pg1', 'subtype': kind,
                  'quads': quads, 'text': 'the marked words', 'author': 'GF',
                  **extra})


def _render(pdf, scale=2.0):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        pg = doc[0]
        img = pg.render(scale=scale, may_draw_forms=True).to_pil().convert('RGB')
        return img, pg.get_height()
    finally:
        doc.close()


def _ink(img, H, scale, x1, y1, x2, y2):
    """Fraction of non-white pixels in a display-frame box (PDF y-up)."""
    px1, px2 = int(x1 * scale), int(x2 * scale)
    py1, py2 = int((H - y2) * scale), int((H - y1) * scale)
    n = hit = 0
    for x in range(px1, px2):
        for y in range(py1, py2):
            r, g, b = img.getpixel((x, y))
            n += 1
            hit += (r + g + b) < 720
    return hit / max(1, n)


# ── written as the standard annotation ──────────────────────────────────────

@pytest.mark.parametrize('kind', V._TEXT_MARKUP)
def test_each_kind_is_written_with_one_quad_per_line(client, sid, kind):
    _mark(client, sid, kind)
    (a,) = _annots(sid)
    assert a['/Subtype'] == '/' + kind
    qp = [float(v) for v in a['/QuadPoints']]
    assert len(qp) == 16, 'one quad (8 numbers) per selected line'
    # TL, TR, BL, BR of the first line
    assert qp[:8] == [72, 714, 300, 714, 72, 700, 300, 700]
    assert a['/PWKind'] == '/TextMarkup'
    assert [float(c) for c in a['/C']] == V._TEXT_MARKUP_DEFAULT_RGB[kind]
    assert a['/Contents'] == 'the marked words'
    assert a['/T'] == 'GF'
    assert '/AP' in a


def test_listed_in_the_comments_panel_with_its_words(client, sid):
    data = _mark(client, sid, 'StrikeOut')
    (item,) = data['annotations']
    assert item['subtype'] == 'StrikeOut' and item['markup'] is True
    assert item['text'] == 'the marked words'


def test_a_selection_with_no_usable_line_is_refused(client, sid):
    r = _post(client, '/api/viewer/add_annotation',
              {'session_id': sid, 'page_id': 'pg1', 'subtype': 'Underline',
               'quads': [[10, 10, 10, 10], ['x', 1, 2, 3]]}, status=400)
    assert r['error_key'] == 'err.no_text_to_mark_up'
    assert _annots(sid) == []


# ── drawn where the words are ───────────────────────────────────────────────

def test_appearances_land_on_the_lines(client, sid):
    for kind in ('Highlight', 'Underline', 'StrikeOut'):
        entry.sessions[sid]['pages'][0]['pdf_bytes'] = _page_bytes()
        _mark(client, sid, kind, quads=[LINES[0]])
        img, H = _render(_bytes(sid))
        x1, y1, x2, y2 = LINES[0]
        if kind == 'Highlight':
            assert _ink(img, H, 2, x1 + 5, y1 + 2, x2 - 5, y2 - 2) > 0.9, 'line covered'
        if kind == 'Underline':
            assert _ink(img, H, 2, x1 + 5, y1, x2 - 5, y1 + 3) > 0.3, 'ink at the bottom'
            assert _ink(img, H, 2, x1 + 5, y1 + 5, x2 - 5, y2 - 2) < 0.02, 'nothing through the words'
        if kind == 'StrikeOut':
            assert _ink(img, H, 2, x1 + 5, y1 + 5, x2 - 5, y1 + 8) > 0.3, 'ink through the middle'
            assert _ink(img, H, 2, x1 + 5, y1, x2 - 5, y1 + 2) < 0.02, 'nothing at the bottom'
        assert _ink(img, H, 2, x2 + 20, y1, x2 + 80, y2) < 0.01, 'nothing past the line end'


def test_rotated_page_round_trips_and_underlines_under_the_words(client):
    # /Rotate 90: the display frame is 792 x 612.  The viewer sends display
    # coords; underline must still sit under the words AS DISPLAYED.
    s = _session('text-markup-rot', _page_bytes(rotate=90))
    try:
        line = [100, 500, 400, 514]
        _mark(client, s, 'Underline', quads=[line])
        (info,) = V._get_editable_annotations(_bytes(s))
        assert info['tool'] == 'textmarkup'
        assert [round(v, 3) for v in info['quads'][0]] == line, 'display frame in = out'
        img, H = _render(_bytes(s))
        x1, y1, x2, y2 = line
        assert _ink(img, H, 2, x1 + 5, y1, x2 - 5, y1 + 3) > 0.3, 'under the displayed words'
        assert _ink(img, H, 2, x1 + 5, y1 + 5, x2 - 5, y2 - 2) < 0.02
    finally:
        entry.sessions.pop(s, None)


def test_cropped_page_round_trips(client):
    s = _session('text-markup-crop', _page_bytes(crop=[100, 150, 512, 700]))
    try:
        line = [20, 300, 200, 312]
        _mark(client, s, 'Highlight', quads=[line])
        (info,) = V._get_editable_annotations(_bytes(s))
        assert [round(v, 3) for v in info['quads'][0]] == line
        img, H = _render(_bytes(s))
        assert _ink(img, H, 2, 25, 302, 195, 310) > 0.9
    finally:
        entry.sessions.pop(s, None)


# ── what the viewer gets back ───────────────────────────────────────────────

def test_viewer_selects_text_markup_but_still_lifts_a_box_highlight(client, sid):
    _mark(client, sid, 'Highlight')
    _post(client, '/api/viewer/add_annotation',
          {'session_id': sid, 'page_id': 'pg1', 'subtype': 'Highlight',
           'rect': [50, 400, 250, 415]})
    tm, box = V._get_editable_annotations(_bytes(sid))
    assert tm['tool'] == 'textmarkup' and tm['markup_kind'] == 'Highlight'
    assert len(tm['quads']) == 2
    assert box['tool'] == 'highlight', 'the box tool is unchanged'


# ── restyle in place ────────────────────────────────────────────────────────

def _restyle(client, sid, **body):
    return _post(client, '/api/viewer/restyle_text_markup',
                 {'session_id': sid, 'page_id': 'pg1', 'annot_idx': 0, **body},
                 status=body.pop('_status', 200) if '_status' in body else 200)


def test_recolour_is_in_place_and_one_undo_step(client, sid):
    _mark(client, sid, 'Highlight')
    nm = _annots(sid)[0]['/NM']
    before = _bytes(sid)
    data = _restyle(client, sid, color=[0.2, 0.8, 0.3])
    (a,) = _annots(sid)
    assert a['/NM'] == nm, 'same object, not a delete + add'
    assert [round(float(c), 3) for c in a['/C']] == [0.2, 0.8, 0.3]
    assert data['editable'][0]['border_color'] == pytest.approx([0.2, 0.8, 0.3])
    _post(client, '/api/viewer/undo', {'session_id': sid})
    assert _bytes(sid) == before, 'one Ctrl+Z restores the previous colour'


def test_change_of_kind_takes_that_kinds_colour(client, sid):
    _mark(client, sid, 'Highlight')
    _restyle(client, sid, kind='StrikeOut')
    (a,) = _annots(sid)
    assert a['/Subtype'] == '/StrikeOut'
    assert [float(c) for c in a['/C']] == V._TEXT_MARKUP_DEFAULT_RGB['StrikeOut']
    assert len([float(v) for v in a['/QuadPoints']]) == 16, 'quads kept'


def test_restyle_refuses_what_is_not_text_markup(client, sid):
    _post(client, '/api/viewer/add_annotation',
          {'session_id': sid, 'page_id': 'pg1', 'subtype': 'Square',
           'rect': [50, 50, 150, 120]})
    before = _bytes(sid)
    _post(client, '/api/viewer/restyle_text_markup',
          {'session_id': sid, 'page_id': 'pg1', 'annot_idx': 0, 'kind': 'Underline'},
          status=400)
    _post(client, '/api/viewer/restyle_text_markup',
          {'session_id': sid, 'page_id': 'pg1', 'annot_idx': 0, 'kind': 'Ink'},
          status=400)
    assert _bytes(sid) == before
