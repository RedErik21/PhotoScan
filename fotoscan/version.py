"""Versione di FotoScan e repository GitHub da cui arrivano gli aggiornamenti.

Per pubblicare un aggiornamento: alzare __version__, fare commit, creare il tag `v<versione>` e spingerlo
(`git tag v2.1.0 && git push origin v2.1.0`): il workflow .github/workflows/release.yml crea la Release.
"""
from __future__ import annotations

import os

__version__ = "2.0.0"

# "proprietario/repository" su GitHub. Vuoto = aggiornamenti disattivati (nessun controllo, nessuna connessione).
# FOTOSCAN_UPDATE_REPO lo sostituisce (test, fork).
UPDATE_REPO = ""


def update_repo() -> str:
    return (os.environ.get("FOTOSCAN_UPDATE_REPO") or UPDATE_REPO).strip().strip("/")
