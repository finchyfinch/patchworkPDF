"""i18n_py.py — server-side translation for the handful of user-facing strings
that Python owns (startup splash, the "already running" dialog, native
file-dialog filter labels).

It reads the SAME locales/<lang>.json catalog the browser uses, so there is one
source of truth. The active language is persisted to a small file in the
per-user app-data folder (written by the frontend via /api/set_lang) so that
even pre-browser code — the splash and the single-instance dialog — can pick it
up on the next launch.

Usage:
    from i18n import i18n_py
    i18n_py.pt('splash.loading_components')          # active language
    i18n_py.pt('filetype.pdf_files')                 # ...
    i18n_py.set_lang('fr')                           # persist a choice
"""
from __future__ import annotations

import functools
import json
import os
import sys

# Locate locales/ next to the exe when frozen, else climb from
# <root>/src/backend/i18n/i18n_py.py to the project <root>.
if getattr(sys, 'frozen', False):
    from platform_io import platform_paths as _pp
    _APP_DIR = _pp.frozen_data_dir()
else:
    _APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
_LOCALES = os.path.join(_APP_DIR, 'locales')

SUPPORTED = ('en', 'es', 'fr', 'de', 'it')
FALLBACK = 'en'


def _settings_dir() -> str:
    from platform_io import platform_paths
    return platform_paths.user_data_dir()


def _lang_file() -> str:
    return os.path.join(_settings_dir(), 'lang.txt')


def get_lang() -> str:
    """The persisted UI language, or English if none/invalid."""
    try:
        with open(_lang_file(), encoding='utf-8') as f:
            v = f.read().strip()
        if v in SUPPORTED:
            return v
    except OSError:
        pass
    return FALLBACK


def saved_lang() -> str | None:
    """The persisted UI language, or None when nothing valid has been saved —
    so a first run still follows the OS language rather than forcing English."""
    try:
        with open(_lang_file(), encoding='utf-8') as f:
            v = f.read().strip()
        return v if v in SUPPORTED else None
    except OSError:
        return None


def os_preferred_languages() -> list[str]:
    """The OS's own ordered UI-language list, where we can read it reliably.

    macOS only, for now: a WKWebView's navigator.language is not guaranteed to
    be the user's first language (3.4 opened in German on an English Mac), so
    the first-run default is taken from NSLocale instead.  Elsewhere returns
    [] and the page keeps using navigator.language, as before."""
    if sys.platform != 'darwin':
        return []
    try:
        from Foundation import NSLocale          # pyobjc, bundled for pywebview
        return [str(x) for x in NSLocale.preferredLanguages()]
    except Exception:
        return []


def os_lang() -> str | None:
    """First SUPPORTED language in the OS list, or English if the list was
    readable but has none of ours, or None when the list is unavailable."""
    langs = os_preferred_languages()
    if not langs:
        return None
    for tag in langs:
        code = tag[:2].lower()
        if code in SUPPORTED:
            return code
    return FALLBACK


def set_lang(lang: str) -> None:
    """Persist the chosen language so pre-browser code can read it next launch."""
    if lang not in SUPPORTED:
        lang = FALLBACK
    try:
        with open(_lang_file(), 'w', encoding='utf-8') as f:
            f.write(lang)
    except OSError:
        pass


@functools.lru_cache(maxsize=None)
def _catalog(lang: str) -> dict:
    try:
        with open(os.path.join(_LOCALES, lang + '.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def pt(key: str, lang: str | None = None, **params) -> str:
    """Translate `key` into `lang` (default: the persisted language), falling
    back to English then to the key itself. Substitutes {name} placeholders."""
    lang = lang if lang in SUPPORTED else (lang or get_lang())
    s = _catalog(lang).get(key)
    if s is None:
        s = _catalog(FALLBACK).get(key, key)
    for k, v in params.items():
        s = s.replace('{' + k + '}', str(v))
    return s
