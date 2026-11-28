"""Etichette suggerite: temi ricorrenti scoperti raggruppando le foto simili (k-means sugli embedding).

Le foto gia' coperte da un'etichetta (testo o esempi) e le copie duplicate restano fuori. Un gruppo diventa un
suggerimento solo se torna in molti giorni e mesi diversi (palestra, scuola, negozio...): gli eventi singoli no.
Accettato, il gruppo diventa un'"etichetta da esempi": il suo vettore medio riconosce le foto simili nel piano.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Callable

import numpy as np

from .config import Config
from .db import from_blob, load_embeddings, to_blob
from .embedder import Embedder, label_vectors

# Nomi candidati (italiano). Il nome proposto e' il piu' vicino al centro del gruppo; l'utente lo puo' cambiare.
VOCAB = [
    "Palestra", "Ufficio e lavoro", "Scuola", "Aula e lezione", "Appunti e lavagna", "Libri", "Computer e scrivania",
    "Cucina di casa", "Camera da letto", "Bagno", "Giardino", "Balcone e terrazzo", "Lavori in casa", "Arredamento",
    "Matrimonio", "Battesimo", "Laurea", "Chiesa", "Ospedale", "Medico e dentista",
    "Calcio", "Stadio", "Piscina", "Corsa", "Bicicletta", "Sci", "Montagna", "Bosco ed escursioni", "Lago", "Fiume",
    "Campagna", "Mare", "Barca", "Aereo e aeroporto", "Treno e stazione", "Viaggio in auto", "Interno dell'auto",
    "Moto", "Strade di notte", "Città di notte", "Fuochi d'artificio", "Bar e caffè", "Pizza", "Dolci",
    "Aperitivo e cocktail", "Supermercato", "Negozio e shopping", "Vestiti e moda", "Scarpe", "Trucco e unghie",
    "Parrucchiere", "Tatuaggi", "Cane", "Gatto", "Cavalli", "Uccelli", "Fiori", "Piante", "Cielo e nuvole",
    "Pioggia", "Neve", "Museo e mostre", "Castelli", "Teatro e spettacoli", "Cinema", "Videogiochi", "Televisione",
    "Musica e strumenti", "Ballo", "Carnevale", "Halloween", "Pasqua", "Regali", "Biglietti", "Mappe",
    "Meme e immagini divertenti", "Chat e messaggi", "Pagine web", "Disegni", "Fumetti", "Vecchie foto stampate",
    "Cantiere", "Officina", "Fattoria", "Piazze", "Monumenti",
]
# Gruppi di persone (selfie, ritratti): troppo generici per diventare una cartella.
PEOPLE = ["Selfie", "Selfie allo specchio", "Ritratti", "Foto di gruppo", "Coppia", "Bambini", "Neonati", "Nonni"]

MIN_SIZE = 30        # foto minime di un gruppo
MIN_DAYS = 8         # giorni diversi minimi: un tema ricorrente, non un evento
MIN_MONTHS = 3
MAX_SUGGESTIONS = 12
SAME_THEME = 0.90    # coseno oltre il quale due centri sono lo stesso tema (gia' accettato o ignorato)


def _prompt(name: str) -> str:
    return f"una foto di {name.lower()}"


def visual_labels(conn: sqlite3.Connection) -> list[dict]:
    return [{"name": r["name"], "vec": from_blob(r["vec"]), "thr": r["thr"]}
            for r in conn.execute("SELECT name, vec, thr FROM visual_labels ORDER BY name")]


def assign_visual(conn: sqlite3.Connection, ids: np.ndarray, mat: np.ndarray, taken: np.ndarray) -> dict[int, tuple[str, float]]:
    """{file_id: (nome, coseno)} per le foto non ancora assegnate (taken=False) vicine a un'etichetta da esempi."""
    labels = visual_labels(conn)
    if not labels or len(ids) == 0:
        return {}
    labels = [l for l in labels if l["vec"].shape[0] == mat.shape[1]]  # etichette di un altro modello: ignorate
    if not labels:
        return {}
    V = np.stack([l["vec"] for l in labels])
    thr = np.array([l["thr"] for l in labels], dtype=np.float32)
    sims = mat @ V.T
    out: dict[int, tuple[str, float]] = {}
    best = sims.argmax(axis=1)
    for i in np.nonzero(~taken)[0]:
        j = best[i]
        if sims[i, j] >= thr[j]:
            out[int(ids[i])] = (labels[j]["name"], float(sims[i, j]))
    return out


