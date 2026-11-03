"""The viewer's Crop tool (W6 — the last of the Phase 1 tools).

Crop is the only page tool that changes the page's SIZE, and that is what makes
it different from the other three:

  * the reply has to carry the new dimensions, because the client cannot lay the
    page out without them;
  * `width_pts` / `height_pts` on the page record have to be rewritten, because
    every page-dict site is required to carry the page's *displayed* size
    (CLAUDE.md, "Viewer request budget") and the viewer fits its opening zoom to
    the widest page from that metadata.  Nothing in the editor's apply path ever
    wrote it back, so a cropped page kept reporting its pre-crop size until the
    document was reopened.

It also exposed a fifth instance of the CropBox/MediaBox conflation, this time
in `_page_dimensions` itself: it measured the MEDIABOX while pdfium renders the
CropBox, so a cropped page was listed at its uncropped size.  The function's own
docstring says "as displayed" and test_rotation.py says the two "have to agree",
so that was a contract violation, not a design choice.  Fixed, and pinned below.

Crop takes client coordinates, so unlike the watermark it DOES need the
cropped+rotated refusal (see tests/test_viewer_redact.py for why that matters).
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pikepdf
import pypdf
import pypdfium2 as pdfium
import pytest

import entry
from pdf.pdf_core import _page_dimensions

_MB = (0, 0, 400, 400)
_CB = (50, 80, 350, 300)


def _page(cropped=False, rotate=0):
    pdf = pikepdf.new()
    # Four coloured corner squares: which ones survive says where the crop went.
    ops = [b'1 1 1 rg 0 0 400 400 re f',
           b'1 0 0 rg 10 10 60 60 re f',        # red    bottom-left
           b'0 1 0 rg 330 10 60 60 re f',       # green  bottom-right
           b'0 0 1 rg 10 330 60 60 re f',       # blue   top-left
           b'1 1 0 rg 330 330 60 60 re f']      # yellow top-right
    d = dict(Type=pikepdf.Name.Page, MediaBox=pikepdf.Array(list(_MB)),
             Resources=pikepdf.Dictionary(),
             Contents=pdf.make_indirect(pikepdf.Stream(pdf, b'\n'.join(ops))))
    if cropped:
        d['CropBox'] = pikepdf.Array(list(_CB))
    if rotate:
        d['Rotate'] = rotate
    pdf.pages.append(pikepdf.Page(pdf.make_indirect(pikepdf.Dictionary(**d))))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _dims(pdf_bytes):
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return doc[0].get_width(), doc[0].get_height()
    finally:
        doc.close()


def _colours(pdf_bytes, scale=2.0):
    """Which corner squares are visible on the rendered (cropped) page."""
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        img = doc[0].render(scale=scale).to_pil().convert('RGB')
    finally:
        doc.close()
    W, H = img.size
    px = img.load()
    seen = set()
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y]
            if r > 180 and g < 90 and b < 90:   seen.add('red')
            elif g > 180 and r < 90 and b < 90: seen.add('green')
            elif b > 180 and r < 90 and g < 90: seen.add('blue')
            elif r > 180 and g > 180 and b < 90: seen.add('yellow')
    return seen


def _cropbox(pdf_bytes):
    cb = pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].cropbox
    return tuple(round(float(v), 1) for v in (cb.left, cb.bottom, cb.right, cb.top))


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _session(pdf_bytes, sid='crop'):
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'c.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': pdf_bytes,
                   'width_pts': 400.0, 'height_pts': 400.0}],
        'comments': {},
    }
    return sid


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _crop(client, sid, rect=None, clear=False):
    body = {'session_id': sid, 'page_id': 'pg1'}
    if clear:
        body['clear'] = True
    else:
        body['rect'] = rect
    return _post(client, '/api/viewer/crop', body)


def _rec(sid):
    return entry.sessions[sid]['pages'][0]


# ── the page-size metadata, which is the point of this tool ─────────────────

def test_page_dimensions_reports_the_cropbox_not_the_mediabox():
    """The contract `_page_dimensions` documents ("as displayed") and that
    test_rotation.py relies on: it must agree with pdfium's own numbers."""
    b = _page(cropped=True)
    assert _page_dimensions(pypdf.PdfReader(io.BytesIO(b)), 0) == _dims(b)
    # And the uncropped case is unchanged — CropBox defaults to MediaBox.
    plain = _page()
    assert _page_dimensions(pypdf.PdfReader(io.BytesIO(plain)), 0) == _dims(plain)


