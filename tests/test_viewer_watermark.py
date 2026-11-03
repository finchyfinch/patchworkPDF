"""The viewer's single-page Watermark tool (W4 of the editor→viewer merge).

The watermark is the odd one out among the page tools, and the reason is worth
stating because it drove the design: **it takes no client geometry**.
apply_edits derives the page centre from the CropBox itself, so a watermark
lands correctly on all eight {plain, cropped} x {0, 90, 180, 270} combinations —
including the cropped+rotated case that Redact and Crop have to refuse.  That
was measured before the route was written, not assumed from Redact's shape, and
test_watermark_is_centred_on_every_geometry is what keeps it true.

The other half is what is deliberately NOT here.  All-pages watermarking would
push one byte snapshot per page against a 30-entry / 64 MB history, so "undo"
would half-revert a long document — worse than no undo at all.  It stays a
document-level command outside the undo stack (plan §3.5), and this route is
single-page only.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pikepdf
import pypdfium2 as pdfium
import pytest

import entry

_MB = (0, 0, 400, 400)
_CB = (50, 80, 350, 300)
_SCALE = 2.0


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


def _render(pdf_bytes):
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        p = doc[0]
        return (p.render(scale=_SCALE).to_pil().convert('RGB'),
                p.get_width(), p.get_height())
    finally:
        doc.close()


def _ink_bbox(img):
    """Bounding box of anything that is not white — the whole fixture page is
    blank, so any ink is the watermark."""
    W, H = img.size
    px = img.load()
    xs, ys = [], []
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y]
            if r < 230 or g < 230 or b < 230:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _session(pdf_bytes, sid='wm'):
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'w.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': pdf_bytes}],
        'comments': {},
    }
    return sid


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


_CFG = {'text': 'DRAFT', 'color': '#808080', 'opacity': 0.6,
        'size': 40, 'angleDeg': 0, 'layout': 'center'}


def _stamp(client, sid, **over):
    cfg = dict(_CFG)
    cfg.update(over)
    return _post(client, '/api/viewer/watermark',
                 {'session_id': sid, 'page_id': 'pg1', 'watermark': cfg})


def _bytes(sid):
    return entry.sessions[sid]['pages'][0]['pdf_bytes']


# ── placement ───────────────────────────────────────────────────────────────

def test_watermark_is_centred_on_every_geometry(client):
    """All eight combinations, including cropped+rotated.

    This is why the watermark needs neither _page_tool_offset nor
    _page_tool_refusal: it never sees a client coordinate.  If a change ever
    makes apply_edits take the watermark position from the caller, this test
    fails and the refusal has to be added.
    """
    for cropped in (False, True):
        for rot in (0, 90, 180, 270):
            sid = _session(_page(cropped, rot), f'wm-{int(cropped)}-{rot}')
            try:
                r = _stamp(client, sid)
                where = f'cropped={cropped} /Rotate {rot}'
                assert r.status_code == 200, f'{where}: {r.get_data(as_text=True)}'
                img, w, h = _render(_bytes(sid))
                bb = _ink_bbox(img)
                assert bb is not None, f'{where}: nothing was drawn'
                cx = (bb[0] + bb[2]) / 2 / _SCALE
                cy = (bb[1] + bb[3]) / 2 / _SCALE
                assert abs(cx - w / 2) < 4, f'{where}: dx={cx - w / 2:.1f}'
                assert abs(cy - h / 2) < 4, f'{where}: dy={cy - h / 2:.1f}'
            finally:
                entry.sessions.pop(f'wm-{int(cropped)}-{rot}', None)


def test_tiled_layout_covers_much_more_of_the_page(client):
    sid_c = _session(_page(), 'wm-centre')
    sid_t = _session(_page(), 'wm-tiled')
    try:
        _stamp(client, sid_c, layout='center')
        _stamp(client, sid_t, layout='tiled')
        c_bb = _ink_bbox(_render(_bytes(sid_c))[0])
        t_bb = _ink_bbox(_render(_bytes(sid_t))[0])
        c_area = (c_bb[2] - c_bb[0]) * (c_bb[3] - c_bb[1])
        t_area = (t_bb[2] - t_bb[0]) * (t_bb[3] - t_bb[1])
        assert t_area > c_area * 3, f'tiled={t_area} vs centre={c_area}'
    finally:
        entry.sessions.pop('wm-centre', None)
        entry.sessions.pop('wm-tiled', None)


def test_the_page_underneath_still_shows_through(client):
    """The watermark is translucent by design — it must not black out content."""
    sid = _session(_page())
    try:
        _stamp(client, sid, opacity=0.25, size=200)
        img, _w, _h = _render(_bytes(sid))
        px = img.load()
        W, H = img.size
        # The very centre is under the text; it should be tinted, not opaque.
        r, g, b = px[W // 2, H // 2]
        assert not (r < 40 and g < 40 and b < 40), 'watermark rendered opaque'
    finally:
        entry.sessions.pop(sid, None)


# ── validation: these values reach a content stream ─────────────────────────

def test_empty_text_is_rejected_and_changes_nothing(client):
    sid = _session(_page())
    try:
        before = _bytes(sid)
        for txt in ('', '   '):
            r = _stamp(client, sid, text=txt)
            assert r.status_code == 400, repr(txt)
            assert (r.get_json() or {}).get('error_key') == 'err.watermark_settings'
        assert _bytes(sid) == before
        assert not entry.sessions[sid].get('annot_undo'), \
            'a rejected watermark must not spend an undo slot'
    finally:
        entry.sessions.pop(sid, None)


def test_out_of_range_values_are_clamped_not_refused(client):
    """A silly size should give a sane watermark, not a 500 or a vast stream."""
    sid = _session(_page())
    try:
        r = _stamp(client, sid, size=99999, opacity=50, angleDeg=1e6)
        assert r.status_code == 200, r.get_data(as_text=True)
        assert _ink_bbox(_render(_bytes(sid))[0]) is not None
    finally:
        entry.sessions.pop(sid, None)


def test_a_bad_colour_degrades_to_the_default_not_to_black(client):
    """`_hex_to_rgb` in apply_edits already fails safe, so a malformed colour
    can never reach the content stream — validating here is not what stops an
    injection, and a test that only checked for injection would pass with the
    validation removed.

    What the validation actually buys is the FALLBACK: without it a bad colour
    becomes black (_hex_to_rgb's except branch), which on a watermark is a very
    visible wrong answer.  With it the user gets the default grey.  That is the
    difference this pins.
    """
    sid = _session(_page())
    try:
        assert _stamp(client, sid, color='not-a-colour',
                      size=120, opacity=0.9).status_code == 200
        img, _w, _h = _render(_bytes(sid))
        px = img.load()
        W, H = img.size
        darkest = min((px[x, y] for y in range(H) for x in range(W)),
                      key=lambda p: p[0] + p[1] + p[2])
        assert sum(darkest) > 90, \
            f'watermark rendered near-black {darkest} — fell through to ' \
            '_hex_to_rgb\'s except branch instead of the grey default'
    finally:
        entry.sessions.pop(sid, None)


def test_a_bad_layout_falls_back_to_centre(client):
    sid = _session(_page())
    try:
        r = _stamp(client, sid, layout='nonsense')
        assert r.status_code == 200
        img, w, h = _render(_bytes(sid))
        bb = _ink_bbox(img)
        cx = (bb[0] + bb[2]) / 2 / _SCALE
        assert abs(cx - w / 2) < 4
    finally:
        entry.sessions.pop(sid, None)


def test_unknown_page_is_reported(client):
    sid = _session(_page())
    try:
        r = _post(client, '/api/viewer/watermark',
                  {'session_id': sid, 'page_id': 'nope', 'watermark': _CFG})
        assert r.status_code == 404
    finally:
        entry.sessions.pop(sid, None)


# ── it joins the existing undo stack ────────────────────────────────────────

def test_one_watermark_is_one_undo_and_it_comes_back(client):
    sid = _session(_page())
    try:
        before = _bytes(sid)
        r = _stamp(client, sid)
        assert r.get_json()['undo_count'] == 1
        assert _ink_bbox(_render(_bytes(sid))[0]) is not None

        u = _post(client, '/api/viewer/undo', {'session_id': sid})
        assert u.status_code == 200
        assert _bytes(sid) == before, 'undo did not restore the original bytes'
        assert _ink_bbox(_render(_bytes(sid))[0]) is None, 'watermark still visible'
    finally:
        entry.sessions.pop(sid, None)


def test_two_watermarks_are_two_undo_steps(client):
    """Stamping twice stacks two marks and two steps — the second must not be
    folded into the first, or one Ctrl+Z removes work the user did twice."""
    sid = _session(_page())
    try:
        _stamp(client, sid, text='ONE')
        r = _stamp(client, sid, text='TWO')
        assert r.get_json()['undo_count'] == 2
        _post(client, '/api/viewer/undo', {'session_id': sid})
        assert _ink_bbox(_render(_bytes(sid))[0]) is not None, \
            'one undo removed both watermarks'
    finally:
        entry.sessions.pop(sid, None)


def test_a_watermark_marks_the_document_dirty(client):
    sid = _session(_page())
    try:
        _stamp(client, sid)
        from bootstrap import recovery as _recovery
        assert _recovery.has_pending(entry.sessions[sid])
    finally:
        entry.sessions.pop(sid, None)


def test_the_single_page_route_only_touches_one_page(client):
    """W4 asserted this by checking no /api/viewer/watermark_all route existed.
    W5 added that route deliberately, so the path name is no longer the thing
    worth pinning — what matters is that the PANEL tool stays per-page.  A
    watermark from the Tools panel must never reach a second page, or the undo
    step the user was promised does not cover what actually changed.
    """
    entry.sessions['wm-one-page'] = {
        'pages': [{'id': f'pg{i}', 'filename': 'w.pdf', 'page_num': i + 1,
                   'total_pages': 3, 'thumbnail': '', 'pdf_bytes': _page()}
                  for i in range(3)],
        'comments': {},
    }
    try:
        r = _post(client, '/api/viewer/watermark',
                  {'session_id': 'wm-one-page', 'page_id': 'pg1',
                   'watermark': _CFG})
        assert r.status_code == 200
        pages = entry.sessions['wm-one-page']['pages']
        assert _ink_bbox(_render(pages[1]['pdf_bytes'])[0]) is not None, \
            'the targeted page was not stamped'
        for i in (0, 2):
            assert _ink_bbox(_render(pages[i]['pdf_bytes'])[0]) is None, \
                f'pg{i} was stamped by a single-page watermark'
        assert len(entry.sessions['wm-one-page']['annot_undo']) == 1
    finally:
        entry.sessions.pop('wm-one-page', None)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
