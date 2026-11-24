#!/bin/bash
# avvia PhotoScan sulla libreria demo (porta 9300), isolato dal catalogo vero
cd "$(dirname "$0")/../.."   # radice del progetto
export FOTOSCAN_CONFIG="C:\FotoScanDemo\app\config.json" FOTOSCAN_DB="C:\FotoScanDemo\app\catalogo.db" FOTOSCAN_CACHE="C:\FotoScanDemo\app\cache" PYTHONIOENCODING=utf-8
exec .venv/Scripts/python.exe -m fotoscan.server --port 9300 > /c/FotoScanDemo/app/server.log 2>&1
