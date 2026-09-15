@echo off
echo ============================
echo    ClipMerger - Build Tool
echo ============================
echo.

echo [0/2] Attivazione ambiente virtuale...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERRORE: venv non trovato. Assicurati di avere il venv nella cartella del progetto.
    pause
    exit /b 1
)

echo [1/2] Installazione PyInstaller (se mancante)...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    pip install pyinstaller
)

echo [2/2] Compilazione con PyInstaller...
pyinstaller ClipMerger.spec
if errorlevel 1 (
    echo ERRORE: Compilazione fallita.
    pause
    exit /b 1
)

echo.
echo Build completata! Eseguibile in dist\ClipMerger\ClipMerger.exe
pause
exit /b 0
