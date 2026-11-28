"""Embedding multimodale (SigLIP/SigLIP2, e modelli CLIP aggiunti dall'utente, via transformers) per immagini,
fotogrammi video e testo."""
from __future__ import annotations

import itertools
import logging
import os
import sqlite3
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

from . import netguard, sourcefs
from .config import Config
from .db import from_blob, get_meta, set_meta, to_blob
from .scanner_metadata import _fs, extract_frames, source_lost_error

log = logging.getLogger("fotoscan")

_LOST = "sorgente-persa"  # segnaposto: file non letto perche' il disco e' sparito (resta 'meta', nessun errore)
DEFAULT_MODEL = "google/siglip2-base-patch16-224"  # modello dei cataloghi vecchi senza meta 'embedding_model'


class ModelMismatch(ValueError):
    """Il modello impostato non e' quello degli embedding gia' nel catalogo (vettori non confrontabili)."""


def catalog_model_conflict(conn: sqlite3.Connection, cfg: Config) -> str | None:
    """Modello degli embedding presenti nel catalogo se diverso da cfg.model_name, altrimenti None."""
    if conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone() is None:
        return None
    cur = get_meta(conn, "embedding_model") or DEFAULT_MODEL
    return cur if cur != cfg.model_name else None


def _is_oom(torch, e: BaseException) -> bool:
    oom = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
    return isinstance(e, MemoryError) or (oom is not None and isinstance(e, oom)) or "out of memory" in str(e).lower()


def _is_device_error(e: BaseException) -> bool:
    """Guasto della GPU (non del singolo file): driver/architettura non supportati, CUDA rotto."""
    msg = str(e).lower()
    return isinstance(e, RuntimeError) and any(k in msg for k in (
        "cuda", "no kernel image", "cudnn", "cublas", "hip", "mps", "device-side assert", "accelerator"))


class Embedder:
    def __init__(self, cfg: Config, device: str | None = None):
        import torch
        from transformers import AutoModel, AutoProcessor, AutoTokenizer

        self.torch = torch
        self.cfg = cfg
        self.device = device or cfg.resolve_device()
        self._batch_cap: int | None = None  # lotto ridotto dopo un OOM della scheda video
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()  # niente warning di config/deprecazione ad ogni caricamento
        hf_logging.disable_progress_bar()
        log.info("carico %s su %s", cfg.model_name, self.device)
        # solo dalla cache locale; la rete (solo Hugging Face) si apre unicamente se il modello manca
        model = netguard.load_pretrained(AutoModel.from_pretrained, cfg.model_name, dtype=dtype)
        try:
            model = model.to(self.device)
        except Exception as e:  # noqa: BLE001 - GPU assente/rotta, driver o architettura non supportati
            if self.device == "cpu":
                raise
            log.warning("dispositivo %s non utilizzabile (%s): ripiego sul processore", self.device, e)
            self.device, dtype = "cpu", torch.float32
            model = model.to("cpu").float()
        self.model = model.eval()
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
        with Image.open(_fs(path)) as im:  # _fs: percorsi >260 caratteri su Windows
            if im.format == "JPEG":
                im.draft("RGB", (self.size * 2, self.size * 2))  # decodifica JPEG ridotta: molto piu' veloce
            im.load()
            return self.prep_image(im)

    # ---- encoding ----
    def _norm(self, t):
        t = self.torch.nn.functional.normalize(t.float(), dim=-1)
        return t.cpu().numpy().astype(np.float32)

    def to_cpu(self) -> None:
        """Ripiego su processore (GPU rotta o troppo piccola anche per un'immagine sola)."""
        log.warning("scheda video non utilizzabile: proseguo con il processore (piu' lento)")
        self.model = self.model.to("cpu").float()
        self.device, self.dtype = "cpu", self.torch.float32
        self._batch_cap = None
        try:
            self.torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def encode_arrays(self, arrays: list[np.ndarray], batch: int | None = None) -> np.ndarray:
        batch = max(1, min(batch or self.cfg.batch_size, self._batch_cap or 10**9))
        outs = []
        i = 0
        with self.torch.inference_mode():
            while i < len(arrays):
                try:
                    x = self.torch.from_numpy(np.stack(arrays[i:i + batch])).to(self.device, dtype=self.dtype)
                    f = self.model.get_image_features(pixel_values=x)
                    if not self.torch.is_tensor(f):
                        f = f.pooler_output
                    outs.append(self._norm(f))
                except Exception as e:  # noqa: BLE001
                    x = f = None
                    if self.device != "cpu" and _is_oom(self.torch, e):
                        try:
                            self.torch.cuda.empty_cache()
                        except Exception:  # noqa: BLE001
                            pass
                        if batch > 1:  # memoria video insufficiente: lotto dimezzato (e ricordato) e si riprova
                            batch = max(1, batch // 2)
                            self._batch_cap = batch
                            log.warning("memoria video esaurita: lotto ridotto a %d", batch)
                            continue
                        self.to_cpu()  # nemmeno un'immagine sola ci sta
                        continue
                    if self.device != "cpu" and _is_device_error(e):
                        self.to_cpu()
                        continue
                    raise
                i += batch
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


class LazyTextEmbedder:
    """Embedder di testo creato al primo encode_texts e poi riusato: piu' chiamate a label_vectors nella stessa
    operazione (etichette, classi nulle, vocabolario dei suggerimenti) caricano il modello una volta sola.
    Se tutti i vettori sono gia' in cache il modello non si carica mai. release() lo libera subito."""

    def __init__(self, cfg: Config, device: str = "cpu"):
        self.cfg, self.device = cfg, device
        self._emb: Embedder | None = None

    def _get(self) -> Embedder:
        if self._emb is None:
            self._emb = Embedder(self.cfg, device=self.device)
        return self._emb

    @property
    def loaded(self) -> bool:
        return self._emb is not None

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        return self._get().encode_texts(texts)

    def __getattr__(self, name):  # logit_scale, encode_query, ... caricano il modello se servono
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._get(), name)

    def release(self) -> None:
        emb, self._emb = self._emb, None
        if emb is not None:
            release(emb)


