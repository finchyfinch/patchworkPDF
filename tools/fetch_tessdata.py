#!/usr/bin/env python3
"""
fetch_tessdata.py — download / verify the bundled Tesseract language packs.

The OCR language data lives in ``assets/tessdata`` and is bundled into every
platform's artifact (see that folder's README).  The packs are ~11 MB of binary,
so a checkout can easily end up without them — and because the packaging step
used to skip a missing folder silently, that produced an installer that looked
fine but could only OCR in English.  This script is the single source of truth
for *which* packs are needed, where they come from and what they must hash to,
and it is run as a pre-flight check by the Windows installer build.

Usage (from anywhere — the repo root is derived from this file's location):

    python tools/fetch_tessdata.py            # download whatever is missing/bad
    python tools/fetch_tessdata.py --verify   # check only; exit 1 if not OK
    python tools/fetch_tessdata.py --force    # re-download everything

Stdlib only, so it runs on a bare interpreter before requirements.txt is
installed.

The Linux (Flatpak) build does NOT use this script: a Flatpak build has no
network access outside declared sources, so its manifest carries the same URLs
and checksums as `tessdata` module sources.  Keep the two lists in step —
`packaging/flatpak/com.patchworkpdf.app.yaml`.
"""

import argparse
import hashlib
import pathlib
import sys
import urllib.request

# tessdata_fast, pinned to the 4.1.0 tag — the recommended speed/accuracy
# balance for Tesseract 4/5, and a tag (not a branch) so the checksums below
# stay valid.  Languages match the app's UI languages (_UI_TO_TESS in
# src/backend/pdf/ocr_utils.py); `eng` is mandatory because ocr_utils points
# Tesseract at this folder with --tessdata-dir and asks for "<uilang>+eng",
# and with --tessdata-dir set Tesseract looks *nowhere else*.
TESSDATA_TAG = "4.1.0"
_BASE_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/" + TESSDATA_TAG

LANGS = {
    "eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "spa": "6f2e04d02774a18f01bed44b1111f2cd7f3ba7ac9dc4373cd3f898a40ea6b464",
    "fra": "ced037562e8c80c13122dece28dd477d399af80911a28791a66a63ac1e3445ca",
    "deu": "19d219bbb6672c869d20a9636c6816a81eb9a71796cb93ebe0cb1530e2cdb22d",
    "ita": "b8f89e1e785118dac4d51ae042c029a64edb5c3ee42ef73027a6d412748d8827",
}

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TESSDATA_DIR = REPO_ROOT / "assets" / "tessdata"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(code, expected):
    """Return (status, detail) for one pack: 'ok', 'missing' or 'mismatch'."""
    path = TESSDATA_DIR / f"{code}.traineddata"
    if not path.is_file():
        return "missing", ""
    actual = sha256(path)
    if actual != expected:
        return "mismatch", actual
    return "ok", actual


def download(code, expected):
    """Fetch one pack and verify it before it replaces anything on disk."""
    url = f"{_BASE_URL}/{code}.traineddata"
    dest = TESSDATA_DIR / f"{code}.traineddata"
    tmp = dest.with_suffix(".traineddata.part")
    print(f"  downloading {code}.traineddata  <-  {url}")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    actual = sha256(tmp)
    if actual != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"{code}.traineddata checksum mismatch\n"
            f"  expected {expected}\n  got      {actual}")
    tmp.replace(dest)
    print(f"  ok: {dest.relative_to(REPO_ROOT)}  ({dest.stat().st_size:,} bytes)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--verify", action="store_true",
                    help="check the packs and exit non-zero if any is "
                         "missing or corrupt; download nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-download every pack even if it is already valid")
    args = ap.parse_args(argv)

    print(f"tessdata dir: {TESSDATA_DIR}")
    TESSDATA_DIR.mkdir(parents=True, exist_ok=True)

    bad = []
    for code, expected in LANGS.items():
        status, actual = check(code, expected)
        if status == "ok" and not args.force:
            print(f"  {code}: ok")
            continue
        if status == "mismatch":
            print(f"  {code}: CHECKSUM MISMATCH (got {actual})")
        elif status == "missing":
            print(f"  {code}: MISSING")
        bad.append(code)

    if not bad:
        print("All tessdata language packs present and verified.")
        return 0

    if args.verify:
        print("")
        print("ERROR: OCR language data is incomplete: " + ", ".join(bad))
        print("Run:  python tools/fetch_tessdata.py")
        return 1

    for code in bad:
        download(code, LANGS[code])
    print("All tessdata language packs present and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
