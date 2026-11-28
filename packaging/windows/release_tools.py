"""Strumenti di build della Release di Windows (usati da .github/workflows/release.yml, provabili in locale).

  runtime-build  --site <site-packages> --out <cartella> --flavor cpu|gpu
        Costruisce <out>/runtime/{runtime.json,site/}: i pacchetti di requirements-runtime.txt con tutte le loro
        dipendenze, copiati da un venv gia' pronto (stessa versione di Python dell'app).
  assemble-full  --app <dist/PhotoScan> --runtime <cartella runtime> --out <cartella>
        Pacchetto completo (compatibile con gli updater <= 0.1.10): l'app leggera + il runtime in _internal/runtime.
  zip-split      --src <cartella> --top <nome> --out-dir <cartella> --name <PhotoScan-...zip>
        Zip con una sola cartella radice <top>, spezzato in parti da 1900 MiB (limite di 2 GiB per asset).
  manifest       --version <v> --out <file>
        release-manifest.json: versione dell'app e del runtime che richiede (letto dall'updater e dal Setup).
  stdlib-scan    --site <cartella site del runtime> [--check]
        Moduli della libreria standard che il runtime importa e che l'app leggera deve quindi portare con se'
        (PyInstaller include dello stdlib solo cio' che l'app importa): aggiorna packaging/windows/runtime_stdlib.txt.
  sums           --dir <cartella> [--out SHA256SUMS]
        SHA-256 di tutti i file della cartella, formato `<hash>  <nome>`.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import json
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from fotoscan import runtime as rt  # noqa: E402
from fotoscan import runtime_version as rv  # noqa: E402

CHUNK = 1900 * 1024 * 1024


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_roots() -> list[str]:
    roots = []
    for raw in (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            roots.append(canon(re.split(r"[=<>!~\[; ]", line, maxsplit=1)[0]))
    return roots


def closure(site: Path, roots: list[str]) -> dict[str, md.Distribution]:
    from packaging.requirements import Requirement

    dists = {canon(d.metadata["Name"]): d for d in md.distributions(path=[str(site)])}
    out: dict[str, md.Distribution] = {}
    missing: list[str] = []
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in out:
            continue
        d = dists.get(name)
        if d is None:
            missing.append(name)
            continue
        out[name] = d
        for line in d.requires or []:
            req = Requirement(line)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            stack.append(canon(req.name))
    if missing:
        raise SystemExit(f"pacchetti richiesti ma non installati in {site}: {', '.join(sorted(set(missing)))}")
    return out


def cmd_runtime_build(a: argparse.Namespace) -> None:
    if rv.PYTHON_TAG != rt.python_tag():
        raise SystemExit(f"Python {rt.python_tag()} ma il runtime e' definito per {rv.PYTHON_TAG} (runtime_version.py)")
    if rt.requirements_hash(ROOT / "requirements-runtime.txt") != rv.REQUIREMENTS_SHA256:
        raise SystemExit("requirements-runtime.txt e' cambiato senza aggiornare fotoscan/runtime_version.py")
    site = Path(a.site).resolve()
    out = Path(a.out).resolve() / "runtime"
    shutil.rmtree(out, ignore_errors=True)
    dest = out / rt.SITE
    dest.mkdir(parents=True)
    shared = {canon(n) for n in rv.SHARED_WITH_APP}
    dists = closure(site, runtime_roots())
    packages: dict[str, str] = {}
    for name, d in sorted(dists.items()):
        if name in shared:
            continue
        packages[name] = d.version
        for f in d.files or []:
            rel = Path(str(f))
            if ".." in rel.parts or rel.is_absolute() or "__pycache__" in rel.parts or rel.suffix == ".pyc":
                continue
            src = site / rel
            if not src.is_file():
                continue
            tgt = dest / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tgt)
    files = [p for p in dest.rglob("*") if p.is_file()]
    manifest = {
        "runtime_version": rv.RUNTIME_VERSION, "python": rt.python_tag(), "flavor": a.flavor, "platform": sys.platform,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": len(files), "bytes": sum(p.stat().st_size for p in files),
        "tree_sha256": rt.tree_hash(dest), "packages": packages,
    }
    (out / rt.MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")   # per ultimo: cartella completa
    print(f"runtime {rv.RUNTIME_VERSION} ({a.flavor}): {len(packages)} pacchetti, {len(files)} file, "
          f"{manifest['bytes'] / 1e9:.2f} GB -> {out}")


def cmd_assemble_full(a: argparse.Namespace) -> None:
    out = Path(a.out).resolve()
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(a.app, out)
    shutil.copytree(a.runtime, out / "_internal" / "runtime")
    print(f"pacchetto completo in {out}")


def cmd_zip_split(a: argparse.Namespace) -> None:
    src, out_dir = Path(a.src).resolve(), Path(a.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    whole = out_dir / a.name
    with zipfile.ZipFile(whole, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        files = sorted(p for p in src.rglob("*") if p.is_file())
        manifest_last = [p for p in files if p.name == rt.MANIFEST and p.parent == src]
        for p in [p for p in files if p not in manifest_last] + manifest_last:
            zf.write(p, Path(a.top) / p.relative_to(src))
    size = whole.stat().st_size
    if size <= CHUNK:
        print(f"{whole.name}: {size / 1e6:.0f} MB")
        return
    with whole.open("rb") as f:
        idx = 1
        while True:
            part = out_dir / f"{a.name}.{idx:03d}"
            written = 0
            with part.open("wb") as pf:
                while written < CHUNK:
                    b = f.read(min(16 * 1024 * 1024, CHUNK - written))
                    if not b:
                        break
                    pf.write(b)
                    written += len(b)
            if written == 0:
                part.unlink()
                break
            print(f"{part.name}: {written / 1e6:.0f} MB")
            idx += 1
    whole.unlink()


STDLIB_LIST = ROOT / "packaging" / "windows" / "runtime_stdlib.txt"
_STDLIB_SKIP = {"tkinter", "idlelib", "turtle", "turtledemo", "test", "lib2to3", "this", "antigravity", "__main__",
                "distutils", "ensurepip", "venv", "pydoc_data"}


def scan_stdlib(site: Path) -> set[str]:
    """Moduli dello stdlib importati (anche in modo condizionale) dai sorgenti del runtime."""
    import ast
    std = set(sys.stdlib_module_names)
    found: set[str] = set()
    for f in site.rglob("*.py"):
        try:
            tree = ast.parse(f.read_bytes())
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for n in names:
                top = n.split(".")[0]
                if top in std and top not in _STDLIB_SKIP:
                    found.add(n)
    return found


def cmd_stdlib_scan(a: argparse.Namespace) -> None:
    found = scan_stdlib(Path(a.site))
    current = set(STDLIB_LIST.read_text(encoding="utf-8").split()) if STDLIB_LIST.exists() else set()
    if a.check:
        missing = sorted(found - current)
        if missing:
            raise SystemExit("moduli stdlib usati dal runtime ma non in runtime_stdlib.txt (rigenera con stdlib-scan): "
                             + ", ".join(missing))
        print(f"runtime_stdlib.txt completo ({len(current)} moduli)")
        return
    STDLIB_LIST.write_text("\n".join(sorted(found | current)) + "\n", encoding="utf-8")
    print(f"{len(found | current)} moduli stdlib -> {STDLIB_LIST}")


def cmd_manifest(a: argparse.Namespace) -> None:
    data = {"version": a.version, "runtime_version": rv.RUNTIME_VERSION, "python": rv.PYTHON_TAG,
            "requirements_sha256": rv.REQUIREMENTS_SHA256}
    Path(a.out).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps(data))


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(16 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def cmd_sums(a: argparse.Namespace) -> None:
    d = Path(a.dir)
    out = Path(a.out) if a.out else d / "SHA256SUMS"
    lines = [f"{sha256_file(p)}  {p.name}" for p in sorted(d.iterdir()) if p.is_file() and p.name != out.name]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("runtime-build")
    p.add_argument("--site", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--flavor", choices=("cpu", "gpu"), required=True)
    p.set_defaults(fn=cmd_runtime_build)
    p = sub.add_parser("assemble-full")
    p.add_argument("--app", required=True)
    p.add_argument("--runtime", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_assemble_full)
    p = sub.add_parser("zip-split")
    p.add_argument("--src", required=True)
    p.add_argument("--top", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(fn=cmd_zip_split)
    p = sub.add_parser("manifest")
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_manifest)
    p = sub.add_parser("stdlib-scan")
    p.add_argument("--site", required=True)
    p.add_argument("--check", action="store_true")
    p.set_defaults(fn=cmd_stdlib_scan)
    p = sub.add_parser("sums")
    p.add_argument("--dir", required=True)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_sums)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
