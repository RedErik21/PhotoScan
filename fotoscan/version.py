"""Versione di PhotoScan e repository GitHub da cui arrivano gli aggiornamenti.

Per pubblicare un aggiornamento: alzare __version__, fare commit, creare il tag `v<versione>` e spingerlo
(`git tag v0.1.4 && git push origin v0.1.4`): il workflow .github/workflows/release.yml crea la Release.
"""
from __future__ import annotations

import os

__version__ = "0.1.11"

# "proprietario/repository" su GitHub. Vuoto = aggiornamenti disattivati (nessun controllo, nessuna connessione).
# FOTOSCAN_UPDATE_REPO lo sostituisce (test, fork).
UPDATE_REPO = "RedErik21/PhotoScan"


def update_repo() -> str:
    return (os.environ.get("FOTOSCAN_UPDATE_REPO") or UPDATE_REPO).strip().strip("/")
