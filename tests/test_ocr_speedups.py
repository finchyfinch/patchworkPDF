"""OCR speed-ups (v3.4) must not change a single result.

A work laptop took 19.6s for one scanned invoice: most of it was the fixed
cost of LAUNCHING Tesseract (once per pass, then once per white-on-dark word
re-read), which security software inspects every time.  Three changes cut the
launches; each is pinned here to give exactly what the old way gave:

* the light-on-dark re-read runs every word in ONE Tesseract process (a list
  file, one page per word) — with a per-word fallback if that ever fails;
* the psm 6 / psm 11 page passes run concurrently;
* a throwaway OCR warms Tesseract after start-up — and does nothing at all
  when Tesseract is not installed (it is an optional install).
"""
import subprocess
import threading

import _bootstrap  # noqa: F401

import pytest

from routes import editor as _editor
from pdf import ocr_utils as U


@pytest.fixture(scope='module', autouse=True)
def _heavy():
    _editor._heavy_init()


def _band_words():
    """A dark band with light words (an invoice header), plus the word dicts
    a page pass would produce for them."""
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np
    font = ImageFont.truetype(U.os.path.join(U.os.path.dirname(U.os.path.dirname(
        U.os.path.dirname(U.os.path.dirname(U.os.path.abspath(U.__file__))))),
        'assets', 'fonts', 'LiberationSans-Bold.ttf'), 44)
    img = Image.new('L', (1400, 120), 255)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 10, 1400, 110), fill=40)
    words, x = [], 30
    for t in ('Description', 'Quantity', 'Unit', 'Price', 'VAT', 'Amount'):
        bb = d.textbbox((x, 35), t, font=font)
        d.text((x, 35), t, fill=235, font=font)
        words.append({'text': t, 'conf': 90, 'left': bb[0], 'top': bb[1],
                      'width': bb[2] - bb[0], 'height': bb[3] - bb[1]})
        x = bb[2] + 50
    return np.asarray(img), words


def test_batched_reread_matches_one_process_per_word():
    if not U.is_ocr_available():
        pytest.skip('Tesseract not installed')
    gray, words = _band_words()
    lang, cfg = U.ocr_lang_config()
    assert all(U._is_light_on_dark(gray, w) for w in words)
    one_by_one = [U._reread_word(gray, w, lang, cfg) for w in words]
    batched = U._reread_batch([U._reread_image(gray, w) for w in words], lang, cfg)
    assert batched == one_by_one
    assert any(t for t, _ in batched), batched     # it really read something


def test_failed_batch_falls_back_to_word_by_word(monkeypatch):
    if not U.is_ocr_available():
        pytest.skip('Tesseract not installed')
    gray, words = _band_words()
    lang, cfg = U.ocr_lang_config()
    calls = []
    real = U._reread_word
    monkeypatch.setattr(U, '_reread_batch',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(U, '_reread_word',
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    U.reread_light_on_dark(gray, [dict(w) for w in words], lang, cfg)
    assert len(calls) == len(words)


def test_concurrent_passes_match_sequential():
    if not U.is_ocr_available():
        pytest.skip('Tesseract not installed')
    from PIL import Image
    gray, _ = _band_words()
    img = Image.fromarray(255 - gray)
    lang, cfg = U.ocr_lang_config()
    T = U._pytesseract
    seq = (T.image_to_data(img, output_type=T.Output.DICT, lang=lang, config='--psm 6' + cfg),
           T.image_to_data(img, output_type=T.Output.DICT, lang=lang, config='--psm 11' + cfg))
    assert U.run_two_passes(img, lang, cfg) == seq


def test_warm_up_does_nothing_without_tesseract(monkeypatch):
    started = []
    monkeypatch.setattr(U, '_WARMED', False)
    monkeypatch.setattr(U, 'is_ocr_available', lambda: False)
    monkeypatch.setattr(U.threading, 'Thread',
                        lambda *a, **k: started.append(1) or threading.Thread(target=lambda: None))
    U.warm_up_ocr()
    assert not started and U._WARMED is False


def test_hidden_console_survives_overlapping_threads():
    orig = subprocess.Popen
    barrier = threading.Barrier(4)

    def worker():
        with U.hidden_console():
            barrier.wait(timeout=5)
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert subprocess.Popen is orig
