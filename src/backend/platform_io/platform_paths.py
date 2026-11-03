"""platform_paths.py — cross-platform paths, OS detection, and shell helpers.

Single source of truth for the handful of things that differ between Windows,
macOS and Linux so the rest of the app never has to special-case the OS inline:

    user_data_dir()            — per-user writable dir for settings/logs/state
    open_path_in_default_app() — open a file in the OS default handler
    is_windows()/is_macos()/is_linux()/is_flatpak()

This module is intentionally dependency-free (stdlib only) and side-effect-free
on import, so it is safe to import from the earliest boot code (log_config,
single_instance) without risking a circular import or slowing startup.
"""

import os
import sys
import subprocess

APP_FOLDER_NAME = 'PatchworkPDF'


# ── OS detection ──────────────────────────────────────────────────────────────
def is_windows():
    return sys.platform == 'win32'


def is_macos():
    return sys.platform == 'darwin'


def is_linux():
    return sys.platform.startswith('linux')


def is_flatpak():
    """True when running inside a Flatpak sandbox.  Flatpak creates a
    ``/.flatpak-info`` file inside the sandbox and sets ``FLATPAK_ID``."""
    return os.path.exists('/.flatpak-info') or 'FLATPAK_ID' in os.environ


# ── Per-user data directory ───────────────────────────────────────────────────
def user_data_dir(app_name=APP_FOLDER_NAME):
    """Return (creating if needed) the per-user writable directory for app state
    — settings, logs, license/update prefs, single-instance lock, recent files.

        Windows : %LOCALAPPDATA%\\<app>        (unchanged from earlier versions)
        macOS   : ~/Library/Application Support/<app>
        Linux   : $XDG_DATA_HOME/<app>  or  ~/.local/share/<app>

    Under Flatpak, ``XDG_DATA_HOME`` is remapped by the sandbox to
    ``~/.var/app/<id>/data`` automatically, so no special-casing is needed here.

    Falls back to ``~/.<app-lower>`` if the preferred base cannot be created, so
    callers always get a usable path.
    """
    try:
        if is_windows():
            base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
            path = os.path.join(base, app_name)
        elif is_macos():
            path = os.path.join(os.path.expanduser('~'), 'Library',
                                'Application Support', app_name)
        else:  # Linux / other Unix
            base = os.environ.get('XDG_DATA_HOME') or os.path.join(
                os.path.expanduser('~'), '.local', 'share')
            path = os.path.join(base, app_name)
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        fallback = os.path.join(os.path.expanduser('~'), '.' + app_name.lower())
        try:
            os.makedirs(fallback, exist_ok=True)
        except OSError:
            pass
        return fallback


# ── Long paths (Windows MAX_PATH) ─────────────────────────────────────────────
# Windows refuses paths over 260 characters unless the process opts in *and* the
# machine has LongPathsEnabled set — which is off by default, and is a domain
# policy most workplaces never turn on.  Deep shares like
# \\fileserver\Projects\<client>\<job>\Accounts\Invoices\... blow past 260 easily,
# and the failure is silent in the worst way: os.path.isfile() simply answers
# False, so the file the user is looking at reads as "not there".
#
# The extended-length prefix is exempt from the limit and needs no opt-in, so
# every filesystem call on a path the *user* supplied goes through fs_path().
# It is for filesystem calls only — the plain path stays in state, in the UI and
# in the recents list, so nothing user-visible ever shows the prefix.
_MAX_PATH_SAFE = 250   # a little clear of 260, since callers may append


def fs_path(path):
    """A form of *path* the OS will accept, whatever its length.

    Returns *path* unchanged on non-Windows, for short paths, and for paths
    already carrying the prefix — so it is safe to wrap every call site.
    """
    if not path or not is_windows():
        return path
    p = str(path)
    if p.startswith('\\\\?\\') or p.startswith('\\\\.\\'):
        return p
    if len(p) <= _MAX_PATH_SAFE:
        return p
    # The prefix turns off all path parsing, so the path must be absolute and
    # fully normalised first — no forward slashes, no '.' or '..' components.
    p = os.path.abspath(p)
    if p.startswith('\\\\'):
        return '\\\\?\\UNC\\' + p[2:]     # \\server\share → \\?\UNC\server\share
    return '\\\\?\\' + p


# ── Shell integration ─────────────────────────────────────────────────────────
def open_path_in_default_app(path):
    """Open *path* in the OS default handler for its type.  Best-effort: returns
    True on apparent success, False if no handler could be launched.  Never
    raises — callers can offer their own fallback (e.g. save-as) on False.

        Windows : os.startfile
        macOS   : open
        Linux   : xdg-open  (works under Flatpak via the desktop portal)
    """
    try:
        if is_windows():
            os.startfile(path)   # noqa: S606 — local file, user-initiated
        elif is_macos():
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
        return True
    except Exception:
        return False


def open_url_in_default_browser(url):
    """Open *url* in the user's default browser.  Same contract as
    open_path_in_default_app: best-effort, returns True/False, never raises.

    Deliberately the shell handlers rather than the `webbrowser` module.  Under
    Flatpak `webbrowser` probes for a browser binary that is not inside the
    sandbox and gives up, while `xdg-open` reaches the real one through the
    desktop portal — the same reason open_path_in_default_app uses it.

    CALLER'S JOB: validate the scheme first.  These are shell handlers, so
    whatever the string names is what the OS runs; only ever pass a URL whose
    scheme is on a known-good allow-list.
    """
    try:
        if is_windows():
            os.startfile(url)    # noqa: S606 — vetted scheme, user-initiated
        elif is_macos():
            subprocess.Popen(['open', url])
        else:
            subprocess.Popen(['xdg-open', url])
        return True
    except Exception:
        return False
