"""Job in background (piano, applicazione, indicizzazione) + bus eventi per SSE."""
from __future__ import annotations

import asyncio
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass
class Job:
    id: str
    name: str
    state: str = "running"  # running | done | error | cancelled
    stage: str = ""
    done: int = 0
    total: int = 0
    message: str = ""
    result: Any = None
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    cancel_requested: bool = False

    def update(self, stage: str | None = None, done: int | None = None, total: int | None = None, message: str | None = None):
        if stage is not None:
            self.stage = stage
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total
        if message is not None:
            self.message = message
        bus.publish("job", self.public())

    def public(self) -> dict:
        d = asdict(self)
        d["elapsed"] = (self.finished or time.time()) - self.started
        return d


class EventBus:
    """Pub/sub thread-safe -> code asyncio (una per client SSE)."""

    def __init__(self):
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(l, x) for l, x in self._subs if x is not q]

    def publish(self, type_: str, data: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            def put(q=q):
                try:
                    q.put_nowait((type_, data))
                except asyncio.QueueFull:
                    pass
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:
                pass


bus = EventBus()
_jobs: dict[str, Job] = {}
_jlock = threading.Lock()
KEEP_FINISHED = 50  # job conclusi tenuti in memoria (per /api/jobs e per chi chiede l'esito); i piu' vecchi escono


def _prune_locked() -> None:
    done = sorted((j for j in _jobs.values() if j.state != "running"), key=lambda j: j.finished or j.started)
    for j in done[:-KEEP_FINISHED] if len(done) > KEEP_FINISHED else []:
        _jobs.pop(j.id, None)


def start_job(name: str, fn: Callable[[Job], Any]) -> Job:
    """Esegue fn(job) in un thread. fn aggiorna job.update(...), controlla job.cancel_requested, ritorna il risultato."""
    job = Job(id=uuid.uuid4().hex[:12], name=name)
    with _jlock:
        _prune_locked()
        _jobs[job.id] = job

    def run():
        try:
            job.result = fn(job)
            job.state = "cancelled" if job.cancel_requested else "done"
        except Exception as e:  # noqa: BLE001
            job.state, job.error = "error", f"{type(e).__name__}: {e}"
            traceback.print_exc()
        job.finished = time.time()
        bus.publish("job", job.public())

    threading.Thread(target=run, daemon=True, name=f"job-{name}").start()
    bus.publish("job", job.public())
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def list_jobs() -> list[Job]:
    with _jlock:
        items = list(_jobs.values())
    return sorted(items, key=lambda j: -j.started)[:50]


def running_job(name: str) -> Job | None:
    with _jlock:
        items = list(_jobs.values())
    for j in items:
        if j.name == name and j.state == "running":
            return j
    return None
