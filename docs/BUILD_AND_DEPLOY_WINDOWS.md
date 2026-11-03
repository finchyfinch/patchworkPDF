# Build & Deploy — Windows

How to build Patchwork PDF into a distributable `.exe` and installer on Windows.
For the design behind the cross-platform work see
[CROSS_PLATFORM_PLAN.md](CROSS_PLATFORM_PLAN.md).

---

## 1. Prerequisites

| Requirement | Notes |
|-------------|-------|
| Windows 10 / 11 (x64) | Build on the same OS you ship — cx_Freeze cannot cross-compile. |
| Python 3.12 (64-bit) | Match the interpreter bitness to your target. |
| Microsoft Edge WebView2 Runtime | The window backend. Pre-installed on current Windows; the installer can also carry the bootstrapper (see §4). |
| Inno Setup 6 (optional) | For the `.exe` installer — https://jrsoftware.org/isinfo.php |
| Microsoft Office (optional) | Only for high-fidelity Word/Excel **import** via COM. Without it the app falls back to LibreOffice, then to "convert manually". |
| Tesseract OCR (optional) | Only for OCR of scanned PDFs. Install to `C:\Program Files\Tesseract-OCR`. |
| OCR language packs (**required for release builds**) | `assets\tessdata\*.traineddata` — part of the source tree. Restore/verify with `python tools\fetch_tessdata.py`. |

### Install Python dependencies
```bat
python -m pip install --upgrade pip
pip install -r requirements.txt
python Scripts\pywin32_postinstall.py -install
```
`requirements.txt` uses environment markers, so on Windows it automatically pulls
`pywin32` (COM Office import) and `pythonnet` (the WebView2/.NET bridge). The
macOS/Linux-only packages are skipped.

---

## 2. How dependencies are satisfied

| Dependency | How it ships |
|------------|--------------|
| Python + all pip libraries | **Frozen into the build** by cx_Freeze (`lib/`). |
| pdfium, QPDF (pikepdf) native DLLs | **Bundled** by `setup.py` (`lib/pypdfium2_raw`, `lib/pikepdf.libs`). |
| WebView2 interop + pythonnet .NET DLLs | **Bundled** by `setup.py` (`lib/webview/lib`, `lib/pythonnet`). |
| WebView2 **runtime** | System component; ship the Evergreen bootstrapper in the installer for offline machines (§4). |
| Tesseract / LibreOffice | **Not bundled** — detected at runtime; optional. |
| OCR language data (`eng/spa/fra/deu/ita`) | **Bundled** by `setup.py` from `assets\tessdata` into `assets\tessdata` beside the `.exe`; the installer ships the whole build tree. The app points Tesseract at it with `--tessdata-dir` and requests `<uilang>+eng`. |

The end user does **not** install Python or any pip package — everything is in the
build folder.

---

## 3. Build the application

```bat
python setup.py build
```
Output: `build\exe.win-amd64-3.12\PatchworkPDF.exe` (plus `lib\`, `assets\`, the
HTML/JS/CSS, `locales\`, `docs\`, `version.json`, `LICENSE.txt`).

Quick sanity run:
```bat
build\exe.win-amd64-3.12\PatchworkPDF.exe
```

### Troubleshooting
- **`[setup] WARNING: missing OCR language packs`** — the build is short of one
  or more `assets\tessdata\*.traineddata`. Run `python tools\fetch_tessdata.py`
  and rebuild. Missing `eng` breaks bundled OCR entirely; a missing other
  language silently degrades that UI language to English.
- **"libpdfium not found"** — confirm `lib\pypdfium2_raw\` copied; see `setup.py`.
- **Missing module at runtime** — add the package name to `packages` in `setup.py`
  and rebuild.
- **Blank window** — `viewer.html` + the JS/CSS must sit next to the `.exe`.

---

## 4. Package an installer

### Option A — Inno Setup (recommended)
```bat
REM run from the repo root
packaging\win\build_installer.bat   REM invokes Inno Setup on packaging\win\installer.iss
```
Step 1 of that script runs the regression suite (`python tools\run_tests.py`)
and aborts before building anything if it fails — so an installer can never be
cut from a tree whose tests are red. Run the suite on its own any time with
`python tools\run_tests.py`.

Produces `PatchworkPDF_Setup_<version>.exe`. This filename is what the in-app
update checker looks for on SourceForge — keep the `PatchworkPDF_Setup_<version>.exe`
pattern.

To make a fresh machine fully self-sufficient, add the WebView2 Evergreen
bootstrapper to `packaging\win\installer.iss` and run it in the `[Run]` section (download
`MicrosoftEdgeWebview2Setup.exe` from Microsoft; one is already vendored under
`assets/`). This installs the runtime silently if absent.

### Option B — MSI
```bat
python setup.py bdist_msi
```
Output: `dist\PatchworkPDF-<version>-win64.msi`.

---

## 5. Deploy / publish

1. Build `PatchworkPDF_Setup_<version>.exe`.
2. Upload it to the SourceForge **files** root:
   `https://sourceforge.net/projects/patchworkpdf/files/`
3. Upload the updated `changelog.docx` there too (the update checker reads release
   notes from it).
4. Bump `version.json` **before** building so the artifact name and the in-app
   version agree.

The update checker (`check_for_updates.py`) reads the SourceForge RSS feed, finds
the newest `PatchworkPDF_Setup_<version>.exe`, and prompts users running an older
version.

---

## 6. Per-user data locations (Windows)

Runtime state lives in `%LOCALAPPDATA%\PatchworkPDF\` — unchanged from earlier
versions (see `platform_paths.user_data_dir()`):
`logfile.log`, `recent_files.json`, `license_accepted.json`, `update_prefs.json`,
`lang.txt`, `instance.lock`, `instance.json`.

---

## 7. Release checklist

- [ ] `version.json` bumped
- [ ] `python tools\run_tests.py` passes (the installer build runs this first and stops if it fails)
- [ ] `python tools\fetch_tessdata.py --verify` passes (OCR language packs intact)
- [ ] `python setup.py build` succeeds with no `[setup] WARNING:` lines, and the `.exe` launches
- [ ] Open/edit/save a PDF; OCR a scan; import a `.docx`/`.xlsx`; tear off a tab
- [ ] `assets\tessdata\` is present in the build output and in the installed folder
- [ ] `packaging\win\build_installer.bat` produces `PatchworkPDF_Setup_<version>.exe`
- [ ] Installer tested on a clean VM (incl. WebView2 bootstrapping)
- [ ] Uploaded to SourceForge with the matching `changelog.docx`
