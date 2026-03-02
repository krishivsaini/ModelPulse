"""
app.py — Flask API layer for ModelPulse.

WHY THIS FILE EXISTS (interview framing):
This is the HTTP boundary between the browser and the backend.  It has ONE job:
translate between HTTP semantics (verbs, status codes, JSON) and the internal
task_manager API (Python dicts, function calls).  It should contain zero business
logic — no convergence math, no thread management, no model knowledge.  If you
see an `import math` here, something is architecturally wrong.

This separation matters because:
  - The task_manager can be tested without spinning up a web server.
  - The Flask routes can be tested with Flask's test client without waiting for
    real model convergence.
  - If MUIOGO later migrates to FastAPI or Django, only this file changes —
    the task_manager and mock_model are untouched.

WHY FLASK (not FastAPI, not Django):
Flask is the simplest production-ready Python web framework.  Our async pattern
uses threads, not asyncio, so FastAPI's async advantage doesn't apply.  Django
is overkill — we have no ORM, no admin panel, no migrations.  Flask gives us
exactly what we need: routing, JSON responses, and template rendering.
"""

from flask import Flask, jsonify, redirect, render_template, request, url_for

from task_manager import create_task, get_task

# ---------------------------------------------------------------------------
# App factory-style initialization
# ---------------------------------------------------------------------------
# WHY NOT use an app factory (create_app function) HERE:
# App factories are great for large projects with multiple configs (test, dev,
# prod).  For a single-config demo, a module-level `app` is simpler and avoids
# indirection.  If ModelPulse grows to need per-environment configs, converting
# to a factory is a 5-minute refactor.

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Allowed scenarios — the validation allow-list
# ---------------------------------------------------------------------------
# WHY A FROZENSET (not a list, not fetched from mock_model):
# 1. frozenset is immutable — can't be accidentally mutated at runtime.
# 2. O(1) membership testing vs O(n) for a list.
# 3. Defined here (not imported from mock_model) because input validation is
#    the API layer's responsibility.  If mock_model adds a new scenario, we
#    *deliberately* want to update the API layer too — it forces us to review
#    the change before exposing it to users.  This is the "allow-list" security
#    pattern: deny by default, permit explicitly.

ALLOWED_SCENARIOS = frozenset([
    "baseline",
    "green_transition",
    "fiscal_reform",
    "mixed_policy",
])


# ---------------------------------------------------------------------------
# Internal status → MUIOGO-contract status mapping
# ---------------------------------------------------------------------------
# WHY MAP HERE (not change the internal values):
# task_manager.py and mock_model.py use lowercase status strings ("queued",
# "running", "completed", "failed") — this is a Python convention and keeps
# the backend layer consistent with itself.  The MUIOGO API contract uses
# capitalized strings ("Queued", "Running", "Completed", "Error") to match
# the real UN system.  Translating at the API boundary means:
#   - Internal code stays Pythonic
#   - Only one file changes if the external contract evolves
#   - No risk of accidentally breaking internal status checks by capitalizing

_STATUS_MAP = {
    "queued":    "Queued",
    "running":   "Running",
    "completed": "Completed",
    "failed":    "Error",
}


def _build_logs(task: dict) -> list[str]:
    """
    Build a human-readable log list from the task's current state.

    WHY BUILD LOGS ON THE FLY (not store them in the task dict):
    Storing a growing list of log strings in the task dict would mean the
    update_callback writes to it on every iteration, the Flask route reads it
    on every poll, and both need to be under the lock.  By building logs from
    the task's scalar fields (status, iteration, epsilon), we avoid storing
    redundant data and keep the lock's critical section short.

    The log messages are designed to match what a real MUIOGO status endpoint
    would return — policymakers see human-readable progress, not raw numbers.
    """
    logs: list[str] = []
    status = task["status"]

    # Every task starts with initialization
    logs.append("Task initialized...")

    if status in ("running", "completed", "failed"):
        logs.append("Solver started...")

    # Add per-iteration log entries from the epsilon history
    # WHY USE epsilon_history (not just current iteration):
    # The client might poll infrequently and miss intermediate iterations.
    # By replaying all completed iterations as log entries, every poll response
    # gives a complete history — the client never sees gaps.
    for i, eps in enumerate(task.get("epsilon_history", []), start=1):
        logs.append(f"Iteration {i} complete — ε = {eps:.6f}")

    # Terminal messages
    if status == "completed":
        final_iter = task.get("iteration", "?")
        logs.append(f"Converged after {final_iter} iterations")
    elif status == "failed":
        error_msg = task.get("error", "Unknown error")
        logs.append(f"Error: {error_msg}")

    return logs


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/run", methods=["POST"])
def api_run():
    """
    Start a new model run.

    WHY HTTP 202 (not 200):
    202 Accepted means "I received your request and will process it, but it's
    not done yet."  200 OK implies the work is finished and the response body
    contains the result.  Since we're enqueuing a 15-minute model run and
    returning immediately, 202 is semantically correct per RFC 7231 §6.3.3.
    This is the same status code that AWS Batch, GitHub Actions, and the real
    MUIOGO API return for async job submissions.  Using 200 here would mislead
    API consumers into thinking the result is already available.
    """
    data = request.get_json(silent=True)

    # --- Input validation ---
    # WHY silent=True ABOVE:
    # If the client sends a non-JSON body (or no Content-Type header),
    # get_json() would raise a 400 by default.  silent=True returns None
    # instead, letting us give a more descriptive error message below.
    if not data or "scenario" not in data:
        return jsonify({
            "error": "Missing required field: 'scenario'",
            "allowed_scenarios": sorted(ALLOWED_SCENARIOS),
        }), 400

    scenario = data["scenario"]

    if scenario not in ALLOWED_SCENARIOS:
        return jsonify({
            "error": f"Unknown scenario: '{scenario}'",
            "allowed_scenarios": sorted(ALLOWED_SCENARIOS),
        }), 400

    task_id = create_task(scenario)

    return jsonify({
        "task_id": task_id,
        "status": "Queued",
        "message": f"Model run '{scenario}' has been queued.",
    }), 202


