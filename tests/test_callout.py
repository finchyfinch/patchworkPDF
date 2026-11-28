"""Callout: a text box with a leader line pointing at something (v3.5).

Written as the standard FreeText callout so Acrobat reads it as one:
/IT /FreeTextCallout, /CL (tip, knee, end), /LE /OpenArrow at the tip, and /RD
saying where the text box sits inside the larger /Rect.  These pin:

* the keys, and that the box and tip the viewer sent come back unchanged
  (display frame) on a plain, a /Rotate 90 and a cropped page;
* the leader leaves the box side FACING the tip, and the appearance really
  draws box, leader and arrow where they belong (rendered pixels);
* a callout is markup, never a comment — including one from another producer
  with none of our markers — so the comment index (which the popup's Save /
  Delete walks) skips it and the comment path never rewrites it as a plain box;
* lifting returns tool 'callout' with its box and tip, and the plain Text tool
  is unchanged.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pypdf
import pytest
from pypdf.generic import (ArrayObject, DictionaryObject, FloatObject,
                           NameObject, NumberObject, TextStringObject)

import entry
from routes import viewer as V


def _page_bytes(rotate=0, crop=None):
    w = pypdf.PdfWriter()
    pg = w.add_blank_page(width=612, height=792)
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
    s = _session('callout', _page_bytes())
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


BOX = [300, 600, 450, 640]      # display frame, PDF y-up
TIP = [150, 500]                # below-left of the box


def _callout(client, sid, box=BOX, tip=TIP, **extra):
    return _post(client, '/api/viewer/add_annotation', {
        'session_id': sid, 'page_id': 'pg1', 'subtype': 'FreeText',
        'rect': box, 'callout_tip': tip, 'text': 'Check this',
        'is_comment': False, 'border_color': [0, 0, 1], 'border_width': 1,
        'font_size': 12, **extra})


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
    px1, px2 = int(x1 * scale), int(x2 * scale)
    py1, py2 = int((H - y2) * scale), int((H - y1) * scale)
    n = hit = 0
    for x in range(px1, px2):
        for y in range(py1, py2):
            r, g, b = img.getpixel((x, y))
            n += 1
            hit += (r + g + b) < 700
    return hit / max(1, n)


# ── the leader rule ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('tip, end, knee', [
    ((100, 620), (300, 620), (288, 620)),   # left of the box  -> left side
    ((600, 620), (450, 620), (462, 620)),   # right            -> right side
    ((375, 700), (375, 640), (375, 652)),   # above            -> top
    ((375, 500), (375, 600), (375, 588)),   # below            -> bottom
])
def test_leader_leaves_the_side_facing_the_tip(tip, end, knee):
    assert V._callout_leader(BOX, tip) == (end, knee)


# ── written as the standard callout ──────────────────────────────────────────

def test_written_as_a_standard_freetext_callout(client, sid):
    _callout(client, sid)
    (a,) = _annots(sid)
    assert a['/Subtype'] == '/FreeText' and a['/IT'] == '/FreeTextCallout'
    assert a['/LE'] == '/OpenArrow'
    cl = [float(v) for v in a['/CL']]
    assert cl[:2] == TIP, 'the leader starts at the tip'
    assert len(cl) == 6
    box, tip = V._callout_geometry_raw(a)
    assert [round(v, 3) for v in box] == BOX, '/Rect inset by /RD is the text box'
    assert list(tip) == TIP
    r = [float(v) for v in a['/Rect']]
    assert r[0] < TIP[0] and r[1] < TIP[1], '/Rect covers the tip too'
    assert a['/Contents'] == 'Check this'


def test_appearance_draws_box_leader_and_nothing_stray(client, sid):
    _callout(client, sid, tip=[100, 620])          # tip straight left of the box
    img, H = _render(_bytes(sid))
    # a 1pt leader fills a quarter of this 4pt band, all the way along
    assert _ink(img, H, 2, 150, 618, 280, 622) > 0.2, 'leader across to the tip'
    assert _ink(img, H, 2, 299, 605, 301.5, 635) > 0.3, 'box left edge'
    assert _ink(img, H, 2, 305, 610, 440, 632) > 0.01, 'some text inside'
    assert _ink(img, H, 2, 150, 660, 280, 700) < 0.005, 'nothing above the leader'


@pytest.mark.parametrize('pdf, box, tip', [
    (dict(rotate=90), [300, 400, 450, 440], [150, 300]),
    (dict(crop=[100, 150, 512, 700]), [150, 300, 300, 340], [40, 250]),
])
def test_round_trips_and_renders_on_rotated_and_cropped_pages(client, pdf, box, tip):
    s = _session('callout-x', _page_bytes(**pdf))
    try:
        _callout(client, s, box=box, tip=tip)
        (info,) = [e for e in V._get_editable_annotations(_bytes(s))]
        assert info['tool'] == 'callout'
        assert [round(v, 3) for v in info['rect']] == box, 'box comes back as drawn'
        assert [round(v, 3) for v in info['tip']] == tip, 'tip comes back as drawn'
        img, H = _render(_bytes(s))
        # the box's left edge, as DISPLAYED
        assert _ink(img, H, 2, box[0] - 1, box[1] + 5, box[0] + 1.5, box[3] - 5) > 0.3
    finally:
        entry.sessions.pop(s, None)


# ── markup, not a comment ────────────────────────────────────────────────────

def test_listed_as_a_callout_and_takes_no_comment_index(client, sid):
    _post(client, '/api/viewer/add_annotation', {
        'session_id': sid, 'page_id': 'pg1', 'subtype': 'Text',
        'rect': [50, 700, 68, 718], 'text': 'a real comment'})
    data = _callout(client, sid)
    note, callout = data['annotations']
    assert callout['kind'] == 'callout' and callout['markup'] is True
    assert callout['idx'] is None
    assert note['idx'] == 0


def _foreign_callout_pdf():
    """An Acrobat-style callout: no /PWKind, 4-number /CL, no appearance."""
    w = pypdf.PdfWriter()
    pg = w.add_blank_page(width=612, height=792)
    ann = DictionaryObject({
        NameObject('/Type'): NameObject('/Annot'),
        NameObject('/Subtype'): NameObject('/FreeText'),
        NameObject('/IT'): NameObject('/FreeTextCallout'),
        NameObject('/Rect'): ArrayObject([FloatObject(v) for v in (90, 490, 460, 650)]),
        NameObject('/RD'): ArrayObject([FloatObject(v) for v in (210, 110, 10, 10)]),
        NameObject('/CL'): ArrayObject([FloatObject(v) for v in (100, 500, 300, 620)]),
        NameObject('/Contents'): TextStringObject('from Acrobat'),
        NameObject('/DA'): TextStringObject('/Helv 10 Tf 0 g'),
        NameObject('/F'): NumberObject(4),
    })
    pg[NameObject('/Annots')] = ArrayObject([w._add_object(ann)])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_another_producers_callout_is_markup_and_lifts_as_a_callout(client):
    s = _session('callout-foreign', _foreign_callout_pdf())
    try:
        (item,) = V._get_annotations(_bytes(s))
        assert item['markup'] is True and item['kind'] == 'callout'
        (info,) = V._get_editable_annotations(_bytes(s))
        assert info['tool'] == 'callout'
        assert info['rect'] == [300, 600, 450, 640]
        assert info['tip'] == [100, 500]
        # the comment popup's Save cannot reach it: there is no comment 0
        _post(client, '/api/viewer/update_annotation',
              {'session_id': s, 'page_id': 'pg1', 'annot_idx': 0,
               'text': 'overwritten', 'author': 'x'}, status=404)
        assert _annots(s)[0]['/Contents'] == 'from Acrobat'
    finally:
        entry.sessions.pop(s, None)


def test_plain_text_tool_is_unchanged(client, sid):
    _post(client, '/api/viewer/add_annotation', {
        'session_id': sid, 'page_id': 'pg1', 'subtype': 'FreeText',
        'rect': [50, 600, 250, 640], 'text': 'typed', 'is_comment': False})
    (a,) = _annots(sid)
    assert '/IT' not in a and '/CL' not in a
    (info,) = V._get_editable_annotations(_bytes(sid))
    assert info['tool'] == 'text' and info['is_comment'] is False
