"""Regression tests for following a document's embedded URL links.

A /Link annotation carrying a /URI action becomes a clickable hit-area in the
viewer, and clicking it hands the URL to the OS default browser.  The whole
feature turns on two things being right, and both are pinned here.

GEOMETRY.  /Rect lives in raw content space; the viewer draws in the displayed
(post-/Rotate) frame.  The same trap `_get_annotations` already has to dodge —
skip the mapping and every link on a rotated page sits somewhere else.

WHAT WE HAND THE SHELL.  A /URI can name anything the producer felt like, and
the far end of this is `os.startfile` / `xdg-open`, which resolve `file:`,
`javascript:` and every registered protocol handler on the machine.  So:

  * the scheme allow-list decides, not a list of things to strip;
  * the page sends an INDEX and the URL is re-read here from the document, so
    the one thing the route cannot be made to do is open a destination that is
    not in the PDF the user is looking at.

The second is the load-bearing one.  /api/open_external (entry.py) answers only
for the update prompt's fixed SourceForge address for the same reason; this
route needs an arbitrary destination, so it takes it from the document instead
of from the caller.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry
from platform_io import platform_paths as _pp
from routes import viewer as V


# ── fixtures ─────────────────────────────────────────────────────────────────

def _page_with_link(uri, rect=(72, 700, 300, 720), rotate=0,
                    subtype='/Link', action='uri'):
    """A one-page PDF carrying a single annotation, as bytes."""
    import pypdf
    from pypdf.generic import (DictionaryObject, NameObject, ArrayObject,
                               FloatObject, TextStringObject)
    w = pypdf.PdfWriter()
    w.add_blank_page(width=612, height=792)
    page = w.pages[0]
    if rotate:
        page.rotate(rotate)
    ann = DictionaryObject()
    ann[NameObject('/Type')]    = NameObject('/Annot')
    ann[NameObject('/Subtype')] = NameObject(subtype)
    ann[NameObject('/Rect')]    = ArrayObject([FloatObject(v) for v in rect])
    if action == 'uri':
        a = DictionaryObject()
        a[NameObject('/S')]   = NameObject('/URI')
        a[NameObject('/URI')] = TextStringObject(uri)
        ann[NameObject('/A')] = a
    elif action == 'goto':                       # internal jump, not a browser link
        a = DictionaryObject()
        a[NameObject('/S')] = NameObject('/GoTo')
        ann[NameObject('/A')] = a
    elif action == 'dest':
        ann[NameObject('/Dest')] = ArrayObject([])
    page[NameObject('/Annots')] = ArrayObject([ann])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _blank_page():
    import pypdf
    w = pypdf.PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture()
def session(monkeypatch):
    """A one-page session holding a page with one https link."""
    sid = 'links-test-session'
    entry.sessions[sid] = {'pages': [{'id': 'pg1',
                                      'pdf_bytes': _page_with_link('https://example.com/a')}],
                           'comments': {}}
    yield sid
    entry.sessions.pop(sid, None)


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


@pytest.fixture()
def launched(monkeypatch):
    """Record what would have been handed to the OS browser, and launch nothing.

    viewer.py imports platform_paths inside the handler, so patching the module
    attribute is what the route actually reads."""
    urls = []
    monkeypatch.setattr(_pp, 'open_url_in_default_browser',
                        lambda url: (urls.append(url), True)[1])
    return urls


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


# ── what counts as a link ────────────────────────────────────────────────────

@pytest.mark.parametrize('uri,expected', [
    ('https://example.com/a?b=1', 'https://example.com/a?b=1'),
    ('http://example.com',        'http://example.com'),
    ('mailto:someone@example.com', 'mailto:someone@example.com'),
    # Word and older Acrobat write typed links with no scheme at all.
    ('www.example.com',           'https://www.example.com'),
    ('   https://example.com   ', 'https://example.com'),
])
def test_followable_uris_become_links(uri, expected):
    links = V._get_links(_page_with_link(uri))
    assert len(links) == 1
    assert links[0]['url'] == expected


@pytest.mark.parametrize('uri', [
    'javascript:alert(1)',              # runs in whatever opens it
    'file:///C:/Windows/win.ini',       # opens a local file through the shell
    'ms-msdt:/id PCWDiagnostic',        # a registered protocol handler (Follina)
    'vbscript:msgbox(1)',
    'data:text/html,<script>x</script>',
    'https://ok.example.com\njavascript:alert(1)',   # second line past the check
    'https://ok.example.com\x00javascript:alert(1)',
    '',
])
def test_uris_we_will_not_follow_get_no_hit_area(uri):
    """The allow-list decides.  A link with no hit-area is also the honest
    answer — the viewer genuinely cannot follow it."""
    assert V._get_links(_page_with_link(uri)) == []


def test_an_over_long_uri_is_refused():
    assert V._get_links(_page_with_link('https://e.com/' + 'a' * 4000)) == []


@pytest.mark.parametrize('action', ['goto', 'dest'])
def test_internal_jumps_are_not_browser_links(action):
    """A /GoTo is a real link, just not one that belongs in a browser.  Giving
    it a hit-area that opens nothing would be worse than leaving it alone."""
    assert V._get_links(_page_with_link('', action=action)) == []


def test_other_annotation_subtypes_are_ignored():
    assert V._get_links(_page_with_link('https://e.com', subtype='/Square')) == []


def test_a_page_with_no_annotations_is_cheap_and_empty():
    assert V._get_links(_blank_page()) == []


def test_damaged_bytes_yield_no_links_rather_than_an_error():
    assert V._get_links(b'not a pdf at all') == []


# ── geometry ─────────────────────────────────────────────────────────────────

def test_rect_is_reported_bottom_left_with_positive_extent():
    (link,) = V._get_links(_page_with_link('https://e.com', rect=(72, 700, 300, 720)))
    assert (link['x'], link['y'], link['w'], link['h']) == (72, 700, 228, 20)


def test_a_rect_given_corner_reversed_is_normalised():
    """/Rect corners are not required to be ordered, and producers do reverse
    them.  Taken literally that is a negative width — an unclickable link."""
    (link,) = V._get_links(_page_with_link('https://e.com', rect=(300, 720, 72, 700)))
    assert (link['x'], link['y'], link['w'], link['h']) == (72, 700, 228, 20)


def test_a_zero_area_rect_is_dropped():
    assert V._get_links(_page_with_link('https://e.com', rect=(72, 700, 72, 700))) == []


def test_links_on_a_rotated_page_are_mapped_to_the_displayed_frame():
    """The viewer's pdfToDisplay applies a y-flip and a scale, no rotation — so
    a /Rotate 90 page whose link is not mapped here puts the hit-area on the
    wrong part of the page entirely."""
    (link,) = V._get_links(_page_with_link('https://e.com', rect=(72, 700, 300, 720),
                                           rotate=90))
    # 612×792 raw becomes 792×612 displayed; the strip along the top-left of the
    # unrotated page becomes a strip down the right-hand side.
    assert (link['x'], link['y'], link['w'], link['h']) == (700, 312, 20, 228)
    assert link['x'] + link['w'] <= 792 and link['y'] + link['h'] <= 612


# ── the routes ───────────────────────────────────────────────────────────────

def test_links_route_returns_them_keyed_by_page(client, session):
    r = _post(client, '/api/viewer/links', {'session_id': session})
    assert r.status_code == 200
    by_page = r.get_json()['links_by_page']
    assert list(by_page) == ['pg1']
    assert by_page['pg1'][0]['url'] == 'https://example.com/a'
    assert by_page['pg1'][0]['idx'] == 0


def test_open_link_resolves_the_url_from_the_document(client, session, launched):
    r = _post(client, '/api/viewer/open_link',
              {'session_id': session, 'page_id': 'pg1', 'idx': 0})
    assert r.status_code == 200
    assert launched == ['https://example.com/a']
    assert r.get_json()['url'] == 'https://example.com/a'


def test_open_link_ignores_a_url_supplied_by_the_caller(client, session, launched):
    """The whole point of indexing: the destination comes from the PDF, so a
    caller naming one of its own is simply not consulted."""
    r = _post(client, '/api/viewer/open_link',
              {'session_id': session, 'page_id': 'pg1', 'idx': 0,
               'url': 'https://evil.example.com/', 'file_path': 'C:/Windows/win.ini'})
    assert r.status_code == 200
    assert launched == ['https://example.com/a']


@pytest.mark.parametrize('idx', [-1, 1, 99, 'nonsense', None])
def test_an_index_that_names_no_link_opens_nothing(client, session, launched, idx):
    r = _post(client, '/api/viewer/open_link',
              {'session_id': session, 'page_id': 'pg1', 'idx': idx})
    assert r.status_code == 404
    assert launched == []


def test_open_link_reports_a_failure_to_launch(client, session, monkeypatch):
    monkeypatch.setattr(_pp, 'open_url_in_default_browser', lambda url: False)
    r = _post(client, '/api/viewer/open_link',
              {'session_id': session, 'page_id': 'pg1', 'idx': 0})
    assert r.status_code == 500
    # Every /api/ error answers in JSON — the viewer parses replies with
    # res.json() and an HTML error page surfaces as a parser error (CLAUDE.md).
    assert r.get_json()['error_key'] == 'err.link_open_failed'


def test_unknown_session_and_page_are_refused(client, session):
    assert _post(client, '/api/viewer/open_link',
                 {'session_id': 'nope', 'page_id': 'pg1', 'idx': 0}).status_code == 404
    assert _post(client, '/api/viewer/open_link',
                 {'session_id': session, 'page_id': 'nope', 'idx': 0}).status_code == 404
    assert _post(client, '/api/viewer/links',
                 {'session_id': 'nope'}).status_code == 404


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
