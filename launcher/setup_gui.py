"""Interfaccia grafica guidata di PhotoScan-Setup (3 passi).

Usa pywebview (WebView2, lo stesso motore di PhotoScan desktop) per replicare fedelmente
il design Indaco / Material 3 della prima configurazione dell'app, con ripiego automatico
su Tkinter se pywebview non è disponibile.

Tutta la logica di rilevamento, download ed estrazione resta in launcher/setup_photoscan.py.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from setup_photoscan import (
    MODELS,
    RUNTIME_GB,
    create_desktop_shortcut,
    detect_hardware,
    download_parts,
    extract,
    installed_state,
    latest_release,
    package_parts,
    recommend,
    space_warning,
    write_config,
    write_install_marker,
)

MODEL_DESC = {
    "base": "Leggero e rapido, ideale su CPU o GPU con poca VRAM.",
    "large": "Buon equilibrio tra precisione dei dettagli e velocità.",
    "so400m": "Massima accuratezza in italiano e su dettagli piccoli.",
}

RUNTIME_OPTIONS = [
    {"id": "gpu", "title": "GPU NVIDIA (CUDA)", "desc": "Analisi molto più veloce con scheda video NVIDIA.", "gb": RUNTIME_GB["gpu"]},
    {"id": "cpu", "title": "Solo processore (CPU)", "desc": "Compatibile con tutti i PC, senza scheda NVIDIA.", "gb": RUNTIME_GB["cpu"]},
]


def data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PhotoScan"


def _find_asset(*rel_candidates: str) -> Path | None:
    bases = [Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.insert(0, Path(meipass))
    for base in bases:
        for rel in rel_candidates:
            p = base / rel
            if p.exists():
                return p
    return None


def _resolve_exe(app_dir: Path) -> Path:
    direct = app_dir / "PhotoScan.exe"
    if direct.exists():
        return direct
    nested = app_dir / "PhotoScan" / "PhotoScan.exe"
    if nested.exists():
        return nested
    return direct


def _set_titlebar_theme(window: Any, dark: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        native = getattr(window, "native", None)
        hwnd = int(native.Handle.ToInt64()) if native is not None and hasattr(native, "Handle") else 0
        if not hwnd:
            return
        val = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        for attr in (20, 19):
            if dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_int(attr), ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


def _wordmark_font_css() -> str:
    font = _find_asset("frontend/vendor/fonts/playwrite-us-trad.woff2", "playwrite-us-trad.woff2")
    if not font:
        return ""
    try:
        b64 = base64.b64encode(font.read_bytes()).decode("ascii")
        return (
            "@font-face { font-family: 'Playwrite US Trad'; "
            f"src: url('data:font/woff2;base64,{b64}') format('woff2'); "
            "font-weight: 400; font-style: normal; font-display: swap; }"
        )
    except OSError:
        return ""


# ==============================================================================
#  HTML / CSS / JS (identico allo stile Indaco + Material 3 di PhotoScan)
# ==============================================================================

LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="38" height="38" aria-hidden="true">
  <defs>
    <linearGradient id="ps-bg" x1="12%" y1="8%" x2="88%" y2="92%">
      <stop offset="0%" stop-color="#2c2488"/><stop offset="48%" stop-color="#4b45d8"/><stop offset="100%" stop-color="#191645"/>
    </linearGradient>
    <radialGradient id="ps-aurora" cx="84%" cy="16%" r="52%">
      <stop offset="0%" stop-color="#fdba74" stop-opacity="0.92"/><stop offset="45%" stop-color="#a855f7" stop-opacity="0.45"/><stop offset="100%" stop-color="#4b45d8" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="ps-rim" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#ffffff" stop-opacity="0.55"/><stop offset="50%" stop-color="#c4b5fd" stop-opacity="0.22"/><stop offset="100%" stop-color="#ffffff" stop-opacity="0.35"/>
    </linearGradient>
    <radialGradient id="ps-lens" cx="38%" cy="34%" r="68%">
      <stop offset="0%" stop-color="#ffffff" stop-opacity="0.28"/><stop offset="55%" stop-color="#a5b4fc" stop-opacity="0.16"/><stop offset="100%" stop-color="#1e1b4b" stop-opacity="0.42"/>
    </radialGradient>
    <linearGradient id="ps-p" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#ffffff"/><stop offset="65%" stop-color="#ece9ff"/><stop offset="100%" stop-color="#ffdcb8"/>
    </linearGradient>
    <linearGradient id="ps-star" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fef08a"/><stop offset="100%" stop-color="#f59e0b"/>
    </linearGradient>
  </defs>
  <rect x="28" y="28" width="456" height="456" rx="108" fill="url(#ps-bg)"/>
  <rect x="28" y="28" width="456" height="456" rx="108" fill="url(#ps-aurora)"/>
  <rect x="30" y="30" width="452" height="452" rx="106" fill="none" stroke="url(#ps-rim)" stroke-width="4"/>
  <circle cx="256" cy="256" r="146" fill="url(#ps-lens)" stroke="#ffffff" stroke-opacity="0.36" stroke-width="5"/>
  <circle cx="256" cy="256" r="106" fill="#2e2980" fill-opacity="0.55" stroke="#ffffff" stroke-opacity="0.45" stroke-width="3.5"/>
  <path d="M 218 332 C 232 276 248 220 264 186 C 282 174 334 176 334 214 C 334 252 292 272 246 270" fill="none" stroke="url(#ps-p)" stroke-width="22" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M 366 128 Q 366 154 392 154 Q 366 154 366 180 Q 366 154 340 154 Q 366 154 366 128 Z" fill="url(#ps-star)"/>
</svg>"""


def _logo_markup() -> str:
    png = _find_asset("frontend/icon-192.png", "fotoscan/icon-256.png", "icon-192.png")
    if png:
        try:
            b64 = base64.b64encode(png.read_bytes()).decode("ascii")
            return f'<img src="data:image/png;base64,{b64}" alt="" width="40" height="40">'
        except OSError:
            pass
    return LOGO_SVG


def _build_html() -> str:
    font_css = _wordmark_font_css()
    logo_html = _logo_markup()
    return f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PhotoScan — Installazione guidata</title>
