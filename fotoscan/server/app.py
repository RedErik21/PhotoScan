"""Applicazione FastAPI: scopre le route in routes_*.py, serve il frontend statico."""
from __future__ import annotations

import asyncio
import importlib
import json
import pkgutil
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import jobs
from ..config import CODE_ROOT
from ..version import __version__

FRONTEND = CODE_ROOT / "frontend"
core = APIRouter(prefix="/api", tags=["core"])


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
    """Frontend sempre fresco (sviluppo/app locale); vendor/ e fonts/ possono essere in cache."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        cacheable = path.startswith("vendor/")
        resp.headers["Cache-Control"] = "public, max-age=86400" if cacheable else "no-cache"
        return resp


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
    async def on_error(_, exc: Exception):  # noqa: ANN001
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    if FRONTEND.is_dir():
        app.mount("/", NoCacheStatic(directory=str(FRONTEND), html=True), name="frontend")
    return app


app = create_app()
