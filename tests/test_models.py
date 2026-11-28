"""Test della calibrazione per modello (Impostazioni > Per esperti): profili, sostituzioni, modelli personalizzati,
calcoli della calibrazione automatica (senza modelli: dati sintetici).

Config.json e catalogo in una cartella temporanea (FOTOSCAN_CONFIG/FOTOSCAN_DB prima dell'import).
Esecuzione: `.\\.venv\\Scripts\\python.exe tests\\test_models.py`.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="models_test_"))
os.environ["FOTOSCAN_CONFIG"] = str(_TMP / "config.json")
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")
(_TMP / "config.json").write_text(json.dumps({"model_name": "google/siglip2-so400m-patch14-384"}), encoding="utf-8")

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from fotoscan import calibrate as cal  # noqa: E402
from fotoscan.config import BASE_SEARCH_THR, Config, all_models, model_profile  # noqa: E402
from fotoscan.server import routes_models, routes_search, routes_status  # noqa: E402
from fotoscan.server.deps import get_cfg  # noqa: E402

BASE, LARGE, SO = "google/siglip2-base-patch16-224", "google/siglip2-large-patch16-384", "google/siglip2-so400m-patch14-384"


def _request(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


@contextlib.contextmanager
def server():
    app = FastAPI()
    app.include_router(routes_status.router)
    app.include_router(routes_models.router)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        th.join(timeout=5)


# ------------------------------------------------------------ profili (config)
def test_profiles_presets_and_overrides():
    assert model_profile(BASE)["source"] == "preset" and model_profile(BASE)["search_thr"] == BASE_SEARCH_THR
    assert model_profile(SO)["source"] == "preset" and model_profile(SO)["temp_factor"] == 0.85
    assert model_profile(LARGE)["source"] == "base"                       # non calibrato: valori di base
    assert model_profile("x/y")["search_thr"] == BASE_SEARCH_THR
    c = Config(model_name=BASE, score_temperature=4.0,
               model_overrides={BASE: {"temp_factor": 0.5, "search_thr": {"min_gap": 0.08}, "source": "manuale"}})
    p = model_profile(BASE, c)
    assert p["source"] == "manuale" and p["temp_factor"] == 0.5
    assert p["search_thr"]["min_gap"] == 0.08 and p["search_thr"]["min_match"] == BASE_SEARCH_THR["min_match"]
    assert c.context_temperature() == 2.0
    c2 = Config(custom_models=[{"id": "a/b", "label": "Mio"}, {"id": BASE}])   # duplicato di un testato: ignorato
    ids = [m["id"] for m in all_models(c2)]
    assert ids.count(BASE) == 1 and "a/b" in ids and all_models(c2)[-1]["custom"] is True


# ------------------------------------------------------------ API
def test_api_override_reset_and_search_thresholds():
    with server() as base:
        code, d = _request(base, "GET", "/api/models")
        assert code == 200 and d["current"] == SO and d["reference_model"] == BASE
        so = next(m for m in d["models"] if m["id"] == SO)
        assert so["current"] and so["effective"]["source"] == "preset" and abs(so["effective"]["temperature"] - 3.4) < 1e-9
        # fuori limiti / ordine sbagliato
        assert _request(base, "PUT", "/api/models/profile", {"id": SO, "temp_factor": 9})[0] == 400
        assert _request(base, "PUT", "/api/models/profile", {"id": SO, "search_thr": {"level_good": 0.2, "level_ok": 0.15}})[0] == 400
        assert _request(base, "PUT", "/api/models/profile", {"id": "x/y", "temp_factor": 1})[0] == 404
        # sostituzione parziale: le altre soglie restano quelle del preset
        code, _ = _request(base, "PUT", "/api/models/profile", {"id": SO, "search_thr": {"min_gap": 0.1}})
        assert code == 200
        thr = routes_search.search_thresholds()
        assert thr["min_gap"] == 0.1 and thr["min_match"] == 0.100
        assert abs(get_cfg().context_temperature() - 3.4) < 1e-9          # temperatura invariata
        code, d = _request(base, "GET", "/api/models")
        so = next(m for m in d["models"] if m["id"] == SO)
        assert so["effective"]["source"] == "manuale" and so["override"]["search_thr"]["min_gap"] == 0.1
        # ripristino
        assert _request(base, "DELETE", "/api/models/profile?id=" + urllib.parse.quote(SO))[0] == 200
        assert routes_search.search_thresholds()["min_gap"] == 0.090
        assert SO not in get_cfg().model_overrides


def test_api_custom_models():
    orig = routes_models._inspect_model
    routes_models._inspect_model = lambda mid: "clip"                       # niente rete nei test
    try:
        with server() as base:
            assert _request(base, "POST", "/api/models/custom", {"id": "non valido"})[0] == 400
            code, e = _request(base, "POST", "/api/models/custom", {"id": "openai/clip-vit-base-patch32", "label": "CLIP"})
            assert code == 200 and "inglese" in e["hint"]
            assert _request(base, "POST", "/api/models/custom", {"id": "openai/clip-vit-base-patch32"})[0] == 409
            code, st = _request(base, "GET", "/api/status")
            assert any(m["id"] == "openai/clip-vit-base-patch32" for m in st["available_models"])
            # calibrazione manuale anche per i modelli personalizzati, poi rimozione (toglie anche la calibrazione)
            assert _request(base, "PUT", "/api/models/profile", {"id": "openai/clip-vit-base-patch32", "temp_factor": 1.3})[0] == 200
            assert _request(base, "DELETE", "/api/models/custom?id=" + urllib.parse.quote(SO))[0] == 409   # in uso
            assert _request(base, "DELETE", "/api/models/custom?id=openai%2Fclip-vit-base-patch32")[0] == 200
            cfg = get_cfg()
            assert not cfg.custom_models and "openai/clip-vit-base-patch32" not in cfg.model_overrides
    finally:
        routes_models._inspect_model = orig


# ------------------------------------------------------------ calcoli della calibrazione automatica
def _unit(x):
    return (x / np.linalg.norm(x, axis=-1, keepdims=True)).astype(np.float32)


def test_calibrate_rejects_invalid_sample_size_and_double_start():
    from fotoscan.server import jobs
    import threading

    with server() as base:
        # "n" non numerico: 400 (prima era un 500 per ValueError)
        code, _ = _request(base, "POST", "/api/models/calibrate", {"id": SO, "n": "abc"})
        assert code == 400, code
        code, _ = _request(base, "POST", "/api/models/calibrate", {"id": SO, "n": True})
        assert code == 400, code
        # un job gia' in corso (anche una misura hardware) blocca l'avvio in modo atomico
        gate = threading.Event()
        job, created = jobs.start_job_unique("hardware_bench", lambda j: gate.wait(5))
        try:
            assert created
            code, _ = _request(base, "POST", "/api/models/calibrate", {"id": SO})
            assert code == 409, code
        finally:
            gate.set()


def test_models_list_uses_complete_cache_check():
    home = Path(tempfile.mkdtemp(prefix="hf_models_"))
    old = os.environ.get("HF_HUB_CACHE")
    os.environ["HF_HUB_CACHE"] = str(home)
    try:
        snap = home / ("models--" + SO.replace("/", "--")) / "snapshots" / "abc"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")             # download interrotto: niente pesi
        with server() as base:
            code, d = _request(base, "GET", "/api/models")
            assert next(m for m in d["models"] if m["id"] == SO)["cached"] is False
            (snap / "model.safetensors").write_bytes(b"x")
            code, d = _request(base, "GET", "/api/models")
            assert next(m for m in d["models"] if m["id"] == SO)["cached"] is True
    finally:
        if old is None:
            os.environ.pop("HF_HUB_CACHE", None)
        else:
            os.environ["HF_HUB_CACHE"] = old


def test_fit_levels_matches_quantiles():
    rng = np.random.default_rng(0)
    ref = rng.normal(0.08, 0.03, (500, 20))
    tgt = ref * 0.8 + 0.01                                                  # stessa classifica, scala diversa
    out = cal.fit_levels(ref, tgt)
    for k in ("min_match", "level_good", "level_ok"):
        assert abs(out[k] - (BASE_SEARCH_THR[k] * 0.8 + 0.01)) < 0.003, (k, out[k])
    assert out["min_match"] <= out["level_good"] <= out["level_ok"]


def test_fit_temp_factor_recovers_scale():
    rng = np.random.default_rng(1)
    d, n = 32, 600
    lab, nul = _unit(rng.normal(size=(6, d))), _unit(rng.normal(size=(3, d)))
    mat = _unit(rng.normal(size=(n, d)) + 2.0 * lab[rng.integers(0, 6, n)])
    ref = {"mat": mat, "lab": lab, "nul": nul, "scale": 20.0}
    tgt = {**ref, "scale": 25.0}                    # logit piu' larghi del 25%: serve una temperatura piu' alta
    r = cal.fit_temp_factor(ref, tgt, 4.0, 0.3)
    assert abs(r["temp_factor"] - 1.25) < 0.05 and r["agreement"] > 0.95, r


def test_fit_min_gap():
    assert cal.fit_min_gap(0.07, None, None) == {"min_gap": 0.07, "gap_estimated": True}
    real = np.linspace(0.10, 0.20, 30)
    junk = np.r_[np.linspace(0.03, 0.08, 18), [0.095, 0.12]]
    g = cal.fit_min_gap(0.07, real, junk)               # la stima (0,07) lascerebbe passare le frasi senza senso
    assert 0.08 <= g["min_gap"] <= 0.11 and g["real_kept"] >= 27 and g["junk_rejected"] >= 18, g


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} test superati")
