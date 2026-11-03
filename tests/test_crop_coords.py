"""Page coordinates on a CROPPED page — viewer frame vs editor frame.

Background (W0 spike for docs/EDITOR_VIEWER_MERGE_PLAN.md).  The two panels do
not agree on where a page's origin is:

  * the editor's c2pdf() adds the page's CropBox offset (editor.js:200), using
    page_left / page_top that editor_open reads from CropBox.left / CropBox.top,
    so it produces ABSOLUTE PDF user-space coordinates;
  * the viewer has no CropBox term at all (viewer-panel.js:4208) — its pageW /
    pageH come from pdfium, which reports the CropBox, so it produces
    CROPBOX-RELATIVE coordinates.

On a page where CropBox == MediaBox (the overwhelming majority) the two agree
and nothing is wrong, which is why this has never been reported.  On a cropped
page everything the viewer writes is displaced by exactly
(CropBox.left, CropBox.bottom).

That is not hypothetical, and it is not only about the tools being moved into
the viewer: **the app can walk itself into it**.  Crop a page with the editor's
Crop tool, return to the viewer, place an annotation — it lands in the wrong
place.  No unusual input file is needed.  See
test_cropping_then_annotating_misplaces_the_annotation.

WHAT THIS FILE PINS
-------------------
Two kinds of test, deliberately separated:

  1. GUARANTEES — behaviour that is correct today and must survive the W1 change
     (plain pages at every rotation; the editor's own origin logic).
  2. CHARACTERISATION — behaviour that is WRONG today, asserted as-is so the
     suite stays green and so W1 has an exact checklist.  Every one of these is
     named *_today and carries a `W1:` note saying what it must become.  When
     W1 lands these assertions flip; that is the point of them.

Do not "fix" a *_today test by loosening it.  Invert it.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pikepdf
import pypdf
import pypdfium2 as pdfium

import entry
from routes.editor import apply_edits, _editor_display_orientation
from routes.viewer import _render_page


# ── geometry under test ──────────────────────────────────────────────────────
# A CropBox that is offset on BOTH axes, so an error in either shows up.
_MB = (0, 0, 400, 400)
_CB = (50, 80, 350, 300)          # left, bottom, right, top → 300 x 220 visible
_CROP_DX, _CROP_DY = _CB[0], _CB[1]

# A red marker square, positioned in ABSOLUTE user space, inside the crop.
_MARK = (100, 120, 140, 160)


def _page(cropped=False, rotate=0, marker=True):
    pdf = pikepdf.new()
    ops = [b'1 1 1 rg 0 0 400 400 re f']
    if marker:
        ops.append(('1 0 0 rg %d %d %d %d re f' % (
            _MARK[0], _MARK[1], _MARK[2] - _MARK[0], _MARK[3] - _MARK[1])).encode())
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


def _editor_origin(pdf_bytes, page_h):
    """Exactly what editor_open computes for page_left / page_top."""
    cb = pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].cropbox
    _items, top, left = _editor_display_orientation(
        pdf_bytes, page_h, [], float(cb.top), float(cb.left))
    return left, top


def _render(pdf_bytes, scale=2.0):
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return doc[0].render(scale=scale).to_pil().convert('RGB')
    finally:
        doc.close()


def _blob(img, pred):
    """Bounding box (px) of pixels satisfying `pred`, or None."""
    W, H = img.size
    px = img.load()
    xs, ys = [], []
    for y in range(H):
        for x in range(W):
            if pred(px[x, y]):
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


_IS_RED = lambda p: p[0] > 150 and p[1] < 100 and p[2] < 100
_IS_BLACK = lambda p: max(p) < 40


def _redact(pdf_bytes, box, page_w, page_h):
    """Run one redaction box through the real apply_edits pipeline."""
    edits = {'deleted_ids': [], 'moved': [], 'text_changed': [], 'markup': [],
             'redacts': [{'x': box[0], 'y': box[1], 'x2': box[2], 'y2': box[3]}],
             'pdf_image_edits': [], 'original_elements': [], 'watermark': None}
    out, _ = apply_edits(pdf_bytes, edits, page_w, page_h)
    return out


def _marker_px(pdf_bytes, scale=2.0):
    """Where the red marker sits in the rendered raster, in screen px."""
    red = _blob(_render(pdf_bytes, scale), _IS_RED)
    assert red is not None, 'marker not visible — fixture is wrong'
    return red


def _covers_marker(redacted_bytes, marker_px, scale=2.0):
    """True when the black cover rect lands on the marker's screen position.

    The cover rect and the marker are the same size, so a correct hit is a
    near-exact overlay; anything else is a miss.  `marker_px` is measured on the
    ORIGINAL page and passed in, so this never has to rebuild the fixture.
    """
    black = _blob(_render(redacted_bytes, scale), _IS_BLACK)
    if black is None:
        return False                      # drew nothing / drew off-page
    bc = ((black[0] + black[2]) / 2, (black[1] + black[3]) / 2)
    mc = ((marker_px[0] + marker_px[2]) / 2, (marker_px[1] + marker_px[3]) / 2)
    return abs(bc[0] - mc[0]) <= 2 and abs(bc[1] - mc[1]) <= 2


def _box_for_marker(marker_px, scale, origin_left, origin_top):
    """Convert the marker's screen rect back to PDF points with a given origin."""
    return (marker_px[0] / scale + origin_left, origin_top - marker_px[3] / scale,
            marker_px[2] / scale + origin_left, origin_top - marker_px[1] / scale)


