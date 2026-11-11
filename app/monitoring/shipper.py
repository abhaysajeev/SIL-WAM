"""Sending captured logs to the central log server, off every request path.

Ported from byky-f's apps/monitoring/shipper.py:

- Its own daemon thread and its own bounded queue, per process. A slow or dead
  log server can stall only this thread, never a request.
- offer() is a put_nowait: full means the log is dropped and counted.
- A batch that fails is retried once, then dropped. Nothing is retried forever,
  and nothing here raises into its caller.
- A batch is at most 100 logs and BATCH_BYTES, inside the log server's limits.
- Off unless LOG_INGEST_URL and LOG_INGEST_KEY are both set.
"""
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request

from app.core.config import settings

log = logging.getLogger("app.monitoring")

MAX_LOGS_PER_BATCH = 100              # the log server's limit
BATCH_BYTES = 4 * 1024 * 1024         # under the log server's 8 MB body limit
RETRY_DELAY_SECONDS = 2.0

_lock = threading.Lock()
_queue: queue.Queue | None = None
_thread: threading.Thread | None = None
_pid: int | None = None
_dropped = 0
_last_warning = 0.0


def enabled() -> bool:
    return bool(settings.LOG_INGEST_URL and settings.LOG_INGEST_KEY)


def offer(document: dict) -> None:
    """Hand one document over for sending. Never raises, never blocks."""
    if not enabled():
        return
    try:
        _ensure_started()
        _queue.put_nowait(document)
    except queue.Full:
        _note_drop()
    except Exception:                       # noqa: BLE001 -- shipping must never break a request
        _warn("could not queue a log for the log server")


def _ensure_started() -> None:
    global _queue, _thread, _pid
    if _thread is not None and _pid == os.getpid() and _thread.is_alive():
        return
    with _lock:
        if _thread is not None and _pid == os.getpid() and _thread.is_alive():
            return
        _queue = queue.Queue(maxsize=settings.LOG_INGEST_QUEUE_SIZE)
        _pid = os.getpid()
        # The thread keeps its own queue: it never picks up a later one.
        _thread = threading.Thread(target=_run, args=(_queue,), name="log-shipper", daemon=True)
        _thread.start()


def _run(source: queue.Queue) -> None:
    while True:
        batch = _take(source, timeout=1.0)
        if batch:
            ship(batch)


def _take(source: queue.Queue, timeout: float) -> list:
    try:
        batch = [source.get(timeout=timeout)]
    except queue.Empty:
        return []
    while len(batch) < MAX_LOGS_PER_BATCH * 5:
        try:
            batch.append(source.get_nowait())
        except queue.Empty:
            break
    return batch


def ship(documents: list) -> None:
    """Post documents in size-capped batches. Never raises."""
    try:
        for chunk in chunks(documents):
            _send(chunk)
    except Exception:                       # noqa: BLE001
        _warn(f"could not ship {len(documents)} log(s)")


def chunks(documents: list):
    """Batches of at most 100 documents and BATCH_BYTES of JSON."""
    chunk, size = [], 2
    for doc in documents:
        doc_size = len(json.dumps(doc, default=str).encode()) + 1
        if chunk and (len(chunk) >= MAX_LOGS_PER_BATCH or size + doc_size > BATCH_BYTES):
            yield chunk
            chunk, size = [], 2
        chunk.append(doc)
        size += doc_size
    if chunk:
        yield chunk


def _send(chunk: list) -> None:
    body = json.dumps({"logs": chunk}, default=str).encode()
    for attempt in (1, 2):
        try:
            _post(body)
            return
        except Exception:                   # noqa: BLE001
            if attempt == 1:
                time.sleep(RETRY_DELAY_SECONDS)
    _note_drop(len(chunk), "the log server did not take a batch")


def _post(body: bytes) -> None:
    request = urllib.request.Request(
        settings.LOG_INGEST_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": settings.LOG_INGEST_KEY},
    )
    with urllib.request.urlopen(request, timeout=settings.LOG_INGEST_TIMEOUT) as response:
        if response.status >= 300:
            raise urllib.error.HTTPError(settings.LOG_INGEST_URL, response.status, "rejected", None, None)


def _note_drop(count: int = 1, why: str = "log shipping queue full") -> None:
    global _dropped
    _dropped += count
    _warn(f"{why}: {_dropped} log(s) not sent to the log server so far")


def _warn(message: str) -> None:
    """At most one console line a minute, whatever the failure."""
    global _last_warning
    now = time.monotonic()
    if now - _last_warning >= 60:
        _last_warning = now
        log.warning(message, exc_info=True)
