"""Regression test for true image redaction (_burn_redactions_into_images).

The security promise is that redacting over image content permanently destroys
the pixels — not just covers them with a black rectangle that can be deleted.
This builds a one-page PDF whose only content is a solid-red 100x100 image
placed 1:1 over a 100x100 page, redacts the bottom-left quadrant, and asserts the
underlying image pixels there are now black while the rest stay red.
"""
import io
import zlib

import _bootstrap  # noqa: F401  (path setup)

import pikepdf
from PIL import Image

from routes.editor import _burn_redactions_into_images


def _one_image_page():
    """A 100x100-pt page showing a solid-red 100x100 image at CTM 100 0 0 100 0 0."""
    pdf = pikepdf.new()
    img = Image.new('RGB', (100, 100), (255, 0, 0))
    im = pikepdf.Stream(pdf, zlib.compress(img.tobytes()))
    im.Type = pikepdf.Name.XObject
    im.Subtype = pikepdf.Name.Image
    im.Width = 100
    im.Height = 100
    im.ColorSpace = pikepdf.Name.DeviceRGB
    im.BitsPerComponent = 8
    im.Filter = pikepdf.Name.FlateDecode

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


def _image_pixels(pdf_bytes):
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        xobjs = pdf.pages[0].Resources.XObject
        obj = xobjs['/Im0']
        return pikepdf.PdfImage(obj).as_pil_image().convert('RGB')


def test_redaction_blacks_out_only_the_redacted_region():
    original = _one_image_page()
    # Redact the bottom-left quadrant in PDF space (y-up): (0,0)-(50,50).
    burned = _burn_redactions_into_images(original, [(0.0, 0.0, 50.0, 50.0)])
    assert burned != original                       # something was rewritten

    px = _image_pixels(burned)
    # PDF y=0 is the page bottom; image row 0 is the top.  So the redacted
    # bottom-left quadrant maps to image rows 50..100, cols 0..50.
    assert px.getpixel((25, 75)) == (0, 0, 0), "redacted pixels not destroyed"
    # The opposite (top-right in the image = top-right on the page) is untouched.
    assert px.getpixel((75, 25)) == (255, 0, 0), "non-redacted pixels were altered"


def test_no_redactions_leaves_bytes_unchanged():
    original = _one_image_page()
    assert _burn_redactions_into_images(original, []) is original


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
