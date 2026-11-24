"""Riconoscimento volti locale (ONNXRuntime + SCRFD det_10g + ArcFace w600k_r50) e clustering stabile.

Nessuna dipendenza da insightface o compilatori C++: usa direttamente onnxruntime (GPU CUDA o CPU),
NumPy e Pillow. I pesi buffalo_l vengono scaricati una sola volta in cache/models/faces/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import urllib.request
import zipfile
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001
    pass

from . import netguard
from .config import Config, load_config
from .db import from_blob, get_meta, set_meta, to_blob

log = logging.getLogger("fotoscan")

FACES_MODEL_ID = "buffalo_l:det_10g+w600k_r50"
BUFFALO_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
DET_FILE = "det_10g.onnx"
REC_FILE = "w600k_r50.onnx"
MODEL_SHA256: dict[str, str] = {
    DET_FILE: "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
    REC_FILE: "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
}
ARC_BATCH_TARGET = 48
ARC_BATCH_MAX_PHOTOS = 32

# Soglie di qualita' del volto (volti sotto soglia restano salvati ma non definiscono cluster/centroidi)
MIN_DET_SCORE = 0.55
MIN_BOX_PX = 40.0
MIN_QUALITY = 0.25

# Landmark canonici ArcFace 112x112 (occhi, naso, angoli bocca)
_ARCFACE_DST = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


@dataclass
class DetectedFace:
    bbox: list[float]          # [x1, y1, x2, y2] in pixel dell'immagine sorgente
    emb: np.ndarray            # float32 normalizzato L2 (512,)
    det_score: float
    quality: float
    landmarks: list[list[float]] | None = None


@dataclass
class FaceCandidate:
    """Volto rilevato da SCRFD e allineato 112x112 RGB, pronto per il batching ArcFace."""
    bbox: list[float]
    crop: np.ndarray           # uint8 (112, 112, 3) RGB
    det_score: float
    quality: float
    landmarks: list[list[float]] | None = None


# ------------------------------------------------------------ disponibilita' e pesi
def models_dir(cfg: Config | None = None) -> Path:
    c = cfg or load_config()
    return Path(c.cache_dir) / "models" / "faces"


def models_present(cfg: Config | None = None) -> bool:
    d = models_dir(cfg)
    det, rec = d / DET_FILE, d / REC_FILE
    return det.is_file() and det.stat().st_size > 1_000_000 and rec.is_file() and rec.stat().st_size > 10_000_000


def active_provider() -> str | None:
    try:
        import onnxruntime as ort

        provs = ort.get_available_providers()
        if "CUDAExecutionProvider" in provs:
            return "CUDAExecutionProvider"
        if "CPUExecutionProvider" in provs:
            return "CPUExecutionProvider"
        return provs[0] if provs else None
    except Exception:
        return None


def availability(cfg: Config | None = None) -> tuple[bool, str]:
    """(ok, motivo_italiano). ok=True se onnxruntime e' installato (i pesi si scaricano al bisogno)."""
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False, "Pacchetto onnxruntime-gpu (o onnxruntime) non installato."
    prov = active_provider()
    if not prov:
        return False, "Nessun provider onnxruntime disponibile."
    if not models_present(cfg):
        return True, "Modelli volti da scaricare al primo avvio (~170 MB)."
    if prov == "CUDAExecutionProvider":
        return True, "Pronto su GPU NVIDIA (CUDA)."
    return True, "Pronto su CPU (piu' lento senza GPU)."


def status_info(cfg: Config | None = None) -> dict:
    ok, reason = availability(cfg)
    prov = active_provider()
    return {
        "available": ok,
        "reason": reason,
        "models_ready": models_present(cfg),
        "provider": "cuda" if prov == "CUDAExecutionProvider" else ("cpu" if prov else None),
        "model_id": FACES_MODEL_ID,
    }


def available(cfg: Config | None = None) -> bool:
    return availability(cfg)[0]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def verify_model_file(path: Path, expected_sha256: str | None = None) -> bool:
    """Verifica che il file ONNX esista e corrisponda all'impronta SHA-256 attesa."""
    if not path.is_file():
        return False
    expected = expected_sha256 or MODEL_SHA256.get(path.name)
    if not expected:
        return True
    try:
        return _sha256_file(path).lower() == expected.lower()
    except OSError:
        return False


