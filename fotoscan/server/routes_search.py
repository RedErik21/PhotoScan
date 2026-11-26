"""Ricerca semantica testo -> immagine (SigLIP2 su CPU) e prova delle etichette di contesto.

Il modello si carica in background al primo uso (apertura della Ricerca, prima query, prova di un'etichetta):
nel frattempo le route rispondono 503 con un messaggio in italiano e il frontend mostra un banner. Dopo
IDLE_UNLOAD_S secondi senza uso il modello e la matrice degli embedding si scaricano, cosi' l'app ferma non
tiene torch in RAM. La matrice si ricostruisce solo quando cambia il numero di embedding o il massimo file_id.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import OrderedDict

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import db as dbm
from ..config import BASE_SEARCH_THR, model_profile
from ..embedder import Embedder, label_vectors
from . import deps


router = APIRouter(prefix="/api", tags=["search"])
log = logging.getLogger("fotoscan")

LOADING_MSG = "Sto preparando la ricerca..."
IDLE_UNLOAD_S = 10 * 60  # modello e matrice si scaricano dopo 10 minuti senza ricerche
# Soglie sul coseno (testo-immagine): valori di SigLIP 2 Base in config.BASE_SEARCH_THR (significato li').
# Gli altri modelli hanno la scala dei coseni diversa: preset in config.AVAILABLE_MODELS["search_thr"], sostituibili
# dall'utente in Impostazioni > Per esperti (config.model_profile).
MIN_MATCH = BASE_SEARCH_THR["min_match"]
MIN_GAP = BASE_SEARCH_THR["min_gap"]
LEVEL_OK = BASE_SEARCH_THR["level_ok"]
LEVEL_GOOD = BASE_SEARCH_THR["level_good"]
_BASE_THR = dict(BASE_SEARCH_THR)


def search_thresholds(model_name: str | None = None) -> dict:
    """Soglie della ricerca per il modello (quello configurato se non indicato), con le sostituzioni dell'utente."""
    cfg = deps.get_cfg()
    return dict(model_profile(model_name or cfg.model_name, cfg)["search_thr"])
MAX_QUERY = 200
# Frasi di partenza quando il catalogo non ha ancora etichette (vedi /search/suggestions).
COMMON_QUERIES = [
    "tramonto", "torta di compleanno", "cane", "gatto", "mare", "montagna", "neve", "scontrino",
    "auto", "bambini", "sorriso", "pizza", "fiori", "concerto", "bicicletta", "natale", "documento",
]
SUGGESTION_STOPWORDS = {"e", "di", "con", "il", "la", "in", "sul", "del"}

# ---------------------------------------------------------------- modello (singleton lazy)
_lock = threading.Lock()  # protegge lo stato del modello
_emb: Embedder | None = None
_loading = False
_error: str | None = None
_load_seconds: float | None = None
_last_used = 0.0  # time.monotonic() dell'ultimo uso del modello o della matrice

_enc_lock = threading.Lock()  # un solo encoding di testo alla volta (torch su CPU)
_qcache: OrderedDict[str, np.ndarray] = OrderedDict()  # vettori delle query recenti

_mat_lock = threading.Lock()
_mat_cache: tuple | None = None  # (chiave, ids, matrice, is_video)