# ── 1. GUARANTEES — must still hold after W1 ─────────────────────────────────

def test_editor_origin_on_a_cropped_page_is_the_cropbox_corner():
    """editor_open's page_left/page_top ARE CropBox.left/CropBox.top."""
    b = _page(cropped=True)
    _i, _w, h, _s = _render_page(b)
    left, top = _editor_origin(b, h)
    assert (left, top) == (float(_CB[0]), float(_CB[3]))


def test_editor_origin_on_a_rotated_uncropped_page_is_the_display_frame():
    """_editor_display_orientation deliberately moves the origin to (0, page_h)
    for a rotated page that is NOT cropped — which is exactly the viewer's own
    implicit origin.  This is why rotation alone has never been a problem, and
    W1's shared helper has to reproduce it rather than read CropBox blindly."""
    for rot in (90, 180, 270):
        b = _page(cropped=False, rotate=rot)
        _i, _w, h, _s = _render_page(b)
        left, top = _editor_origin(b, h)
        assert (left, top) == (0.0, float(h)), f'/Rotate {rot}'


def test_plain_pages_agree_at_every_rotation():
    """The viewer's current (origin-free) conversion is already CORRECT for a
    page whose CropBox is its MediaBox, at every rotation.  W1 must not regress
    this — it is the case essentially every real document is in."""
    for rot in (0, 90, 180, 270):
        b = _page(cropped=False, rotate=rot)
        _i, w, h, scale = _render_page(b)
        mark = _marker_px(b, scale)
        box = _box_for_marker(mark, scale, 0.0, h)   # viewer's implicit origin
        assert _covers_marker(_redact(b, box, w, h), mark, scale), \
            f'plain /Rotate {rot}: viewer conversion should already be correct'


def test_cropbox_aware_conversion_lands_a_redaction_on_target():
    """The specification for W1's shared helper: origin at (CropBox.left,
    CropBox.top) puts a redaction exactly on the marker of a cropped page."""
    b = _page(cropped=True)
    _i, w, h, scale = _render_page(b)
    mark = _marker_px(b, scale)
    left, top = _editor_origin(b, h)
    box = _box_for_marker(mark, scale, left, top)
    assert _covers_marker(_redact(b, box, w, h), mark, scale)

    # ...and the viewer's origin, on the SAME page, does not — so this test is
    # measuring the origin and not something that would pass either way.
    miss = _box_for_marker(mark, scale, 0.0, h)
    assert not _covers_marker(_redact(b, miss, w, h), mark, scale)


# ── 2. CHARACTERISATION — wrong today; W1 inverts these ──────────────────────

def test_viewer_conversion_misses_the_marker_on_a_cropped_page_today():
    """W1: once the render route reports page_left/page_top and the client uses
    them, this must become a HIT — rename to ..._lands_on_target and assert
    _marker_covered(...) is True.

    Today the box is displaced by (CropBox.left, CropBox.bottom); with this
    fixture it lands wholly outside the visible page, so NOTHING is covered —
    the dangerous failure mode for a redaction, because the operation reports
    success while the content stays readable."""
    b = _page(cropped=True)
    _i, w, h, scale = _render_page(b)
    mark = _marker_px(b, scale)
    box = _box_for_marker(mark, scale, 0.0, h)       # viewer's implicit origin
    out = _redact(b, box, w, h)
    assert not _covers_marker(out, mark, scale), 'expected the known miss'
    assert _blob(_render(out, scale), _IS_RED) is not None, \
        'the marker should still be fully readable — that is the whole problem'


