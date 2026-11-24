"""Test delle route file (fotoscan/server/routes_files.py) con TestClient, senza avviare il server.

Lavora su una COPIA del catalogo (mai il catalogo vero). Esecuzione: `python -m pytest tests/test_api_files.py`
oppure `python tests/test_api_files.py`.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEED = ROOT / "catalogo.db"
_TMP = Path(tempfile.mkdtemp(prefix="fotoscan_files_"))
DB_COPY = _TMP / "catalogo.db"
os.environ["FOTOSCAN_DB"] = str(DB_COPY)
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")


def _copia_catalogo() -> None:
    """Snapshot consistente del catalogo vero (sola lettura), poi schema completo e dati di prova nel piano."""
    src = sqlite3.connect(f"file:{SEED}?mode=ro", uri=True)
    out = sqlite3.connect(DB_COPY)
    src.backup(out)
    out.close()
    src.close()
    from fotoscan import db as dbm

    conn = dbm.connect(str(DB_COPY))  # crea le tabelle mancanti (plan, favorites, ...)
    # piano di prova: una cartella con caratteri speciali e alcuni duplicati
    ids = [r[0] for r in conn.execute("SELECT id FROM files ORDER BY id LIMIT 300")]
    speciale = "2020/06 - Evento 100% _ 'x' & \"y\""
    for i, fid in enumerate(ids):
        folder = speciale if i < 40 else f"2019/0{1 + i % 9} - Varie"
        is_dup = 1 if i % 10 == 9 else 0
        dup_of = ids[0] if is_dup else None
        conn.execute(
            "INSERT OR REPLACE INTO plan(file_id, folder, context, score, is_dup, dup_of) VALUES(?,?,?,?,?,?)",
            (fid, folder, None, 0.5, is_dup, dup_of),
        )
    # date mancanti (per testare la coda "unknown") e un file con data incerta
    conn.execute("UPDATE files SET date_taken=NULL WHERE id IN (SELECT id FROM files ORDER BY id DESC LIMIT 25)")
    conn.commit()
    conn.close()


_copia_catalogo()

from fastapi.testclient import TestClient  # noqa: E402

from fotoscan import db as dbm  # noqa: E402
from fotoscan.server.app import app  # noqa: E402

client = TestClient(app)


def _db():
    return dbm.connect(str(DB_COPY))


def _get(url: str, **kw):
    r = client.get(url, **kw)
    return r


def _lista_tutta(query: str = "") -> list[dict]:
    """Tutti gli item di /files con paginazione da 500."""
    out, off = [], 0
    while True:
        r = _get(f"/api/files?offset={off}&limit=500{query}")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        out += items
        if len(items) < 500:
            return out
        off += 500


# ------------------------------------------------------------------ conteggi
def test_totali_coerenti_con_db_stats():
    from fotoscan import db as dbm2

    conn = _db()
    stats = dbm2.stats(conn)
    conn.close()
    r = _get("/api/files?dup=all&limit=1")
    assert r.json()["total"] == stats["indicizzati"] == 14245
    dup = _get("/api/files?dup=only&limit=1").json()["total"]
    hide = _get("/api/files?limit=1").json()["total"]
    assert hide + dup == 14245
    assert _get("/api/files?uncertain=1&dup=all&limit=1").json()["total"] == stats["data_incerta"]


def test_paginazione_stabile_e_completa():
    tutta = _lista_tutta()
    ids = [x["id"] for x in tutta]
    assert len(ids) == len(set(ids)) == _get("/api/files?limit=1").json()["total"]
    # le pagine concatenate coincidono con la lista intera (ordine stabile)
    pag = []
    for off in range(0, 1000, 120):
        pag += [x["id"] for x in _get(f"/api/files?offset={off}&limit=120").json()["items"]]
    assert pag == ids[: len(pag)] and len(pag) == 1080


def test_ordine_data_desc_con_date_nulle_e_incerte_in_coda():
    # Ordine: date certe decrescenti, poi senza data, poi le date incerte (gruppo "Data incerta").
    tutta = _lista_tutta("&dup=all")
    certe = [x for x in tutta if not x["date_uncertain"]]
    assert tutta[:len(certe)] == certe  # nessuna incerta prima delle certe
    date = [x["date"] for x in certe]
    con = [d for d in date if d]
    assert con == sorted(con, reverse=True)
    primo_null = date.index(None) if None in date else len(date)
    assert all(d is None for d in date[primo_null:])


# --------------------------------------------------------------- bucket
def test_bucket_giorno_coerenti_con_offset_di_files():
    buckets = _get("/api/timeline/buckets?granularity=day").json()
    assert buckets["total"] == _get("/api/files?limit=1").json()["total"]
    # ogni bucket: offset cumulativo e primo elemento di /files a quell'offset nella data del bucket
    cum = 0
    for b in buckets["buckets"]:
        assert b["offset"] == cum
        cum += b["count"]
        if b["key"] in ("unknown", "uncertain"):
            item = _get(f"/api/files?offset={b['offset']}&limit=1").json()["items"][0]
            assert item["date_uncertain"] is (b["key"] == "uncertain"), (b, item)
            continue
        item = _get(f"/api/files?offset={b['offset']}&limit=1").json()["items"][0]
        assert item["date"][:10] == b["key"], (b, item["date"])
    chiavi = [b["key"] for b in buckets["buckets"]]
    if "uncertain" in chiavi:
        assert chiavi[-1] == "uncertain"  # date incerte in fondo a tutto
    if "unknown" in chiavi and "uncertain" in chiavi:
        assert chiavi.index("unknown") < chiavi.index("uncertain")  # senza data prima delle incerte


def test_bucket_mese_con_filtri_e_totale():
    b = _get("/api/timeline/buckets?granularity=month&kind=video&favorite=0").json()
    assert b["total"] == _get("/api/files?kind=video&favorite=0&limit=1").json()["total"]
    for x in b["buckets"]:
        if x["key"] not in ("unknown", "uncertain"):
            item = _get(f"/api/files?kind=video&favorite=0&offset={x['offset']}&limit=1").json()["items"][0]
            assert item["date"][:7] == x["key"]


def test_bucket_con_dup_only_coerenti():
    b = _get("/api/timeline/buckets?granularity=month&dup=only").json()
    assert b["total"] == _get("/api/files?dup=only&limit=1").json()["total"]
    for x in b["buckets"]:
        if x["key"] not in ("unknown", "uncertain"):
            item = _get(f"/api/files?dup=only&offset={x['offset']}&limit=1").json()["items"][0]
            assert item["date"][:7] == x["key"]


def test_date_incerte_in_coda_con_date_desc():
    # Con sort=date_desc le date certe vengono prima di tutte le incerte (gruppo "Data incerta" in coda).
    certe = _get("/api/files?dup=all&uncertain=0&limit=1").json()["total"]
    totale = _get("/api/files?dup=all&limit=1").json()["total"]
    assert 0 < certe < totale
    in_coda = _get(f"/api/files?dup=all&offset={certe}&limit=500").json()["items"]
    assert in_coda and all(x["date_uncertain"] is True for x in in_coda)
    prima = _get(f"/api/files?dup=all&offset={certe - 1}&limit=1").json()["items"][0]
    assert prima["date_uncertain"] is False


def test_anni():
    anni = _get("/api/timeline/years?dup=all").json()
    senza_data = _get("/api/files?dup=all&limit=1").json()["total"] - sum(a["count"] for a in anni)
    assert senza_data == 25  # i 25 file senza data resi NULL nella copia di prova
    assert [a["year"] for a in anni] == sorted((a["year"] for a in anni), reverse=True)


# ---------------------------------------------------------------- filtri
def test_filtri_base():
    assert _get("/api/files?kind=video&dup=all&limit=1").json()["total"] == 2032
    t = _get("/api/files?kind=image&dup=all&limit=1").json()["total"]
    assert t == 12213
    for x in _get("/api/files?year=2019&dup=all&limit=500").json()["items"]:
        assert x["date"].startswith("2019")
    for x in _get("/api/files?year=2019&month=3&dup=all&limit=500").json()["items"]:
        assert x["date"][:7] == "2019-03"
    d = _get("/api/files?day=2019-03-10&dup=all&limit=500").json()["items"]
    assert all(x["date"][:10] == "2019-03-10" for x in d)


def test_filtro_cartella_con_caratteri_speciali():
    folder = "2020/06 - Evento 100% _ 'x' & \"y\""
    r = _get("/api/files", params={"folder": folder, "dup": "all", "limit": 500})
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and all(x["folder"] == folder for x in items)
    assert r.json()["total"] == 40


def test_filtro_preferiti_e_uncertain_e_sort():
    assert _get("/api/files?favorite=1&limit=1").json()["total"] == 0
    nome = _get("/api/files?sort=name&dup=all&limit=50").json()["items"]
    nomi = [x["name"].lower() for x in nome]
    assert nomi == sorted(nomi)
    assert _get("/api/files?uncertain=1&dup=all&limit=500").json()["items"][0]["date_uncertain"] is True


def test_ids_stessi_filtri_di_files():
    r = _get("/api/files/ids?kind=video&dup=all&limit=5000").json()
    assert r["total"] == 2032 and len(r["ids"]) == 2032
    primi = [x["id"] for x in _get("/api/files?kind=video&dup=all&limit=5").json()["items"]]
    assert _get("/api/files/ids?kind=video&dup=all&limit=5").json()["ids"] == primi


def test_parametri_invalidi_messaggi_italiani():
    casi = [
        "/api/files?kind=gif",
        "/api/files?limit=0",
        "/api/files?limit=abc",
        "/api/files?offset=-1",
        "/api/files?day=2020-02-30",
        "/api/files?month=13",
        "/api/files?sort=casuale",
        "/api/files?dup=forse",
        "/api/files?favorite=talvolta",
        "/api/timeline/buckets?granularity=anno",
        "/api/memories?on=ieri",
    ]
    for url in casi:
        r = _get(url)
        assert r.status_code in (400, 422), url
        assert isinstance(r.json()["detail"], str) and r.json()["detail"], url
        assert "must" not in r.json()["detail"] and "Input" not in r.json()["detail"], url


def test_limite_pagina_documentato_e_ids_per_tutti():
    # /files: limit massimo 500 (vedi docstring di routes_files.py); per "tutti gli id" c'e' /files/ids
    r = _get("/api/files?limit=5000")
    assert r.status_code == 422 and "500" in r.json()["detail"]
    ids = _get("/api/files/ids?limit=5000").json()
    assert len(ids["ids"]) == min(5000, ids["total"])


# ------------------------------------------------------------ dettaglio
def test_dettaglio_file_e_gruppo():
    dup = _get("/api/files?dup=only&limit=1").json()["items"][0]
    r = _get(f"/api/files/{dup['id']}")
    assert r.status_code == 200
    j = r.json()
    assert j["id"] == dup["id"]
    assert j["status"] == "embedded" and "hash" in j
    assert any(x["id"] == dup["id"] for x in j["group"])
    assert _get("/api/files/999999999").status_code == 404
    assert _get("/api/files/abc").status_code == 422


# -------------------------------------------------------------- ricordi
def test_memories_con_dati_reali():
    conn = _db()
    ref = conn.execute(
        "SELECT date_taken FROM files f LEFT JOIN plan p ON p.file_id=f.id WHERE f.status='embedded' "
        "AND COALESCE(p.is_dup,0)=0 AND date_taken IS NOT NULL AND date_taken < '2025-01-01' "
        "ORDER BY date_taken DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()
    giorno = "2025-" + ref[5:10]
    r = _get(f"/api/memories?on={giorno}&limit=50")
    assert r.status_code == 200
    gruppi = r.json()
    assert gruppi, "nessun ricordo per una data con foto"
    years = [g["year"] for g in gruppi]
    assert years == sorted(years, reverse=True) and all(y < 2025 for y in years)
    for g in gruppi:
        for it in g["items"]:
            assert it["date"][5:10] == ref[5:10] and int(it["date"][:4]) == g["year"]


# ------------------------------------------------------------ preferiti
def test_preferiti_singolo_e_massivo_scrivono_il_catalogo_isolato():
    base = _get("/api/files?dup=all&limit=1").json()["items"][0]
    fid = base["id"]
    r = client.post(f"/api/files/{fid}/favorite", json={"value": True})
    assert r.status_code == 200 and r.json() == {"favorite": True}
    assert _get(f"/api/files/{fid}").json()["favorite"] is True
    assert _get("/api/files?favorite=1&dup=all&limit=1").json()["total"] == 1

    ids = [x["id"] for x in _get("/api/files?dup=all&limit=6").json()["items"]]
    r = client.post("/api/files/favorite", json={"ids": ids, "value": True})
    assert r.status_code == 200 and r.json()["changed"] == len(ids) - 1  # fid gia' preferito
    r = client.post("/api/files/favorite", json={"ids": ids, "value": False})
    assert r.json()["changed"] == len(ids)
    assert _get("/api/files?favorite=1&dup=all&limit=1").json()["total"] == 0

    assert client.post("/api/files/999999999/favorite", json={"value": True}).status_code == 404
    assert client.post(f"/api/files/{fid}/favorite", json={"value": "si"}).status_code == 422
    assert client.post("/api/files/favorite", json={"ids": "x", "value": True}).status_code == 422
    assert client.post(f"/api/files/{fid}/favorite", json={"value": False}).json() == {"favorite": False}


# ------------------------------------------------------------- prestazioni
def test_risposte_sotto_100_ms():
    per_url = [
        "/api/files?offset=5000&limit=120",
        "/api/files?kind=image&sort=name&limit=120",
        "/api/files?year=2019&month=5&dup=all&limit=120",
        "/api/timeline/buckets?granularity=day",
        "/api/timeline/buckets?granularity=month&dup=all",
        "/api/timeline/years",
        "/api/memories?on=2025-06-15",
        "/api/files/ids?limit=5000",
    ]
    for url in per_url:
        _get(url)  # riscaldamento
        tempi = []
        for _ in range(3):  # il minimo riduce il rumore dovuto al carico della macchina
            t0 = time.perf_counter()
            r = _get(url)
            tempi.append((time.perf_counter() - t0) * 1000)
        dt = min(tempi)
        assert r.status_code == 200, url
        print(f"{dt:7.1f} ms  {url}")
        assert dt < 100, f"{url} lenta: {dt:.0f} ms"


def test_indici_creati_all_avvio():
    # lo startup dell'app (eseguito con il context manager del TestClient) crea gli indici
    with TestClient(app):
        conn = _db()
        nomi = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
    assert {"ix_files_date", "ix_plan_folder", "ix_files_status_date"} <= nomi


if __name__ == "__main__":
    import traceback

    ok = fail = 0
    for nome, fn in list(globals().items()):
        if nome.startswith("test_") and callable(fn):
            try:
                fn()
                ok += 1
                print("OK  ", nome)
            except Exception:
                fail += 1
                print("FAIL", nome)
                traceback.print_exc()
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{ok} ok, {fail} falliti")
    sys.exit(1 if fail else 0)
