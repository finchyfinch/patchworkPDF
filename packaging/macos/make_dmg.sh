#!/bin/bash
# make_dmg.sh — build PatchworkPDF.app (cx_Freeze) and wrap it in a .dmg.
# Run on macOS from the repository root:
#     bash packaging/macos/make_dmg.sh
#
# Produces: dist/PatchworkPDF-<version>-<Intel|AppleSilicon>.dmg
#
# cx_Freeze bundles the interpreter and the architecture-specific wheels of the
# machine it runs on, so a build is NEVER universal: building here yields an
# artifact for THIS Mac's architecture only.  The arch therefore goes in the
# filename, so a user downloading from SourceForge can tell at a glance which
# one they need, and so both can sit side by side in the same release folder.
# The name matches the update checker's macOS regex
# (services/check_for_updates.py), which only offers a user the DMG built for
# the architecture they are running.
#
# Signing / notarization is intentionally NOT done here — see the
# "Signing & notarization" section of docs/BUILD_AND_DEPLOY_MACOS.md.
set -euo pipefail

cd "$(dirname "$0")/../.."

VERSION="$(python3 -c 'import json;print(json.load(open("version.json"))["version"])')"

# uname -m reports the architecture of the running process: arm64 on Apple
# Silicon, x86_64 on Intel (and on an arm64 Mac running this under Rosetta,
# which correctly produces an Intel build).
case "$(uname -m)" in
  arm64)  ARCH_LABEL="AppleSilicon" ;;
  x86_64) ARCH_LABEL="Intel" ;;
  *)      echo "ERROR: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

APP="build/PatchworkPDF.app"
DMG="dist/PatchworkPDF-${VERSION}-${ARCH_LABEL}.dmg"

echo "==> Building PatchworkPDF ${VERSION} for ${ARCH_LABEL} ($(uname -m))"

# Nothing gets packaged from a tree whose tests fail.  Same gate as the Windows
# and Linux builds; works with or without pytest on the build machine.
echo "==> Running regression tests"
if ! python3 tools/run_tests.py --quiet; then
  echo "" >&2
  echo "ERROR: regression tests failed - build aborted, nothing was packaged." >&2
  echo "       Re-run them on their own with:  python3 tools/run_tests.py" >&2
  exit 1
fi
echo "==> Tests passed - proceeding with the build"

echo "==> Generating icons (icon.icns)"
python3 tools/make_icons.py

echo "==> Building the .app bundle"
rm -rf build/PatchworkPDF.app
python3 setup.py bdist_mac

if [ ! -d "$APP" ]; then
  echo "ERROR: $APP was not produced by bdist_mac" >&2
  exit 1
fi

echo "==> Creating $DMG"
mkdir -p dist
rm -f "$DMG"

if command -v create-dmg >/dev/null 2>&1; then
  # Nicer DMG (background, /Applications shortcut) if create-dmg is installed
  #   brew install create-dmg
  create-dmg \
    --volname "Patchwork PDF ${VERSION} (${ARCH_LABEL})" \
    --app-drop-link 480 200 \
    --icon "PatchworkPDF.app" 160 200 \
    "$DMG" "$APP"
else
  # Fallback: plain read-only DMG via hdiutil (always available on macOS)
  STAGE="$(mktemp -d)"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -volname "Patchwork PDF ${VERSION} (${ARCH_LABEL})" \
    -srcfolder "$STAGE" -ov -format UDZO "$DMG"
  rm -rf "$STAGE"
fi

# Sanity-check the architecture of the payload, so a mislabelled artifact is
# caught here rather than by a user on the wrong Mac.
#
# NOTE: do NOT judge this by Contents/MacOS/PatchworkPDF — the cx_Freeze launcher
# stub inherits python.org's universal2 build and reports "universal" on both
# architectures, which tells you nothing.  What decides whether the app actually
# runs is the bundled C extensions (.so) and their dylibs, which come from the
# architecture-specific wheels pip installed on THIS machine.
echo "==> Verifying architecture of the bundled native libraries"
NATIVE_TOTAL=0; NATIVE_WRONG=0
while IFS= read -r -d '' f; do
  ARCHS="$(lipo -archs "$f" 2>/dev/null || echo unknown)"
  NATIVE_TOTAL=$((NATIVE_TOTAL + 1))
  case " $ARCHS " in
    *" $(uname -m) "*) ;;                       # contains this machine's arch
    *) NATIVE_WRONG=$((NATIVE_WRONG + 1)); echo "    WRONG ARCH: $(basename "$f") [$ARCHS]" ;;
  esac
done < <(find "$APP" \( -name '*.so' -o -name '*.dylib' \) -print0)

echo "    ${NATIVE_TOTAL} native libraries, ${NATIVE_WRONG} not built for $(uname -m)"
if [ "$NATIVE_WRONG" -ne 0 ]; then
  echo "ERROR: bundle contains libraries that cannot run on $(uname -m)" >&2
  exit 1
fi

echo "==> Done: $DMG"