def test_annotation_on_a_cropped_page_lands_where_it_was_drawn():
    """Through the real add_annotation route, on a cropped page.

    Before W1 this landed displaced by exactly (CropBox.left, CropBox.bottom) —
    (-50, +80) pt on this fixture — because the viewer's write path handled
    rotation but never the CropBox origin.  _page_display_frame now supplies it.
    """
    sid = 'crop-coords-annot'
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'c.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '',
                   'pdf_bytes': _page(cropped=True, marker=False)}],
        'comments': {},
    }
    entry.app.config['TESTING'] = True
    try:
        vis_w, vis_h = _CB[2] - _CB[0], _CB[3] - _CB[1]
        asked = [vis_w / 2 - 20, vis_h / 2 - 20, vis_w / 2 + 20, vis_h / 2 + 20]
        with entry.app.test_client() as c:
            r = c.post('/api/viewer/add_annotation',
                       json={'session_id': sid, 'page_id': 'pg1',
                             'subtype': 'Square', 'rect': asked,
                             'border_color': [1.0, 0.0, 0.0],
                             'fill_color': [1.0, 0.0, 0.0], 'border_width': 3.0},
                       headers={'X-App-Token': entry.APP_TOKEN})
            assert r.status_code == 200

        out = entry.sessions[sid]['pages'][0]['pdf_bytes']
        scale = 2.0
        img = _render(out, scale)
        blob = _blob(img, _IS_RED)
        assert blob is not None, 'annotation drew nothing at all'
        got = ((blob[0] + blob[2]) / 2 / scale, (blob[1] + blob[3]) / 2 / scale)
        want = (vis_w / 2, vis_h / 2)          # asked for the dead centre
        dx, dy = got[0] - want[0], got[1] - want[1]
        assert abs(dx) < 2, f'dx={dx:.1f} — displaced horizontally'
        assert abs(dy) < 2, f'dy={dy:.1f} — displaced vertically'
    finally:
        entry.sessions.pop(sid, None)


def test_cropping_then_annotating_puts_the_annotation_where_asked():
    """The self-inflicted path — no unusual input file required.

    Crop a plain page with the editor's own Crop tool, then annotate it in the
    viewer.  Every step is a supported user action, and before W1 the annotation
    landed 50pt left and 80pt up from where the user put it.  This is the test
    that matters most: it needs no strangely-authored PDF to reproduce.
    """
    plain = _page(cropped=False, marker=False)
    cropped, _ = apply_edits(
        plain,
        {'deleted_ids': [], 'moved': [], 'text_changed': [], 'markup': [],
         'redacts': [], 'pdf_image_edits': [], 'original_elements': [],
         'watermark': None,
         'crop': {'x': _CB[0], 'y': _CB[1], 'x2': _CB[2], 'y2': _CB[3]}},
        _MB[2], _MB[3])

    cb = pypdf.PdfReader(io.BytesIO(cropped)).pages[0].cropbox
    assert (round(float(cb.left)), round(float(cb.bottom))) == (_CB[0], _CB[1]), \
        'the Crop tool did not set the CropBox this test depends on'

    sid = 'crop-coords-selfinflict'
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'x.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': cropped}],
        'comments': {},
    }
    entry.app.config['TESTING'] = True
    try:
        _i, w, h, scale = _render_page(cropped)
        asked = [w / 2 - 20, h / 2 - 20, w / 2 + 20, h / 2 + 20]
        with entry.app.test_client() as c:
            r = c.post('/api/viewer/add_annotation',
                       json={'session_id': sid, 'page_id': 'pg1',
                             'subtype': 'Square', 'rect': asked,
                             'border_color': [1.0, 0.0, 0.0],
                             'fill_color': [1.0, 0.0, 0.0], 'border_width': 3.0},
                       headers={'X-App-Token': entry.APP_TOKEN})
            assert r.status_code == 200

        out = entry.sessions[sid]['pages'][0]['pdf_bytes']
        img = _render(out, scale)
        blob = _blob(img, _IS_RED)
        assert blob is not None, 'annotation drew nothing at all'
        got_cx = (blob[0] + blob[2]) / 2 / scale
        got_cy = (blob[1] + blob[3]) / 2 / scale
        assert abs(got_cx - w / 2) < 2, \
            f'{got_cx:.1f} vs {w / 2:.1f} — displaced horizontally'
        assert abs(got_cy - h / 2) < 2, \
            f'{got_cy:.1f} vs {h / 2:.1f} — displaced vertically'
    finally:
        entry.sessions.pop(sid, None)