def test_cropping_updates_the_recorded_page_size(client):
    sid = _session(_page())
    try:
        r = _crop(client, sid, {'x': 60, 'y': 80, 'x2': 340, 'y2': 300})
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert (body['width_pts'], body['height_pts']) == (280.0, 220.0)
        rec = _rec(sid)
        assert (rec['width_pts'], rec['height_pts']) == (280.0, 220.0), \
            'the page record still claims the pre-crop size'
        assert _dims(rec['pdf_bytes']) == (280.0, 220.0), \
            'the reply and the actual page disagree'
    finally:
        entry.sessions.pop(sid, None)


# ── it crops where the user drew ────────────────────────────────────────────

def test_cropping_keeps_the_part_that_was_inside_the_box(client):
    """Corner squares say where the crop landed: keep the bottom-left one."""
    sid = _session(_page())
    try:
        assert 'red' in _colours(_rec(sid)['pdf_bytes'])
        r = _crop(client, sid, {'x': 0, 'y': 0, 'x2': 150, 'y2': 150})
        assert r.status_code == 200
        seen = _colours(_rec(sid)['pdf_bytes'])
        assert seen == {'red'}, f'expected only the bottom-left square, got {seen}'
    finally:
        entry.sessions.pop(sid, None)


def test_cropping_an_already_cropped_page_uses_the_visible_frame(client):
    """The coordinates are display-frame, so a crop on a cropped page is
    relative to what the user can see — the server adds the CropBox origin."""
    sid = _session(_page(cropped=True))
    try:
        # Visible page is 300x220 with origin (50, 80). Keep its bottom-left
        # quarter, which in absolute space is (50,80)-(200,190).
        r = _crop(client, sid, {'x': 0, 'y': 0, 'x2': 150, 'y2': 110})
        assert r.status_code == 200
        assert _cropbox(_rec(sid)['pdf_bytes']) == (50.0, 80.0, 200.0, 190.0)
        assert (r.get_json()['width_pts'], r.get_json()['height_pts']) == (150.0, 110.0)
    finally:
        entry.sessions.pop(sid, None)


def test_cropping_works_on_rotated_pages(client):
    for rot in (90, 180, 270):
        sid = _session(_page(rotate=rot), f'crop-rot{rot}')
        try:
            w0, h0 = _dims(_rec(sid)['pdf_bytes'])
            r = _crop(client, sid, {'x': 20, 'y': 20,
                                    'x2': w0 - 20, 'y2': h0 - 20})
            assert r.status_code == 200, f'/Rotate {rot}: {r.get_data(as_text=True)}'
            w1, h1 = _dims(_rec(sid)['pdf_bytes'])
            assert (round(w1), round(h1)) == (round(w0) - 40, round(h0) - 40), \
                f'/Rotate {rot}: {w0}x{h0} → {w1}x{h1}'
        finally:
            entry.sessions.pop(f'crop-rot{rot}', None)


# ── clearing ────────────────────────────────────────────────────────────────

def test_clearing_the_crop_restores_the_whole_page(client):
    sid = _session(_page())
    try:
        _crop(client, sid, {'x': 0, 'y': 0, 'x2': 150, 'y2': 150})
        assert _colours(_rec(sid)['pdf_bytes']) == {'red'}

        r = _crop(client, sid, clear=True)
        assert r.status_code == 200, r.get_data(as_text=True)
        assert _cropbox(_rec(sid)['pdf_bytes']) == (0.0, 0.0, 400.0, 400.0)
        assert _colours(_rec(sid)['pdf_bytes']) == {'red', 'green', 'blue', 'yellow'}, \
            'clearing the crop did not bring the rest of the page back'
        assert (r.get_json()['width_pts'], r.get_json()['height_pts']) == (400.0, 400.0)
        assert (_rec(sid)['width_pts'], _rec(sid)['height_pts']) == (400.0, 400.0)
    finally:
        entry.sessions.pop(sid, None)


