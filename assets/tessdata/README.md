# Bundled Tesseract language data (OCR)

This folder holds the Tesseract `.traineddata` files that let OCR recognise
scanned documents in each supported UI language. `ocr_utils.py` auto-detects
this directory and points Tesseract at it with `--tessdata-dir`, requesting the
active UI language combined with English (e.g. `deu+eng`).

## Required files

These are part of the source tree — they are **not** git-ignored and they are
**not** optional for a release build. To restore or verify them on any platform:

```bash
python tools/fetch_tessdata.py            # download whatever is missing
python tools/fetch_tessdata.py --verify   # check only; non-zero exit if not OK
```

They come from the **tessdata_fast** repository — the recommended balance of
speed and accuracy for Tesseract 4/5 — pinned to the `4.1.0` tag so the
checksums stay stable:

| File | Language | SHA-256 |
|------|----------|---------|
| `eng.traineddata` | English | `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2` |
| `spa.traineddata` | Spanish | `6f2e04d02774a18f01bed44b1111f2cd7f3ba7ac9dc4373cd3f898a40ea6b464` |
| `fra.traineddata` | French  | `ced037562e8c80c13122dece28dd477d399af80911a28791a66a63ac1e3445ca` |
| `deu.traineddata` | German  | `19d219bbb6672c869d20a9636c6816a81eb9a71796cb93ebe0cb1530e2cdb22d` |
| `ita.traineddata` | Italian | `b8f89e1e785118dac4d51ae042c029a64edb5c3ee42ef73027a6d412748d8827` |

Source: https://github.com/tesseract-ocr/tessdata_fast/tree/4.1.0

The language list mirrors `_UI_TO_TESS` in `src/backend/pdf/ocr_utils.py`. To
add a language, add it in all three places: `_UI_TO_TESS`, `LANGS` in
`tools/fetch_tessdata.py`, and the `tessdata` module in
`packaging/flatpak/com.patchworkpdf.app.yaml`.

## How each platform ships them

| Platform | Mechanism |
|----------|-----------|
| Windows  | `setup.py` copies this folder into the cx_Freeze build; `packaging\win\installer.iss` ships the whole build tree. `packaging\win\build_installer.bat` runs `fetch_tessdata.py --verify` first and refuses to build without them. |
| macOS    | Same `setup.py` copy; `bdist_mac` carries it into `PatchworkPDF.app/Contents/MacOS/assets/tessdata`. |
| Linux    | The Flatpak manifest's `tessdata` module downloads the same pinned files (a Flatpak build cannot use this script — network is only allowed for declared sources). |

## Important notes

- **`eng.traineddata` is mandatory.** With `--tessdata-dir` pointing here,
  Tesseract looks *only* in this folder, so English must be present for the
  `<lang>+eng` request to work at all.
- If one of the other packs is missing, that language degrades to English —
  the app never fails to OCR because of a missing file.
- If this whole folder is absent (e.g. a stripped checkout), the app falls back
  to whatever the user's installed Tesseract provides.
- `setup.py` prints a `[setup] WARNING:` line naming any missing pack rather
  than skipping the folder silently, so a deficient build is visible in the log.

Tesseract itself is still a user-installed dependency on Windows and macOS (see
the "Install Tesseract" link in the app); this folder only supplies the
*language data*. On Linux the Flatpak builds Tesseract into the sandbox.
