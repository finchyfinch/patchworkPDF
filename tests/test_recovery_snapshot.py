"""Regression tests for the disk-backed working-bytes snapshot store.

This is the server half of "this tab holds unsaved work".  The page's doc-dirty
flag drives the close prompt; `bootstrap/recovery.py` is what actually makes
that work survivable — it snapshots the session so a crash can restore it, and
`has_pending()` is what stops the session LRU evicting a tab the user is still
editing.  The two have to agree, or the app warns about work it has already
thrown away (or throws away work it just warned about).

The gaps these come from:

  * Only routes that rewrote page BYTES marked the session.  Reordering pages,
    deleting one, filling a form, adding a bookmark, editing the document
    properties — all real unsaved changes — marked nothing, so the snapshot on
    disk still described the document as it was several edits ago, and
    has_pending() answered False for a session the user was actively changing.
    `mark_meta_dirty` is the fix: enqueue for a manifest rewrite without
    rewriting a single unchanged page.

  * The manifest carried page bytes and comments and nothing else, so a restore
    silently dropped the outline, the filled form values and the document
    properties even when it had faithfully recovered every page.

Run: pytest tests/test_recovery_snapshot.py   (or: python tests/test_recovery_snapshot.py)
"""
import os
import io
import json
import shutil
import tempfile

import _bootstrap  # noqa: F401  (path setup)

from bootstrap import recovery
from bootstrap import single_instance


# ── harness ──────────────────────────────────────────────────────────────────
class _Store:
    """A recovery store rooted in a temp dir, flushed synchronously.

    The real writer is a debounced daemon thread; these tests call the flush
    directly so they assert on what lands on disk without sleeping for it.
    """

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='pwrec-')
        self._orig = single_instance._state_dir
        single_instance._state_dir = lambda: self.dir

    def close(self):
        single_instance._state_dir = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def flush(self):
        """Run exactly what the writer daemon does for the queued sessions."""
        recovery.flush_all()

    def manifest(self, session):
        rec_id = session.get('_rec_id')
        path = os.path.join(self.dir, 'recovery', rec_id or '', recovery._MANIFEST)
        if not os.path.isfile(path):
            return None
        with io.open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def page_files(self, session):
        d = os.path.join(self.dir, 'recovery', session.get('_rec_id') or '')
        if not os.path.isdir(d):
            return set()
        return {n for n in os.listdir(d) if n != recovery._MANIFEST and not n.endswith('.tmp')}


def _session(n_pages=3, **extra):
    sess = {
        'pages': [{
            'id': f'p{i}', 'filename': 'doc.pdf', 'page_num': i + 1,
            'total_pages': n_pages, 'rotation': 0,
            'width_pts': 612, 'height_pts': 792, 'thumbnail': '',
            'pdf_bytes': f'PAGE{i}'.encode('ascii'),
            'form_fields': [],
        } for i in range(n_pages)],
        'first_file_path': r'C:\docs\doc.pdf',
    }
    sess.update(extra)
    return sess


def _store():
    s = _Store()
    recovery._dirty.clear()
    return s


# ── a change that touches no page bytes is still unsaved work ────────────────

def test_a_meta_only_change_is_pending_and_writes_a_manifest():
    st = _store()
    try:
        sess = _session()
        assert recovery.has_pending(sess) is False, 'a freshly opened document is clean'

        recovery.mark_meta_dirty(sess)          # e.g. the pages were reordered
        assert recovery.has_pending(sess) is True, (
            'the session LRU must not evict a tab holding unsaved changes — '
            'has_pending is the only thing standing between it and the user')

        st.flush()
        man = st.manifest(sess)
        assert man is not None, 'a change with no byte rewrite still needs a snapshot'
        assert [p['id'] for p in man['pages']] == ['p0', 'p1', 'p2']
    finally:
        st.close()


def test_a_meta_mark_rewrites_the_manifest_without_rewriting_page_bytes():
    st = _store()
    try:
        sess = _session()
        recovery.mark_dirty(sess)               # first full snapshot
        st.flush()
        d = os.path.join(st.dir, 'recovery', sess['_rec_id'])
        before = {n: os.path.getmtime(os.path.join(d, n)) for n in st.page_files(sess)}

        # Reorder, then mark meta-dirty the way /api/reorder does.
        sess['pages'].reverse()
        recovery.mark_meta_dirty(sess)
        st.flush()

        man = st.manifest(sess)
        assert [p['id'] for p in man['pages']] == ['p2', 'p1', 'p0'], \
            'the new order is what a restore has to come back to'
        after = {n: os.path.getmtime(os.path.join(d, n)) for n in st.page_files(sess)}
        assert after == before, (
            'no page changed, so a meta mark must not rewrite their bytes — on a '
            '300-page scan that is the difference between instant and seconds')
    finally:
        st.close()


