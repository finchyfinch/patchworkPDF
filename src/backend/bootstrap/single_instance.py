"""
single_instance.py — single-instance arbitration, port selection and hand-off.

Patchwork PDF allows one instance per machine.  entry.py invokes
handle_existing_instance() BEFORE its heavy imports (Flask, pypdf,
editor/viewer), so a double-clicked document that hits an already-running
instance hands the file off and exits in milliseconds instead of loading the
whole PDF stack first.

Design (v2.11):
  * The single-instance guarantee is an OS file lock (msvcrt on Windows,
    fcntl.flock on POSIX) held for the process lifetime — NOT the network port.
    Decoupling the two removes the old fragility where any unrelated process on
    the fixed port (very common for 5000) made us think Patchwork was running.
  * The serving port is chosen at runtime from a small list of uncommon
    candidate ports (falling back to an OS-assigned ephemeral port), so a busy
    port no longer blocks startup.
  * The chosen port is published in a per-user rendezvous file so a second
    launch knows where to forward its file.
  * Before treating anything on a port as "us", we verify identity with a
    /api/ping probe — a foreign server answering on a candidate port is ignored
    rather than mistaken for Patchwork.

Deliberately stdlib-only — adding a third-party import here would put its load
time back on the second-instance fast path.
"""

import atexit
import json
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Default + fallback ports, chosen from the IANA dynamic/private range
# (49152–65535) to avoid the crowded 5000/8000/8080 space.  PATCHWORK_PORT
# overrides the list entirely (used by headless/dev/test runs).
DEFAULT_PORT    = 49371
CANDIDATE_PORTS = [49371, 49372, 49373, 49374, 49375]

# Loopback probe timeout.  A live local server answers in well under 5 ms, so a
# quarter-second is enormously generous for a real reply — while capping the
# stall on machines where a connect to a *closed* loopback port is silently
# dropped (firewall / endpoint-security policy common on managed business
# machines) rather than refused instantly.  The old 1.0s here, multiplied across
# a stale-rendezvous ping plus a 5-port fallback scan, was the ~6s cold-start
# stall diagnosed in docs/BACKGROUND_PRELOAD_PLAN.md (Phase 0).
_PROBE_TIMEOUT = 0.25

# How long a launch waits for a running instance to answer — or, if that
# instance is shutting down, for it to finish and free the lock.  A closed
# window takes ~3 s to exit on a fast PC (WebView2 tear-down) and several times
# that on a slow laptop; the old 5 s wait, which never retried the lock, showed
# "already running" whenever a document was opened within that window.
_HANDOFF_WAIT = 20.0

# Identity probe — distinguishes our server from any other process that happens
# to hold one of our ports.  Must match the /api/ping handler in entry.py.
_PING_PATH = '/api/ping'
_APP_ID    = 'patchwork-pdf'

# Holds the OS lock for the process lifetime; closing it releases the lock.
_lock_handle = None


# ── Per-user state directory (matches pdf_core / log_config convention) ──────
def _state_dir():
    from platform_io import platform_paths
    return platform_paths.user_data_dir()


def _lock_path():
    return os.path.join(_state_dir(), 'instance.lock')


def _rendezvous_path():
    return os.path.join(_state_dir(), 'instance.json')


# ── Port helpers ─────────────────────────────────────────────────────────────
def _candidate_ports():
    """Ports to try, in order.  PATCHWORK_PORT, if set, forces a single port."""
    forced = os.environ.get('PATCHWORK_PORT')
    if forced:
        try:
            return [int(forced)]
        except ValueError:
            logger.warning("Ignoring invalid PATCHWORK_PORT=%r", forced)
    return list(CANDIDATE_PORTS)


