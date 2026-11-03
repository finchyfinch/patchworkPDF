"""The viewer's Page text / Text Box tools.

These write into the page's CONTENT, which is what separates them from the
viewer's own Text annotation: a FreeText annotation is an object laid on top of
the page and can be selected and deleted afterwards; this is indistinguishable
from the document's own text once placed.  The tests that matter therefore check
the text is really *in the page* — extractable by the same text layer the
document's own words come from — not merely drawn over it.

Two shapes, one route:

  single line  x,y is the text BASELINE.
  text box     x,y is the box's TOP-LEFT, and `lines` carries the text already
               wrapped to the box by the client, because only the client can
               measure the font it is previewing with.  A missing or unusable
               wrap must still place something rather than silently nothing.

Like every other page tool it takes client coordinates, so it inherits the
cropped+rotated refusal.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pikepdf
import pypdfium2 as pdfium
import pytest

import entry

_MB = (0, 0, 400, 400)
_CB = (50, 80, 350, 300)


def _page(cropped=False, rotate=0):
    pdf = pikepdf.new()
    d = dict(Type=pikepdf.Name.Page, MediaBox=pikepdf.Array(list(_MB)),
             Resources=pikepdf.Dictionary(),
             Contents=pdf.make_indirect(pikepdf.Stream(
                 pdf, b'1 1 1 rg 0 0 400 400 re f')))
    if cropped:
        d['CropBox'] = pikepdf.Array(list(_CB))
    if rotate:
        d['Rotate'] = rotate
    pdf.pages.append(pikepdf.Page(pdf.make_indirect(pikepdf.Dictionary(**d))))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _page_text(pdf_bytes):
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return doc[0].get_textpage().get_text_range()
    finally:
        doc.close()


def _char_boxes(pdf_bytes):
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        tp = doc[0].get_textpage()
        return [tp.get_charbox(i) for i in range(tp.count_chars())]
    finally:
        doc.close()


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _session(pdf_bytes, sid='addtext'):
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 't.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': pdf_bytes}],
        'comments': {},
    }
    return sid


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _add(client, sid, **item):
    base = {'text': 'Hello', 'x': 60, 'y': 200, 'size': 24,
            'color': '#000000', 'rl_font': 'Helvetica'}
    base.update(item)
    return _post(client, '/api/viewer/add_text',
                 {'session_id': sid, 'page_id': 'pg1', 'item': base})


def _bytes(sid):
    return entry.sessions[sid]['pages'][0]['pdf_bytes']


# ── it is page content, not an annotation ───────────────────────────────────

def test_placed_text_is_part_of_the_page_text_layer(client):
    """The whole point of this tool: the words become the document's words."""
    sid = _session(_page())
    try:
        assert _page_text(_bytes(sid)).strip() == ''
        assert _add(client, sid, text='Invoice total').status_code == 200
        assert 'Invoice total' in _page_text(_bytes(sid))

        # ...and it is NOT an annotation, which is what the Text *note* tool makes.
        import pypdf
        annots = pypdf.PdfReader(io.BytesIO(_bytes(sid))).pages[0].get('/Annots')
        assert not annots, 'page text must not be written as an annotation'
    finally:
        entry.sessions.pop(sid, None)


def test_text_lands_where_it_was_placed(client):
    sid = _session(_page())
    try:
        _add(client, sid, text='X', x=120, y=250, size=36)
        boxes = _char_boxes(_bytes(sid))
        assert boxes, 'no characters found'
        left, bottom, _right, _top = boxes[0]
        assert abs(left - 120) < 6, f'x off: {left}'
        assert abs(bottom - 250) < 12, f'baseline off: {bottom}'
    finally:
        entry.sessions.pop(sid, None)


def test_text_on_a_cropped_page_uses_the_visible_frame(client):
    """Display-frame coordinates: the server adds the CropBox origin."""
    sid = _session(_page(cropped=True))
    try:
        _add(client, sid, text='X', x=20, y=30, size=24)
        left, bottom, _r, _t = _char_boxes(_bytes(sid))[0]
        # Absolute position should be the visible-frame one plus the origin.
        assert abs(left - (20 + _CB[0])) < 6, f'x={left}'
        assert abs(bottom - (30 + _CB[1])) < 12, f'y={bottom}'
    finally:
        entry.sessions.pop(sid, None)


def test_text_works_on_rotated_pages(client):
    for rot in (90, 180, 270):
        sid = _session(_page(rotate=rot), f'addtext-rot{rot}')
        try:
            r = _add(client, sid, text='Rotated')
            assert r.status_code == 200, f'/Rotate {rot}: {r.get_data(as_text=True)}'
            assert 'Rotated' in _page_text(_bytes(sid)), f'/Rotate {rot}'
        finally:
            entry.sessions.pop(f'addtext-rot{rot}', None)


