# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for HeyBuddy (one-file, windowed).

Build:  pyinstaller --noconfirm heybuddy.spec   ->  dist/HeyBuddy.exe

* sounddevice ships PortAudio as a private DLL: collect it explicitly.
* imageio-ffmpeg ships ffmpeg.exe as package data: collect it so spoken
  replies can be decoded on machines without ffmpeg.
* pystray picks its Win32 backend at runtime: hidden import.
* config/settings.json is bundled as the first-run template; the live
  settings are written to %APPDATA%\\HeyBuddy (never inside the bundle).
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

hiddenimports = [
    "pystray._win32",
    "websocket._abnf",
    "websocket._app",
    "websocket._core",
    "_sounddevice",
    "src.tools.selfcheck",
]

datas = [("config/settings.json", "config"), ("assets", "assets")]
datas += collect_data_files("sounddevice")
datas += collect_data_files("imageio_ffmpeg")

binaries = collect_dynamic_libs("sounddevice")

a = Analysis(
    ["src/main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest", "PyQt6.QtWebEngineCore", "PyQt6.Qt3DCore"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="HeyBuddy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX-compressed Qt DLLs sometimes fail to load
    runtime_tmpdir=None,
    console=False,        # tray app: no console window
    disable_windowed_traceback=False,
    icon="assets/heybuddy.ico" if Path("assets/heybuddy.ico").exists() else None,
    version="version_info.txt" if Path("version_info.txt").exists() else None,
)
