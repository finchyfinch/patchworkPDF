"""The run-once "What's New" note.

v3.1 moved the editor's tools into the viewer and removed the Editor button.
Someone who used that button every day needs telling once, or they reasonably
conclude the app is broken — that is the whole reason this exists.

Three behaviours carry the weight, and each has a way of going wrong that has
already happened in this codebase once:

  * **It fails CLOSED.**  Only an explicit, successfully-read "not yet shown"
    opens it.  `/api/license_status` learned this the hard way: treating an
    unreadable state file as "never accepted" put the first-run licence wall in
    front of the app on *every* launch.
  * **A brand-new user never sees it.**  Nothing has moved for someone who has
    never seen the old layout.  "Has this user been here before?" is answered by
    the licence acceptance file, which every returning user has.
  * **It is shown once per VERSION**, so the next release can reuse it.
"""
import io
import json
import os

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


@pytest.fixture()
def state(tmp_path, monkeypatch):
    """Point the licence and What's New state files at a temp dir."""
    lic = tmp_path / 'license_accepted.json'
    wn = tmp_path / 'whats_new.json'
    monkeypatch.setattr(entry, '_license_state_file', lambda: str(lic))
    monkeypatch.setattr(entry, '_whats_new_state_file', lambda: str(wn))
    return {'lic': lic, 'wn': wn}


def _returning_user(state):
    state['lic'].write_text(json.dumps({'accepted': True}), encoding='utf-8')


def _status(client):
    r = client.get('/api/whats_new_status',
                   headers={'X-App-Token': entry.APP_TOKEN})
    assert r.status_code == 200
    return r.get_json()


def _ack(client):
    return client.post('/api/whats_new_ack',
                       headers={'X-App-Token': entry.APP_TOKEN})


# ── who sees it ─────────────────────────────────────────────────────────────

def test_a_returning_user_sees_it_once(client, state):
    _returning_user(state)
    assert _status(client)['show'] is True, 'a returning user should be told'
    assert _ack(client).status_code == 200
    assert _status(client)['show'] is False, 'and only once'


def test_a_brand_new_user_never_sees_it(client, state):
    """No licence record means this user has never run an older build, so
    nothing has moved for them."""
    assert not state['lic'].exists()
    assert _status(client)['show'] is False


def test_it_reappears_for_a_new_version(client, state):
    _returning_user(state)
    state['wn'].write_text(json.dumps({'shown_for': '3.0'}), encoding='utf-8')
    body = _status(client)
    assert body['show'] is True, 'a different version must be announced again'
    assert body['version'] and body['version'] != '3.0'


def test_acknowledging_records_the_current_version(client, state):
    _returning_user(state)
    version = _status(client)['version']
    _ack(client)
    saved = json.loads(state['wn'].read_text(encoding='utf-8'))
    assert saved['shown_for'] == version


# ── it fails closed ─────────────────────────────────────────────────────────

def test_an_unreadable_state_file_does_not_show_it(client, state, monkeypatch):
    """A status the app cannot read is not evidence the user has never seen it.
    The licence dialog once made the opposite assumption and appeared on every
    single launch."""
    _returning_user(state)

    def boom():
        raise OSError('state dir unavailable')

    monkeypatch.setattr(entry, '_whats_new_state_file', boom)
    assert _status(client)['show'] is False


def test_garbage_in_the_state_file_does_not_show_it_twice(client, state):
    _returning_user(state)
    state['wn'].write_text('{not json', encoding='utf-8')
    # Unreadable content is handled, and the answer is the safe one.
    assert _status(client)['show'] is False


def test_a_failed_acknowledgement_is_reported(client, state, monkeypatch):
    _returning_user(state)
    monkeypatch.setattr(entry, '_whats_new_state_file',
                        lambda: os.path.join(str(state['wn']), 'nope', 'x.json'))
    r = _ack(client)
    assert r.status_code == 500
    assert 'error' in (r.get_json() or {})



def test_the_suite_never_touches_the_real_user_folder():
    """The tests used to write crash-recovery snapshots (and the log, and
    recent files) into the REAL %LOCALAPPDATA%\PatchworkPDF, so the next real
    launch opened on a Recover dialog full of test fixtures."""
    from platform_io import platform_paths
    iso = os.environ.get('PATCHWORK_TEST_STATE_DIR')
    assert iso, '_bootstrap did not isolate the user state dir'
    assert os.path.abspath(platform_paths.user_data_dir()).startswith(os.path.abspath(iso))

if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
