"""
In-process fan-out of sync events to agents' open connections (GET /agent-api/v1/events).

Production runs ONE uvicorn worker, so a dict of queues is enough. Everything goes
through publish()/subscribe(), so moving to Postgres LISTEN/NOTIFY for several workers is
a change to this file only.

publish() is called from a scheduler thread; each subscriber's queue belongs to the
event loop serving its connection, so items are handed over with call_soon_threadsafe.
"""
import asyncio
import threading
from collections import defaultdict

_lock = threading.Lock()
_subs: dict[str, set] = defaultdict(set)      # agent_id → {(loop, queue)}

MAX_QUEUE = 200


def subscribe(agent_id: str):
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
    entry = (loop, q)
    with _lock:
        _subs[str(agent_id)].add(entry)
    return entry


def unsubscribe(agent_id: str, entry) -> None:
    with _lock:
        subs = _subs.get(str(agent_id))
        if subs:
            subs.discard(entry)
            if not subs:
                _subs.pop(str(agent_id), None)


def _put(q: asyncio.Queue, event: dict) -> None:
    if q.full():
        # A connection that stopped reading: drop the oldest. Losing one is harmless,
        # the next event (or the app's own sync) carries the latest rev anyway.
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(event)


def publish(agent_id: str, event: dict) -> int:
    """Deliver to every open connection of this agent. Returns how many."""
    with _lock:
        targets = list(_subs.get(str(agent_id), ()))
    for loop, q in targets:
        try:
            loop.call_soon_threadsafe(_put, q, event)
        except RuntimeError:
            pass        # loop closed: the connection is gone
    return len(targets)


def connected(agent_id: str) -> int:
    with _lock:
        return len(_subs.get(str(agent_id), ()))
