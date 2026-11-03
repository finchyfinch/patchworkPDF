"""A scan that has been edited, SAVED and opened again.

Editing a scan writes real text operators into it.  While the session lives,
`ocr_source_bytes` remembers the pristine scan (see test_viewer_scanned_edit).
But that stash is in memory, not in the file: after Save and reopen the page
has native characters and nothing says it was ever a scan, so every "is this
a scan?" test — which counts characters — says no.  Reported against v3.1:

    "Now when I select an area around text, it isn't doing OCR to detect the
     text and copy to clipboard.  In fact, the only text I can now edit is the
     text that was modified in the save."

Measured on the sample invoice: 185 editable words before the save, 1 after.
v2.24 left the user a manual "Force OCR" button for this; v3.1 dropped it with
the editor.  The fix recognises the page (`is_mixed_scan_page`) instead, so
there is no button to find.

A page with no extractable text is what production calls a scan (that is
literally the check), so a blank page stands in for one, and OCR itself is
replaced by a stub — the suite must not depend on Tesseract being installed.
"""
import io
import re

import _bootstrap  # noqa: F401  (path setup)

import pypdf
import pytest

import entry
from pdf import ocr_utils
from routes import editor as _editor
from routes import viewer as _viewer


@pytest.fixture(scope='module', autouse=True)
def _heavy():
    _editor._heavy_init()


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


def _post(client, path, body):
    return client.post(path, json=body, headers={'X-App-Token': entry.APP_TOKEN})


def _blank_page(w=612, h=792):
    wr = pypdf.PdfWriter()
    wr.add_blank_page(width=w, height=h)
    buf = io.BytesIO()
    wr.write(buf)
    return buf.getvalue()


def _text_page():
    wr = pypdf.PdfWriter()
    wr.add_page(pypdf.PdfReader('tests/fixtures/text_page.pdf').pages[1])
    buf = io.BytesIO()
    wr.write(buf)
    return buf.getvalue()


def _saved(page_bytes):
    """What Save + reopen hands back: the page written out through pypdf, on a
    fresh record with no in-memory stash."""
    wr = pypdf.PdfWriter()
    wr.add_page(pypdf.PdfReader(io.BytesIO(page_bytes)).pages[0])
    buf = io.BytesIO()
    wr.write(buf)
    return buf.getvalue()


def _session(page_bytes, sid):
    entry.sessions[sid] = {
        'pages': [{'id': 'pg1', 'filename': 's.pdf', 'page_num': 1,
                   'total_pages': 1, 'thumbnail': '', 'pdf_bytes': page_bytes}],
        'comments': {},
    }
    return sid


def _rec(sid):
    return entry.sessions[sid]['pages'][0]


def _stamp(client, sid, text='GST', x=100, y=700):
    r = _post(client, '/api/viewer/add_text',
              {'session_id': sid, 'page_id': 'pg1',
               'item': {'type': 'text', 'text': text, 'x': x, 'y': y,
                        'size': 12, 'color': '#000000', 'rl_font': 'Helvetica'}})
    assert r.status_code == 200, r.get_data(as_text=True)


def _edited_scan(client, sid='mx'):
    """A scan with one word written into it, as it comes back from a save."""
    _session(_blank_page(), sid + '_src')
    try:
        _stamp(client, sid + '_src')
        return _saved(_rec(sid + '_src')['pdf_bytes'])
    finally:
        entry.sessions.pop(sid + '_src', None)


# ── recognising the page ────────────────────────────────────────────────────

def test_a_saved_edited_scan_is_recognised(client):
    assert _editor.is_mixed_scan_page(_edited_scan(client)) is True


def test_an_untouched_scan_is_not_mixed_it_is_just_a_scan():
    # Image-only pages have their own (working) path; don't reroute them.
    assert _editor.is_mixed_scan_page(_blank_page()) is False


def test_a_text_page_is_not_mixed():
    assert _editor.is_mixed_scan_page(_text_page()) is False


def test_an_edited_text_page_is_not_mixed(client):
    """Our marker alone must not trip it: the page's own text is still there
    (covered, not removed), so native text outnumbers what we injected."""
    sid = _session(_text_page(), 'mx_txt')
    try:
        _stamp(client, sid)
        assert _editor.is_mixed_scan_page(_saved(_rec(sid)['pdf_bytes'])) is False
    finally:
        entry.sessions.pop(sid, None)