<style>
{font_css}
:root {{
  --primary: #4b45d8;
  --primary-hover: #3d37c2;
  --on-primary: #ffffff;
  --primary-container: #e5e3ff;
  --on-primary-container: #17125e;
  --tertiary: #9a4b1e;
  --tertiary-container: #ffe2d1;
  --on-tertiary-container: #3a1500;
  --error: #c0262d;
  --error-container: #ffe1de;
  --on-error-container: #5c0008;
  --surface: #f6f6f9;
  --on-surface: #18181f;
  --on-surface-variant: #5a5a6e;
  --outline: #8a8a9c;
  --outline-variant: #e1e0ea;
  --surface-low: #f0f0f5;
  --surface-mid: #ebebf1;
  --surface-high: #e5e5ed;
  --card: #ffffff;
  --success: #1f8a4c;
  --success-container: #dcf5e5;
  --on-success-container: #0b3a1f;
  --hero-a: #4b45d8;
  --hero-b: #8a5cf0;
  --hero-c: #e0742f;
  --shadow: rgba(20, 20, 43, 0.16);
  --font-plain: 'Segoe UI Variable', 'Segoe UI', system-ui, -apple-system, sans-serif;
  --font-brand: 'Playwrite US Trad', 'Segoe UI Variable Display', 'Segoe UI', sans-serif;
}}
:root[data-theme="dark"] {{
  --primary: #b3afff;
  --primary-hover: #c7c4ff;
  --on-primary: #1d1870;
  --primary-container: #34309c;
  --on-primary-container: #e5e3ff;
  --tertiary: #ffb68c;
  --tertiary-container: #6a2f0e;
  --on-tertiary-container: #ffe2d1;
  --error: #ffb3ad;
  --error-container: #7a1520;
  --on-error-container: #ffe1de;
  --surface: #0e0e13;
  --on-surface: #e7e6ee;
  --on-surface-variant: #a6a5b8;
  --outline: #6f6e82;
  --outline-variant: #272632;
  --surface-low: #15151b;
  --surface-mid: #1a1a21;
  --surface-high: #22222a;
  --card: #16161c;
  --success: #6fdc9a;
  --success-container: #12402a;
  --on-success-container: #c9f5d9;
  --hero-a: #3b35b8;
  --hero-b: #6a3fc9;
  --hero-c: #b0541c;
  --shadow: rgba(0, 0, 0, 0.45);
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden;
  font-family: var(--font-plain); background: var(--surface); color: var(--on-surface);
  -webkit-font-smoothing: antialiased; user-select: none;
}}
.shell {{
  display: flex; width: 100%; height: 100%;
  background: var(--card);
}}

/* ---------- Colonna sinistra: identità PhotoScan (identica a view-onboarding.css) ---------- */
.aside {{
  position: relative; isolation: isolate; flex: none; width: 312px;
  display: flex; flex-direction: column; justify-content: space-between; gap: 20px;
  padding: 28px 24px; color: #fff; overflow: hidden;
  background:
    radial-gradient(120% 130% at 100% 0%, color-mix(in srgb, var(--hero-c) 68%, transparent) 0%, transparent 48%),
    radial-gradient(95% 115% at 0% 100%, color-mix(in srgb, var(--hero-b) 92%, transparent) 0%, transparent 62%),
    linear-gradient(145deg, var(--hero-a), var(--hero-b));
}}
.aside::after {{
  content: ""; position: absolute; inset: 0; z-index: -1; pointer-events: none;
  background-image: radial-gradient(rgba(255, 255, 255, 0.22) 1px, transparent 1px);
  background-size: 20px 20px;
  -webkit-mask-image: linear-gradient(160deg, #000 10%, transparent 78%);
  mask-image: linear-gradient(160deg, #000 10%, transparent 78%);
}}
.aside__brand {{
  display: flex; align-items: center; gap: 12px;
}}
.aside__logo {{
  width: 40px; height: 40px; border-radius: 11px; flex: none;
  box-shadow: 0 8px 20px rgba(10, 9, 28, 0.32); overflow: hidden;
  display: grid; place-items: center;
}}
.aside__logo img, .aside__logo svg {{ width: 40px; height: 40px; display: block; border-radius: 10px; }}
.aside__wordmark {{
  font-family: var(--font-brand); font-size: 1.45rem; font-weight: 400; line-height: 1.15;
  letter-spacing: -0.01em; color: #fff;
  text-shadow: 0 4px 14px rgba(10, 9, 28, 0.25);
}}
.aside__visual {{
  display: flex; flex-direction: column; gap: 16px; margin-block: auto;
}}
.aside__art {{
  display: grid; place-items: center; padding: 18px; border-radius: 22px;
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.22);
  backdrop-filter: blur(8px);
}}
.aside__art svg {{ width: 100%; max-width: 210px; height: auto; display: block; }}
.aside__quote {{
  margin: 0; font-size: 1.0625rem; font-weight: 600; line-height: 1.4;
  letter-spacing: -0.01em; color: rgba(255, 255, 255, 0.96);
}}
.aside__sub {{
  margin: 6px 0 0; font-size: 0.875rem; line-height: 1.45;
  color: rgba(255, 255, 255, 0.82);
}}
.aside__trust {{
  display: inline-flex; align-items: center; gap: 8px; align-self: flex-start;
  padding: 6px 12px; border-radius: 999px;
  background: rgba(255, 255, 255, 0.14);
  border: 1px solid rgba(255, 255, 255, 0.24);
  font-size: 0.75rem; font-weight: 500; color: rgba(255, 255, 255, 0.94);
}}
.aside__trust svg {{ width: 14px; height: 14px; flex: none; }}

/* ---------- Colonna destra: flusso interattivo ---------- */
.workspace {{
  flex: 1 1 auto; min-width: 0; min-height: 0;
  display: flex; flex-direction: column; background: var(--card);
}}
.head {{
  flex: none; display: flex; align-items: center; justify-content: space-between;
  padding: 16px 28px 8px;
}}
.head__pill {{
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 12px; border-radius: 999px;
  background: var(--primary-container); color: var(--on-primary-container);
  font-size: 0.75rem; font-weight: 600;
}}
.icon-btn {{
  width: 36px; height: 36px; border-radius: 999px; border: 0; background: transparent;
  color: var(--on-surface-variant); cursor: pointer; display: grid; place-items: center;
  transition: background-color 150ms ease, color 150ms ease;
}}
.icon-btn:hover {{ background: var(--surface-low); color: var(--on-surface); }}
.icon-btn svg {{ width: 20px; height: 20px; }}

/* Stepper orizzontale identico a view-onboarding.css */
.stepper {{ flex: none; padding: 6px 28px 0; }}
.stepper__list {{
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
  list-style: none; margin: 0 0 10px; padding: 0;
}}
.stepper__btn {{
  display: flex; align-items: center; gap: 8px; width: 100%;
  border: 0; background: transparent; padding: 6px 4px; border-radius: 10px;
  color: var(--on-surface-variant); font: 500 0.8125rem/1.2 var(--font-plain);
  cursor: default; text-align: left; transition: background-color 150ms ease, color 150ms ease;
}}
.stepper__btn.is-done {{ cursor: pointer; color: var(--on-surface); }}
.stepper__btn.is-done:hover {{ background: var(--surface-low); }}
.stepper__btn.is-current {{ color: var(--primary); font-weight: 600; }}
.stepper__dot {{
  width: 22px; height: 22px; border-radius: 999px; flex: none;
  display: grid; place-items: center; font-size: 0.75rem; font-weight: 700;
  border: 1.5px solid var(--outline-variant); color: var(--on-surface-variant);
}}
.stepper__btn.is-current .stepper__dot {{
  background: var(--primary); border-color: var(--primary); color: var(--on-primary);
}}
.stepper__btn.is-done .stepper__dot {{
  background: var(--success); border-color: var(--success); color: #fff;
}}
.stepper__track {{
  width: 100%; height: 4px; border-radius: 999px; background: var(--surface-high); overflow: hidden;
}}
.stepper__fill {{
  height: 100%; border-radius: inherit; background: var(--primary);
  transition: width 280ms cubic-bezier(0.2, 0, 0, 1);
}}

/* Corpo scorrevole */
.body {{
  flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 20px 28px 16px;
}}
.panel {{ animation: slideIn 220ms cubic-bezier(0.2, 0, 0, 1) both; }}
@keyframes slideIn {{
  from {{ opacity: 0; transform: translateY(8px); }}
  to {{ opacity: 1; transform: none; }}
}}
.title {{
  margin: 0; font-size: 1.5rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1.25;
}}
.lead {{
  margin: 6px 0 18px; font-size: 0.9375rem; line-height: 1.5; color: var(--on-surface-variant);
}}

/* Griglia hardware 2x2 */
.hw-grid {{
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px;
}}
.hw-card {{
  display: flex; flex-direction: column; gap: 6px; padding: 14px 16px; border-radius: 16px;
  background: var(--surface-low); border: 1px solid var(--outline-variant);
}}
.hw-card__top {{
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
}}
.hw-card__lbl {{
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 0.72rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--on-surface-variant);
}}
.hw-card__lbl svg {{ width: 15px; height: 15px; color: var(--primary); }}
.hw-card__val {{
  font-size: 1.05rem; font-weight: 700; color: var(--on-surface); line-height: 1.25;
  overflow-wrap: anywhere;
}}
.hw-card__sub {{
  font-size: 0.8125rem; color: var(--on-surface-variant);
}}

