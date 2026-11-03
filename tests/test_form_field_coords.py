"""Form-field geometry between the viewer's display frame and PDF user space.

The third instance of the CropBox-origin bug (see tests/test_crop_coords.py and
CLAUDE.md, "Viewer page coordinates"), and the worst of the three.

form_utils stores a widget's geometry straight off its `/Rect` — absolute PDF
user space — alongside a `page_h` taken from the MEDIABOX.  The viewer renders
the CropBox and maps with:

    x = x1 * s                      (viewer-panel.js _formFieldDisplayRect)
    y = (page_h - y2) * s

so before this was fixed, a widget on the 400x400 page cropped to
(50,80)-(350,300) below was drawn at y=250 on a 220pt-tall page: not merely
displaced but **off the page entirely**, invisible and unreachable.  A rotated
page was wrong too, because `page_h` was then the wrong axis.

`/api/viewer/form_fields` now translates into the display frame on the way out,
and the viewer's own add/update routes translate back on the way in.  The stored
dict stays in absolute user space, because `/api/editor/*` serves the same list
to the editor, which works in that frame via its own pdf2c.

The placement routes are viewer-namespaced rather than a flag on the editor's,
deliberately: one route with two coordinate conventions is the drift that caused
all three instances of this bug.
"""
import io
import threading

import _bootstrap  # noqa: F401  (path setup)

import pikepdf
import pypdfium2 as pdfium
import pytest

import entry
from pdf import form_utils

# Same fixture geometry as tests/test_crop_coords.py, on purpose.
_MB = (0, 0, 400, 400)
_CB = (50, 80, 350, 300)          # visible 300 x 220, origin (50, 80)
_WIDGET = (100, 120, 220, 150)    # absolute user space


def _page(cropped=False, rotate=0, widget=True):
    pdf = pikepdf.new()
    d = dict(Type=pikepdf.Name.Page,
             MediaBox=pikepdf.Array(list(_MB)),
             Resources=pikepdf.Dictionary(),
             Contents=pdf.make_indirect(pikepdf.Stream(
                 pdf, b'1 1 1 rg 0 0 400 400 re f')))
    if cropped:
        d['CropBox'] = pikepdf.Array(list(_CB))
    if rotate:
        d['Rotate'] = rotate
    if widget:
        w = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget,
            FT=pikepdf.Name.Tx, T='field1', V='',
            Rect=pikepdf.Array(list(_WIDGET))))
        d['Annots'] = pikepdf.Array([w])
    pdf.pages.append(pikepdf.Page(pdf.make_indirect(pikepdf.Dictionary(**d))))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _display_size(pdf_bytes):
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return doc[0].get_width(), doc[0].get_height()
    finally:
        doc.close()


def _page_annots(pdf_bytes):
    """[(subtype, [x0,y0,x1,y1]), …] for every annotation on page 0."""
    import pypdf
    annots = pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].get('/Annots')
    if not annots:
        return []
    if hasattr(annots, 'get_object'):
        annots = annots.get_object()
    out = []
    for ref in annots:
        a = ref.get_object() if hasattr(ref, 'get_object') else ref
        rect = a.get('/Rect')
        if hasattr(rect, 'get_object'):
            rect = rect.get_object()
        out.append((str(a.get('/Subtype', '')), [float(v) for v in rect]))
    return out


def _overlay_rect(field):
    """Replicate viewer-panel.js _formFieldDisplayRect at scale 1."""
    return {'x': field['x1'],
            'y': field['page_h'] - field['y2'],
            'w': field['x2'] - field['x1'],
            'h': field['y2'] - field['y1']}


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _session(pdf_bytes, sid='ff-coords'):
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'f.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': pdf_bytes,
                   'form_fields': []}],
        'comments': {},
    }
    fields = form_utils.extract_all_form_fields(pdf_bytes)
    entry.sessions[sid]['pages'][0]['form_fields'] = fields.get(0, [])
    return sid


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _served_fields(client, sid):
    r = _post(client, '/api/viewer/form_fields', {'session_id': sid})
    assert r.status_code == 200
    return (r.get_json() or {}).get('fields_by_page', {}).get('pg1', [])


# ── the overlay lands where the widget is ────────────────────────────────────

