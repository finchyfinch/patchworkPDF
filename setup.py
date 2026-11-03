"""
Lean cx_Freeze setup.py for Patchwork PDF
----------------------------------------

Build:
    python setup.py build

This version avoids over-bundling and relies on cx_Freeze's
dependency detection wherever possible.
"""

import sys
import pathlib
from cx_Freeze import setup, Executable

# The Python backend lives under src/backend/ as top-level packages
# (bootstrap, platform_io, pdf, routes, i18n, services) plus entry.py.
# Put that directory on the path so cx_Freeze's module finder — and the
# `includes` / `packages` lists below — resolve them exactly as the frozen
# runtime will (the exe runs src/backend/entry.py with src/backend on sys.path).
_BACKEND_DIR = pathlib.Path(__file__).parent / "src" / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pkg_dir(name):
    """Return the directory of an installed package."""
    import importlib.util
    spec = importlib.util.find_spec(name)
    if spec and spec.submodule_search_locations:
        return pathlib.Path(list(spec.submodule_search_locations)[0])
    if spec and spec.origin:
        return pathlib.Path(spec.origin).parent
    raise FileNotFoundError(f"Cannot locate package: {name}")


def _ext_file_excludes(*modules):
    """Exclude entries that drop compiled extension modules from the build.

    Excluding "PIL._avif" stops cx_Freeze importing it, but it still copies the
    .pyd/.so across as package DATA — and that copy skips only exact file names
    (freezer._copy_package_data).  So name the file itself too, once per
    extension suffix this interpreter uses (cp314-win_amd64.pyd, .so, ...)."""
    import importlib.machinery
    return [m + s for m in modules
            for s in importlib.machinery.EXTENSION_SUFFIXES]


# ---------------------------------------------------------------------------
# Include files (ONLY non-Python assets or required binaries)
# ---------------------------------------------------------------------------

include_files = []

app_dir = pathlib.Path(__file__).parent

# Your frontend / app assets.  Frontend files live under src/frontend/ and are
# copied to the same relative path next to the exe so the Flask routes (which
# resolve them via FRONTEND_DIR = APP_DIR/src/frontend) work in the frozen build.
for asset in ["src/frontend/viewer.html",
              "src/frontend/css/viewer.css",
              "src/frontend/js/layout.js",
              "src/frontend/js/shared.js",
              "src/frontend/js/viewer-panel.js",
              "src/frontend/js/i18n.js",
              "assets/icon.ico",
              "docs/changelog.docx",
              "docs/PatchworkPDF_Manual.pdf",
              "version.json",
              "LICENSE.txt"]:
    path = app_dir / asset
    if path.exists():
        include_files.append((str(path), asset))

# Bundled editor fonts (Option B — font matching): copy the whole assets/fonts
# directory of .ttf faces + licenses so the embed/preview paths work when frozen.
_fonts_dir = app_dir / "assets" / "fonts"
if _fonts_dir.exists():
    include_files.append((str(_fonts_dir), "assets/fonts"))

# Localization: bundle the whole locales/ directory of per-language UI JSON.
_locales_dir = app_dir / "locales"
if _locales_dir.exists():
    include_files.append((str(_locales_dir), "locales"))

# OCR language data: bundle assets/tessdata (the Tesseract .traineddata packs so
# scanned documents can be recognised in each UI language). See its README.
#
# Report loudly on anything missing.  These are ~11 MB binaries that a checkout
# can easily lack, and a silent skip here yields an installer that runs fine but
# only ever OCRs in English — the failure surfaces on a user's machine, not in
# the build log.  `eng` is special: ocr_utils passes --tessdata-dir, which stops
# Tesseract looking anywhere else, so without it the "<uilang>+eng" request has
# no English half at all.  tools/fetch_tessdata.py fetches/verifies the set.
_TESSDATA_LANGS = ("eng", "spa", "fra", "deu", "ita")   # see tools/fetch_tessdata.py
_tessdata_dir = app_dir / "assets" / "tessdata"
_tessdata_missing = [c for c in _TESSDATA_LANGS
                     if not (_tessdata_dir / f"{c}.traineddata").is_file()]
