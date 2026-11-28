# -*- mode: python ; coding: utf-8 -*-
# Build: .venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath build_out\launcher --workpath build_out\work-launcher launcher\PhotoScan-Setup.spec
# Risultato: build_out\launcher\PhotoScan-Setup.exe (eseguibile singolo con icona e asset di PhotoScan).

a = Analysis(
    ["setup_photoscan.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("../fotoscan/photoscan.ico", "."),
        ("../frontend/icon-192.png", "."),
        ("../frontend/vendor/fonts/playwrite-us-trad.woff2", "."),
    ],
    # importati dentro funzioni: li si dichiara per non dipendere dall'analisi statica
    hiddenimports=["setup_gui", "uninstall_photoscan", "psutil", "winreg"],
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
    name="PhotoScan-Setup",
    icon="../fotoscan/photoscan.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
