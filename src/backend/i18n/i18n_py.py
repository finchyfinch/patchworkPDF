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
    _APP_DIR = os.path.dirname(sys.executable)
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
