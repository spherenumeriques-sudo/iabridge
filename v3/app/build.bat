@echo off
REM ── IABridge Desktop — Build script Windows ──
REM
REM Prérequis : Python 3.11+ installé, dans le PATH
REM Ce script crée un venv, installe les deps, compile en .exe

echo === IABridge Desktop Build ===
echo.

REM Créer le venv si absent
if not exist "venv\" (
    echo [1/5] Création du venv...
    python -m venv venv
)

REM Activer le venv
call venv\Scripts\activate.bat

REM Installer les dépendances
echo [2/5] Installation des dépendances...
pip install -q pyinstaller pywebview[cef] pystray pillow psutil ^
    websockets fastapi uvicorn aiosqlite ^
    pyautogui mss pyperclip pywin32 ^
    2>nul

REM Installer Playwright si pas déjà fait
pip install -q playwright 2>nul
python -m playwright install chromium 2>nul

REM Build
echo [3/5] Compilation PyInstaller...
python -m PyInstaller iabridge.spec --noconfirm

REM Copier vers AppData
echo [4/5] Installation dans %%APPDATA%%\IABridge\...
set TARGET=%APPDATA%\IABridge\app
if not exist "%TARGET%\" mkdir "%TARGET%"
xcopy /E /Y /Q dist\IABridge\* "%TARGET%\" >nul

REM Créer raccourci bureau
echo [5/5] Raccourci bureau...
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'IABridge.lnk')); $s.TargetPath = '%TARGET%\IABridge.exe'; $s.Description = 'IABridge Desktop'; $s.Save()"

echo.
echo === Build terminé ===
echo Exe : %TARGET%\IABridge.exe
echo Raccourci : Bureau\IABridge.lnk
echo.
pause
