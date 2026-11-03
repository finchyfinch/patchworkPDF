"""Regression tests for the /api/ origin + token guard.

The bug these come from: the first-run licence dialog reappeared on every
launch, even though acceptance was recorded on disk.

`Referrer-Policy: no-referrer` (added in 2.17) strips the Referer, and browsers
send no Origin on a same-origin GET — so _check_origin() saw neither header and
answered 403 to every GET /api/ route the app's own page called.  Two routes are
GET and not auth-exempt: /api/license_status (hence the dialog, because the page
read the error body as "never accepted") and /api/update_check (so update
notifications were silently dead for the same three releases).

The guard now lets a header-less request through to the token check that always
follows it — the token is what actually protects these routes.  What must stay
true is that a request from somewhere else is still refused, which is what the
bulk of these pin.
"""
import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry


@pytest.fixture()
def client():
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        yield c


TOKEN = lambda: entry.APP_TOKEN          # noqa: E731  (read at call time)

OURS = 'http://127.0.0.1:49371'
THEM = 'https://evil.example.com'


# ── the regression: our own page's GETs are answered ─────────────────────────

def test_same_origin_get_with_no_origin_or_referer_is_served():
    """Exactly what the app's page sends: a token, and neither header."""
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        r = c.get('/api/license_status', headers={'X-App-Token': TOKEN()})
    assert r.status_code == 200, (
        'the page sends no Origin on a same-origin GET and no Referer under '
        'Referrer-Policy: no-referrer — refusing that breaks the app itself')
    assert 'accepted' in r.get_json()


def test_license_status_reports_the_recorded_acceptance(client, tmp_path, monkeypatch):
    state = tmp_path / 'license_accepted.json'
    state.write_text('{"accepted": true, "version": "2.20"}', encoding='utf-8')
    monkeypatch.setattr(entry, '_license_state_file', lambda: str(state))

    r = client.get('/api/license_status', headers={'X-App-Token': TOKEN()})
    assert r.status_code == 200
    assert r.get_json()['accepted'] is True, 'an accepted licence must not re-prompt'


def test_update_check_route_is_reachable_too(client):
    # The other GET route the same 403 silently killed.  Only the guard is under
    # test here — the check itself fails soft and may report anything.
    r = client.get('/api/update_check', headers={'X-App-Token': TOKEN()})
    assert r.status_code != 403


# ── everything else is still refused ─────────────────────────────────────────

def test_a_missing_token_is_still_refused(client):
    r = client.get('/api/license_status')
    assert r.status_code == 403, 'the token is what guards these routes'


def test_a_wrong_token_is_still_refused(client):
    r = client.get('/api/license_status', headers={'X-App-Token': 'not-the-token'})
    assert r.status_code == 403


def test_a_foreign_origin_is_refused_even_with_a_valid_token(client):
    r = client.get('/api/license_status',
                   headers={'X-App-Token': TOKEN(), 'Origin': THEM})
    assert r.status_code == 403, 'a header that IS present must be one of ours'


def test_a_foreign_referer_is_refused(client):
    r = client.get('/api/license_status',
                   headers={'X-App-Token': TOKEN(), 'Referer': THEM + '/page'})
    assert r.status_code == 403


def test_our_own_origin_with_a_token_is_served(client):
    r = client.post('/api/recovery/list', json={},
                    headers={'X-App-Token': TOKEN(), 'Origin': OURS})
    assert r.status_code == 200, 'a POST carries Origin and must keep working'


def test_a_foreign_origin_cannot_reach_a_write_route(client):
    r = client.post('/api/accept_license',
                    headers={'X-App-Token': TOKEN(), 'Origin': THEM})
    assert r.status_code == 403


# ── the guard's own contract ─────────────────────────────────────────────────

def test_ping_stays_open_and_the_rest_do_not(client):
    # /api/ping must answer before a caller can hold a token (that is how a
    # second launch identifies us); nothing else gets that.
    assert client.get('/api/ping').status_code == 200
    assert client.get('/api/license_status').status_code == 403


def test_api_errors_answer_in_json_not_html(client):
    r = client.get('/api/license_status', headers={'X-App-Token': 'wrong'})
    assert r.status_code == 403
    assert r.mimetype == 'application/json', (
        'the front end parses every reply with res.json()')
    assert 'error' in r.get_json()


def test_an_error_body_carries_no_accepted_field(client):
    """Why the page must fail closed: this body is what it used to read as
    'the user never accepted the licence'."""
    r = client.get('/api/license_status', headers={'X-App-Token': 'wrong'})
    assert 'accepted' not in r.get_json()


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
