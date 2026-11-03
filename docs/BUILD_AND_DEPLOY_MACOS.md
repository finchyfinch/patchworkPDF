# Build & Deploy — macOS

How to build Patchwork PDF into a `.app` bundle and `.dmg` on macOS.
For the design behind the cross-platform work see
[CROSS_PLATFORM_PLAN.md](CROSS_PLATFORM_PLAN.md).

---

## 1. Prerequisites

| Requirement | Notes |
|-------------|-------|
| macOS 12+ | Build on the same OS/arch you ship — cx_Freeze cannot cross-compile. |
| Python 3.12+ | python.org build or Homebrew. Verified on 3.14.6 (Intel, macOS 14). |
| Xcode Command Line Tools | `xcode-select --install` (compilers for wheels). |
| WKWebView | **System** — nothing to install. This is the pywebview `cocoa` backend. |
| `create-dmg` (optional) | Nicer DMG layout: `brew install create-dmg`. Falls back to `hdiutil`. |
| LibreOffice (optional) | Word/Excel **import**: install to `/Applications`. Detected at runtime. |
| Tesseract (optional) | OCR: `brew install tesseract`. Detected on PATH / Homebrew dirs. |

### Install Python dependencies
```bash
python3 -m pip install --upgrade pip
pip3 install -r requirements.txt
```
Thanks to environment markers, macOS pulls `pyobjc-framework-WebKit` (the cocoa
backend binding) and skips the Windows-only packages.

On an **Intel** Mac two pins in `requirements.txt` do the work (both scoped by
environment marker, so Windows/Linux/Apple-Silicon resolution is unaffected):

- `pikepdf` — the whole 10.x line tags its Intel-macOS wheels `macosx_15_0_x86_64`,
  so pip refuses them on macOS 14 or older and falls back to a source build that
  needs a full qpdf/C++ toolchain. Intel therefore pins `pikepdf==9.11.0`, the last
  release with a `macosx_13_0_x86_64` wheel. `pikepdf.Real()` is 10.x-only, so
  `pdf/form_utils.py` deliberately uses plain Python numbers (valid on 9.x and 10.x).
- `cryptography` (transitively via `pdfplumber` → `pdfminer.six`) — release 49.0
  dropped Intel-macOS wheels entirely, so Intel pins `cryptography<49`.

Without those two pins, `pip install -r requirements.txt` fails on an Intel Mac
trying to compile qpdf and Rust from source.

### Architecture (decision point)
- Apple-silicon Mac → **arm64** build → `…-AppleSilicon.dmg`.
- Intel Mac → **x86_64** build → `…-Intel.dmg`.

cx_Freeze cannot cross-compile and there is no universal2 path (the wheels are
per-arch), so build on each architecture you want to support. See §3.

---

## 2. How dependencies are satisfied

| Dependency | How it ships |
|------------|--------------|
| Python + all pip libraries (incl. pyobjc) | **Frozen into the `.app`** by cx_Freeze. |
| pdfium, QPDF native `.dylib`s | Bundled — cx_Freeze's scanner collects them (verify on a clean Mac). |
| WKWebView runtime | **System** component. |
| Tesseract / LibreOffice | **Not bundled** — detected at runtime; optional. |
| OCR language data (`eng/spa/fra/deu/ita`) | **Bundled** by `setup.py` from `assets/tessdata` into `PatchworkPDF.app/Contents/MacOS/assets/tessdata`. Restore/verify with `python3 tools/fetch_tessdata.py`; `setup.py` prints a `[setup] WARNING:` for any missing pack. |

The user does not install Python or pip packages — the `.app` is self-contained
(except the optional Tesseract/LibreOffice tools).

---

## 3. Build the `.app` and `.dmg`

One command does icons → `.app` → `.dmg`:
```bash
bash packaging/macos/make_dmg.sh
```
The script runs the regression suite (`python3 tools/run_tests.py`) first and
aborts before building anything if it fails.
Output: `dist/PatchworkPDF-<version>-<Intel|AppleSilicon>.dmg` — the architecture
label is chosen automatically from `uname -m`, and the name matches the update
checker's macOS regex.

