"""Regression tests for "Already running" when a document is opened just after
closing the app.

Closing the window does not end the process at once: WebView2 tears down and the
clean-exit purge runs (~3 s on a fast PC, far longer on a slow laptop), and the
single-instance lock is held until the process is gone.  A document
double-clicked in that gap met one of two failures:

  * the launcher found the lock held, polled the dying instance for 5 s without
    ever re-trying the lock, and said "already running" — reproduced on every
    attempt here, even 600 ms after closing;
  * or, while the dying instance still answered, the file was handed to it,
    loaded into a session no window would show, and lost with the process.

What must stay true:

  * the launcher re-tries the lock while it waits, and starts as the primary as
    soon as the old process has gone;
  * an instance whose last window has closed says so — /api/ping reports it,
    /api/open_in_new_tab answers 503 (also when the window closes mid-load) — and
    the launcher treats that as "wait", not "delivered";
  * a genuinely running instance still gets the hand-off, and a launch with no
    file still hears "already running".
"""
import urllib.error

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry
from bootstrap import single_instance as si


@pytest.fixture()
def fake_clock(monkeypatch):
    """time.time advances with every sleep, so waits finish instantly."""
    now = [1000.0]
    monkeypatch.setattr(si.time, 'time', lambda: now[0])
    monkeypatch.setattr(si.time, 'sleep', lambda s: now.__setitem__(0, now[0] + s))
    return now


@pytest.fixture()
def recorder(monkeypatch):
    calls = {'message': 0, 'forward': []}
    monkeypatch.setattr(si, '_show_already_running_message',
                        lambda: calls.__setitem__('message', calls['message'] + 1))
    return calls


def _lock_frees_after(monkeypatch, n_refusals):
    """The lock is held for the first n attempts, then the holder exits."""
    attempts = [0]

    def _try():
        attempts[0] += 1
        return attempts[0] > n_refusals
    monkeypatch.setattr(si, '_try_acquire_lock', _try)
    return attempts


# ── launcher: outlast an instance that is closing ────────────────────────────

def test_takes_over_once_the_closing_instance_exits(monkeypatch, fake_clock, recorder):
    _lock_frees_after(monkeypatch, 15)              # ~1.4 s of polling, then free
    monkeypatch.setattr(si, 'find_running_instance', lambda: None)   # stopped answering
    assert si.handle_existing_instance('C:/docs/a.pdf') is False, (
        'once the old process has exited the launch must carry on as the primary')
    assert recorder['message'] == 0, 'and never say "already running"'


def test_takes_over_with_no_file_too(monkeypatch, fake_clock, recorder):
    _lock_frees_after(monkeypatch, 5)
    monkeypatch.setattr(si, 'find_running_instance', lambda: None)
    assert si.handle_existing_instance(None) is False
    assert recorder['message'] == 0


def test_refused_hand_off_waits_for_the_lock(monkeypatch, fake_clock, recorder):
    """The dying instance still answered but refused (503): wait, then start."""
    _lock_frees_after(monkeypatch, 4)
    monkeypatch.setattr(si, 'find_running_instance', lambda: 49371)
    sent = []
    monkeypatch.setattr(si, '_forward_file', lambda p, f: sent.append(f) or False)
    assert si.handle_existing_instance('C:/docs/a.pdf') is False
    assert sent, 'it was offered first'
    assert recorder['message'] == 0


def test_slow_exit_is_covered_by_the_wait(monkeypatch, fake_clock, recorder):
    """A slow laptop: the old process takes ~12 s to exit — inside the wait."""
    _lock_frees_after(monkeypatch, 120)             # 120 polls x 0.1 s
    monkeypatch.setattr(si, 'find_running_instance', lambda: None)
    assert si.handle_existing_instance('C:/docs/a.pdf') is False
    assert recorder['message'] == 0


def test_become_primary_also_outlasts_a_closing_instance(monkeypatch, fake_clock, recorder):
    _lock_frees_after(monkeypatch, 10)
    monkeypatch.setattr(si, 'find_running_instance', lambda: None)
    monkeypatch.setattr(si, '_pick_free_port', lambda: 49372)
    monkeypatch.setattr(si, '_write_rendezvous', lambda *a, **k: None)
    monkeypatch.setattr(si.atexit, 'register', lambda f: None)
    assert si.become_primary_or_handoff('C:/docs/a.pdf') == 49372
    assert recorder['message'] == 0


# ── launcher: unchanged behaviour with a live instance ───────────────────────

def test_live_instance_still_gets_the_file(monkeypatch, fake_clock, recorder):
    monkeypatch.setattr(si, '_try_acquire_lock', lambda: False)
    monkeypatch.setattr(si, 'find_running_instance', lambda: 49371)
    sent = []
    monkeypatch.setattr(si, '_forward_file', lambda p, f: sent.append((p, f)) or True)
    assert si.handle_existing_instance('C:/docs/a.pdf') is True
    assert sent == [(49371, 'C:/docs/a.pdf')]
    assert recorder['message'] == 0