# ── the text box ────────────────────────────────────────────────────────────

def test_a_text_box_places_every_wrapped_line(client):
    sid = _session(_page())
    try:
        r = _add(client, sid, text='one two three', multiline=True,
                 boxW=200, boxH=90, x=40, y=300,
                 lines=['one', 'two', 'three'])
        assert r.status_code == 200
        got = _page_text(_bytes(sid))
        for word in ('one', 'two', 'three'):
            assert word in got, f'{word!r} missing from {got!r}'
    finally:
        entry.sessions.pop(sid, None)


def test_text_box_lines_stack_downwards(client):
    sid = _session(_page())
    try:
        _add(client, sid, text='A\nB', multiline=True, boxW=200, boxH=90,
             x=40, y=300, size=20, lines=['A', 'B'])
        boxes = _char_boxes(_bytes(sid))
        assert len(boxes) >= 2
        # PDF y is up, so the second line must sit LOWER than the first.
        assert boxes[1][1] < boxes[0][1], f'lines did not stack: {boxes[:2]}'
    finally:
        entry.sessions.pop(sid, None)


def test_a_text_box_with_no_wrap_still_places_the_text(client):
    """A client that sends no `lines` must not silently place nothing."""
    sid = _session(_page())
    try:
        r = _add(client, sid, text='unwrapped words', multiline=True,
                 boxW=200, boxH=60, x=40, y=300)
        assert r.status_code == 200
        assert 'unwrapped' in _page_text(_bytes(sid))
    finally:
        entry.sessions.pop(sid, None)


# ── guards ──────────────────────────────────────────────────────────────────

def test_empty_text_is_rejected_and_changes_nothing(client):
    sid = _session(_page())
    try:
        before = _bytes(sid)
        for txt in ('', '   '):
            r = _add(client, sid, text=txt)
            assert r.status_code == 400, repr(txt)
            assert (r.get_json() or {}).get('error_key') == 'err.no_text'
        assert _bytes(sid) == before
        assert not entry.sessions[sid].get('annot_undo'), \
            'a rejected placement must not spend an undo slot'
    finally:
        entry.sessions.pop(sid, None)


def test_cropped_and_rotated_is_refused(client):
    for rot in (90, 180, 270):
        sid = _session(_page(cropped=True, rotate=rot), f'addtext-cr{rot}')
        try:
            before = _bytes(sid)
            r = _add(client, sid)
            assert r.status_code == 400, f'/Rotate {rot}'
            assert (r.get_json() or {}).get('error_key') == 'err.cropped_and_rotated'
            assert _bytes(sid) == before
        finally:
            entry.sessions.pop(f'addtext-cr{rot}', None)


def test_a_silly_size_is_clamped_rather_than_refused(client):
    sid = _session(_page())
    try:
        assert _add(client, sid, size=1e9).status_code == 200
        assert 'Hello' in _page_text(_bytes(sid))
    finally:
        entry.sessions.pop(sid, None)


def test_a_bad_colour_does_not_reach_the_content_stream(client):
    sid = _session(_page())
    try:
        r = _add(client, sid, color='1 0 0 rg 0 0 999 999 re f')
        assert r.status_code == 200
        doc = pdfium.PdfDocument(_bytes(sid))
        try:
            img = doc[0].render(scale=1.0).to_pil().convert('RGB')
        finally:
            doc.close()
        px = img.load()
        r0, g0, b0 = px[5, 5]
        assert not (r0 > 200 and g0 < 80 and b0 < 80), 'colour was not sanitised'
    finally:
        entry.sessions.pop(sid, None)


def test_unknown_page_is_reported(client):
    sid = _session(_page())
    try:
        r = _post(client, '/api/viewer/add_text',
                  {'session_id': sid, 'page_id': 'nope',
                   'item': {'text': 'x', 'x': 1, 'y': 1}})
        assert r.status_code == 404
    finally:
        entry.sessions.pop(sid, None)


# ── undo ────────────────────────────────────────────────────────────────────

def test_placed_text_is_one_undo_step(client):
    sid = _session(_page())
    try:
        before = _bytes(sid)
        r = _add(client, sid, text='Removable')
        assert r.get_json()['undo_count'] == 1
        assert 'Removable' in _page_text(_bytes(sid))

        u = _post(client, '/api/viewer/undo', {'session_id': sid})
        assert u.status_code == 200
        assert _bytes(sid) == before
        assert 'Removable' not in _page_text(_bytes(sid))
    finally:
        entry.sessions.pop(sid, None)


def test_placing_text_marks_the_document_dirty(client):
    sid = _session(_page())
    try:
        _add(client, sid)
        from bootstrap import recovery as _recovery
        assert _recovery.has_pending(entry.sessions[sid])
    finally:
        entry.sessions.pop(sid, None)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