def test_injected_characters_are_counted_with_escapes_as_one():
    content = (b'q\n% pw_inj 0a1b2c3d\nBT\n/F1 12 Tf\n1 0 0 1 10 10 Tm\n'
               b'(GST) Tj\nET\n% pw_inj 0a1b2c3e\nBT\n(a\\(b\\051 c) Tj\nET\nQ\n'
               b'BT (not ours) Tj ET')
    # "GST" + "a(b)c" (the space is not visible) — and nothing outside a block.
    assert _editor._injected_visible_chars(content) == 3 + 5


# ── the Pointer on a reopened page ──────────────────────────────────────────

def test_reopened_page_is_extracted_against_its_own_pixels(client, monkeypatch):
    seen = {}
    real = _editor._extract_text_and_graphics

    def spy(pdf_bytes, ocr_source_bytes=None, progress_cb=None):
        seen['src'], seen['cur'] = ocr_source_bytes, pdf_bytes
        return real(pdf_bytes, ocr_source_bytes, progress_cb=progress_cb)

    monkeypatch.setattr(_editor, '_extract_text_and_graphics', spy)
    sid = _session(_edited_scan(client), 'mx_open')
    try:
        r = _post(client, '/api/viewer/page_elements',
                  {'session_id': sid, 'page_id': 'pg1'})
        assert r.status_code == 200
        assert seen['src'] == seen['cur'], \
            'a reopened scan went down the native path: only the edits are editable'
    finally:
        entry.sessions.pop(sid, None)


def _fake_ocr(items):
    def ocr(_pdf_bytes, scale=None, progress_cb=None):
        return [dict(it) for it in items]
    return ocr


def test_a_mixed_source_is_OCRd_and_its_edits_are_not_offered_twice(
        client, monkeypatch):
    """extract_page_elements only OCRs a page with NO characters, so a mixed
    source has to be OCR'd explicitly — and since its pixels show the edits,
    an OCR word lying under a native run is that run read again."""
    cur = _edited_scan(client)
    native = [t for t in _editor.extract_page_elements(cur, allow_ocr_fallback=False)[4]
              if t.get('type') == 'text']
    assert native, 'precondition: the stamped word is native text'
    n = native[0]
    monkeypatch.setattr(_editor, 'ocr_extract_text_items', _fake_ocr([
        {'id': 'ocr_1', 'type': 'text', 'ocr': True, 'text': 'GST',
         'x': n['x'], 'y': n['y'], 'x2': n['x2'], 'y2': n['y2']},
        {'id': 'ocr_2', 'type': 'text', 'ocr': True, 'text': 'Invoice',
         'x': 300, 'y': 400, 'x2': 350, 'y2': 410},
    ]))
    items = _editor._extract_text_and_graphics(cur, cur)[4]
    texts = sorted(t['text'] for t in items if t.get('type') == 'text')
    assert 'Invoice' in texts, 'the scanned words were not OCRd'
    assert texts.count('GST') == 1, f'the edit was offered twice: {texts}'


def test_a_pre_crop_source_is_not_used(client, monkeypatch):
    """OCR boxes from before a crop land in the wrong place afterwards; text
    selection already refused that case, the Pointer now does too."""
    seen = {}
    real = _editor._extract_text_and_graphics

    def spy(pdf_bytes, ocr_source_bytes=None, progress_cb=None):
        seen['src'] = ocr_source_bytes
        return real(pdf_bytes, ocr_source_bytes, progress_cb=progress_cb)

    monkeypatch.setattr(_editor, '_extract_text_and_graphics', spy)
    sid = _session(_blank_page(400, 500), 'mx_crop')
    try:
        _rec(sid)['ocr_source_bytes'] = _blank_page(612, 792)
        _post(client, '/api/viewer/page_elements', {'session_id': sid, 'page_id': 'pg1'})
        assert seen['src'] is None
    finally:
        entry.sessions.pop(sid, None)


def test_editing_a_reopened_scan_keeps_its_pixels_as_the_source(client):
    sid = _session(_edited_scan(client), 'mx_keep')
    try:
        reopened = _rec(sid)['pdf_bytes']
        _stamp(client, sid, 'More', x=100, y=600)
        assert _rec(sid)['ocr_source_bytes'] == reopened, \
            'without the stash every later edit re-OCRs from scratch'
    finally:
        entry.sessions.pop(sid, None)


# ── text selection on a reopened page ───────────────────────────────────────

