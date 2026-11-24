"""Embedding multimodale (SigLIP/SigLIP2, e modelli CLIP aggiunti dall'utente, via transformers) per immagini,
fotogrammi video e testo."""
from __future__ import annotations

import itertools
import logging
import sqlite3
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

from . import netguard
from .config import Config
from .db import from_blob, get_meta, set_meta, to_blob
from .scanner_metadata import extract_frames

log = logging.getLogger("fotoscan")


class Embedder:
    def __init__(self, cfg: Config, device: str | None = None):
        import torch
        from transformers import AutoModel, AutoProcessor, AutoTokenizer

        self.torch = torch
        self.cfg = cfg
        self.device = device or cfg.resolve_device()
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()  # niente warning di config/deprecazione ad ogni caricamento
        hf_logging.disable_progress_bar()
        log.info("carico %s su %s", cfg.model_name, self.device)
        # solo dalla cache locale; la rete (solo Hugging Face) si apre unicamente se il modello manca
        self.model = netguard.load_pretrained(AutoModel.from_pretrained, cfg.model_name, dtype=dtype).to(self.device).eval()
        self.tokenizer = netguard.load_pretrained(AutoTokenizer.from_pretrained, cfg.model_name)
        proc = netguard.load_pretrained(AutoProcessor.from_pretrained, cfg.model_name)
        ip = proc.image_processor
        self.size = int(self.model.config.vision_config.image_size)
        self.mean = np.array(ip.image_mean, dtype=np.float32).reshape(1, 1, 3)
        self.std = np.array(ip.image_std, dtype=np.float32).reshape(1, 1, 3)
        self.dtype = dtype
        self.logit_scale = float(self.model.logit_scale.exp().item()) if hasattr(self.model, "logit_scale") else 100.0
        self.logit_bias = float(self.model.logit_bias.item()) if hasattr(self.model, "logit_bias") else -16.0
        self.max_len = min(64, int(getattr(self.model.config.text_config, "max_position_embeddings", 64)))
        # SigLIP e' addestrato con testo riempito fino a 64 token e senza maschera; CLIP e simili vogliono la maschera.
        self.model_type = str(getattr(self.model.config, "model_type", ""))
        self.use_mask = not self.model_type.startswith("siglip")

    # ---- preprocessing (in thread, senza toccare gli originali) ----
    def prep_image(self, im: Image.Image) -> np.ndarray:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            # trasparenza: appiattisce su bianco (convert("RGB") darebbe colori casuali dove alpha=0)
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, rgba)
        im = im.convert("RGB")
        im = im.resize((self.size, self.size), Image.BICUBIC)
        a = (np.asarray(im, dtype=np.float32) / 255.0 - self.mean) / self.std
        return a.transpose(2, 0, 1)

    def load_prep(self, path: str) -> np.ndarray:
        with Image.open(path) as im:
            if im.format == "JPEG":
                im.draft("RGB", (self.size * 2, self.size * 2))  # decodifica JPEG ridotta: molto piu' veloce
            im.load()
            return self.prep_image(im)

    # ---- encoding ----
    def _norm(self, t):
        t = self.torch.nn.functional.normalize(t.float(), dim=-1)
        return t.cpu().numpy().astype(np.float32)

    def encode_arrays(self, arrays: list[np.ndarray], batch: int | None = None) -> np.ndarray:
        batch = batch or self.cfg.batch_size
        outs = []
        with self.torch.inference_mode():
            for i in range(0, len(arrays), batch):
                x = self.torch.from_numpy(np.stack(arrays[i:i + batch])).to(self.device, dtype=self.dtype)
                f = self.model.get_image_features(pixel_values=x)
                if not self.torch.is_tensor(f):
                    f = f.pooler_output
                outs.append(self._norm(f))
        return np.concatenate(outs) if outs else np.zeros((0, 0), dtype=np.float32)

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        texts = [t.lower() for t in texts]
        tok = self.tokenizer(texts, padding="max_length", truncation=True, max_length=self.max_len, return_tensors="pt")
        extra = {}
        if self.use_mask and "attention_mask" in tok:
            extra["attention_mask"] = tok["attention_mask"].to(self.device)
        with self.torch.inference_mode():
            f = self.model.get_text_features(input_ids=tok["input_ids"].to(self.device), **extra)
            if not self.torch.is_tensor(f):
                f = f.pooler_output
        return self._norm(f)

    def encode_query(self, text: str) -> np.ndarray:
        """Prompt ensembling leggero per query brevi: media 3 formulazioni naturali e rinormalizza."""
        q = " ".join(text.split()).lower()
        if not q:
            return self.encode_texts([""])[0]
        if q.startswith(("una ", "un ", "un'", "uno ", "foto ", "immagine ")):
            variants = [q, f"una foto di {q}"] if not q.startswith(("una foto", "un'immagine")) else [q]
        else:
            variants = [q, f"una foto di {q}", f"un'immagine di {q}"]
        vecs = self.encode_texts(variants)
        mean = vecs.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        return (mean / norm).astype(np.float32) if norm > 0 else vecs[0]