def release(emb) -> None:
    """Libera subito la memoria di un modello non piu' usato (RAM e, se su GPU, VRAM)."""
    if emb is None:
        return
    if isinstance(emb, LazyTextEmbedder):
        emb.release()
        return
    torch = getattr(emb, "torch", None)
    dev = getattr(emb, "device", "cpu")
    for attr in ("model", "tokenizer"):
        if hasattr(emb, attr):
            try:
                setattr(emb, attr, None)
            except AttributeError:
                pass
    import gc
    gc.collect()
    if torch is not None and dev == "cuda":
        try:
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


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
        vecs = emb.encode_texts(missing)
        if embedder is None:
            release(emb)  # creato qui solo per questi prompt: non resta in memoria
        for p, v in zip(missing, vecs):
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
        other = catalog_model_conflict(conn, cfg)
        if other:  # vettori di modelli diversi nello stesso catalogo: dimensioni incompatibili, ricerca e piano si rompono
            raise ModelMismatch(f"Il catalogo contiene embedding del modello {other}, ma il modello impostato e' "
                                f"{cfg.model_name}: azzera gli embedding dalle Impostazioni oppure torna a {other}.")
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

    lost = threading.Event()  # sorgente sparita (disco scollegato): i file non letti restano 'meta', nessun errore

    def prepare(r) -> tuple[int, list[np.ndarray] | None, str | None]:
        if lost.is_set():
            return r["id"], None, _LOST
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
            if cfg.source_dir and not os.path.exists(_fs(r["path"])) and not sourcefs.reachable(cfg.source_dir):
                lost.set()
                return r["id"], None, _LOST
            return r["id"], None, f"{type(e).__name__}: {e}"

    def flush(part: list) -> None:
        """Codifica un blocco di file preparati e scrive embedding + stato in una transazione breve: la codifica
        (lenta) avviene prima di toccare il catalogo, cosi' non c'e' lock di scrittura durante la GPU."""
        flat: list[np.ndarray] = []
        spans: list[tuple[int, int, int]] = []
        errors: list[tuple[int, str]] = []
        for fid, arrs, err in part:
            if err == _LOST:
                continue
            if err:
                errors.append((fid, err))
                continue
            spans.append((fid, len(flat), len(flat) + len(arrs)))
            flat.extend(arrs)
        vecs: dict[int, np.ndarray] = {}
        if flat:
            try:
                allv = emb.encode_arrays(flat)
                for fid, a, b in spans:
                    vecs[fid] = allv[a:b]
            except Exception:  # noqa: BLE001
                log.exception("errore embedding blocco: riprovo un file alla volta")
                if emb.device == "cuda":
                    emb.torch.cuda.empty_cache()
                for fid, a, b in spans:  # un solo file difettoso non deve far scartare tutto il blocco
                    try:
                        vecs[fid] = emb.encode_arrays(flat[a:b])
                    except Exception as e2:  # noqa: BLE001
                        errors.append((fid, f"embedding: {e2}"))
        for fid, err in errors:
            conn.execute("UPDATE files SET status='error', error=? WHERE id=?", (err, fid))
        for fid, vv in vecs.items():
            v = vv.mean(axis=0)
            v /= (np.linalg.norm(v) + 1e-9)
            conn.execute("INSERT OR REPLACE INTO embeddings(file_id, vec) VALUES(?,?)", (fid, to_blob(v)))
            conn.execute("UPDATE files SET status='embedded' WHERE id=?", (fid,))
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
            if (should_stop and should_stop()) or lost.is_set():
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
    if lost.is_set():
        raise source_lost_error(cfg)
    return done
