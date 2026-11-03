"""Review threads: author and date on everything, replies, status (v3.2).

A reply is the standard PDF shape — a /Text annotation whose /IRT points at the
annotation it answers — and a review status is the same plus /StateModel
/Review and /State.  So threads written here read as threads in Acrobat, and
Acrobat's read as threads here.  What has to hold:

  * Replies are never items of their own.  They ride on their parent in the
    Comments list, draw nothing on the page, and — the trap — take no comment
    index, or the sticky-note popup's Save/Delete would land on a reply.
  * One status per item: a new one replaces the old; 'None' clears it.
  * A thread goes where its parent goes.  Deleting the parent deletes it;
    LIFTING the parent (which deletes and re-adds it, to move or restyle a
    shape) must bring it back, dates and all — otherwise moving a shape would
    silently drop the discussion on it.
  * Each thread change is one undo step.
"""
import io
import json

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
    sid = 'comment-threads-session'
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'a.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': _blank_page()}],
        'comments': {},
    }
    yield sid
    entry.sessions.pop(sid, None)


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _post(client, path, body, status=200):
    r = client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})
    assert r.status_code == status, r.get_json()
    return r.get_json()


def _add(client, sid, **body):
    return _post(client, '/api/viewer/add_annotation',
                 {'session_id': sid, 'page_id': 'pg1', **body})


def _thread(client, sid, **body):
    return _post(client, '/api/viewer/comment_thread',
                 {'session_id': sid, 'page_id': 'pg1', **body})


def _listed(sid):
    return V._get_annotations(entry.sessions[sid]['pages'][0]['pdf_bytes'])


def _raw_annots(sid):
    import pypdf
    r = pypdf.PdfReader(io.BytesIO(entry.sessions[sid]['pages'][0]['pdf_bytes']))
    annots = r.pages[0].get('/Annots') or []
    return [a.get_object() for a in annots]


# ── author and dates ────────────────────────────────────────────────────────

def test_everything_added_is_stamped_with_author_and_dates(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120], author='Ana')
    a = _raw_annots(session)[0]
    assert str(a['/T']) == 'Ana'
    assert str(a['/CreationDate']).startswith('D:')
    assert str(a['/M']).startswith('D:')
    assert str(a.get('/NM', ''))
    item = _listed(session)[0]
    assert item['author'] == 'Ana'
    assert item['created'] and item['modified']


def test_pdf_dates_round_trip_and_bad_ones_are_none():
    assert V._iso_from_pdf_date("D:20260927105200+01'00'") == '2026-09-27T10:52:00+01:00'
    assert V._iso_from_pdf_date('D:20260927105200Z') == '2026-09-27T10:52:00+00:00'
    assert V._iso_from_pdf_date('D:2026') == '2026-01-01T00:00:00'
    assert V._iso_from_pdf_date('not a date') is None
    now = V._pdf_date_now()
    assert V._pdf_date_from_iso(V._iso_from_pdf_date(now)) == now


# ── replies ─────────────────────────────────────────────────────────────────

def test_a_reply_rides_on_its_parent_and_is_not_an_item(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120])
    parent = _listed(session)[0]
    _thread(client, session, action='reply', annot_idx=parent['annot_idx'],
            text='Move this up', author='Ben')
    listed = _listed(session)
    assert len(listed) == 1, 'a reply must not show up as a comment of its own'
    (reply,) = listed[0]['replies']
    assert (reply['text'], reply['author']) == ('Move this up', 'Ben')
    assert reply['created']


def test_a_reply_draws_nothing_on_the_page(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120])
    _thread(client, session, action='reply', annot_idx=0, text='x', author='Ben')
    reply = _raw_annots(session)[1]
    ap = reply['/AP'].get_object()['/N'].get_object()
    assert ap.get_data() == b'', 'pdfium would draw a second note icon'


def test_replies_take_no_comment_index(client, session):
    """Sticky A, a reply to A, then sticky B: B must still be comment #1, and
    the popup editing #1 must change B — not the reply."""
    _add(client, session, subtype='Text', rect=[10, 700, 28, 718], text='A')
    _thread(client, session, action='reply', annot_idx=0, text='reply to A', author='Ben')
    _add(client, session, subtype='Text', rect=[100, 700, 118, 718], text='B')
    idxs = [(i['text'], i['idx']) for i in _listed(session)]
    assert idxs == [('A', 0), ('B', 1)]
    _post(client, '/api/viewer/update_annotation',
          {'session_id': session, 'page_id': 'pg1', 'annot_idx': 1,
           'text': 'B edited', 'author': ''})
    listed = _listed(session)
    assert [i['text'] for i in listed] == ['A', 'B edited']
    assert listed[0]['replies'][0]['text'] == 'reply to A'


