@echo off
REM DLSS 5 Autopilot - build script
REM Requires Python 3.10+ and pyinstaller
cd /d "%~dp0"

echo [1/2] Checking PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo     not installed, installing...
    python -m pip install --quiet pyinstaller || goto :failed
)

echo [2/2] Building dlss5-autopilot.exe...
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name dlss5-autopilot ^
    --distpath "%~dp0" ^
    --workpath "%TEMP%\dlss5-autopilot-build" ^
    --specpath "%TEMP%\dlss5-autopilot-build" ^
    --noconfirm ^
    dlss5_autopilot.py || goto :failed

echo.
echo Done: "%~dp0dlss5-autopilot.exe"
pause
exit /b 0

:failed
echo.
echo BUILD FAILED.
pause
exit /b 1
