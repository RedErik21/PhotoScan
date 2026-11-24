"""Configurazione centrale. Salvata in config.json (modificabile anche dalla UI)."""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Eseguibile PyInstaller (PhotoScan.exe): il codice e le risorse stanno nella cartella temporanea _MEIPASS
# (sola lettura), mentre catalogo, cache, config e log vanno in %LOCALAPPDATA%\PhotoScan.
FROZEN = bool(getattr(sys, "frozen", False))
CODE_ROOT = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parent.parent
if FROZEN:
    ROOT = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PhotoScan"
    ROOT.mkdir(parents=True, exist_ok=True)
else:
    ROOT = CODE_ROOT
CONFIG_FILE =Path(os.environ["FOTOSCAN_CONFIG"]) if os.environ.get("FOTOSCAN_CONFIG") else ROOT / "config.json"  # override per test isolati

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".wmv")

# Modelli multimodali supportati (selezionabili in Impostazioni > Generale).
#   gflops          costo di un'immagine (torre visiva, un passaggio) in miliardi di operazioni: serve a stimare la velocita'
#                   di un modello non ancora misurato a partire da uno misurato (calcolo da dimensioni e token del modello)
#   download_gb     peso del download (pesi in float32; "base" e "so400m" misurati sui file scaricati, "large" stimato dai parametri)
#   weights_gb      memoria video occupata dai pesi in float16 (meta' del download)
#   act_gb_per_img  memoria video per immagine di un lotto (stima da un'esecuzione reale di so400m con lotto 64: ~4,7 GB totali)
#   rank            accuratezza relativa (1 = piu' veloce, 3 = piu' accurato)
#   calibrated      True se temp_factor/search_thr sono misurati per il modello (False: valori di base, da calibrare)
#   temp_factor     calibrazione dei contesti per modello: temperatura effettiva = score_temperature x fattore.
#                   score_temperature/score_threshold sono tarati a mano su "base" (fattore 1). Per "so400m" il fattore
#                   e' misurato sulle stesse 3000 foto dell'archivio reale (2026-11-23): con 0,85 assegna lo stesso numero
#                   di foto di base (512 vs 525) con il massimo accordo per etichetta. "large" non e' ancora misurato (1).
#   search_thr      soglie della ricerca a parole per il modello; senza la chiave valgono BASE_SEARCH_THR.
# L'utente puo' sostituire temp_factor/search_thr per qualsiasi modello (Config.model_overrides, Impostazioni > Per
# esperti) e aggiungere modelli suoi (Config.custom_models): vedi model_profile() e all_models().
AVAILABLE_MODELS: list[dict] = [
    {
        "id": "google/siglip2-base-patch16-224",
        "label": "SigLIP 2 Base (224×224) — Veloce",
        "hint": "Bilanciato e leggero (download ~1,5 GB). Ottimo sia su CPU che su GPU.",
        "rank": 1, "calibrated": True, "temp_factor": 1.0, "gflops": 35, "download_gb": 1.5, "weights_gb": 0.75, "act_gb_per_img": 0.006,
    },
    {
        "id": "google/siglip2-large-patch16-384",
        "label": "SigLIP 2 Large (384×384) — Alta precisione",
        "hint": "Quasi 3× i pixel (download ~3,5 GB): riconosce dettagli più fini e scene complesse. Consigliata GPU NVIDIA.",
        "rank": 2, "temp_factor": 1.0, "gflops": 380, "download_gb": 3.5, "weights_gb": 1.75, "act_gb_per_img": 0.025,
    },
    {
        "id": "google/siglip2-so400m-patch14-384",
        "label": "SigLIP 2 SO400M (384×384) — Massima precisione",
        "hint": "Il modello più accurato (download ~4,5 GB), eccellente in italiano e su dettagli piccoli. Consigliata GPU NVIDIA.",
        "rank": 3, "calibrated": True, "temp_factor": 0.85,
        # soglie della ricerca a parole (coseno). min_match/level_*: stessa quota di risultati delle soglie di base
        # (3000 foto, stessi file per i due modelli). min_gap: sull'archivio intero (47.697 file) 30 ricerche vere hanno
        # stacco >= 0,099, 18 frasi senza senso su 20 restano sotto 0,090 (con 0,065 di base ne passavano 16 su 20).
        "search_thr": {"min_match": 0.100, "min_gap": 0.090, "level_good": 0.114, "level_ok": 0.125},
        "gflops": 690, "download_gb": 4.5, "weights_gb": 2.3, "act_gb_per_img": 0.035,
    },
]