def test_viewer_annotations_land_correctly_across_the_whole_matrix():
    """{plain, cropped} x {0, 90, 180, 270} — all eight, through the real route.

    The W0 spike measured cropped+rotated as wrong in BOTH panels and the plan
    expected Phase 1 to merely inherit that.  It did better: because
    _raw_rect_to_display now translates by the CropBox origin FIRST and rotates
    within the CropBox extent SECOND, the composition is correct for every
    combination, and the viewer's annotation path handles all eight.

    (The EDITOR's apply_edits pipeline uses a different convention and is still
    wrong on cropped+rotated — see
    test_editor_apply_is_still_unsupported_on_cropped_rotated_pages.)

    This is the guard that matters most: it covers the common cases the viewer
    has always got right as well as the ones it did not, so a future change to
    the transforms cannot quietly trade one for the other.
    """
    for cropped in (False, True):
        for rot in (0, 90, 180, 270):
            sid = f'crop-matrix-{int(cropped)}-{rot}'
            entry.sessions[sid] = {
                'pages': [{'id': 'pg1', 'filename': 'm.pdf', 'page_num': 1,
                           'total_pages': 1, 'thumbnail': '',
                           'pdf_bytes': _page(cropped=cropped, rotate=rot,
                                              marker=False)}],
                'comments': {},
            }
            entry.app.config['TESTING'] = True
            try:
                _i, w, h, scale = _render_page(
                    entry.sessions[sid]['pages'][0]['pdf_bytes'])
                asked = [w / 2 - 20, h / 2 - 20, w / 2 + 20, h / 2 + 20]
                with entry.app.test_client() as c:
                    r = c.post('/api/viewer/add_annotation',
                               json={'session_id': sid, 'page_id': 'pg1',
                                     'subtype': 'Square', 'rect': asked,
                                     'border_color': [1.0, 0.0, 0.0],
                                     'fill_color': [1.0, 0.0, 0.0],
                                     'border_width': 3.0},
                               headers={'X-App-Token': entry.APP_TOKEN})
                    assert r.status_code == 200
                out = entry.sessions[sid]['pages'][0]['pdf_bytes']
                blob = _blob(_render(out, scale), _IS_RED)
                where = f'cropped={cropped} /Rotate {rot}'
                assert blob is not None, f'{where}: nothing drawn'
                cx = (blob[0] + blob[2]) / 2 / scale
                cy = (blob[1] + blob[3]) / 2 / scale
                assert abs(cx - w / 2) < 2, f'{where}: dx={cx - w / 2:.1f}'
                assert abs(cy - h / 2) < 2, f'{where}: dy={cy - h / 2:.1f}'
            finally:
                entry.sessions.pop(sid, None)


def test_editor_apply_is_still_unsupported_on_cropped_rotated_pages():
    """A page that is cropped AND rotated is explicitly out of scope for the
    EDITOR's pipeline (routes/editor.py logs 'cropped+rotated page; leaving
    coords in raw space'), and W0 measured content landing up to 100px out.

    W1 fixed the viewer's own annotation path for this combination (see
    test_viewer_annotations_land_correctly_across_the_whole_matrix) but did NOT
    touch apply_edits, which the Phase 1 page tools feed.  So the tools must
    still REFUSE on this geometry rather than misfire: a redaction that silently
    lands elsewhere leaves sensitive content readable while reporting success.

    This pins the trigger condition the refusal keys off.  If a later change
    makes apply_edits handle the combination, this test fails — and that is the
    signal to drop the refusal, not to loosen the assertion.
    """
    for rot in (90, 180, 270):
        b = _page(cropped=True, rotate=rot)
        _i, _w, h, _s = _render_page(b)
        left, top = _editor_origin(b, h)
        # The rotated-uncropped override did NOT run: the origin is still the
        # raw CropBox corner, which is what makes the combination unsupported.
        assert (left, top) == (float(_CB[0]), float(_CB[3])), \
            f'/Rotate {rot}: expected the unsupported-combination fallback'


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
