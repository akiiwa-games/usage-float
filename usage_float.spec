# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

spec_dir = Path(SPECPATH)

a = Analysis(
    [str(spec_dir / "usage_float.py")],
    pathex=[str(spec_dir)],
    binaries=[],
    datas=[(str(spec_dir / "wallpaper-start.lua"), ".")],
    hiddenimports=[
        "tkinter",
        "tkinter.ttk",
        "tkinter.font",
        "tkinter.filedialog",
        "tkinter.constants",
        "tkinter.commondialog",
        "tkinter.messagebox",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL", "numpy", "unittest", "pytest", "setuptools"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UsageFloat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="UsageFloat",
)
