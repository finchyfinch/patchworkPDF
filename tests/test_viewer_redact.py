"""The viewer's Redact tool (W3 of the editor→viewer merge).

Three things separate this from an annotation and each is pinned here.

**It is destructive.**  apply_edits scrubs the native text under the box and
_burn_redactions_into_images destroys the pixels inside any image XObject, so
the content is gone rather than covered.  tests/test_redaction.py already pins
the burn itself; what matters here is that the viewer's route actually reaches
it, with the box in the right place.

**It is undoable anyway.**  The viewer's undo stack snapshots whole page BYTES
(_annot_push_undo), not annotation objects, so a content rewrite joins the
existing Undo/Redo with no new machinery — which is the observation the whole of
Phase 1 rests on.

**Placing it wrong is a security failure, not a cosmetic one.**  A box that
lands elsewhere covers nothing while the operation reports success, leaving the
text readable and the user believing it is not.  That is why a page that is both
cropped and rotated — geometry apply_edits is documented as unable to place
content on — is REFUSED rather than attempted, and why the refusal must leave
the page byte-identical.
"""
import io
import threading

import _bootstrap  # noqa: F401  (path setup)

import pikepdf
import pypdfium2 as pdfium
import pytest

import entry

_MB = (0, 0, 400, 400)
_CB = (50, 80, 350, 300)          # visible 300 x 220, origin (50, 80)
_MARK = (100, 120, 140, 160)      # red square, absolute user space


def _page(cropped=False, rotate=0):
    pdf = pikepdf.new()
    ops = [b'1 1 1 rg 0 0 400 400 re f',
           ('1 0 0 rg %d %d %d %d re f' % (
               _MARK[0], _MARK[1], _MARK[2] - _MARK[0],
               _MARK[3] - _MARK[1])).encode()]
    d = dict(Type=pikepdf.Name.Page,
             MediaBox=pikepdf.Array(list(_MB)),
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


_SCALE = 2.0
_IS_RED = lambda p: p[0] > 150 and p[1] < 100 and p[2] < 100
_IS_BLACK = lambda p: max(p) < 40


def _render(pdf_bytes):
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        page = doc[0]
        return (page.render(scale=_SCALE).to_pil().convert('RGB'),
                page.get_width(), page.get_height())
    finally:
        doc.close()


def _blob(img, pred):
    W, H = img.size
    px = img.load()
    xs, ys = [], []
    for y in range(H):
        for x in range(W):
            if pred(px[x, y]):
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def _marker_display_rect(pdf_bytes):
    """The marker's position in DISPLAY-frame points — what the viewer would
    send after the user dragged a box around it."""
    img, _w, h = _render(pdf_bytes)
    b = _blob(img, _IS_RED)
    assert b is not None, 'marker not visible — fixture is wrong'
    # Screen px (y-down) → display-frame points (y-up), with a small margin so
    # the box fully contains the marker the way a real drag would.
    return {'x':  b[0] / _SCALE - 1, 'y':  h - b[3] / _SCALE - 1,
            'x2': b[2] / _SCALE + 1, 'y2': h - b[1] / _SCALE + 1}


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _session(pdf_bytes, sid='redact'):
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'r.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': pdf_bytes}],
        'comments': {},
    }
    return sid


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _redact(client, sid, rect):
    return _post(client, '/api/viewer/redact',
                 {'session_id': sid, 'page_id': 'pg1', 'rect': rect})


def _bytes(sid):
    return entry.sessions[sid]['pages'][0]['pdf_bytes']


def _marker_gone(sid):
    img, _w, _h = _render(_bytes(sid))
    return _blob(img, _IS_RED) is None


# ── it lands on the target, in every supported geometry ─────────────────────

def test_redaction_covers_the_target_on_a_plain_page(client):
    sid = _session(_page())
    try:
        r = _redact(client, sid, _marker_display_rect(_bytes(sid)))
        assert r.status_code == 200, r.get_data(as_text=True)
        assert _marker_gone(sid), 'the marker is still readable'
    finally:
        entry.sessions.pop(sid, None)


def test_redaction_covers_the_target_on_a_cropped_page(client):
    """The W0 failure case.  Viewer-frame coordinates used to land the box off
    the visible page entirely — nothing covered, success reported."""
    sid = _session(_page(cropped=True))
    try:
        r = _redact(client, sid, _marker_display_rect(_bytes(sid)))
        assert r.status_code == 200, r.get_data(as_text=True)
        assert _marker_gone(sid), 'the marker is still readable on a cropped page'
        img, _w, _h = _render(_bytes(sid))
        assert _blob(img, _IS_BLACK) is not None, 'nothing was drawn at all'
    finally:
        entry.sessions.pop(sid, None)


def test_redaction_covers_the_target_on_rotated_pages(client):
    for rot in (90, 180, 270):
        sid = _session(_page(rotate=rot), f'redact-rot{rot}')
        try:
            r = _redact(client, sid, _marker_display_rect(_bytes(sid)))
            assert r.status_code == 200, f'/Rotate {rot}: {r.get_data(as_text=True)}'
            assert _marker_gone(sid), f'/Rotate {rot}: marker still readable'
        finally:
            entry.sessions.pop(f'redact-rot{rot}', None)


# ── the refusal ─────────────────────────────────────────────────────────────

