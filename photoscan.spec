# -*- mode: python ; coding: utf-8 -*-
# Build: .venv\Scripts\python.exe -m PyInstaller --noconfirm photoscan.spec
# Risultato: dist\PhotoScan\PhotoScan.exe (cartella "onedir"). I modelli NON sono inclusi:
# si scaricano al primo uso in %LOCALAPPDATA%\PhotoScan\cache\models.
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("fotoscan", filter=lambda name: name != "fotoscan.app_streamlit")

a = Analysis(
    ["photoscan.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("frontend", "frontend"),
        ("fotoscan/photoscan.ico", "fotoscan"),
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["streamlit", "tkinter", "matplotlib", "IPython", "jupyter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhotoScan",
    icon="fotoscan/photoscan.ico",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PhotoScan",
)
