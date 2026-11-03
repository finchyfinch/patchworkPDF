"""The Comments panel lists every review markup — and comment edits still land
on the right comment.

Before v3.2 `_get_annotations` returned only sticky notes and comment FreeText,
so a reviewer's highlight, cloud or arrow never appeared in the list, and nor
did text drawn with the Text tool.  It now returns those too, marked
`markup: True`.

The trap is the index.  A comment's `idx` is its position among the page's
COMMENTS, and `update_annotation` (the popup's Save / Delete) walks the page
counting comments the same way to find its target.  Put markup into that count
on one side and not the other and the popup edits the wrong comment — so markup
carries `idx: None`, and these tests edit a comment that sits AFTER several
markups to prove the two still agree.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry
from routes import viewer as V


def _blank_page():
    import pypdf
    w = pypdf.PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture()
def session():
    sid = 'comments-list-session'
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'a.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '',
                   'pdf_bytes': _blank_page()}],
        'comments': {},
    }
    yield sid
    entry.sessions.pop(sid, None)


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _post(client, path, body):
    r = client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _add(client, sid, **body):
    return _post(client, '/api/viewer/add_annotation',
                 {'session_id': sid, 'page_id': 'pg1', **body})


def _page(sid):
    return entry.sessions[sid]['pages'][0]['pdf_bytes']


def _populate(client, sid):
    """In /Annots order: shape, sticky, Text-tool text, arrow, highlight,
    comment FreeText.  Two comments, four markups, interleaved."""
    _add(client, sid, subtype='Square', rect=[50, 50, 150, 120])
    _add(client, sid, subtype='Text', rect=[200, 700, 218, 718],
         text='first comment', author='GF')
    _add(client, sid, subtype='FreeText', rect=[50, 600, 250, 640],
         text='typed on the page', is_comment=False)
    _add(client, sid, subtype='Line', line=[300, 300, 400, 350],
         arrow=True, arrow_start=True)
    _add(client, sid, subtype='Highlight', rect=[50, 400, 250, 415])
    return _add(client, sid, subtype='FreeText', rect=[300, 600, 500, 640],
                text='second comment', author='GF', is_comment=True)


def test_every_review_markup_is_listed_in_page_order(client, session):
    _populate(client, session)
    anns = V._get_annotations(_page(session))
    assert [a['subtype'] for a in anns] == [
        'Square', 'Text', 'FreeText', 'Line', 'Highlight', 'FreeText']
    assert [a['markup'] for a in anns] == [True, False, True, True, True, False]


def test_only_comments_carry_a_comment_index(client, session):
    _populate(client, session)
    anns = V._get_annotations(_page(session))
    assert [a['idx'] for a in anns] == [None, 0, None, None, None, 1]


def test_a_double_headed_line_is_reported_as_such(client, session):
    _populate(client, session)
    line = [a for a in V._get_annotations(_page(session)) if a['subtype'] == 'Line'][0]
    assert line['kind'] == 'darrow'


def test_the_popup_edits_the_comment_it_was_opened_on(client, session):
    _populate(client, session)
    second = [a for a in V._get_annotations(_page(session)) if a['idx'] == 1][0]
    _post(client, '/api/viewer/update_annotation',
          {'session_id': session, 'page_id': 'pg1', 'annot_idx': second['idx'],
           'text': 'second comment, edited', 'author': 'GF'})
    texts = [a['text'] for a in V._get_annotations(_page(session))]
    assert texts[5] == 'second comment, edited'
    assert texts[1] == 'first comment', 'the edit landed on the wrong comment'
    assert texts[2] == 'typed on the page'


def test_the_popup_deletes_the_comment_it_was_opened_on(client, session):
    _populate(client, session)
    _post(client, '/api/viewer/update_annotation',
          {'session_id': session, 'page_id': 'pg1', 'annot_idx': 1,
           'delete': True})
    anns = V._get_annotations(_page(session))
    assert [a['subtype'] for a in anns] == [
        'Square', 'Text', 'FreeText', 'Line', 'Highlight']
    assert anns[1]['text'] == 'first comment'


def test_inserted_images_and_signatures_are_not_listed(client, session):
    # 1x1 PNG
    png = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8Dw'
           'HwAFBQIAX8jx0gAAAABJRU5ErkJggg==')
    _add(client, session, subtype='Stamp', rect=[50, 50, 150, 150],
         image='data:image/png;base64,' + png)
    assert V._get_annotations(_page(session)) == []


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
