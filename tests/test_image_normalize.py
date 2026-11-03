"""Regression tests for _normalize_page_for_pdfium's Indexed-image rewrite.

Before rendering, the viewer rewrites Indexed images into a plain device
colorspace to sidestep a pdfium crash.  That rewrite has to reproduce the
original colours exactly — it runs for the main render but not for the
thumbnails, so any mistake shows up as a page that looks wrong in the viewer
while its thumbnail looks right.

The mistake these guard against is mis-reading how wide a palette entry is:
an Indexed palette over an ICCBased-CMYK base has four bytes per entry, and
reading it as one byte per entry slices every entry apart and renders the image
near-black.  Related: an /Indexed image's 2-entry /Decode array must not survive
onto the rewritten image, where it would mean something else entirely.
"""
import io
import zlib

import _bootstrap  # noqa: F401  (path setup)

import pikepdf

from routes.viewer import _indexed_base_ncomp, _normalize_page_for_pdfium

WIDTH, HEIGHT = 4, 2


def _indexed_page(make_base, palette, indices, bpc=8, decode=None):
    """A one-page PDF whose only XObject is an 8-bpc Indexed image.

    *make_base* is called with the new Pdf and returns the palette's base
    colorspace, so bases that need their own objects (ICCBased) can be built
    inside the same file.
    """
    pdf = pikepdf.new()
    base_cs = make_base(pdf)
    hival = len(palette) - 1
    im = pikepdf.Stream(pdf, zlib.compress(bytes(indices)))
    im.Type = pikepdf.Name.XObject
    im.Subtype = pikepdf.Name.Image
    im.Width = WIDTH
    im.Height = HEIGHT
    im.BitsPerComponent = bpc
    im.Filter = pikepdf.Name.FlateDecode
    im.ColorSpace = pikepdf.Array([
        pikepdf.Name.Indexed, base_cs, hival,
        pdf.make_indirect(pikepdf.Stream(pdf, bytes(b for entry in palette for b in entry))),
    ])
    if decode is not None:
        im.Decode = pikepdf.Array(decode)

    content = pikepdf.Stream(pdf, b'q 100 0 0 100 0 0 cm /Im0 Do Q')
    page = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=pikepdf.Array([0, 0, 100, 100]),
        Resources=pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Im0=pdf.make_indirect(im))),
        Contents=pdf.make_indirect(content),
    ))
    pdf.pages.append(pikepdf.Page(page))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _iccbased(n):
    """A factory for an /ICCBased colorspace array declaring *n* components."""
    def make(pdf):
        prof = pikepdf.Stream(pdf, b'\x00' * 16)
        prof.N = n
        return pikepdf.Array([pikepdf.Name.ICCBased, pdf.make_indirect(prof)])
    return make


def _name(cs):
    """A factory for a plain colorspace name."""
    return lambda _pdf: cs


def _image(pdf_bytes):
    """The (already-open) first image XObject of a PDF, as a dict + raw pixels."""
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    obj = pdf.pages[0].Resources.XObject['/Im0']
    return pdf, obj


# ── the reported bug ─────────────────────────────────────────────────────────

def test_cmyk_palette_entries_are_read_four_bytes_wide():
    # Palette over ICCBased N=4: black, white, then two mid tones.
    palette = [(0, 0, 0, 255), (0, 0, 0, 0), (255, 0, 0, 0), (0, 0, 255, 32)]
    indices = [0, 1, 2, 3, 3, 2, 1, 0]
    out = _normalize_page_for_pdfium(
        _indexed_page(_iccbased(4), palette, indices))

    pdf, obj = _image(out)
    with pdf:
        assert str(obj.ColorSpace) == '/DeviceCMYK', \
            'a CMYK palette must stay 4-component, not collapse to grey'
        assert int(obj.BitsPerComponent) == 8
        pixels = bytes(obj.read_bytes())
        assert len(pixels) == WIDTH * HEIGHT * 4
        expected = b''.join(bytes(palette[i]) for i in indices)
        assert pixels == expected, 'palette lookup produced the wrong colours'


def test_cmyk_palette_page_renders_as_it_did_before_normalizing():
    # End-to-end version of the reported symptom: the normalized page is what
    # the viewer rasterizes and the original is what the thumbnail rasterizes,
    # so the two renders have to agree.  A mis-read palette showed up here as a
    # mostly-black page while the thumbnail stayed light.
    import numpy as np
    import pypdfium2 as pdfium

    palette = [(0, 0, 0, 0), (0, 0, 0, 255), (255, 0, 0, 0), (0, 255, 0, 0)]
    original = _indexed_page(_name(pikepdf.Name.DeviceCMYK), palette,
                             [0, 1, 2, 3, 3, 2, 1, 0])
    out = _normalize_page_for_pdfium(original)
    assert out != original, 'the image should have been rewritten'

    def render(pdf_bytes):
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            return np.asarray(doc[0].render(scale=1.0).to_pil().convert('RGB'),
                              dtype=np.int16)
        finally:
            doc.close()

    before, after = render(original), render(out)
    assert before.shape == after.shape
    assert np.abs(before - after).max() <= 2, \
        'the normalized page renders differently from the original'
    # Guard the symptom itself, not just the delta: this page is mostly light.
    assert after.mean() > 128, 'the page came back dark'


