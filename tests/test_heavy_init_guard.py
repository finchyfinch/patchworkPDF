"""The viewer's page tools must work in the first moments after launch.

`routes/editor` loads pdfium and the OCR stack lazily, in a background thread
started once the window is up.  Its before_request guard used to force that
load only for /api/editor/* — the Editor's own routes, which v3.1 removed —
while the VIEWER's editing routes, which call straight into the module, had no
guard at all.  Edit text used in the first seconds after launch failed with
`pdfium` still None, and the mixed-page check quietly answered "no".
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pypdf
import pytest

import entry
from routes import editor as _editor


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _text_page():
    w = pypdf.PdfWriter()
    w.add_page(pypdf.PdfReader('tests/fixtures/text_page.pdf').pages[1])
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def test_page_elements_before_the_background_load(client, monkeypatch):
    _editor._heavy_init()                       # make sure the real deps exist...
    monkeypatch.setattr(_editor, 'pdfium', None)       # ...then pretend they don't
    monkeypatch.setattr(_editor, '_INIT_DONE', False)
    entry.sessions['early'] = {
        'pages': [{'id': 'pg1', 'filename': 's.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': _text_page()}],
        'comments': {}}
    try:
        r = client.post('/api/viewer/page_elements',
                        json={'session_id': 'early', 'page_id': 'pg1'},
                        headers={'X-App-Token': entry.APP_TOKEN})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()['items'], 'no text found: the heavy deps were not loaded'
        assert _editor.pdfium is not None, 'the guard did not run the load'
    finally:
        entry.sessions.pop('early', None)


@pytest.mark.parametrize('path', [
    '/api/viewer/page_elements', '/api/viewer/page_text', '/api/viewer/edit_text',
    '/api/viewer/edit_block', '/api/viewer/add_text', '/api/viewer/redact',
    '/api/viewer/watermark', '/api/viewer/watermark_all', '/api/viewer/crop',
    '/api/header_footer'])
def test_every_route_that_calls_into_the_module_is_guarded(path):
    guard = _editor.register_editor_routes.__code__.co_consts
    needs = next(c for c in guard if isinstance(c, tuple) and '/api/viewer/page_elements' in c)
    assert path.startswith(needs), f'{path} can run before pdfium is loaded'


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
