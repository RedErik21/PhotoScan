"""Test del launcher PhotoScan-Setup: scelta runtime/modello, parti della Release, ricomposizione, config."""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "launcher"))

import setup_photoscan as sp  # noqa: E402


def _hw(gpu=None, vram=0, ram=16.0):
    return sp.Hardware(cores=8, ram_gb=ram, gpu_name=gpu, vram_mb=vram, free_disk_gb=100.0)


def test_recommend_tiers():
    assert sp.recommend(_hw("RTX", 8000, 32)) == ("gpu", "so400m")
    assert sp.recommend(_hw("RTX", 6000, 16)) == ("gpu", "so400m")
    assert sp.recommend(_hw("GTX", 4000, 8)) == ("gpu", "large")
    assert sp.recommend(_hw("GTX", 2000, 16)) == ("cpu", "base")  # VRAM piccola: meglio CPU + base
    assert sp.recommend(_hw(None, 0, 32)) == ("cpu", "base")      # nessuna NVIDIA
    assert sp.recommend(_hw("RTX", 8000, 8)) == ("gpu", "large")  # RAM bassa: niente SO400M


def test_space_warning():
    hw = sp.Hardware(4, 16, None, 0, free_disk_gb=2.0)
    assert sp.space_warning(hw, "cpu", "base") is not None
    assert sp.space_warning(_hw(), "cpu", "base") is None


def test_package_parts_selects_runtime_and_order():
    assets = [
        {"name": "PhotoScan-0.1.4-windows.zip.002", "browser_download_url": "u2", "size": 2},
        {"name": "PhotoScan-0.1.4-windows.zip.001", "browser_download_url": "u1", "size": 1},
        {"name": "PhotoScan-0.1.4-windows-cpu.zip", "browser_download_url": "c", "size": 9},
        {"name": "PhotoScan-0.1.3-windows.zip", "browser_download_url": "old", "size": 9},
        {"name": "PhotoScan-0.1.4-windows.zip.checksums", "browser_download_url": "x", "size": 1},
    ]
    gpu = sp.package_parts(assets, "0.1.4", "gpu")
    assert [p["name"] for p in gpu] == ["PhotoScan-0.1.4-windows.zip.001", "PhotoScan-0.1.4-windows.zip.002"]
    cpu = sp.package_parts(assets, "0.1.4", "cpu")
    assert [p["name"] for p in cpu] == ["PhotoScan-0.1.4-windows-cpu.zip"]


def test_download_reassembles_parts_and_extracts():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"
        src.mkdir()
        # zip di prova, poi spezzato in due parti come farà la build
        zsrc = tmp / "full.zip"
        with zipfile.ZipFile(zsrc, "w") as z:
            z.writestr("PhotoScan/PhotoScan.exe", b"MZ" + b"x" * 5000)
            z.writestr("PhotoScan/_internal/lib.dll", b"y" * 3000)
        data = zsrc.read_bytes()
        half = len(data) // 2
        parts = []
        for i, chunk in enumerate([data[:half], data[half:]], 1):
            p = src / f"PhotoScan-9.9.9-windows.zip.{i:03d}"
            p.write_bytes(chunk)
            parts.append({"name": p.name, "url": p.as_uri(), "size": len(chunk)})
        got = []
        zip_path = sp.download_parts(parts, tmp / "dl", lambda f, t: got.append(f))
        assert zip_path.read_bytes() == data
        assert not any((tmp / "dl" / p["name"]).exists() for p in parts)  # parti rimosse
        app = tmp / "app"
        sp.extract(zip_path, app)
        assert (app / "PhotoScan" / "PhotoScan.exe").exists()
        # reinstallazione: sostituisce la versione precedente
        sp.extract(zip_path, app)
        assert (app / "PhotoScan" / "_internal" / "lib.dll").exists()


def test_download_detects_truncated_part():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        p = tmp / "PhotoScan-9.9.9-windows.zip"
        p.write_bytes(b"abc")
        parts = [{"name": p.name, "url": p.as_uri(), "size": 10}]
        try:
            sp.download_parts(parts, tmp / "dl")
        except RuntimeError as e:
            assert "incompleta" in str(e)
        else:
            raise AssertionError("parte incompleta non rilevata")


def test_write_config_keeps_other_keys_and_sets_model():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"keep": 1, "model_name": "old"}), encoding="utf-8")
        sp.write_config(cfg, "google/siglip2-base-patch16-224")
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data == {"keep": 1, "model_name": "google/siglip2-base-patch16-224"}


def test_install_marker_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert sp.installed_state(root) is None
        sp.write_install_marker(root, "0.1.4", "gpu", "so400m")
        assert sp.installed_state(root) == {"version": "0.1.4", "runtime": "gpu", "model": "so400m"}
