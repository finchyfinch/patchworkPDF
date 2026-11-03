r"""Regression tests for opening and saving files on deep Windows paths.

Windows refuses paths over 260 characters unless both the process and the
machine opt in, and ``LongPathsEnabled`` is off by default — so on a work file
server (``\\fileserver\Projects\<client>\<job>\Accounts\Invoices\...``) a
document can sit well past the limit.  The failure is silent: ``os.path.isfile``
answers False, so a file the user is looking at reads as "not there".  Launching
by double-click then opened an empty window with no message at all, while the
same file opened from a short local path was fine.

``platform_paths.fs_path`` maps such a path to the extended-length form for
filesystem calls only — the plain path stays in the session, the recents list
and the UI.  These tests cover the mapping and the entry points that lost the
file.
"""
import io
import os
import shutil
import sys

import _bootstrap  # noqa: F401  (path setup)

import pikepdf

from platform_io.platform_paths import fs_path
from bootstrap.single_instance import find_initial_file
from pdf.pdf_core import _ingest_document, get_save_dir

WINDOWS = sys.platform == 'win32'
_EXT = '\\\\?\\'          # extended-length prefix


def _skip(reason):
    try:
        import pytest
        pytest.skip(reason)
    except ImportError:
        print('SKIP  ' + reason)
    return True


def _deep_dir(tmp_root, depth=12):
    """A directory whose path is comfortably past 260 characters."""
    p = os.path.abspath(tmp_root)
    for i in range(depth):
        p = os.path.join(p, 'Project Documentation Folder %02d' % i)
    os.makedirs(_EXT + p if WINDOWS else p, exist_ok=True)
    return p


def _one_page_pdf():
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _rmtree(path):
    shutil.rmtree((_EXT + path) if WINDOWS else path, ignore_errors=True)


# ── the mapping ──────────────────────────────────────────────────────────────

def test_fs_path_leaves_ordinary_paths_alone():
    short = r'C:\Users\someone\Documents\invoice.pdf' if WINDOWS else '/home/x/invoice.pdf'
    assert fs_path(short) == short
    assert fs_path('') == ''
    assert fs_path(None) is None


def test_fs_path_prefixes_a_long_local_path():
    if not WINDOWS:
        return _skip('Windows-only path semantics')
    long_path = 'C:\\dir' + '\\subdirectory' * 25 + '\\invoice.pdf'
    assert len(long_path) > 260
    mapped = fs_path(long_path)
    assert mapped.startswith('\\\\?\\C:\\'), mapped
    assert mapped.endswith('invoice.pdf')


def test_fs_path_prefixes_a_long_unc_path():
    if not WINDOWS:
        return _skip('Windows-only path semantics')
    unc = '\\\\fileserver\\Projects' + '\\subdirectory' * 25 + '\\invoice.pdf'
    assert len(unc) > 260
    # \\server\share → \\?\UNC\server\share (not \\?\\\server\share)
    assert fs_path(unc).startswith('\\\\?\\UNC\\fileserver\\Projects'), fs_path(unc)


def test_fs_path_does_not_double_prefix():
    if not WINDOWS:
        return _skip('Windows-only path semantics')
    already = '\\\\?\\C:\\dir' + '\\subdirectory' * 25 + '\\invoice.pdf'
    assert fs_path(already) == already


# ── the entry points that lost the file ──────────────────────────────────────

def test_document_on_a_deep_path_is_found_at_launch(tmp_path=None):
    if not WINDOWS:
        return _skip('Windows-only path semantics')
    root = os.path.abspath('_lp_launch')
    try:
        target = os.path.join(_deep_dir(root), 'error_opening_doc.pdf')
        assert len(target) > 260
        with open(_EXT + target, 'wb') as f:
            f.write(_one_page_pdf())

        # Plain path: what Explorer hands the app on double-click.
        assert not os.path.isfile(target), 'precondition: Windows hides it'
        assert find_initial_file(['patchworkpdf.exe', target]) == target, \
            'launching with a deep path must still find the document'
    finally:
        _rmtree(root)


def test_document_on_a_deep_path_opens(tmp_path=None):
    if not WINDOWS:
        return _skip('Windows-only path semantics')
    root = os.path.abspath('_lp_open')
    try:
        target = os.path.join(_deep_dir(root), 'error_opening_doc.pdf')
        with open(_EXT + target, 'wb') as f:
            f.write(_one_page_pdf())

        session = {'pages': [], 'first_file_path': None}
        pages, err, pw = _ingest_document(session, target, os.path.basename(target))
        assert err is None, f'deep path should open, got: {err}'
        assert pw is None
        assert len(pages) == 1
    finally:
        _rmtree(root)


def test_save_dialog_still_defaults_to_a_deep_source_folder():
    if not WINDOWS:
        return _skip('Windows-only path semantics')
    root = os.path.abspath('_lp_save')
    try:
        folder = _deep_dir(root)
        target = os.path.join(folder, 'error_opening_doc.pdf')
        with open(_EXT + target, 'wb') as f:
            f.write(_one_page_pdf())

        # The plain path is what goes to the dialog, so the prefix must not leak.
        got = get_save_dir({'first_file_path': target, 'pages': []})
        assert got == folder, f'save dialog should open in the source folder, got {got!r}'
        assert not got.startswith(_EXT), 'the extended-length prefix must not reach the UI'

        # And writing there works.
        out = os.path.join(folder, 'saved.pdf')
        with open(fs_path(out), 'wb') as f:
            f.write(_one_page_pdf())
        assert os.path.isfile(fs_path(out))
    finally:
        _rmtree(root)


def test_unreadable_file_is_reported_not_raised():
    # A file we cannot read (locked by another app, an un-hydrated cloud
    # placeholder, permissions) must come back as an open error.  Raising here
    # turns into a 500 the front end can only describe as a parse failure.
    root = os.path.abspath('_lp_unreadable')
    blocker = os.path.join(root, 'invoice.pdf')
    try:
        os.makedirs(blocker, exist_ok=True)      # a *directory* named like a PDF
        session = {'pages': [], 'first_file_path': None}
        pages, err, pw = _ingest_document(session, blocker, 'invoice.pdf')
        assert pages == []
        assert err and 'invoice.pdf' in err, f'expected a reported error, got {err!r}'
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    import sys as _sys
    _sys.exit(_bootstrap.run_module_tests(dict(globals())))
