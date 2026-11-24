"""Punto d'ingresso di PhotoScan.exe (PyInstaller, vedi photoscan.spec).

- Senza argomenti: avvia l'applicazione (fotoscan.desktop), come l'icona sul Desktop.
- Con `-m <modulo> [argomenti]`: esegue quel modulo, come `python -m <modulo>`.
  Serve perché l'app avvia i propri sottoprocessi (server, indicizzazione, test modelli)
  con `sys.executable -m ...`, che nell'eseguibile punta a PhotoScan.exe stesso.
"""
from __future__ import annotations

import multiprocessing
import runpy
import sys


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "-m":
        module = sys.argv[2]
        sys.argv = [module, *sys.argv[3:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0
    from fotoscan.desktop import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