# Soglie della ricerca a parole (coseno) tarate a mano su "base" (archivio reale, 2026-11):
# - min_match: sotto questo valore un risultato e' rumore (le query senza senso restano intorno a 0.10).
# - min_gap: il migliore deve staccarsi dalla media dell'archivio; le query senza senso non si staccano.
# - level_good / level_ok: fasce qualitative mostrate in UI ("buona" / "ottima").
BASE_SEARCH_THR: dict[str, float] = {"min_match": 0.105, "min_gap": 0.065, "level_good": 0.120, "level_ok": 0.135}
SEARCH_THR_KEYS = tuple(BASE_SEARCH_THR)
# Famiglie di modelli testo-immagine che Embedder sa usare (transformers, get_image_features/get_text_features).
SUPPORTED_MODEL_TYPES = ("siglip", "siglip2", "clip", "chinese_clip", "altclip")


# Etichette di contesto predefinite (nome cartella, descrizione per il modello).
DEFAULT_LABELS: list[dict[str, str]] = [
    # Calibrate a mano su foto di famiglia (Google Foto, 2026-11): prompt brevi e concreti.
    # Niente etichette generiche tipo "persone": dominerebbero ogni mese.
    {"name": "Mare e spiaggia", "prompt": "una foto di mare con onde e spiaggia di sabbia"},
    {"name": "Tramonti", "prompt": "un tramonto o un'alba sul mare o sulla campagna"},
    {"name": "Feste e compleanni", "prompt": "una festa di compleanno con torta e candeline"},
    {"name": "Cibo e ristoranti", "prompt": "un piatto di cibo"},
    {"name": "Cena in compagnia", "prompt": "amici seduti a tavola che mangiano insieme"},
    {"name": "Animali", "prompt": "un animale domestico, un cane o un gatto"},
    {"name": "Città e monumenti", "prompt": "una strada di una città con edifici e monumenti"},
    {"name": "Documenti e scontrini", "prompt": "un documento di carta con testo scritto"},
    {"name": "Screenshot", "prompt": "uno screenshot di uno schermo di telefono con interfaccia di app"},
    {"name": "Auto e moto", "prompt": "un'automobile parcheggiata per strada"},
    {"name": "Casa e interni", "prompt": "l'interno di una stanza di casa con divano e mobili"},
    {"name": "Concerto e discoteca", "prompt": "un concerto o una discoteca con luci colorate e folla"},
    {"name": "Natale", "prompt": "un albero di Natale con luci e decorazioni natalizie"},
    {"name": "Palestra e sport", "prompt": "una foto in palestra con attrezzi, pesi o macchine per allenarsi"},
]


