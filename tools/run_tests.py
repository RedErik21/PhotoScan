"""Esegue tutti i tests/test_*.py come script (il venv non ha pytest) e stampa un riepilogo.

  .\\.venv\\Scripts\\python.exe tools\\run_tests.py            # tutti
  .\\.venv\\Scripts\\python.exe tools\\run_tests.py api_files   # solo i file che contengono il testo

Codice di uscita: 0 se tutti i file passano, 1 altrimenti (anche per crash o timeout).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
TIMEOUT = 900  # secondi per singolo file


def main(argv: list[str]) -> int:
    files = sorted(TESTS.glob("test_*.py"))
    if argv:
        files = [f for f in files if any(a in f.name for a in argv)]
    if not files:
        print("Nessun test trovato.")
        return 1

    risultati: list[tuple[str, bool, float, str]] = []
    for f in files:
        print(f"==> {f.name}", flush=True)
        t0 = time.time()
        try:
            proc = subprocess.run([sys.executable, "-X", "utf8", str(f)], cwd=ROOT, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
            ok = proc.returncode == 0
            esito = f"exit {proc.returncode}"
            out = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            ok, esito, out = False, f"timeout dopo {TIMEOUT} s", ""
        dt = time.time() - t0
        risultati.append((f.name, ok, dt, esito))
        # output completo solo se fallisce: in caso di successo basta il riepilogo
        if not ok:
            print(out[-4000:])
        print(f"    {'OK' if ok else 'FALLITO'}  ({dt:.1f} s, {esito})", flush=True)

    print("\n" + "=" * 60)
    print(f"{'file':<30} {'esito':<10} {'tempo':>8}")
    for nome, ok, dt, _ in risultati:
        print(f"{nome:<30} {'PASS' if ok else 'FAIL':<10} {dt:>7.1f}s")
    passati = sum(1 for r in risultati if r[1])
    print("=" * 60)
    print(f"Totale: {passati} pass, {len(risultati) - passati} fail su {len(risultati)} file")
    return 0 if passati == len(risultati) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
