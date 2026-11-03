"""Regression tests for the memory/session and form-button changes.

Covers:
  * _annot_trim_undo — the byte + count bound on annotation undo history that
    stops a document of large scanned pages growing the stack without limit.
  * form button helpers — the export-state derivation used to give checkboxes and
    radios real /AP appearance states and mutually-exclusive radio groups.
"""
import _bootstrap  # noqa: F401  (path setup)

from routes.viewer import _annot_trim_undo, _ANNOT_HISTORY_CAP, _ANNOT_HISTORY_BYTES
from pdf.form_utils import _sanitize_state_name, _button_on_state, _button_is_on


def test_trim_respects_count_cap():
    stack = [('p', b'x') for _ in range(_ANNOT_HISTORY_CAP + 5)]
    _annot_trim_undo(stack)
    assert len(stack) == _ANNOT_HISTORY_CAP


def test_trim_respects_byte_budget_but_keeps_one():
    big = b'\x00' * (_ANNOT_HISTORY_BYTES // 2 + 1)   # two of these exceed budget
    stack = [('p', big), ('p', big), ('p', big)]
    _annot_trim_undo(stack)
    assert len(stack) >= 1
    assert sum(len(b) for _pid, b in stack) <= _ANNOT_HISTORY_BYTES or len(stack) == 1
    # The single most-recent entry is never dropped, even if it alone exceeds it.
    huge = b'\x00' * (_ANNOT_HISTORY_BYTES + 10)
    solo = [('p', huge)]
    _annot_trim_undo(solo)
    assert len(solo) == 1


def test_sanitize_state_name():
    assert _sanitize_state_name('Option 1') == 'Option_1'
    assert _sanitize_state_name('a/b(c)') == 'a_b_c'
    assert _sanitize_state_name('') == 'On'
    assert _sanitize_state_name(None) == 'On'


def test_button_on_state():
    assert _button_on_state({'type': 'checkbox'}) == 'Yes'
    assert _button_on_state({'type': 'radio', 'options': ['Yes please', 'No']}) == 'Yes_please'
    assert _button_on_state({'type': 'radio', 'options': []}) == 'Yes'


def test_button_is_on():
    assert _button_is_on({'value': 'on'}, 'Yes')
    assert _button_is_on({'value': True}, 'Yes')
    assert not _button_is_on({'value': ''}, 'Yes')
    # Radio: selected value matches this button's export state.
    assert _button_is_on({'type': 'radio', 'value': 'Option 1'}, 'Option_1')
    assert not _button_is_on({'type': 'radio', 'value': 'Option 2'}, 'Option_1')


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