@dataclass
class Config:
    source_dir: str = ""
    output_dir: str = ""
    db_path: str = str(ROOT / "catalogo.db")
    cache_dir: str = str(ROOT / "cache")
    ffmpeg_path: str = ""  # vuoto = autodetect

    model_name: str = "google/siglip2-base-patch16-224"
    device: str = "auto"  # auto | cuda | mps | cpu
    batch_size: int = 64
    workers: int = 8
    video_fractions: list[float] = field(default_factory=lambda: [0.15, 0.50, 0.85])

    score_threshold: float = 0.30  # prob. minima (softmax con classe nulla) per assegnare un contesto
    score_temperature: float = 4.0  # >1 = distribuzione piu' morbida
    null_prompts: list[str] = field(default_factory=lambda: [
        "una foto", "un'immagine", "una foto sfocata", "una persona che guarda la fotocamera",
        "un selfie", "un ritratto di una persona", "un oggetto appoggiato su un tavolo",
    ])
    event_gap_hours: float = 24.0  # pausa che separa due eventi: 24h tiene insieme viaggi e weekend (6h spezzava ogni giorno)
    min_event_size: int = 15  # eventi piu' piccoli confluiscono in "MM - Varie" (3 produceva ~1900 cartelle-evento)
    event_burst_ratio: float = 4.0  # evento solo se nel giorno piu' pieno ci sono >= N volte le foto di un giorno tipico dell'anno (0 = spento)
    event_names: str = "descrittivi"  # descrittivi ('12 ottobre - Roma - Feste') | data ('Evento 2019-10-12')
    context_period: str = "anno"  # anno: una cartella per tema e anno ("2024/Palestra e sport") | mese: per mese se >= min_context_size
    min_context_size: int = 10  # cartelle di contesto (mese+etichetta) con meno foto tornano negli eventi / Varie
    dup_mode: str = "subfolder"  # subfolder (_Duplicati) | skip
    link_mode: str = "hardlink"  # hardlink (fallback copia) | copy
    folder_template: str = "{anno}/{mese:02d} - {contesto}"

    enable_faces: bool = False
    faces_threshold: float = 0.50   # soglia coseno per creare nuovi gruppi di volti
    faces_assign_threshold: float = 0.52  # soglia coseno conservativa per agganciare nuovi volti a persone gia' esistenti
    faces_min_photos: int = 3       # foto distinte minime per creare una persona
    faces_det_size: int = 640       # lato del riquadro di rilevamento SCRFD (480=veloce, 640=bilanciato, 800/960=volti piccoli)
    labels: list[dict[str, str]] = field(default_factory=lambda: [dict(x) for x in DEFAULT_LABELS])
    # Per esperti: modelli aggiunti dall'utente ({id, label, hint, model_type}) e calibrazioni sostituite per modello
    # ({id: {"temp_factor": x, "search_thr": {...}, "source": "manuale"|"automatica", "at": epoch}}).
    custom_models: list[dict] = field(default_factory=list)
    model_overrides: dict[str, dict] = field(default_factory=dict)

    # duplicati quasi-identici (organizer.build_plan): 0 = soglia coseno disattivata
    near_dup_threshold: float = 0.985  # coseno minimo tra embedding (stesso tipo di file, data EXIF)
    near_dup_seconds: float = 120.0  # finestra temporale massima tra i due scatti
    near_dup_name_rule: bool = True  # "<base>-modificato/-modificata" nella stessa cartella
    max_event_days: float = 7.0  # durata massima di un evento: oltre, si taglia nella pausa piu' lunga
    # controlli di contenuto sulle coppie candidate (organizer._near_dups); 0 / -1 = controllo disattivato
    near_dup_min_luma: float = 12.0  # luminanza media minima (0-255) di entrambe le foto (nere/quasi nere)
    near_dup_min_std: float = 10.0  # deviazione standard minima (contrasto) di entrambe le foto (uniformi)
    near_dup_min_corr: float = 0.15  # correlazione minima fra le miniature 64x64 in grigio (inquadrature diverse)
    near_dup_name_min_sim: float = 0.90  # coseno minimo fra due foto per la regola "-modificato" (se c'e' l'embedding)

    # aggiornamenti (fotoscan/updater.py, Impostazioni > Aggiornamenti)
    update_check_on_start: bool = True   # all'avvio chiede a GitHub se c'e' una versione nuova
    update_auto_download: bool = False   # se c'e', la scarica subito (senza installarla)
    update_auto_install: bool = False    # installa da sola al prossimo avvio quella scaricata
    update_prerelease: bool = False      # considera anche le versioni di prova (pre-release)

    # ---- utilita' ----
    def context_temperature(self) -> float:
        """Temperatura dei contesti calibrata per il modello in uso (vedi temp_factor in AVAILABLE_MODELS)."""
        return max(self.score_temperature * model_profile(self.model_name, self)["temp_factor"], 0.1)

    def resolve_device(self) -> str:
        if self.device != "auto":
            return self.device
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def find_ffmpeg(self) -> tuple[str | None, str | None]:
        """Ritorna (ffmpeg, ffprobe) cercando anche nelle cartelle di winget."""
        def look(name: str) -> str | None:
            if self.ffmpeg_path:
                cand = Path(self.ffmpeg_path)
                cand = cand if cand.is_dir() else cand.parent
                for ext in ("", ".exe"):
                    p = cand / (name + ext)
                    if p.exists():
                        return str(p)
            found = shutil.which(name)
            if found:
                return found
            local = os.environ.get("LOCALAPPDATA", "")
            pats = [
                f"{local}/Microsoft/WinGet/Links/{name}.exe",
                f"{local}/Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/{name}.exe",
                f"C:/ffmpeg/bin/{name}.exe",
                f"C:/Program Files/ffmpeg/bin/{name}.exe",
            ]
            for pat in pats:
                hits = glob.glob(pat, recursive=True)
                if hits:
                    return hits[0]
            return None

        return look("ffmpeg"), look("ffprobe")

    def save(self, path: Path = CONFIG_FILE) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    @property
    def thumbs_dir(self) -> Path:
        return Path(self.cache_dir) / "thumbs"

    @property
    def frames_dir(self) -> Path:
        return Path(self.cache_dir) / "frames"


