@echo off
REM Convenience wrapper: build the frozen exe from the repo root.
REM setup.py stays at the project root (it is the shared cx_Freeze config used
REM by CI and the macOS build too), so hop up two levels before invoking it.
cd /d "%~dp0..\.."
python setup.py build
pause