def test_deleting_a_page_prunes_its_file_from_the_snapshot():
    st = _store()
    try:
        sess = _session()
        recovery.mark_dirty(sess)
        st.flush()
        assert len(st.page_files(sess)) == 3

        sess['pages'] = [p for p in sess['pages'] if p['id'] != 'p1']
        recovery.mark_meta_dirty(sess)          # what /api/delete does
        st.flush()

        assert len(st.page_files(sess)) == 2, 'the deleted page leaves no orphan behind'
        assert [p['id'] for p in st.manifest(sess)['pages']] == ['p0', 'p2']
    finally:
        st.close()


# ── everything unsaved is in the snapshot, not just the page bytes ───────────

def test_bookmarks_form_values_and_properties_survive_a_round_trip():
    st = _store()
    try:
        sess = _session()
        sess['bookmarks'] = [{'id': 'b1', 'title': 'Chapter 1', 'page_id': 'p0',
                              'y': 700.0, 'level': 0}]
        sess['metadata']  = {'title': 'Report', 'author': 'GF',
                             'subject': '', 'keywords': ''}
        sess['comments']  = {'c1': {'id': 'c1', 'page_id': 'p0', 'text': 'check this'}}
        sess['pages'][0]['form_fields'] = [{'id': 'f1', 'type': 'text', 'value': 'typed'}]

        recovery.mark_meta_dirty(sess)
        st.flush()

        snap = recovery.load(sess['_rec_id'])
        assert snap is not None
        assert snap['bookmarks'] == sess['bookmarks'], \
            'an outline is unsaved work — it reaches the file only at save time'
        assert snap['metadata']['title'] == 'Report'
        assert snap['comments']['c1']['text'] == 'check this'
        assert snap['pages'][0]['form_fields'][0]['value'] == 'typed', \
            'a filled form recovered as blank is the same loss as a lost edit'
    finally:
        st.close()


def test_an_older_snapshot_without_the_new_fields_still_loads():
    st = _store()
    try:
        sess = _session()
        recovery.mark_dirty(sess)
        st.flush()
        # Rewrite the manifest as a pre-upgrade one: no bookmarks/metadata keys,
        # and no per-page form_fields.
        path = os.path.join(st.dir, 'recovery', sess['_rec_id'], recovery._MANIFEST)
        with io.open(path, 'r', encoding='utf-8') as f:
            man = json.load(f)
        man.pop('bookmarks', None)
        man.pop('metadata', None)
        for p in man['pages']:
            p.pop('form_fields', None)
        with io.open(path, 'w', encoding='utf-8') as f:
            json.dump(man, f)

        snap = recovery.load(sess['_rec_id'])
        assert snap is not None, 'a snapshot written by the previous version must still restore'
        assert len(snap['pages']) == 3
        assert snap['bookmarks'] is None and snap['metadata'] is None
        assert snap['pages'][0]['form_fields'] == []
    finally:
        st.close()


# ── saving ends it; encryption never starts it ───────────────────────────────

def test_saving_drops_the_snapshot_and_the_pending_flag():
    st = _store()
    try:
        sess = _session()
        recovery.mark_meta_dirty(sess)
        st.flush()
        assert st.manifest(sess) is not None

        recovery.clear(sess)                    # what a successful Save does
        assert recovery.has_pending(sess) is False, \
            'a saved document is freely evictable and must not warn on close'
        assert st.manifest(sess) is None, 'and nothing is left to re-offer on next launch'
    finally:
        st.close()


def test_a_meta_mark_on_an_encrypted_session_writes_nothing_and_purges():
    st = _store()
    try:
        sess = _session()
        recovery.mark_dirty(sess)
        st.flush()
        assert st.manifest(sess) is not None

        # The user sets a save password: decrypted plaintext must never sit on disk.
        sess['password'] = 'secret'
        recovery.mark_meta_dirty(sess)
        assert st.manifest(sess) is None, 'the existing snapshot is purged on the spot'
        st.flush()
        assert st.manifest(sess) is None, 'and the writer does not put one back'
    finally:
        st.close()


def test_a_meta_mark_on_a_pageless_session_is_a_no_op():
    st = _store()
    try:
        sess = {'pages': []}
        recovery.mark_meta_dirty(sess)
        assert '_rec_id' not in sess, 'nothing to snapshot, so no directory is claimed'
        assert recovery.has_pending(sess) is False
    finally:
        st.close()


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
