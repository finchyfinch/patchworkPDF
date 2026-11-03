"""Regression tests for PDF encryption round-trips.

Covers the save-time encrypt path (_encrypt_pdf_bytes) and the open-time
password handling (_open_encrypted_reader / _get_decrypted_pdf_bytes) that gate
whether a protected document opens, opens read-only, or prompts for a password.
"""
import io

import _bootstrap  # noqa: F401  (path setup)

import pypdf
from pdf.pdf_core import (
    _encrypt_pdf_bytes, _open_encrypted_reader, _get_decrypted_pdf_bytes,
    _create_blank_page_bytes,
)


def _plain_pdf():
    return _create_blank_page_bytes(200.0, 200.0)


def test_no_password_returns_original_bytes():
    plain = _plain_pdf()
    assert _encrypt_pdf_bytes(plain, '') is plain      # empty pw → untouched


def test_encrypt_then_open_with_correct_password():
    enc = _encrypt_pdf_bytes(_plain_pdf(), 'secret')
    reader = pypdf.PdfReader(io.BytesIO(enc))
    assert reader.is_encrypted
    opened, restricted, _is_open_pw = _open_encrypted_reader(reader, 'secret')
    assert opened                                       # correct pw opens it
    assert not restricted                               # owner==user pw → full rights


def test_wrong_password_is_rejected():
    enc = _encrypt_pdf_bytes(_plain_pdf(), 'secret')
    reader = pypdf.PdfReader(io.BytesIO(enc))
    opened, _restricted, _is_open_pw = _open_encrypted_reader(reader, 'nope')
    assert not opened                                   # caller must prompt again


def test_decrypted_bytes_are_readable_and_unencrypted():
    enc = _encrypt_pdf_bytes(_plain_pdf(), 'secret')
    reader = pypdf.PdfReader(io.BytesIO(enc))
    reader.decrypt('secret')
    dec = _get_decrypted_pdf_bytes(reader)
    plain_reader = pypdf.PdfReader(io.BytesIO(dec))
    assert not plain_reader.is_encrypted
    assert len(plain_reader.pages) == 1


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
