"""Componente di calcolo (runtime) di PhotoScan per Windows: PyTorch, torchvision, transformers e le loro dipendenze.

L'app (PhotoScan.exe) non le contiene: stanno in una cartella a parte, versionata a parte, che si scarica solo
quando cambia la versione del runtime (fotoscan/runtime_version.py). Cosi' un aggiornamento normale pesa poche
centinaia di MB invece di alcuni GB.

Cartella del runtime:
    runtime.json     manifesto: versione del runtime, versione di Python, cpu/gpu, piattaforma, impronta dei file
    site/            pacchetti Python (torch, transformers, ...), aggiunta a sys.path
Dove viene cercata (nell'ordine):
    1. $FOTOSCAN_RUNTIME                           (prove e sviluppo)
    2. <cartella del programma>/_internal/runtime   (installazione completa: runtime dentro il pacchetto)
    3. <dati>/runtime/<versione>-<cpu|gpu>          (%LOCALAPPDATA%\\PhotoScan\\runtime)

activate() va chiamato PRIMA di importare torch/transformers: lo fa photoscan.py (punto d'ingresso, anche dei
sottoprocessi `-m fotoscan ...`). Solo libreria standard: questo modulo non importa nulla di pesante.
"""
from __future__ import annotations

import hashlib
import importlib.machinery
import json
import os
import shutil
import sys
from pathlib import Path

MANIFEST = "runtime.json"
SITE = "site"
_dll_handles: list = []   # os.add_dll_directory chiude la cartella quando l'oggetto viene eliminato
_active: dict = {}


def required_version() -> str:
    from .runtime_version import RUNTIME_VERSION
    return RUNTIME_VERSION


def python_tag() -> str:
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def _roots() -> tuple[Path, Path]:
    from .config import CODE_ROOT, ROOT
    return CODE_ROOT, ROOT


def runtimes_dir() -> Path:
    """Cartella delle copie scaricate del runtime (una per versione e tipo)."""
    env = os.environ.get("FOTOSCAN_RUNTIME_DIR")
    return Path(env) if env else _roots()[1] / "runtime"


def bundled_dir() -> Path:
    env = os.environ.get("FOTOSCAN_RUNTIME_BUNDLE")   # prove: finto «dentro il pacchetto»
    return Path(env) if env else _roots()[0] / "runtime"


def dir_name(version: str, flavor: str) -> str:
    return f"{version}-{flavor}"


