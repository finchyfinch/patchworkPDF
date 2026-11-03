"""Regression tests for "the app always reverts to dark mode after a restart".

The theme lived only in localStorage, and the page read its language back only
from there.  localStorage belongs to the page's origin — which includes the port
picked at launch (49371, then 49372-49375, then any free port) — and it sits in
WebView2's data folder under the roaming %APPDATA%.  On a machine where the port
moves between launches, or the roaming profile is reset, the choice was simply
gone: light mode came back dark on every start (seen on a work PC, never at
home, where the port is always 49371).

The per-user data dir is now the source of truth, handed to the page as
window.PW_PREFS.  What must stay true:

  * a saved theme comes back in the page on the next load, whatever the port;
  * the preferences are defined BEFORE i18n.js runs — it picks its language the
    moment it loads in <head>, and then writes that pick back to lang.txt;
  * nothing saved means null, so a first run still follows the page defaults;
  * bad input and a damaged file never break the page.
"""
import json
import re

import _bootstrap  # noqa: F401  (path setup)

import pytest

import entry
from i18n import i18n_py


OURS = 'http://127.0.0.1:49371'


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, '_ui_prefs_file', lambda: str(tmp_path / 'ui_prefs.json'))
    monkeypatch.setattr(i18n_py, '_lang_file', lambda: str(tmp_path / 'lang.txt'))
    entry.app.config['TESTING'] = True
    with entry.app.test_client() as c:
        c.prefs_dir = tmp_path
        yield c


def _page_prefs(client):
    html = client.get('/').get_data(as_text=True)
    m = re.search(r'window\.PW_PREFS = (\{.*?\});', html)
    assert m, 'the page must carry window.PW_PREFS'
    return json.loads(m.group(1)), html


def _save_theme(client, theme):
    return client.post('/api/ui_prefs', json={'theme': theme},
                       headers={'X-App-Token': entry.APP_TOKEN, 'Origin': OURS})


def test_a_saved_theme_comes_back_on_the_next_load(client):
    assert _save_theme(client, 'light').status_code == 200
    prefs, _ = _page_prefs(client)
    assert prefs['theme'] == 'light', (
        'the theme must come from the server copy — localStorage forgets it '
        'whenever the app starts on a different port')
    _save_theme(client, 'dark')
    assert _page_prefs(client)[0]['theme'] == 'dark'


def test_prefs_are_defined_before_i18n_js_runs(client):
    _, html = _page_prefs(client)
    assert html.index('window.PW_PREFS') < html.index('/i18n.js'), (
        'i18n.js picks the language as soon as it loads in <head>; prefs '
        'injected after it (e.g. at </head>, with the token) arrive too late')


def test_nothing_saved_means_null_so_page_defaults_apply(client):
    prefs, _ = _page_prefs(client)
    assert prefs == {'theme': None, 'lang': None}


def test_the_saved_language_is_handed_to_the_page(client):
    i18n_py.set_lang('de')
    assert _page_prefs(client)[0]['lang'] == 'de'


@pytest.mark.parametrize('bad', ['blue', '', None, '<script>'])
def test_anything_but_light_or_dark_is_refused(client, bad):
    assert _save_theme(client, bad).status_code == 400
    assert _page_prefs(client)[0]['theme'] is None


def test_a_damaged_prefs_file_falls_back_rather_than_breaking_the_page(client):
    (client.prefs_dir / 'ui_prefs.json').write_text('{not json', encoding='utf-8')
    assert _page_prefs(client)[0]['theme'] is None
    assert _save_theme(client, 'light').status_code == 200, 'and saving repairs it'
    assert _page_prefs(client)[0]['theme'] == 'light'


def test_saving_needs_the_app_token(client):
    r = client.post('/api/ui_prefs', json={'theme': 'light'},
                    headers={'X-App-Token': 'wrong', 'Origin': OURS})
    assert r.status_code == 403