def _lower_thread_priority() -> None:
    """Il caricamento in background non deve rallentare il server: priorita' sotto la normale (solo Windows)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:  # noqa: BLE001 - e' solo un'ottimizzazione
        pass


def _touch() -> None:
    global _last_used
    _last_used = time.monotonic()


def _idle_worker() -> None:
    """Scarica modello e matrice dopo IDLE_UNLOAD_S senza uso; termina quando il modello non e' piu' in memoria."""
    while True:
        time.sleep(min(60.0, max(1.0, IDLE_UNLOAD_S / 4)))
        with _lock:
            if _emb is None:
                return
            idle = time.monotonic() - _last_used
        if idle >= IDLE_UNLOAD_S:
            log.info("modello di ricerca scaricato dopo %d s di inattivita'", int(idle))
            reset_model()
            import gc
            gc.collect()
            return


def _load_worker(cfg) -> None:
    global _emb, _loading, _error, _load_seconds
    _lower_thread_priority()
    t0 = time.perf_counter()
    try:
        emb = Embedder(cfg, device="cpu")  # CPU: non contende la GPU con l'indicizzazione
        with _lock:
            _emb = emb
            _load_seconds = round(time.perf_counter() - t0, 1)
        _touch()
        threading.Thread(target=_idle_worker, daemon=True, name="search-idle").start()
        log.info("modello di ricerca caricato in %.1f s", _load_seconds)
    except Exception as e:  # noqa: BLE001 - l'errore va mostrato, non propagato al thread
        log.exception("caricamento del modello di ricerca fallito")
        with _lock:
            _error = f"{type(e).__name__}: {e}"
    finally:
        with _lock:
            _loading = False


def start_warmup() -> None:
    """Avvia il caricamento in background (no-op se gia' caricato o in corso)."""
    global _loading, _error
    with _lock:
        if _emb is not None or _loading:
            return
        _loading, _error = True, None
    threading.Thread(target=_load_worker, args=(deps.get_cfg(),), daemon=True, name="search-model").start()


def _require_embedder() -> Embedder:
    """Modello pronto, oppure 503 (avviando il caricamento se serve)."""
    with _lock:
        emb, err = _emb, _error
    if emb is not None:
        _touch()
        return emb
    start_warmup()  # dopo un errore riprova al contatto successivo
    if err:
        raise HTTPException(503, f"La ricerca non e' disponibile: {err}")
    raise HTTPException(503, LOADING_MSG)


def reset_model() -> None:
    """Scarica il modello e le cache in memoria (chiamato dopo un cambio modello o reset embedding)."""
    global _emb, _loading, _error, _load_seconds, _mat_cache, _hash_cache
    with _lock:
        _emb = None
        _loading = False
        _error = None
        _load_seconds = None
    with _enc_lock:
        _qcache.clear()
    with _mat_lock:
        _mat_cache = None
        _hash_cache = None


def _text_vector(emb: Embedder, text: str) -> np.ndarray:
    """Embedding (normalizzato) del testo con prompt ensembling e cache LRU."""
    with _enc_lock:
        v = _qcache.get(text)
        if v is None:
            v = emb.encode_query(text) if hasattr(emb, "encode_query") else emb.encode_texts([text])[0]
            _qcache[text] = v
            while len(_qcache) > 256:
                _qcache.popitem(last=False)
        else:
            _qcache.move_to_end(text)
        return v


# ---------------------------------------------------------------- matrice e ranking
def _embedding_matrix(conn) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(ids, matrice, is_video) dal catalogo; ricostruita solo se cambiano conteggio o max file_id."""
    global _mat_cache
    _touch()
    cfg = deps.get_cfg()
    row = conn.execute("SELECT COUNT(*), MAX(file_id) FROM embeddings").fetchone()
    key = (cfg.db_path, row[0], row[1])
    with _mat_lock:
        if _mat_cache is None or _mat_cache[0] != key:
            ids, mat = dbm.load_embeddings(conn)
            kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM files")}
            is_video = np.fromiter((kinds.get(int(i)) == "video" for i in ids), dtype=bool, count=len(ids))
            _mat_cache = (key, ids, mat, is_video)
            log.info("matrice ricerca ricostruita: %d embedding", len(ids))
        _, ids, mat, is_video = _mat_cache
    return ids, mat, is_video


def _candidate_mask(conn, ids: np.ndarray, is_video: np.ndarray, kind: str) -> np.ndarray:
    """File ammessi: senza duplicati (is_dup>0) e del tipo richiesto."""
    mask = np.ones(len(ids), dtype=bool)
    dup = [r[0] for r in conn.execute("SELECT file_id FROM plan WHERE is_dup>0")]
    if dup:
        mask &= ~np.isin(ids, np.asarray(dup, dtype=np.int64))
    if kind == "video":
        mask &= is_video
    elif kind == "image":
        mask &= ~is_video
    return mask


def _top(scores: np.ndarray, mask: np.ndarray, limit: int, offset: int,
         floor: float | None = None) -> tuple[int, np.ndarray]:
    """(totale, indici ordinati della pagina). Con `floor` contano solo i file con coseno >= floor
    (il totale e' quindi onesto: quanti risultati superano la soglia). Top-k con argpartition: O(n)."""
    s = np.where(mask, scores, -np.inf)
    total = int(np.count_nonzero(s >= floor)) if floor is not None else int(mask.sum())
    k = min(offset + limit, total)
    if k <= offset:
        return total, np.zeros(0, dtype=np.int64)
    part = np.argpartition(-s, k - 1)[:k]
    order = part[np.argsort(-s[part])]
    return total, order[offset:offset + limit]


def match_level(score: float, thr: dict | None = None) -> str:
    """Fascia qualitativa per la UI: 'ottima' | 'buona' | 'possibile' (soglie di base se non indicate)."""
    thr = thr or _BASE_THR
    if score >= thr["level_ok"]:
        return "ottima"
    if score >= thr["level_good"]:
        return "buona"
    return "possibile"


def _files_for(conn, fids: list[int], scores, thr: dict | None = None) -> list[dict]:
    """File del contratto (+ match) nell'ordine dato."""
    if not fids:
        return []
    marks = ",".join("?" * len(fids))
    rows = {r["id"]: r for r in conn.execute(deps.SELECT_FILE + f"WHERE f.id IN ({marks})", fids)}
    out = []
    for fid, sc in zip(fids, scores):
        r = rows.get(fid)
        if r is not None:
            out.append({**deps.file_to_dict(r), "match": round(float(sc), 3), "match_level": match_level(float(sc), thr)})
    return out


def _status_dict(conn) -> dict:
    with _lock:
        loaded, loading, err, secs = _emb is not None, _loading, _error, _load_seconds
    indexed = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    return {"model_loaded": loaded, "loading": loading, "indexed": indexed, "error": err, "load_seconds": secs}


# ---------------------------------------------------------------- route
_hash_cache: tuple | None = None  # (chiave, hash per posizione in ids), come la matrice


def _hashes_for(conn, ids: np.ndarray) -> list:
    """Hash del contenuto di ogni file (None se mancante), allineato a `ids`. Ricostruito con la matrice."""
    global _hash_cache
    row = conn.execute("SELECT COUNT(*), MAX(file_id) FROM embeddings").fetchone()
    key = (deps.get_cfg().db_path, row[0], row[1])
    with _mat_lock:
        if _hash_cache is None or _hash_cache[0] != key:
            by_id = {r[0]: r[1] for r in conn.execute("SELECT id, hash FROM files WHERE hash IS NOT NULL")}
            _hash_cache = (key, [by_id.get(int(i)) for i in ids])
        return _hash_cache[1]


@router.get("/search")
def search(
    q: str = Query(""),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    kind: str = Query(""),
    incerti: bool = Query(False),  # True: include anche i risultati "possibile" (sotto LEVEL_GOOD)
    conn=Depends(deps.get_db),
):
    text = " ".join(q.split()).lower()
    if not text:
        raise HTTPException(400, "Scrivi cosa cercare.")
    if len(text) > MAX_QUERY:
        raise HTTPException(400, f"Testo troppo lungo (massimo {MAX_QUERY} caratteri).")
    if kind not in ("", "image", "video"):
        raise HTTPException(400, "Tipo non valido: usa 'image' o 'video'.")
    emb = _require_embedder()
    qv = _text_vector(emb, text)
    ids, mat, is_video = _embedding_matrix(conn)
    if len(ids) == 0:
        return {"total": 0, "items": []}
    scores = mat @ qv
    mask = _candidate_mask(conn, ids, is_video, kind)
    if not mask.any():
        return {"total": 0, "items": []}
    thr = search_thresholds()
    best = float(np.max(np.where(mask, scores, -np.inf)))
    # Nessun match: il migliore e' sotto la soglia assoluta, oppure non si stacca dal resto dell'archivio.
    if best < thr["min_match"] or best - float(scores.mean()) < thr["min_gap"]:
        return {"total": 0, "items": []}
    # Ordine completo dei candidati, senza copie identiche (stesso hash): resta la prima, la migliore.
    cand = np.flatnonzero(mask & (scores >= thr["min_match"]))
    cand = cand[np.argsort(-scores[cand], kind="stable")]
    hashes = _hashes_for(conn, ids)
    seen: set[str] = set()
    keep: list[int] = []
    for i in cand.tolist():
        h = hashes[i]
        if h is not None:
            if h in seen:
                continue
            seen.add(h)
        keep.append(i)
    order = np.asarray(keep, dtype=np.int64)
    sure = order[scores[order] >= thr["level_good"]]
    shown = order if incerti else sure
    page = shown[offset:offset + limit]
    items = _files_for(conn, [int(ids[i]) for i in page], scores[page], thr)
    return {"total": int(len(shown)), "items": items, "uncertain": int(len(order) - len(sure))}


@router.get("/search/status")
def search_status(conn=Depends(deps.get_db)):
    return _status_dict(conn)


@router.post("/search/warmup")
def search_warmup(conn=Depends(deps.get_db)):
    start_warmup()
    return _status_dict(conn)


class LabelTest(BaseModel):
    prompt: str = ""


@router.post("/labels/test")
def labels_test(body: LabelTest, conn=Depends(deps.get_db)):
    """Prova un'etichetta prima di salvarla, con la formula del piano (organizer.context_probs): il prompt
    compete con le etichette configurate e con lo sfondo generico; temperatura calibrata per modello."""
    prompt = " ".join(body.prompt.split()).lower()
    if not prompt:
        raise HTTPException(400, "Scrivi il testo dell'etichetta.")
    if len(prompt) > MAX_QUERY:
        raise HTTPException(400, f"Testo troppo lungo (massimo {MAX_QUERY} caratteri).")
    from ..organizer import context_probs  # import locale: organizer importa embedder/torch

    cfg = deps.get_cfg()
    emb = _require_embedder()
    # Stessa formula del piano: il nuovo prompt compete con le etichette gia' configurate e con lo sfondo generico.
    others = [l["prompt"] for l in cfg.labels if " ".join(l["prompt"].split()).lower() != prompt]
    tv = label_vectors(conn, others + [prompt], emb, cfg)
    null_vecs = label_vectors(conn, list(cfg.null_prompts), emb, cfg)
    ids, mat, is_video = _embedding_matrix(conn)
    if len(ids) == 0:
        return {"items": [], "coverage": {"above_threshold": 0, "total": 0}}
    if mat.shape[1] != tv.shape[1]:
        raise HTTPException(409, "Le foto sono state analizzate con un altro modello: rifai l'analisi "
                                 "(Impostazioni > Modello AI) prima di provare le etichette.")
    scale = float(dbm.get_meta(conn, "logit_scale", "100") or 100)
    p = context_probs(mat, tv, null_vecs, scale, cfg.context_temperature())
    me = len(others)
    prob = np.where(p.argmax(axis=1) == me, p[:, me], 0.0)
    mask = _candidate_mask(conn, ids, is_video, "")   # senza duplicati: stesso conteggio dell'anteprima
    above = int(np.count_nonzero((prob >= cfg.score_threshold) & mask))
    _, idx = _top(prob, mask, 24, 0)
    items = _files_for(conn, [int(ids[i]) for i in idx], prob[idx])
    return {"items": items, "coverage": {"above_threshold": above, "total": int(len(ids))}}


def _suggestion_stems(text: str) -> set[str]:
    """Prefissi (5 lettere, o la parola intera se più corta) delle parole significative di una frase."""
    return {w[:5] for w in text.split() if len(w) >= 3 and w not in SUGGESTION_STOPWORDS}


@router.get("/search/suggestions")
def search_suggestions():
    """Nomi delle etichette (in minuscolo) seguiti da frasi comuni, senza ripetizioni né quasi-duplicati."""
    out: list[str] = []
    stems: set[str] = set()  # prefissi delle parole già presenti in output
    for s in [l["name"].lower() for l in deps.get_cfg().labels]:
        if s not in out:
            out.append(s)
            stems |= _suggestion_stems(s)
    for s in COMMON_QUERIES:
        if s in out:
            continue
        words = _suggestion_stems(s)
        if words & stems:  # "tramonto" con "tramonti", "auto" con "auto e moto"...
            continue
        out.append(s)
        stems |= words
    return out[:24]