/* Pillole / badge */
.pill {{
  display: inline-flex; align-items: center; padding: 2px 9px; border-radius: 999px;
  font-size: 0.72rem; font-weight: 700; white-space: nowrap;
}}
.pill--ok {{ background: var(--success-container); color: var(--on-success-container); }}
.pill--primary {{ background: var(--primary-container); color: var(--on-primary-container); }}
.pill--muted {{ background: var(--surface-high); color: var(--on-surface-variant); }}

/* Callout / banner */
.callout {{
  display: flex; align-items: flex-start; gap: 12px;
  margin-top: 14px; padding: 14px 16px; border-radius: 16px;
  border: 1px solid var(--outline-variant);
}}
.callout__ic {{
  width: 32px; height: 32px; border-radius: 10px; flex: none;
  display: grid; place-items: center;
}}
.callout__ic svg {{ width: 18px; height: 18px; }}
.callout__title {{ margin: 0; font-size: 0.9375rem; font-weight: 700; }}
.callout__text {{ margin: 3px 0 0; font-size: 0.84rem; line-height: 1.45; }}
.callout--info {{
  background: color-mix(in srgb, var(--primary-container) 55%, var(--card));
  border-color: color-mix(in srgb, var(--primary) 24%, transparent);
  color: var(--on-primary-container);
}}
.callout--info .callout__ic {{ background: var(--primary-container); color: var(--primary); }}
.callout--ok {{
  background: color-mix(in srgb, var(--success-container) 75%, var(--card));
  border-color: color-mix(in srgb, var(--success) 32%, transparent);
  color: var(--on-success-container);
}}
.callout--ok .callout__ic {{ background: var(--success-container); color: var(--success); }}
.callout--warn {{
  background: color-mix(in srgb, var(--tertiary-container) 75%, var(--card));
  border-color: color-mix(in srgb, var(--tertiary) 32%, transparent);
  color: var(--on-tertiary-container);
}}
.callout--warn .callout__ic {{ background: var(--tertiary-container); color: var(--tertiary); }}
.callout--err {{
  background: color-mix(in srgb, var(--error-container) 82%, var(--card));
  border-color: color-mix(in srgb, var(--error) 35%, transparent);
  color: var(--on-error-container);
}}
.callout--err .callout__ic {{ background: var(--error-container); color: var(--error); }}

/* Sezioni di scelta (Passo 2) */
.sec-label {{
  margin: 14px 0 8px; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--on-surface-variant);
}}
.sec-label:first-of-type {{ margin-top: 0; }}
.choices {{ display: flex; flex-direction: column; gap: 8px; }}
.choice {{
  display: flex; align-items: flex-start; gap: 12px;
  padding: 12px 14px; border-radius: 16px; cursor: pointer;
  background: var(--surface-low); border: 1.5px solid var(--outline-variant);
  transition: background-color 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
}}
.choice:hover {{ background: var(--surface-mid); }}
.choice.is-selected {{
  background: color-mix(in srgb, var(--primary-container) 55%, var(--card));
  border-color: var(--primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 14%, transparent);
}}
.choice__radio {{
  width: 18px; height: 18px; border-radius: 999px; flex: none; margin-top: 2px;
  border: 2px solid var(--outline); display: grid; place-items: center;
  transition: border-color 140ms ease;
}}
.choice.is-selected .choice__radio {{ border-color: var(--primary); }}
.choice__radio::after {{
  content: ""; width: 8px; height: 8px; border-radius: 999px;
  background: var(--primary); transform: scale(0); transition: transform 140ms ease;
}}
.choice.is-selected .choice__radio::after {{ transform: scale(1); }}
.choice__body {{ flex: 1 1 auto; min-width: 0; }}
.choice__head {{
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
}}
.choice__title {{ font-size: 0.9375rem; font-weight: 600; color: var(--on-surface); }}
.choice__size {{
  margin-left: auto; font-size: 0.78rem; font-weight: 700; color: var(--on-surface-variant);
  font-variant-numeric: tabular-nums;
}}
.choice__desc {{ margin: 3px 0 0; font-size: 0.8125rem; color: var(--on-surface-variant); line-height: 1.4; }}

/* Passo 3: installazione */
.prog-card {{
  padding: 20px; border-radius: 18px;
  background: var(--surface-low); border: 1px solid var(--outline-variant);
}}
.prog-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
.prog-status {{ font-size: 1rem; font-weight: 700; color: var(--on-surface); }}
.prog-pct {{ font-size: 0.9375rem; font-weight: 700; color: var(--primary); font-variant-numeric: tabular-nums; }}
.prog-bar {{
  margin: 14px 0 10px; height: 8px; border-radius: 999px;
  background: var(--surface-high); overflow: hidden;
}}
.prog-bar__fill {{
  height: 100%; border-radius: inherit;
  background: linear-gradient(90deg, var(--primary), var(--hero-b));
  transition: width 220ms ease;
}}
.prog-detail {{ font-size: 0.8125rem; color: var(--on-surface-variant); word-break: break-all; }}

