"""Regression tests: every route that changes a document says so.

Companion to `test_recovery_snapshot.py`, which pins the store itself.  These
drive the real Flask routes and assert the session came out marked — because the
gap that produced the bug report was never in the store, it was in the routes
that quietly skipped it.

Two things hang off that mark:

  * the crash-recovery snapshot (what the user gets back after a power cut), and
  * `touch_session`'s LRU, which refuses to evict a session with unsaved work.

A route that mutates and does not mark gets both wrong at once: the change is
absent from the snapshot AND the whole session becomes a legal eviction victim
while its tab is still open on screen.

Run: pytest tests/test_unsaved_marking.py   (or: python tests/test_unsaved_marking.py)
"""
import os
import shutil
import tempfile

import _bootstrap  # noqa: F401  (path setup)

import entry
from pdf import pdf_core
from bootstrap import recovery
from bootstrap import single_instance


# ── harness ──────────────────────────────────────────────────────────────────
class _App:
    """Test client + a throwaway recovery root, with one session loaded."""

    def __init__(self, n_pages=3):
        self.dir = tempfile.mkdtemp(prefix='pwmark-')
        self._orig_state = single_instance._state_dir
        single_instance._state_dir = lambda: self.dir
        recovery._dirty.clear()

        entry.app.config['TESTING'] = True
        self.client = entry.app.test_client()
        self.sid = 'test-session'
        # Real single-page PDFs: /api/rotate parses the bytes it is handed.
        page_bytes = pdf_core._create_blank_page_bytes(612, 792)
        pdf_core.sessions[self.sid] = {
            'pages': [{
                'id': f'p{i}', 'filename': 'doc.pdf', 'page_num': i + 1,
                'total_pages': n_pages, 'rotation': 0,
                'width_pts': 612, 'height_pts': 792, 'thumbnail': '',
                'pdf_bytes': page_bytes, 'form_fields': [],
            } for i in range(n_pages)],
            'first_file_path': os.path.join(self.dir, 'doc.pdf'),
        }

    @property
    def session(self):
        return pdf_core.sessions[self.sid]

    def post(self, url, body):
        return self.client.post(url, json=dict(body, session_id=self.sid),
                                headers={'X-App-Token': entry.APP_TOKEN})

    def close(self):
        pdf_core.sessions.pop(self.sid, None)
        recovery._dirty.clear()
        single_instance._state_dir = self._orig_state
        shutil.rmtree(self.dir, ignore_errors=True)


def _marked(app):
    """True when the session is queued for a snapshot — the same question
    touch_session asks before deciding a session is safe to evict."""
    return recovery.has_pending(app.session)


def _check(url, body, why, n_pages=3, prepare=None):
    app = _App(n_pages)
    try:
        if prepare:
            prepare(app)
        recovery._dirty.clear()
        app.session['_rec_dirty'] = set()
        assert not _marked(app), f'{url}: the fixture starts clean'
        r = app.post(url, body)
        assert r.status_code == 200, f'{url}: {r.status_code} {r.get_data(as_text=True)[:200]}'
        assert _marked(app), f'{url} {why}'
    finally:
        app.close()


# ── page structure: the change is in the order, not the bytes ────────────────

def test_reordering_pages_marks_the_session():
    _check('/api/reorder', {'order': ['p2', 'p1', 'p0']},
           'changes the document — the reader gets a different document back')


def test_deleting_a_page_marks_the_session():
    _check('/api/delete', {'page_id': 'p1'},
           'removes a page; a snapshot that still lists it is wrong')


def test_inserting_a_blank_page_marks_the_session():
    _check('/api/insert_blank_page',
           {'ref_page_id': 'p1', 'insert_before': False,
            'width_pts': 595.28, 'height_pts': 841.89},
           'adds a page whose bytes exist nowhere else')


def test_rotating_a_page_marks_the_session():
    _check('/api/rotate', {'page_id': 'p0', 'direction': 90},
           'rewrites the page')


# ── session-held state, written into the PDF only at save time ───────────────

