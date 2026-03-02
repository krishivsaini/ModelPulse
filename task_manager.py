"""
task_manager.py — Async task orchestration layer for ModelPulse.

WHY THIS FILE EXISTS (interview framing):
MUIOGO models take 15–20 minutes.  If a Flask route called run_model() directly,
the HTTP request would hang for 20 minutes — the browser would time out, the
user would see a spinner, and no other requests could be served by that worker.
This module exists to break that synchronous coupling: it accepts work, hands
it to a background thread, and returns a task_id in milliseconds.  The client
polls for progress.  This is the same pattern used by AWS Batch, Celery, and
Google Cloud Tasks — just implemented at a smaller scale suitable for a demo.

WHY ThreadPoolExecutor (not ProcessPoolExecutor, not Celery, not asyncio):
──────────────────────────────────────────────────────────────────────────
If an interviewer asks "why threads?", here's the full answer:

1. Our model is I/O-bound (time.sleep simulates waiting for solver iterations).
   Python's GIL blocks CPU-bound threads from running in parallel, but it
   *releases* during I/O operations (sleep, network, disk).  So threads give
   us real concurrency here.

2. ProcessPoolExecutor would work, but every function argument and return value
   must be pickled and sent across process boundaries.  Our progress callback
   (a closure) can't be pickled at all — it would crash.  We'd have to replace
   callbacks with multiprocessing.Queue, adding complexity for zero benefit.

3. Celery is production-grade but needs Redis or RabbitMQ as a broker — that's
   infrastructure overhead that's wrong for a demo.  If MUIOGO scales this to
   production, Celery is the natural upgrade path.

4. asyncio would require rewriting the entire app (async def routes, await
   everywhere, an async-compatible web server like uvicorn).  For a Flask demo,
   ThreadPoolExecutor is the right tool.

WHY max_workers=4:
──────────────────
Each worker holds one running simulation.  4 lets a user compare up to 4
scenarios side-by-side.  More than 4 on a demo machine risks memory pressure
with real solvers.  In production you'd set this from an environment variable
tied to your deployment's resource budget.
"""

import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, Optional

from mock_model import run_model


# ---------------------------------------------------------------------------
# Module-level executor and task store
# ---------------------------------------------------------------------------

_executor = ThreadPoolExecutor(max_workers=4)

# WHY A PLAIN DICT + LOCK (not a database, not Redis):
# For this demo, tasks only need to survive the lifetime of the process.  A
# Python dict is the simplest in-memory store — O(1) reads, zero dependencies.
# The tradeoff: if the server crashes, all task state is lost.  In production,
# you'd persist task state to PostgreSQL or Redis so a restarted server could
# resume reporting on in-flight tasks.
#
# WHY THE LOCK IS NECESSARY:
# This dict is read by the Flask request-handling thread (when the client polls
# GET /tasks/<id>) and written by background executor threads (via the update
# callback).  Without a lock, a reader could see a *torn write*: for example,
# `status` set to "completed" but `results` still None.  The Flask route would
# then return {"status": "completed", "results": null} and the front-end would
# crash trying to render null results.  The lock guarantees that every read
# sees a fully-consistent snapshot.
#
# WHY threading.Lock (not RLock, not asyncio.Lock):
# Lock is the simplest mutex.  We don't need RLock (reentrant) because no
# codepath acquires the lock twice in the same call stack.  asyncio.Lock is
# for async code — we're using threads, so threading.Lock is the correct
# primitive.
_tasks: Dict[str, Dict] = {}
_tasks_lock = threading.Lock()


