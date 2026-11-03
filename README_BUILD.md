# Patchwork PDF — Build & Distribution Guide

> **Per-platform build & deploy guides** (dependency handling, installers, publishing):
> [Windows](docs/BUILD_AND_DEPLOY_WINDOWS.md) ·
> [macOS](docs/BUILD_AND_DEPLOY_MACOS.md) ·
> [Linux/Flatpak](docs/BUILD_AND_DEPLOY_LINUX.md).
> Cross-platform design notes: [docs/CROSS_PLATFORM_PLAN.md](docs/CROSS_PLATFORM_PLAN.md).

## Prerequisites

Install all dependencies:

```bash
pip install -r requirements.txt
```

Or manually:

```bash
pip install flask pypdf pypdfium2 pikepdf Pillow pytesseract numpy reportlab python-docx openpyxl docx2pdf
```

**Windows only** (Office document import/export):

```bash
pip install pywin32
python Scripts/pywin32_postinstall.py -install
```

Install cx_Freeze on the machine you are building on:

```bash
pip install cx_Freeze
```

---

## Building a distributable

Run this on **each target platform** (Windows, macOS, Linux).
cx_Freeze links against the platform's own Python runtime and native
libraries, so cross-compilation is not supported.

```bash
python setup.py build
```

Output is written to `./build/exe.<platform>-<pyversion>/`.

### Windows

```bash
python setup.py build
# Output: build\exe.win-amd64-3.12\PatchworkPDF.exe
```

To build an MSI installer:

```bash
python setup.py bdist_msi
# Output: dist\PatchworkPDF-2.03-win64.msi
```

Alternatively, use the provided Inno Setup script to produce a full installer:

```bash
packaging\win\build_installer.bat
# Run from the repo root. Requires Inno Setup — see packaging\win\installer.iss
```

### macOS

```bash
python setup.py build
# Output: build/exe.macosx-*/PatchworkPDF

# Or as an .app bundle:
python setup.py bdist_mac
# Output: build/PatchworkPDF.app
```

### Linux

```bash
python setup.py build
# Output: build/exe.linux-x86_64-3.12/PatchworkPDF
```

---

## What gets bundled

| Component | Purpose |
|-----------|---------|
| `PatchworkPDF` / `PatchworkPDF.exe` | Main executable (built from `entry.py`) |
| `lib/` | All Python packages (Flask, pypdf, pypdfium2, pikepdf, Pillow, reportlab, …) |
| `lib/pypdfium2_raw/libpdfium.*` | Native pdfium binary (~5 MB) |
| `viewer.html` | Single-page application (viewer + editor UI) |
| `viewer.css` | Application stylesheet |
| `shared.js` | Shared JS utilities |
| `layout.js` | Layout/panel JS |
| `viewer-panel.js` | Viewer panel JS |
| `editor.js` | Editor JS |
| `assets/icon.ico` | Application icon |
| `docs/changelog.docx` | Bundled changelog (opened via Help menu) |
| `docs/PatchworkPDF_Manual.pdf` | Bundled user manual (opened via Help menu) |
| `version.json` | Version metadata |
| `LICENSE.txt` | Usage license + third-party notices (shown in About box / first-run dialog) |

---

## Distributing

Zip the entire build output folder and send it to users. No installation
required — just unzip and run the executable.

**Minimum distribution checklist:**
- [ ] `PatchworkPDF` (or `.exe`)
- [ ] `lib/` directory (entire folder)
- [ ] `viewer.html`
- [ ] `viewer.css`
- [ ] `shared.js`
- [ ] `layout.js`
- [ ] `viewer-panel.js`
- [ ] `editor.js`
- [ ] `assets/icon.ico`
- [ ] `version.json`
- [ ] `LICENSE.txt`

---

## Running without building (development)

```bash
python src/backend/entry.py
```

The app opens `http://127.0.0.1:5000` in your default browser automatically.

Optional environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PATCHWORK_PORT` | `5000` | Port the Flask server listens on |
| `PATCHWORK_NO_BROWSER` | _(unset)_ | Set to any value to suppress auto-open |
| `PATCHWORK_DEBUG` | _(unset)_ | Set to any value to enable Flask debug mode |

You can also pass a file path directly to pre-load it on startup:

```bash
python src/backend/entry.py path/to/document.pdf
```

---

## Project structure

| Path | Purpose |
|------|---------|
| `src/backend/entry.py` | Flask app, API routes, main entry point |
| `src/backend/routes/` | Flask feature routes (`viewer.py`, `editor.py`, `layout.py`) |
| `src/backend/pdf/` | PDF domain logic (`pdf_core.py`, `form_utils.py`, `ocr_utils.py`) |
| `src/backend/bootstrap/` | Startup & lifecycle (logging, splash, single-instance, recovery, boot timing) |
| `src/backend/platform_io/` | OS integration & paths (`platform_paths.py`, `win_chrome.py`, `font_assets.py`) |
| `src/backend/i18n/` | Server-side localization (`i18n_py.py`) |
| `src/backend/services/` | Support services (`check_for_updates.py`) |
| `src/frontend/viewer.html` | Single-page application HTML |
| `src/frontend/css/viewer.css` | Application stylesheet |
| `src/frontend/js/shared.js` | Shared JS utilities |
| `src/frontend/js/layout.js` | Layout/panel JS |
| `src/frontend/js/viewer-panel.js` | Viewer panel JS |
| `src/frontend/js/editor.js` | Editor JS |
| `setup.py` | cx_Freeze build config (cross-platform) |
| `packaging/win/installer.iss` | Inno Setup installer script |
| `packaging/win/build_installer.bat` | Helper to invoke Inno Setup |
| `requirements.txt` | Python dependencies |
| `version.json` | Version metadata |
| `LICENSE.txt` | Usage license + third-party notices |
| `assets/` | Icon files |
| `docs/` | User manual and changelog |

---

## Troubleshooting builds

### "DLL not found" / "libpdfium not found" on Windows
cx_Freeze should copy `pdfium.dll` automatically from the `pypdfium2_raw`
package. If it doesn't, manually copy `pdfium.dll` from your Python
`site-packages/pypdfium2_raw/` into the build folder next to the `.exe`.

### Missing modules at runtime
Run with `optimize=0` in `setup.py` to see every file cx_Freeze
processes, then add any missing package names to the `packages` list.

### App opens but shows blank / "page not found"
`viewer.html` and the JS/CSS files must be in the **same folder** as
the executable. Check that `include_files` in `setup.py` is copying them
correctly.

### macOS "App can't be opened because it is from an unidentified developer"
```bash
xattr -cr PatchworkPDF.app
# or sign with your Apple Developer certificate:
codesign --deep --force --sign "Developer ID Application: Your Name" PatchworkPDF.app
```

### Linux permission denied
```bash
chmod +x PatchworkPDF
./PatchworkPDF
```

---

## App icon

To add or replace the icon:

- **Windows:** Update `assets/icon.ico` (256×256 + smaller sizes). The path
  is set via `icon=` in the `Executable()` call in `setup.py`.
- **macOS:** Create `icon.icns` and set `icon="icon.icns"` in both
  `Executable()` and `bdist_mac_options`.
- **Linux:** Icons are typically set via a `.desktop` file, not the binary.

---

## Version history

| Version | Notes |
|---------|-------|
| 2.03 | Current release |
| 2.00 | Unified SPA — `index.html`/`editor.html` replaced by `viewer.html`; entry point renamed from `app.py` to `entry.py` |
| 1.0.0 | Initial distributable release |
