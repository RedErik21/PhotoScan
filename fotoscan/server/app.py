"""Applicazione FastAPI: scopre le route in routes_*.py, serve il frontend statico."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import os
import pkgutil
import re
import threading
import time
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import jobs
from ..config import CODE_ROOT
from ..version import __version__

FRONTEND = CODE_ROOT / "frontend"
core = APIRouter(prefix="/api", tags=["core"])
log = logging.getLogger("fotoscan")
VERSIONED = "/_v"  # /_v/<impronta>/js/main.js: stesso file di /js/main.js, in cache per sempre


@core.get("/health")
def health():
    return {"ok": True}


@core.get("/jobs")
def jobs_list():
    return [j.public() for j in jobs.list_jobs()]


@core.get("/jobs/{job_id}")
def job_get(job_id: str):
    j = jobs.get_job(job_id)
    if not j:
        raise HTTPException(404, "job non trovato")
    return j.public()


@core.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    j = jobs.get_job(job_id)
    if not j:
        raise HTTPException(404, "job non trovato")
    j.cancel_requested = True
    return {"ok": True}


@core.get("/events")
async def events():
    """Server-Sent Events. Eventi: `job` (Job.public()), `index` (stato indicizzazione), `hello`."""
    q = jobs.bus.subscribe()

    async def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    type_, data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {type_}\ndata: {json.dumps(data, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            jobs.bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class NoCacheStatic(StaticFiles):
    """Percorsi non versionati: sempre rivalidati (vendor/ in cache un giorno)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        cacheable = path.startswith("vendor/")
        resp.headers["Cache-Control"] = "public, max-age=86400" if cacheable else "no-cache"
        return resp


class VersionedStatic(StaticFiles):
    """/_v/<impronta>/...: l'impronta cambia con qualunque file del frontend, quindi il contenuto di un URL
    non cambia mai e il browser lo tiene in cache senza rivalidare (prima: una richiesta per file a ogni avvio)."""

    async def get_response(self, path, scope):
        _token, _, rest = path.replace("\\", "/").partition("/")
        resp = await super().get_response(rest or "index.html", scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


_token_lock = threading.Lock()
_token_cache: tuple[float, str] | None = None
TOKEN_TTL_S = 2.0
# href/src relativi di <link> e <script> in index.html (non assoluti, non ancore, non data:)
_ASSET = re.compile(r'(<(?:link|script)\b[^>]*?\b(?:href|src)=")(?![a-z]+:|/|#)([^"]+")', re.IGNORECASE)


def frontend_token() -> str:
    """Impronta di versione + data e dimensione di ogni file del frontend (solo stat, ricalcolata ogni 2 s):
    in sviluppo un file salvato cambia subito l'impronta, nell'app installata resta fissa."""
    global _token_cache
    now = time.monotonic()
    with _token_lock:
        if _token_cache and now - _token_cache[0] < TOKEN_TTL_S:
            return _token_cache[1]
    h = hashlib.sha1(__version__.encode())
    for dirpath, dirs, files in os.walk(FRONTEND):
        dirs.sort()
        for name in sorted(files):
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            h.update(f"{dirpath}/{name}:{st.st_mtime_ns}:{st.st_size};".encode("utf-8", "replace"))
    tok = h.hexdigest()[:12]
    with _token_lock:
        _token_cache = (now, tok)
    return tok


def index_html() -> HTMLResponse:
    """index.html con css e js sul prefisso versionato /_v/<impronta>/: gli import relativi dei moduli e
    import.meta.url ereditano il prefisso, quindi tutto il codice resta in cache finche' il frontend non
    cambia. Niente <base>: i link a hash (#/pagina) devono restare sulla pagina corrente."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    prefix = f"{VERSIONED}/{frontend_token()}/"
    html = _ASSET.sub(lambda m: m.group(1) + prefix + m.group(2), html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def create_app() -> FastAPI:
    app = FastAPI(title="PhotoScan", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.include_router(core)

    # scoperta automatica: ogni fotoscan/server/routes_*.py espone `router`
    pkg = importlib.import_module("fotoscan.server")
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name.startswith("routes_"):
            mod = importlib.import_module(f"fotoscan.server.{m.name}")
            r = getattr(mod, "router", None)
            if r is not None:
                app.include_router(r)

    @app.exception_handler(Exception)
    async def on_error(request, exc: Exception):  # noqa: ANN001
        # il dettaglio (tipo, messaggio, percorsi) va nel registro, non al client
        log.error("errore non gestito su %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"detail": "Errore interno. I dettagli sono nel registro dell'app."}, status_code=500)

    if FRONTEND.is_dir():
        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        def _index():
            return index_html()

        app.mount(VERSIONED, VersionedStatic(directory=str(FRONTEND)), name="frontend-v")
        app.mount("/", NoCacheStatic(directory=str(FRONTEND), html=True), name="frontend")
    return app


app = create_app()