def ensure_models(cfg: Config | None = None, progress: Callable[[str], None] | None = None) -> tuple[Path, Path]:
    """Garantisce che det_10g.onnx e w600k_r50.onnx siano in cache/models/faces/ e integri (SHA-256)."""
    d = models_dir(cfg)
    det_path, rec_path = d / DET_FILE, d / REC_FILE
    say = progress or (lambda m: log.info("volti: %s", m))
    if models_present(cfg):
        if verify_model_file(det_path) and verify_model_file(rec_path):
            return det_path, rec_path
        log.warning("volti: verifica SHA-256 fallita sui modelli in cache, riscarico")
        say("Modelli volti incompleti o danneggiati: li riscarico…")
        for p in (det_path, rec_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    d.mkdir(parents=True, exist_ok=True)
    say("Scarico i modelli volti buffalo_l (~280 MB, una sola volta)…")
    tmp_zip = Path(tempfile.mkstemp(prefix="fs_buffalo_", suffix=".zip", dir=str(d))[1])
    try:
        req = urllib.request.Request(BUFFALO_URL, headers={"User-Agent": "PhotoScan/2.0"})
        with netguard.download_window("download modelli volti buffalo_l (GitHub)"):
            with urllib.request.urlopen(req, timeout=180) as resp, open(tmp_zip, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        say("Estraggo e verifico i modelli ONNX…")
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            names = zf.namelist()
            for target_name, dest in ((DET_FILE, det_path), (REC_FILE, rec_path)):
                match = next((n for n in names if n.endswith("/" + target_name) or n == target_name), None)
                if not match:
                    raise RuntimeError(f"File {target_name} mancante nell'archivio buffalo_l.zip")
                tmp_out = dest.with_suffix(".onnx.tmp")
                with zf.open(match) as src, open(tmp_out, "wb") as dst:
                    while True:
                        buf = src.read(1024 * 1024)
                        if not buf:
                            break
                        dst.write(buf)
                if not verify_model_file(tmp_out, MODEL_SHA256.get(target_name)):
                    tmp_out.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"Il file {target_name} scaricato e' corrotto (SHA-256 non valido). Controlla la connessione e riprova."
                    )
                os.replace(tmp_out, dest)
        say("Modelli volti pronti.")
        return det_path, rec_path
    finally:
        try:
            tmp_zip.unlink(missing_ok=True)
        except OSError:
            pass


# ------------------------------------------------------------ geometria e qualita'
def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float = 0.4) -> list[int]:
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = np.argsort(-scores)
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-6)
        order = rest[iou <= iou_thr]
    return keep


def _similarity_affine_inv(src: np.ndarray, dst: np.ndarray) -> tuple[float, float, float, float, float, float]:
    """Trasformazione di similitudine 2D inversa (dst -> src) per Pillow Image.transform(AFFINE).
    Risolve min ||s R dst + t - src||^2 (Umeyama 2D senza riflessioni)."""
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    mu_s = src.mean(axis=0)
    mu_d = dst.mean(axis=0)
    ds = src - mu_s
    dd = dst - mu_d
    var_d = float((dd * dd).sum()) + 1e-12
    a = float((dd[:, 0] * ds[:, 0] + dd[:, 1] * ds[:, 1]).sum()) / var_d
    b = float((dd[:, 0] * ds[:, 1] - dd[:, 1] * ds[:, 0]).sum()) / var_d
    # x_src = a * x_dst - b * y_dst + tx ; y_src = b * x_dst + a * y_dst + ty
    tx = float(mu_s[0] - (a * mu_d[0] - b * mu_d[1]))
    ty = float(mu_s[1] - (b * mu_d[0] + a * mu_d[1]))
    return (a, -b, tx, b, a, ty)


def _laplacian_sharpness(crop_rgb: np.ndarray) -> float:
    """Indice di nitidezza (varianza del Laplaciano 3x3 su scala di grigi)."""
    g = (0.299 * crop_rgb[:, :, 0] + 0.587 * crop_rgb[:, :, 1] + 0.114 * crop_rgb[:, :, 2]).astype(np.float32)
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1] + g[2:, 1:-1]
        + g[1:-1, :-2] + g[1:-1, 2:]
    )
    return float(lap.var())


def _face_quality(det_score: float, bbox: list[float], sharpness: float) -> float:
    """Punteggio composito 0..1 basato su confidenza detector, dimensione volto e nitidezza."""
    w = max(0.0, bbox[2] - bbox[0])
    h = max(0.0, bbox[3] - bbox[1])
    side = min(w, h)
    q_det = np.clip((det_score - 0.40) / 0.45, 0.0, 1.0)
    q_size = np.clip((side - 28.0) / 100.0, 0.0, 1.0)
    q_sharp = np.clip(sharpness / 120.0, 0.0, 1.0)
    return float(round(0.45 * q_det + 0.35 * q_size + 0.20 * q_sharp, 4))


