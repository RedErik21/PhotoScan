"""CLI: python -m fotoscan {index|plan|apply|stats|ui|serve|ui-legacy}"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time

from . import db as dbm
from .config import ROOT, load_config


def _progress_factory(conn):
    last = {"t": 0.0}

    def progress(stage: str, done: int, total: int) -> None:
        dbm.set_meta(conn, "progress", f"{stage}|{done}|{total}|{time.time():.0f}")
        now = time.time()
        if now - last["t"] > 2 or done >= total:
            last["t"] = now
            print(f"[{stage}] {done}/{total}", flush=True)

    return progress


def cmd_index(args, cfg):
    from . import sourcefs
    from .embedder import Embedder, embed_pending
    from .scanner_metadata import process_metadata, scan_into_db

    conn = dbm.connect(cfg.db_path)
    if args.source:
        cfg.source_dir = args.source
    _restore_source(conn, cfg)
    if not cfg.source_dir:
        sys.exit("Imposta la cartella sorgente (--source oppure dalla UI).")
    dbm.set_meta(conn, "source_dir", cfg.source_dir)  # serve a plan/apply in un comando successivo
    dbm.set_meta(conn, "last_index_error", "")
    prog = _progress_factory(conn)
    ffmpeg, ffprobe = cfg.find_ffmpeg()
    if args.workers:
        cfg.workers = args.workers
    print(f"ffmpeg: {ffmpeg or 'NON TROVATO (i video verranno saltati)'}  device: {cfg.resolve_device()}  "
          f"thread: {cfg.workers}")
    try:
        n = scan_into_db(conn, cfg, limit=args.limit, subdir=args.subdir, progress=prog)
    except FileNotFoundError as e:
        err_code = "network_unreachable" if ("non risponde" in str(e).lower() or sourcefs.is_network(cfg.source_dir)) else "source_missing"
        dbm.set_meta(conn, "last_index_error", err_code)
        sys.exit(f"Errore: {e}")
    print(f"file nuovi o modificati da elaborare: {n}")
    try:
        process_metadata(conn, cfg, progress=prog)
        embed_pending(conn, cfg, progress=prog)
    except KeyboardInterrupt:
        # ogni blocco e' una transazione: il catalogo resta consistente, basta rilanciare index
        sys.exit("\ninterrotto: catalogo consistente, rilancia 'index' per riprendere")
    if cfg.enable_faces:
        from . import faces

        ok, reason = faces.availability(cfg)
        if ok:
            faces.detect_faces(conn, cfg=cfg, progress=prog)
            print("persone riconosciute:", faces.cluster_faces(conn, cfg=cfg))
        else:
            print(f"enable_faces=true ma riconoscimento volti non disponibile ({reason}): saltato")
    dbm.set_meta(conn, "progress", "completato|1|1|%d" % time.time())
    print("indicizzazione completata:", dbm.stats(conn))


def _restore_source(conn, cfg) -> None:
    """La sorgente salvata nel catalogo (da 'index --source') vale se non e' data esplicitamente."""
    if not cfg.source_dir:
        cfg.source_dir = dbm.get_meta(conn, "source_dir", "") or ""


def cmd_plan(args, cfg):
    from . import organizer
    from .embedder import Embedder

    conn = dbm.connect(cfg.db_path)
    _restore_source(conn, cfg)
    emb = Embedder(cfg, device="cpu") if _needs_model(conn, cfg) else None
    res = organizer.build_plan(conn, cfg, embedder=emb)
    print("piano calcolato:", res)
    for r in organizer.folder_summary(conn)[:60]:
        print(f"  {r['n']:6d}  {r['folder']}")


def _needs_model(conn, cfg) -> bool:
    cached = {r[0] for r in conn.execute("SELECT prompt FROM label_cache")}
    return any(l["prompt"] not in cached for l in cfg.labels)


def cmd_apply(args, cfg):
    from . import organizer

    if args.output:
        cfg.output_dir = args.output
    conn = dbm.connect(cfg.db_path)
    _restore_source(conn, cfg)
    if args.apply:
        print(f"APPLICO il piano in: {cfg.output_dir}  (modo: {cfg.link_mode})")
    else:
        print("DRY-RUN: nessun file verra' creato. Usa --apply per eseguire.")
    try:
        print(organizer.apply_plan(conn, cfg, apply=args.apply, progress=_progress_factory(conn)))
    except ValueError as e:
        sys.exit(f"Errore: {e}")


def cmd_stats(args, cfg):
    conn = dbm.connect(cfg.db_path)  # nessuna dipendenza da sorgente/modello
    for k, v in dbm.stats(conn).items():
        print(f"{k:>16}: {v}")


def cmd_ui(args, cfg):
    """Applicazione: server FastAPI + finestra dedicata (vedi fotoscan/desktop.py)."""
    from . import desktop

    sys.exit(desktop.main())


def cmd_ui_legacy(args, cfg):
    """Vecchia interfaccia Streamlit (solo di riferimento, non piu' mantenuta)."""
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(ROOT / "fotoscan" / "app_streamlit.py"),
                    "--browser.gatherUsageStats", "false"], cwd=ROOT)


def cmd_serve(args, cfg):
    """Solo il server (sviluppo): stampa l'URL e resta in primo piano. Ctrl+C per fermarlo."""
    from .server.__main__ import is_loopback

    if not is_loopback(args.host):
        print(f"Indirizzo {args.host!r} rifiutato: PhotoScan non ha password e accetta solo indirizzi locali "
              "(127.0.0.1, ::1, localhost).", file=sys.stderr)
        return 2
    url = f"http://{args.host}:{args.port}/"
    print(f"PhotoScan server: {url}   (documentazione API: {url}api/docs)  Ctrl+C per fermare", flush=True)
    subprocess.run([sys.executable, "-m", "fotoscan.server", "--host", args.host, "--port", str(args.port)], cwd=ROOT)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fotoscan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="scansiona + metadati + embedding (riprendibile)")
    p.add_argument("--source")
    p.add_argument("--limit", type=int, help="campione uniforme di N file (test)")
    p.add_argument("--subdir", help="sottocartella della sorgente")
    p.add_argument("--workers", type=int, help="thread di decodifica/ffmpeg (default: config)")
    p.set_defaults(fn=cmd_index)
    p = sub.add_parser("plan", help="calcola il piano (dry-run) nel catalogo")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("apply", help="simula; con --apply crea le cartelle reali")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--output")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("stats")
    p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("ui", help="avvia l'applicazione (server + finestra dedicata)")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("ui-legacy", help="vecchia interfaccia Streamlit (riferimento)")
    p.set_defaults(fn=cmd_ui_legacy)
    p = sub.add_parser("serve", help="solo il server API+frontend (sviluppo), stampa l'URL")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_serve)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "huggingface_hub", "urllib3", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args.fn(args, load_config())


if __name__ == "__main__":
    main()
