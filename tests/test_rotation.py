"""Regression tests for the page-rotation coordinate mapping and page sizes.

_raw_rect_to_display maps char/word/search/annotation boxes from raw content
space into the displayed (post-/Rotate) frame; _display_rect_to_raw is its
inverse (used when writing user-drawn annotations back).  Getting either wrong
lands selection highlights and annotations on the wrong part of a rotated page,
so the round-trip identity and the concrete 90° mapping are pinned here.

_page_dimensions reports the size of a page as *displayed*, which is the same
rotation question one level up: a portrait MediaBox with /Rotate 90 is a
landscape page.  The viewer fits its opening zoom to the widest page in the
document from these numbers and then lays each page out at the size pdfium
reports, so the two have to agree — when they did not, a mixed-orientation
document opened zoomed past the viewport with the first page clipped.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pypdf
from pypdf.generic import NameObject, NumberObject

from routes.viewer import _raw_rect_to_display, _display_rect_to_raw
from pdf.pdf_core import _page_dimensions, _page_rotate, _rotated_dims

_W, _H = 600.0, 800.0
_RECT = {'x': 100.0, 'y': 50.0, 'w': 30.0, 'h': 10.0}


def _close(a, b, eps=1e-6):
    return all(abs(a[k] - b[k]) < eps for k in ('x', 'y', 'w', 'h'))


def test_rotate_zero_is_identity():
    assert _raw_rect_to_display(_RECT, 0, _W, _H) == _RECT
    assert _display_rect_to_raw(_RECT, 0, _W, _H) == _RECT


def test_roundtrip_identity_all_rotations():
    for rot in (90, 180, 270):
        disp = _raw_rect_to_display(_RECT, rot, _W, _H)
        back = _display_rect_to_raw(disp, rot, _W, _H)
        assert _close(back, _RECT), f"round-trip failed at {rot}: {back}"


def test_rotate_90_concrete_mapping():
    # Documented mapping for 90°: (x,y,w,h) -> (y, W-(x+w), h, w).
    d = _raw_rect_to_display(_RECT, 90, _W, _H)
    assert _close(d, {'x': 50.0, 'y': _W - 130.0, 'w': 10.0, 'h': 30.0})


def test_rotate_swaps_dimensions_for_90_270():
    for rot in (90, 270):
        d = _raw_rect_to_display(_RECT, rot, _W, _H)
        assert (d['w'], d['h']) == (_RECT['h'], _RECT['w'])   # axes swapped
    for rot in (0, 180):
        d = _raw_rect_to_display(_RECT, rot, _W, _H)
        assert (d['w'], d['h']) == (_RECT['w'], _RECT['h'])   # axes preserved


def test_preserves_extra_keys():
    rect = dict(_RECT, c='A')
    assert _raw_rect_to_display(rect, 90, _W, _H)['c'] == 'A'


# ── page dimensions as displayed ───────────────────────────────────────

_MB_W, _MB_H = 595.0, 842.0   # a portrait MediaBox


def _pdf_bytes(rotations, inherited=None):
    """A PDF with one portrait page per entry in *rotations* (None = no /Rotate).
    *inherited* sets /Rotate on the /Pages node instead of on the pages."""
    writer = pypdf.PdfWriter()
    for rot in rotations:
        page = writer.add_blank_page(width=_MB_W, height=_MB_H)
        if rot is not None:
            page[NameObject('/Rotate')] = NumberObject(rot)
    if inherited is not None:
        writer.root_object['/Pages'][NameObject('/Rotate')] = NumberObject(inherited)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _reader(rotations, inherited=None):
    return pypdf.PdfReader(io.BytesIO(_pdf_bytes(rotations, inherited)))


def test_quarter_turns_swap_the_reported_size():
    reader = _reader([0, 90, 180, 270])
    assert _page_dimensions(reader, 0) == (_MB_W, _MB_H)   # upright
    assert _page_dimensions(reader, 1) == (_MB_H, _MB_W)   # landscape
    assert _page_dimensions(reader, 2) == (_MB_W, _MB_H)   # upside down, still portrait
    assert _page_dimensions(reader, 3) == (_MB_H, _MB_W)   # landscape


def test_page_without_rotate_is_the_mediabox():
    assert _page_dimensions(_reader([None]), 0) == (_MB_W, _MB_H)


def test_rotate_inherited_from_the_pages_node_counts():
    # /Rotate is an inheritable attribute: a document can set it once on /Pages
    # and leave the pages themselves bare.
    reader = _reader([None], inherited=90)
    assert _page_rotate(reader.pages[0]) == 90
    assert _page_dimensions(reader, 0) == (_MB_H, _MB_W)


def test_out_of_range_and_negative_rotations():
    reader = _reader([450, -90, 360])
    assert _page_dimensions(reader, 0) == (_MB_H, _MB_W)   # 450 -> 90
    assert _page_dimensions(reader, 1) == (_MB_H, _MB_W)   # -90 -> 270
    assert _page_dimensions(reader, 2) == (_MB_W, _MB_H)   # 360 -> 0


def test_bad_input_falls_back_to_a4():
    assert _page_dimensions(_reader([0]), 99) == (595.28, 841.89)


def test_agrees_with_the_renderer():
    # The invariant that matters: the page list and the renderer must describe
    # the same page the same way.  pdfium is what draws it, so it is the
    # reference.
    import pypdfium2 as pdfium
    data   = _pdf_bytes([0, 90, 180, 270])
    reader = pypdf.PdfReader(io.BytesIO(data))
    doc    = pdfium.PdfDocument(data)
    try:
        for i in range(4):
            w, h = _page_dimensions(reader, i)
            assert abs(w - doc[i].get_width())  < 0.5, f'width disagrees on page {i}'
            assert abs(h - doc[i].get_height()) < 0.5, f'height disagrees on page {i}'
    finally:
        doc.close()


def test_rotated_dims_helper():
    # Used by /api/rotate to keep a page's recorded size in step with a quarter
    # turn the user applies during the session.
    assert _rotated_dims(10, 20, 90)  == (20, 10)
    assert _rotated_dims(10, 20, 270) == (20, 10)
    assert _rotated_dims(10, 20, 0)   == (10, 20)
    assert _rotated_dims(10, 20, 180) == (10, 20)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
