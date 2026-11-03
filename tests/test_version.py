"""Regression tests for the update-checker version logic.

Guards the comparison that decides whether a newer release is offered — the
zero-padded-minor case ("2.9" vs "2.11") that naive float comparison gets wrong,
plus changelog note extraction.
"""
import _bootstrap  # noqa: F401  (path setup)

from services.check_for_updates import (
    _version_key, _is_newer, _notes_for_version,
)


def test_version_key_parses_and_ignores_junk():
    assert _version_key('2.16') == (2, 16)
    assert _version_key(' 2.09 ') == (2, 9)
    assert _version_key('v2.16.1') == (2, 16, 1)
    assert _version_key('2.x') == (2,)


def test_is_newer_handles_zero_padded_minors():
    # The bug naive float comparison hits: 2.11 IS newer than 2.9 / 2.09.
    assert _is_newer('2.11', '2.9')
    assert _is_newer('2.11', '2.09')
    assert _is_newer('2.16', '2.15')
    assert not _is_newer('2.9', '2.11')
    assert not _is_newer('2.16', '2.16')      # same version is not "newer"
    assert not _is_newer('2.15', '2.16')


def test_is_newer_across_major():
    assert _is_newer('3.0', '2.99')
    assert not _is_newer('2.99', '3.0')


def test_notes_for_version_extracts_only_that_release():
    from docx import Document
    doc = Document()
    for line in ['patchworkPDF Change Log', '',
                 '2.16', 'Fixed redaction', 'Bounded memory', '',
                 '2.15', 'Old change']:
        doc.add_paragraph(line)
    assert _notes_for_version(doc, '2.16') == ['Fixed redaction', 'Bounded memory']
    assert _notes_for_version(doc, '2.15') == ['Old change']
    assert _notes_for_version(doc, '9.99') == []   # absent version → no notes


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
