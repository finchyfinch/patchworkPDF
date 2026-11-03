r"""Regression tests for pointing Tesseract at the bundled tessdata packs.

The editor OCRs any page it can't extract text from, so a document made of
scans — or of text converted to vector outlines — goes straight through this
path.  When it breaks, /api/editor/open returns a 500 and the front end reports
"Failed to load: Unexpected token '<'", because it got Flask's HTML error page
where it expected JSON.

The break: the directory used to be passed as ``--tessdata-dir "<path>"`` inside
pytesseract's ``config`` string.  pytesseract splits that with
``shlex.split(config, posix=not_windows)``, and on Windows (posix=False) shlex
does NOT strip quotes — tesseract received a path with literal quote characters:

    Error opening data file "C:\...\assets\tessdata"/eng.traineddata

Dropping the quotes only moves the failure to any path containing a space, which
every installed build has (``C:\Program Files\...``).  The path now travels in
TESSDATA_PREFIX, which is immune to argument splitting.
"""
import os
import shlex

import _bootstrap  # noqa: F401  (path setup)

from pdf import ocr_utils
from pdf.ocr_utils import _apply_tessdata_env, ocr_lang_config


def _restore_env(value):
    if value is None:
        os.environ.pop('TESSDATA_PREFIX', None)
    else:
        os.environ['TESSDATA_PREFIX'] = value


# ── the defect ───────────────────────────────────────────────────────────────

def test_tessdata_dir_never_travels_in_the_config_string():
    saved = os.environ.get('TESSDATA_PREFIX')
    try:
        _lang, cfg = ocr_lang_config()
        assert '--tessdata-dir' not in cfg, (
            'the tessdata path must not go through pytesseract\'s config string - '
            'it cannot survive Windows argument splitting')
        assert '"' not in cfg, 'a quote in the config string reaches tesseract literally'
    finally:
        _restore_env(saved)


def test_bundled_packs_are_advertised_through_the_environment():
    saved = os.environ.get('TESSDATA_PREFIX')
    try:
        os.environ.pop('TESSDATA_PREFIX', None)
        lang, _cfg = ocr_lang_config()
        bundled = ocr_utils._bundled_tessdata_dir()
        if not bundled:
            return                      # dev checkout without the packs
        assert os.environ.get('TESSDATA_PREFIX'), \
            'the bundled packs must be pointed at somehow'
        assert 'eng' in lang
    finally:
        _restore_env(saved)


def test_env_var_is_reasserted_on_the_cached_path():
    # ocr_lang_config() caches per UI language; the early return must still set
    # the variable, or the second and later OCR runs in a process lose the packs.
    saved = os.environ.get('TESSDATA_PREFIX')
    try:
        ocr_lang_config()                       # populate the cache
        if not ocr_utils._bundled_tessdata_dir():
            return
        os.environ.pop('TESSDATA_PREFIX', None)
        ocr_lang_config()                       # cache hit
        assert os.environ.get('TESSDATA_PREFIX'), \
            'the cached path dropped TESSDATA_PREFIX'
    finally:
        _restore_env(saved)


def test_prefix_matches_the_tesseract_major_version():
    # Tesseract 5 wants TESSDATA_PREFIX to BE the tessdata dir; 4 wants the
    # parent and appends 'tessdata' itself.
    saved = os.environ.get('TESSDATA_PREFIX')
    saved_major = ocr_utils._TESS_MAJOR
    d = os.path.join('C:' + os.sep, 'app', 'assets', 'tessdata')
    try:
        ocr_utils._TESS_MAJOR = 5
        _apply_tessdata_env(d)
        assert os.environ['TESSDATA_PREFIX'] == d

        ocr_utils._TESS_MAJOR = 4
        _apply_tessdata_env(d)
        assert os.environ['TESSDATA_PREFIX'] == os.path.dirname(d)

        # Nothing bundled -> leave whatever the system already has alone.
        os.environ['TESSDATA_PREFIX'] = 'untouched'
        _apply_tessdata_env(None)
        assert os.environ['TESSDATA_PREFIX'] == 'untouched'
    finally:
        ocr_utils._TESS_MAJOR = saved_major
        _restore_env(saved)


# ── why the old form could not work (guards against "simplifying" it back) ───

def test_a_quoted_path_does_not_survive_pytesseract_arg_splitting():
    # This is pytesseract's own splitting, with the Windows flag.
    cfg = ' --tessdata-dir "%s"' % (r'C:\Program Files\PatchworkPDF\assets\tessdata')
    token = shlex.split('--psm 6' + cfg, posix=False)[-1]
    assert token.startswith('"') and token.endswith('"'), \
        'shlex(posix=False) is expected to keep the quotes - that was the bug'


def test_an_unquoted_path_with_spaces_splits_apart():
    cfg = ' --tessdata-dir %s' % (r'C:\Program Files\PatchworkPDF\assets\tessdata')
    tokens = shlex.split('--psm 6' + cfg, posix=False)
    assert tokens[-1] != r'C:\Program Files\PatchworkPDF\assets\tessdata', \
        'unquoting is not a fix either - the path breaks at its spaces'


# ── end to end, when Tesseract is actually installed ─────────────────────────

def test_ocr_reads_text_with_the_bundled_packs():
    ocr_utils.init_ocr()
    if not ocr_utils.is_ocr_available():
        try:
            import pytest
            pytest.skip('Tesseract not installed')
        except ImportError:
            print('SKIP  Tesseract not installed')
        return
    if not ocr_utils._bundled_tessdata_dir():
        return

    from PIL import Image, ImageDraw
    import pytesseract

    saved = os.environ.get('TESSDATA_PREFIX')
    try:
        img = Image.new('L', (320, 80), 255)
        ImageDraw.Draw(img).text((10, 25), 'HELLO WORLD', fill=0)
        lang, cfg = ocr_lang_config()          # sets TESSDATA_PREFIX
        text = pytesseract.image_to_string(img, lang=lang, config='--psm 6' + cfg)
        assert 'HELLO' in text.upper().replace(' ', ''), \
            f'OCR produced no usable text: {text!r}'
    finally:
        _restore_env(saved)


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