def test_filling_a_form_field_marks_the_session():
    def prepare(app):
        app.session['pages'][0]['form_fields'] = [
            {'id': 'f1', 'type': 'text', 'value': ''}]
    _check('/api/viewer/fill_form', {'values': {'f1': 'typed'}},
           'is typed work that only reaches the PDF at save time', prepare=prepare)


def test_adding_a_bookmark_marks_the_session():
    _check('/api/viewer/add_bookmark', {'page_id': 'p0', 'title': 'Chapter 1'},
           'edits the outline, which is written at save time')


def test_deleting_a_bookmark_marks_the_session():
    app = _App()
    try:
        bid = app.post('/api/viewer/add_bookmark',
                       {'page_id': 'p0', 'title': 'X'}).get_json()['added']['id']
        recovery._dirty.clear(); app.session['_rec_dirty'] = set()
        assert not _marked(app)
        app.post('/api/viewer/delete_bookmark', {'bookmark_id': bid})
        assert _marked(app), 'removing an outline entry is a change too'
    finally:
        app.close()


def test_renaming_and_moving_a_bookmark_mark_the_session():
    app = _App()
    try:
        ids = [app.post('/api/viewer/add_bookmark',
                        {'page_id': 'p0', 'title': f'B{i}'}).get_json()['added']['id']
               for i in range(2)]
        for url, body, why in [
            ('/api/viewer/rename_bookmark', {'bookmark_id': ids[0], 'title': 'New'}, 'a rename'),
            ('/api/viewer/move_bookmark',   {'bookmark_id': ids[0], 'direction': 'down'}, 'a move'),
            ('/api/viewer/delete_bookmarks', {'all': True}, 'a bulk delete'),
        ]:
            recovery._dirty.clear(); app.session['_rec_dirty'] = set()
            assert not _marked(app)
            assert app.post(url, body).status_code == 200
            assert _marked(app), f'{why} of the outline is an unsaved change'
    finally:
        app.close()


def test_editing_document_properties_marks_the_session():
    _check('/api/metadata',
           {'action': 'set', 'metadata': {'title': 'Report', 'author': 'GF'}},
           'is applied at save time, so the session now differs from the file')


# ── reading is not changing ──────────────────────────────────────────────────

def test_reading_the_document_never_marks_it():
    app = _App()
    try:
        for url, body in [
            ('/api/viewer/pages',    {}),
            ('/api/viewer/bookmarks', {}),
            ('/api/metadata',         {'action': 'get'}),
            ('/api/viewer/form_fields', {}),
        ]:
            recovery._dirty.clear(); app.session['_rec_dirty'] = set()
            app.post(url, body)
            assert not _marked(app), (
                f'{url} only reads — marking it would make every freshly opened '
                'document claim unsaved changes')
    finally:
        app.close()


def test_a_fill_that_matches_no_field_does_not_mark():
    app = _App()
    try:
        recovery._dirty.clear(); app.session['_rec_dirty'] = set()
        r = app.post('/api/viewer/fill_form', {'values': {'nonexistent': 'x'}})
        assert r.get_json()['updated'] == 0
        assert not _marked(app), 'nothing was updated, so nothing is unsaved'
    finally:
        app.close()


# ── protecting a document must never leave plaintext behind ──────────────────

def test_setting_a_password_purges_the_snapshot_and_removing_it_re_arms():
    app = _App()
    try:
        app.post('/api/rotate', {'page_id': 'p0', 'direction': 90})
        recovery.flush_all()
        assert _marked(app)

        assert app.post('/api/set_password', {'password': 'secret'}).status_code == 200
        assert not _marked(app), (
            'a protected document must leave no decrypted bytes on disk — the '
            'snapshot is purged and no new one is taken')

        assert app.post('/api/set_password', {'password': ''}).status_code == 200
        assert _marked(app), (
            'unprotecting re-arms the snapshot: nothing was captured while it '
            'was protected, so the whole document has to be taken again')
    finally:
        app.close()


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
