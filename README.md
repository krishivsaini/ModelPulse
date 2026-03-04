# ModelPulse

## What is ModelPulse

MUIOGO's OG-Core and CLEWS model runs take 15–20 minutes per scenario. During that window, the UI is blocked — policymakers can't do anything except wait, and they have no visibility into whether the solver is converging, stalled, or failed. ModelPulse is a working demonstration of the async execution + polling pattern that solves this. It launches model runs in background threads, returns an HTTP 202 immediately, and exposes a status endpoint that the frontend polls every 2 seconds to render live iteration counts, convergence metrics, and structured logs. This is the exact pattern the real MUIOGO Track 1 backend needs — ModelPulse proves it works end-to-end with a realistic convergence simulation, a production-ready task lifecycle, and a fully functional polling UI.

## Live Demo

> 🔗 Deploy link: https://modelpulse.onrender.com

## Architecture

Full request lifecycle from user click to results page:

```
User clicks "Run Scenario"
      │
      ▼
POST /api/run  →  validate scenario  →  create_task(scenario)
      │                                        │
      │                                        ▼
      │                              ThreadPoolExecutor.submit()
      │                              stores task with status="queued"
      │                                        │
      ▼                                        ▼
HTTP 202 Accepted                    Background thread starts
{ task_id: "a1b2c3d4" }             mock_model.run_model() begins
      │                              iterative convergence loop
      │                                        │
      ▼                                        │
Browser redirects to                 Each iteration fires callback:
/running/<task_id>                   ┌─────────────────────────────┐
      │                              │ update_callback({           │
      ▼                              │   iteration: 3,             │
Frontend polls every 2s:             │   epsilon: 0.042,           │
GET /api/status/<task_id>  ◄─────────│   progress_pct: 25.0,       │
      │                              │   epsilon_history: [...]     │
      │                              │ })                           │
      ▼                              └─────────────────────────────┘
Response includes:                             │
  metadata.track1_iteration ──► live UI        │
  metadata.current_epsilon  ──► Plotly chart   │
  logs[] ──────────────────► append-only log   │
      │                                        │
      ▼                                        ▼
status === "Completed"               run_model() returns results
      │                              task status → "completed"
      ▼                              task results → macro + energy
Browser redirects to
/results/<task_id>
      │
      ▼
3 Plotly charts rendered:
  • GDP & Employment (macro)
  • Energy Mix (climate)
  • Convergence Trace (ε history)
```

**Key architectural boundaries:**
- `app.py` — HTTP translation layer only. Zero business logic, zero thread management.
- `task_manager.py` — Owns concurrency. `ThreadPoolExecutor` + `Lock`-protected task dict.
- `mock_model.py` — Pure convergence simulation. Communicates only via callback. Knows nothing about HTTP, threads, or storage.

## API Contract

The status endpoint returns the confirmed MUIOGO API payload (verified by NamanmeetSingh, Track 1 backend contributor):

```json
{
  "status": "Running",
  "logs": ["Calculating macro shocks...", "Solving iteration 3..."],
  "result": null,
  "start_time": "2026-03-02T15:45:00",
  "metadata": {
    "track1_iteration": 3,
    "current_epsilon": 0.042,
    "dampening_alpha": 0.2
  }
}
```

| Field | Description |
|---|---|
| `status` | Task lifecycle state: `"Queued"`, `"Running"`, `"Completed"`, or `"Error"` |
| `logs` | Ordered list of human-readable progress messages, rebuilt from task state on each poll |
| `result` | `null` while running; full macro + energy_climate result dict when completed |
| `start_time` | ISO 8601 timestamp recorded at task creation (user intent, not executor pickup) |
| `metadata.track1_iteration` | Current solver iteration, mirrored from the callback on every update |
| `metadata.current_epsilon` | Current convergence residual (ε), drives the live Plotly chart |
| `metadata.dampening_alpha` | Solver dampening constant (0.2), fixed for the duration of a run |

