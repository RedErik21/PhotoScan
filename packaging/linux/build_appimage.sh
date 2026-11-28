#!/bin/sh
# Costruisce PhotoScan-<versione>-x86_64.AppImage dentro il container di build.
# Input: /src (copia del repo nell'immagine). Output: /out (file .AppImage). Il lavoro avviene in /work.
set -eu

SRC=/src
WORK=/work
OUT=/out

VERSION=$(python - <<'EOF'
import re
text = open("/src/fotoscan/version.py", encoding="utf-8").read()
print(re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1))
EOF
)
echo "==> PhotoScan ${VERSION}"

rm -rf "$WORK" && mkdir -p "$WORK" "$OUT"
# copia del codice senza dati locali, cache, build precedenti e materiale privato
tar -C "$SRC" \
    --exclude=./.git --exclude=./build_out --exclude=./cache --exclude=./_privato --exclude=./video \
    --exclude=./catalogo.db --exclude=./config.json --exclude=./fotoscan_ui.log --exclude=./site \
    --exclude=./tests --exclude=./tools --exclude=./launcher --exclude=./docs \
    -cf - . | tar -C "$WORK" -xf -
cd "$WORK"

echo "==> Dipendenze (versioni esatte; PyTorch solo CPU: nessun CUDA nel pacchetto Linux)"
# PyTorch, transformers e compagni: requirements-runtime.txt. Su Linux restano DENTRO l'AppImage (file unico).
pip install --quiet -r requirements-runtime.txt --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
pip install --quiet numpy==2.5.4 pillow==13.0.0 pillow-heif==1.8.1 xxhash==4.1.0 pandas==3.1.1     fastapi==0.165.0 "uvicorn[standard]==0.54.0" psutil==7.2.2 onnxruntime==1.32.0     pyinstaller==6.22.3 pyinstaller-hooks-contrib==2026.10

echo "==> PyInstaller (cartella onedir)"
pyinstaller --noconfirm photoscan.spec >/tmp/pyinstaller.log 2>&1 || { tail -80 /tmp/pyinstaller.log; exit 1; }

echo "==> Prova di fumo: l'eseguibile importa torch/transformers e si avvia"
"$WORK/dist/PhotoScan/PhotoScan" --runtime-check /tmp/runtime-check.json || { cat /tmp/runtime-check.json; exit 1; }
"$WORK/dist/PhotoScan/PhotoScan" -m fotoscan --help > /dev/null

APPDIR="$WORK/AppDir"
rm -rf "$APPDIR" && mkdir -p "$APPDIR/usr/bin"
cp -r "$WORK/dist/PhotoScan/." "$APPDIR/usr/bin/"

echo "==> ffmpeg statico (per i video: fotogrammi e durata)"
curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 -o /tmp/ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
mkdir -p /tmp/ffmpeg && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1
cp /tmp/ffmpeg/ffmpeg /tmp/ffmpeg/ffprobe "$APPDIR/usr/bin/"

cp "$SRC/packaging/linux/AppRun" "$APPDIR/AppRun"
cp "$SRC/packaging/linux/PhotoScan.desktop" "$APPDIR/PhotoScan.desktop"
cp "$SRC/frontend/icon-192.png" "$APPDIR/PhotoScan.png"
chmod +x "$APPDIR/AppRun" "$APPDIR/usr/bin/PhotoScan" "$APPDIR/usr/bin/ffmpeg" "$APPDIR/usr/bin/ffprobe"

echo "==> AppImage"
ARCH=x86_64 appimagetool --no-appstream "$APPDIR" "$OUT/PhotoScan-${VERSION}-x86_64.AppImage"
ls -lh "$OUT"