def test_clearing_is_its_own_undo_step(client):
    sid = _session(_page())
    try:
        _crop(client, sid, {'x': 0, 'y': 0, 'x2': 150, 'y2': 150})
        r = _crop(client, sid, clear=True)
        assert r.get_json()['undo_count'] == 2
        # Undo the clear → cropped again.
        _post(client, '/api/viewer/undo', {'session_id': sid})
        assert _colours(_rec(sid)['pdf_bytes']) == {'red'}
    finally:
        entry.sessions.pop(sid, None)


# ── refusal and guards ──────────────────────────────────────────────────────

def test_cropped_and_rotated_is_refused_without_changing_the_page(client):
    """Crop takes client coordinates, so it inherits the same unsupported
    geometry Redact does."""
    for rot in (90, 180, 270):
        sid = _session(_page(cropped=True, rotate=rot), f'crop-cr{rot}')
        try:
            before = _rec(sid)['pdf_bytes']
            r = _crop(client, sid, {'x': 10, 'y': 10, 'x2': 100, 'y2': 100})
            assert r.status_code == 400, f'/Rotate {rot}: expected a refusal'
            assert (r.get_json() or {}).get('error_key') == 'err.cropped_and_rotated'
            assert _rec(sid)['pdf_bytes'] == before
            assert not entry.sessions[sid].get('annot_undo')
        finally:
            entry.sessions.pop(f'crop-cr{rot}', None)


def test_a_tiny_crop_is_rejected(client):
    """A near-zero CropBox makes the page unrenderable, and a 2pt drag is
    almost certainly a stray click rather than an intention."""
    sid = _session(_page())
    try:
        before = _rec(sid)['pdf_bytes']
        r = _crop(client, sid, {'x': 10, 'y': 10, 'x2': 14, 'y2': 14})
        assert r.status_code == 400
        assert (r.get_json() or {}).get('error_key') == 'err.crop_too_small'
        assert _rec(sid)['pdf_bytes'] == before
        assert not entry.sessions[sid].get('annot_undo')
    finally:
        entry.sessions.pop(sid, None)


def test_unknown_page_is_reported(client):
    sid = _session(_page())
    try:
        r = _post(client, '/api/viewer/crop',
                  {'session_id': sid, 'page_id': 'nope',
                   'rect': {'x': 0, 'y': 0, 'x2': 100, 'y2': 100}})
        assert r.status_code == 404
    finally:
        entry.sessions.pop(sid, None)


# ── undo ────────────────────────────────────────────────────────────────────

def test_a_crop_is_one_undo_and_restores_the_size_too(client):
    sid = _session(_page())
    try:
        before = _rec(sid)['pdf_bytes']
        r = _crop(client, sid, {'x': 0, 'y': 0, 'x2': 150, 'y2': 150})
        assert r.get_json()['undo_count'] == 1

        u = _post(client, '/api/viewer/undo', {'session_id': sid})
        assert u.status_code == 200
        assert _rec(sid)['pdf_bytes'] == before
        assert _dims(_rec(sid)['pdf_bytes']) == (400.0, 400.0)
        # The undo route has to hand back the restored size, or the client keeps
        # laying the page out at the cropped dimensions.
        body = u.get_json()
        assert body.get('width_pts') == 400.0 and body.get('height_pts') == 400.0, \
            f'undo did not report the restored page size: {body}'
    finally:
        entry.sessions.pop(sid, None)


def test_a_crop_marks_the_document_dirty(client):
    sid = _session(_page())
    try:
        _crop(client, sid, {'x': 0, 'y': 0, 'x2': 150, 'y2': 150})
        from bootstrap import recovery as _recovery
        assert _recovery.has_pending(entry.sessions[sid])
    finally:
        entry.sessions.pop(sid, None)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
