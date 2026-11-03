"""Editing the page's OWN text from the viewer — the Pointer.

This is the last and hardest tool to come across from the editor, and it works
differently from the editor's version on purpose: the editor STAGES edits in a
client-side override model and bakes them at Apply, while the viewer makes each
edit immediately, as one byte rewrite and one Ctrl+Z, like every other page
tool.  That avoids running two undo systems side by side, which was the single
biggest hazard identified when this merge was planned.

**The thing that makes this tool subtle: editing text does not remove it.**
`apply_edits` paints a background-coloured rectangle over the original glyphs
and injects the replacement on top.  The old words are still in the content
stream.  `covered_text_bboxes` on the page record is what stops them being
offered for editing again — and because UNDO restores the uncovered original,
that list has to travel with the bytes in the undo snapshot.  Without that,
undoing an edit leaves the restored text permanently invisible to the Pointer:
the user can see it but can never edit it again.  That is what
test_undo_makes_the_original_editable_again pins.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pypdf
import pypdfium2 as pdfium
import pytest

import entry
from routes import editor as _editor


@pytest.fixture(scope='module', autouse=True)
def _heavy():
    # Extraction reaches pdfium through routes.editor's lazily-loaded handle,
    # which the app fills in on a background thread at startup.
    _editor._heavy_init()


def _text_page():
    """One page carrying real, extractable text runs."""
    pdf = pypdf.PdfWriter()
    src = pypdf.PdfReader('tests/fixtures/text_page.pdf')
    pdf.add_page(src.pages[1])          # the contents page — many short runs
    buf = io.BytesIO()
    pdf.write(buf)
    return buf.getvalue()


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _session(sid='edittext'):
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 'm.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '',
                   'pdf_bytes': _text_page()}],
        'comments': {},
    }
    return sid


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _items(client, sid):
    r = _post(client, '/api/viewer/page_elements',
              {'session_id': sid, 'page_id': 'pg1'})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()['items']


def _words(client, sid):
    return [i['text'].strip() for i in _items(client, sid)]


def _find(client, sid, word):
    return next(i for i in _items(client, sid) if i['text'].strip() == word)


def _edit(client, sid, el_id, op, **kw):
    body = {'session_id': sid, 'page_id': 'pg1', 'id': el_id, 'op': op}
    body.update(kw)
    return _post(client, '/api/viewer/edit_text', body)


def _rendered_text(sid):
    doc = pdfium.PdfDocument(entry.sessions[sid]['pages'][0]['pdf_bytes'])
    try:
        return doc[0].get_textpage().get_text_range()
    finally:
        doc.close()


# ── loading the element model ───────────────────────────────────────────────

def test_page_elements_returns_editable_runs_without_a_page_image(client):
    """The viewer already has the page on screen; shipping the editor's hi-res
    render again would add ~250 KB per page to a request made on demand while
    scrolling."""
    sid = _session()
    try:
        r = _post(client, '/api/viewer/page_elements',
                  {'session_id': sid, 'page_id': 'pg1'})
        body = r.get_json()
        assert 'image' not in body, 'the element list must not carry a render'
        assert len(body['items']) > 20, 'no text runs found on a text page'
        one = body['items'][0]
        for k in ('id', 'text', 'x', 'y', 'x2', 'y2', 'color', 'rl_font'):
            assert k in one, f'{k} missing — the overlay needs it'
    finally:
        entry.sessions.pop(sid, None)


def test_unknown_page_is_reported(client):
    sid = _session()
    try:
        r = _post(client, '/api/viewer/page_elements',
                  {'session_id': sid, 'page_id': 'nope'})
        assert r.status_code == 404
    finally:
        entry.sessions.pop(sid, None)


# ── changing the words ──────────────────────────────────────────────────────

def test_replacing_a_run_puts_the_new_words_in_the_page(client):
    sid = _session()
    try:
        tgt = _find(client, sid, 'Introduction')
        assert _edit(client, sid, tgt['id'], 'text',
                     text='REPLACED').status_code == 200
        assert 'REPLACED' in _rendered_text(sid)
    finally:
        entry.sessions.pop(sid, None)


def test_the_replaced_run_is_not_offered_for_editing_again(client):
    """The original glyphs are covered, not deleted, so without the ghost
    filter the user would be offered the text they just replaced — sitting
    invisibly underneath its replacement."""
    sid = _session()
    try:
        tgt = _find(client, sid, 'Introduction')
        _edit(client, sid, tgt['id'], 'text', text='REPLACED')
        words = _words(client, sid)
        assert 'REPLACED' in words
        assert 'Introduction' not in words, 'the covered ghost is still offered'
    finally:
        entry.sessions.pop(sid, None)


def test_neighbouring_text_is_untouched(client):
    sid = _session()
    try:
        before = _words(client, sid)
        tgt = _find(client, sid, 'Introduction')
        _edit(client, sid, tgt['id'], 'text', text='REPLACED')
        after = _words(client, sid)
        lost = (set(before) - set(after)) - {'Introduction'}
        assert not lost, f'editing one run disturbed others: {sorted(lost)}'
    finally:
        entry.sessions.pop(sid, None)


def test_emptying_a_run_deletes_it_rather_than_covering_a_blank(client):
    sid = _session()
    try:
        tgt = _find(client, sid, 'Introduction')
        assert _edit(client, sid, tgt['id'], 'text', text='   ').status_code == 200
        assert 'Introduction' not in _words(client, sid)
    finally:
        entry.sessions.pop(sid, None)


# ── delete, move, restyle ───────────────────────────────────────────────────

def test_deleting_a_run_removes_it_from_the_editable_set(client):
    sid = _session()
    try:
        tgt = _find(client, sid, 'Introduction')
        assert _edit(client, sid, tgt['id'], 'delete').status_code == 200
        assert 'Introduction' not in _words(client, sid)
    finally:
        entry.sessions.pop(sid, None)


def test_moving_a_run_changes_where_it_is(client):
    sid = _session()
    try:
        tgt = _find(client, sid, 'Introduction')
        x0, y0 = tgt['x'], tgt['y']
        assert _edit(client, sid, tgt['id'], 'move',
                     x=x0 + 60, y=y0 - 40).status_code == 200
        moved = [i for i in _items(client, sid)
                 if i['text'].strip() == 'Introduction']
        assert moved, 'the moved run vanished'
        assert abs(moved[0]['x'] - (x0 + 60)) < 3, f"x={moved[0]['x']} want {x0+60}"
        assert abs(moved[0]['y'] - (y0 - 40)) < 3, f"y={moved[0]['y']} want {y0-40}"
    finally:
        entry.sessions.pop(sid, None)


def test_restyling_keeps_the_words(client):
    sid = _session()
    try:
        tgt = _find(client, sid, 'Introduction')
        r = _edit(client, sid, tgt['id'], 'style',
                  size=20, color='#ff0000', rl_font='Times-Roman')
        assert r.status_code == 200, r.get_data(as_text=True)
        assert 'Introduction' in _rendered_text(sid)
    finally:
        entry.sessions.pop(sid, None)


# ── undo: the part that needed the snapshot to carry metadata ───────────────

def test_undo_makes_the_original_editable_again(client):
    """The defect this guards: undo restores the UNCOVERED original bytes, but
    `covered_text_bboxes` lives on the page record.  If it does not come back
    with the bytes, the restored text stays filtered out — visible on the page
    and impossible to edit ever again."""
    sid = _session()
    try:
        tgt = _find(client, sid, 'Introduction')
        _edit(client, sid, tgt['id'], 'text', text='REPLACED')
        assert 'Introduction' not in _words(client, sid)

        _post(client, '/api/viewer/undo', {'session_id': sid})
        words = _words(client, sid)
        assert 'Introduction' in words, \
            'undone text is still hidden from the Pointer'
        assert 'REPLACED' not in words

        _post(client, '/api/viewer/redo', {'session_id': sid})
        words = _words(client, sid)
        assert 'REPLACED' in words and 'Introduction' not in words, \
            'redo did not restore the edited state'
    finally:
        entry.sessions.pop(sid, None)


def test_each_edit_is_one_undo_step(client):
    sid = _session()
    try:
        a = _find(client, sid, 'Introduction')
        assert _edit(client, sid, a['id'], 'text',
                     text='ONE').get_json()['undo_count'] == 1
        b = _find(client, sid, 'ONE')
        assert _edit(client, sid, b['id'], 'text',
                     text='TWO').get_json()['undo_count'] == 2
        _post(client, '/api/viewer/undo', {'session_id': sid})
        assert 'ONE' in _words(client, sid), 'one undo went back two edits'
    finally:
        entry.sessions.pop(sid, None)


def test_an_edit_marks_the_document_dirty(client):
    sid = _session()
    try:
        tgt = _find(client, sid, 'Introduction')
        _edit(client, sid, tgt['id'], 'text', text='X')
        from bootstrap import recovery as _recovery
        assert _recovery.has_pending(entry.sessions[sid])
    finally:
        entry.sessions.pop(sid, None)


# ── guards ──────────────────────────────────────────────────────────────────

def test_editing_a_run_that_is_gone_is_reported_not_crashed(client):
    """Two tabs, or an undo in between: the id the client quotes may no longer
    exist.  It has to say so rather than 500."""
    sid = _session()
    try:
        r = _edit(client, sid, 'txt_99999', 'text', text='x')
        assert r.status_code == 404
        assert (r.get_json() or {}).get('error_key') == 'err.text_run_gone'
    finally:
        entry.sessions.pop(sid, None)


def test_an_unknown_operation_is_rejected(client):
    sid = _session()
    try:
        tgt = _find(client, sid, 'Introduction')
        before = entry.sessions[sid]['pages'][0]['pdf_bytes']
        r = _edit(client, sid, tgt['id'], 'explode')
        assert r.status_code == 400
        assert entry.sessions[sid]['pages'][0]['pdf_bytes'] == before
    finally:
        entry.sessions.pop(sid, None)


# ── fitting the replacement into the space ──────────────────────────────────
#
# Nothing in a PDF reflows, so a replacement longer than the run it replaces
# draws straight over the next word unless it is made to fit.  The unit-level
# rules live in tests/test_text_fit.py; these pin the route end to end.

def _stream(sid):
    page = pypdf.PdfReader(
        io.BytesIO(entry.sessions[sid]['pages'][0]['pdf_bytes'])).pages[0]
    return page.get_contents().get_data()


def _neighbour_of(items, run):
    """The nearest run to the right of *run* on its own line, or None."""
    best = None
    for o in items:
        if o['id'] == run['id']:
            continue
        ov = min(run['y2'], o['y2']) - max(run['y'], o['y'])
        if ov < 0.35 * min(run['y2'] - run['y'], o['y2'] - o['y']):
            continue
        if o['x'] <= run['x'] + 0.5:
            continue
        if best is None or o['x'] < best['x']:
            best = o
    return best


def _crowded_run(client, sid):
    """A run with a neighbour genuinely BEYOND its own right edge — i.e. one
    with a real constraint to respect.  Extraction also reports neighbours that
    start inside the previous run's box; those impose no constraint at all (see
    the clamp in _fit_to_available) and make a useless fixture."""
    items = _items(client, sid)
    best = None
    for it in items:
        if len(it['text'].strip()) < 3:
            continue
        nb = _neighbour_of(items, it)
        if nb is None or nb['x'] <= it['x2']:
            continue
        gap = nb['x'] - it['x']
        if best is None or gap < best[1]:
            best = (it, gap)
    assert best, 'no run on this page has a clear neighbour — bad fixture'
    return best


def test_a_longer_replacement_is_condensed_to_clear_its_neighbour(client):
    sid = _session()
    try:
        run, gap = _crowded_run(client, sid)
        nb_x = _neighbour_of(_items(client, sid), run)['x']
        # Twice the original's width: too long to leave alone, well inside
        # what condensing plus a little shrinking can absorb.
        r = _edit(client, sid, run['id'], 'text', text=run['text'].strip() * 2)
        assert r.status_code == 200, r.get_data(as_text=True)
        assert 'too_long_for_space' not in r.get_json(),             'this fixture is meant to be fittable'
        assert b' Tz' in _stream(sid), 'nothing was condensed'

        # And it really clears: no injected run may start before the neighbour
        # and extend past it.
        after = _items(client, sid)
        mine = [i for i in after
                if i['x'] >= run['x'] - 1 and i['x'] < nb_x and i['y'] >= run['y'] - 2
                and i['y'] <= run['y'] + 2]
        if mine:
            assert max(i['x2'] for i in mine) <= nb_x + 0.5,                 'the replacement still runs over the next word'
    finally:
        entry.sessions.pop(sid, None)


def test_a_condensed_run_puts_the_scaling_back(client):
    """Tz is text STATE.  Every block we append lives inside the same outer q,
    so a run left condensed would squash everything emitted after it."""
    sid = _session()
    try:
        run, _gap = _crowded_run(client, sid)
        _edit(client, sid, run['id'], 'text', text=run['text'].strip() * 2)
        body = _stream(sid)
        if b' Tz' in body:
            assert b'100 Tz' in body, 'horizontal scaling was never reset'
    finally:
        entry.sessions.pop(sid, None)


def test_a_replacement_that_fits_is_not_touched_at_all(client):
    """The common edit must be byte-for-byte what it was before fitting
    existed — no Tz, no size change, nothing reported.

    The replacement is the original's FIRST CHARACTER, which is narrower than
    the original itself in any font, so it fits by the clamp alone and the
    assertion does not depend on this page's spacing."""
    sid = _session()
    try:
        run, _gap = _crowded_run(client, sid)
        r = _edit(client, sid, run['id'], 'text', text=run['text'].strip()[0])
        assert r.status_code == 200
        body = r.get_json()
        assert 'shrunk_to_fit' not in body and 'too_long_for_space' not in body
        assert b' Tz' not in _stream(sid)
    finally:
        entry.sessions.pop(sid, None)


def test_a_free_line_end_is_never_constrained(client):
    """With nothing to the right there is nothing to collide with, so the text
    is emitted at its natural width however long it is."""
    sid = _session()
    try:
        items = _items(client, sid)
        free = next((i for i in items if _neighbour_of(items, i) is None), None)
        assert free, 'no run on this page ends its line — bad fixture'
        r = _edit(client, sid, free['id'], 'text',
                  text='a considerably longer replacement than the original')
        assert r.status_code == 200
        assert 'shrunk_to_fit' not in r.get_json()
        assert 'too_long_for_space' not in r.get_json()
    finally:
        entry.sessions.pop(sid, None)


def test_text_that_cannot_be_made_to_fit_says_so(client):
    """Readable-but-overlapping beats illegible-but-contained — but the user
    has to be told, or the only clue is spotting the overlap."""
    sid = _session()
    try:
        run, _gap = _crowded_run(client, sid)
        r = _edit(client, sid, run['id'], 'text',
                  text='Conditions of settlement apply to this invoice in full '
                       'and without any exception whatsoever')
        assert r.status_code == 200
        assert r.get_json().get('too_long_for_space'),             'the user was left to discover the overlap on their own'
    finally:
        entry.sessions.pop(sid, None)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
