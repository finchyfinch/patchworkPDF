"""Layout's "Split document from here" (v3.4): the right-clicked page and every
later page leave the document and become a new, unsaved one."""
import _bootstrap  # noqa: F401

import pytest

import entry
from pdf.pdf_core import _create_blank_page_bytes


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _doc(sid, n=5):
    pages = [{'id': f'p{i}', 'filename': 'a.pdf', 'page_num': i + 1,
              'total_pages': n, 'thumbnail': '', 'rotation': 0,
              'pdf_bytes': _create_blank_page_bytes(200, 300)} for i in range(n)]
    entry.sessions[sid] = {
        'pages': pages, 'first_file_path': 'C:/docs/a.pdf', 'comments': {},
        'bookmarks': [{'id': 'b1', 'page_id': 'p1', 'title': 'one'},
                      {'id': 'b3', 'page_id': 'p3', 'title': 'three'}],
        'annot_undo': [('p1', b'x', {}), ('p4', b'y', {})],
    }
    return sid


def test_split_moves_the_tail_into_a_new_unsaved_session(client):
    sid = _doc('splitA')
    r = _post(client, '/api/split_from', {'session_id': sid, 'page_id': 'p2'})
    body = r.get_json()
    assert r.status_code == 200, body
    assert body['moved'] == 3 and body['remaining'] == 2
    assert [p['id'] for p in entry.sessions[sid]['pages']] == ['p0', 'p1']
    new = entry.sessions[body['session_id']]
    assert [p['id'] for p in new['pages']] == ['p2', 'p3', 'p4']
    assert body['page_id'] == 'p2'
    assert new['first_file_path'] is None          # unsaved: Save asks where
    assert all(p['filename'] == 'a (from page 3).pdf' for p in new['pages'])
    # Bookmarks follow their page; undo entries for moved pages are dropped.
    assert [b['id'] for b in entry.sessions[sid]['bookmarks']] == ['b1']
    assert [b['id'] for b in new['bookmarks']] == ['b3']
    assert [e[0] for e in entry.sessions[sid]['annot_undo']] == ['p1']
    # The new document is a real one: the viewer can list it.
    pl = _post(client, '/api/viewer/pages', {'session_id': body['session_id']})
    if pl.status_code == 200:
        assert len(pl.get_json().get('pages', [])) == 3


def test_split_at_the_first_page_is_refused_and_changes_nothing(client):
    sid = _doc('splitB')
    r = _post(client, '/api/split_from', {'session_id': sid, 'page_id': 'p0'})
    assert r.status_code == 400
    assert r.get_json()['error_key'] == 'err.split_first_page'
    assert len(entry.sessions[sid]['pages']) == 5


def test_split_unknown_page_is_404(client):
    sid = _doc('splitC')
    assert _post(client, '/api/split_from',
                 {'session_id': sid, 'page_id': 'nope'}).status_code == 404
