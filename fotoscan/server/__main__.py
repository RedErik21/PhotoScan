"""python -m fotoscan.server [--port 8501] [--host 127.0.0.1]"""
from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8501)
    args = ap.parse_args()
    os.environ["FOTOSCAN_HOST"] = args.host  # mostrato in Impostazioni > Rete
    from .routes_update import startup_check

    startup_check()  # ricerca aggiornamenti in background, se attiva nelle impostazioni
    uvicorn.run("fotoscan.server.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
