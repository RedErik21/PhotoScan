"""python -m fotoscan.server [--port 8501] [--host 127.0.0.1]"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys

import uvicorn


def is_loopback(host: str) -> bool:
    """Solo indirizzi locali: le API non hanno autenticazione e leggono/spostano file del PC."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8501)
    args = ap.parse_args()
    if not is_loopback(args.host):
        print(f"Indirizzo {args.host!r} rifiutato: PhotoScan non ha password e accetta solo indirizzi locali "
              "(127.0.0.1, ::1, localhost).", file=sys.stderr)
        sys.exit(2)
    os.environ["FOTOSCAN_HOST"] = args.host  # mostrato in Impostazioni > Rete
    from .routes_update import startup_check

    startup_check()  # ricerca aggiornamenti in background, se attiva nelle impostazioni
    uvicorn.run("fotoscan.server.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