# ── the surrounding behaviour this rewrite already had ───────────────────────

def test_rgb_palette_is_expanded_to_devicergb():
    palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 20, 30)]
    indices = [0, 1, 2, 3, 3, 2, 1, 0]
    out = _normalize_page_for_pdfium(
        _indexed_page(_name(pikepdf.Name.DeviceRGB), palette, indices))
    pdf, obj = _image(out)
    with pdf:
        assert str(obj.ColorSpace) == '/DeviceRGB'
        assert bytes(obj.read_bytes()) == b''.join(bytes(palette[i]) for i in indices)


def test_greyscale_rgb_palette_collapses_to_devicegray():
    palette = [(0, 0, 0), (128, 128, 128), (255, 255, 255), (64, 64, 64)]
    indices = [0, 1, 2, 3, 3, 2, 1, 0]
    out = _normalize_page_for_pdfium(
        _indexed_page(_name(pikepdf.Name.DeviceRGB), palette, indices))
    pdf, obj = _image(out)
    with pdf:
        assert str(obj.ColorSpace) == '/DeviceGray'
        assert bytes(obj.read_bytes()) == bytes(palette[i][0] for i in indices)


def test_indexed_decode_array_is_dropped():
    # [0 hival] indexes the palette on the source image; on the rewritten
    # DeviceRGB image pdfium reads it as "keep red, zero green and blue".
    palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 20, 30)]
    out = _normalize_page_for_pdfium(
        _indexed_page(_name(pikepdf.Name.DeviceRGB), palette, [0, 1, 2, 3, 3, 2, 1, 0],
                      decode=[0, 3]))
    pdf, obj = _image(out)
    with pdf:
        assert '/Decode' not in obj.stream_dict


def test_palette_we_cannot_map_is_left_alone():
    # /Separation entries are tint values, not device values — rewriting them as
    # if they were grey would invent colours.  Leave the image as it was.
    sep = pikepdf.Array([pikepdf.Name.Separation, pikepdf.Name('/Spot'),
                         pikepdf.Name.DeviceCMYK])
    original = _indexed_page(_name(sep), [(0,), (128,), (255,), (64,)],
                             [0, 1, 2, 3, 3, 2, 1, 0])
    out = _normalize_page_for_pdfium(original)
    pdf, obj = _image(out)
    with pdf:
        assert str(obj.ColorSpace[0]) == '/Indexed', 'image should be untouched'


def test_short_palette_is_left_alone():
    # A palette with fewer entries than /HiVal claims: reading past it would
    # invent pixels.  hival says 3 (4 entries) but only 2 are present.
    pdf = pikepdf.new()
    im = pikepdf.Stream(pdf, zlib.compress(bytes([0, 1, 0, 1, 1, 0, 1, 0])))
    im.Type = pikepdf.Name.XObject
    im.Subtype = pikepdf.Name.Image
    im.Width, im.Height, im.BitsPerComponent = WIDTH, HEIGHT, 8
    im.Filter = pikepdf.Name.FlateDecode
    im.ColorSpace = pikepdf.Array([
        pikepdf.Name.Indexed, pikepdf.Name.DeviceRGB, 3,
        pdf.make_indirect(pikepdf.Stream(pdf, bytes(6))),   # only 2 RGB entries
    ])
    content = pikepdf.Stream(pdf, b'q 100 0 0 100 0 0 cm /Im0 Do Q')
    page = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Page, MediaBox=pikepdf.Array([0, 0, 100, 100]),
        Resources=pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Im0=pdf.make_indirect(im))),
        Contents=pdf.make_indirect(content)))
    pdf.pages.append(pikepdf.Page(page))
    buf = io.BytesIO()
    pdf.save(buf)

    out = _normalize_page_for_pdfium(buf.getvalue())
    got, obj = _image(out)
    with got:
        assert str(obj.ColorSpace[0]) == '/Indexed', 'image should be untouched'


# ── the helper itself ────────────────────────────────────────────────────────

def test_indexed_base_ncomp_covers_the_device_spaces():
    owner = pikepdf.new()            # keeps the ICC streams below alive
    assert _indexed_base_ncomp(pikepdf.Name.DeviceGray) == 1
    assert _indexed_base_ncomp(pikepdf.Name.DeviceRGB) == 3
    assert _indexed_base_ncomp(pikepdf.Name.DeviceCMYK) == 4
    assert _indexed_base_ncomp(_iccbased(1)(owner)) == 1
    assert _indexed_base_ncomp(_iccbased(3)(owner)) == 3
    assert _indexed_base_ncomp(_iccbased(4)(owner)) == 4
    assert _indexed_base_ncomp(pikepdf.Array(
        [pikepdf.Name.CalRGB, pikepdf.Dictionary()])) == 3
    # Not convertible → None, so the caller leaves the image alone.
    assert _indexed_base_ncomp(pikepdf.Array(
        [pikepdf.Name.Lab, pikepdf.Dictionary()])) is None
    assert _indexed_base_ncomp(_iccbased(2)(owner)) is None
    assert _indexed_base_ncomp(pikepdf.Name('/Whatever')) is None


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