def read_manifest(d: Path) -> dict | None:
    try:
        data = json.loads((Path(d) / MANIFEST).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def check_dir(d: Path, version: str | None = None) -> tuple[bool, str]:
    """(valido, motivo). Controllo rapido: manifesto coerente con questa app e cartella dei pacchetti presente."""
    d = Path(d)
    m = read_manifest(d)
    if not m:
        return False, f"manifesto del runtime mancante in {d}"
    want = version or required_version()
    if str(m.get("runtime_version")) != str(want):
        return False, f"versione del runtime {m.get('runtime_version')} invece della {want}"
    if m.get("python") != python_tag():
        return False, f"il runtime e' per Python {m.get('python')}, questa app usa Python {python_tag()}"
    if m.get("platform") and m["platform"] != sys.platform:
        return False, f"il runtime e' per {m['platform']}, non per {sys.platform}"
    if not (d / SITE).is_dir():
        return False, f"cartella {SITE} mancante in {d}"
    return True, ""


def tree_hash(site_dir: Path) -> str:
    """Impronta dell'elenco dei file (percorso e dimensione): scopre un'estrazione incompleta senza rileggere GB."""
    h = hashlib.sha256()
    base = Path(site_dir)
    entries = []
    for p in base.rglob("*"):
        if p.is_file():
            entries.append((p.relative_to(base).as_posix(), p.stat().st_size))
    for rel, size in sorted(entries):
        h.update(f"{rel}\0{size}\n".encode("utf-8"))
    return h.hexdigest()


def requirements_hash(path: Path) -> str:
    """Impronta di requirements-runtime.txt senza commenti, righe vuote e ordine."""
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip().lower()
        if line:
            lines.append(line)
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def preferred_flavor() -> str:
    """'gpu' o 'cpu': quello scelto in installazione (install.json), altrimenti quello del runtime gia' presente."""
    try:
        inst = json.loads((_roots()[1] / "install.json").read_text(encoding="utf-8-sig"))
        if inst.get("runtime") in ("cpu", "gpu"):
            return inst["runtime"]
    except (OSError, ValueError, AttributeError):
        pass
    m = read_manifest(bundled_dir())
    if m and m.get("flavor") in ("cpu", "gpu"):
        return m["flavor"]
    base = runtimes_dir()
    if base.is_dir():
        for d in sorted(base.iterdir(), reverse=True):
            m = read_manifest(d) if d.is_dir() else None
            if m and m.get("flavor") in ("cpu", "gpu"):
                return m["flavor"]
    return "gpu"


def external_dir(version: str, flavor: str) -> Path:
    return runtimes_dir() / dir_name(version, flavor)


def candidates(version: str | None = None) -> list[Path]:
    version = version or required_version()
    out: list[Path] = []
    env = os.environ.get("FOTOSCAN_RUNTIME")
    if env:
        out.append(Path(env))
    out.append(bundled_dir())
    first = preferred_flavor()
    for fl in (first, "cpu" if first == "gpu" else "gpu"):
        out.append(external_dir(version, fl))
    return out


def find(version: str | None = None) -> tuple[Path | None, str]:
    """(cartella valida del runtime, motivo se non trovata)."""
    reasons = []
    for d in candidates(version):
        if not d.is_dir():
            continue
        ok, why = check_dir(d, version)
        if ok:
            return d, ""
        reasons.append(why)
    return None, ("; ".join(reasons) if reasons else "runtime non installato")


def installed(version: str, flavor: str) -> Path | None:
    """Cartella gia' presente e valida per (versione, tipo): dentro il pacchetto o scaricata a parte."""
    for d in (bundled_dir(), external_dir(version, flavor)):
        if d.is_dir():
            m = read_manifest(d)
            if m and m.get("flavor") == flavor and check_dir(d, version)[0]:
                return d
    return None


class _SiteFallback:
    """Sottomoduli che l'app ha solo in parte (es. jinja2.meta, che PyInstaller non ha incluso perche' l'app non
    lo importa) si cercano anche nel runtime: stessa versione, perche' app e runtime escono dalla stessa build."""

    def __init__(self, site: Path) -> None:
        self.site = site

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001 - protocollo dei finder
        if "." not in fullname:
            return None
        parent = self.site.joinpath(*fullname.split(".")[:-1])
        if not parent.is_dir():
            return None
        return importlib.machinery.PathFinder.find_spec(fullname, [str(parent)])


def _torch_in_package() -> bool:
    """PyTorch e' dentro il pacchetto (AppImage Linux, build completa): nessun runtime a parte da cercare."""
    try:
        import importlib.util
        return importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        return False


def _development() -> bool:
    return not getattr(sys, "frozen", False) and not os.environ.get("FOTOSCAN_RUNTIME")


def activate() -> dict:
    """Mette il runtime in sys.path e nelle cartelle delle DLL. Idempotente. Ritorna lo stato (vedi status())."""
    if _active:
        return dict(_active)
    if _development():
        _active.update({"ok": True, "dir": None, "reason": "", "external": False})   # sviluppo: torch e' nel venv
        return dict(_active)
    d, why = find()
    if d is None:
        if _torch_in_package():
            _active.update({"ok": True, "dir": None, "reason": "", "external": False})
            return dict(_active)
        return {"ok": False, "dir": None, "reason": why, "external": False}
    site = d / SITE
    if str(site) not in sys.path:
        sys.path.append(str(site))           # in coda: i moduli gia' nell'app hanno la precedenza
        sys.meta_path.append(_SiteFallback(site))
    dirs = [site, site / "torch" / "lib", site / "torchvision"]
    dirs += [p for p in site.glob("*.libs") if p.is_dir()]
    existing = [p for p in dirs if p.is_dir()]
    if sys.platform == "win32":
        for p in existing:
            try:
                _dll_handles.append(os.add_dll_directory(str(p)))
            except OSError:
                pass
    os.environ["PATH"] = os.pathsep.join([str(p) for p in existing] + [os.environ.get("PATH", "")])
    os.environ["FOTOSCAN_RUNTIME_ACTIVE"] = str(d)
    _active.update({"ok": True, "dir": str(d), "reason": "", "external": d != bundled_dir()})
    return dict(_active)


def status() -> dict:
    """{ok, dir, reason, external, required, python}: stato del runtime per l'app."""
    out = {"required": required_version(), "python": python_tag()}
    if _active:
        out.update({k: _active.get(k) for k in ("ok", "dir", "reason", "external")})
    elif _development():
        out.update({"ok": True, "dir": None, "reason": "", "external": False})
    else:
        d, why = find()
        in_package = d is None and _torch_in_package()
        out.update({"ok": d is not None or in_package, "dir": str(d) if d else None, "reason": "" if in_package else why,
                    "external": bool(d) and d != bundled_dir()})
    return out


# ------------------------------------------------------------------ gestione delle cartelle
def link_tree(src: Path, dst: Path) -> None:
    """Copia src in dst usando collegamenti fisici dove possibile (stesso volume: istantaneo, nessuno spazio in piu')."""
    def _copy(a: str, b: str) -> None:
        try:
            os.link(a, b)
        except OSError:
            shutil.copy2(a, b)
    shutil.copytree(src, dst, copy_function=_copy)


def adopt_bundled(version: str, flavor: str) -> Path | None:
    """Runtime dentro il pacchetto -> cartella a parte, cosi' sopravvive alla sostituzione del pacchetto.
    Ritorna la cartella esterna (gia' presente o appena creata), None se il pacchetto non ha il runtime giusto."""
    ext = external_dir(version, flavor)
    if ext.is_dir() and check_dir(ext, version)[0]:
        return ext
    src = bundled_dir()
    m = read_manifest(src) if src.is_dir() else None
    if not m or m.get("flavor") != flavor or not check_dir(src, version)[0]:
        return None
    tmp = ext.with_name(".staging-" + ext.name)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_tree(src, tmp)
        if ext.exists():
            shutil.rmtree(ext, ignore_errors=True)
        os.replace(tmp, ext)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return ext


def prune(keep_versions: set[str]) -> list[str]:
    """Toglie dalla cartella dei runtime le versioni non piu' necessarie e gli avanzi di installazioni interrotte.
    Si chiama solo dopo un avvio riuscito: il runtime vecchio serve finche' la versione nuova non ha funzionato."""
    removed = []
    base = runtimes_dir()
    if not base.is_dir():
        return removed
    for d in base.iterdir():
        if not d.is_dir():
            continue
        ver = d.name.split("-", 1)[0]
        if d.name.startswith((".staging-", ".old-")) or ver not in keep_versions:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed


# ------------------------------------------------------------------ verifica
def self_check() -> dict:
    """Importa davvero i pacchetti pesanti: usato da `PhotoScan.exe --runtime-check` e dallo smoke test del workflow."""
    res: dict = {"runtime": activate(), "python": sys.version.split()[0], "modules": {}, "ok": True}
    for name in ("numpy", "PIL", "torch", "torchvision", "transformers", "safetensors", "tokenizers"):
        try:
            mod = __import__(name)
            info = {"version": getattr(mod, "__version__", "?"), "file": str(getattr(mod, "__file__", ""))}
            if name == "torch":
                info["cuda_build"] = bool(getattr(getattr(mod, "version", None), "cuda", None))
            res["modules"][name] = info
        except Exception as e:  # noqa: BLE001
            res["modules"][name] = {"error": f"{type(e).__name__}: {e}"}
            res["ok"] = False
    return res


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = Path(argv[0]) if argv else None
    res = self_check()
    text = json.dumps(res, indent=2, ensure_ascii=False)
    if out is not None:
        out.write_text(text, encoding="utf-8")
    elif sys.stdout is not None:
        print(text)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
