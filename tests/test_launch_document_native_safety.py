"""Regression tests for "double-clicking a PDF crashes the app at the splash".

v3.3 moved the launch document's load onto a background thread to overlap the
WebView start-up.  The load runs pdfium and pikepdf, which then ran while the
main thread loaded the .NET runtime and WinForms for the window; the shared CRT
heap was corrupted and pdfium aborted the process (0x80000003, faulting module
pdfium.dll) — about half of all double-click launches of a 2.6 MB invoice.
Opening the same file by drag-and-drop was fine: no overlap.

Two halves, both pinned here:

  * main() loads a launch document on the MAIN thread, before the server or the
    window is started — nothing to overlap with;
  * pdf_core's own pdfium calls (_preload_file, render_page_thumbnail) hold
    _NATIVE_LOCK, because the same loads run on Flask threads for a
    second-instance hand-off or an upload while the viewer renders — and the
    lock is reentrant so callers that already hold it keep working.
"""
import os
import threading

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry
from pdf import pdf_core
from pdf.ocr_utils import _NATIVE_LOCK
from bootstrap import single_instance

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'text_page.pdf')


def _held():
    return _NATIVE_LOCK._is_owned()


# ── main(): the launch document loads before anything else is running ────────

def test_launch_document_loads_on_the_main_thread_before_the_server(monkeypatch):
    events = []
    monkeypatch.setenv('PATCHWORK_NO_BROWSER', '1')          # headless: no window
    monkeypatch.setattr(single_instance, 'find_initial_file', lambda *a: FIXTURE)
    monkeypatch.setattr(single_instance, 'become_primary_or_handoff',
                        lambda *a, **k: 49999)
    monkeypatch.setattr(entry, 'start_heavy_init_background', lambda: None)
    monkeypatch.setattr(entry, '_mru_record', lambda *a: None)

    def fake_preload(path, sid):
        events.append(('preload', threading.current_thread() is threading.main_thread(),
                       threading.active_count()))
        entry.sessions[sid]['pages'].append({'id': 'p1'})
    monkeypatch.setattr(entry, '_preload_file', fake_preload)
    monkeypatch.setattr(entry.app, 'run', lambda *a, **k: events.append(('server',)))

    before = threading.active_count()
    try:
        entry.main()
    finally:
        entry.sessions.pop(entry.INITIAL_SESSION_ID, None)

    assert [e[0] for e in events] == ['preload', 'server'], (
        'the launch document must be loaded before the server/window start')
    assert events[0][1], (
        'the launch document must load on the main thread — on a worker it '
        'races the .NET/WinForms start-up and pdfium aborts the app')
    assert events[0][2] == before, 'and no thread may be started for it'
    assert not any(t.name == 'pw-initial-preload' for t in threading.enumerate())


# ── pdf_core holds the native lock around its own pdfium calls ───────────────

class _SpyDoc:
    """Stands in for pypdfium2.PdfDocument and records whether the lock was
    held for every call made on it."""
    seen = []

    def __init__(self, *_a, **_k):
        _SpyDoc.seen.append(('open', _held()))

    def close(self):
        _SpyDoc.seen.append(('close', _held()))


@pytest.fixture()
def spy_pdfium(monkeypatch):
    import pypdfium2
    _SpyDoc.seen = []
    monkeypatch.setattr(pypdfium2, 'PdfDocument', _SpyDoc)

    def fake_thumb(doc, i, width=200):
        _SpyDoc.seen.append(('render', _held()))
        return 'thumb'
    monkeypatch.setattr(pdf_core, '_render_thumbnail_from_doc', fake_thumb)
    return _SpyDoc.seen


def test_preload_holds_the_lock_for_every_pdfium_call(spy_pdfium):
    sid = 'native-lock-test'
    pdf_core.sessions[sid] = {'pages': [], 'first_file_path': FIXTURE, 'comments': {}}
    try:
        pdf_core._preload_file(FIXTURE, sid)
        assert pdf_core.sessions[sid]['pages'], 'the fixture should load'
    finally:
        pdf_core.sessions.pop(sid, None)
    kinds = [k for k, _ in spy_pdfium]
    assert kinds[0] == 'open' and kinds[-1] == 'close' and 'render' in kinds
    assert all(held for _, held in spy_pdfium), (
        f'every pdfium call in _preload_file must hold _NATIVE_LOCK: {spy_pdfium}')
    assert not _held(), 'and release it afterwards'


def test_render_page_thumbnail_takes_the_lock_itself(spy_pdfium):
    pdf_core.render_page_thumbnail(b'%PDF', 0)
    assert spy_pdfium and all(held for _, held in spy_pdfium), spy_pdfium


def test_callers_already_holding_the_lock_do_not_deadlock(spy_pdfium):
    """pdf_core calls render_page_thumbnail from inside `with _NATIVE_LOCK:` —
    with a plain Lock this would hang forever."""
    done = threading.Event()

    def work():
        with _NATIVE_LOCK:
            pdf_core.render_page_thumbnail(b'%PDF', 0)
        done.set()
    t = threading.Thread(target=work, daemon=True)
    t.start()
    assert done.wait(5), 'nested acquisition deadlocked — _NATIVE_LOCK must be reentrant'