ModelPulse extends the contract with additional fields for its frontend (`iteration`, `max_iterations`, `epsilon`, `progress_pct`, `epsilon_history`) — all served from the same `GET /api/status/<task_id>` endpoint to keep the client to a single poll request.

## Key Engineering Decisions

### Why ThreadPoolExecutor over Celery or multiprocessing

The model simulation is I/O-bound — each iteration is a `time.sleep` call simulating real solver wait time. Python's GIL releases during I/O, so `ThreadPoolExecutor` gives us real concurrency without the complexity of multiprocessing. `ProcessPoolExecutor` would require pickling every function argument across process boundaries, and our progress callback is a closure that can't be pickled — it would crash at runtime. Celery is the production-grade answer, but it requires a Redis or RabbitMQ broker, which is infrastructure overhead that's wrong for a demo that evaluators need to run in under 60 seconds. `ThreadPoolExecutor` with `max_workers=4` gives us a bounded worker pool, automatic queueing when all workers are busy, and zero external dependencies. If MUIOGO scales this to production, Celery is the natural upgrade path — the task_manager's public API (`create_task`, `get_task`) wouldn't change.

### Why HTTP polling over WebSockets for this use case

WebSockets are bidirectional persistent connections — they're the right tool when the server needs to push high-frequency updates to many clients simultaneously (chat apps, live dashboards with sub-second updates). ModelPulse's updates arrive every 1–2 seconds per iteration, and each client tracks exactly one task. At this frequency and fan-out, polling is simpler and equally responsive. HTTP polling also works through every corporate proxy, load balancer, and CDN without special configuration — WebSockets require sticky sessions or a dedicated upgrade path that many government IT environments don't support. Polling is stateless: if the server restarts, the client's next `GET` just gets a 404 and can show a clean error. With WebSockets, the client needs reconnection logic, heartbeats, and state reconciliation. For MUIOGO's use case — a handful of concurrent policymaker sessions with second-granularity updates — HTTP polling is the right engineering tradeoff.

### Why HTTP 202 Accepted and what it communicates semantically

HTTP 202 means "I received your request and will process it, but the work isn't done yet." This is semantically distinct from 200 OK, which implies the response body contains the finished result. Since `POST /api/run` enqueues a model run that takes minutes and returns a `task_id` within milliseconds, 202 is the correct status code per RFC 7231 §6.3.3. It tells API consumers — whether they're our frontend, a curl script, or the future MUIOGO integration layer — that they need to poll for the result. This is the same status code used by AWS Batch, GitHub Actions, and Google Cloud Tasks for async job submission. Using 200 here would mislead consumers into treating the response as a completed result, which would cause silent failures when the `result` field is `null`.

### Why epsilon_history is built by appending scalars

The mock model sends a full `epsilon_history` array in each callback for convenience, but the real MUIOGO API only sends `metadata.current_epsilon` — a single scalar per poll response. To match this constraint, `task_manager.py` ignores the model's array and instead appends each `epsilon` value to its own server-side list on every callback. This means the convergence chart on the results page can always render from `task.epsilon_history`, even if the client-side accumulation from `running.html` is lost on a page refresh. Building history by appending scalars is also forward-compatible: when `mock_model.py` is replaced with real OG-Core calls that only emit one epsilon per iteration, the task_manager's accumulation logic works without any changes.

## Project Structure