def test_selection_on_a_reopened_scan_OCRs_the_rest_of_the_page(
        client, monkeypatch):
    sid = _session(_edited_scan(client), 'mx_sel')
    try:
        chars = _viewer._get_page_chars(_rec(sid)['pdf_bytes'])
        vis = [c for c in chars if c['c'].strip()]
        x1 = min(c['x'] for c in vis); x2 = max(c['x'] + c['w'] for c in vis)
        y1 = min(c['y'] for c in vis); y2 = max(c['y'] + c['h'] for c in vis)
        words = [{'text': 'GST', 'x': x1, 'y': y1, 'w': x2 - x1, 'h': y2 - y1},
                 {'text': 'Invoice', 'x': 300, 'y': 400, 'w': 50, 'h': 10}]
        monkeypatch.setattr(_viewer, '_get_ocr_words_cached',
                            lambda *a, **k: [dict(w) for w in words])
        r = _post(client, '/api/viewer/page_text', {'session_id': sid, 'page_id': 'pg1'})
        d = r.get_json()
        got = [c['c'] for c in d['chars']]
        assert d['ocr_used'] is True
        assert 'Invoice' in got, 'the scanned words are still unselectable'
        assert 'GST' not in got, 'the edit would be copied twice (OCR word + chars)'
        assert ''.join(c for c in got if len(c) == 1).count('G') == 1
    finally:
        entry.sessions.pop(sid, None)