def _can_bind(port):
    """True if we can bind 127.0.0.1:port right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def _pick_free_port():
    """First bindable candidate port, or an OS-assigned ephemeral port if every
    candidate is busy.  A small TOCTOU window exists between this check and
    Flask's later bind, but we already hold the single-instance lock so no other
    Patchwork can race us, and a foreign process grabbing the exact port in that
    millisecond is vanishingly unlikely."""
    for port in _candidate_ports():
        if _can_bind(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def is_listening(port, timeout=_PROBE_TIMEOUT):
    """Plain TCP check — used while waiting for our own Flask to come up.  Short
    timeout so the pre-bind attempts (before Flask has bound the port) can't each
    stall the full second on a drop-policy machine."""
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            return True
    except OSError:
        return False


def _ping(port, timeout=_PROBE_TIMEOUT):
    """Return the identity dict if *Patchwork* answers on *port*, else None.
    A non-Patchwork server (or no server) yields None.

    Two-stage and short-timeout on purpose.  On managed business machines a
    connect to a *closed* loopback port is silently dropped rather than refused,
    so it stalls for the full timeout instead of failing instantly; a live local
    server answers in well under 5 ms.  We therefore (1) keep the timeout small
    and (2) do a cheap TCP pre-connect before paying for the HTTP round-trip, and
    (3) bypass any configured proxy so a PAC/WPAD lookup can't stall a loopback
    request."""
    # Stage 1: bounded TCP connect.  A dropped/closed port fails here (after at
    # most `timeout`), so we never build the HTTP stack for a dead port.
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            pass
    except OSError:
        return None
    # Stage 2: identity check over HTTP, with the proxy explicitly bypassed.
    try:
        url = 'http://127.0.0.1:%d%s' % (port, _PING_PATH)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if isinstance(data, dict) and data.get('app') == _APP_ID:
            return data
    except Exception:
        pass
    return None


# ── Rendezvous file ──────────────────────────────────────────────────────────
def _read_rendezvous():
    try:
        with open(_rendezvous_path(), 'r', encoding='utf-8') as f:
            rec = json.load(f)
        if isinstance(rec, dict) and isinstance(rec.get('port'), int):
            return rec
    except (OSError, ValueError):
        pass
    return None


def _write_rendezvous(port, token=None):
    rec  = {'pid': os.getpid(), 'port': port}
    if token:
        # Per-launch IPC token, shared ONLY through this user-private file so a
        # second local instance can authenticate its /api/open_in_new_tab hand-off.
        # A web page (even one that guesses the port) cannot read this file, so it
        # cannot forge the call.
        rec['token'] = token
    path = _rendezvous_path()
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        os.replace(tmp, path)               # atomic publish
    except OSError as e:
        logger.info("Could not write rendezvous file: %s", e)


def _grant_foreground(pid):
    """Hand OUR foreground rights to the running primary (Windows only).

    Windows only lets the process that currently owns the foreground — or one it
    has explicitly blessed — call SetForegroundWindow; every other caller is
    denied and the taskbar button is flashed instead.  Explorer grants those
    rights to the process it launches, so when a document is double-clicked THIS
    (second, short-lived) process may foreground a window but the primary, which
    actually owns the window, may not.  That is exactly the "icon pulses in the
    taskbar but the minimised window never comes back" report.

    AllowSetForegroundWindow transfers the right to the primary's PID, so its
    own ShowWindow/SetForegroundWindow in /api/open_in_new_tab is honoured.  The
    grant lasts until the target uses it, and we stay alive across the forwarding
    POST, so it is still valid when the primary restores itself.

    Best-effort: a failure just leaves the old (flashing) behaviour."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ASFW_ANY = -1                        # last resort: let any process do it
        ctypes.windll.user32.AllowSetForegroundWindow(
            int(pid) if pid else ASFW_ANY)
    except Exception as e:
        logger.info("AllowSetForegroundWindow failed: %s", e)


def _cleanup_rendezvous():
    try:
        os.remove(_rendezvous_path())
    except OSError:
        pass


# ── Single-instance lock ─────────────────────────────────────────────────────
def _try_acquire_lock():
    """Grab the machine-wide single-instance lock.  Returns True if acquired
    (we are the primary instance) or False if another instance already holds it.
    The lock is held until the process exits (the OS releases it on death).

    Idempotent: once we hold the lock, re-calling this returns True without
    re-locking.  This lets the pre-heavy-import fast path acquire the lock early
    (lock-first, see handle_existing_instance) and the later authoritative
    become_primary_or_handoff() reuse that same acquisition."""
    global _lock_handle
    if _lock_handle is not None:
        return True                          # already held by this process
    try:
        fh = open(_lock_path(), 'a+b')
    except OSError as e:
        # Can't even open the lock file — fail open rather than wrongly block
        # the user from running the app at all.
        logger.info("Lock file unavailable (%s); skipping mutual-exclusion lock", e)
        return True
    try:
        if sys.platform == 'win32':
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _lock_handle = fh                        # keep alive → keep the lock
    return True


# ── Public API ───────────────────────────────────────────────────────────────
def find_initial_file(argv=None):
    """Return the first existing file path among the command-line arguments
    (Windows file association / "open with"), or None.  Works in both frozen
    (cx_Freeze) and unfrozen modes — cx_Freeze may pass the exe path as argv[0]
    and the document as argv[1]."""
    from platform_io.platform_paths import fs_path
    args = (sys.argv if argv is None else argv)[1:]
    for arg in args:
        # fs_path so a document on a deep share (over Windows' 260-char limit)
        # is still found — without it the app launches empty and says nothing,
        # which reads as "the file won't open".
        if not arg.startswith('-') and os.path.isfile(fs_path(arg)):
            return arg
    return None