if _tessdata_dir.exists():
    include_files.append((str(_tessdata_dir), "assets/tessdata"))
if _tessdata_missing:
    print("[setup] WARNING: missing OCR language packs in assets/tessdata: "
          + ", ".join(_tessdata_missing))
    # ASCII only in these prints: the Windows build console is cp1252/cp437 and
    # mangles non-ASCII punctuation.
    print("[setup] WARNING: OCR in those UI languages will fall back to English"
          + (" -- and eng.traineddata itself is missing, so bundled OCR will not "
             "work at all" if "eng" in _tessdata_missing else ""))
    print("[setup] Fix with:  python tools/fetch_tessdata.py")
else:
    print(f"[setup] Bundling OCR language packs: {', '.join(_TESSDATA_LANGS)}")

# ---------------------------------------------------------------------------
# pypdfium2 native binary handling (IMPORTANT - keep this)
# ---------------------------------------------------------------------------

try:
    raw_dir = pkg_dir("pypdfium2_raw")
    include_files.append((str(raw_dir), "lib/pypdfium2_raw"))
    print(f"[setup] Using pypdfium2_raw from {raw_dir}")
except FileNotFoundError:
    # Fallback for older layouts
    pdfium_dir = pkg_dir("pypdfium2")
    for suffix in ("*.dll", "*.so", "*.dylib", "*.pyd"):
        for lib in pdfium_dir.rglob(suffix):
            include_files.append((str(lib), f"lib/pypdfium2/{lib.name}"))
            print(f"[setup] Bundling pdfium binary: {lib.name}")

# ---------------------------------------------------------------------------
# pikepdf / QPDF native DLLs (delvewheel companion folder) — WINDOWS ONLY.
# The delvewheel patch in pikepdf/__init__.py looks for DLLs at
#   <pikepdf_dir>/../pikepdf.libs/
# In a cx_Freeze build pikepdf lands at lib/pikepdf/, so the DLLs must be
# placed at lib/pikepdf.libs/ to match that relative path.
# On macOS (delocate → pikepdf.dylibs) and Linux (auditwheel → pikepdf.libs)
# the shared libraries are laid out differently and cx_Freeze's own dependency
# scanner picks them up reliably, so no manual copy is needed there.
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    pikepdf_libs = pkg_dir("pikepdf").parent / "pikepdf.libs"
    if pikepdf_libs.exists():
        for dll in sorted(pikepdf_libs.glob("*.dll")):
            include_files.append((str(dll), f"lib/pikepdf.libs/{dll.name}"))
            print(f"[setup] Bundling pikepdf DLL: {dll.name}")
    else:
        print("[setup] WARNING: pikepdf.libs not found — qpdf DLLs will be missing from build")

# ---------------------------------------------------------------------------
# Packages (keep minimal - add only if runtime errors occur)
# ---------------------------------------------------------------------------

packages = [
    "flask",
    "pypdf",
    "pypdfium2",
    "pikepdf",
    "PIL",
    "numpy",
    "reportlab",
    "pytesseract",
    "docx",
    "openpyxl",
    # Ruled-table detection for Export-to-Excel.  pdfminer.six ships cmap data
    # files (whole-package copy needed); cryptography is compiled; charset_normalizer
    # is imported dynamically by pdfminer — list them so cx_freeze bundles them all.
    "pdfplumber",
    "pdfminer",
    "cryptography",
    "charset_normalizer",
    "email",  # required by http.client/http.server (Werkzeug dependency)
    # Native window stack (replaces OS-browser launch)
    "webview",
    # ── First-party backend packages (src/backend/) — list them so cx_Freeze
    #    bundles every submodule even the ones imported lazily inside functions.
    "bootstrap",
    "platform_io",
    "pdf",
    "routes",
    "i18n",
    "services",
]

