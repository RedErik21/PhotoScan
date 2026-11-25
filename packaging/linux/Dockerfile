# Ambiente di build dell'AppImage di PhotoScan (solo per chi costruisce il pacchetto).
# L'utente finale non usa Docker: riceve il file PhotoScan-<versione>-x86_64.AppImage.
# Costruzione (dalla radice del repo; il codice viene copiato nell'immagine, nessun mount):
#   docker build -t photoscan-linux-build -f packaging/linux/Dockerfile .
# Estrazione dell'AppImage:
#   docker run --rm --entrypoint cat photoscan-linux-build /out/PhotoScan-<versione>-x86_64.AppImage > PhotoScan.AppImage
FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APPIMAGE_EXTRACT_AND_RUN=1

# Strumenti di sistema: FUSE non serve (appimagetool estratto e lanciato senza mount).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils file binutils libgl1 libglib2.0-0 libxkbcommon0 \
    && rm -rf /var/lib/apt/lists/*

# appimagetool (costruisce l'AppImage a partire dalla cartella AppDir)
RUN curl -fsSL -o /usr/local/bin/appimagetool \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage \
    && chmod +x /usr/local/bin/appimagetool

# Codice del repo (il .dockerignore esclude dati, cache e build precedenti)
COPY . /src
COPY packaging/linux/build_appimage.sh /usr/local/bin/build_appimage.sh
RUN chmod +x /usr/local/bin/build_appimage.sh \
    && mkdir -p /out \
    && /usr/local/bin/build_appimage.sh \
    && rm -rf /work /tmp/ffmpeg /tmp/ffmpeg.tar.xz /tmp/pyinstaller.log

CMD ["ls", "-lh", "/out"]