def _ping_live(port):
    """Like _ping, but an instance that reports it is closing (its last window
    has gone and it is exiting) counts as not running: a document handed to it
    would never be shown."""
    data = _ping(port)
    return data if data and not data.get('closing') else None


def find_running_instance():
    """Return the port of an already-running, identity-verified Patchwork
    instance that is not shutting down, or None.  Trusts the rendezvous file
    first (the primary publishes its port there), then falls back to scanning
    the candidate ports — the fallback runs the probes concurrently so a run of
    dropped/closed ports costs ~one probe timeout, not one per port."""
    rec = _read_rendezvous()
    if rec and _ping_live(rec['port']):
        return rec['port']
    # Fallback: rendezvous missing or stale.  Probe the remaining candidates in
    # parallel.  (Reached only when an instance is believed to be running — the
    # lock-first cold-start path skips probing entirely, so this never runs on a
    # normal first launch.)
    scanned = rec.get('port') if rec else None
    ports = [p for p in _candidate_ports() if p != scanned]
    if not ports:
        return None
    import concurrent.futures
    found = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ports)) as ex:
        futs = {ex.submit(_ping_live, p): p for p in ports}
        for fut in concurrent.futures.as_completed(futs):
            if found is None and fut.result():
                found = futs[fut]
    return found


def _forward_file(port, initial_file):
    """POST the file path to the running instance so it opens as a new tab.

    Returns False only when the instance REFUSED it because it is closing
    (HTTP 503) or vanished mid-request — the caller then waits for its lock and
    opens the file itself.  Anything else counts as delivered: the running
    instance received the request even if our response is slow or it rejected
    the file, and a second copy of the app must not start for it."""
    logger.info("Sending '%s' to running instance via /api/open_in_new_tab", initial_file)
    rec = _read_rendezvous() or {}
    # Bless the primary BEFORE the POST: it foregrounds itself while handling the
    # request, and by then we are blocked in urlopen (still alive, so the grant
    # still stands).  Without this the window stays minimised and the taskbar
    # button merely flashes.
    _grant_foreground(rec.get('pid'))
    try:
        body = json.dumps({'file_path': initial_file}).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        # Authenticate the hand-off with the primary's per-launch token, read from
        # the user-private rendezvous file.  The endpoint requires this token, so a
        # foreign web page (which cannot read the file) cannot drive it.
        token = rec.get('token')
        if token:
            headers['X-App-Token'] = token
        req  = urllib.request.Request(
            'http://127.0.0.1:%d/api/open_in_new_tab' % port,
            data=body,
            headers=headers,
        )
        # The running app loads the file (slow for large/scanned PDFs), so allow
        # a generous timeout.  Do NOT show the "already running" dialog here —
        # the user double-clicked a file and expects it to open; the running
        # instance received the request and opens it even if our response is slow.
        urllib.request.urlopen(req, timeout=120)
        logger.info("File forwarded successfully — exiting second instance.")
    except urllib.error.HTTPError as http_err:
        if http_err.code == 503:
            logger.info("Running instance is closing — waiting to open the file ourselves.")
            return False
        logger.info("Running instance answered %s for the file.", http_err.code)
    except urllib.error.URLError as url_err:
        # urlopen wraps a refused connect; a reset mid-response surfaces bare
        # (http.client.RemoteDisconnected is a ConnectionResetError).
        if isinstance(url_err.reason, (ConnectionRefusedError, ConnectionResetError)):
            logger.info("Running instance went away mid-hand-off (%s) — waiting for it.", url_err)
            return False
        logger.info("Forward response not received (file likely still opening): %s", url_err)
    except ConnectionResetError as gone:
        logger.info("Running instance went away mid-hand-off (%s) — waiting for it.", gone)
        return False
    except Exception as fwd_err:
        # Best-effort: the running instance almost certainly still opens the
        # file.  Log and exit quietly rather than show a misleading error.
        logger.info("Forward response not received (file likely still opening): %s", fwd_err)
    return True