**A macOS build is never universal.** cx_Freeze freezes the interpreter together
with the architecture-specific wheels pip installed on the build machine, so the
`.app` only runs natively on the architecture it was built on. (Don't judge this
by `file Contents/MacOS/PatchworkPDF` — that launcher stub inherits python.org's
universal2 build and reports "universal" either way. The bundled `.so`/`.dylib`
files are what count; `make_dmg.sh` checks them and fails the build on a
mismatch.)

To publish for both, run the same script once on an Intel Mac and once on an
Apple Silicon Mac, and upload both DMGs to the same release folder. An Apple
Silicon user *can* run the Intel build under Rosetta, so the Intel DMG is the
safe fallback if you can only produce one.

Or step by step:
```bash
python3 tools/make_icons.py         # writes assets/icon.icns
python3 setup.py bdist_mac          # writes build/PatchworkPDF.app
open build/PatchworkPDF.app         # smoke test
```

`setup.py` sets the bundle id `com.patchworkpdf.app`, the icon, `LSMinimumSystemVersion`,
and registers the app as a PDF handler (`CFBundleDocumentTypes`) so Finder
double-click / "Open With" / dock-drop route the file in on launch.

### Troubleshooting
- **App launches from `build/` but not after copying** — a native `.dylib` was
  missed. Run from Terminal to see the missing-library error, then add the package
  to `packages` in `setup.py`. Always test on a **different** Mac than the build one.
- **No cocoa backend / blank window** — confirm `pyobjc-framework-WebKit` installed
  and that `objc`/`WebKit` are in the frozen `lib/`.

---

## 4. Signing & notarization (decide later)

The build is **unsigned** by default. Two ways to run it:

### a) Ship unsigned (internal / technical users)
Users clear the quarantine flag on first run:
```bash
xattr -cr /Applications/PatchworkPDF.app
```
or right-click → **Open** → **Open**. Document this for your users.

### b) Sign + notarize (public, Gatekeeper-clean) — requires an Apple Developer account ($99/yr)
```bash
# 1. Sign (hardened runtime)
codesign --deep --force --options runtime \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  build/PatchworkPDF.app

# 2. Build the DMG (packaging/macos/make_dmg.sh), then notarize it
xcrun notarytool submit dist/PatchworkPDF-<version>-<arch>.dmg \
  --apple-id you@example.com --team-id TEAMID --password APP_SPECIFIC_PW --wait

# 3. Staple the ticket
xcrun stapler staple dist/PatchworkPDF-<version>-<arch>.dmg
```
Wire these three steps into `make_dmg.sh` (after the build) once you have the
certificate. Until then, leave them out — the rest of the pipeline is unaffected.

---

## 5. Deploy / publish

1. Build `dist/PatchworkPDF-<version>-Intel.dmg` on an Intel Mac and
   `dist/PatchworkPDF-<version>-AppleSilicon.dmg` on an Apple Silicon Mac.
2. Upload both to the SourceForge **files** root alongside the Windows installer
   and `changelog.docx`. The arch is in the filename, so a visitor can tell at a
   glance which one they need.
3. Bump `version.json` before building so the artifact name and reported version
   agree. The update checker's macOS branch looks for
   `PatchworkPDF-<version>[-Intel|-AppleSilicon].dmg` and only offers a user the
   DMG matching the architecture they are running — so if you publish a new
   version for one architecture only, users on the other are correctly told they
   are still up to date rather than being handed an app that won't run.
   Unsuffixed DMGs from before this scheme are still recognised.

---

## 6. Per-user data locations (macOS)

Runtime state lives in `~/Library/Application Support/PatchworkPDF/`
(`platform_paths.user_data_dir()`): `logfile.log`, `recent_files.json`,
`license_accepted.json`, `update_prefs.json`, `lang.txt`, `instance.lock`,
`instance.json`.

---

## 7. Release checklist

- [ ] `version.json` bumped
- [ ] `python3 tools/run_tests.py` passes (the DMG build runs this first and stops if it fails)
- [ ] `bash packaging/macos/make_dmg.sh` succeeds
- [ ] `.app` launches on a **second** Mac (clean, same arch)
- [ ] Open/edit/save a PDF; OCR (Tesseract present); import `.docx`/`.xlsx`
      (LibreOffice present); tab tear-off opens a new window; window uses the
      **native** title bar with OS min/max/close
- [ ] Signed + notarized **or** the `xattr` workaround documented for users
- [ ] Uploaded to SourceForge with the matching `changelog.docx`
