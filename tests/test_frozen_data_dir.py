"""Frozen builds find their shipped files on every platform.

A macOS .app keeps them in Contents/Resources; Contents/MacOS has only the
executable and symlinks for sub-folders, so LICENSE.txt and version.json were
"missing" on macOS 3.4 (the About/first-run box said it could not load the
license text).  Every site that used dirname(sys.executable) now goes through
platform_paths.frozen_data_dir().
"""
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src' / 'backend'))

from platform_io import platform_paths  # noqa: E402


def test_macos_bundle_uses_resources(tmp_path):
    contents = tmp_path / 'PatchworkPDF.app' / 'Contents'
    (contents / 'MacOS').mkdir(parents=True)
    (contents / 'Resources').mkdir()
    exe = contents / 'MacOS' / 'PatchworkPDF'
    exe.write_bytes(b'')
    assert platform_paths.frozen_data_dir(str(exe)) == str(contents / 'Resources')


def test_flat_layout_uses_exe_dir(tmp_path):
    exe = tmp_path / 'PatchworkPDF.exe'
    exe.write_bytes(b'')
    assert platform_paths.frozen_data_dir(str(exe)) == str(tmp_path)


def test_no_site_resolves_next_to_the_executable():
    """A new dirname(sys.executable) would bring the macOS bug back."""
    offenders = []
    for p in (ROOT / 'src' / 'backend').rglob('*.py'):
        if 'dirname(sys.executable)' in p.read_text(encoding='utf-8', errors='ignore'):
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, offenders


def test_os_lang_picks_first_supported(monkeypatch):
    from i18n import i18n_py
    monkeypatch.setattr(i18n_py, 'os_preferred_languages', lambda: ['ja-JP', 'en-GB', 'de-DE'])
    assert i18n_py.os_lang() == 'en'
    monkeypatch.setattr(i18n_py, 'os_preferred_languages', lambda: ['ja-JP'])
    assert i18n_py.os_lang() == 'en'
    monkeypatch.setattr(i18n_py, 'os_preferred_languages', lambda: [])
    assert i18n_py.os_lang() is None
