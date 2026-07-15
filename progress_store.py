"""
progress_store.py — thread-safe, in-memory live progress tracking for a
running clean+validate pipeline call.

Why this exists: the pipeline runs on a worker thread (via
run_in_executor), while the browser needs live updates on the main event
loop. This module is the shared, lock-protected state between them —
cleaning_engine.py / validation_engine.py report real step start/end events
into it as they execute — from the worker thread actually doing the work.
routes_clean.py exposes it as a Server-Sent Events stream
(/api/clean/progress-stream/{file_id}): the server PUSHES an update the
instant something real happens, instead of the browser repeatedly asking
"are you done yet?" on a timer. One connection per run, zero requests while
nothing has changed.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable, Optional

_LOCK = threading.Lock()
_STORE: dict[str, dict[str, Any]] = {}

# Finished entries older than this are swept opportunistically on the next
# new() call, so a long-running server doesn't accumulate stale entries for
# files that were cleaned once and never revisited.
_MAX_AGE_SECONDS = 900


def new(file_id: str) -> None:
    """Start tracking a fresh pipeline run for *file_id* (overwrites any
    previous entry for the same id — a new run supersedes it)."""
    with _LOCK:
        # Capture the running event loop so worker-thread event() calls can
        # safely wake up any SSE stream subscribed to this run (asyncio
        # queues aren't thread-safe to touch directly from another thread —
        # call_soon_threadsafe is the correct way to cross that boundary).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        _STORE[file_id] = {
            "current": None,             # {"type", "key"?, "cond"?} | None
            "current_started_at": None,  # time.time() when current started
            "done": [],                  # [{"type","key"?/"cond"?,"seconds"}]
            "started_at": time.time(),
            "finished": False,
            "error": None,
            "_loop": loop,
            "_queues": [],   # subscribed asyncio.Queue objects (SSE streams)
        }
        cutoff = time.time() - _MAX_AGE_SECONDS
        stale = [
            k for k, v in _STORE.items()
            if v.get("finished") and v.get("started_at", 0) < cutoff
        ]
        for k in stale:
            _STORE.pop(k, None)


def _snapshot_locked(st: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    elapsed_current = (
        round(now - st["current_started_at"], 3)
        if (st["current"] is not None and st["current_started_at"] is not None)
        else 0.0
    )
    return {
        "current":         st["current"],
        "done":            list(st["done"]),
        "elapsed_current": elapsed_current,
        "finished":        st["finished"],
        "error":           st["error"],
        "known":           True,
    }


def _push(st: dict[str, Any]) -> None:
    """Wake every SSE stream subscribed to this run with the fresh state —
    called from whichever thread reported the event (usually the cleaning
    worker thread), so it hops back onto the owning event loop safely."""
    loop = st.get("_loop")
    queues = st.get("_queues") or []
    if not loop or not queues:
        return
    snap = _snapshot_locked(st)
    for q in list(queues):
        try:
            loop.call_soon_threadsafe(q.put_nowait, snap)
        except Exception:
            pass


def event(
    file_id: str,
    kind: str,
    action: str,
    *,
    key: Optional[str] = None,
    cond: Optional[str] = None,
    seconds: Optional[float] = None,
) -> None:
    """Report a real step boundary. action is "start" or "end".

    kind: "stage" (write — whole-pipeline phases with no sub-steps),
    "clean_step" (a named cleaning step, keyed like the frontend's
    PIPELINE_STEP_CATALOG — regex/trim/null/special/cnic/...), or "filter"
    (one user-configured validation filter, identified by its cond).
    """
    with _LOCK:
        st = _STORE.get(file_id)
        if st is None:
            return
        now = time.time()
        identity: dict[str, Any] = {"type": kind}
        if key is not None:
            identity["key"] = key
        if cond is not None:
            identity["cond"] = cond

        if action == "start":
            # Defensively close out whatever was "current" if the caller
            # started a new step without ending the previous one.
            if st["current"] is not None and st["current_started_at"] is not None:
                prev = dict(st["current"])
                prev["seconds"] = round(now - st["current_started_at"], 3)
                st["done"].append(prev)
            st["current"] = identity
            st["current_started_at"] = now
        elif action == "end":
            entry = dict(identity)
            entry["seconds"] = round(
                seconds if seconds is not None
                else (now - (st["current_started_at"] or now)),
                3,
            )
            st["done"].append(entry)
            cur = st["current"]
            if cur is not None and cur.get("key") == key and cur.get("cond") == cond:
                st["current"] = None
                st["current_started_at"] = None

        _push(st)


def finish(file_id: str, error: Optional[str] = None) -> None:
    with _LOCK:
        st = _STORE.get(file_id)
        if st is None:
            return
        if st["current"] is not None and st["current_started_at"] is not None:
            prev = dict(st["current"])
            prev["seconds"] = round(time.time() - st["current_started_at"], 3)
            st["done"].append(prev)
        st["current"] = None
        st["finished"] = True
        st["error"] = error
        _push(st)


def snapshot(file_id: str) -> dict[str, Any]:
    """Read-only one-shot view (used for a plain GET fallback and for the
    SSE stream's opening message)."""
    with _LOCK:
        st = _STORE.get(file_id)
        if st is None:
            return {
                "current": None, "done": [], "elapsed_current": 0.0,
                "finished": False, "error": None, "known": False,
            }
        return _snapshot_locked(st)


def subscribe(file_id: str) -> Optional["asyncio.Queue"]:
    """Register a new SSE stream's queue against a run. Returns None if the
    run isn't known yet (caller should fall back to a single snapshot)."""
    with _LOCK:
        st = _STORE.get(file_id)
        if st is None:
            return None
        q: asyncio.Queue = asyncio.Queue()
        st["_queues"].append(q)
        return q


def snapshot_and_subscribe(file_id: str) -> tuple[dict[str, Any], Optional["asyncio.Queue"]]:
    """Atomic combination of snapshot() + subscribe() — avoids a race where
    an event lands in the gap between reading the current state and
    registering to hear about future ones (which would otherwise be missed
    by both the snapshot and the new subscription)."""
    with _LOCK:
        st = _STORE.get(file_id)
        if st is None:
            return (
                {"current": None, "done": [], "elapsed_current": 0.0,
                 "finished": False, "error": None, "known": False},
                None,
            )
        snap = _snapshot_locked(st)
        q: Optional[asyncio.Queue] = None
        if not st["finished"]:
            q = asyncio.Queue()
            st["_queues"].append(q)
        return snap, q


def unsubscribe(file_id: str, q: "asyncio.Queue") -> None:
    with _LOCK:
        st = _STORE.get(file_id)
        if st is not None:
            try:
                st["_queues"].remove(q)
            except ValueError:
                pass


def clean_step_cb(file_id: str) -> Callable[..., None]:
    """Build the callback cleaning_engine.py's _timed()/nested timers call."""
    def cb(action: str, key: str, seconds: Optional[float] = None) -> None:
        event(file_id, "clean_step", action, key=key, seconds=seconds)
    return cb


def filter_cb(file_id: str) -> Callable[..., None]:
    """Build the callback validation_engine.py's per-filter loop calls."""
    def cb(action: str, cond: str, seconds: Optional[float] = None) -> None:
        event(file_id, "filter", action, cond=cond, seconds=seconds)
    return cb