def _kmeans(X: np.ndarray, k: int, iters: int = 25, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    import torch

    dev = "cpu"  # niente contesa della GPU con l'indicizzazione
    T = torch.from_numpy(X).to(dev)
    g = torch.Generator(device=dev).manual_seed(seed)
    C = T[torch.randperm(len(T), generator=g)[:k]].clone()
    for _ in range(iters):
        a = (T @ C.T).argmax(1)
        for j in range(k):
            m = a == j
            if bool(m.any()):
                c = T[m].mean(0)
                C[j] = c / c.norm()
    a = (T @ C.T).argmax(1)
    return a.numpy(), C.numpy()


def discover(conn: sqlite3.Connection, cfg: Config, embedder: Embedder | None = None,
             progress: Callable[[str], None] | None = None) -> list[dict]:
    """Calcola i suggerimenti e li salva in label_suggestions (sostituendo i precedenti)."""
    say = progress or (lambda _m: None)
    from .organizer import score_contexts  # import locale: organizer importa questo modulo

    say("Leggo le foto")
    ids, mat = load_embeddings(conn)
    if len(ids) == 0:
        return []
    from .embedder import LazyTextEmbedder

    own = embedder is None
    if own:  # un solo caricamento del modello di testo per contesti e vocabolario (e solo se serve)
        embedder = LazyTextEmbedder(cfg)
    try:
        return _discover(conn, cfg, embedder, ids, mat, say)
    finally:
        if own:
            embedder.release()


def _discover(conn, cfg, embedder, ids, mat, say) -> list[dict]:
    from .organizer import score_contexts

    _, best, score = score_contexts(conn, cfg, embedder, emb_data=(ids, mat))
    good = (best >= 0) & (score >= cfg.score_threshold)
    rows = {r["id"]: r for r in conn.execute(
        "SELECT f.id, f.kind, f.date_taken, f.data_incerta, COALESCE(p.is_dup,0) AS is_dup "
        "FROM files f LEFT JOIN plan p ON p.file_id=f.id WHERE f.status='embedded'")}
    # solo foto (i fotogrammi dei video, spesso bui o mossi, si somigliano tutti e sporcano i gruppi)
    images = np.array([int(f) in rows and rows[int(f)]["kind"] == "image" for f in ids], dtype=bool)
    vis = assign_visual(conn, ids, mat, good | ~images)
    keep = np.array([images[i] and not good[i] and int(fid) not in vis and rows[int(fid)]["is_dup"] == 0
                     and rows[int(fid)]["date_taken"] for i, fid in enumerate(ids)], dtype=bool)
    X, fids = mat[keep].astype(np.float32), ids[keep]
    if len(X) < MIN_SIZE * 2:
        _save(conn, [])
        return []

    say("Raggruppo le foto simili")
    k = int(min(150, max(20, len(X) // 250)))
    assign, C = _kmeans(X, k)
    sims_all = (X * C[assign]).sum(axis=1)

    say("Scelgo i nomi")
    names = VOCAB + PEOPLE
    Tv = label_vectors(conn, [_prompt(n) for n in names], embedder, cfg)
    known = [from_blob(r["vec"]) for r in conn.execute("SELECT vec FROM visual_labels")]
    known += [from_blob(r["vec"]) for r in conn.execute("SELECT vec FROM label_dismissed")]
    # vettori salvati con un altro modello (dimensione diversa, es. "ignorati" dopo un cambio modello): non confrontabili
    known = [v for v in known if v.shape[0] == mat.shape[1]]
    K = np.stack(known) if known else None

    out = []
    for j in range(k):
        m = assign == j
        n = int(m.sum())
        if n < MIN_SIZE:
            continue
        days = {rows[int(f)]["date_taken"][:10] for f in fids[m]}
        months = {d[:7] for d in days}
        if len(days) < MIN_DAYS or len(months) < MIN_MONTHS:
            continue
        c = C[j]
        order = np.argsort(-(Tv @ c))
        if names[order[0]] in PEOPLE:
            continue
        if K is not None and float((K @ c).max()) >= SAME_THEME:
            continue
        cands = [names[i] for i in order if names[i] not in PEOPLE]
        member_sims = sims_all[m]
        # soglia: quella di un terzo delle foto del gruppo, mai sotto 0,80 (sotto entrano foto a caso)
        thr = float(max(0.80, np.percentile(member_sims, 35)))
        sample = fids[m][np.argsort(-member_sims)][:9]
        out.append(dict(name=cands[0], alt=cands[1], vec=c, thr=thr, n=n, days=len(days), months=len(months),
                        sample=[int(x) for x in sample]))
    # temi uguali in piu' gruppi (stesso nome proposto): si tiene il piu' ricorrente, gli altri si uniscono
    merged: dict[str, dict] = {}
    for s in sorted(out, key=lambda s: -s["days"]):
        if s["name"] in merged:
            m = merged[s["name"]]
            w = m["n"] + s["n"]
            v = (m["vec"] * m["n"] + s["vec"] * s["n"]) / w
            m["vec"] = v / np.linalg.norm(v)
            m["n"], m["thr"] = w, min(m["thr"], s["thr"])
            m["days"] += s["days"]
            continue
        merged[s["name"]] = s
    result = sorted(merged.values(), key=lambda s: -s["days"])[:MAX_SUGGESTIONS]
    _save(conn, result)
    return result


def _save(conn: sqlite3.Connection, items: list[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM label_suggestions")
    conn.executemany(
        "INSERT INTO label_suggestions(name, alt, vec, thr, n, days, months, sample, created) VALUES(?,?,?,?,?,?,?,?,?)",
        [(s["name"], s["alt"], to_blob(s["vec"]), s["thr"], s["n"], s["days"], s["months"], json.dumps(s["sample"]), now)
         for s in items])
    conn.commit()