def test_selection_on_a_text_page_does_not_OCR(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError('OCR ran on a page whose text layer is complete')
    monkeypatch.setattr(_viewer, '_get_ocr_words_cached', boom)
    sid = _session(_text_page(), 'mx_native')
    try:
        r = _post(client, '/api/viewer/page_text', {'session_id': sid, 'page_id': 'pg1'})
        assert r.get_json()['ocr_used'] is False
    finally:
        entry.sessions.pop(sid, None)


# ── the OCR second opinion (VAT read as WAT) ────────────────────────────────

@pytest.mark.parametrize('orig,conf,reread,rconf,want', [
    ('WAT',      96, 'VAT|',     87, 'VAT'),       # the report: band edge as '|'
    ('Unt',      38, 'Unit',     96, 'Unit'),      # a lost thin stroke
    ('VAT',      96, 'VAT',      64, None),        # agreement changes nothing
    ('GROUP',    96, 'GR',       95, None),        # shorter: the crop lost context
    ('Quantity', 54, 'Quantit',  92, None),
    ('No',       95, 'N',        93, None),
    ('No.',      95, '[Nou',     36, None),        # not confident enough
    ('Excl.',    58, 'Excl.',    64, None),
    ('Descriptio', 81, 'Descriptio!', 94, None),   # edge junk only
    ('WAT',      96, 'VAT|',     40, None),        # right answer, too unsure
    ('Unt',      90, 'Unit',     96, None),        # insertion needs a big margin
    ('ABCDEF',   90, 'XYZDEF',   95, None),        # too many letters changed
])
def test_reread_is_only_accepted_when_it_keeps_the_word_shape(
        orig, conf, reread, rconf, want):
    assert ocr_utils._accept_reread(orig, conf, reread, rconf) == want


def test_edge_junk_is_kept_where_the_word_really_has_it():
    assert ocr_utils._strip_edge_artifacts('(VAT)|', '(VAT)') == '(VAT)'
    assert ocr_utils._strip_edge_artifacts('|VAT|', 'VAT') == 'VAT'


# ── editing a row / area as one block ───────────────────────────────────────

def _row(items):
    """The first line of the page with at least three words, left to right."""
    rows = {}
    for t in items:
        rows.setdefault(round((t['y'] + t['y2']) / 2), []).append(t)
    for _, r in sorted(rows.items(), reverse=True):
        if len(r) >= 3:
            return sorted(r, key=lambda t: t['x'])
    raise AssertionError('no multi-word row on the manual page')


def _two_rows(items):
    rows = {}
    for t in items:
        rows.setdefault(round((t['y'] + t['y2']) / 2), []).append(t)
    keys = sorted((k for k, r in rows.items() if len(r) >= 2), reverse=True)
    for a, b in zip(keys, keys[1:]):
        if 6 < a - b < 30:
            return (sorted(rows[a], key=lambda t: t['x'])[:2],
                    sorted(rows[b], key=lambda t: t['x'])[:2])
    raise AssertionError('no pair of adjacent rows on the manual page')


def _elements(client, sid):
    r = _post(client, '/api/viewer/page_elements', {'session_id': sid, 'page_id': 'pg1'})
    assert r.status_code == 200
    return r.get_json()['items']


def _box(items, pad=2):
    return {'x1': min(t['x'] for t in items) - pad, 'y1': min(t['y'] for t in items) - pad,
            'x2': max(t['x2'] for t in items) + pad, 'y2': max(t['y2'] for t in items) + pad}


def test_a_row_becomes_one_block_in_one_undo_step(client):
    sid = _session(_text_page(), 'blk1')
    try:
        row = _row(_elements(client, sid))[:3]
        before = _rec(sid)['pdf_bytes']
        r = _post(client, '/api/viewer/edit_block', {
            'session_id': sid, 'page_id': 'pg1', 'ids': [t['id'] for t in row],
            'text': 'Zebra quokka', 'lines': ['Zebra quokka'], 'box': _box(row)})
        assert r.status_code == 200, r.get_data(as_text=True)
        d = r.get_json()
        assert d['success'] and d['undo_count'] >= 1
        assert _rec(sid).get('merge_regions'), 'the replaced footprint was not recorded'
        text = ''.join(c['c'] for c in _post(client, '/api/viewer/page_text',
                       {'session_id': sid, 'page_id': 'pg1'}).get_json()['chars'])
        assert 'Zebraquokka' in text.replace(' ', '')

        u = _post(client, '/api/viewer/undo', {'session_id': sid})
        assert u.status_code == 200, u.get_data(as_text=True)
        assert _rec(sid)['pdf_bytes'] == before, 'one Ctrl+Z must restore every word'
        assert not _rec(sid).get('merge_regions'), \
            'undo restored the bytes but left their words filtered out'
    finally:
        entry.sessions.pop(sid, None)


def test_rows_keep_their_own_line_pitch(client):
    """A default 1.35 em spread a tight two-line table header well below its
    band; the block uses the pitch of the rows it replaced."""
    sid = _session(_text_page(), 'blk2')
    try:
        top, bottom = _two_rows(_elements(client, sid))
        picked = top + bottom
        pitch = (sum(t['y2'] for t in top) / len(top)
                 - sum(t['y2'] for t in bottom) / len(bottom))
        r = _post(client, '/api/viewer/edit_block', {
            'session_id': sid, 'page_id': 'pg1', 'ids': [t['id'] for t in picked],
            'text': 'one two', 'lines': ['one', 'two'], 'box': _box(picked)})
        assert r.status_code == 200, r.get_data(as_text=True)
        content = pypdf.PdfReader(io.BytesIO(_rec(sid)['pdf_bytes'])).pages[0] \
            .get_contents().get_data()
        ys = []
        for blk in re.findall(rb'% pw_inj [0-9a-f]+\s(.*?)\bET\b', content, re.S):
            m = re.search(rb'([-\d.]+) ([-\d.]+) Tm', blk)
            if m and (b'(one)' in blk or b'(two)' in blk):
                ys.append(float(m.group(2)))
        assert len(ys) == 2, ys
        assert abs(abs(ys[0] - ys[1]) - pitch) < 0.6, \
            f'lines {abs(ys[0] - ys[1]):.2f}pt apart, the rows were {pitch:.2f}pt'
    finally:
        entry.sessions.pop(sid, None)


def test_an_empty_block_deletes_the_words(client):
    sid = _session(_text_page(), 'blk3')
    try:
        row = _row(_elements(client, sid))[:2]
        r = _post(client, '/api/viewer/edit_block', {
            'session_id': sid, 'page_id': 'pg1', 'ids': [t['id'] for t in row],
            'text': '   ', 'lines': [], 'box': _box(row)})
        assert r.status_code == 200, r.get_data(as_text=True)
        same_line = {(t['text'], round(t['x'])) for t in _elements(client, sid)
                     if abs(t['y'] - row[0]['y']) < 2}
        assert not any((t['text'], round(t['x'])) in same_line for t in row), same_line
    finally:
        entry.sessions.pop(sid, None)


def test_stale_ids_are_refused_and_change_nothing(client):
    sid = _session(_text_page(), 'blk4')
    try:
        before = _rec(sid)['pdf_bytes']
        r = _post(client, '/api/viewer/edit_block', {
            'session_id': sid, 'page_id': 'pg1', 'ids': ['nope'],
            'text': 'x', 'lines': ['x'], 'box': {'x1': 0, 'y1': 0, 'x2': 9, 'y2': 9}})
        assert r.status_code == 404
        assert r.get_json().get('error_key') == 'err.text_run_gone'
        assert _rec(sid)['pdf_bytes'] == before
    finally:
        entry.sessions.pop(sid, None)



# ── alignment of a row / area block ─────────────────────────────────────────
# A right-aligned or centred header cell used to come back hard left.

def _run(text, x, x2, y):
    return {'text': text, 'x': x, 'x2': x2, 'y': y, 'y2': y + 7}


@pytest.mark.parametrize('rows,want', [
    # "Line Amount / Excl. VAT" from the sample invoice: shared right edge.
    ([[('Line', 486, 502), ('Amount', 505, 539.3)],
      [('Excl.', 502, 519), ('VAT', 522, 539.3)]], 'right'),
    # centred: edges differ, centres agree
    ([[('Quantity', 100, 140)], [('ea', 115, 125)]], 'center'),
    # ragged-right paragraph: lefts agree
    ([[('The', 72, 90), ('quick', 93, 120)], [('fox', 72, 85)]], 'left'),
    # justified: both edges agree -> left wins the tie
    ([[('aaaa', 72, 200)], [('bbbb', 72, 200)]], 'left'),
    # nothing lines up -> left
    ([[('x', 72, 90)], [('y', 130, 170)]], 'left'),
])
def test_block_alignment_is_read_from_the_rows(rows, want):
    items = []
    for i, row in enumerate(rows):
        items += [_run(t, x, x2, 500 - i * 10) for t, x, x2 in row]
    assert _viewer._block_alignment(items, 4.2, 10.8)[0] == want


def test_a_single_row_stays_left():
    assert _viewer._block_alignment([_run('a', 72, 90, 500)], 4.2, 10.8) == ('left', None)


def test_right_aligned_lines_end_on_the_original_edge():
    """apply_edits hangs each line off the anchor, measured with the embedded
    font's own widths — so different-length lines share one right edge."""
    from platform_io import font_assets as fa
    item = {'id': 'mk_t', 'type': 'text', 'multiline': True,
            'text': 'Line Amount Excl. GST', 'lines': ['Line Amount', 'Excl. GST'],
            'x': 480.0, 'y': 440.0, 'boxW': 60, 'boxH': 30, 'size': 10.0,
            'lineH': 9.6, 'color': '#000000', 'rl_font': 'Calibri',
            'align': 'right', '_alignDX': 59.28}
    out, _ = _editor.apply_edits(_blank_page(), {
        'deleted_ids': [], 'moved': [], 'text_changed': [], 'markup': [item],
        'redacts': [], 'pdf_image_edits': [], 'original_elements': [],
        'watermark': None}, 612, 792)
    content = pypdf.PdfReader(io.BytesIO(out)).pages[0].get_contents().get_data()
    ends = []
    for blk in re.findall(rb'% pw_inj [0-9a-f]+\s(.*?)\bET\b', content, re.S):
        m = re.search(rb'([-\d.]+) ([-\d.]+) Tm', blk)
        t = re.search(rb'\((.*?)\) Tj', blk)
        if m and t:
            ends.append(float(m.group(1)) + fa.string_width('Calibri', t.group(1).decode('latin-1'), 10.0))
    assert len(ends) == 2, ends
    assert all(abs(e - 539.28) < 0.05 for e in ends), ends


# ── stale ids are refused ───────────────────────────────────────────────────
# Run ids are positional; after an undo or rotate the same id can name another
# run.  The client sends the text it means, and a mismatch changes nothing.

def test_edit_text_refuses_an_id_that_names_different_words(client):
    sid = _session(_text_page(), 'stale1')
    try:
        it = _elements(client, sid)[0]
        before = _rec(sid)['pdf_bytes']
        r = _post(client, '/api/viewer/edit_text', {
            'session_id': sid, 'page_id': 'pg1', 'id': it['id'],
            'expect': it['text'] + ' (was)', 'op': 'text', 'text': 'Wrong'})
        assert r.status_code == 409
        assert r.get_json()['error_key'] == 'err.text_run_gone'
        assert _rec(sid)['pdf_bytes'] == before, 'the page was rewritten anyway'
        ok = _post(client, '/api/viewer/edit_text', {
            'session_id': sid, 'page_id': 'pg1', 'id': it['id'],
            'expect': it['text'], 'op': 'text', 'text': 'Right'})
        assert ok.status_code == 200, ok.get_data(as_text=True)
    finally:
        entry.sessions.pop(sid, None)


def test_edit_block_refuses_ids_that_name_different_words(client):
    sid = _session(_text_page(), 'stale2')
    try:
        row = _row(_elements(client, sid))[:2]
        before = _rec(sid)['pdf_bytes']
        r = _post(client, '/api/viewer/edit_block', {
            'session_id': sid, 'page_id': 'pg1', 'ids': [t['id'] for t in row],
            'expect': [row[1]['text'], row[0]['text']],        # wrong order
            'text': 'x', 'lines': ['x'], 'box': _box(row)})
        assert r.status_code == 409
        assert _rec(sid)['pdf_bytes'] == before
    finally:
        entry.sessions.pop(sid, None)

if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
