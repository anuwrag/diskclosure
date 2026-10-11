# -*- mode: python ; coding: utf-8 -*-
"""One-file Windows console build. Run with PyInstaller on Windows."""

from PyInstaller.utils.hooks import collect_all

textual_datas, textual_binaries, textual_hidden = collect_all("textual")
rich_datas, rich_binaries, rich_hidden = collect_all("rich")

a = Analysis(
    ["diskclosure.py"],
    pathex=[],
    binaries=textual_binaries + rich_binaries,
    datas=textual_datas + rich_datas,
    hiddenimports=textual_hidden + rich_hidden + ["windisk", "windisk.app", "windisk.scanner"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Diskclosure",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