def test_a_reply_cannot_itself_be_replied_to_and_only_replies_are_deleted_as_such(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120])
    _thread(client, session, action='reply', annot_idx=0, text='r', author='Ben')
    _post(client, '/api/viewer/comment_thread',
          {'session_id': session, 'page_id': 'pg1', 'action': 'reply',
           'annot_idx': 1, 'text': 'nested'}, status=404)
    _post(client, '/api/viewer/comment_thread',
          {'session_id': session, 'page_id': 'pg1', 'action': 'delete_reply',
           'annot_idx': 0}, status=404)


def test_an_empty_reply_is_refused(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120])
    body = _post(client, '/api/viewer/comment_thread',
                 {'session_id': session, 'page_id': 'pg1', 'action': 'reply',
                  'annot_idx': 0, 'text': '   '}, status=400)
    assert body['error_key'] == 'err.empty_reply'


def test_deleting_a_reply(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120])
    _thread(client, session, action='reply', annot_idx=0, text='one', author='A')
    _thread(client, session, action='reply', annot_idx=0, text='two', author='B')
    first = _listed(session)[0]['replies'][0]
    _thread(client, session, action='delete_reply', annot_idx=first['annot_idx'])
    assert [r['text'] for r in _listed(session)[0]['replies']] == ['two']


# ── status ──────────────────────────────────────────────────────────────────

def test_one_status_per_item_and_none_clears_it(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120])
    _thread(client, session, action='status', annot_idx=0, state='Accepted', author='Ana')
    item = _listed(session)[0]
    assert (item['status']['state'], item['status']['author']) == ('Accepted', 'Ana')
    _thread(client, session, action='status', annot_idx=0, state='Rejected', author='Ben')
    assert _listed(session)[0]['status']['state'] == 'Rejected'
    states = [a for a in _raw_annots(session) if a.get('/State') is not None]
    assert len(states) == 1, 'the new status replaces the old one'
    _thread(client, session, action='status', annot_idx=0, state='None')
    assert _listed(session)[0]['status'] is None
    assert len(_raw_annots(session)) == 1


def test_an_unknown_status_is_refused(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120])
    _post(client, '/api/viewer/comment_thread',
          {'session_id': session, 'page_id': 'pg1', 'action': 'status',
           'annot_idx': 0, 'state': 'Approved!'}, status=400)


# ── the thread goes where its parent goes ───────────────────────────────────

def _threaded_square(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120], author='Ana')
    _thread(client, session, action='reply', annot_idx=0, text='Why?', author='Ben')
    _thread(client, session, action='status', annot_idx=0, state='Accepted', author='Cy')


def test_deleting_the_parent_deletes_its_thread(client, session):
    _threaded_square(client, session)
    _post(client, '/api/viewer/delete_annotation',
          {'session_id': session, 'page_id': 'pg1', 'annot_idx': 0})
    assert _raw_annots(session) == []


def test_deleting_a_comment_from_its_popup_deletes_its_thread(client, session):
    _add(client, session, subtype='Text', rect=[10, 700, 28, 718], text='A')
    _thread(client, session, action='reply', annot_idx=0, text='r', author='Ben')
    _post(client, '/api/viewer/update_annotation',
          {'session_id': session, 'page_id': 'pg1', 'annot_idx': 0, 'delete': True})
    assert _raw_annots(session) == []


