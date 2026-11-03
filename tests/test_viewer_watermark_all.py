"""Watermark every page — the document-level command (W5).

Three things make this different from the panel's single-page tool, and all
three are load-bearing.

**It applies exactly once per page.**  The shipped editor path did not: the
client looped over the pages calling `/api/watermark/apply_all` once per page,
while the route itself loops over every page and reads `exclude_page_id` — which
that client never sent.  An N-page document therefore got N stacked watermarks
on *every* page, measurably darker with each pass.  Measured on a 3-page
document: mean ink darkness 187 versus 225 for a single application.
test_every_page_gets_exactly_one_watermark is what stops that shape coming back.
(The editor route and its looping caller were removed with the Editor in v3.1.)

**It is outside the undo stack, by design.**  One byte snapshot per page against
a 30-entry / 64 MB history would be trimmed on any real document, so Ctrl+Z
would half-revert it — worse than no undo at all (plan §3.5).

**So it clears the history.**  Leaving the earlier entries would let one Ctrl+Z
restore a single page to a snapshot taken before the watermark, silently
removing it from that page alone while every other page kept it.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pypdf
import pypdfium2 as pdfium
import pytest

import entry

_SCALE = 2.0
_CFG = {'text': 'DRAFT', 'color': '#808080', 'opacity': 0.25,
        'size': 40, 'angleDeg': 45, 'layout': 'center'}


def _blank(w=400, h=400):
    wr = pypdf.PdfWriter()
    wr.add_blank_page(width=w, height=h)
    buf = io.BytesIO()
    wr.write(buf)
    return buf.getvalue()


def _render(pdf_bytes):
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return doc[0].render(scale=_SCALE).to_pil().convert('RGB')
    finally:
        doc.close()


def _ink(pdf_bytes):
    """(inked pixel count, mean darkness).  Stacked marks read darker."""
    img = _render(pdf_bytes)
    W, H = img.size
    px = img.load()
    vals = [(px[x, y][0] + px[x, y][1] + px[x, y][2]) / 3
            for y in range(H) for x in range(W)
            if px[x, y][0] < 250 or px[x, y][1] < 250 or px[x, y][2] < 250]
    return len(vals), (sum(vals) / len(vals) if vals else 255.0)


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _session(n=3, sid='wm-all'):
    entry.sessions[sid] = {
        'pages': [{'id': f'pg{i}', 'filename': 'a.pdf', 'page_num': i + 1,
                   'total_pages': n, 'thumbnail': '', 'pdf_bytes': _blank()}
                  for i in range(n)],
        'comments': {},
    }
    return sid


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _all(client, sid, **over):
    cfg = dict(_CFG)
    cfg.update(over)
    return _post(client, '/api/viewer/watermark_all',
                 {'session_id': sid, 'watermark': cfg})


def _pages(sid):
    return entry.sessions[sid]['pages']


# ── once per page ───────────────────────────────────────────────────────────

def test_every_page_gets_exactly_one_watermark(client):
    """The regression that motivated the route: N pages must not mean N marks.

    Compared against a single-page application of the same settings — the two
    have to be indistinguishable in ink, because both are one watermark.
    """
    sid = _session(3)
    one = _session(1, 'wm-all-baseline')
    try:
        assert _all(client, sid).status_code == 200
        assert _post(client, '/api/viewer/watermark',
                     {'session_id': one, 'page_id': 'pg0',
                      'watermark': _CFG}).status_code == 200

        _n1, d1 = _ink(_pages(one)[0]['pdf_bytes'])
        for p in _pages(sid):
            n, d = _ink(p['pdf_bytes'])
            assert n > 0, f"{p['id']} was not watermarked at all"
            assert abs(d - d1) < 3, (
                f"{p['id']} darkness {d:.1f} vs single-application {d1:.1f} — "
                'more than one watermark was stacked')
    finally:
        entry.sessions.pop(sid, None)
        entry.sessions.pop('wm-all-baseline', None)


def test_calling_it_twice_stacks_exactly_twice(client):
    """The counterpart: it is not idempotent, and should not pretend to be.
    Two deliberate runs stack two marks — which is why one run must be one."""
    sid = _session(2)
    try:
        _all(client, sid)
        _n, once = _ink(_pages(sid)[0]['pdf_bytes'])
        _all(client, sid)
        _n2, twice = _ink(_pages(sid)[0]['pdf_bytes'])
        assert twice < once - 3, f'second pass changed nothing ({once} → {twice})'
    finally:
        entry.sessions.pop(sid, None)


def test_it_reports_which_pages_it_changed(client):
    sid = _session(4)
    try:
        data = _all(client, sid).get_json()
        assert data['failed'] == 0
        assert [u['id'] for u in data['updated']] == ['pg0', 'pg1', 'pg2', 'pg3']
        assert all(u['thumbnail'] for u in data['updated']), \
            'thumbnails must come back, or the page rail shows stale pages'
    finally:
        entry.sessions.pop(sid, None)


# ── history ─────────────────────────────────────────────────────────────────

def test_it_adds_no_undo_entries(client):
    sid = _session(5)
    try:
        _all(client, sid)
        s = entry.sessions[sid]
        assert s.get('annot_undo') == [], \
            'five snapshots against a 30-entry history is the thing this avoids'
        assert _all(client, sid).get_json()['undo_count'] == 0
    finally:
        entry.sessions.pop(sid, None)


def test_it_clears_history_that_would_otherwise_half_revert_the_document(client):
    """An earlier per-page action must not stay undoable across this.

    Otherwise one Ctrl+Z restores that page's pre-watermark bytes and the
    watermark vanishes from one page while every other page keeps it.
    """
    sid = _session(3)
    try:
        r = _post(client, '/api/viewer/add_annotation',
                  {'session_id': sid, 'page_id': 'pg0', 'subtype': 'Square',
                   'rect': [10, 10, 60, 60], 'border_color': [1.0, 0.0, 0.0],
                   'border_width': 2.0})
        assert r.status_code == 200
        assert len(entry.sessions[sid]['annot_undo']) == 1

        assert _all(client, sid).status_code == 200
        assert entry.sessions[sid]['annot_undo'] == []
        assert entry.sessions[sid]['annot_redo'] == []

        # And an undo now genuinely has nothing to do, rather than quietly
        # restoring pg0 to a state without the watermark.
        _n, d0 = _ink(_pages(sid)[0]['pdf_bytes'])
        _post(client, '/api/viewer/undo', {'session_id': sid})
        _n2, d1 = _ink(_pages(sid)[0]['pdf_bytes'])
        assert abs(d0 - d1) < 1, 'undo reached past an un-undoable command'
    finally:
        entry.sessions.pop(sid, None)


def test_it_marks_the_document_dirty(client):
    sid = _session(2)
    try:
        _all(client, sid)
        from bootstrap import recovery as _recovery
        assert _recovery.has_pending(entry.sessions[sid])
    finally:
        entry.sessions.pop(sid, None)


# ── validation ──────────────────────────────────────────────────────────────

def test_empty_text_changes_nothing(client):
    sid = _session(3)
    try:
        before = [p['pdf_bytes'] for p in _pages(sid)]
        r = _all(client, sid, text='  ')
        assert r.status_code == 400
        assert (r.get_json() or {}).get('error_key') == 'err.watermark_settings'
        assert [p['pdf_bytes'] for p in _pages(sid)] == before
    finally:
        entry.sessions.pop(sid, None)


def test_unknown_session_is_reported(client):
    r = _post(client, '/api/viewer/watermark_all',
              {'session_id': 'no-such', 'watermark': _CFG})
    assert r.status_code == 404


def test_a_page_that_cannot_be_stamped_is_counted_not_fatal(client):
    """One broken page must not abandon the rest of the document."""
    sid = _session(3)
    try:
        _pages(sid)[1]['pdf_bytes'] = b'%PDF-1.4 not really a pdf'
        data = _all(client, sid).get_json()
        assert data['failed'] == 1, data
        assert [u['id'] for u in data['updated']] == ['pg0', 'pg2']
        assert _ink(_pages(sid)[0]['pdf_bytes'])[0] > 0
        assert _ink(_pages(sid)[2]['pdf_bytes'])[0] > 0
    finally:
        entry.sessions.pop(sid, None)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
