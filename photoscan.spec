# -*- mode: python ; coding: utf-8 -*-
# Build: .venv\Scripts\python.exe -m PyInstaller --noconfirm photoscan.spec
# Risultato: dist\PhotoScan\PhotoScan.exe (cartella "onedir"). I modelli NON sono inclusi:
# si scaricano al primo uso in %LOCALAPPDATA%\PhotoScan\cache\models.
#
# PHOTOSCAN_BUILD=full   (predefinito, e AppImage Linux) PyTorch e transformers dentro il pacchetto.
# PHOTOSCAN_BUILD=light  app leggera: PyTorch, torchvision, transformers e dipendenze pesanti restano FUORI e
#                        si trovano nel runtime (fotoscan/runtime.py, packaging/windows/release_tools.py).
import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# Lo spec gira fuori dalla cartella del progetto: senza questo, "fotoscan" non si importa e
# collect_submodules restituisce una lista vuota (il programma poi non trova server e database).
sys.path.insert(0, os.path.abspath("."))

from fotoscan.runtime_version import HEAVY_MODULES
from fotoscan.version import __version__ as APP_VERSION

BUILD = os.environ.get("PHOTOSCAN_BUILD", "full").strip().lower()
if BUILD not in ("full", "light"):
    raise SystemExit(f"photoscan.spec: PHOTOSCAN_BUILD={BUILD!r} non valido (full | light)")
LIGHT = BUILD == "light"

_vparts = [int(x) for x in APP_VERSION.split(".") if x.isdigit()]
while len(_vparts) < 4:
    _vparts.append(0)
_vtup = tuple(_vparts[:4])

version_info = None
if sys.platform == "win32":  # risorsa di versione solo per PhotoScan.exe (su Linux il modulo richiede pefile)
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=_vtup, prodvers=_vtup, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
        kids=[
            StringFileInfo([
                StringTable("041004B0", [
                    StringStruct("CompanyName", "PhotoScan"),
                    StringStruct("FileDescription", "PhotoScan"),
                    StringStruct("FileVersion", APP_VERSION),
                    StringStruct("InternalName", "PhotoScan"),
                    StringStruct("OriginalFilename", "PhotoScan.exe"),
                    StringStruct("ProductName", "PhotoScan"),
                    StringStruct("ProductVersion", APP_VERSION),
                ]),
            ]),
            VarFileInfo([VarStruct("Translation", [0x0410, 1200])]),
        ],
    )

def _torchvision_ext_modules():
    """Estensioni .pyd di torchvision (_C_stable, image_stable) da mettere nel bundle nella stessa cartella."""
    import glob
    import importlib.util

    spec_tv = importlib.util.find_spec("torchvision")
    if spec_tv is None or not spec_tv.submodule_search_locations:
        return []
    base = list(spec_tv.submodule_search_locations)[0]
    return [(p, "torchvision") for p in glob.glob(os.path.join(base, "*.pyd"))]


hidden = collect_submodules("fotoscan", filter=lambda name: name != "fotoscan.app_streamlit")
if not any(h.startswith("fotoscan.server") for h in hidden) or "fotoscan.db" not in hidden:
    raise SystemExit("photoscan.spec: moduli fotoscan non raccolti (fotoscan.server / fotoscan.db mancanti)")

if LIGHT:
    # moduli della libreria standard che il runtime importa: PyInstaller non li vede (l'app non li usa)
    _std = os.path.join("packaging", "windows", "runtime_stdlib.txt")
    if not os.path.isfile(_std):
        raise SystemExit("photoscan.spec: manca packaging/windows/runtime_stdlib.txt (release_tools.py stdlib-scan)")
    with open(_std, encoding="utf-8") as _f:
        hidden = hidden + _f.read().split()

a = Analysis(
    ["photoscan.py"],
    pathex=["."],
    # torchvision: le estensioni native (_C_stable.pyd, image_stable.pyd) vengono caricate come librerie
    # (torch.ops.load_library), non importate, quindi l'analisi non le trova. Senza, AutoProcessor fallisce:
    # "operator torchvision::nms does not exist".
    binaries=[] if LIGHT else collect_dynamic_libs("torchvision") + _torchvision_ext_modules(),
    datas=[
        ("frontend", "frontend"),
        ("fotoscan/photoscan.ico", "fotoscan"),
        ("fotoscan/phone_mtp.ps1", "fotoscan"),   # importazione dal telefono (Windows): senza, la funzione non parte
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["streamlit", "tkinter", "matplotlib", "IPython", "jupyter", "pytest", *(HEAVY_MODULES if LIGHT else ())],
    noarchive=False,
)
if LIGHT:
    # niente metadati (dist-info) dei pacchetti pesanti: quelli giusti (cpu o gpu) arrivano col runtime
    _heavy = tuple(name + "-" for name in HEAVY_MODULES)
    a.datas = [d for d in a.datas if not (d[0].lower().endswith(".dist-info") or ".dist-info" + os.sep in d[0].lower()
                                          or ".dist-info/" in d[0].lower()) or not d[0].lower().replace("_", "-").startswith(_heavy)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhotoScan",
    icon="fotoscan/photoscan.ico",
    version=version_info,
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
