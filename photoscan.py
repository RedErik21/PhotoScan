"""Punto d'ingresso di PhotoScan.exe (PyInstaller, vedi photoscan.spec).

- Senza argomenti: avvia l'applicazione (fotoscan.desktop), come l'icona sul Desktop.
- Con `-m <modulo> [argomenti]`: esegue quel modulo, come `python -m <modulo>`.
  Serve perché l'app avvia i propri sottoprocessi (server, indicizzazione, test modelli)
  con `sys.executable -m ...`, che nell'eseguibile punta a PhotoScan.exe stesso.
- Con `--runtime-check [file]`: importa torch/transformers dal runtime e scrive l'esito (JSON). Per i test di build.

Il runtime (PyTorch, transformers, ...) sta fuori dall'app o in _internal/runtime: va messo in sys.path PRIMA di
qualunque import di torch. Per questo l'attivazione e' qui, a livello di modulo (vale anche per i sottoprocessi
`-m fotoscan ...` e per i processi figli di multiprocessing, che rieseguono questo script).
"""
from __future__ import annotations

import multiprocessing
import runpy
import sys

try:
    from fotoscan import runtime as _runtime

    _runtime.activate()
except Exception:  # noqa: BLE001 - l'avvio non si ferma qui: il launcher mostra il problema del runtime
    pass


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--runtime-check":
        from fotoscan import runtime

        return runtime.main(sys.argv[2:])
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
