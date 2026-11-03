#!/usr/bin/env bash
###############################################################################
#  build_flatpak.sh  —  Build Patchwork PDF as a self-hosted .flatpak bundle
#  ---------------------------------------------------------------------------
#
#  WHAT THIS DOES (in order)
#    0. Checks the metainfo's newest <release> matches version.json, and aborts
#       if not — a stale entry makes every build report the same version.
#    0b. Runs the regression suite (tools/run_tests.py) and aborts if anything
#       fails, so no bundle is ever cut from a broken tree.  It runs here, before
#       the ~1 GB of downloads and the Tesseract compile, so a failure costs
#       seconds rather than the whole build.
#    1. Installs `flatpak` + `flatpak-builder` if they are missing (uses sudo).
#       (Step 3 additionally picks org.flatpak.Builder when the system builder is
#       too old for the AppStream step — see the comment there.)
#    2. Adds the Flathub remote and installs the GNOME Platform + SDK whose
#       version is read straight from the manifest (`runtime-version:`).
#       -> The FIRST run downloads ~1 GB (SDK + runtime + GL drivers). Slow.
#          Every later run reuses them and is much faster.
#    3. Runs `flatpak-builder` to build the app into your *user* Flatpak repo.
#       -> This compiles Leptonica + Tesseract from source for OCR. That is
#          the slowest part of the build. To skip it, comment out the
#          autoconf-archive / leptonica / tesseract / tessdata modules in
#          com.patchworkpdf.app.yaml (the app then reports "OCR unavailable").
#    4. Bundles everything into a single file:  PatchworkPDF-<version>.flatpak
#       (version comes from ../../version.json, so the name always matches).
#    5. OPTIONAL: copies that bundle to a destination folder you pass as the
#       first argument — e.g. a VirtualBox shared folder that maps to a
#       Windows path.
#
#  ---------------------------------------------------------------------------
#  HOW TO RUN  (inside a Linux box / VM with internet access)
#
#    # From anywhere — the script finds the repo root itself:
#    chmod +x packaging/flatpak/build_flatpak.sh          # first time only
#    ./packaging/flatpak/build_flatpak.sh
#
#    # ...and also copy the finished bundle into a folder (e.g. a shared
#    #    folder that appears on the Windows host as C:\Temp):
#    ./packaging/flatpak/build_flatpak.sh /media/sf_CTemp
#
#  ---------------------------------------------------------------------------
#  IMPORTANT GOTCHAS
#
#  * VirtualBox shared folders are mounted root:vboxsf and are NOT readable by
#    your normal user unless that user is in the `vboxsf` group. So do NOT try
#    to BUILD directly from the share. Instead copy the source into your home
#    first, then build from there, e.g.:
#        sudo cp -r /media/sf_CTemp/patchworkPDF_source_vX.YZ ~/pw_build
#        sudo chown -R "$USER":"$USER" ~/pw_build
#        cd ~/pw_build && ./packaging/flatpak/build_flatpak.sh /media/sf_CTemp
#    (To make yourself a permanent member instead: `sudo usermod -aG vboxsf
#     $USER` then log out and back in.)
#
#  * The GNOME runtime version must stay in sync with `runtime-version:` in
#    com.patchworkpdf.app.yaml. This script reads it from there automatically,
#    so you only ever change it in one place (the manifest).
#
#  * Nothing here needs root except the one-off `apt install` in step 1 and,
#    if used, the copy-to-shared-folder in step 5 (retried with sudo only if a
#    plain copy is denied).
#
#  ---------------------------------------------------------------------------
#  AFTER IT FINISHES — test the bundle:
#      flatpak install --user -y ./PatchworkPDF-<version>.flatpak
#      flatpak run com.patchworkpdf.app
#
#  ...and CONFIRM you are running what you just built. `flatpak install` with no
#  scope flag installs SYSTEM-wide, while this script installs --user; if both
#  exist, the USER one wins silently and you test the old build:
#      flatpak list --app --columns=application,version,branch,installation
#      flatpak run --command=cat com.patchworkpdf.app \
#          /app/share/patchworkpdf/version.json
#  Pick one scope and stick to it. See docs/BUILD_AND_DEPLOY_LINUX.md §10.
#  Then upload PatchworkPDF-<version>.flatpak to SourceForge (see
#  docs/BUILD_AND_DEPLOY_LINUX.md §6).
###############################################################################

set -euo pipefail

# --- locate things --------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="$SCRIPT_DIR/com.patchworkpdf.app.yaml"
APP_ID="com.patchworkpdf.app"
DEST_DIR="${1:-}"            # optional: where to also copy the finished bundle

cd "$REPO_ROOT"

if [ ! -f "$MANIFEST" ]; then
  echo "ERROR: manifest not found at $MANIFEST" >&2
  exit 1
fi

# --- derive version + runtime from the tree (single source of truth) ------
VERSION="$(python3 -c 'import json;print(json.load(open("version.json"))["version"])')"
BUNDLE="PatchworkPDF-${VERSION}.flatpak"
RUNTIME_VER="$(grep -E "^runtime-version:" "$MANIFEST" | head -1 | sed -E "s/[^0-9]*([0-9]+).*/\1/")"