def label_vectors(conn: sqlite3.Connection, prompts: list[str], embedder: Embedder | None, cfg: Config) -> np.ndarray:
    """Embedding testuali delle etichette, con cache nel DB (il modello si carica solo se servono nuovi prompt)."""
    if get_meta(conn, "label_model") != cfg.model_name:
        # vettori di un altro modello non sono confrontabili con gli embedding immagine
        conn.execute("DELETE FROM label_cache")
        set_meta(conn, "label_model", cfg.model_name)
    cached = {r["prompt"]: from_blob(r["vec"]) for r in conn.execute("SELECT prompt, vec FROM label_cache")}
    missing = [p for p in prompts if p not in cached]
    if missing:
        emb = embedder or Embedder(cfg, device="cpu")
        for p, v in zip(missing, emb.encode_texts(missing)):
            conn.execute("INSERT OR REPLACE INTO label_cache(prompt, vec) VALUES(?,?)", (p, to_blob(v)))
            cached[p] = v
        conn.commit()
    return np.stack([cached[p] for p in prompts])


# ------------------------------------------------------------- pipeline
def embed_pending(conn: sqlite3.Connection, cfg: Config, embedder: Embedder | None = None,
                  progress: Callable[[str, int, int], None] | None = None,
                  should_stop: Callable[[], bool] | None = None) -> int:
    """status meta -> embedded (o error). Riprendibile: salta cio' che e' gia' nel catalogo."""
    rows = conn.execute("SELECT id,path,kind,duration FROM files WHERE status='meta' ORDER BY id").fetchall()
    total, done = len(rows), 0
    if rows:
        set_meta(conn, "embedding_model", cfg.model_name)
    if progress:  # la fase si dichiara subito, prima del caricamento del modello (secondi su GPU)
        progress("embedding", 0, total)
    if not rows:
        return 0
    emb = embedder or Embedder(cfg)
    set_meta(conn, "logit_scale", str(emb.logit_scale))
    set_meta(conn, "logit_bias", str(emb.logit_bias))
    set_meta(conn, "model", cfg.model_name)
    ffmpeg, _ = cfg.find_ffmpeg()
    chunk = max(16, cfg.batch_size)
    window = max(chunk, 4 * cfg.workers)  # file preparati in anticipo (limita la memoria)

    def prepare(r) -> tuple[int, list[np.ndarray] | None, str | None]:
        try:
            if r["kind"] == "image":
                return r["id"], [emb.load_prep(r["path"])], None
            frames = extract_frames(r["path"], r["duration"], cfg.frames_dir, r["id"], cfg.video_fractions, ffmpeg)
            arrs = []
            for fp in frames:
                try:
                    arrs.append(emb.load_prep(str(fp)))
                except Exception:
                    continue
            if not arrs:
                raise RuntimeError("fotogrammi illeggibili")
            return r["id"], arrs, None
        except Exception as e:
            return r["id"], None, f"{type(e).__name__}: {e}"

    def flush(part: list) -> None:
        """Codifica un blocco di file preparati e scrive embedding + stato in un'unica transazione."""
        flat: list[np.ndarray] = []
        spans: list[tuple[int, int, int]] = []
        for fid, arrs, err in part:
            if err:
                conn.execute("UPDATE files SET status='error', error=? WHERE id=?", (err, fid))
                continue
            spans.append((fid, len(flat), len(flat) + len(arrs)))
            flat.extend(arrs)
        if flat:
            try:
                vecs = emb.encode_arrays(flat)
                for fid, a, b in spans:
                    v = vecs[a:b].mean(axis=0)
                    v /= (np.linalg.norm(v) + 1e-9)
                    conn.execute("INSERT OR REPLACE INTO embeddings(file_id, vec) VALUES(?,?)", (fid, to_blob(v)))
                    conn.execute("UPDATE files SET status='embedded' WHERE id=?", (fid,))
            except Exception as e:  # es. OOM: segna il blocco in errore e prosegui
                log.exception("errore embedding blocco")
                for fid, _, _ in spans:
                    conn.execute("UPDATE files SET status='error', error=? WHERE id=?", (f"embedding: {e}", fid))
                if emb.device == "cuda":
                    emb.torch.cuda.empty_cache()
        conn.commit()

    # Pipeline a finestra (niente barriera per blocco): la CPU prepara i file successivi mentre la GPU
    # codifica il blocco corrente. Memoria limitata alla finestra, ordine di rows preservato.
    it = iter(rows)
    pending: deque = deque()
    batch: list = []
    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        for r in itertools.islice(it, window):
            pending.append(ex.submit(prepare, r))
        while pending:
            if should_stop and should_stop():
                break
            batch.append(pending.popleft().result())
            nxt = next(it, None)
            if nxt is not None:
                pending.append(ex.submit(prepare, nxt))
            if len(batch) >= chunk:
                flush(batch)
                done += len(batch)
                batch = []
                if progress:
                    progress("embedding", done, total)
        if batch:  # resto, anche in caso di stop richiesto
            flush(batch)
            done += len(batch)
            if progress:
                progress("embedding", done, total)
    return done
