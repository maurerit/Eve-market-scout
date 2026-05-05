# -*- mode: python ; coding: utf-8 -*-
# EVE Market Scout - PyInstaller Spec File
#
# Build with: pyinstaller EVE-Market-Scout.spec
#
# This creates a folder distribution (not single file) for faster startup.

import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Collect all aiohttp submodules (it has many hidden imports)
aiohttp_hiddenimports = collect_submodules('aiohttp')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Include sound file if it exists (optional)
        # ('SELL.WAV', '.'),
    ],
    hiddenimports=[
        # aiohttp and its dependencies
        *aiohttp_hiddenimports,
        'aiohttp',
        'asyncio',
        'multidict',
        'yarl',
        'async_timeout',
        'charset_normalizer',
        'aiosignal',
        'frozenlist',
        # requests and dependencies
        'requests',
        'urllib3',
        'certifi',
        'idna',
        # tkinter (usually auto-detected but be safe)
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        # Standard library that might be missed
        'sqlite3',
        'bz2',
        'statistics',
        'json',
        'csv',
        'hashlib',
        'secrets',
        'base64',
        'dataclasses',
        'enum',
        'pathlib',
        'webbrowser',
        'http.server',
        # Matplotlib for price history charts
        'matplotlib',
        'matplotlib.pyplot',
        'matplotlib.backends.backend_tkagg',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unnecessary modules to reduce size
        # NOTE: numpy is required by matplotlib - do not exclude
        'pandas',
        'PIL',
        'scipy',
        'pytest',
        'unittest',
        'IPython',
        'notebook',
        'sphinx',
        'docutils',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EVE-Market-Scout',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,  # Compress executable (smaller size)
    console=False,  # Set to True if you want to see print() output for debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Uncomment and set path to add an icon (Windows .ico, Linux .png)
    # icon='icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='EVE-Market-Scout',
)