@app.route("/api/status/<task_id>", methods=["GET"])
def api_status(task_id):
    """
    Poll the current status of a model run.

    Response shape matches the MUIOGO API contract:
    {
        "status":    "Running",
        "logs":      ["Task initialized...", "Solver started...", ...],
        "result":    null | { ... },
        "start_time": "2026-03-02T15:45:00+00:00",

        // Extra fields for the frontend's live chart and progress bar:
        "iteration":       4,
        "max_iterations":  15,
        "epsilon":         0.042,
        "progress_pct":    26.7,
        "epsilon_history": [0.55, 0.31, 0.17, 0.042]
    }

    WHY BOTH CONTRACT FIELDS AND EXTRA FIELDS:
    The contract fields (status, logs, result, start_time) match what the real
    MUIOGO API returns — this proves we understand their interface.  The extra
    fields (iteration, epsilon, etc.) power our custom frontend features (live
    convergence chart, progress bar) that go beyond what MUIOGO's basic UI
    offers.  Keeping both in one endpoint means the frontend makes ONE poll
    request, not two — simpler client code and half the network traffic.
    """
    task = get_task(task_id)

    if task is None:
        return jsonify({"error": f"Task '{task_id}' not found"}), 404

    # --- Map internal status to MUIOGO convention ---
    muiogo_status = _STATUS_MAP.get(task["status"], task["status"])

    # --- Build the response ---
    # WHY result IS ONLY INCLUDED WHEN COMPLETED:
    # Sending partial/null results for in-progress tasks wastes bandwidth and
    # could confuse the frontend into rendering incomplete data.  The client
    # checks `status === "Completed"` before accessing `result` — this is a
    # standard guard in the MUIOGO frontend.
    response = {
        # ── MUIOGO contract fields ──
        "status":     muiogo_status,
        "logs":       _build_logs(task),
        "result":     task["results"] if task["status"] == "completed" else None,
        "start_time": task.get("start_time"),
        "metadata":   task.get("metadata"),

        # ── Extra fields for ModelPulse frontend ──
        "iteration":       task["iteration"],
        "max_iterations":  task["max_iterations"],
        "epsilon":         task["epsilon"],
        "progress_pct":    task["progress_pct"],
        "epsilon_history": task["epsilon_history"],
    }

    return jsonify(response), 200


# ---------------------------------------------------------------------------
# Page Routes (template rendering — HTML comes tomorrow)
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the main landing page."""
    return render_template("index.html")


@app.route("/running/<task_id>")
def running(task_id):
    """
    Serve the live-progress page for a running task.

    WHY REDIRECT TO INDEX (not 404):
    If someone bookmarks /running/abc123 and comes back after a server restart,
    the task_id won't exist.  Showing a 404 is confusing for a non-technical
    user.  Redirecting to the index page (where they can start a new run) is
    a better UX — it's what they'd do next anyway.
    """
    task = get_task(task_id)
    if task is None:
        return redirect(url_for("index"))
    return render_template("running.html", task_id=task_id)


@app.route("/results/<task_id>")
def results(task_id):
    """
    Serve the results page for a completed task.

    WHY REDIRECT TO RUNNING (not show empty results):
    If the user navigates to /results/abc123 while the task is still running,
    showing an empty results page would be confusing.  Redirecting them to the
    running page lets them watch the progress and they'll be redirected to
    results automatically when it completes (the frontend handles this).
    """
    task = get_task(task_id)
    if task is None:
        return redirect(url_for("index"))
    if task["status"] != "completed":
        return redirect(url_for("running", task_id=task_id))
    return render_template("results.html", task_id=task_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# WHY debug=True AND port=5001 (not 5000):
# debug=True enables auto-reload on code changes and detailed error pages —
# essential during development.  NEVER enable this in production (it exposes
# a Python debugger that can execute arbitrary code).
#
# WHY PORT 5001 (not Flask's default 5000):
# macOS Monterey and later use port 5000 for AirPlay Receiver.  If we used
# 5000, curl would silently connect to AirPlay instead of Flask and return
# a 403 Forbidden with "Server: AirTunes" — an incredibly confusing bug.
# Port 5001 avoids the conflict entirely.

if __name__ == "__main__":
    app.run(debug=True, port=5001)