```
ModelPulse/
├── app.py                  # Flask API layer — routes, status mapping, zero business logic
├── mock_model.py           # Simulated OG-Core convergence loop — 4 scenarios, callbacks
├── task_manager.py         # ThreadPoolExecutor orchestration — Lock-protected task dict
├── requirements.txt        # Flask + gunicorn (production), no async framework deps
├── render.yaml             # Render.com deployment config — free tier Python web service
├── .gitignore              # Python standard ignores (__pycache__, .env, venv/)
│
├── templates/
│   ├── index.html          # Scenario selector — dropdown + Run button, POSTs to /api/run
│   ├── running.html        # Live polling page — Plotly convergence chart, append-only logs
│   └── results.html        # Final results — 3 Plotly charts (macro, energy, convergence)
│
├── static/
│   └── style.css           # Government-appropriate design — plain CSS, no frameworks
│
└── tests/
    └── test_task_manager.py # 4 unittest tests — full task lifecycle + negative lookup
```

## How to Run Locally

**Prerequisites:** Python 3.10+

```bash
# Clone the repo
git clone https://github.com/krishivsaini/ModelPulse.git
cd ModelPulse

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the development server
python app.py
```

The app will be available at **http://localhost:5001**.

**Run the test suite:**

```bash
python -m pytest tests/ -v
```

All 4 tests should pass. Tests run against the real `ThreadPoolExecutor` and `mock_model` — no mocking, no fakes.

## How to Deploy on Render

[Render](https://render.com) offers a free tier for Python web services. ModelPulse includes a `render.yaml` blueprint for one-click deployment.

### Step-by-step

1. **Push your code to GitHub** (if not already done).

2. **Connect Render to your repo:**
   - Go to [dashboard.render.com](https://dashboard.render.com)
   - Click **New → Web Service**
   - Connect your GitHub account and select the `ModelPulse` repository

3. **Configure the service** (or let `render.yaml` auto-detect):
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`
   - **Plan:** Free

4. **Environment variables:**
   - `PYTHON_VERSION` = `3.11.4` (set automatically by `render.yaml`)

5. **Deploy.** Render will install dependencies, start gunicorn, and give you a public URL.

### Start command explained

```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
```

- `app:app` — module `app`, Flask instance `app`
- `--workers 2` — 2 gunicorn worker processes (free tier has 512 MB RAM)
- `--threads 4` — 4 threads per worker, matching our `ThreadPoolExecutor(max_workers=4)`
- `--timeout 120` — extended timeout so long-polling requests don't get killed
- `--bind 0.0.0.0:$PORT` — Render injects `$PORT` at runtime

### Free tier limitations

Render's free tier spins down the service after 15 minutes of inactivity. The first request after spin-down takes ~30 seconds while the container cold-starts. This means a model run started right after spin-up may feel slow on the first iteration. For a demo or portfolio evaluation, this is acceptable — evaluators expect it. For production use, Render's paid tier ($7/month) keeps the service warm and adds persistent disks if you need to survive restarts.

## Relevance to MUIOGO GSoC 2026

ModelPulse directly demonstrates the patterns that MUIOGO's Track 1 backend needs. The core problem — long-running OG-Core and CLEWS model runs blocking policymaker workflows — is solved here with the same architecture the real system would use: non-blocking task submission (HTTP 202 + `task_id`), background thread execution, callback-driven progress reporting, and a status endpoint that a polling frontend can consume. The API contract (`status`, `logs`, `result`, `start_time`, `metadata`) isn't invented — it was confirmed by NamanmeetSingh, the Track 1 backend contributor. Every engineering decision (ThreadPoolExecutor over Celery, polling over WebSockets, append-only epsilon history) was made with MUIOGO's real constraints in mind: government IT environments, single-digit concurrent users, and second-granularity solver updates.

The next integration step is replacing `mock_model.py` with real OG-Core API calls. The architecture is designed for this: `mock_model.run_model()` takes a `task_id`, a `scenario` string, and a callback — the same interface a wrapper around OG-Core's Python API would expose. The `task_manager` doesn't know or care what's behind that function signature. Swap the import, adjust the scenario parameters to match OG-Core's input schema, and the rest of the system — the Flask routes, the polling frontend, the status contract, the convergence chart — works without modification. This is what the separation of concerns buys: the model layer, the orchestration layer, and the HTTP layer can evolve independently.