def preset_for(model_id: str) -> dict | None:
    return next((m for m in AVAILABLE_MODELS if m["id"] == model_id), None)


def preset_profile(model_id: str) -> dict:
    """Calibrazione di fabbrica: quella misurata per i modelli testati, altrimenti i valori di base."""
    m = preset_for(model_id) or {}
    return {"temp_factor": float(m.get("temp_factor", 1.0)),
            "search_thr": {**BASE_SEARCH_THR, **(m.get("search_thr") or {})}}


def model_profile(model_id: str, cfg: "Config | None" = None) -> dict:
    """Calibrazione effettiva di un modello: preset (o base) con sopra le sostituzioni dell'utente.

    Ritorna {temp_factor, search_thr, source}; source = "preset" | "base" | "manuale" | "automatica"."""
    prof = preset_profile(model_id)
    m = preset_for(model_id)
    source = "preset" if m and m.get("calibrated") else "base"
    ov = (cfg.model_overrides if cfg is not None else {}).get(model_id) or {}
    if ov:
        if isinstance(ov.get("temp_factor"), (int, float)):
            prof["temp_factor"] = float(ov["temp_factor"])
        thr = ov.get("search_thr") or {}
        prof["search_thr"].update({k: float(v) for k, v in thr.items() if k in SEARCH_THR_KEYS and isinstance(v, (int, float))})
        source = ov.get("source") or "manuale"
    prof["source"] = source
    return prof


def all_models(cfg: "Config | None" = None) -> list[dict]:
    """Modelli testati + modelli aggiunti dall'utente (tested=False, custom=True)."""
    out = [{**m, "tested": True, "custom": False} for m in AVAILABLE_MODELS]
    known = {m["id"] for m in out}
    for c in (cfg.custom_models if cfg is not None else []):
        if c.get("id") and c["id"] not in known:
            out.append({"id": c["id"], "label": c.get("label") or c["id"],
                        "hint": c.get("hint") or "Modello aggiunto da te: non testato, calibralo sul tuo archivio.",
                        "model_type": c.get("model_type"), "tested": False, "custom": True})
            known.add(c["id"])
    return out


def load_config(path: Path = CONFIG_FILE) -> Config:
    cfg = Config()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except Exception:
            pass
    # Override da ambiente: permettono catalogo/cache isolati per test paralleli.
    if os.environ.get("FOTOSCAN_DB"):
        cfg.db_path = os.environ["FOTOSCAN_DB"]
    if os.environ.get("FOTOSCAN_CACHE"):
        cfg.cache_dir = os.environ["FOTOSCAN_CACHE"]
    Path(cfg.cache_dir).mkdir(parents=True, exist_ok=True)
    return cfg
