# -*- mode: python ; coding: utf-8 -*-
"""
IABridge Desktop — spec PyInstaller

Build commande :
    cd v3/app
    pip install pyinstaller pywebview pystray pillow psutil websockets fastapi uvicorn aiosqlite pyautogui mss pyperclip pywin32 playwright
    python -m PyInstaller iabridge.spec

Résultat : dist/IABridge/IABridge.exe (one-folder mode)
"""
import os
from pathlib import Path

block_cipher = None
app_dir = os.path.dirname(os.path.abspath(SPEC))

# Données à inclure dans le bundle
datas = [
    (os.path.join(app_dir, 'ui', 'static'), 'ui/static'),
]

# Imports cachés que PyInstaller ne détecte pas automatiquement
hiddenimports = [
    'aiosqlite',
    'websockets',
    'websockets.legacy',
    'websockets.legacy.client',
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'fastapi',
    'starlette',
    'starlette.responses',
    'starlette.staticfiles',
    'pydantic',
    'pydantic_core',
    'webview',
    'pystray',
    'psutil',
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'PIL.ImageFont',
]

a = Analysis(
    [os.path.join(app_dir, 'main.py')],
    pathex=[app_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'numpy', 'scipy', 'pandas',
        'test', 'unittest', 'doctest',
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='IABridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # Pas de fenêtre console — on a pywebview
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(app_dir, 'icon.ico') if os.path.exists(os.path.join(app_dir, 'icon.ico')) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='IABridge',
)
