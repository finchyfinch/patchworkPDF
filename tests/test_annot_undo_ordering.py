"""Regression tests for viewer annotation undo: every step, in order, once.

Two field reports: undo "sometimes doesn't work after drawing some annotations",
and on one occasion it "removed annotation lines out of order".  Both fall out of
the same gap.

Annotations are baked into the page's PDF bytes server-side, so every mutation is
a read-modify-write of `page_rec['pdf_bytes']` with a snapshot of the pre-change
bytes pushed onto the session's undo stack.  `get_session_lock` exists precisely
to make that atomic — its docstring says to hold it "around any multi-step
read-modify-write of session['pages'] or the annotation undo/redo stacks" — but
the three annotation handlers took it only for the snapshot push and then did the
read-modify-write outside it.

Flask serves concurrently and the viewer fires these from several places that do
not queue behind each other (the lift delete, the sticky add, the popup edit),
so two mutations of one page overlap: both read the same bytes, the second write
lands on top of the first, and one annotation is silently gone.  The undo stack
is left holding two snapshots of the SAME state — and `_annot_apply` discards a
snapshot equal to the current bytes, so one Ctrl+Z then jumps two user actions or
appears to do nothing at all.

These tests pin the guarantees rather than the mechanism:

  * a mutation never loses a concurrent one;
  * the undo stack has exactly one entry per mutation;
  * undo replays them newest-first, one user action at a time.
"""
import io
import threading

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry
from routes import viewer as V


# ── fixtures ─────────────────────────────────────────────────────────────────

def _blank_page():
    import pypdf
    w = pypdf.PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture()
def session():
    sid = 'annot-undo-session'
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
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _add_body(session_id, n):
    """One square annotation, tagged by position so it is identifiable later."""
    return {
        'session_id': session_id, 'page_id': 'pg1', 'subtype': 'Square',
        'rect': [50 + n * 10, 50 + n * 10, 100 + n * 10, 100 + n * 10],
        'border_color': [1.0, 0.0, 0.0], 'border_width': 2.0,
    }


# The stored /Rect is inflated by the border width (a 2pt stroke straddles the
# edge), so the tag that comes back is the requested left edge minus 2.
def TAG(n):
    return 48 + n * 10


def _rect_xs(pdf_bytes):
    """Left edge of every annotation on the page — the identity tag above."""
    import pypdf
    r = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    annots = r.pages[0].get('/Annots')
    if not annots:
        return []
    if hasattr(annots, 'get_object'):
        annots = annots.get_object()
    out = []
    for ref in annots:
        a = ref.get_object() if hasattr(ref, 'get_object') else ref
        rect = a.get('/Rect')
        if hasattr(rect, 'get_object'):
            rect = rect.get_object()
        out.append(round(float(rect[0])))
    return out


def _page_xs(session_id):
    return _rect_xs(entry.sessions[session_id]['pages'][0]['pdf_bytes'])


def _counts(session_id):
    s = entry.sessions[session_id]
    return len(s.get('annot_undo', [])), len(s.get('annot_redo', []))


# ── the ordinary case, which has to keep working ─────────────────────────────

def test_undo_removes_one_annotation_per_press_newest_first(client, session):
    for n in range(5):
        r = _post(client, '/api/viewer/add_annotation', _add_body(session, n))
        assert r.status_code == 200, r.get_data(as_text=True)
    assert _page_xs(session) == [TAG(n) for n in range(5)]
    assert _counts(session)[0] == 5, 'one undo step per annotation drawn'

    for keep in (4, 3, 2, 1, 0):
        r = _post(client, '/api/viewer/undo', {'session_id': session,
                                               'page_id': 'pg1'})
        assert r.status_code == 200
        assert _page_xs(session) == [TAG(n) for n in range(keep)], (
            'each Ctrl+Z must reverse exactly one drawing, newest first')
    assert _counts(session) == (0, 5)


def test_redo_replays_them_in_the_order_they_were_drawn(client, session):
    for n in range(3):
        _post(client, '/api/viewer/add_annotation', _add_body(session, n))
    for _ in range(3):
        _post(client, '/api/viewer/undo', {'session_id': session, 'page_id': 'pg1'})
    assert _page_xs(session) == []
    for keep in (1, 2, 3):
        _post(client, '/api/viewer/redo', {'session_id': session, 'page_id': 'pg1'})
        assert _page_xs(session) == [TAG(n) for n in range(keep)]