def test_field_overlay_is_on_the_page_when_cropped(client):
    """The regression that motivated this file: off-page, not just displaced."""
    b = _page(cropped=True)
    sid = _session(b)
    try:
        disp_w, disp_h = _display_size(b)
        field = _served_fields(client, sid)[0]
        r = _overlay_rect(field)
        assert 0 <= r['y'] <= disp_h - r['h'], \
            f"overlay y={r['y']:.1f} outside a {disp_h:.0f}pt page"
        assert 0 <= r['x'] <= disp_w - r['w'], \
            f"overlay x={r['x']:.1f} outside a {disp_w:.0f}pt page"
        # And exactly where the widget actually is, relative to the CropBox.
        assert abs(r['x'] - (_WIDGET[0] - _CB[0])) < 0.5
        assert abs(r['y'] - (_CB[3] - _WIDGET[3])) < 0.5
    finally:
        entry.sessions.pop(sid, None)


def test_field_overlay_geometry_across_the_matrix(client):
    """{plain, cropped} x {0, 90, 180, 270} — the overlay is always on the page
    and always the widget's own size (rotation swaps width and height)."""
    for cropped in (False, True):
        for rot in (0, 90, 180, 270):
            b = _page(cropped=cropped, rotate=rot)
            sid = _session(b, f'ff-matrix-{int(cropped)}-{rot}')
            try:
                disp_w, disp_h = _display_size(b)
                field = _served_fields(client, sid)[0]
                r = _overlay_rect(field)
                where = f'cropped={cropped} /Rotate {rot}'
                assert field['page_h'] == pytest.approx(disp_h), \
                    f'{where}: page_h={field["page_h"]} but page is {disp_h}'
                assert -0.5 <= r['x'] <= disp_w - r['w'] + 0.5, f'{where}: x={r["x"]:.1f}'
                assert -0.5 <= r['y'] <= disp_h - r['h'] + 0.5, f'{where}: y={r["y"]:.1f}'
                wid, hgt = _WIDGET[2] - _WIDGET[0], _WIDGET[3] - _WIDGET[1]
                exp = (hgt, wid) if rot in (90, 270) else (wid, hgt)
                assert (round(r['w']), round(r['h'])) == exp, where
            finally:
                entry.sessions.pop(f'ff-matrix-{int(cropped)}-{rot}', None)


def test_serving_fields_does_not_mutate_the_stored_geometry(client):
    """The editor reads the same list in absolute user space — translating on
    the way out must not write back."""
    b = _page(cropped=True)
    sid = _session(b)
    try:
        before = dict(entry.sessions[sid]['pages'][0]['form_fields'][0])
        _served_fields(client, sid)
        _served_fields(client, sid)          # twice: a second pass would compound
        after = entry.sessions[sid]['pages'][0]['form_fields'][0]
        for k in ('x1', 'y1', 'x2', 'y2', 'page_h'):
            assert after[k] == before[k], f'{k} was mutated by serving'
    finally:
        entry.sessions.pop(sid, None)


# ── placing a field from the viewer ──────────────────────────────────────────

def _place(client, sid, rect, ftype='text'):
    return _post(client, '/api/viewer/add_form_field', {
        'session_id': sid, 'page_id': 'pg1',
        'field': {'type': ftype, 'x1': rect[0], 'y1': rect[1],
                  'x2': rect[2], 'y2': rect[3]}})


def test_placed_field_comes_back_where_it_was_drawn(client):
    """Round trip through the display frame on a cropped page."""
    b = _page(cropped=True, widget=False)
    sid = _session(b)
    try:
        drawn = (20.0, 30.0, 140.0, 60.0)        # display frame
        r = _place(client, sid, drawn)
        assert r.status_code == 200, r.get_data(as_text=True)
        echoed = r.get_json()['field']
        for got, want in zip((echoed['x1'], echoed['y1'],
                              echoed['x2'], echoed['y2']), drawn):
            assert abs(got - want) < 0.5

        served = _served_fields(client, sid)
        assert len(served) == 1
        for got, want in zip((served[0]['x1'], served[0]['y1'],
                              served[0]['x2'], served[0]['y2']), drawn):
            assert abs(got - want) < 0.5, 'reload disagreed with the echo'
    finally:
        entry.sessions.pop(sid, None)


def test_placed_field_is_stored_in_absolute_user_space(client):
    """What lands in the session (and so in the PDF, and so in the editor) is
    the widget's real position — display coords translated by the CropBox."""
    b = _page(cropped=True, widget=False)
    sid = _session(b)
    try:
        drawn = (20.0, 30.0, 140.0, 60.0)
        assert _place(client, sid, drawn).status_code == 200
        stored = entry.sessions[sid]['pages'][0]['form_fields'][0]
        assert abs(stored['x1'] - (drawn[0] + _CB[0])) < 0.5
        assert abs(stored['y1'] - (drawn[1] + _CB[1])) < 0.5
        assert 'page_h' not in stored, 'a display-frame value was stored'

        # The widget really is in the PDF at that spot.
        out = entry.sessions[sid]['pages'][0]['pdf_bytes']
        rects = [r for _st, r in _page_annots(out)]
        assert any(abs(rc[0] - (drawn[0] + _CB[0])) < 0.5 for rc in rects), rects
    finally:
        entry.sessions.pop(sid, None)


