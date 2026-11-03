@echo off
setlocal enabledelayedexpansion

REM -- Run from the project root regardless of where this .bat is launched
REM This script lives in packaging\win\ but drives a root-relative build
REM (setup.py, build\, version.json, Output\ all live at the repo root).
cd /d "%~dp0..\.."

echo.
echo  ============================================================
echo    PatchworkPDF - Build Installer
echo  ============================================================
echo.

REM -- Read version from version.json
for /f "delims=" %%v in ('python -c "import json,sys; d=json.load(open('version.json')); print(d['version'])"') do set "APP_VERSION=%%v"
for /f "delims=" %%v in ('python -c "import json,sys; d=json.load(open('version.json')); print(d['version']+'.0.0')"') do set "APP_VERSION_INFO=%%v"

if not defined APP_VERSION (
  echo  ERROR: Could not read version from version.json
  pause
  exit /b 1
)

echo  Version: %APP_VERSION%
echo.

REM -- Step 1: Run the regression tests
REM Nothing gets packaged from a tree whose tests fail. This is the same gate
REM the macOS and Linux builds run (tools\run_tests.py), and it works with or
REM without pytest installed on the build machine.
echo  [1/5] Running regression tests...
echo.
python tools\run_tests.py --quiet
if errorlevel 1 goto :tests_failed
echo.
echo  Tests passed - proceeding with the build.

REM -- Step 2: Verify the OCR language packs
REM assets\tessdata\*.traineddata are ~11 MB binaries that cx_Freeze copies into
REM the build and the installer then ships wholesale. If they are absent the
REM build still succeeds and the installer still builds - it just silently loses
REM multi-language OCR. Fail here instead, where it is cheap to fix.
echo.
echo  [2/5] Verifying OCR language packs (assets\tessdata)...
echo.
python tools\fetch_tessdata.py --verify
if errorlevel 1 goto :no_tessdata

REM -- Step 3: Build the frozen executable
REM Start from an EMPTY build dir.  cx_Freeze writes over build\exe.win-* but
REM never removes anything, so a module that setup.py has since excluded (or a
REM frontend file that was deleted) stays behind from the previous build and
REM ships in the installer anyway.  A failed delete - usually a PatchworkPDF.exe
REM still running from build\ - must stop the build rather than fall back to an
REM in-place one.
echo.
echo  [3/5] Building PatchworkPDF executable (cx_Freeze)...
echo.
for /d %%i in (build\exe.win-*) do rmdir /s /q "%%i"
for /d %%i in (build\exe.win-*) do goto :clean_error
python setup.py build
if errorlevel 1 goto :build_error

REM -- Step 4: Locate the build output directory
echo.
echo  [4/5] Locating build output...
set "BUILD_DIR="
for /d %%i in (build\exe.win-*) do set "BUILD_DIR=%%i"

if not defined BUILD_DIR goto :no_build_dir
if not exist "%BUILD_DIR%\PatchworkPDF.exe" goto :no_exe

echo  Found: %BUILD_DIR%

REM -- Step 5: Locate Inno Setup
echo.
echo  [5/5] Building installer package (Inno Setup)...
echo.

REM Pre-expand Program Files paths into plain variables.
REM The ) in "Program Files (x86)" breaks CMD block-parsing even inside quotes;
REM using variables here keeps all (x86) out of if-condition source text.
set "PF=%ProgramFiles%"
set "PF86=%ProgramFiles(x86)%"

set "ISCC="
if exist "%PF86%\Inno Setup 6\ISCC.exe" set "ISCC=%PF86%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%PF%\Inno Setup 6\ISCC.exe"  set "ISCC=%PF%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%PF86%\Inno Setup 5\ISCC.exe" set "ISCC=%PF86%\Inno Setup 5\ISCC.exe"
if not defined ISCC if exist "%PF%\Inno Setup 5\ISCC.exe"  set "ISCC=%PF%\Inno Setup 5\ISCC.exe"

if not defined ISCC goto :no_iscc

echo  Using: %ISCC%
echo.

if not exist Output mkdir Output

"%ISCC%" "/DBuildDir=%BUILD_DIR%" "/DAppVersion=%APP_VERSION%" "/DAppVersionInfo=%APP_VERSION_INFO%" packaging\win\installer.iss
if errorlevel 1 goto :iscc_error

echo.
echo  ============================================================
echo    SUCCESS!
echo    Installer is ready in:  Output\
echo  ============================================================
echo.
pause
exit /b 0

REM -- Error handlers
REM  All error exits use goto labels so no (...) blocks are needed.
REM  This avoids CMD mis-parsing the ) in "Program Files (x86)" as a block close.

:tests_failed
echo.
echo  ============================================================
echo    BUILD ABORTED - regression tests failed
echo  ============================================================
echo.
echo  The failing tests are listed above. Nothing has been built.
echo.
echo  Re-run them on their own with:   python tools\run_tests.py
echo.
pause
exit /b 1

:no_tessdata
echo.
echo  ERROR: OCR language data is missing or corrupt (see above).
echo  The installer would ship without multi-language OCR support.
echo.
echo  Fix it with:   python tools\fetch_tessdata.py
echo  ^(downloads the pinned tessdata_fast packs into assets\tessdata^)
pause
exit /b 1

:clean_error
echo.
echo  ERROR: Could not delete the previous build under build\exe.win-*
echo  Close any PatchworkPDF running from build\ and re-run this script.
pause
exit /b 1

:build_error
echo.
echo  ERROR: cx_Freeze build failed. See output above.
pause
exit /b 1

:no_build_dir
echo.
echo  ERROR: No build output found under build\exe.win-*
echo  Ensure the cx_Freeze step completed without errors.
pause
exit /b 1

:no_exe
echo.
echo  ERROR: PatchworkPDF.exe not found in: %BUILD_DIR%
pause
exit /b 1

:no_iscc
echo.
echo  ERROR: Inno Setup not found. Checked:
echo    %PF86%\Inno Setup 6\
echo    %PF%\Inno Setup 6\
echo.
echo  Download Inno Setup 6 (free) from: https://jrsoftware.org/isinfo.php
echo  Install it, then re-run this script.
pause
exit /b 1

:iscc_error
echo.
echo  ERROR: Inno Setup compiler failed. See output above.
pause
exit /b 1