def test_a_delete_is_its_own_undo_step(client, session):
    for n in range(3):
        _post(client, '/api/viewer/add_annotation', _add_body(session, n))
    _post(client, '/api/viewer/delete_annotation',
          {'session_id': session, 'page_id': 'pg1', 'annot_idx': 1})
    assert _page_xs(session) == [TAG(0), TAG(2)]
    assert _counts(session)[0] == 4

    _post(client, '/api/viewer/undo', {'session_id': session, 'page_id': 'pg1'})
    assert _page_xs(session) == [TAG(n) for n in range(3)], (
        'undo brings the deleted one back')
    _post(client, '/api/viewer/undo', {'session_id': session, 'page_id': 'pg1'})
    assert _page_xs(session) == [TAG(0), TAG(1)], 'and the next step is the last draw'


# ── the reported faults ──────────────────────────────────────────────────────

def _concurrent(fn, n):
    """Run fn(i) for i in range(n) on n threads released together."""
    start = threading.Barrier(n)
    errors = []

    def run(i):
        try:
            start.wait(timeout=10)
            fn(i)
        except Exception as e:                # noqa: BLE001 — reported below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f'worker raised: {errors[0]!r}'


def test_concurrent_draws_do_not_lose_each_other(session):
    """Two annotations committed at once must both survive.

    The viewer serialises the DRAW path through its commit chain, but the lift
    delete, the sticky add and the popup edit all fire outside it — so a fast
    user really does put two mutations of one page in flight together.  Both read
    the same bytes; without the lock held across the write, the second one's
    output is built from a page that never saw the first.
    """
    N = 6

    def add(i):
        with entry.app.test_client() as c:
            r = _post(c, '/api/viewer/add_annotation', _add_body(session, i))
            assert r.status_code == 200, r.get_data(as_text=True)

    _concurrent(add, N)
    assert len(_page_xs(session)) == N, (
        f'{N} annotations were committed; the page kept {len(_page_xs(session))} '
        '— a lost write is an annotation that silently never appeared')


def test_every_mutation_leaves_exactly_one_undo_step(session):
    """A duplicated snapshot is what makes one Ctrl+Z skip two user actions:
    _annot_apply discards a snapshot equal to the current bytes and keeps
    popping."""
    N = 6

    def add(i):
        with entry.app.test_client() as c:
            _post(c, '/api/viewer/add_annotation', _add_body(session, i))

    _concurrent(add, N)
    undo, _redo = _counts(session)
    assert undo == N, f'expected {N} undo steps for {N} mutations, got {undo}'

    # Entries are (page_id, bytes[, meta]) — index rather than unpack, so the
    # metadata a page-text edit snapshots alongside the bytes does not break
    # this read.
    snaps = [bytes(e[1]) for e in entry.sessions[session]['annot_undo']]
    assert len(set(snaps)) == N, (
        'two undo entries hold the same page state, so one of them undoes '
        'nothing and the next Ctrl+Z reaches past the action the user meant')


def test_undo_after_concurrent_draws_unwinds_one_at_a_time(session, client):
    """The out-of-order report: with the stack corrupted above, undo pops a
    snapshot that predates several drawings and removes them together."""
    N = 6

    def add(i):
        with entry.app.test_client() as c:
            _post(c, '/api/viewer/add_annotation', _add_body(session, i))

    _concurrent(add, N)
    remaining = len(_page_xs(session))
    for step in range(remaining):
        _post(client, '/api/viewer/undo', {'session_id': session, 'page_id': 'pg1'})
        left = len(_page_xs(session))
        assert left == remaining - step - 1, (
            f'undo #{step + 1} should leave {remaining - step - 1} annotations, '
            f'left {left} — each press must reverse exactly one action')


def test_a_delete_racing_a_draw_keeps_the_history_consistent(session):
    """Mixed traffic, which is what the viewer actually generates: the lift path
    deletes while the commit chain adds."""
    for n in range(4):
        with entry.app.test_client() as c:
            _post(c, '/api/viewer/add_annotation', _add_body(session, n))
    before = len(_page_xs(session))
    assert before == 4

    def mutate(i):
        with entry.app.test_client() as c:
            if i % 2:
                _post(c, '/api/viewer/add_annotation', _add_body(session, 10 + i))
            else:
                _post(c, '/api/viewer/delete_annotation',
                      {'session_id': session, 'page_id': 'pg1', 'annot_idx': 0})

    _concurrent(mutate, 4)
    # 2 adds and 2 deletes on a page of 4 → 4 again, whatever the interleaving.
    assert len(_page_xs(session)) == 4, 'no mutation may be lost'
    undo, _r = _counts(session)
    assert undo == 8, f'4 + 4 mutations = 8 undo steps, got {undo}'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