def test_moving_a_field_uses_the_display_frame(client):
    b = _page(cropped=True, widget=False)
    sid = _session(b)
    try:
        assert _place(client, sid, (20.0, 30.0, 140.0, 60.0)).status_code == 200
        fid = entry.sessions[sid]['pages'][0]['form_fields'][0]['id']
        moved = {'x1': 50.0, 'y1': 70.0, 'x2': 170.0, 'y2': 100.0}
        r = _post(client, '/api/viewer/update_form_field',
                  {'session_id': sid, 'page_id': 'pg1', 'field_id': fid,
                   'updates': moved})
        assert r.status_code == 200, r.get_data(as_text=True)
        served = _served_fields(client, sid)[0]
        for k, want in moved.items():
            assert abs(served[k] - want) < 0.5, k
        stored = entry.sessions[sid]['pages'][0]['form_fields'][0]
        assert abs(stored['x1'] - (moved['x1'] + _CB[0])) < 0.5
    finally:
        entry.sessions.pop(sid, None)


def test_deleting_a_field_removes_it(client):
    b = _page(cropped=True, widget=False)
    sid = _session(b)
    try:
        assert _place(client, sid, (20.0, 30.0, 140.0, 60.0)).status_code == 200
        fid = entry.sessions[sid]['pages'][0]['form_fields'][0]['id']
        r = _post(client, '/api/viewer/delete_form_field',
                  {'session_id': sid, 'page_id': 'pg1', 'field_id': fid})
        assert r.status_code == 200
        assert _served_fields(client, sid) == []
    finally:
        entry.sessions.pop(sid, None)


def test_unknown_field_and_page_are_reported_not_crashed(client):
    b = _page(cropped=True, widget=False)
    sid = _session(b)
    try:
        r = _post(client, '/api/viewer/update_form_field',
                  {'session_id': sid, 'page_id': 'pg1', 'field_id': 'nope',
                   'updates': {'x1': 1.0}})
        assert r.status_code == 404
        r = _post(client, '/api/viewer/add_form_field',
                  {'session_id': sid, 'page_id': 'no-such-page',
                   'field': {'type': 'text', 'x1': 0, 'y1': 0, 'x2': 1, 'y2': 1}})
        assert r.status_code == 404
        r = _post(client, '/api/viewer/add_form_field',
                  {'session_id': sid, 'page_id': 'pg1', 'field': {}})
        assert r.status_code == 400
    finally:
        entry.sessions.pop(sid, None)


# ── the reason these routes are serialised ───────────────────────────────────

def test_field_write_and_annotation_write_do_not_lose_each_other(client):
    """In the viewer a field placement can race an annotation on the same page
    bytes.  The editor's routes are unserialised and safe only because the
    editor is modal; these are not, so they carry @_serialised_on_session.

    Without it both writes read the same bytes and one is silently dropped —
    the same failure mode as the six-draws-one-annotation report in CLAUDE.md.
    """
    b = _page(cropped=True, widget=False)
    sid = _session(b)
    try:
        errors = []

        def add_field():
            try:
                with entry.app.test_client() as c:
                    _place(c, sid, (20.0, 30.0, 140.0, 60.0))
            except Exception as exc:          # pragma: no cover
                errors.append(exc)

        def add_annot():
            try:
                with entry.app.test_client() as c:
                    _post(c, '/api/viewer/add_annotation',
                          {'session_id': sid, 'page_id': 'pg1',
                           'subtype': 'Square', 'rect': [10, 10, 60, 60],
                           'border_color': [1.0, 0.0, 0.0], 'border_width': 2.0})
            except Exception as exc:          # pragma: no cover
                errors.append(exc)

        ts = [threading.Thread(target=add_field), threading.Thread(target=add_annot)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        assert not errors, errors

        page = entry.sessions[sid]['pages'][0]
        assert len(page['form_fields']) == 1, 'the field write was lost'
        subtypes = [st for st, _r in _page_annots(page['pdf_bytes'])]
        assert '/Widget' in subtypes, f'widget missing from the PDF: {subtypes}'
        assert '/Square' in subtypes, f'annotation lost: {subtypes}'
    finally:
        entry.sessions.pop(sid, None)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