# Windows: pywin32 (COM Office import) + the pythonnet/.NET bridge that the
# edgechromium (WebView2) backend needs.  These packages don't exist on other
# OSes, so listing them unconditionally would break the mac/Linux build.
if sys.platform == "win32":
    packages += [
        # win32com.client only — the Office import just Dispatches.  Listing the
        # whole "win32com" package also dragged in its test/demo/shell trees and,
        # through them, the pythonwin MFC layer (win32ui.pyd + mfc140u.dll, ~6.7 MB).
        "win32com.client",
        "pythoncom",
        "pywintypes",
        "clr",          # pythonnet's compiled module — needed by the edgechromium backend
        "clr_loader",
        "pythonnet",
    ]
# macOS: the cocoa (WKWebView) backend bridges through pyobjc.
elif sys.platform == "darwin":
    packages += [
        "objc",
        "Foundation",
        "AppKit",
        "WebKit",
    ]
# Linux: the gtk (WebKitGTK) backend uses PyGObject ("gi"), which is provided by
# the system / Flatpak runtime rather than frozen in — so it is not listed here.

# ---------------------------------------------------------------------------
# pywebview native interop DLLs + pythonnet runtime — WINDOWS ONLY.
# pywebview loads WebView2 interop assemblies from <webview>/lib at runtime, and
# the edgechromium backend bridges to .NET via pythonnet.  cx_Freeze does NOT
# auto-detect these, so bundle them.  On macOS (cocoa) and Linux (gtk) there is
# no WebView2/.NET layer, so this whole section is skipped.
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    try:
        _wv_lib = pkg_dir("webview") / "lib"
        if _wv_lib.exists():
            include_files.append((str(_wv_lib), "lib/webview/lib"))
            print(f"[setup] Bundling pywebview lib from {_wv_lib}")
        else:
            print("[setup] WARNING: webview/lib not found — WebView2 interop DLLs missing")
    except FileNotFoundError:
        print("[setup] WARNING: pywebview not installed — window backend will be missing")

    try:
        _pynet = pkg_dir("pythonnet")
        for _f in _pynet.rglob("*.dll"):
            include_files.append((str(_f), f"lib/pythonnet/{_f.relative_to(_pynet)}"))
            print(f"[setup] Bundling pythonnet DLL: {_f.name}")
    except FileNotFoundError:
        print("[setup] WARNING: pythonnet not installed — .NET bridge will be missing")

# ---------------------------------------------------------------------------
# Build options
# ---------------------------------------------------------------------------

build_exe_options = {
    "packages": packages,
    # win_chrome is imported lazily (inside functions) so name it explicitly to
    # guarantee cx_Freeze bundles it.  single_instance is imported before the
    # heavy app imports — named here too so the fast second-instance exit can
    # never be lost to ModuleFinder quirks.  i18n_py is imported lazily from
    # entry/single_instance/ocr_utils for the server-side translations.
    # platform_paths is imported lazily from most call sites (data-dir helpers),
    # so name it too — it is a core module the app cannot start without.
    "includes": ["platform_io.win_chrome", "bootstrap.single_instance",
                 "i18n.i18n_py", "platform_io.platform_paths"],
    "include_files": include_files,
    "excludes": [
        "tkinter",
        "unittest",
        "pydoc",
        "doctest",
        "distutils",
        "lib2to3",
        "test",
        "tests",
        "pandas",
        "scipy",
        "matplotlib",
        "IPython",
        "notebook",
        "jupyter",
        "cv2",
        # ── Optional imports of bundled packages that the app never reaches.
        #    Each is pulled in only because a package above is listed whole.
        #    Names that don't exist on a platform are simply ignored.
        "pygments",              # 4.9 MB — only reportlab.lib.pygments2xpre
        "yaml",                  # numpy.__config__.show() only
        # cffi's build-time helper (_shimmed_dist_utils) imports setuptools,
        # which brings its vendored tree.  Keep cffi + pycparser themselves:
        # clr_loader (the .NET bridge) runs ffi.cdef() at runtime.
        "setuptools", "pkg_resources", "_distutils_hack", "wheel",
        "jaraco", "backports", "more_itertools", "platformdirs",
        "PIL._avif",             # 7.7 MB AVIF codec; AVIF is not an accepted
                                 # import type and Pillow degrades to "unsupported"
        # python-docx / openpyxl use lxml.etree only.
        "lxml.objectify", "lxml.html", "lxml.isoschematron", "lxml.includes",
        # pythonwin (MFC) — reached only via win32com's test/demo code.
        "win32ui", "win32uiole", "dde", "pywin",
        "win32com.test", "win32com.demos", "win32com.servers",
        "numpy._core.tests",
        "_pyrepl", "xmlrpc", "colorama", "pypdfium2_cli",
    ] + _ext_file_excludes("PIL._avif", "lxml.objectify"),
    "include_msvcr": True,
    "optimize": 1,
}

