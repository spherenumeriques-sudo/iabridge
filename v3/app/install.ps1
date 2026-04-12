# ── IABridge Desktop — Script d'installation PowerShell ──
# Utilisation : powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"

Write-Host "=== IABridge Desktop Install ===" -ForegroundColor Cyan
Write-Host ""

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target = Join-Path $env:APPDATA "IABridge\app"

# Venv
if (!(Test-Path (Join-Path $AppDir "venv"))) {
    Write-Host "[1/6] Création du venv..." -ForegroundColor Yellow
    python -m venv (Join-Path $AppDir "venv")
}

# Activation
$activate = Join-Path $AppDir "venv\Scripts\Activate.ps1"
& $activate

# Dépendances
Write-Host "[2/6] Installation des dépendances..." -ForegroundColor Yellow
pip install -q pyinstaller pywebview pystray pillow psutil `
    websockets fastapi uvicorn aiosqlite `
    pyautogui mss pyperclip pywin32 2>$null

# Playwright
Write-Host "[3/6] Installation Playwright..." -ForegroundColor Yellow
pip install -q playwright 2>$null
python -m playwright install chromium 2>$null

# Build
Write-Host "[4/6] Compilation PyInstaller..." -ForegroundColor Yellow
python -m PyInstaller (Join-Path $AppDir "iabridge.spec") --noconfirm

# Install
Write-Host "[5/6] Installation dans $Target..." -ForegroundColor Yellow
if (!(Test-Path $Target)) { New-Item -ItemType Directory -Path $Target -Force | Out-Null }
Copy-Item -Path (Join-Path $AppDir "dist\IABridge\*") -Destination $Target -Recurse -Force

# Raccourci bureau
Write-Host "[6/6] Raccourci bureau..." -ForegroundColor Yellow
$Desktop = [Environment]::GetFolderPath("Desktop")
$Shortcut = Join-Path $Desktop "IABridge.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Lnk = $Shell.CreateShortcut($Shortcut)
$Lnk.TargetPath = Join-Path $Target "IABridge.exe"
$Lnk.Description = "IABridge Desktop"
$Lnk.WorkingDirectory = $Target
$Lnk.Save()

# Tâche planifiée au logon
Write-Host ""
$addTask = Read-Host "Créer une tâche planifiée pour démarrer IABridge au logon ? (O/n)"
if ($addTask -ne "n") {
    $action = New-ScheduledTaskAction -Execute (Join-Path $Target "IABridge.exe")
    $trigger = New-ScheduledTaskTrigger -AtLogon
    Register-ScheduledTask -TaskName "IABridge Desktop" -Action $action -Trigger $trigger -Description "IABridge Desktop Agent" -RunLevel Limited -Force | Out-Null
    Write-Host "Tâche planifiée créée." -ForegroundColor Green
}

Write-Host ""
Write-Host "=== Installation terminée ===" -ForegroundColor Green
Write-Host "Exe    : $Target\IABridge.exe"
Write-Host "Bureau : $Shortcut"
Write-Host ""
