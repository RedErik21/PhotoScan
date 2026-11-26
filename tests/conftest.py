"""Isolamento per pytest: config.json, catalogo e cache in una cartella temporanea PRIMA di ogni import.

Con pytest tutti i moduli di test girano nello stesso processo: il primo che importa fotoscan.config fissa
CONFIG_FILE. Senza questo file, i test che salvano impostazioni (test_models, test_api_status_settings, ...)
scrivevano nel config.json vero se un altro modulo aveva importato fotoscan prima di loro.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="fs_pytest_"))
os.environ["FOTOSCAN_CONFIG"] = str(_TMP / "config.json")
os.environ.setdefault("FOTOSCAN_DB", str(_TMP / "catalogo.db"))
os.environ.setdefault("FOTOSCAN_CACHE", str(_TMP / "cache"))

import fotoscan.config as _config  # noqa: E402

# protezione in piu': se qualcosa avesse gia' fissato il percorso vero, il test si ferma subito
assert Path(_config.CONFIG_FILE).resolve() != (Path(_config.ROOT) / "config.json").resolve(), \
    "i test non devono usare il config.json reale"
