"""
check_for_updates.py — online update checker for PatchworkPDF.

The application is published on SourceForge:

    https://sourceforge.net/projects/patchworkpdf/files/

Each release ships three things in that root folder:

  * ``PatchworkPDF_Setup_<version>.exe`` — the Windows installer
  * ``patchworkPDF_source_v<version>.zip`` — the source archive
  * ``changelog.docx`` — a single Word document holding the full change history

The *published installer filenames* are the source of truth for "what is the
latest version": each release ships ``PatchworkPDF_Setup_<version>.exe``, so the
version is right there in the name.  The RSS feed lists those filenames and is a
cheap bit of XML — far lighter than the Word changelog — so we read it first.

The changelog is consulted only for the human-readable release notes, and only
when the feed shows a genuinely newer version.  Its structure
(see docs/changelog.docx) is:

    patchworkPDF Change Log          <- title
                                     <- blank line
    2.11                             <- a version heading (just the number)
    • bullet ...                     <- List Paragraph items = release notes
    • bullet ...
                                     <- blank line
    2.10                             <- next (older) version heading
    • bullet ...
    ...

This module does three things:

  1. Read the SourceForge RSS feed and take the newest ``PatchworkPDF_Setup_
     <version>.exe`` filename as the latest published version.
  2. Compare that version against the running application's version.
  3. Only if it is newer, download ``changelog.docx`` and pull out that
     release's notes.  When there is nothing newer, no changelog is fetched at
     all — the common case costs a single lightweight RSS request.

It is written to run standalone for testing::

    python check_for_updates.py

and to drop into the rest of the application later via a single call::

    from services.check_for_updates import check_for_updates
    result = check_for_updates()
    if result.new_release:
        ...   # prompt the user, open result.download_url, etc.

The only third-party dependency is ``python-docx`` (already in requirements.txt).
Everything else uses the standard library so the checker stays lightweight.
"""

from __future__ import annotations

import io
import os
import re
import sys
import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from xml.etree import ElementTree

from docx import Document

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration — everything site-specific lives here so integration is a
# matter of importing check_for_updates() and (optionally) tweaking these.
# ─────────────────────────────────────────────────────────────────────────────

PROJECT = "patchworkpdf"
BASE_URL = f"https://sourceforge.net/projects/{PROJECT}/files/"

# SourceForge serves a per-folder RSS feed listing every published file with its
# canonical download link.  It is a far more reliable "what's on the page"
# signal than scraping the HTML, and it doubles as our file-existence check.
RSS_URL = f"https://sourceforge.net/projects/{PROJECT}/rss?path=/"

CHANGELOG_NAME = "changelog.docx"

# A SourceForge file is downloaded via .../files/<name>/download
DOWNLOAD_URL_TEMPLATE = BASE_URL + "{name}/download"

DEFAULT_TIMEOUT = 30  # seconds, per network request
USER_AGENT = "PatchworkPDF-UpdateChecker"

# A version heading in the changelog is a paragraph whose entire text is a
# dotted number such as "2.11" or "2.09" (optionally surrounded by whitespace).
_VERSION_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)\s*$")

# A published installer's filename carries the version as a dotted number; this
# is our "latest version" signal, read straight from the RSS file listing.  The
# artifact differs per platform, so the pattern is chosen at runtime:
#   Windows : PatchworkPDF_Setup_<version>.exe
#   macOS   : PatchworkPDF-<version>-<Intel|AppleSilicon>.dmg
#   Linux   : PatchworkPDF-<version>.flatpak  (self-hosted on SourceForge)
#
# A macOS build is never universal — cx_Freeze freezes the interpreter and
# wheels of the machine it ran on — so each release publishes one DMG per
# architecture and the arch label is part of the filename.  The label is
# optional in the pattern so DMGs published before this scheme (plain
# ``PatchworkPDF-<version>.dmg``) still register as valid updates.
_INSTALLER_VERSION_RE_WIN = re.compile(
    r"^PatchworkPDF_Setup_(\d+(?:\.\d+)+)\.exe$", re.IGNORECASE
)
_INSTALLER_VERSION_RE_MAC = re.compile(
    r"^PatchworkPDF[-_](\d+(?:\.\d+)+)(?:[-_](Intel|AppleSilicon))?\.dmg$",
    re.IGNORECASE,
)
_INSTALLER_VERSION_RE_LINUX = re.compile(
    r"^PatchworkPDF[-_](\d+(?:\.\d+)+)\.flatpak$", re.IGNORECASE
)


def _mac_arch_label() -> str:
    """'AppleSilicon' or 'Intel' for the machine we're running on.

    platform.machine() reports the architecture of the *running process*, which
    is what we want: an Intel build under Rosetta reports x86_64 and should be
    offered the Intel DMG, since that is the one that will run.
    """
    import platform
    return "AppleSilicon" if platform.machine() == "arm64" else "Intel"


def _installer_pattern():
    """The installer-filename regex for the platform we're running on."""
    from platform_io import platform_paths
    if platform_paths.is_macos():
        return _INSTALLER_VERSION_RE_MAC
    if platform_paths.is_linux():
        return _INSTALLER_VERSION_RE_LINUX
    return _INSTALLER_VERSION_RE_WIN