def is_reliable_face(det_score: float | None, bbox: list[float] | None, quality: float | None) -> bool:
    if det_score is not None and det_score < MIN_DET_SCORE:
        return False
    if quality is not None and quality < MIN_QUALITY:
        return False
    if bbox and len(bbox) == 4:
        if min(bbox[2] - bbox[0], bbox[3] - bbox[1]) < MIN_BOX_PX:
            return False
    return True


# ------------------------------------------------------------ motore ONNX (SCRFD + ArcFace)
class OnnxFaceEngine:
    """Rilevatore SCRFD (det_10g) + riconoscitore ArcFace (w600k_r50) su ONNXRuntime puro."""

    def __init__(self, cfg: Config | None = None, det_size: int | None = None):
        import onnxruntime as ort

        netguard.quiet_onnxruntime(ort)
        self.cfg = cfg or load_config()
        self.det_size = int(det_size if det_size is not None else getattr(self.cfg, "faces_det_size", 640))
        det_path, rec_path = ensure_models(self.cfg)
        provs = ["CUDAExecutionProvider", "CPUExecutionProvider"] if active_provider() == "CUDAExecutionProvider" else ["CPUExecutionProvider"]
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.det = ort.InferenceSession(str(det_path), sess_options=opts, providers=provs)
        self.rec = ort.InferenceSession(str(rec_path), sess_options=opts, providers=provs)
        self.provider = self.det.get_providers()[0]
        log.info("motore volti ONNX pronto su %s (det_size=%d)", self.provider, self.det_size)
        self._det_in = self.det.get_inputs()[0].name
        self._rec_in = self.rec.get_inputs()[0].name
        self._anchor_cache: dict[tuple[int, int, int], np.ndarray] = {}

    def _anchors(self, h: int, w: int, stride: int, num_anchors: int = 2) -> np.ndarray:
        key = (h, w, stride)
        a = self._anchor_cache.get(key)
        if a is None:
            ys, xs = np.mgrid[:h, :w]
            centers = np.stack([xs, ys], axis=-1).astype(np.float32) * stride
            centers = centers.reshape(-1, 2)
            if num_anchors > 1:
                centers = np.repeat(centers, num_anchors, axis=0)
            self._anchor_cache[key] = centers
            a = centers
        return a

    def detect_boxes(self, im: Image.Image) -> list[FaceCandidate]:
        """Rileva i volti nell'immagine RGB con SCRFD e prepara i ritagli 112x112 allineati per ArcFace."""
        orig_w, orig_h = im.size
        if orig_w < 20 or orig_h < 20:
            return []
        scale = min(self.det_size / orig_w, self.det_size / orig_h)
        new_w, new_h = max(1, int(round(orig_w * scale))), max(1, int(round(orig_h * scale)))
        resized = im.resize((new_w, new_h), Image.BILINEAR)
        canvas = np.zeros((self.det_size, self.det_size, 3), dtype=np.float32)
        canvas[:new_h, :new_w, :] = np.asarray(resized, dtype=np.float32)
        # SCRFD buffalo_l accetta RGB/BGR normalizzato (x - 127.5) / 128.0 in NCHW (BGR come OpenCV)
        blob = ((canvas[:, :, ::-1] - 127.5) / 128.0).transpose(2, 0, 1)[None, ...]
        outs = self.det.run(None, {self._det_in: blob})

        fmc = 3
        strides = (8, 16, 32)
        all_scores, all_boxes, all_kps = [], [], []
        for idx, stride in enumerate(strides):
            scores = outs[idx].reshape(-1)
            bbox_preds = outs[idx + fmc].reshape(-1, 4) * stride
            kps_preds = outs[idx + fmc * 2].reshape(-1, 10) * stride
            keep = scores >= 0.45
            if not np.any(keep):
                continue
            fh, fw = self.det_size // stride, self.det_size // stride
            anchors = self._anchors(fh, fw, stride, num_anchors=2)[keep]
            sc = scores[keep]
            bp = bbox_preds[keep]
            kp = kps_preds[keep]
            boxes = np.stack([
                anchors[:, 0] - bp[:, 0],
                anchors[:, 1] - bp[:, 1],
                anchors[:, 0] + bp[:, 2],
                anchors[:, 1] + bp[:, 3],
            ], axis=-1) / scale
            kps = kp.reshape(-1, 5, 2)
            kps[:, :, 0] = (anchors[:, 0:1] + kps[:, :, 0]) / scale
            kps[:, :, 1] = (anchors[:, 1:2] + kps[:, :, 1]) / scale
            all_scores.append(sc)
            all_boxes.append(boxes)
            all_kps.append(kps)

        if not all_scores:
            return []
        scores_cat = np.concatenate(all_scores)
        boxes_cat = np.concatenate(all_boxes)
        kps_cat = np.concatenate(all_kps)
        keep_idx = _nms(boxes_cat, scores_cat, iou_thr=0.4)
        if not keep_idx:
            return []

        candidates: list[FaceCandidate] = []
        for i in keep_idx:
            box = [
                float(np.clip(boxes_cat[i, 0], 0, orig_w)),
                float(np.clip(boxes_cat[i, 1], 0, orig_h)),
                float(np.clip(boxes_cat[i, 2], 0, orig_w)),
                float(np.clip(boxes_cat[i, 3], 0, orig_h)),
            ]
            if box[2] - box[0] < 12 or box[3] - box[1] < 12:
                continue
            lm = kps_cat[i]
            mat = _similarity_affine_inv(lm, _ARCFACE_DST)
            aligned = im.transform((112, 112), Image.AFFINE, mat, resample=Image.BILINEAR)
            arr = np.asarray(aligned, dtype=np.uint8)
            sharp = _laplacian_sharpness(arr)
            sc = float(scores_cat[i])
            qual = _face_quality(sc, box, sharp)
            candidates.append(
                FaceCandidate(
                    bbox=box,
                    crop=arr,
                    det_score=sc,
                    quality=qual,
                    landmarks=[[float(x), float(y)] for x, y in lm],
                )
            )
        return candidates

    def embed_crops(self, crops: list[np.ndarray], batch_size: int = 64) -> np.ndarray:
        """Calcola gli embedding ArcFace 512-dim normalizzati L2 in lotti da 32-64 volti."""
        if not crops:
            return np.empty((0, 512), dtype=np.float32)
        out_chunks: list[np.ndarray] = []
        bs = max(1, int(batch_size))
        for start in range(0, len(crops), bs):
            chunk = crops[start:start + bs]
            # ArcFace batch inference (RGB normalizzato (x - 127.5) / 127.5 in NCHW)
            batch = ((np.stack(chunk).astype(np.float32) - 127.5) / 127.5).transpose(0, 3, 1, 2)
            embs = self.rec.run(None, {self._rec_in: batch})[0].astype(np.float32)
            norms = np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9
            out_chunks.append(embs / norms)
        return np.concatenate(out_chunks, axis=0)

    def detect(self, im: Image.Image) -> list[DetectedFace]:
        """Rileva tutti i volti nell'immagine RGB e calcola embedding ArcFace 512-dim + qualita'."""
        cands = self.detect_boxes(im)
        if not cands:
            return []
        embs = self.embed_crops([c.crop for c in cands])
        return [
            DetectedFace(bbox=c.bbox, emb=embs[idx], det_score=c.det_score, quality=c.quality, landmarks=c.landmarks)
            for idx, c in enumerate(cands)
        ]


