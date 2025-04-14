# -*- mode: python ; coding: utf-8 -*-

import os
import importlib.util

# Find the installed esptool package path dynamically
esptool_spec = importlib.util.find_spec('esptool')
if esptool_spec and esptool_spec.origin:
    esptool_dir = os.path.dirname(esptool_spec.origin)
    local_stub_flasher_path = os.path.join(esptool_dir, "targets", "stub_flasher")
else:
    # Fallback or error if esptool is not found (should not happen if requirements are installed)
    raise ImportError("Could not find the esptool package. Is it installed?")

a = Analysis(
    ['nodemcu-pyflasher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ("images", "images"),
        (os.path.join(local_stub_flasher_path, "1"), os.path.join(".", "esptool", "targets", "stub_flasher", "1")),
        (os.path.join(local_stub_flasher_path, "2"), os.path.join(".", "esptool", "targets", "stub_flasher", "2"))
    ],
    hiddenimports=[],
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
    name='NodeMCU-PyFlasher',
    version='windows-version-info.txt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='images\\icon-256.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
