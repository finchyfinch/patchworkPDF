"""Fitting replacement text into the space the original run occupied.

A PDF does not reflow.  Text injected over an edited run cannot push its
neighbours along, so a replacement longer than the run it replaces simply draws
on top of the next word — "Invoice No: 106688" edited to "Statement" rendered as
"StatementNo: 106688" on a real invoice.

The rules, in order of how much they cost the reader:

  1. **If it fits, change nothing.**  The overwhelmingly common edit must come
     out byte-for-byte what it did before any of this existed.
  2. **If the rest of the line is FREE, change nothing.**  Only a measured
     neighbour constrains the text.  Inventing a page-edge limit would mean
     guessing at a coordinate frame to solve a problem that is not there.
  3. **Condense before shrinking.**  Tz keeps the baseline and the cap height,
     so a few percent is invisible, while a smaller font size sits obviously
     wrong beside its unchanged neighbours.
  4. **Stop at the floors and SAY SO.**  A word replaced by a sentence cannot be
     made to fit and stay readable; 3pt text loses the content as surely as an
     overlap does.  So it fits as far as it can and reports that it could not
     finish the job — the one thing the caller cannot work out for itself.
"""
import _bootstrap  # noqa: F401  (path setup)

import pytest

from platform_io import font_assets as _fa
from routes.editor import (_fit_to_available, _same_line_neighbour_x,
                           _FIT_MIN_TZ, _FIT_MIN_SCALE)


def _run(id_, x, x2, y=100.0, y2=110.0, **kw):
    d = {'id': id_, 'type': 'text', 'x': x, 'y': y, 'x2': x2, 'y2': y2,
         'text': kw.pop('text', 'w')}
    d.update(kw)
    return d


# ── measuring ───────────────────────────────────────────────────────────────

def test_width_scales_with_size_and_length():
    a = _fa.string_width('Helvetica', 'Invoice', 12)
    assert a > 0
    assert _fa.string_width('Helvetica', 'Invoice', 24) == pytest.approx(a * 2, rel=0.02)
    assert _fa.string_width('Helvetica', 'Statement', 12) > a, \
        'the longer word must measure wider — this is the whole premise'
    assert _fa.string_width('Helvetica', '', 12) == 0


def test_an_unknown_face_still_returns_a_usable_width():
    """The fallback must not be zero — zero reads as "fits" and would silently
    disable the whole mechanism for that font."""
    assert _fa.string_width('NoSuchFontFace', 'Invoice', 12) > 0


# ── finding the obstacle ────────────────────────────────────────────────────

def test_the_nearest_run_to_the_right_on_the_same_line_wins():
    target = _run('a', 60, 95)
    others = [_run('b', 130, 160), _run('c', 99, 120), _run('d', 10, 40)]
    assert _same_line_neighbour_x(target, [target] + others, set()) == 99


def test_a_run_on_another_line_is_not_an_obstacle():
    target = _run('a', 60, 95, y=100, y2=110)
    below  = _run('b', 99, 120, y=80, y2=90)
    assert _same_line_neighbour_x(target, [target, below], set()) is None


def test_a_partly_overlapping_box_still_counts_as_the_same_line():
    """OCR word boxes on one line vary with ascenders and descenders, so the
    test is vertical OVERLAP, not equal baselines."""
    target = _run('a', 60, 95, y=100, y2=110)
    tall   = _run('b', 99, 120, y=98, y2=114)     # a word with a descender
    assert _same_line_neighbour_x(target, [target, tall], set()) == 99


def test_a_run_being_deleted_or_moved_away_is_not_an_obstacle():
    """It will not be there when the replacement is drawn."""
    target = _run('a', 60, 95)
    going  = _run('b', 99, 120)
    assert _same_line_neighbour_x(target, [target, going], {'b'}) is None


def test_a_free_line_end_returns_None_meaning_do_not_constrain():
    target = _run('a', 60, 95)
    assert _same_line_neighbour_x(target, [target], set()) is None


# ── fitting ─────────────────────────────────────────────────────────────────

def test_text_that_already_fits_is_left_completely_alone():
    size, tz, over = _fit_to_available('Hi', 10, 'Helvetica', x=60, limit_x=400)
    assert (size, tz, over) == (10, 100.0, False)


def test_a_slightly_long_replacement_is_condensed_not_shrunk():
    natural = _fa.string_width('Helvetica', 'Statement', 10)
    avail = natural * 0.9          # a gap 10% short of what the text needs
    size, tz, over = _fit_to_available('Statement', 10, 'Helvetica',
                                       x=0, limit_x=avail)
    assert size == 10, 'the font size must not move for a small overrun'
    assert _FIT_MIN_TZ <= tz < 100
    assert not over
    assert natural * tz / 100 <= avail, 'it still does not fit'


