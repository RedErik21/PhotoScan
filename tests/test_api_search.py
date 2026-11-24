"""Test delle route di ricerca (/search, /search/status, /search/warmup, /labels/test, /search/suggestions).

Il modello vero non si carica mai: l'Embedder e' sostituito da un finto con vettori a 4 dimensioni
(una per "concetto": mare, cane, torta, altro). Nessuna dipendenza da pytest.
Esecuzione: `python tests\\test_api_search.py` oppure `python -m pytest tests\\test_api_search.py`.
"""
from __future__ import annotations

import contextlib
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import db as dbm  # noqa: E402
from fotoscan.config import Config  # noqa: E402
from fotoscan.server import deps, routes_search as rs  # noqa: E402
from fotoscan.server.app import app  # noqa: E402

DIM = 4
CONCEPTS = {"mare": 0, "cane": 1, "torta": 2}


def vec_for(text: str) -> np.ndarray:
    if "xqzwv" in text:  # query senza senso: stesso coseno (0.5) con tutti i concetti, nessuno si stacca
        return np.full(DIM, 0.5, dtype=np.float32)
    v = np.zeros(DIM, dtype=np.float32)
    v[next((i for w, i in CONCEPTS.items() if w in text), 3)] = 1.0
    return v


class FakeEmbedder:
    def __init__(self, cfg, device=None):
        self.cfg = cfg
        self.logit_scale = 100.0
        self.logit_bias = -16.0

    def encode_texts(self, texts):
        return np.stack([vec_for(t.lower()) for t in texts]).astype(np.float32)


class FailingEmbedder:
    def __init__(self, cfg, device=None):
        raise RuntimeError("modello mancante")


