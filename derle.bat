@echo off
REM DLSS 5 Kurulum Araci - exe derleme
REM Gereken: Python 3.10+  ve  pip install pyinstaller
cd /d "%~dp0"

echo [1/2] PyInstaller kontrol ediliyor...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo     kurulu degil, kuruluyor...
    python -m pip install --quiet pyinstaller || goto :hata
)

echo [2/2] dlss5kur.exe derleniyor...
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name dlss5kur ^
    --distpath "%~dp0" ^
    --workpath "%TEMP%\dlss5kur_build" ^
    --specpath "%TEMP%\dlss5kur_build" ^
    --noconfirm ^
    dlss5kur.py || goto :hata

echo.
echo Bitti: "%~dp0dlss5kur.exe"
pause
exit /b 0

:hata
echo.
echo DERLEME BASARISIZ.
pause
exit /b 1