def _blank_task(task_id: str, scenario: str) -> Dict:
    """
    Return the canonical initial state for a task entry.

    WHY A DEDICATED FUNCTION (not an inline dict):
    The task shape is the implicit API contract between the backend and the
    front-end.  Having it in one place means:
      - Every task starts in an identical, predictable state
      - If we add a new field, we change it in one place, not five
      - Tests can assert against a known shape without duplicating it
    """
    return {
        "task_id": task_id,
        "status": "queued",
        "scenario": scenario,
        "iteration": 0,
        "max_iterations": 0,
        "epsilon": None,
        "progress_pct": 0.0,
        "epsilon_history": [],
        "results": None,
        "error": None,
        # WHY start_time IS RECORDED HERE (not when the model starts running):
        # The MUIOGO API contract requires a start_time in every status response.
        # We record it at task *creation* — not when the executor picks it up —
        # because from the user's perspective, they clicked "Run" at this moment.
        # If the executor is busy (all 4 workers occupied), the task sits in the
        # queue.  Recording start_time here means the timestamp reflects user
        # intent, not executor availability.  This matches how AWS Batch reports
        # "submitted at" vs "started at".
        "start_time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_task(scenario: str) -> str:
    """
    Enqueue a new model run and return its task_id *immediately*.

    WHY THIS IS NON-BLOCKING (the most important design decision in this file):
    The Flask route calls create_task() and returns the task_id to the browser
    in the HTTP response — all within milliseconds.  The browser then polls
    GET /tasks/<id> every few seconds to check progress.

    If this were blocking (i.e., we called run_model() here and waited), the
    HTTP request would take 15–20 minutes.  Most browsers time out after 60s.
    Reverse proxies (nginx) default to 60s.  Even if you increased timeouts,
    the user would see zero feedback for 20 minutes.  Non-blocking task
    creation is what makes real-time progress reporting possible.

    WHY 8-CHAR UUID (not full UUID, not auto-increment):
    Full UUIDs are ugly in URLs.  Auto-increment integers leak information
    (how many tasks have been created — a minor security concern).  8 hex
    chars from uuid4 give 4 billion possible IDs with no guessability, and
    they're short enough for clean API URLs like /tasks/a1b2c3d4.
    """
    task_id = uuid.uuid4().hex[:8]

    with _tasks_lock:
        _tasks[task_id] = _blank_task(task_id, scenario)

    # WHY submit() AND NOT direct thread creation (threading.Thread):
    # ThreadPoolExecutor manages a *pool* of reusable threads.  Creating a new
    # thread per task has startup overhead (~1ms) and no concurrency limit — if
    # 100 users hit the API, you'd spawn 100 threads and likely OOM.  The pool
    # caps at max_workers=4, queuing excess tasks automatically.  submit()
    # returns a Future we intentionally discard — we don't need it because
    # progress is reported via the callback, not the return value.
    _executor.submit(_run_task, task_id, scenario)
    return task_id


def get_task(task_id: str) -> Optional[Dict]:
    """
    Return a *copy* of the task's current state, or None if unknown.

    WHY DEEP COPY (not returning the dict directly):
    If we returned a reference to the live dict, the Flask route could
    (accidentally or intentionally) modify it — and those modifications would
    corrupt the authoritative task state.  For example:
        task = get_task(id)
        task["status"] = "done"   # This would mutate the real record!
    copy.deepcopy() breaks that link.  The caller gets an independent snapshot.
    This is a defensive-programming pattern that prevents an entire class of
    concurrency bugs.

    WHY RETURN NONE (not raise KeyError):
    The Flask route needs to distinguish "task doesn't exist" (→ 404) from
    "task exists" (→ 200).  Returning None lets the route do a simple
    `if task is None: return 404` check.  Raising would force a try/except
    in the route handler, which is noisier and less Pythonic for a "not found"
    case that isn't truly exceptional.
    """
    with _tasks_lock:
        task = _tasks.get(task_id)
        return copy.deepcopy(task) if task is not None else None


# ---------------------------------------------------------------------------
# Internal: runs on a background thread
# ---------------------------------------------------------------------------

def _run_task(task_id: str, scenario: str) -> None:
    """
    Execute the model and funnel progress updates into the shared task dict.

    WHY BROAD EXCEPTION HANDLING (not letting it propagate):
    This is a ThreadPoolExecutor worker.  If an exception escapes, the executor
    catches it and stores it on the Future — but *nobody calls .result() on
    that Future* because we use callback-based progress, not Future-based.
    So without our try/except, a crash here would be **completely silent**:
    the task would stay "running" forever, the user would poll indefinitely,
    and there'd be nothing in the logs.  By catching broadly, we guarantee the
    task always reaches a terminal state ("completed" or "failed") with a
    meaningful error message.
    """
    def _update_callback(progress: Dict) -> None:
        """
        Merge iteration-level progress into the task's authoritative record.

        WHY A CLOSURE (not a class method, not a module-level function):
        This closure captures `task_id` from the enclosing scope.  The mock
        model calls update_callback(dict) without knowing anything about
        task_manager's internals — it doesn't know about _tasks, _tasks_lock,
        or even that threads are involved.  This is the Dependency Inversion
        principle: the high-level module (task_manager) defines the contract,
        and the low-level module (mock_model) depends only on the abstraction
        (a callable).  If we later switch to WebSocket push, we only change
        this closure — the model code is untouched.
        """
        with _tasks_lock:
            task = _tasks[task_id]
            task["status"] = progress.get("status", task["status"])
            task["iteration"] = progress.get("iteration", task["iteration"])
            task["max_iterations"] = progress.get("max_iterations", task["max_iterations"])
            task["epsilon"] = progress.get("epsilon", task["epsilon"])
            task["progress_pct"] = progress.get("progress_pct", task["progress_pct"])
            task["epsilon_history"] = progress.get("epsilon_history", task["epsilon_history"])

    try:
        # WHY SET "running" BEFORE the model call:
        # There's a brief moment between submit() and the first callback where
        # the task is "queued".  We explicitly transition to "running" here so
        # the client sees a clean state machine: queued → running → completed.
        # Without this, the first status a polling client might see is "queued"
        # with iteration=1 — a contradictory state.
        with _tasks_lock:
            _tasks[task_id]["status"] = "running"

        results = run_model(task_id, scenario, _update_callback)

        # WHY SET progress_pct TO 100.0 EXPLICITLY:
        # If the model converged early (e.g., iteration 10 of 18), the last
        # callback reported progress_pct = 55.6%.  But from the user's
        # perspective, the task is *done* — showing 55% complete when the
        # results are ready is confusing.  We override to 100% here for a
        # clean UX signal.
        with _tasks_lock:
            _tasks[task_id]["status"] = "completed"
            _tasks[task_id]["results"] = results
            _tasks[task_id]["progress_pct"] = 100.0

    except Exception as exc:
        with _tasks_lock:
            _tasks[task_id]["status"] = "failed"
            _tasks[task_id]["error"] = str(exc)