/* Skeleton scansione */
.skel {{
  height: 82px; border-radius: 16px;
  background: linear-gradient(90deg, var(--surface-low) 25%, var(--surface-high) 50%, var(--surface-low) 75%);
  background-size: 200% 100%; animation: shimmer 1.2s infinite linear;
}}
@keyframes shimmer {{ from {{ background-position: 200% 0; }} to {{ background-position: -200% 0; }} }}

/* Hero di caricamento (passo 1 mentre si rileva il PC) */
.hero {{
  display: flex; align-items: center; gap: 18px; margin-bottom: 18px;
  padding: 18px 22px; border-radius: 20px; color: #fff;
  background:
    radial-gradient(120% 130% at 100% 0%, color-mix(in srgb, var(--hero-c) 68%, transparent) 0%, transparent 48%),
    radial-gradient(95% 115% at 0% 100%, color-mix(in srgb, var(--hero-b) 92%, transparent) 0%, transparent 62%),
    linear-gradient(145deg, var(--hero-a), var(--hero-b));
  box-shadow: 0 14px 30px -18px var(--shadow);
}}
.hero__art {{ flex: none; width: 112px; }}
.hero__art svg {{ width: 100%; height: auto; display: block; }}
.hero__t {{ margin: 0; font-size: 1.125rem; font-weight: 700; letter-spacing: -0.01em; }}
.hero__s {{ margin: 4px 0 0; font-size: 0.875rem; line-height: 1.45; color: rgba(255, 255, 255, 0.86); }}

/* Footer fisso in basso */
.foot {{
  flex: none; display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 14px 28px; border-top: 1px solid var(--outline-variant); background: var(--card);
}}
.btn {{
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  height: 40px; padding: 0 20px; border-radius: 999px; border: 0;
  font: 600 0.875rem/1 var(--font-plain); cursor: pointer;
  transition: background-color 140ms ease, box-shadow 140ms ease, opacity 140ms ease, transform 80ms ease;
}}
.btn:active:not(:disabled) {{ transform: scale(0.98); }}
.btn:disabled {{ opacity: 0.45; cursor: not-allowed; }}
.btn svg {{ width: 18px; height: 18px; }}
.btn--primary {{
  background: var(--primary); color: var(--on-primary);
  box-shadow: 0 4px 14px -4px color-mix(in srgb, var(--primary) 55%, transparent);
}}
.btn--primary:hover:not(:disabled) {{ background: var(--primary-hover); }}
.btn--outlined {{
  background: transparent; color: var(--on-surface);
  border: 1px solid var(--outline-variant);
}}
.btn--outlined:hover:not(:disabled) {{ background: var(--surface-low); }}
.btn--text {{ background: transparent; color: var(--on-surface-variant); padding: 0 14px; }}
.btn--text:hover:not(:disabled) {{ background: var(--surface-low); color: var(--on-surface); }}

@media (prefers-reduced-motion: reduce) {{
  .panel, .skel {{ animation: none; }}
  .stepper__fill, .prog-bar__fill {{ transition: none; }}
}}
</style>
</head>
<body>
<div class="shell">
  <aside class="aside">
    <div class="aside__brand">
      <span class="aside__logo">{logo_html}</span>
      <span class="aside__wordmark">PhotoScan</span>
    </div>
    <div class="aside__visual">
      <div id="aside-art" class="aside__art"></div>
      <div>
        <p id="aside-quote" class="aside__quote"></p>
        <p id="aside-sub" class="aside__sub"></p>
      </div>
    </div>
    <div class="aside__trust">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
      <span>100% locale · Originali intatti</span>
    </div>
  </aside>

  <main class="workspace">
    <header class="head">
      <span id="step-pill" class="head__pill">Passo 1 di 3</span>
      <button id="theme-btn" class="icon-btn" type="button" title="Cambia tema chiaro/scuro" aria-label="Cambia tema"></button>
    </header>

    <div class="stepper">
      <ol id="stepper-list" class="stepper__list"></ol>
      <div class="stepper__track"><div id="stepper-fill" class="stepper__fill" style="width:33.3%"></div></div>
    </div>

    <div id="body" class="body"></div>
    <footer id="foot" class="foot"></footer>
  </main>
</div>

<script>
const STEPS = ['Analisi del PC', 'Cosa scaricare', 'Installazione'];
const ASIDE = [
  {{
    quote: 'Un solo programma, su misura per il tuo PC.',
    sub: 'Rileviamo scheda video, memoria e spazio libero per proporti la configurazione più veloce.',
    art: `<svg viewBox="0 0 220 140" fill="none">
      <rect x="34" y="18" width="152" height="90" rx="16" fill="rgba(255,255,255,0.16)" stroke="rgba(255,255,255,0.38)" stroke-width="1.5"/>
      <rect x="44" y="28" width="132" height="70" rx="10" fill="rgba(255,255,255,0.94)"/>
      <circle cx="56" cy="39" r="3" fill="#e0742f"/>
      <circle cx="66" cy="39" r="3" fill="#c4b5fd"/>
      <circle cx="76" cy="39" r="3" fill="#6fdc9a"/>
      <rect x="54" y="51" width="54" height="36" rx="8" fill="#eeedff"/>
      <path d="M64 78 A17 17 0 0 1 98 78" stroke="#c9c6f8" stroke-width="5" stroke-linecap="round"/>
      <path d="M64 78 A17 17 0 0 1 93 66" stroke="#4b45d8" stroke-width="5" stroke-linecap="round"/>
      <circle cx="81" cy="77" r="4" fill="#e0742f"/>
      <rect x="118" y="54" width="46" height="7" rx="3.5" fill="#4b45d8" fill-opacity="0.85"/>
      <rect x="118" y="67" width="38" height="6" rx="3" fill="#8a5cf0" fill-opacity="0.45"/>
      <rect x="118" y="79" width="44" height="6" rx="3" fill="#1f8a4c" fill-opacity="0.55"/>
      <path d="M96 108 H124 L130 120 H90 Z" fill="rgba(255,255,255,0.28)"/>
      <rect x="78" y="120" width="64" height="5" rx="2.5" fill="rgba(255,255,255,0.48)"/>
      <circle cx="176" cy="24" r="13" fill="#6fdc9a"/>
      <path d="M170.5 24 L174.2 27.8 L181.5 20.2" stroke="#0b3a1f" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`,
  }},
  {{
    quote: 'Scegli tu cosa scaricare.',
    sub: 'Puoi usare il pacchetto consigliato oppure cambiarlo in qualsiasi momento. Il modello AI si scarica una sola volta.',
    art: `<svg viewBox="0 0 220 140" fill="none">
      <rect x="28" y="22" width="164" height="96" rx="16" fill="rgba(255,255,255,0.14)" stroke="rgba(255,255,255,0.34)" stroke-width="1.5"/>
      <circle cx="56" cy="50" r="10" fill="#6fdc9a"/>
      <path d="M52 50 L55 53 L61 46" stroke="#0b3a1f" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
      <rect x="76" y="45" width="92" height="9" rx="4.5" fill="rgba(255,255,255,0.85)"/>
      <circle cx="56" cy="78" r="10" fill="#6fdc9a"/>
      <path d="M52 78 L55 81 L61 74" stroke="#0b3a1f" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
      <rect x="76" y="73" width="76" height="9" rx="4.5" fill="rgba(255,255,255,0.68)"/>
      <rect x="46" y="100" width="128" height="6" rx="3" fill="rgba(255,255,255,0.22)"/>
      <rect x="46" y="100" width="92" height="6" rx="3" fill="#ffffff"/>
    </svg>`,
  }},
  {{
    quote: 'Quasi pronto per i tuoi ricordi.',
    sub: 'Installiamo PhotoScan in locale e creiamo l’icona sul Desktop. Nessun dato lascia il tuo computer.',
    art: `<svg viewBox="0 0 220 140" fill="none">
      <rect x="58" y="18" width="104" height="78" rx="14" fill="rgba(255,255,255,0.22)" transform="rotate(9 110 57)"/>
      <rect x="48" y="26" width="104" height="78" rx="14" fill="rgba(255,255,255,0.34)" transform="rotate(-6 100 65)"/>
      <rect x="54" y="30" width="112" height="84" rx="15" fill="rgba(255,255,255,0.96)"/>
      <circle cx="84" cy="58" r="10" fill="#e0742f"/>
      <path d="M62 106 L94 72 L116 92 L134 76 L158 106 Z" fill="#4b45d8"/>
      <circle cx="164" cy="34" r="14" fill="#ffe2d1"/>
      <path d="M164 26 L166.4 31.6 L172 34 L166.4 36.4 L164 42 L161.6 36.4 L156 34 L161.6 31.6 Z" fill="#9a4b1e"/>
    </svg>`,
  }},
];

