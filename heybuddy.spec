# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for HeyBuddy.

Build:

    pip install pyinstaller
    pyinstaller heybuddy.spec

The output lands in `dist/HeyBuddy.exe` (one-file build, no console window).

Why a spec file and not a CLI invocation:

* `sounddevice` ships a private PortAudio DLL that PyInstaller's hook usually
  picks up — but only if we point `collect_dynamic_libs` at it explicitly.
* PyQt6's platform plugins (`platforms/qwindows.dll`) are not always picked
  up by the default hook; `collect_data_files` is a safer bet.
* `pystray._win32` is a runtime-resolved backend; `hiddenimports` makes sure
  PyInstaller actually packages it.

If a build is missing a module at runtime, add it to `hiddenimports` here
rather than monkey-patching the build command.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

hiddenimports = [
    # pystray's platform backend is selected at runtime
    "pystray._win32",
    # websocket-client's transport submodules
    "websocket._abnf",
    "websocket._app",
    "websocket._core",
    # sounddevice resolves the PortAudio binding lazily
    "sounddevice",
    "_sounddevice",
    # pydub's MP3 decoder pulls audioop
    "audioop",
]

datas = []
datas += collect_data_files("PyQt6", includes=["Qt6/plugins/platforms/*"])
datas += collect_data_files("sounddevice")

binaries = []
binaries += collect_dynamic_libs("sounddevice")

a = Analysis(
    ["src/main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavyweight stdlib bits we don't need; trimming saves ~10 MB.
        "tkinter",
        "test",
        "unittest",
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="HeyBuddy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX-compressed PyQt6 DLLs occasionally fail to load
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/tray.ico" if __import__("pathlib").Path("assets/tray.ico").exists() else None,
)