# ---------------------------------------------------------------------------
# Executable config
# ---------------------------------------------------------------------------

import cx_Freeze as _cxf
import json as _json
_cxf_ver = tuple(int(x) for x in getattr(_cxf, '__version__', '6.0').split('.')[:2])

# Single-source the version from version.json so every artifact name matches.
try:
    with open(app_dir / "version.json", encoding="utf-8") as _vf:
        _APP_VERSION = str(_json.load(_vf).get("version", "0.0.0"))
except Exception:
    _APP_VERSION = "0.0.0"

# Platform-appropriate application icon (None if the file is absent, so the
# build still succeeds before the icons have been generated).
_ICON_BY_PLATFORM = {
    "win32":  app_dir / "assets" / "icon.ico",
    "darwin": app_dir / "assets" / "icon.icns",
}
_icon = _ICON_BY_PLATFORM.get(sys.platform)
_icon = str(_icon) if (_icon and _icon.exists()) else None

base = None
if sys.platform == "win32":
    base = "gui" if _cxf_ver >= (7, 0) else "Win32GUI"

executables = [
    Executable(
        script=str(_BACKEND_DIR / "entry.py"),
        base=base,
        target_name="PatchworkPDF",
        icon=_icon,
        uac_admin=False,
    )
]

# ---------------------------------------------------------------------------
# macOS bundle options — .app via `python setup.py bdist_mac`
# ---------------------------------------------------------------------------

_icns = app_dir / "assets" / "icon.icns"
bdist_mac_options = {
    "bundle_name": "PatchworkPDF",
    "plist_items": [
        ("CFBundleName", "PatchworkPDF"),
        ("CFBundleDisplayName", "Patchwork PDF"),
        ("CFBundleIdentifier", "com.patchworkpdf.app"),
        ("CFBundleVersion", _APP_VERSION),
        ("CFBundleShortVersionString", _APP_VERSION),
        ("NSHighResolutionCapable", True),
        ("LSMinimumSystemVersion", "12.0"),
        # Register as a PDF handler so "Open With", Finder double-click and
        # dock-drop route the file in on argv (single_instance.find_initial_file).
        ("CFBundleDocumentTypes", [
            {
                "CFBundleTypeName": "PDF document",
                "CFBundleTypeRole": "Editor",
                "LSItemContentTypes": ["com.adobe.pdf"],
                "LSHandlerRank": "Alternate",
            },
        ]),
    ],
}
if _icns.exists():
    bdist_mac_options["iconfile"] = str(_icns)

# ---------------------------------------------------------------------------
# Windows MSI options (optional)
# ---------------------------------------------------------------------------

bdist_msi_options = {
    "upgrade_code": "{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}",
    "add_to_path": False,
    "initial_target_dir": r"[ProgramFilesFolder]\PatchworkPDF",
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

setup(
    name="PatchworkPDF",
    version=_APP_VERSION,
    description="Patchwork PDF — a PDF page editor",
    author="Gareth Finch",
    options={
        "build_exe": build_exe_options,
        "bdist_mac": bdist_mac_options,
        "bdist_msi": bdist_msi_options,
    },
    executables=executables,
)