const IC = {{
  cpu: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/></svg>`,
  ram: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 17v3M12 17v3M17 17v3M7 11h2M11 11h2M15 11h2"/></svg>`,
  gpu: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="9" cy="12" r="3"/><path d="M15 10h4M15 14h4"/></svg>`,
  disk: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.66 3.58 3 8 3s8-1.34 8-3V6"/><path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3"/></svg>`,
  ok: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M20 6L9 17l-5-5"/></svg>`,
  info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>`,
  warn: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>`,
  err: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg>`,
  sun: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32l1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41m11.32-11.32l1.41-1.41"/></svg>`,
  moon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`,
  arrowR: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`,
  arrowL: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M19 12H5M11 6l-6 6 6 6"/></svg>`,
  play: `<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="6,4 20,12 6,20"/></svg>`,
}};

const S = {{
  step: 0,
  dark: matchMedia('(prefers-color-scheme: dark)').matches,
  data: null,
  runtime: 'cpu',
  model: 'base',
  warn: null,
  install: {{ running: false, frac: 0, text: 'Preparazione…', done: false, error: null }},
  pollTimer: 0,
}};

function esc(str) {{
  return String(str ?? '').replace(/[&<>"']/g, (c) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function applyTheme(dark) {{
  S.dark = !!dark;
  document.documentElement.dataset.theme = S.dark ? 'dark' : 'light';
  document.getElementById('theme-btn').innerHTML = S.dark ? IC.sun : IC.moon;
  window.pywebview?.api?.set_theme?.(S.dark).catch(() => {{}});
}}
document.getElementById('theme-btn').addEventListener('click', () => applyTheme(!S.dark));

function renderChrome() {{
  document.getElementById('step-pill').textContent = `Passo ${{S.step + 1}} di ${{STEPS.length}}`;
  document.getElementById('stepper-fill').style.width = `${{((S.step + 1) / STEPS.length) * 100}}%`;
  const a = ASIDE[S.step];
  document.getElementById('aside-art').innerHTML = a.art;
  document.getElementById('aside-quote').textContent = a.quote;
  document.getElementById('aside-sub').textContent = a.sub;

  const list = document.getElementById('stepper-list');
  list.innerHTML = STEPS.map((label, i) => {{
    const done = i < S.step;
    const cur = i === S.step;
    const cls = done ? 'is-done' : cur ? 'is-current' : '';
    return `<li><button type="button" class="stepper__btn ${{cls}}" data-step="${{i}}" ${{done && !S.install.running ? '' : 'disabled'}}>
      <span class="stepper__dot">${{done ? '✓' : i + 1}}</span><span>${{label}}</span>
    </button></li>`;
  }}).join('');
  list.querySelectorAll('button[data-step]').forEach((b) => {{
    b.addEventListener('click', () => {{
      const t = Number(b.dataset.step);
      if (t < S.step && !S.install.running) goStep(t);
    }});
  }});
}}

function goStep(n) {{
  S.step = n;
  renderChrome();
  if (n === 0) renderScan();
  else if (n === 1) renderChoose();
  else renderInstall();
}}

function renderScan() {{
  const body = document.getElementById('body');
  const foot = document.getElementById('foot');
  if (!S.data) {{
    body.innerHTML = `<div class="panel">
      <div class="hero">
        <div class="hero__art">${{ASIDE[0].art}}</div>
        <div>
          <p class="hero__t">Benvenuto in PhotoScan</p>
          <p class="hero__s">Stiamo controllando il tuo PC per consigliarti la configurazione giusta. Ci vuole solo un momento.</p>
        </div>
      </div>
      <h1 class="title">Analisi del tuo computer</h1>
      <p class="lead">Stiamo rilevando processore, memoria RAM, scheda video NVIDIA e spazio libero su disco…</p>
      <div class="hw-grid"><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>
    </div>`;
    foot.innerHTML = `<span></span><button class="btn btn--primary" disabled>Avanti ${{IC.arrowR}}</button>`;
    return;
  }}
  const {{ hw, state, rec_runtime, rec_model, models, dest_drive }} = S.data;
  const gpuOk = !!(hw.gpu_name && hw.vram_mb >= 4000);
  const gpuVal = hw.gpu_name || 'Nessuna NVIDIA';
  const gpuSub = hw.gpu_name ? `${{(hw.vram_mb / 1024).toFixed(1)}} GB VRAM · CUDA pronta` : 'Userà il processore (CPU)';

  let banner;
  if (state) {{
    const rt = state.runtime === 'gpu' ? 'GPU NVIDIA' : 'Solo CPU';
    const mod = (models.find((m) => m.id === state.model)?.label) || state.model;
    banner = `<div class="callout callout--ok">
      <span class="callout__ic">${{IC.ok}}</span>
      <div><p class="callout__title">PhotoScan ${{esc(state.version)}} è già installato</p>
      <p class="callout__text">Configurazione attuale: <strong>${{esc(rt)}}</strong> · ${{esc(mod)}}. Nel passo successivo puoi aggiornarla o cambiarla.</p></div>
    </div>`;
  }} else {{
    const rt = rec_runtime === 'gpu' ? 'GPU NVIDIA (CUDA)' : 'Solo CPU';
    const mod = (models.find((m) => m.id === rec_model)?.label) || rec_model;
    banner = `<div class="callout callout--info">
      <span class="callout__ic">${{IC.info}}</span>
      <div><p class="callout__title">Pronto per la prima installazione</p>
      <p class="callout__text">Consigliato per questo PC: <strong>${{esc(rt)}}</strong> + ${{esc(mod)}}.</p></div>
    </div>`;
  }}

  body.innerHTML = `<div class="panel">
    <h1 class="title">Analisi del tuo computer</h1>
    <p class="lead">Ecco cosa abbiamo rilevato: useremo questi dati per consigliarti il pacchetto e il modello AI migliori.</p>
    <div class="hw-grid">
      <div class="hw-card">
        <div class="hw-card__top"><span class="hw-card__lbl">${{IC.cpu}} Processore</span></div>
        <div class="hw-card__val">${{hw.cores}} core</div>
        <div class="hw-card__sub">Elaborazione locale</div>
      </div>
      <div class="hw-card">
        <div class="hw-card__top"><span class="hw-card__lbl">${{IC.ram}} Memoria RAM</span></div>
        <div class="hw-card__val">${{Math.round(hw.ram_gb)}} GB</div>
        <div class="hw-card__sub">Sistema + modello AI</div>
      </div>
      <div class="hw-card">
        <div class="hw-card__top">
          <span class="hw-card__lbl">${{IC.gpu}} Scheda video</span>
          <span class="pill ${{gpuOk ? 'pill--ok' : 'pill--muted'}}">${{gpuOk ? 'Accelerata' : 'CPU'}}</span>
        </div>
        <div class="hw-card__val">${{esc(gpuVal)}}</div>
        <div class="hw-card__sub">${{esc(gpuSub)}}</div>
      </div>
      <div class="hw-card">
        <div class="hw-card__top"><span class="hw-card__lbl">${{IC.disk}} Spazio libero</span></div>
        <div class="hw-card__val">${{Math.round(hw.free_disk_gb)}} GB</div>
        <div class="hw-card__sub">Unità ${{esc(dest_drive)}}</div>
      </div>
    </div>
    ${{banner}}
  </div>`;

  foot.innerHTML = `<span></span><button id="btn-next" class="btn btn--primary" type="button">Avanti ${{IC.arrowR}}</button>`;
  document.getElementById('btn-next').addEventListener('click', () => goStep(1));
}}

async function updateSpaceWarning() {{
  if (!window.pywebview?.api) return;
  S.warn = await window.pywebview.api.check_space(S.runtime, S.model);
  const el = document.getElementById('space-warn');
  if (!el) return;
  if (S.warn) {{
    el.innerHTML = `<div class="callout callout--warn"><span class="callout__ic">${{IC.warn}}</span>
      <div><p class="callout__title">Spazio su disco ridotto</p><p class="callout__text">${{esc(S.warn)}}</p></div></div>`;
  }} else {{
    el.innerHTML = '';
  }}
}}

function renderChoose() {{
  const body = document.getElementById('body');
  const foot = document.getElementById('foot');
  const {{ runtimes, models, rec_runtime, rec_model, state }} = S.data;

  const rtHtml = runtimes.map((r) => {{
    const sel = S.runtime === r.id ? 'is-selected' : '';
    const bRec = r.id === rec_runtime ? `<span class="pill pill--ok">Consigliato</span>` : '';
    const bInst = state && state.runtime === r.id ? `<span class="pill pill--primary">Installato</span>` : '';
    return `<div class="choice ${{sel}}" data-rt="${{r.id}}">
      <span class="choice__radio"></span>
      <div class="choice__body">
        <div class="choice__head">
          <span class="choice__title">${{esc(r.title)}}</span>${{bRec}}${{bInst}}
          <span class="choice__size">~${{r.gb}} GB</span>
        </div>
        <p class="choice__desc">${{esc(r.desc)}}</p>
      </div>
    </div>`;
  }}).join('');

  const modHtml = models.map((m) => {{
    const sel = S.model === m.id ? 'is-selected' : '';
    const bRec = m.id === rec_model ? `<span class="pill pill--ok">Consigliato</span>` : '';
    const bCur = state && state.model === m.id ? `<span class="pill pill--primary">Attuale</span>` : '';
    return `<div class="choice ${{sel}}" data-mod="${{m.id}}">
      <span class="choice__radio"></span>
      <div class="choice__body">
        <div class="choice__head">
          <span class="choice__title">${{esc(m.label)}}</span>${{bRec}}${{bCur}}
          <span class="choice__size">~${{m.download_gb}} GB</span>
        </div>
        <p class="choice__desc">${{esc(m.desc)}}</p>
      </div>
    </div>`;
  }}).join('');

  body.innerHTML = `<div class="panel">
    <h1 class="title">Scegli cosa scaricare</h1>
    <p class="lead">Le opzioni migliori per il tuo PC sono già selezionate. Il modello AI verrà scaricato automaticamente al primo avvio.</p>
    <p class="sec-label">Programma</p>
    <div class="choices">${{rtHtml}}</div>
    <p class="sec-label">Modello di riconoscimento AI</p>
    <div class="choices">${{modHtml}}</div>
    <div id="space-warn"></div>
  </div>`;

  body.querySelectorAll('[data-rt]').forEach((el) => el.addEventListener('click', () => {{
    S.runtime = el.dataset.rt;
    body.querySelectorAll('[data-rt]').forEach((x) => x.classList.toggle('is-selected', x.dataset.rt === S.runtime));
    updateSpaceWarning();
  }}));
  body.querySelectorAll('[data-mod]').forEach((el) => el.addEventListener('click', () => {{
    S.model = el.dataset.mod;
    body.querySelectorAll('[data-mod]').forEach((x) => x.classList.toggle('is-selected', x.dataset.mod === S.model));
    updateSpaceWarning();
  }}));

  const cta = state ? 'Aggiorna e installa' : 'Installa';
  foot.innerHTML = `<button id="btn-back" class="btn btn--outlined" type="button">${{IC.arrowL}} Indietro</button>
    <button id="btn-go" class="btn btn--primary" type="button">${{cta}} ${{IC.arrowR}}</button>`;
  document.getElementById('btn-back').addEventListener('click', () => goStep(0));
  document.getElementById('btn-go').addEventListener('click', startInstall);
  updateSpaceWarning();
}}

async function startInstall() {{
  S.install = {{ running: true, frac: 0.02, text: 'Preparazione dell’installazione…', done: false, error: null }};
  goStep(2);
  await window.pywebview.api.start_install(S.runtime, S.model);
  clearInterval(S.pollTimer);
  S.pollTimer = setInterval(pollInstall, 150);
}}

async function pollInstall() {{
  const st = await window.pywebview.api.poll_install();
  S.install = st;
  updateInstallUI();
  if (!st.running) {{
    clearInterval(S.pollTimer);
    renderChrome();
  }}
}}

function renderInstall() {{
  const body = document.getElementById('body');
  const rt = S.runtime === 'gpu' ? 'GPU NVIDIA (CUDA)' : 'Solo CPU';
  const mod = (S.data.models.find((m) => m.id === S.model)?.label) || S.model;
  body.innerHTML = `<div class="panel">
    <h1 class="title">Installazione di PhotoScan</h1>
    <p class="lead">Configurazione scelta: <strong>${{esc(rt)}}</strong> · ${{esc(mod)}}</p>
    <div class="prog-card">
      <div class="prog-head">
        <span id="p-status" class="prog-status"></span>
        <span id="p-pct" class="prog-pct">0%</span>
      </div>
      <div class="prog-bar"><div id="p-fill" class="prog-bar__fill" style="width:2%"></div></div>
      <div id="p-detail" class="prog-detail">Cartella di destinazione: ${{esc(S.data.dest_app)}}</div>
    </div>
    <div id="p-result"></div>
  </div>`;
  updateInstallUI();
}}

function updateInstallUI() {{
  if (S.step !== 2) return;
  const st = S.install;
  const pct = Math.max(0, Math.min(100, Math.round((st.frac || 0) * 100)));
  document.getElementById('p-status').textContent = st.text || 'Installazione…';
  document.getElementById('p-pct').textContent = st.error ? '—' : `${{pct}}%`;
  document.getElementById('p-fill').style.width = `${{st.done ? 100 : pct}}%`;

  const res = document.getElementById('p-result');
  const foot = document.getElementById('foot');
  if (st.done) {{
    document.getElementById('p-detail').textContent = 'Collegamento creato sul Desktop. Il modello AI verrà scaricato al primo avvio.';
    res.innerHTML = `<div class="callout callout--ok"><span class="callout__ic">${{IC.ok}}</span>
      <div><p class="callout__title">PhotoScan è pronto all’uso</p>
      <p class="callout__text">Premi «Avvia PhotoScan» qui sotto oppure apri l’icona appena creata sul Desktop.</p></div></div>`;
    foot.innerHTML = `<button id="btn-close" class="btn btn--text" type="button">Chiudi</button>
      <button id="btn-launch" class="btn btn--primary" type="button">${{IC.play}} Avvia PhotoScan</button>`;
    document.getElementById('btn-close').addEventListener('click', () => window.pywebview.api.close_window());
    document.getElementById('btn-launch').addEventListener('click', () => window.pywebview.api.launch_app());
  }} else if (st.error) {{
    res.innerHTML = `<div class="callout callout--err"><span class="callout__ic">${{IC.err}}</span>
      <div><p class="callout__title">Installazione non riuscita</p>
      <p class="callout__text">${{esc(st.error)}}</p></div></div>`;
    foot.innerHTML = `<button id="btn-back2" class="btn btn--outlined" type="button">${{IC.arrowL}} Indietro</button>
      <button id="btn-retry" class="btn btn--primary" type="button">Riprova ${{IC.arrowR}}</button>`;
    document.getElementById('btn-back2').addEventListener('click', () => goStep(1));
    document.getElementById('btn-retry').addEventListener('click', startInstall);
  }} else {{
    res.innerHTML = '';
    foot.innerHTML = `<span></span><button class="btn btn--primary" disabled>${{IC.play}} Avvia PhotoScan</button>`;
  }}
}}

async function boot() {{
  applyTheme(S.dark);
  goStep(0);
  try {{
    const d = await window.pywebview.api.scan_hardware();
    S.data = d;
    S.runtime = d.rec_runtime;
    S.model = d.rec_model;
    if (S.step === 0) renderScan();
  }} catch (e) {{
    document.getElementById('body').innerHTML = `<div class="callout callout--err"><span class="callout__ic">${{IC.err}}</span>
      <div><p class="callout__title">Impossibile leggere lo stato del PC</p><p class="callout__text">${{esc(e?.message || e)}}</p></div></div>`;
  }}
}}

window.addEventListener('pywebviewready', boot);
</script>
</body>
</html>"""


# ==============================================================================
#  Bridge Python <-> WebView2
# ==============================================================================

class _SetupBridge:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.window: Any = None
        self.hw: Any = None
        self.state = installed_state(root_dir)
        self._lock = threading.Lock()
        self._install: dict[str, Any] = {
            "running": False, "frac": 0.0, "text": "Pronto", "done": False, "error": None,
        }
        self._exe: Path | None = None

    def set_theme(self, dark: bool) -> None:
        if self.window is not None:
            _set_titlebar_theme(self.window, bool(dark))

    def scan_hardware(self) -> dict[str, Any]:
        hw = detect_hardware(self.root_dir)
        self.hw = hw
        rec_rt, rec_mod = recommend(hw)
        self.state = installed_state(self.root_dir)
        return {
            "hw": {
                "cores": hw.cores,
                "ram_gb": hw.ram_gb,
                "gpu_name": hw.gpu_name,
                "vram_mb": hw.vram_mb,
                "free_disk_gb": hw.free_disk_gb,
            },
            "rec_runtime": rec_rt,
            "rec_model": rec_mod,
            "state": self.state,
            "runtimes": RUNTIME_OPTIONS,
            "models": [
                {"id": k, "label": v["label"], "download_gb": v["download_gb"], "desc": MODEL_DESC.get(k, "")}
                for k, v in MODELS.items()
            ],
            "dest_drive": self.root_dir.drive or str(self.root_dir),
            "dest_app": str(self.root_dir / "app"),
        }

    def check_space(self, runtime: str, model: str) -> str | None:
        if self.hw is None:
            return None
        return space_warning(self.hw, runtime, model)

    def start_install(self, runtime: str, model: str) -> bool:
        with self._lock:
            if self._install["running"]:
                return False
            self._install = {"running": True, "frac": 0.02, "text": "Controllo la versione più recente…", "done": False, "error": None}
        threading.Thread(target=self._run_install, args=(runtime, model), daemon=True).start()
        return True

    def poll_install(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._install)

    def _set_prog(self, frac: float, text: str) -> None:
        with self._lock:
            self._install["frac"] = float(frac)
            self._install["text"] = text

    def _run_install(self, runtime: str, model: str) -> None:
        try:
            self._set_prog(0.03, "Controllo la versione più recente…")
            release = latest_release()
            version = release["tag_name"].lstrip("vV")
            app_dir = self.root_dir / "app"
            same = (self.state and self.state.get("version") == version and self.state.get("runtime") == runtime
                    and _resolve_exe(app_dir).exists())  # install.json da solo non basta: l'app deve esserci
            if not same:
                parts = package_parts(release.get("assets", []), version, runtime)
                if not parts:
                    raise RuntimeError(f"La Release {release['tag_name']} non contiene il pacchetto «{runtime}».")
                dl = self.root_dir / "download"
                zip_path = download_parts(parts, dl, self._set_prog)
                self._set_prog(0.95, "Estrazione dei file…")
                extract(zip_path, app_dir, self._set_prog)
                shutil.rmtree(dl, ignore_errors=True)
            else:
                self._set_prog(0.85, "Versione già presente, aggiorno la configurazione…")
            write_config(self.root_dir / "config.json", MODELS[model]["id"])
            write_install_marker(self.root_dir, version, runtime, model)
            exe = _resolve_exe(app_dir)
            create_desktop_shortcut(exe)
            self.state = installed_state(self.root_dir)
            self._exe = exe
            with self._lock:
                self._install = {"running": False, "frac": 1.0, "text": "Installazione completata", "done": True, "error": None}
        except Exception as e:
            with self._lock:
                self._install = {"running": False, "frac": 0.0, "text": "Installazione non riuscita", "done": False, "error": str(e)}

    def launch_app(self) -> None:
        if self._exe:
            subprocess.Popen([str(self._exe)], cwd=str(self._exe.parent))
        if self.window is not None:
            self.window.destroy()

    def close_window(self) -> None:
        if self.window is not None:
            self.window.destroy()


def _run_webview(root_dir: Path) -> bool:
    try:
        import webview
    except ImportError:
        return False
    try:
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PhotoScan.Setup")
            except Exception:
                pass
        bridge = _SetupBridge(root_dir)
        window = webview.create_window(
            "PhotoScan — Installazione guidata",
            html=_build_html(),
            js_api=bridge,
            width=940,
            height=680,
            min_size=(820, 600),
            resizable=True,
            background_color="#f6f6f9",
        )
        bridge.window = window
        ico = _find_asset("photoscan.ico", "fotoscan/photoscan.ico")
        webview.start(icon=str(ico) if ico else None)
        return True
    except Exception:
        return False


# ==============================================================================
#  Ripiego Tkinter (usato solo se pywebview / WebView2 non è disponibile)
# ==============================================================================

def _run_tk_fallback(root_dir: Path) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root_dir.mkdir(parents=True, exist_ok=True)
    hw = detect_hardware(root_dir)
    rec_rt, rec_mod = recommend(hw)
    state = installed_state(root_dir)

    win = tk.Tk()
    win.title("PhotoScan — Installazione guidata")
    ico = _find_asset("photoscan.ico", "fotoscan/photoscan.ico")
    if ico:
        try:
            win.iconbitmap(str(ico))
        except Exception:
            pass
    win.geometry("560x500")
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=20)
    frame.pack(fill="both", expand=True)

    gpu_txt = f"{hw.gpu_name} ({hw.vram_mb // 1024} GB VRAM)" if hw.gpu_name else "nessuna GPU NVIDIA"
    ttk.Label(frame, text="PhotoScan — Installazione", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    ttk.Label(frame, text=f"CPU: {hw.cores} core · RAM: {hw.ram_gb:.0f} GB · GPU: {gpu_txt} · Liberi: {hw.free_disk_gb:.0f} GB").pack(anchor="w", pady=(4, 10))
    if state:
        ttk.Label(frame, text=f"Già installato: PhotoScan {state.get('version')} ({state.get('runtime')}, {state.get('model')})").pack(anchor="w", pady=(0, 8))

    rt_var, mod_var = tk.StringVar(value=rec_rt), tk.StringVar(value=rec_mod)
    ttk.Label(frame, text="Programma", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    for r in RUNTIME_OPTIONS:
        ttk.Radiobutton(frame, text=f"{r['title']} (~{r['gb']:g} GB)" + (" (consigliato)" if r["id"] == rec_rt else ""), value=r["id"], variable=rt_var).pack(anchor="w")

    ttk.Label(frame, text="Modello AI (scaricato al primo avvio)", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 0))
    for k, m in MODELS.items():
        ttk.Radiobutton(frame, text=f"{m['label']} (~{m['download_gb']:g} GB)" + (" (consigliato)" if k == rec_mod else ""), value=k, variable=mod_var).pack(anchor="w")

    status = ttk.Label(frame, text="Pronto.")
    status.pack(anchor="w", pady=(14, 4))
    bar = ttk.Progressbar(frame, maximum=100)
    bar.pack(fill="x")

    btns = ttk.Frame(frame)
    btns.pack(anchor="e", pady=(14, 0))
    install_btn = ttk.Button(btns, text="Installa")
    launch_btn = ttk.Button(btns, text="Avvia PhotoScan", state="disabled")
    install_btn.pack(side="left", padx=4)
    launch_btn.pack(side="left", padx=4)

    def _job() -> None:
        try:
            rt, mod = rt_var.get(), mod_var.get()
            win.after(0, lambda: status.config(text="Controllo la versione più recente…"))
            rel = latest_release()
            ver = rel["tag_name"].lstrip("vV")
            app_dir = root_dir / "app"
            if not (state and state.get("version") == ver and state.get("runtime") == rt and _resolve_exe(app_dir).exists()):
                parts = package_parts(rel.get("assets", []), ver, rt)
                if not parts:
                    raise RuntimeError(f"La Release {rel['tag_name']} non ha il pacchetto «{rt}».")
                dl = root_dir / "download"
                zp = download_parts(parts, dl, lambda f, t: win.after(0, lambda: (bar.config(value=f * 100), status.config(text=t))))
                extract(zp, app_dir, lambda f, t: win.after(0, lambda: (bar.config(value=f * 100), status.config(text=t))))
                shutil.rmtree(dl, ignore_errors=True)
            write_config(root_dir / "config.json", MODELS[mod]["id"])
            write_install_marker(root_dir, ver, rt, mod)
            exe = _resolve_exe(app_dir)
            create_desktop_shortcut(exe)
            win.after(0, lambda: (bar.config(value=100), status.config(text="Installazione completata."),
                                  launch_btn.config(state="normal", command=lambda: (subprocess.Popen([str(exe)], cwd=str(exe.parent)), win.destroy()))))
        except Exception as e:
            win.after(0, lambda msg=str(e): (install_btn.config(state="normal"), status.config(text="Errore."), messagebox.showerror("PhotoScan", msg)))

    install_btn.config(command=lambda: (install_btn.config(state="disabled"), threading.Thread(target=_job, daemon=True).start()))
    win.mainloop()
    return 0


def run_gui() -> int:
    root_dir = data_root()
    if _run_webview(root_dir):
        return 0
    return _run_tk_fallback(root_dir)