def test_live_instance_with_no_file_still_says_already_running(monkeypatch, fake_clock, recorder):
    monkeypatch.setattr(si, '_try_acquire_lock', lambda: False)
    monkeypatch.setattr(si, 'find_running_instance', lambda: 49371)
    assert si.handle_existing_instance(None) is True
    assert recorder['message'] == 1


def test_hung_holder_still_ends_in_the_message(monkeypatch, fake_clock, recorder):
    """Lock held forever, nothing answering: give up after the wait."""
    monkeypatch.setattr(si, '_try_acquire_lock', lambda: False)
    monkeypatch.setattr(si, 'find_running_instance', lambda: None)
    start = fake_clock[0]
    assert si.handle_existing_instance('C:/docs/a.pdf') is True
    assert recorder['message'] == 1
    assert fake_clock[0] - start >= si._HANDOFF_WAIT


# ── launcher: reading the running instance's answers ─────────────────────────

def test_an_instance_reporting_closing_is_not_running(monkeypatch):
    monkeypatch.setattr(si, '_read_rendezvous', lambda: {'port': 49371})
    monkeypatch.setattr(si, '_ping', lambda p, timeout=None: {'app': 'patchwork-pdf', 'closing': True})
    monkeypatch.setattr(si, '_candidate_ports', lambda: [49371])
    assert si.find_running_instance() is None


def test_a_live_instance_is_running(monkeypatch):
    monkeypatch.setattr(si, '_read_rendezvous', lambda: {'port': 49371})
    monkeypatch.setattr(si, '_ping', lambda p, timeout=None: {'app': 'patchwork-pdf', 'closing': False})
    assert si.find_running_instance() == 49371


@pytest.mark.parametrize('error, delivered', [
    (urllib.error.HTTPError('u', 503, 'closing', {}, None), False),
    (urllib.error.URLError(ConnectionRefusedError()), False),
    (ConnectionResetError(), False),
    (urllib.error.HTTPError('u', 500, 'could not load', {}, None), True),
    (TimeoutError('slow load'), True),
    (None, True),
])
def test_forward_file_reports_whether_it_was_delivered(monkeypatch, error, delivered):
    monkeypatch.setattr(si, '_read_rendezvous', lambda: {'pid': 1, 'port': 49371, 'token': 't'})
    monkeypatch.setattr(si, '_grant_foreground', lambda pid: None)

    def _urlopen(req, timeout=None):
        if error is not None:
            raise error
    monkeypatch.setattr(si.urllib.request, 'urlopen', _urlopen)
    assert si._forward_file(49371, 'C:/docs/a.pdf') is delivered


# ── the closing instance: say so, and refuse documents ───────────────────────

@pytest.fixture()
def closing(monkeypatch):
    monkeypatch.setattr(entry, '_APP_CLOSING', False)
    entry.app.config['TESTING'] = True
    return lambda: monkeypatch.setattr(entry, '_APP_CLOSING', True)


def test_ping_reports_closing(closing):
    with entry.app.test_client() as c:
        assert c.get('/api/ping').get_json()['closing'] is False
        closing()
        assert c.get('/api/ping').get_json()['closing'] is True


def test_hand_off_is_refused_once_closing(closing, monkeypatch, tmp_path):
    pdf = tmp_path / 'a.pdf'
    pdf.write_bytes(b'%PDF-1.4\n')
    loaded = []
    monkeypatch.setattr(entry, '_preload_file', lambda p, s: loaded.append(p))
    closing()
    with entry.app.test_client() as c:
        r = c.post('/api/open_in_new_tab', json={'file_path': str(pdf)},
                   headers={'X-App-Token': entry.APP_TOKEN})
    assert r.status_code == 503
    assert loaded == [], 'nothing is loaded into a process that is exiting'


def test_window_closing_mid_load_hands_the_file_back(closing, monkeypatch, tmp_path):
    pdf = tmp_path / 'a.pdf'
    pdf.write_bytes(b'%PDF-1.4\n')
    monkeypatch.setattr(entry, '_target_window', lambda: None)
    before = set(entry.sessions)

    def _slow_load(path, sid):
        entry.sessions[sid]['pages'] = [{'id': 'p1'}]
        closing()                                   # the user closed the window meanwhile
    monkeypatch.setattr(entry, '_preload_file', _slow_load)
    with entry.app.test_client() as c:
        r = c.post('/api/open_in_new_tab', json={'file_path': str(pdf)},
                   headers={'X-App-Token': entry.APP_TOKEN})
    assert r.status_code == 503, 'the launcher must open it itself, not lose it'
    assert set(entry.sessions) == before, 'and the orphaned session is dropped'


def test_mark_closing_is_idempotent(monkeypatch):
    monkeypatch.setattr(entry, '_APP_CLOSING', False)
    entry._mark_closing()
    entry._mark_closing()
    assert entry._APP_CLOSING is True
