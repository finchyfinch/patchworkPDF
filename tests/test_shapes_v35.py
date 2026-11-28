"""Line ends, Polygon / Polyline (with the cloud style) and the Pen (v3.5).

* Line: a style per end (none / open / closed / circle / square / slash),
  written as the standard /LE names; the old arrow flags still work, and a
  start-only arrow lifts with its ends as they are (no endpoint flip).
* Polygon / PolyLine from vertices; a cloud is a border style (/IT
  /PolygonCloud + /BE /S /C) whose bumps face OUTWARD whichever way round the
  vertices were clicked; a rectangle in Cloud style is the old rectangle cloud
  and lifts as a Rectangle in Cloud style.
* Ink: pen strokes, and the translucent multiplied highlighter.
* All of them round-trip their geometry through the display frame on a
  /Rotate 90 page, and render where they were drawn.
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
    s = _session('shapes35', _page_bytes())
    yield s
    entry.sessions.pop(s, None)


def _post(client, path, body, status=200):
    r = client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})
    assert r.status_code == status, r.get_json()
    return r.get_json()


def _add(client, sid, status=200, **body):
    return _post(client, '/api/viewer/add_annotation',
                 {'session_id': sid, 'page_id': 'pg1', 'border_color': [1, 0, 0],
                  'border_width': 2, **body}, status=status)


def _bytes(sid):
    return entry.sessions[sid]['pages'][0]['pdf_bytes']


def _annots(sid):
    page = pypdf.PdfReader(io.BytesIO(_bytes(sid))).pages[0]
    return [a.get_object() for a in (page.get('/Annots') or [])]


def _render(pdf, scale=2.0):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        pg = doc[0]
        img = pg.render(scale=scale, may_draw_forms=True).to_pil().convert('RGB')
        return img, pg.get_height()
    finally:
        doc.close()


def _red(img, H, scale, x1, y1, x2, y2):
    """Fraction of reddish pixels in a display-frame box (PDF y-up)."""
    px1, px2 = int(x1 * scale), int(x2 * scale)
    py1, py2 = int((H - y2) * scale), int((H - y1) * scale)
    n = hit = 0
    for x in range(px1, px2):
        for y in range(py1, py2):
            r, g, b = img.getpixel((x, y))
            n += 1
            hit += (r > 150 and g < 120 and b < 120)
    return hit / max(1, n)


# ── Line ends ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('end, name', [('open', '/OpenArrow'), ('closed', '/ClosedArrow'),
                                        ('circle', '/Circle'), ('square', '/Square'),
                                        ('slash', '/Slash')])
def test_each_line_end_is_written_and_drawn(client, sid, end, name):
    _add(client, sid, subtype='Line', line=[100, 400, 300, 400], line_ends=['none', end])
    (a,) = _annots(sid)
    assert [str(v) for v in a['/LE']] == ['/None', name]
    img, H = _render(_bytes(sid))
    assert _red(img, H, 2, 294, 393, 306, 407) > 0.12, 'something drawn at the end'
    assert _red(img, H, 2, 94, 393, 99, 407) < 0.02, 'nothing at the plain start'
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['tool'] == 'line' and info['line_ends'] == ['none', end]


def test_closed_arrow_is_filled_open_is_not(client, sid):
    _add(client, sid, subtype='Line', line=[100, 400, 300, 400], line_ends=['open', 'closed'],
         border_width=1)
    img, H = _render(_bytes(sid))
    filled = _red(img, H, 2, 292, 397, 297, 403)     # inside the closed head
    hollow = _red(img, H, 2, 103, 403.5, 108, 406)   # between the open barbs, off the line
    assert filled > 0.6 and hollow < 0.3


def test_old_arrow_flags_still_work(client, sid):
    _add(client, sid, subtype='Line', line=[100, 400, 300, 400], arrow=True, arrow_start=True)
    assert [str(v) for v in _annots(sid)[0]['/LE']] == ['/OpenArrow', '/OpenArrow']


def test_start_only_arrow_lifts_with_its_ends_unflipped(client, sid):
    _add(client, sid, subtype='Line', line=[100, 400, 300, 400], line_ends=['open', 'none'])
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['line'] == [100, 400, 300, 400]
    assert info['line_ends'] == ['open', 'none']


# ── Polygon / Polyline ───────────────────────────────────────────────────────

TRI = [100, 400, 300, 400, 200, 560]


def test_polygon_round_trips_with_fill(client, sid):
    _add(client, sid, subtype='Polygon', vertices=TRI, fill_color=[1, 1, 0])
    (a,) = _annots(sid)
    assert a['/Subtype'] == '/Polygon' and '/IT' not in a
    assert [float(v) for v in a['/Vertices']] == TRI
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['tool'] == 'polygon' and info['closed'] is True
    assert info['vertices'] == TRI and info['fill_color'] == [1, 1, 0]
    img, H = _render(_bytes(sid))
    r, g, b = img.getpixel((int(200 * 2), int((H - 450) * 2)))
    assert r > 200 and g > 200 and b < 80, 'filled yellow inside'


def test_polyline_with_ends(client, sid):
    _add(client, sid, subtype='PolyLine', vertices=[100, 400, 200, 500, 300, 400],
         line_ends=['none', 'closed'])
    (a,) = _annots(sid)
    assert a['/Subtype'] == '/PolyLine'
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['tool'] == 'polygon' and info['closed'] is False
    assert info['line_ends'] == ['none', 'closed']
    img, H = _render(_bytes(sid))
    assert _red(img, H, 2, 145, 445, 155, 455) > 0.2, 'first leg drawn'
    assert _red(img, H, 2, 190, 410, 210, 420) < 0.01, 'not closed'


@pytest.mark.parametrize('verts', [TRI, TRI[4:] + TRI[2:4] + TRI[:2]])   # CCW, CW
def test_cloud_bumps_face_outward_either_way_round(client, sid, verts):
    _add(client, sid, subtype='Polygon', vertices=verts, border_style='cloud')
    (a,) = _annots(sid)
    assert a['/IT'] == '/PolygonCloud' and a['/BE']['/S'] == '/C'
    img, H = _render(_bytes(sid))
    # The bottom edge runs y=400 from x=100..300: outward is DOWN (below 400).
    below = _red(img, H, 2, 130, 391, 270, 399)
    above = _red(img, H, 2, 130, 403, 270, 411)
    assert below > above + 0.03, f'bumps outside the shape (below {below:.3f}, above {above:.3f})'
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['tool'] == 'polygon' and info['border_style'] == 'cloud'


def test_rectangle_cloud_style_is_the_old_cloud(client, sid):
    _add(client, sid, subtype='Square', rect=[100, 400, 300, 500], border_style='cloud')
    (a,) = _annots(sid)
    assert a['/Subtype'] == '/Polygon' and a['/IT'] == '/PolygonCloud'
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['tool'] == 'rect' and info['border_style'] == 'cloud'
    assert info['rect'] == [100, 400, 300, 500]


def test_too_few_points_are_refused(client, sid):
    r = _add(client, sid, status=400, subtype='Polygon', vertices=[1, 2, 3, 4])
    assert r['error_key'] == 'err.not_enough_points'
    assert _annots(sid) == []


# ── Pen ──────────────────────────────────────────────────────────────────────

STROKE = [100, 300, 150, 320, 200, 300, 250, 320]


def test_pen_strokes_round_trip_and_draw(client, sid):
    _add(client, sid, subtype='Ink', ink=[STROKE, [400, 300, 420, 340]])
    (a,) = _annots(sid)
    assert a['/Subtype'] == '/Ink'
    assert [[float(v) for v in st] for st in a['/InkList']] == [STROKE, [400, 300, 420, 340]]
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['tool'] == 'pen' and info['highlighter'] is False
    assert info['strokes'][0] == STROKE
    img, H = _render(_bytes(sid))
    assert _red(img, H, 2, 120, 302, 130, 318) > 0.1, 'stroke drawn'


def test_highlighter_is_translucent_and_listed_as_such(client, sid):
    data = _add(client, sid, subtype='Ink', ink=[STROKE], highlighter=True,
                border_color=[1, 0.92, 0], border_width=12)
    (a,) = _annots(sid)
    assert float(a['/CA']) == pytest.approx(0.35)
    assert data['annotations'][0]['kind'] == 'highlighter'
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['highlighter'] is True


# ── rotated page ─────────────────────────────────────────────────────────────

def test_vertices_and_strokes_round_trip_on_a_rotated_page(client):
    s = _session('shapes35-rot', _page_bytes(rotate=90))
    try:
        _add(client, s, subtype='Polygon', vertices=TRI)
        _add(client, s, subtype='Ink', ink=[STROKE])
        poly, ink = V._get_editable_annotations(_bytes(s))
        assert [round(v, 3) for v in poly['vertices']] == TRI
        assert [round(v, 3) for v in ink['strokes'][0]] == STROKE
        img, H = _render(_bytes(s))
        assert _red(img, H, 2, 140, 398, 260, 402) > 0.3, 'bottom edge where drawn'
    finally:
        entry.sessions.pop(s, None)
