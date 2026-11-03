"""Editing a SCANNED page more than once.

`_extract_text_and_graphics` decides whether to OCR from a char count: a page
with no extractable text is a scan, and gets OCR'd; anything else takes the
native path.  That is fine until the viewer writes into the page, because
`apply_edits` injects REAL text operators.  One edit and the page no longer
looks like a scan, so the next extraction takes the native path, finds only the
run that was just injected, and every other word on the page stops being
offered — the Pointer goes dead for the rest of the page after a single change.
Measured on a 178-word scanned invoice: the second load returned 1 item.

The editor already solved this.  `editor_apply` stashes the untouched scan on
the page record as `ocr_source_bytes` before its first rewrite, and
`_extract_text_and_graphics` then OCRs THAT while taking native runs from the
current bytes.  These tests pin the viewer doing the same thing, at the same
moment — before the first rewrite, while the bytes are still the scan.

A page with no extractable text IS what production calls image-only (that is
literally the check), so a blank page is a faithful stand-in here and keeps the
suite independent of whether Tesseract is installed.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pypdf
import pytest

import entry
from routes import editor as _editor


@pytest.fixture(scope='module', autouse=True)
def _heavy():
    _editor._heavy_init()


def _no_text_page():
    """A page with zero extractable characters — image-only, as far as the
    production check (`is_image_only_page`) is concerned."""
    w = pypdf.PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _text_page():
    w = pypdf.PdfWriter()
    w.add_page(pypdf.PdfReader('tests/fixtures/text_page.pdf').pages[1])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _session(page_bytes, sid='scanedit'):
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 's.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': page_bytes}],
        'comments': {},
    }
    return sid


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _rec(sid):
    return entry.sessions[sid]['pages'][0]


def _stamp(client, sid, text='Hello'):
    return _post(client, '/api/viewer/add_text',
                 {'session_id': sid, 'page_id': 'pg1',
                  'item': {'type': 'text', 'text': text, 'x': 100, 'y': 700,
                           'size': 12, 'color': '#000000',
                           'rl_font': 'Helvetica'}})


# ── the stash ───────────────────────────────────────────────────────────────

def test_writing_into_a_scan_keeps_the_pristine_scan(client):
    sid = _session(_no_text_page())
    try:
        original = _rec(sid)['pdf_bytes']
        assert _stamp(client, sid).status_code == 200
        assert _rec(sid)['pdf_bytes'] != original, 'the page was not rewritten'
        assert _rec(sid).get('ocr_source_bytes') == original, \
            'the scan was not kept, so its other words are now unreachable'
    finally:
        entry.sessions.pop(sid, None)


def test_the_scan_is_kept_from_BEFORE_the_first_edit_not_after(client):
    """A second edit must not overwrite the stash with once-edited bytes —
    that would bake the first change into the OCR source and lose whatever it
    covered."""
    sid = _session(_no_text_page())
    try:
        original = _rec(sid)['pdf_bytes']
        _stamp(client, sid, 'One')
        _stamp(client, sid, 'Two')
        assert _rec(sid)['ocr_source_bytes'] == original
    finally:
        entry.sessions.pop(sid, None)


def test_a_native_page_records_None_and_is_not_re_checked(client):
    """None is the 'already looked' marker.  Without it every mutation on a
    native page would pay for another pdfium open to answer the same question."""
    sid = _session(_text_page())
    try:
        assert _stamp(client, sid).status_code == 200
        assert 'ocr_source_bytes' in _rec(sid), 'the answer was not cached'
        assert _rec(sid)['ocr_source_bytes'] is None
    finally:
        entry.sessions.pop(sid, None)


# ── and it is actually used ─────────────────────────────────────────────────

def test_page_elements_extracts_against_the_pristine_scan(client, monkeypatch):
    """The stash is worthless unless the extraction is told about it.  Capturing
    the call is the honest assertion here: proving it end to end would need
    Tesseract, which is not a test dependency."""
    seen = {}
    real = _editor._extract_text_and_graphics

    def spy(pdf_bytes, ocr_source_bytes=None, progress_cb=None):
        seen['src'] = ocr_source_bytes
        seen['cur'] = pdf_bytes
        return real(pdf_bytes, ocr_source_bytes, progress_cb=progress_cb)

    monkeypatch.setattr(_editor, '_extract_text_and_graphics', spy)

    sid = _session(_no_text_page())
    try:
        original = _rec(sid)['pdf_bytes']
        _stamp(client, sid)
        r = _post(client, '/api/viewer/page_elements',
                  {'session_id': sid, 'page_id': 'pg1'})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert seen['src'] == original, \
            'extraction was not pointed at the untouched scan'
        assert seen['cur'] == _rec(sid)['pdf_bytes'], \
            'native runs must still come from the CURRENT bytes'
    finally:
        entry.sessions.pop(sid, None)


def test_an_unedited_scan_is_extracted_directly(client, monkeypatch):
    """Nothing has been injected yet, so there is no second source to merge and
    the straightforward path must be taken."""
    seen = {}
    real = _editor._extract_text_and_graphics

    def spy(pdf_bytes, ocr_source_bytes=None, progress_cb=None):
        seen['src'] = ocr_source_bytes
        return real(pdf_bytes, ocr_source_bytes, progress_cb=progress_cb)

    monkeypatch.setattr(_editor, '_extract_text_and_graphics', spy)

    sid = _session(_no_text_page())
    try:
        _post(client, '/api/viewer/page_elements',
              {'session_id': sid, 'page_id': 'pg1'})
        assert seen['src'] is None
    finally:
        entry.sessions.pop(sid, None)


def test_a_failed_check_does_not_break_the_edit(client, monkeypatch):
    """The stash is an optimisation for a later load; it must never be the
    reason an edit fails."""
    def boom(_b):
        raise RuntimeError('pdfium unavailable')

    monkeypatch.setattr(_editor, 'is_image_only_page', boom)
    sid = _session(_no_text_page())
    try:
        assert _stamp(client, sid).status_code == 200
        assert _rec(sid)['ocr_source_bytes'] is None
    finally:
        entry.sessions.pop(sid, None)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