# ------------------------------------------------------------ pipeline di rilevamento
def _load_image_rgb(path: str, max_side: int = 1600) -> Image.Image:
    with Image.open(path) as im:
        if im.format == "JPEG":
            im.draft("RGB", (max_side, max_side))
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_side, max_side), Image.BILINEAR)
        return im.copy()


def _box_iou(a: list[float], b: list[float]) -> float:
    xx1, yy1 = max(a[0], b[0]), max(a[1], b[1])
    xx2, yy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, xx2 - xx1) * max(0.0, yy2 - yy1)
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return inter / (area_a + area_b - inter + 1e-6)


def _insert_photo_faces(
    conn: sqlite3.Connection,
    fid: int,
    found: list[DetectedFace],
    existing_preserved: dict[int, list[list[float]]],
) -> int:
    """Inserisce i volti di una foto evitando duplicati su volti locked/excluded gia' presenti (post reset-scan)."""
    preserved = existing_preserved.get(fid)
    added = 0
    for f in found:
        if preserved and any(_box_iou(f.bbox, pb) > 0.5 for pb in preserved):
            continue
        conn.execute(
            "INSERT INTO faces(file_id, bbox, emb, cluster, person_id, locked, excluded, det_score, quality, landmarks) "
            "VALUES(?, ?, ?, NULL, NULL, 0, 0, ?, ?, ?)",
            (
                fid,
                json.dumps([round(float(x), 1) for x in f.bbox]),
                to_blob(np.asarray(f.emb, dtype=np.float32)),
                float(f.det_score),
                float(f.quality),
                json.dumps(f.landmarks) if f.landmarks else None,
            ),
        )
        added += 1
    conn.execute("INSERT OR IGNORE INTO faces_done(file_id) VALUES(?)", (fid,))
    return added