def test_lifting_a_shape_and_putting_it_back_keeps_its_thread(client, session):
    """What the viewer does to move a shape: read its editable info, delete it,
    re-add it (elsewhere) with that info.  The thread and the original creation
    date must survive the round trip."""
    _threaded_square(client, session)
    before = _listed(session)[0]
    pdf = entry.sessions[session]['pages'][0]['pdf_bytes']
    info = [i for i in V._get_editable_annotations(pdf) if i['tool'] == 'rect'][0]
    assert info['created'] == before['created']
    assert {r.get('text') or r.get('state') for r in info['replies']} == {'Why?', 'Accepted'}

    _post(client, '/api/viewer/delete_annotation',
          {'session_id': session, 'page_id': 'pg1', 'annot_idx': info['annot_idx']})
    _add(client, session, subtype='Square', rect=[200, 200, 300, 270], author=info['author'],
         created=info['created'], replies=info['replies'], no_snapshot=True)

    after = _listed(session)[0]
    assert after['created'] == before['created'], 'moving it is not re-creating it'
    assert [(r['text'], r['author'], r['created']) for r in after['replies']] == \
           [(r['text'], r['author'], r['created']) for r in before['replies']]
    assert (after['status']['state'], after['status']['author']) == ('Accepted', 'Cy')


def test_replies_are_not_offered_for_lifting(client, session):
    _threaded_square(client, session)
    pdf = entry.sessions[session]['pages'][0]['pdf_bytes']
    assert [i['tool'] for i in V._get_editable_annotations(pdf)] == ['rect']


# ── undo ────────────────────────────────────────────────────────────────────

def test_a_reply_is_one_undo_step(client, session):
    _add(client, session, subtype='Square', rect=[50, 50, 150, 120])
    _thread(client, session, action='reply', annot_idx=0, text='r', author='B')
    _post(client, '/api/viewer/undo', {'session_id': session})
    assert _listed(session)[0]['replies'] == []
    assert len(_listed(session)) == 1, 'undo reached past the reply'


# ── threads from another reader ─────────────────────────────────────────────

def test_an_acrobat_style_thread_is_read_and_marked_ticks_are_ignored(session):
    """Build what Acrobat writes: a reply with /IRT, and a /Marked tick (a
    private check mark, not a review status) that must not show at all."""
    import pypdf
    from pypdf.generic import (DictionaryObject, NameObject, ArrayObject,
                               FloatObject, TextStringObject)
    w = pypdf.PdfWriter()
    w.add_blank_page(width=612, height=792)

    def annot(**kv):
        d = DictionaryObject({NameObject('/Type'): NameObject('/Annot'),
                              NameObject('/Rect'): ArrayObject([FloatObject(v) for v in (10, 10, 30, 30)])})
        for k, v in kv.items():
            d[NameObject('/' + k)] = v
        return w._add_object(d)

    parent = annot(Subtype=NameObject('/Text'), Contents=TextStringObject('Check totals'),
                   T=TextStringObject('Reviewer'), CreationDate=TextStringObject("D:20260101090000Z"))
    reply = annot(Subtype=NameObject('/Text'), Contents=TextStringObject('Done'),
                  T=TextStringObject('Author'), IRT=parent,
                  CreationDate=TextStringObject("D:20260102090000Z"))
    tick = annot(Subtype=NameObject('/Text'), IRT=parent, StateModel=NameObject('/Marked'),
                 State=NameObject('/Marked'), T=TextStringObject('Reviewer'))
    w.pages[0][NameObject('/Annots')] = ArrayObject([parent, reply, tick])
    buf = io.BytesIO()
    w.write(buf)

    (item,) = V._get_annotations(buf.getvalue())
    assert item['text'] == 'Check totals' and item['idx'] == 0
    assert item['created'] == '2026-01-01T09:00:00+00:00'
    assert [(r['text'], r['author']) for r in item['replies']] == [('Done', 'Author')]
    assert item['status'] is None


# ── your name ───────────────────────────────────────────────────────────────

def test_the_reviewer_name_persists_and_is_cleaned(client, tmp_path, monkeypatch):
    monkeypatch.setattr(entry, '_reviewer_file', lambda: str(tmp_path / 'reviewer.json'))
    h = {'X-App-Token': entry.APP_TOKEN}
    assert client.get('/api/reviewer_name', headers=h).get_json() == {'name': ''}
    r = client.post('/api/reviewer_name', json={'name': '  Ana\u0007  Lima \n'}, headers=h)
    assert r.get_json() == {'name': 'Ana Lima'}
    assert client.get('/api/reviewer_name', headers=h).get_json() == {'name': 'Ana Lima'}
    assert json.loads((tmp_path / 'reviewer.json').read_text(encoding='utf-8')) == {'name': 'Ana Lima'}


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