def test_a_much_longer_replacement_also_shrinks_and_still_fits():
    text = 'Conditions of settlement'
    natural = _fa.string_width('Helvetica', text, 10)
    avail = natural * 0.5
    size, tz, over = _fit_to_available(text, 10, 'Helvetica',
                                       x=0, limit_x=avail)
    assert tz == _FIT_MIN_TZ, 'condensing should be pushed to its floor first'
    assert size < 10
    assert not over
    rendered = _fa.string_width('Helvetica', text, size) * tz / 100
    assert rendered <= avail + 0.5, f'{rendered} did not fit {avail}'


def test_an_impossible_replacement_stops_at_the_floors_and_reports_it():
    """Readable-but-overlapping beats illegible-but-contained, and either way
    the user has to be told."""
    text = 'Conditions of settlement apply to this invoice in full'
    natural = _fa.string_width('Helvetica', text, 10)
    size, tz, over = _fit_to_available(text, 10, 'Helvetica',
                                       x=0, limit_x=natural * 0.1)
    assert over is True, 'the caller was not told it still overlaps'
    assert tz == _FIT_MIN_TZ
    assert size == pytest.approx(10 * _FIT_MIN_SCALE), \
        'the size floor was breached — the text would be unreadable'


def test_a_sub_point_overrun_is_left_alone():
    """Adjacent runs on a justified line sit a point or two apart and their
    boxes are rounded, so chasing a fraction of a point would condense text for
    an overlap nobody could see."""
    natural = _fa.string_width('Helvetica', 'Statement', 10)
    size, tz, over = _fit_to_available('Statement', 10, 'Helvetica',
                                       x=0, limit_x=natural - 0.5)
    assert (size, tz, over) == (10, 100.0, False)


def test_the_original_s_own_width_is_always_available():
    """Extraction routinely reports a neighbour starting INSIDE the previous
    run's box — adjacent runs, kerned pairs, slack OCR boxes.  Taken at face
    value that condensed replacements SHORTER than what they replaced: a live
    page turned "X" into 80%-scaled text because its neighbour was reported
    5.6pt away while the run itself was wider than that."""
    w = _fa.string_width('Helvetica', 'X', 11)
    size, tz, over = _fit_to_available('X', 11, 'Helvetica',
                                       x=94.1, limit_x=99.7,
                                       min_x2=94.1 + w)
    assert (size, tz, over) == (11, 100.0, False)


def test_but_a_replacement_wider_than_the_original_still_fits_to_the_gap():
    """The clamp is a floor on the room available, not a licence to ignore the
    neighbour once the text grows past the original."""
    natural = _fa.string_width('Helvetica', 'Statement', 10)
    size, tz, over = _fit_to_available('Statement', 10, 'Helvetica',
                                       x=0, limit_x=natural * 0.9,
                                       min_x2=natural * 0.5)
    assert tz < 100, 'the neighbour was ignored'


def test_the_word_space_the_original_left_is_not_eaten():
    """Stopping a hair short of the next word's ink is technically not an
    overlap, but it reads as one: "Invoice No:" edited to "Statement" came out
    "StatementNo:" on a real page.  As much of the original's trailing gap as a
    space in this font occupies has to survive."""
    size = 11.0
    space = _fa.string_width('Helvetica', ' ', size)
    orig_w = _fa.string_width('Helvetica', 'Invoice', size)
    x, x2 = 60.0, 60.0 + orig_w
    limit = x2 + 4.0                      # the original left a real word space
    got_size, tz, over = _fit_to_available('Statement', size, 'Helvetica',
                                           x=x, limit_x=limit, min_x2=x2)
    rendered = _fa.string_width('Helvetica', 'Statement', got_size) * tz / 100
    assert not over
    assert x + rendered <= limit - min(4.0, space) + 0.01,         'the space before the next word was consumed'


def test_a_generous_gap_is_not_all_reserved():
    """Only a word space is protected, not the whole gap — otherwise a run at
    the end of a sentence would be condensed with inches of room beside it."""
    size = 11.0
    orig_w = _fa.string_width('Helvetica', 'Date:', size)
    x, x2 = 60.0, 60.0 + orig_w
    wide = _fit_to_available('Date:xx', size, 'Helvetica',
                             x=x, limit_x=x2 + 40.0, min_x2=x2)
    assert wide == (size, 100.0, False), 'condensed despite ample room'


def test_no_room_at_all_is_reported_rather_than_squashed_to_nothing():
    size, tz, over = _fit_to_available('Anything', 10, 'Helvetica',
                                       x=100, limit_x=100)
    assert (size, tz) == (10, 100.0)
    assert over is True


def test_empty_replacement_text_never_reports_an_overflow():
    assert _fit_to_available('', 10, 'Helvetica', x=100, limit_x=100)[2] is False


if __name__ == '__main__':
    import sys
    sys.exit(_bootstrap.run_module_tests(dict(globals())))