def detect_faces(
    conn: sqlite3.Connection,
    cfg: Config | None = None,
    detector=None,
    progress: Callable[[str, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Rileva volti nelle foto indicizzate non ancora analizzate; prefetch scorrevole I/O + batching ArcFace."""
    c = cfg or load_config()
    prev_model = get_meta(conn, "faces_model")
    if prev_model and prev_model != FACES_MODEL_ID:
        log.info("modello volti cambiato (%s -> %s): azzero i volti precedenti", prev_model, FACES_MODEL_ID)
        with conn:
            conn.execute("DELETE FROM faces")
            conn.execute("DELETE FROM faces_done")
            conn.execute("DELETE FROM persons")
    set_meta(conn, "faces_model", FACES_MODEL_ID)

    rows = conn.execute(
        "SELECT id, path FROM files WHERE kind='image' AND status='embedded' "
        "AND id NOT IN (SELECT file_id FROM faces_done) ORDER BY id"
    ).fetchall()
    total = len(rows)
    if total == 0:
        if progress:
            progress("volti", 0, 0)
        return 0
    if progress:
        progress("volti", 0, total)

    # Eventuali volti bloccati/esclusi conservati da un reset-scan: evitiamo di reinserire lo stesso riquadro.
    existing_preserved: dict[int, list[list[float]]] = {}
    for pr in conn.execute("SELECT file_id, bbox FROM faces WHERE COALESCE(locked,0)=1 OR COALESCE(excluded,0)=1"):
        if pr["bbox"]:
            try:
                box = json.loads(pr["bbox"])
                if len(box) == 4:
                    existing_preserved.setdefault(int(pr["file_id"]), []).append([float(x) for x in box])
            except Exception:  # noqa: BLE001
                pass

    engine = detector or OnnxFaceEngine(c, det_size=int(getattr(c, "faces_det_size", 640)))
    supports_batch = callable(getattr(engine, "detect_boxes", None)) and callable(getattr(engine, "embed_crops", None))
    workers = max(1, min(int(c.workers or 4), 8))
    window = max(16, workers * 4)
    inserted = 0
    processed = 0

    def _open(r):
        fid, path = int(r["id"]), str(r["path"])
        try:
            return fid, path, _load_image_rgb(path), None
        except Exception as exc:  # noqa: BLE001
            return fid, path, None, exc

    pending_photos: list[tuple[int, str, list[FaceCandidate]]] = []
    pending_crops: list[np.ndarray] = []

    def _flush_pending() -> None:
        nonlocal inserted, processed
        if not pending_photos:
            return
        embs = None
        if pending_crops:
            try:
                embs = engine.embed_crops(pending_crops, batch_size=64)
            except Exception as exc:  # noqa: BLE001
                log.warning("volti batch ArcFace: %s", exc)
                embs = None
        cursor = 0
        with conn:
            for fid, _path, cands in pending_photos:
                found: list[DetectedFace] = []
                if cands and embs is not None:
                    for cand in cands:
                        found.append(
                            DetectedFace(
                                bbox=cand.bbox,
                                emb=embs[cursor],
                                det_score=cand.det_score,
                                quality=cand.quality,
                                landmarks=cand.landmarks,
                            )
                        )
                        cursor += 1
                else:
                    cursor += len(cands)
                inserted += _insert_photo_faces(conn, fid, found, existing_preserved)
                processed += 1
        pending_photos.clear()
        pending_crops.clear()
        if progress:
            progress("volti", processed, total)

    row_iter = iter(rows)
    in_flight: deque[Future] = deque()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        def _refill() -> None:
            while len(in_flight) < window:
                if should_stop and should_stop():
                    return
                nxt = next(row_iter, None)
                if nxt is None:
                    return
                in_flight.append(pool.submit(_open, nxt))

        _refill()
        while in_flight:
            if should_stop and should_stop():
                break
            fut = in_flight.popleft()
            _refill()
            fid, path, im, err = fut.result()
            if should_stop and should_stop():
                break

            if supports_batch:
                if err is not None or im is None:
                    log.warning("volti %s: %s", path, err)
                    pending_photos.append((fid, path, []))
                else:
                    try:
                        cands = engine.detect_boxes(im)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("volti inferenza %s: %s", path, exc)
                        cands = []
                    pending_photos.append((fid, path, cands))
                    for cand in cands:
                        pending_crops.append(cand.crop)
                if len(pending_crops) >= ARC_BATCH_TARGET or len(pending_photos) >= ARC_BATCH_MAX_PHOTOS:
                    _flush_pending()
            else:
                found: list[DetectedFace] = []
                if err is not None or im is None:
                    log.warning("volti %s: %s", path, err)
                else:
                    try:
                        found = engine.detect(im)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("volti inferenza %s: %s", path, exc)
                with conn:
                    inserted += _insert_photo_faces(conn, fid, found, existing_preserved)
                    processed += 1
                if progress and (processed % 8 == 0 or processed == total):
                    progress("volti", processed, total)

        # Chiude eventuali foto accumulate nel batch e annulla i future residui se interrotto
        _flush_pending()
        for fut in in_flight:
            fut.cancel()

    if progress:
        progress("volti", processed, total)
    return inserted


# ------------------------------------------------------------ clustering stabile e persone persistenti
def _norm_vec(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return (v / n).astype(np.float32) if n > 0 else v


def recompute_person(conn: sqlite3.Connection, person_id: int) -> None:
    """Ricalcola centroide, conteggio foto distinte (n) e miglior cover_face_id di una persona."""
    rows = conn.execute(
        "SELECT id, file_id, emb, det_score, quality, bbox, locked FROM faces "
        "WHERE person_id=? AND COALESCE(excluded,0)=0",
        (person_id,),
    ).fetchall()
    if not rows:
        # Non elimina persone con nome; azzera solo conteggio e centroide
        conn.execute("UPDATE persons SET n=0, centroid=NULL, cover_face_id=NULL, cover_locked=0 WHERE id=?", (person_id,))
        return
    p_info = conn.execute("SELECT cover_face_id, COALESCE(cover_locked,0) AS cover_locked FROM persons WHERE id=?", (person_id,)).fetchone()
    distinct_files = len({int(r["file_id"]) for r in rows})
    row_ids = {int(r["id"]) for r in rows}
    vecs = []
    best_cover = rows[0]["id"]
    best_q = -1.0
    for r in rows:
        q = float(r["quality"]) if r["quality"] is not None else 0.5
        if q > best_q:
            best_q = q
            best_cover = r["id"]
        if r["emb"] is not None:
            bbox = json.loads(r["bbox"]) if r["bbox"] else None
            if r["locked"] or is_reliable_face(r["det_score"], bbox, r["quality"]):
                vecs.append(_norm_vec(from_blob(r["emb"])))
    if not vecs:
        vecs = [_norm_vec(from_blob(r["emb"])) for r in rows if r["emb"] is not None]
    centroid = to_blob(_norm_vec(np.mean(np.stack(vecs), axis=0))) if vecs else None
    # Preserva la foto profilo scelta manualmente finche' quel volto resta assegnato alla persona
    keep_locked = bool(p_info and p_info["cover_locked"] and p_info["cover_face_id"] in row_ids)
    final_cover = int(p_info["cover_face_id"]) if keep_locked else int(best_cover)
    conn.execute(
        "UPDATE persons SET n=?, centroid=?, cover_face_id=?, cover_locked=? WHERE id=?",
        (distinct_files, centroid, final_cover, 1 if keep_locked else 0, person_id),
    )


def _blockwise_components(mat: np.ndarray, threshold: float, block: int = 2048) -> list[list[int]]:
    """Componenti connesse sul grafo sim(i,j) >= threshold, calcolate a blocchi (mai matrice N x N).
    100% indipendente dall'ordine delle righe."""
    n = len(mat)
    if n == 0:
        return []
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for i in range(0, n, block):
        bi = mat[i:i + block]
        for j in range(i, n, block):
            bj = mat[j:j + block]
            sims = bi @ bj.T
            if i == j:
                r_idx, c_idx = np.where(np.triu(sims, k=1) >= threshold)
            else:
                r_idx, c_idx = np.where(sims >= threshold)
            for r, c in zip(r_idx.tolist(), c_idx.tolist()):
                union(i + r, j + c)

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)
    return list(groups.values())


def cluster_faces(
    conn: sqlite3.Connection,
    cfg: Config | None = None,
    threshold: float | None = None,
    assign_threshold: float | None = None,
    min_photos: int | None = None,
    reset_unlocked: bool = False,
) -> int:
    """Clustering stabile e idempotente dei volti in `persons`.

    - Non tocca mai volti con locked=1 o excluded=1.
    - Non rinumera né elimina persone con nome.
    - Assegna prima i volti nuovi alle persone esistenti (soglia conservativa), poi raggruppa
      i volti rimasti in modo indipendente dall'ordine; crea una persona solo se ha >= min_photos foto distinte.
    """
    c = cfg or load_config()
    thr = float(threshold if threshold is not None else c.faces_threshold)
    assign_thr = float(assign_threshold if assign_threshold is not None else getattr(c, "faces_assign_threshold", 0.52))
    min_n = max(1, int(min_photos if min_photos is not None else c.faces_min_photos))

    with conn:
        if reset_unlocked:
            # Mantiene intatti i volti locked/excluded e tutte le persone con nome o con volti locked.
            conn.execute("UPDATE faces SET person_id=NULL, cluster=NULL WHERE COALESCE(locked,0)=0 AND COALESCE(excluded,0)=0")
            conn.execute(
                "DELETE FROM persons WHERE (name IS NULL OR TRIM(name)='') "
                "AND id NOT IN (SELECT DISTINCT person_id FROM faces WHERE person_id IS NOT NULL)"
            )

        # 1. Aggiorna i centroidi delle persone esistenti
        existing_ids = [int(r[0]) for r in conn.execute("SELECT id FROM persons ORDER BY id")]
        for pid in existing_ids:
            recompute_person(conn, pid)

        existing = conn.execute(
            "SELECT id, centroid FROM persons WHERE centroid IS NOT NULL ORDER BY id"
        ).fetchall()
        ex_ids = [int(r["id"]) for r in existing]
        ex_mat = np.stack([_norm_vec(from_blob(r["centroid"])) for r in existing]) if existing else None

        # 2. Carica i volti non assegnati, affidabili e non esclusi/bloccati
        raw_unassigned = conn.execute(
            "SELECT id, file_id, emb, det_score, quality, bbox FROM faces "
            "WHERE person_id IS NULL AND COALESCE(excluded,0)=0 AND COALESCE(locked,0)=0 AND emb IS NOT NULL "
            "ORDER BY id"
        ).fetchall()

        reliable: list[tuple[int, int, np.ndarray]] = []
        for r in raw_unassigned:
            bbox = json.loads(r["bbox"]) if r["bbox"] else None
            if is_reliable_face(r["det_score"], bbox, r["quality"]):
                reliable.append((int(r["id"]), int(r["file_id"]), _norm_vec(from_blob(r["emb"]))))

        # 3. Assegna prima alle persone esistenti (soglia conservativa)
        remaining: list[tuple[int, int, np.ndarray]] = []
        touched_persons: set[int] = set()
        if reliable and ex_mat is not None:
            rmat = np.stack([v for _, _, v in reliable])
            sims = rmat @ ex_mat.T
            best_j = np.argmax(sims, axis=1)
            best_s = sims[np.arange(len(reliable)), best_j]
            for idx, (fid_row, file_id, vec) in enumerate(reliable):
                if float(best_s[idx]) >= assign_thr:
                    pid = ex_ids[int(best_j[idx])]
                    conn.execute("UPDATE faces SET person_id=?, cluster=? WHERE id=?", (pid, pid, fid_row))
                    touched_persons.add(pid)
                else:
                    remaining.append((fid_row, file_id, vec))
        else:
            remaining = reliable

        # 4. Raggruppa i volti rimasti in modo indipendente dall'ordine
        if remaining:
            mat = np.stack([v for _, _, v in remaining])
            comps = _blockwise_components(mat, threshold=thr)
            # Filtra membri ponte deboli nei componenti grandi e ordina deterministicamente
            valid_clusters: list[tuple[int, int, list[int]]] = []
            for comp in comps:
                if len(comp) > 10:
                    centroid = _norm_vec(mat[comp].mean(axis=0))
                    comp = [i for i in comp if float(mat[i] @ centroid) >= thr - 0.05]
                distinct_files = {remaining[i][1] for i in comp}
                if len(distinct_files) >= min_n:
                    min_file = min(distinct_files)
                    valid_clusters.append((-len(distinct_files), min_file, comp))
            valid_clusters.sort()

            now = datetime.now().isoformat(timespec="seconds")
            for _, _, comp in valid_clusters:
                centroid = _norm_vec(mat[comp].mean(axis=0))
                cur = conn.execute(
                    "INSERT INTO persons(name, created, centroid, n, hidden) VALUES(NULL, ?, ?, 0, 0)",
                    (now, to_blob(centroid)),
                )
                pid = int(cur.lastrowid)
                face_ids = [remaining[i][0] for i in comp]
                conn.executemany("UPDATE faces SET person_id=?, cluster=? WHERE id=?", [(pid, pid, x) for x in face_ids])
                touched_persons.add(pid)

        # 5. Ricalcola centroidi, conteggi e copertine di tutte le persone toccate
        for pid in touched_persons:
            recompute_person(conn, pid)

        # Pulizia persone senza nome rimaste vuote
        conn.execute("DELETE FROM persons WHERE (name IS NULL OR TRIM(name)='') AND COALESCE(n,0)=0")

    return int(conn.execute("SELECT COUNT(*) FROM persons WHERE COALESCE(hidden,0)=0 AND COALESCE(n,0)>0").fetchone()[0])


# ------------------------------------------------------------ operazioni utente (persistenti)
def merge_persons(conn: sqlite3.Connection, ids: list[int], into_id: int) -> dict:
    """Unisce `ids` dentro `into_id`, spostando i volti (locked=1), ricalcolando il centroide ed eliminando i gruppi assorbiti."""
    others = [int(x) for x in dict.fromkeys(ids) if int(x) != int(into_id)]
    if not others:
        return {"merged": 0, "into": int(into_id)}
    with conn:
        marks = ",".join("?" * len(others))
        conn.execute(
            f"UPDATE faces SET person_id=?, cluster=?, locked=1 WHERE person_id IN ({marks})",
            [int(into_id), int(into_id), *others],
        )
        conn.execute(f"DELETE FROM persons WHERE id IN ({marks})", others)
        recompute_person(conn, int(into_id))
    return {"merged": len(others), "into": int(into_id)}


def assign_faces(conn: sqlite3.Connection, face_ids: list[int], target: int | str | None) -> dict:
    """Sposta `face_ids` su `target` (person_id intero, None=rimuovi persona, 'new'=crea nuova persona). Imposta locked=1."""
    fids = list(dict.fromkeys(int(x) for x in face_ids))
    if not fids:
        return {"person_id": None, "updated": 0}
    with conn:
        marks = ",".join("?" * len(fids))
        prev_pids = {
            int(r[0]) for r in conn.execute(f"SELECT DISTINCT person_id FROM faces WHERE id IN ({marks}) AND person_id IS NOT NULL", fids)
        }
        if target == "new":
            now = datetime.now().isoformat(timespec="seconds")
            cur = conn.execute("INSERT INTO persons(name, created, n, hidden) VALUES(NULL, ?, 0, 0)", (now,))
            dest_pid: int | None = int(cur.lastrowid)
        elif target is None:
            dest_pid = None
        else:
            dest_pid = int(target)
        conn.execute(
            f"UPDATE faces SET person_id=?, cluster=?, locked=1, excluded=0 WHERE id IN ({marks})",
            [dest_pid, dest_pid, *fids],
        )
        for pid in prev_pids | ({dest_pid} if dest_pid is not None else set()):
            recompute_person(conn, pid)
    return {"person_id": dest_pid, "updated": len(fids)}


def set_face_excluded(conn: sqlite3.Connection, face_id: int, excluded: bool) -> dict:
    """Esclude o ripristina un volto (locked=1, mai riassegnato dal clustering automatico)."""
    row = conn.execute("SELECT person_id FROM faces WHERE id=?", (int(face_id),)).fetchone()
    if row is None:
        raise KeyError(face_id)
    prev_pid = row["person_id"]
    with conn:
        if excluded:
            conn.execute("UPDATE faces SET excluded=1, locked=1, person_id=NULL, cluster=NULL WHERE id=?", (int(face_id),))
        else:
            conn.execute("UPDATE faces SET excluded=0, locked=0 WHERE id=?", (int(face_id),))
        if prev_pid is not None:
            recompute_person(conn, int(prev_pid))
    return {"id": int(face_id), "excluded": bool(excluded)}