# --- 0. metainfo version must match version.json --------------------------
# The newest <release> in the metainfo is what `flatpak list` and GNOME Software
# report. If it drifts, every release reports the same stale number and — since
# the branch is always 'master' — there is no way to tell two installs apart.
METAINFO="$SCRIPT_DIR/com.patchworkpdf.app.metainfo.xml"
META_VER="$(grep -oE '<release version="[^"]+"' "$METAINFO" | head -1 \
            | sed -E 's/.*version="([^"]+)".*/\1/')"
if [ "$META_VER" != "$VERSION" ]; then
  echo "ERROR: version mismatch" >&2
  echo "  version.json          : $VERSION" >&2
  echo "  metainfo newest release: ${META_VER:-<none>}" >&2
  echo "" >&2
  echo "Add a release entry at the TOP of <releases> in:" >&2
  echo "  $METAINFO" >&2
  echo "  <release version=\"$VERSION\" date=\"$(date +%F)\"/>" >&2
  exit 1
fi

echo ">>> Patchwork PDF  ->  Flatpak"
echo "    repo root : $REPO_ROOT"
echo "    version   : $VERSION   (bundle: $BUNDLE)"
echo "    runtime   : org.gnome.Platform//$RUNTIME_VER"
[ -n "$DEST_DIR" ] && echo "    copy to   : $DEST_DIR"
echo

# --- 0b. regression tests -------------------------------------------------
# Nothing gets packaged from a tree whose tests fail.  Same gate as the Windows
# and macOS builds, and it runs before the expensive part (step 3 compiles
# Tesseract and Leptonica from source) rather than after it.
echo ">>> Running regression tests ..."
if ! python3 tools/run_tests.py --quiet; then
  echo "" >&2
  echo "ERROR: regression tests failed - build aborted, nothing was packaged." >&2
  echo "       Re-run them on their own with:  python3 tools/run_tests.py" >&2
  exit 1
fi
echo ">>> Tests passed - proceeding with the build"
echo

# --- 1. build tooling -----------------------------------------------------
if ! command -v flatpak >/dev/null 2>&1 || ! command -v flatpak-builder >/dev/null 2>&1; then
  echo ">>> Installing flatpak + flatpak-builder (sudo) ..."
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y flatpak flatpak-builder
fi

# --- 2. flathub remote + GNOME Platform/SDK -------------------------------
flatpak remote-add --user --if-not-exists flathub \
  https://flathub.org/repo/flathub.flatpakrepo
echo ">>> Ensuring org.gnome.Platform//$RUNTIME_VER and org.gnome.Sdk//$RUNTIME_VER"
echo "    (first run pulls ~1 GB from Flathub — be patient) ..."
flatpak install --user -y flathub \
  "org.gnome.Platform//$RUNTIME_VER" "org.gnome.Sdk//$RUNTIME_VER"

# --- 3. build into the user flatpak repo ----------------------------------
# Pick a builder that can generate AppStream data. The manifest installs a
# metainfo.xml, which makes flatpak-builder run an appstream compose step at
# finalization: builders older than 1.4 call appstream-glib's `appstream-compose`,
# which the GNOME 50 runtime no longer ships, so they fail with
# "bwrap: execvp appstream-compose: No such file or directory".
# Prefer the Flatpak-packaged builder, which is always current.
if flatpak info org.flatpak.Builder >/dev/null 2>&1; then
  BUILDER=(flatpak run org.flatpak.Builder)
  echo ">>> using org.flatpak.Builder (flatpak-packaged)"
else
  FB_VER="$(flatpak-builder --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)"
  if [ -n "$FB_VER" ] && [ "$(printf '%s\n%s\n' "1.4" "$FB_VER" | sort -V | head -1)" = "1.4" ]; then
    BUILDER=(flatpak-builder)
    echo ">>> using system flatpak-builder $FB_VER"
  else
    echo ">>> system flatpak-builder ${FB_VER:-unknown} is too old for the AppStream step."
    echo ">>> installing org.flatpak.Builder from flathub ..."
    flatpak install --user -y flathub org.flatpak.Builder
    BUILDER=(flatpak run org.flatpak.Builder)
  fi
fi

echo ">>> building (OCR modules compile Tesseract/Leptonica — slow) ..."
"${BUILDER[@]}" --user --force-clean --install build-dir "$MANIFEST"

# --- 4. bundle into one .flatpak file -------------------------------------
echo ">>> build-bundle -> $BUNDLE"
flatpak build-bundle \
  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
  "$HOME/.local/share/flatpak/repo" \
  "$REPO_ROOT/$BUNDLE" \
  "$APP_ID"
echo ">>> Built: $REPO_ROOT/$BUNDLE"

# --- 5. optional copy to a destination folder -----------------------------
if [ -n "$DEST_DIR" ]; then
  echo ">>> Copying bundle to $DEST_DIR ..."
  if cp "$REPO_ROOT/$BUNDLE" "$DEST_DIR/" 2>/dev/null; then
    echo "    copied."
  else
    echo "    plain copy denied (root-owned share?) — retrying with sudo ..."
    sudo cp "$REPO_ROOT/$BUNDLE" "$DEST_DIR/"
    sudo chmod 666 "$DEST_DIR/$BUNDLE" 2>/dev/null || true
    echo "    copied (sudo)."
  fi
fi

echo
echo ">>> DONE."
echo "    Test it:   flatpak install --user -y \"$REPO_ROOT/$BUNDLE\" && flatpak run $APP_ID"
