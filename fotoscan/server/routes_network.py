"""Impostazioni > Rete: stato del blocco di rete, connessioni previste e registro dei tentativi verso internet."""
from __future__ import annotations

import os
import socket

from fastapi import APIRouter, Query, Request

from .. import netguard
from . import deps

router = APIRouter(prefix="/api", tags=["network"])


def _expected_updates(cfg) -> dict:
    from ..version import update_repo

    repo = update_repo()
    if not repo:
        state = "Aggiornamenti non configurati: nessuna connessione"
    elif cfg.update_check_on_start:
        state = "Controllo a ogni avvio attivo (si spegne in Impostazioni > Aggiornamenti)"
    else:
        state = "Controllo all'avvio spento: solo quando premi «Cerca aggiornamenti»"
    return {
        "id": "github-updates", "name": "GitHub (aggiornamenti)", "hosts": ["api.github.com", "codeload.github.com"],
        "purpose": "Sapere se c'è una versione nuova di FotoScan e scaricarla",
        "when": "All'avvio (se attivo) o quando cerchi gli aggiornamenti",
        "sends": f"Richiesta delle versioni pubblicate{f' di {repo}' if repo else ''}. Nessuna foto.",
        "state": state,
        "ok": not repo or not cfg.update_check_on_start, "managed": True,
    }


def _expected(cfg) -> list[dict]:
    """Le uniche destinazioni esterne note, con quando e cosa viene inviato."""
    from .. import faces
    from .routes_status import _model_cached

    model_ok = _model_cached(cfg.model_name)
    try:
        faces_ok = faces.models_present(cfg)
    except Exception:  # noqa: BLE001
        faces_ok = False
    return [
        {
            "id": "huggingface", "name": "Hugging Face", "hosts": ["huggingface.co", "*.hf.co"],
            "purpose": "Scaricare il modello di riconoscimento delle immagini",
            "when": "Solo se il modello scelto non è ancora sul computer",
            "sends": "Nome del modello e richiesta dei file. Nessuna foto.",
            "state": "Modello già sul computer: nessuna connessione" if model_ok
                     else "Modello da scaricare: servirà internet una volta",
            "ok": model_ok, "managed": True,
        },
        {
            "id": "github", "name": "GitHub", "hosts": ["github.com", "*.githubusercontent.com"],
            "purpose": "Scaricare i modelli per il riconoscimento dei volti (buffalo_l)",
            "when": "Una volta sola, se manca e lo chiedi dalla sezione Persone",
            "sends": "Richiesta del file buffalo_l.zip. Nessuna foto.",
            "state": "Modelli volti già sul computer: nessuna connessione" if faces_ok
                     else "Modelli volti non scaricati",
            "ok": faces_ok, "managed": True,
        },
        _expected_updates(cfg),
        {
            "id": "osm", "name": "OpenStreetMap", "hosts": ["www.openstreetmap.org"],
            "purpose": "Aprire la mappa di un luogo",
            "when": "Solo quando clicchi il link della mappa (si apre nel browser)",
            "sends": "Coordinate GPS della foto nell'indirizzo della pagina",
            "state": "Fuori dal blocco: lo apre il browser, non FotoScan",
            "ok": None, "managed": False,
        },
        {
            "id": "microsoft", "name": "Microsoft (Edge / WebView2, Windows)", "hosts": ["vari"],
            "purpose": "Servizi del browser e di Windows (aggiornamenti, SmartScreen, telemetria di sistema)",
            "when": "Indipendente da FotoScan",
            "sends": "Dati del browser o del sistema, non le tue foto",
            "state": "Fuori dal blocco: si controlla dalle impostazioni di Windows o con il firewall",
            "ok": None, "managed": False,
        },
    ]


@router.get("/network")
def network_status(request: Request, limit: int = Query(200, ge=1, le=1000)):
    cfg = deps.get_cfg()
    all_events = netguard.read_log(100000)
    events = all_events[:limit]
    host = os.environ.get("FOTOSCAN_HOST") or ""
    return {
        "guard": netguard.status(),
        "server": {
            "host": host or None,
            "port": request.url.port,
            "local_only": netguard.is_loopback(host) if host else None,
        },
        "expected": _expected(cfg),
        "summary": {
            "blocked": sum(1 for e in all_events if e.get("outcome") == "bloccato"),
            "allowed": sum(1 for e in all_events if e.get("outcome") == "consentito"),
            "last": events[0]["ts"] if events else None,
        },
        "events": events,
    }


@router.delete("/network/log")
def network_clear_log():
    netguard.clear_log()
    return {"ok": True}


@router.post("/network/test")
def network_test():
    """Prova il blocco: tenta di raggiungere example.com e verifica che venga rifiutato."""
    try:
        with socket.create_connection(("example.com", 443), timeout=5):
            pass
    except netguard.NetworkBlocked:
        return {"blocked": True, "message": "Bloccato: FotoScan non può uscire su internet."}
    except OSError as e:
        return {"blocked": False, "message": f"Connessione non riuscita ma NON fermata dal blocco: {e}"}
    return {"blocked": False, "message": "Attenzione: la connessione è riuscita, il blocco non è attivo."}