@contextlib.contextmanager
def fresh_env(model_loaded=True, embedder=FakeEmbedder):
    """Catalogo temporaneo con 6 file (uno e' un duplicato), stato del modulo azzerato, dipendenze finte."""
    tmp = Path(tempfile.mkdtemp(prefix="fs_search_"))
    cfg = Config(db_path=str(tmp / "c.db"), cache_dir=str(tmp / "cache"))
    # db.SCHEMA crea l'indice ix_plan_folder prima della tabella plan: su un catalogo nuovo serve la tabella prima
    raw = sqlite3.connect(cfg.db_path)
    raw.executescript("CREATE TABLE plan(file_id INTEGER PRIMARY KEY, folder TEXT, context TEXT, score REAL, "
                      "is_dup INTEGER DEFAULT 0, dup_of INTEGER);")
    raw.close()
    conn = dbm.connect(cfg.db_path)
    files = [  # (id, nome, tipo, concetto, duplicato)
        (1, "mare.jpg", "image", "mare", False),
        (2, "cane.jpg", "image", "cane", False),
        (3, "mare.mp4", "video", "mare", False),
        (4, "torta.jpg", "image", "torta", False),
        (5, "mare-copia.jpg", "image", "mare", True),
        (6, "altro.jpg", "image", "altro", False),
    ]
    for fid, name, kind, concept, dup in files:
        conn.execute("INSERT INTO files(id, path, name, kind, status) VALUES(?,?,?,?,'embedded')",
                     (fid, f"D:/foto/{name}", name, kind))
        conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(?,?)", (fid, dbm.to_blob(vec_for(concept))))
        if dup:
            conn.execute("INSERT INTO plan(file_id, folder, is_dup, dup_of) VALUES(?,NULL,1,1)", (fid,))
    conn.execute("INSERT INTO meta(key,value) VALUES('logit_scale','100')")
    conn.commit()

    def get_db_override():
        c = dbm.connect(cfg.db_path)
        try:
            yield c
        finally:
            c.close()

    try:
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(deps, "get_cfg", lambda: cfg))
            st.enter_context(mock.patch.object(rs, "Embedder", embedder))
            st.enter_context(mock.patch.object(rs, "_emb", embedder(cfg, device="cpu") if model_loaded else None))
            st.enter_context(mock.patch.object(rs, "_loading", False))
            st.enter_context(mock.patch.object(rs, "_error", None))
            st.enter_context(mock.patch.object(rs, "_mat_cache", None))
            rs._qcache.clear()
            app.dependency_overrides[deps.get_db] = get_db_override
            try:
                yield {"cfg": cfg, "conn": conn, "client": TestClient(app), "st": st}
            finally:
                app.dependency_overrides.pop(deps.get_db, None)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def wait_until(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def ids_of(resp):
    return [f["id"] for f in resp.json()["items"]]


# ------------------------------------------------------------ stato del modello
def test_search_503_while_model_loading():
    with fresh_env(model_loaded=False) as env:
        env["st"].enter_context(mock.patch.object(rs, "start_warmup", lambda: None))
        env["st"].enter_context(mock.patch.object(rs, "_loading", True))
        r = env["client"].get("/api/search", params={"q": "mare"})
        assert r.status_code == 503
        assert r.json()["detail"] == "Sto preparando la ricerca..."


def test_search_triggers_lazy_load_and_503():
    calls = []
    with fresh_env(model_loaded=False) as env:
        env["st"].enter_context(mock.patch.object(rs, "start_warmup", lambda: calls.append(1)))
        r = env["client"].get("/api/search", params={"q": "mare"})
        assert r.status_code == 503 and calls == [1]


def test_warmup_loads_model_in_background():
    with fresh_env(model_loaded=False) as env:
        c = env["client"]
        st = c.post("/api/search/warmup").json()
        assert set(st) >= {"model_loaded", "loading", "indexed"}
        assert st["indexed"] == 6
        assert wait_until(lambda: c.get("/api/search/status").json()["model_loaded"])
        st = c.get("/api/search/status").json()
        assert st["loading"] is False and st["error"] is None and st["load_seconds"] is not None
        assert c.get("/api/search", params={"q": "mare"}).status_code == 200


def test_load_error_is_reported_in_italian():
    with fresh_env(model_loaded=False, embedder=FailingEmbedder) as env:
        c = env["client"]
        c.post("/api/search/warmup")
        assert wait_until(lambda: not c.get("/api/search/status").json()["loading"])
        st = c.get("/api/search/status").json()
        assert st["model_loaded"] is False and "modello mancante" in st["error"]
        r = c.get("/api/search", params={"q": "mare"})
        assert r.status_code == 503 and "non e' disponibile" in r.json()["detail"]


# ------------------------------------------------------------ ricerca
def test_search_ranks_and_hides_duplicates():
    with fresh_env() as env:
        r = env["client"].get("/api/search", params={"q": "Mare"})
        body = r.json()
        assert r.status_code == 200
        assert body["total"] == 2  # file 1 e 3; il duplicato 5 e' nascosto
        assert set(ids_of(r)) == {1, 3}
        assert abs(body["items"][0]["match"] - 1.0) < 1e-3
        assert {"id", "thumb", "display", "media", "date_uncertain", "is_dup"} <= set(body["items"][0])


def test_search_kind_filter_and_pagination():
    with fresh_env() as env:
        c = env["client"]
        assert ids_of(c.get("/api/search", params={"q": "mare", "kind": "video"})) == [3]
        assert ids_of(c.get("/api/search", params={"q": "mare", "kind": "image"})) == [1]
        page = c.get("/api/search", params={"q": "mare", "limit": 1, "offset": 1}).json()
        assert page["total"] == 2 and [f["id"] for f in page["items"]] == [3]


def test_search_validation_errors():
    with fresh_env() as env:
        c = env["client"]
        r = c.get("/api/search", params={"q": "   "})
        assert r.status_code == 400 and r.json()["detail"]
        assert c.get("/api/search", params={"q": "mare", "kind": "audio"}).status_code == 400
        r = c.get("/api/search", params={"q": "x" * 201})
        assert r.status_code == 400 and "troppo lungo" in r.json()["detail"]


def test_matrix_is_rebuilt_when_embeddings_change():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        assert c.get("/api/search", params={"q": "cane"}).json()["total"] == 1
        conn.execute("INSERT INTO files(id, path, name, kind, status) "
                     "VALUES(7,'D:/foto/cane2.jpg','cane2.jpg','image','embedded')")
        conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(7, ?)", (dbm.to_blob(vec_for("cane")),))
        conn.commit()
        r = c.get("/api/search", params={"q": "cane"}).json()
        assert r["total"] == 2 and r["items"][0]["id"] in (2, 7)


def test_search_on_empty_catalog():
    with fresh_env() as env:
        env["conn"].execute("DELETE FROM embeddings")
        env["conn"].commit()
        r = env["client"].get("/api/search", params={"q": "mare"})
        assert r.status_code == 200 and r.json() == {"total": 0, "items": []}


# ------------------------------------------------------------ prova etichette
def test_labels_test_top_and_coverage():
    with fresh_env() as env:
        # Etichette configurate che competono con il prompt provato (come nel piano): una per il mare.
        env["cfg"].labels = [{"name": "Mare", "prompt": "il mare"}]
        r = env["client"].post("/api/labels/test", json={"prompt": "un cane"})
        assert r.status_code == 200
        body = r.json()
        # Stessa formula del piano: conta solo dove "un cane" batte le altre etichette E lo sfondo generico.
        # cane.jpg -> ~1. mare.jpg/mare.mp4 vanno all'etichetta "Mare"; torta.jpg non e' un cane (pari merito
        # con lo sfondo: non deve contare); "altro" coincide con lo sfondo; il duplicato e' escluso.
        assert body["items"][0]["id"] == 2
        assert body["items"][0]["match"] > 0.99
        assert body["coverage"]["total"] == 6
        assert body["coverage"]["above_threshold"] == 1  # prima (formula vecchia) erano 5 su 6: conteggio gonfiato
        assert len(body["items"]) <= 24


def test_labels_test_validation_and_loading():
    with fresh_env(model_loaded=False) as env:
        c = env["client"]
        env["st"].enter_context(mock.patch.object(rs, "start_warmup", lambda: None))
        assert c.post("/api/labels/test", json={"prompt": "cane"}).status_code == 503
        env["st"].enter_context(mock.patch.object(rs, "_emb", FakeEmbedder(None)))
        r = c.post("/api/labels/test", json={"prompt": "   "})
        assert r.status_code == 400 and "etichetta" in r.json()["detail"]


def test_nonsense_query_has_no_results():
    with fresh_env() as env:
        r = env["client"].get("/api/search", params={"q": "xqzwv nulla"})
        assert r.status_code == 200 and r.json() == {"total": 0, "items": []}


def test_absolute_floor_and_levels():
    scores = np.array([0.20, 0.13, 0.125, 0.11, 0.10, 0.02], dtype=np.float32)
    mask = np.ones(len(scores), dtype=bool)
    total, idx = rs._top(scores, mask, 10, 0, floor=rs.MIN_MATCH)
    assert total == 4 and list(idx) == [0, 1, 2, 3]  # sotto 0.105 nessun risultato, anche se e' il solo
    total, idx = rs._top(scores, mask, 2, 1, floor=rs.MIN_MATCH)
    assert total == 4 and list(idx) == [1, 2]  # paginazione: il totale non cambia
    assert rs.match_level(0.16) == "ottima" and rs.match_level(0.125) == "buona"
    assert rs.match_level(0.11) == "possibile"


def test_items_carry_quality_label_not_percentage():
    with fresh_env() as env:
        body = env["client"].get("/api/search", params={"q": "mare"}).json()
        assert all(f["match_level"] in ("ottima", "buona", "possibile") for f in body["items"])
        assert body["items"][0]["match_level"] == "ottima"


# ------------------------------------------------------------ suggerimenti
def test_suggestions_labels_then_common_without_duplicates():
    with fresh_env() as env:
        out = env["client"].get("/api/search/suggestions").json()
        assert out[0] == "mare e spiaggia"  # prima le etichette del catalogo
        assert "cane" in out and "montagna" in out  # frasi comuni nuove restano
        # niente quasi-doppioni delle etichette ("tramonti" -> no "tramonto", "auto e moto" -> no "auto")
        assert "tramonto" not in out and "auto" not in out and "mare" not in out
        assert len(out) == len(set(out)) <= 24


# ------------------------------------------------------------ calibrazione per modello
def test_thresholds_are_calibrated_per_model():
    base, so = "google/siglip2-base-patch16-224", "google/siglip2-so400m-patch14-384"
    # base: i valori tarati a mano; so400m: coseni su scala piu' stretta -> soglie piu' basse
    assert rs.search_thresholds(base) == {"min_match": rs.MIN_MATCH, "min_gap": rs.MIN_GAP,
                                          "level_good": rs.LEVEL_GOOD, "level_ok": rs.LEVEL_OK}
    t = rs.search_thresholds(so)
    assert t["min_match"] < rs.MIN_MATCH and t["level_ok"] < rs.LEVEL_OK and t["level_good"] < t["level_ok"]
    assert rs.search_thresholds("modello/sconosciuto")["level_ok"] == rs.LEVEL_OK      # ripiego: base
    assert rs.match_level(0.13, t) == "ottima" and rs.match_level(0.13) == "buona"   # stesso coseno, modello diverso
    # contesti: temperatura effettiva = score_temperature x fattore del modello
    assert Config(model_name=base, score_temperature=4.0).context_temperature() == 4.0
    assert abs(Config(model_name=so, score_temperature=4.0).context_temperature() - 3.4) < 1e-9
    assert Config(model_name="modello/sconosciuto", score_temperature=4.0).context_temperature() == 4.0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} test superati")
