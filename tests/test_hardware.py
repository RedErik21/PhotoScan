"""Test di fotoscan.hardware senza GPU e senza modelli: consiglio, adattamento alla memoria, stime e misure salvate.

Esecuzione: `python tests/test_hardware.py` (come gli altri file) oppure `python -m pytest tests/test_hardware.py`.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import hardware as hw_mod  # noqa: E402
from fotoscan.config import AVAILABLE_MODELS  # noqa: E402

BASE, LARGE, SO400M = (m["id"] for m in sorted(AVAILABLE_MODELS, key=lambda m: m["rank"]))
LIB = {"photos": 1000, "videos": 100, "pending_photos": 1000, "pending_videos": 100}


def _hw(device="cuda", vram=8.0, ram=24.0, threads=16, name="GPU di prova"):
    gpu = {"name": name, "vendor": "nvidia", "vram_gb": vram, "driver": "1", "compute_cap": None} if device == "cuda" else None
    return {"os": "test", "cpu": {"name": "CPU di prova", "cores": threads // 2, "threads": threads}, "ram_gb": ram,
            "gpus": [gpu] if gpu else [], "gpu": gpu, "torch": {"installed": True, "version": "x", "cuda_build": device == "cuda"},
            "device": device, "issues": []}


def _report(hw, bench=None):
    return hw_mod.build_report(hw, AVAILABLE_MODELS, LIB, bench, {}, fractions=3)


def test_big_gpu_gets_most_accurate_model():
    rep = _report(_hw(vram=8))
    assert rep["recommendation"]["model"] == SO400M
    assert all(m["fits"] for m in rep["models"])
    assert rep["recommendation"]["batch_size"] <= hw_mod.MAX_BATCH


def test_small_gpu_gets_a_smaller_model_and_smaller_batch():
    rep = _report(_hw(vram=4))
    by_id = {m["id"]: m for m in rep["models"]}
    assert not by_id[SO400M]["fits"]
    assert rep["recommendation"]["model"] == LARGE
    assert by_id[LARGE]["batch_size"] < hw_mod.MAX_BATCH


def test_tiny_gpu_falls_back_to_lightest():
    rep = _report(_hw(vram=2))
    assert rep["recommendation"]["model"] == BASE
    assert rep["recommendation"]["batch_size"] >= 4


def test_cpu_only_recommends_lightest_and_explains_why():
    rep = _report(_hw(device="cpu"))
    assert rep["recommendation"]["model"] == BASE
    assert "scheda video" in rep["recommendation"]["reason"]


def test_not_enough_ram_on_cpu_excludes_big_models():
    rep = _report(_hw(device="cpu", ram=6))
    by_id = {m["id"]: m for m in rep["models"]}
    assert by_id[BASE]["fits"] and not by_id[SO400M]["fits"]


def test_no_measurements_means_no_speed_claims():
    rep = _report(_hw())
    assert all("per_s" not in m for m in rep["models"])
    assert all(m["eta_total_s"] is None for m in rep["models"])


def test_speed_is_the_slower_of_compute_and_photo_reading():
    hw = _hw()
    bench = {"fingerprint": hw_mod.fingerprint(hw), "results": {SO400M: {"encode_per_s": 40.0, "prep_per_s": 15.0}}}
    m = {x["id"]: x for x in _report(hw, bench)["models"]}[SO400M]
    assert m["per_s"] == 15.0 and m["bottleneck"] == "foto" and m["estimated"] is False
    bench["results"][SO400M]["prep_per_s"] = 90.0
    m = {x["id"]: x for x in _report(hw, bench)["models"]}[SO400M]
    assert m["per_s"] == 40.0 and m["bottleneck"] == "calcolo"


def test_unmeasured_models_are_extrapolated_from_cost_and_flagged():
    hw = _hw()
    bench = {"fingerprint": hw_mod.fingerprint(hw), "results": {SO400M: {"encode_per_s": 30.0, "prep_per_s": None}}}
    by_id = {x["id"]: x for x in _report(hw, bench)["models"]}
    assert by_id[BASE]["estimated"] is True and by_id[SO400M]["estimated"] is False
    assert by_id[BASE]["per_s"] > by_id[LARGE]["per_s"] > by_id[SO400M]["per_s"]   # piu' leggero = piu' veloce


def test_eta_counts_video_frames():
    hw = _hw()
    bench = {"fingerprint": hw_mod.fingerprint(hw), "results": {SO400M: {"encode_per_s": 10.0, "prep_per_s": None}}}
    rep = _report(hw, bench)
    m = {x["id"]: x for x in rep["models"]}[SO400M]
    assert rep["library"]["frames_total"] == 1000 + 100 * 3
    assert m["eta_total_s"] == 130          # 1300 immagini / 10 al secondo


def test_stale_measurements_from_another_machine_are_ignored():
    hw = _hw()
    bench = {"fingerprint": "cuda|un'altra scheda|16", "stale": True,
             "results": {SO400M: {"encode_per_s": 99.0, "prep_per_s": None}}}
    assert all("per_s" not in m for m in _report(hw, bench)["models"])


def test_slow_machine_prefers_a_model_that_finishes_in_time():
    hw = _hw()
    lib = {"photos": 100_000, "videos": 0, "pending_photos": 100_000, "pending_videos": 0}
    bench = {"fingerprint": hw_mod.fingerprint(hw), "results": {
        BASE: {"encode_per_s": 60.0, "prep_per_s": None}, SO400M: {"encode_per_s": 3.0, "prep_per_s": None}}}
    rep = hw_mod.build_report(hw, AVAILABLE_MODELS, lib, bench, {}, fractions=3)
    # so400m: 100000/3 = 9,3 ore (> 6 h) -> il consiglio scende al piu' accurato che ci sta
    assert rep["recommendation"]["model"] != SO400M
    assert "ore" in rep["recommendation"]["reason"]


def test_saved_bench_roundtrip_and_staleness():
    hw = _hw()
    with tempfile.TemporaryDirectory() as tmp:
        assert hw_mod.load_bench(tmp, hw) is None
        hw_mod.bench_path(tmp).write_text(json.dumps({"fingerprint": hw_mod.fingerprint(hw), "results": {}}), encoding="utf-8")
        assert hw_mod.load_bench(tmp, hw)["stale"] is False
        assert hw_mod.load_bench(tmp, _hw(vram=16))["stale"] is True      # altra scheda video -> misure non valide


def test_detect_describes_this_machine_without_loading_torch():
    before = "torch" in sys.modules
    d = hw_mod.detect(True)
    assert d["device"] in ("cuda", "mps", "cpu")
    assert d["cpu"]["threads"] and d["cpu"]["name"]
    assert ("torch" in sys.modules) == before        # nessun import di torch: non si apre CUDA mentre si analizza


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, f"{type(e).__name__}: {e}")
    raise SystemExit(1 if fails else 0)