def _installer_matches_machine(match: "re.Match") -> bool:
    """Whether a matched installer filename is one this machine can install.

    Only macOS names carry an architecture label.  An unlabelled DMG predates
    the per-arch scheme, so accept it rather than leave the user with no update
    path at all.
    """
    if match.re is not _INSTALLER_VERSION_RE_MAC:
        return True
    arch = match.group(2)
    return arch is None or arch.lower() == _mac_arch_label().lower()


# Back-compat alias for any external caller referencing the old name.
_INSTALLER_VERSION_RE = _INSTALLER_VERSION_RE_WIN


# ─────────────────────────────────────────────────────────────────────────────
# Result type — a plain, serialisable container the rest of the app can consume.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UpdateCheckResult:
    """Outcome of an update check.

    Attributes:
        new_release:      True if a newer version than ``current_version`` is
                          published *and* its installer exists online.
        release_version:  The latest version string from the changelog, e.g.
                          "2.11" (None if the changelog could not be read).
        release_notes:    The bullet-point notes for that release, one string
                          per bullet (empty list if none/unavailable).
        current_version:  The version the running application reported.
        download_url:     Direct SourceForge download link for the installer
                          (None if the installer was not found online).
        installer_name:   The installer filename we looked for.
        checked_online:   True if we successfully reached SourceForge.
        error:            A human-readable message if the check failed; None on
                          success.  The checker never raises for expected
                          network/parse problems — it reports them here so the
                          caller can fail soft.
    """

    new_release: bool = False
    release_version: str | None = None
    release_notes: list[str] = field(default_factory=list)
    current_version: str | None = None
    download_url: str | None = None
    installer_name: str | None = None
    checked_online: bool = False
    error: str | None = None

    @property
    def release_notes_text(self) -> str:
        """Release notes rendered as a simple bulleted block of text."""
        return "\n".join(f"• {line}" for line in self.release_notes)

    def to_dict(self) -> dict:
        """Plain dict (e.g. for passing to JS / serialising to JSON)."""
        return {
            "new_release": self.new_release,
            "release_version": self.release_version,
            "release_notes": list(self.release_notes),
            "current_version": self.current_version,
            "download_url": self.download_url,
            "installer_name": self.installer_name,
            "checked_online": self.checked_online,
            "error": self.error,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Version helpers
# ─────────────────────────────────────────────────────────────────────────────

def _version_key(version: str) -> tuple[int, ...]:
    """Turn a dotted version string into a comparable tuple of ints.

    Handles the changelog's zero-padded minor numbers correctly, e.g.
    "2.9", "2.09" and "2.11" become (2, 9), (2, 9) and (2, 11) — so 2.11 is
    correctly newer than 2.9 (which naive float comparison gets wrong).
    Non-numeric junk is ignored gracefully.
    """
    parts = []
    for chunk in version.strip().split("."):
        digits = re.sub(r"\D", "", chunk)
        if digits != "":
            parts.append(int(digits))
    return tuple(parts)


def _is_newer(candidate: str, current: str) -> bool:
    """True if ``candidate`` is a strictly newer version than ``current``."""
    return _version_key(candidate) > _version_key(current)


def _load_current_version() -> str | None:
    """Read the running application's version from version.json (next to this
    file).  Returns None if it cannot be read so the caller can decide what to
    do (the standalone test falls back to a sensible default).
    """
    # frozen: version.json sits next to the exe; dev: climb from
    # <root>/src/backend/services/check_for_updates.py to the project <root>.
    if getattr(sys, "frozen", False):
        here = os.path.dirname(sys.executable)
    else:
        here = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
    path = os.path.join(here, "version.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("version", "")).strip() or None
    except (OSError, ValueError) as exc:
        logger.warning("Could not read version.json (%s): %s", path, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Network helpers
# ─────────────────────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int) -> bytes:
    """GET a URL and return the raw body.  Raises urllib errors on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _list_remote_files(timeout: int) -> list[str]:
    """Return the list of filenames currently published in the files root.

    Parsed from the SourceForge RSS feed, whose <item><link> elements point at
    ``.../files/<filename>/download``.  This is our authoritative "what is on
    the web page" listing.
    """
    raw = _http_get(RSS_URL, timeout)
    root = ElementTree.fromstring(raw)

    names: list[str] = []
    # RSS layout: rss > channel > item > link
    for link in root.iter("link"):
        href = (link.text or "").strip()
        m = re.search(r"/files/(.+?)/download/?$", href)
        if m:
            # Path may include subfolders; we only care about the leaf name.
            names.append(m.group(1).split("/")[-1])
    return names


def _latest_installer(remote_files: list[str]) -> tuple[str | None, str | None]:
    """Pick the newest published installer from a file listing.

    Returns ``(version, filename)`` for the highest-versioned installer of this
    platform's type present, or ``(None, None)`` if none is listed.  The version
    is read straight from the filename, so no changelog download is needed to
    learn "what is the latest release".
    """
    pattern = _installer_pattern()
    best_version: str | None = None
    best_name: str | None = None
    for name in remote_files:
        m = pattern.match(name)
        if not m or not _installer_matches_machine(m):
            continue
        version = m.group(1)
        if best_version is None or _version_key(version) > _version_key(best_version):
            best_version, best_name = version, name
    return best_version, best_name


def _download_changelog(timeout: int) -> Document:
    """Download changelog.docx into memory and open it with python-docx."""
    url = DOWNLOAD_URL_TEMPLATE.format(name=CHANGELOG_NAME)
    logger.info("Downloading changelog: %s", url)
    data = _http_get(url, timeout)
    return Document(io.BytesIO(data))


# ─────────────────────────────────────────────────────────────────────────────
# Changelog parsing
# ─────────────────────────────────────────────────────────────────────────────

def _notes_for_version(doc: Document, version: str) -> list[str]:
    """Return the release notes for a specific version from the changelog.

    Scans for the version heading whose number equals ``version`` and collects
    the non-empty paragraphs that follow it up to the next version heading (or
    end of document).  Returns an empty list if that version has no heading —
    the notes are a nicety, so a missing section is not treated as an error.
    """
    want = _version_key(version)
    collecting = False
    notes: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        heading = _VERSION_HEADING_RE.match(text)
        if heading:
            if collecting:
                # Reached the next (older) release → we're done.
                break
            if _version_key(heading.group(1)) == want:
                collecting = True
            continue

        if collecting:
            notes.append(text)

    return notes


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def check_for_updates(
    current_version: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> UpdateCheckResult:
    """Check SourceForge for a newer PatchworkPDF release.

    Args:
        current_version: The running app's version (e.g. "2.11").  If omitted,
            it is read from version.json next to this file.
        timeout: Per-request network timeout in seconds.

    Returns:
        An :class:`UpdateCheckResult`.  Expected failures (no network, parse
        problems) are reported via ``result.error`` rather than raised, so the
        application can fail soft and simply not offer an update.
    """
    if current_version is None:
        current_version = _load_current_version()

    result = UpdateCheckResult(current_version=current_version)

    # The Linux build is self-hosted on SourceForge (as a .flatpak) alongside the
    # Windows/macOS artifacts, so the same RSS-based check applies on every OS;
    # _installer_pattern() selects the right filename type.

    # 1. Read the lightweight RSS listing → newest published installer version.
    #    This alone answers "is there anything newer"; the heavy changelog is
    #    only touched below if the answer is yes.
    try:
        remote_files = _list_remote_files(timeout)
    except (urllib.error.URLError, OSError, ElementTree.ParseError) as exc:
        result.error = f"Could not list published files: {exc}"
        logger.warning(result.error)
        return result

    result.checked_online = True

    version, installer_name = _latest_installer(remote_files)
    if not version:
        result.error = "No installer found in the published files listing."
        logger.warning(result.error)
        return result

    result.release_version = version
    result.installer_name = installer_name
    result.download_url = DOWNLOAD_URL_TEMPLATE.format(name=installer_name)

    # 2. Nothing newer than what's running → done, no changelog download.
    if not (current_version and _is_newer(version, current_version)):
        logger.info("Up to date: latest %s, running %s", version, current_version)
        return result

    result.new_release = True
    logger.info("Update available: %s (running %s)", version, current_version)

    # 3. A newer release exists → now (and only now) fetch the changelog to pull
    #    out its release notes.  Notes are a nicety: if the changelog is missing
    #    or unreadable we still report the update, just without the bullet list.
    try:
        doc = _download_changelog(timeout)
        result.release_notes = _notes_for_version(doc, version)
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Could not download changelog for notes: %s", exc)
    except Exception as exc:  # malformed/locked docx, etc.
        logger.warning("Could not read changelog for notes: %s", exc)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test harness
# ─────────────────────────────────────────────────────────────────────────────

def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Check SourceForge for a newer PatchworkPDF release."
    )
    parser.add_argument(
        "--current",
        metavar="VERSION",
        help="Pretend this version is installed (default: read version.json). "
             "Use e.g. --current 2.00 to force an 'update available' result.",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help=f"Network timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the raw result as JSON instead of a formatted report.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    result = check_for_updates(current_version=args.current, timeout=args.timeout)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.error is None else 1

    print()
    print("=" * 64)
    print(" PatchworkPDF — update check")
    print("=" * 64)
    print(f" Installed version : {result.current_version or '(unknown)'}")
    print(f" Latest version    : {result.release_version or '(unknown)'}")
    print(f" Installer found   : {result.installer_name or '(n/a)'}"
          f"{'  ✓' if result.download_url else '  ✗'}")
    print(f" New release?       : {'YES' if result.new_release else 'no'}")
    if result.download_url:
        print(f" Download URL      : {result.download_url}")
    if result.error:
        print(f" Note              : {result.error}")
    if result.release_notes:
        print("-" * 64)
        print(f" Release notes for {result.release_version}:")
        for line in result.release_notes:
            print(f"   • {line}")
    print("=" * 64)
    print()

    return 0 if result.error is None else 1


if __name__ == "__main__":
    raise SystemExit(_main())