def test_cropped_and_rotated_is_refused_not_attempted(client):
    """apply_edits cannot place content on this geometry, so a redaction would
    land somewhere else and cover nothing.  Refuse — and leave the page exactly
    as it was, because a half-applied redaction is worse than none."""
    for rot in (90, 180, 270):
        sid = _session(_page(cropped=True, rotate=rot), f'redact-cr{rot}')
        try:
            before = _bytes(sid)
            r = _redact(client, sid, _marker_display_rect(before))
            assert r.status_code == 400, f'/Rotate {rot}: expected a refusal'
            body = r.get_json() or {}
            assert body.get('error_key') == 'err.cropped_and_rotated', body
            assert _bytes(sid) == before, \
                f'/Rotate {rot}: the page was modified despite the refusal'
            assert not entry.sessions[sid].get('annot_undo'), \
                f'/Rotate {rot}: a refusal must not spend an undo slot'
        finally:
            entry.sessions.pop(f'redact-cr{rot}', None)


def test_a_negligible_rect_is_rejected(client):
    sid = _session(_page())
    try:
        before = _bytes(sid)
        r = _redact(client, sid, {'x': 10, 'y': 10, 'x2': 10.2, 'y2': 10.2})
        assert r.status_code == 400
        assert _bytes(sid) == before
    finally:
        entry.sessions.pop(sid, None)


def test_unknown_page_is_reported(client):
    sid = _session(_page())
    try:
        r = _post(client, '/api/viewer/redact',
                  {'session_id': sid, 'page_id': 'nope',
                   'rect': {'x': 1, 'y': 1, 'x2': 50, 'y2': 50}})
        assert r.status_code == 404
    finally:
        entry.sessions.pop(sid, None)


# ── it joins the existing undo stack ────────────────────────────────────────

def test_one_redaction_is_one_undo_and_it_comes_back(client):
    """The observation Phase 1 rests on: the undo stack holds page BYTES, so a
    destructive content rewrite is undoable with no new machinery."""
    sid = _session(_page())
    try:
        before = _bytes(sid)
        r = _redact(client, sid, _marker_display_rect(before))
        assert r.status_code == 200
        assert r.get_json()['undo_count'] == 1, 'expected exactly one undo step'
        assert _marker_gone(sid)

        u = _post(client, '/api/viewer/undo', {'session_id': sid})
        assert u.status_code == 200, u.get_data(as_text=True)
        assert not _marker_gone(sid), 'undo did not bring the content back'
        assert _bytes(sid) == before, 'undo did not restore the original bytes'
    finally:
        entry.sessions.pop(sid, None)


def test_redo_puts_the_redaction_back(client):
    sid = _session(_page())
    try:
        _redact(client, sid, _marker_display_rect(_bytes(sid)))
        _post(client, '/api/viewer/undo', {'session_id': sid})
        assert not _marker_gone(sid)
        r = _post(client, '/api/viewer/redo', {'session_id': sid})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert _marker_gone(sid), 'redo did not re-apply the redaction'
    finally:
        entry.sessions.pop(sid, None)


def test_a_redaction_and_an_annotation_are_separate_undo_steps(client):
    """They share one stack, so the counts have to add up — otherwise Ctrl+Z
    reaches past the action the user meant (CLAUDE.md, annotation undo)."""
    sid = _session(_page())
    try:
        _redact(client, sid, _marker_display_rect(_bytes(sid)))
        r = _post(client, '/api/viewer/add_annotation',
                  {'session_id': sid, 'page_id': 'pg1', 'subtype': 'Square',
                   'rect': [10, 10, 60, 60], 'border_color': [0.0, 0.0, 1.0],
                   'border_width': 2.0})
        assert r.status_code == 200
        assert r.get_json()['undo_count'] == 2

        _post(client, '/api/viewer/undo', {'session_id': sid})   # the annotation
        assert _marker_gone(sid), 'one undo reached past the annotation'
        _post(client, '/api/viewer/undo', {'session_id': sid})   # the redaction
        assert not _marker_gone(sid)
    finally:
        entry.sessions.pop(sid, None)


def test_a_redaction_marks_the_document_dirty(client):
    sid = _session(_page())
    try:
        _redact(client, sid, _marker_display_rect(_bytes(sid)))
        from bootstrap import recovery as _recovery
        assert _recovery.has_pending(entry.sessions[sid]), \
            'a byte rewrite must mark the session dirty'
    finally:
        entry.sessions.pop(sid, None)


# ── concurrency, for the same reason every viewer mutation is serialised ────

def test_a_redaction_and_an_annotation_do_not_lose_each_other(client):
    sid = _session(_page())
    try:
        rect = _marker_display_rect(_bytes(sid))
        errors = []

        def do_redact():
            try:
                with entry.app.test_client() as c:
                    _redact(c, sid, rect)
            except Exception as exc:          # pragma: no cover
                errors.append(exc)

        def do_annot():
            try:
                with entry.app.test_client() as c:
                    _post(c, '/api/viewer/add_annotation',
                          {'session_id': sid, 'page_id': 'pg1',
                           'subtype': 'Square', 'rect': [200, 20, 260, 70],
                           'border_color': [0.0, 0.0, 1.0], 'border_width': 2.0})
            except Exception as exc:          # pragma: no cover
                errors.append(exc)

        ts = [threading.Thread(target=do_redact), threading.Thread(target=do_annot)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        assert not errors, errors

        assert _marker_gone(sid), 'the redaction was lost'
        import pypdf
        annots = pypdf.PdfReader(io.BytesIO(_bytes(sid))).pages[0].get('/Annots')
        assert annots, 'the annotation was lost'
        u, r = 0, 0
        s = entry.sessions[sid]
        u, r = len(s.get('annot_undo', [])), len(s.get('annot_redo', []))
        assert u == 2, f'expected one undo step each, got {u}'
    finally:
        entry.sessions.pop(sid, None)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