def _show_already_running_message():
    """Show a native OS message box telling the user not to run twice."""
    _en_msg = (
        "Patchwork PDF is already running.\n\n"
        "Only one instance can run at a time.\n"
        "Please use the existing window, or close it first before starting again."
    )
    _en_title = "Patchwork PDF — Already Running"
    # Localize to the language persisted from the last session (best effort).
    try:
        from i18n import i18n_py
        msg = i18n_py.pt('native.already_running_msg')
        if msg == 'native.already_running_msg':
            msg = _en_msg
        title = i18n_py.pt('native.already_running_title')
        if title == 'native.already_running_title':
            title = _en_title
    except Exception:
        msg, title = _en_msg, _en_title
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, msg, title, 0x30)  # MB_ICONWARNING
        elif sys.platform == 'darwin':
            import subprocess
            subprocess.run(['osascript', '-e',
                f'display dialog "{msg}" buttons {{"OK"}} default button "OK" '
                f'with icon caution with title "Patchwork PDF"'], check=False)
        else:
            # Linux: try zenity, then notify-send, then print to stderr
            import subprocess, shutil
            if shutil.which('zenity'):
                subprocess.run(['zenity', '--warning', '--title=Patchwork PDF',
                                f'--text={msg}'], check=False)
            elif shutil.which('notify-send'):
                subprocess.run(['notify-send', '-u', 'critical',
                                'Patchwork PDF', msg], check=False)
            else:
                print(f"\n{'='*60}\n{msg}\n{'='*60}\n", file=sys.stderr)
    except Exception:
        # Last resort — always visible
        print(f"\n{'='*60}\n{msg}\n{'='*60}\n", file=sys.stderr)


def handle_existing_instance(initial_file):
    """Fast second-instance path (called before the heavy imports).

    Lock-first: grab the machine-wide single-instance lock immediately.  Getting
    it proves nothing else is running, so we return False to become the primary
    WITHOUT any port probing — avoiding the multi-second stall that probing dead
    loopback ports causes on machines whose firewall silently drops those
    connects (see docs/BACKGROUND_PRELOAD_PLAN.md Phase 0/1).  Only if the lock
    is already held (an instance really is running) do we locate that instance
    and hand the file off / show the message.  Returns True when the caller
    should exit; the acquired lock is carried into become_primary_or_handoff()."""
    if _try_acquire_lock():
        return False                         # we hold the lock → we are primary
    return _defer_to_running_instance(initial_file)


def _defer_to_running_instance(initial_file):
    """The lock is held by another instance.  Hand the file to it, or — if it is
    on its way out — wait for it to exit and take over.  Returns True when the
    running instance has dealt with this launch (the caller exits), False once
    WE hold the lock (the caller carries on as the primary).

    Each pass re-tries the lock FIRST.  The holder may be:
      * running normally            → it answers; hand off (or say it is running);
      * just starting (lock taken a moment ago, not yet serving) → keep polling;
      * closing (window gone, exit under way, which takes seconds) → it answers
        "closing" or not at all, and refuses hand-offs; its lock frees when the
        process ends, and we start up as if it had never been there.
    The pre-fix loop never re-tried the lock, so that last case ran out its wait
    and said "already running" — every time a document was double-clicked within
    a few seconds of closing the app."""
    deadline = time.time() + _HANDOFF_WAIT
    announced = False
    while True:
        if _try_acquire_lock():
            logger.info("Previous instance has exited — starting as the primary.")
            return False
        port = find_running_instance()
        if port is not None:
            if not initial_file:
                _show_already_running_message()
                return True
            if not announced:
                logger.info("Existing Patchwork instance found on port %d.", port)
                announced = True
            if _forward_file(port, initial_file):
                return True
            # Refused: it is closing.  Keep waiting for its lock.
        if time.time() >= deadline:
            break
        time.sleep(0.1)
    # Lock held the whole time and nothing live answered (a hung or crashed
    # holder).  Nothing else can be done from here.
    logger.warning("Lock held but no live instance answered within %.0fs.", _HANDOFF_WAIT)
    _show_already_running_message()
    return True


def become_primary_or_handoff(initial_file, ipc_token=None):
    """Authoritative single-instance arbitration, called from main().  Acquires
    the machine-wide lock and returns a free port to serve on.  If another
    instance owns the lock, waits for it to start serving, hands the file off
    (or shows the message) and returns None so the caller exits.

    ipc_token is the primary's per-launch token; it is published in the private
    rendezvous file so a later second instance can authenticate its hand-off.

    This is the real arbiter; handle_existing_instance() above is a fast
    pre-check that, under the lock-first scheme, has usually ALREADY acquired the
    lock (the acquisition is idempotent, so the call below just confirms it) and
    handles the already-running case before the heavy imports.  This path still
    stands alone for callers that import entry and invoke main() directly."""
    if not _try_acquire_lock():
        # Another instance won the lock: hand off to it, or outlast it if it is
        # closing (see _defer_to_running_instance).
        logger.info("Another instance holds the single-instance lock — handing off.")
        if _defer_to_running_instance(initial_file):
            return None
    port = _pick_free_port()
    _write_rendezvous(port, token=ipc_token)
    atexit.register(_cleanup_rendezvous)
    logger.info("Primary instance — serving on port %d.", port)
    return port
