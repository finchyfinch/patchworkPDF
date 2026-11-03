"""
boot_timing.py — lightweight, one-shot startup instrumentation.

Captures a monotonic T0 at import time (so import this FIRST in entry.py, before
the heavy modules) and records named milestones through the whole bootstrap —
from process start to the first page being rendered and displayed in the
WebView.  Every mark logs the elapsed time since T0 and the delta since the
previous mark, all on ONE clock, so the slowest phase is obvious at a glance:

    [BOOT]      0.0 ms  (+    0.0)  process start (entry.py top)
    [BOOT]    412.7 ms  (+  412.7)  heavy imports loaded
    ...
    [BOOT]   1853.4 ms  (+  611.2)  client: first page rendered & displayed

Marks taken before logging is configured are buffered and flushed by
enable_logging().  Browser-side milestones are fed in via mark('client: ...')
from the /api/boot_mark route and land on the same timeline (the localhost
round-trip is sub-millisecond, negligible for bottleneck hunting).

This is diagnostic-only; it holds no app state and is safe to leave in place.
"""

import time
import logging
import threading

T0      = time.perf_counter()       # monotonic reference — as early as possible
T0_WALL = time.time()               # wall-clock of the same instant (for context)

_lock     = threading.Lock()
_marks    = []      # (label, elapsed_ms, delta_ms, client_ms)
_last     = T0
_ready    = False
_emitted  = 0
logger    = logging.getLogger('boot')


def mark(label, client_ms=None):
    """Record a bootstrap milestone.

    client_ms — the browser's performance.now() for client marks (logged for
    reference); None for server-side marks.
    """
    global _last, _emitted
    with _lock:
        now   = time.perf_counter()
        rec   = (label, (now - T0) * 1000.0, (now - _last) * 1000.0, client_ms)
        _last = now
        _marks.append(rec)
        if _ready:
            _emit(rec)
            _emitted = len(_marks)


def _emit(rec):
    label, elapsed, delta, client_ms = rec
    if client_ms is None:
        logger.info('[BOOT] %9.1f ms  (+%8.1f)  %s', elapsed, delta, label)
    else:
        logger.info('[BOOT] %9.1f ms  (+%8.1f)  %s  [client perf=%.0f ms]',
                    elapsed, delta, label, client_ms)


def enable_logging():
    """Flush marks buffered before logging was configured, then log live."""
    global _ready, _emitted
    with _lock:
        _ready = True
        if _emitted == 0:
            logger.info('[BOOT] ---- startup timeline (wall start %s) ----',
                        time.strftime('%Y-%m-%d %H:%M:%S',
                                      time.localtime(T0_WALL)))
        while _emitted < len(_marks):
            _emit(_marks[_emitted])
            _emitted += 1


def report():
    """Return a copy of all marks: list of (label, elapsed_ms, delta_ms, client_ms)."""
    with _lock:
        return list(_marks)
