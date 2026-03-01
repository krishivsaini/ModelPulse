"""
mock_model.py — Simulated OG-Core / CLEWS convergence loop for ModelPulse.

WHY THIS FILE EXISTS (interview framing):
The UN MUIOGO program runs two heavyweight solvers — OG-Core (macroeconomic) and
OSeMOSYS/CLEWS (energy/climate).  Both take 15–20 minutes per run.  We can't ask
every front-end developer to install those solvers locally, and we can't wait
15 minutes every time we want to test a UI change.  So we built a mock that
*behaves* like the real solver — iterative convergence, realistic timing, progress
callbacks — without any external dependencies.

WHY CALLBACKS INSTEAD OF RETURN VALUES:
An interviewer might ask: "Why not just return the final result?"  Because a
15-minute run with zero feedback is a terrible UX.  Policymakers need to see
progress — is the model converging?  How many iterations are left?  The callback
pattern lets the model *push* updates after every iteration.  The caller decides
what to do with them: store in a dict for polling, push over a WebSocket, write
to a database — the model doesn't care.  This is the Strategy pattern applied
to progress reporting.  It means we can swap the entire transport layer without
touching a single line of solver code.
"""

import math
import random
import time
from typing import Callable, Dict


# ---------------------------------------------------------------------------
# Scenario outcome tables
# ---------------------------------------------------------------------------
# WHY A STATIC TABLE WITH NOISE (not random generation):
# In a real policy tool, "baseline" always produces roughly the same GDP growth.
# If we generated fully random numbers, the demo would show GDP jumping from 2%
# to 8% between runs — policymakers would never trust it.  Instead, we store
# plausible base values per scenario and add ±3% noise.  This gives realistic
# run-to-run variation (like a real stochastic solver) while keeping results
# anchored to economically sensible ranges.
#
# WHY FOUR SCENARIOS:
# These map to the kinds of policy levers MUIOGO analysts actually model:
#   - baseline:         status quo — the "control group" for comparison
#   - green_transition: aggressive renewables push — GDP dips, emissions drop
#   - fiscal_reform:    tax restructuring — GDP rises, energy stays flat
#   - mixed_policy:     combination — shows tradeoffs aren't always linear
# Having 4 scenarios is enough to demo side-by-side comparison in the UI
# without overwhelming the user.

_SCENARIO_RESULTS: Dict[str, Dict] = {
    "baseline": {
        "macro": {
            "gdp_growth_pct": 2.1,
            "employment_rate_pct": 94.5,
            "tax_revenue_gdp_pct": 18.3,
            "consumption_growth": 1.8,
        },
        "energy_climate": {
            "total_energy_demand_gwh": 4520.0,
            "renewable_share_pct": 22.0,
            "emissions_mtco2": 310.0,
            "energy_intensity": 0.85,
        },
    },
    "green_transition": {
        "macro": {
            "gdp_growth_pct": 1.7,
            "employment_rate_pct": 93.8,
            "tax_revenue_gdp_pct": 19.1,
            "consumption_growth": 1.4,
        },
        "energy_climate": {
            "total_energy_demand_gwh": 4100.0,
            "renewable_share_pct": 48.5,
            "emissions_mtco2": 195.0,
            "energy_intensity": 0.62,
        },
    },
    "fiscal_reform": {
        "macro": {
            "gdp_growth_pct": 2.8,
            "employment_rate_pct": 95.2,
            "tax_revenue_gdp_pct": 21.6,
            "consumption_growth": 2.3,
        },
        "energy_climate": {
            "total_energy_demand_gwh": 4680.0,
            "renewable_share_pct": 24.0,
            "emissions_mtco2": 305.0,
            "energy_intensity": 0.81,
        },
    },
    "mixed_policy": {
        "macro": {
            "gdp_growth_pct": 2.3,
            "employment_rate_pct": 94.9,
            "tax_revenue_gdp_pct": 20.0,
            "consumption_growth": 1.9,
        },
        "energy_climate": {
            "total_energy_demand_gwh": 4350.0,
            "renewable_share_pct": 35.0,
            "emissions_mtco2": 248.0,
            "energy_intensity": 0.73,
        },
    },
}


def _add_noise(value: float, pct: float = 0.03) -> float:
    """Apply ±pct random noise to a scalar so repeated runs differ slightly."""
    return round(value * (1.0 + random.uniform(-pct, pct)), 4)


def _noisy_results(scenario: str) -> Dict:
    """
    Return a deep copy of the scenario's base results with ±3 % noise on every
    numeric field.

    WHY A COPY (not mutating in place):
    _SCENARIO_RESULTS is module-level state.  If we mutated it directly, the
    first run would shift all base values, and every subsequent run would drift
    further.  By building a fresh dict each time, the base values stay pristine.
    This is the same reason you'd use copy.deepcopy when returning shared state
    from a cache — avoid accidental mutation of the source of truth.
    """
    base = _SCENARIO_RESULTS[scenario]
    return {
        section: {k: _add_noise(v) for k, v in fields.items()}
        for section, fields in base.items()
    }


# ---------------------------------------------------------------------------
# Core convergence loop
# ---------------------------------------------------------------------------

def run_model(
    task_id: str,
    scenario: str,
    update_callback: Callable[[Dict], None],
) -> Dict:
    """
    Simulate an iterative solver convergence loop.

    Parameters
    ----------
    task_id : str
        Opaque identifier — passed through so the callback payload is
        self-describing (useful when a single listener handles multiple tasks).
    scenario : str
        One of the four scenario keys defined in _SCENARIO_RESULTS.
    update_callback : callable(dict) -> None
        Invoked after *every* iteration with a progress dict.  The task manager
        uses this to push state into the shared tasks dictionary; a WebSocket
        handler could push it directly to the client.

    Returns
    -------
    dict
        Final results with 'macro' and 'energy_climate' sub-dicts, plus
        convergence metadata.

    INTERVIEW-READY DESIGN DECISIONS:

    1. WHY EXPONENTIAL DECAY (not linear countdown):
       Real numerical solvers don't decrease error linearly.  They converge
       fast initially (big corrections) then slow down as they approach the
       solution.  Exponential decay with noise replicates this.  If an
       interviewer asks "why not just use a progress bar?", this is why —
       we're showing the *mathematical behaviour* of convergence, not just
       a percentage.

    2. WHY EARLY STOPPING (ε < 0.001):
       Real solvers check a convergence criterion every iteration and stop
       early if the residual is small enough.  This matters for the UI: the
       front-end needs to handle runs that finish at iteration 10 *and* runs
       that go all 18.  It also means different scenarios converge at
       different speeds — which is realistic and makes the demo more
       compelling for evaluators.

    3. WHY THIS FUNCTION IS BLOCKING (time.sleep):
       This might seem wrong — "shouldn't async code be non-blocking?"  But
       this function *never runs on the main thread*.  It always runs inside
       a ThreadPoolExecutor worker (managed by task_manager.py).  Making it
       blocking is actually the *correct* design: the thread pool handles
       concurrency, and this function stays simple and testable.  If we made
       it async (asyncio), we'd need to rewrite the entire Flask app to use
       an async framework — complexity with zero benefit for this use case.

    4. WHY task_id IS PASSED THROUGH (not generated here):
       The task_manager generates the ID so it can store it *before* the model
       starts running.  If the model generated it, there'd be a race condition:
       the client could poll for a task_id that doesn't exist yet because the
       background thread hasn't started.  Generating the ID up front and
       returning it immediately is what makes the API truly non-blocking.
    """
    if scenario not in _SCENARIO_RESULTS:
        raise ValueError(
            f"Unknown scenario '{scenario}'. "
            f"Choose from: {list(_SCENARIO_RESULTS.keys())}"
        )

    max_iterations = random.randint(12, 18)
    epsilon = 1.0

    # WHY THIS SPECIFIC DECAY RANGE (0.50–0.60):
    # At decay=0.55, epsilon after 12 iterations ≈ 0.55^12 ≈ 0.0003, which is
    # below our 0.001 threshold.  The range adds per-run variability: some runs
    # converge at iteration 10, others at 14.  This isn't arbitrary — it's
    # tuned so the early-stopping behaviour fires naturally within the
    # iteration window, making the demo feel realistic.
    decay_factor = random.uniform(0.50, 0.60)
    epsilon_history: list[float] = []

    for iteration in range(1, max_iterations + 1):
        # WHY ±5% NOISE ON EPSILON (not smooth decay):
        # Real solvers have numerical jitter — the residual doesn't decrease
        # monotonically.  Adding noise makes the convergence chart in the UI
        # look like a real solver trace, not a textbook exponential curve.
        # This is a small detail that shows attention to domain realism.
        noise = random.uniform(-0.05, 0.05)
        epsilon *= decay_factor * (1.0 + noise)
        epsilon = max(epsilon, 0.0)  # clamp to non-negative
        epsilon_history.append(round(epsilon, 6))

        progress_pct = round((iteration / max_iterations) * 100, 1)

        # WHY WE SEND A LIST COPY OF epsilon_history:
        # Without list(), the callback receiver gets a reference to our local
        # list.  If it stores that reference, it sees every future mutation.
        # This is a classic aliasing bug — easy to miss, hard to debug.
        update_callback({
            "task_id": task_id,
            "status": "running",
            "iteration": iteration,
            "max_iterations": max_iterations,
            "epsilon": round(epsilon, 6),
            "progress_pct": progress_pct,
            "epsilon_history": list(epsilon_history),
        })

        # --- Early stopping: the solver has converged ---
        if epsilon < 0.001:
            break

        # WHY 1–2 SECONDS (not faster):
        # Real OG-Core iterations take 1–2 seconds each.  Matching this timing
        # means the front-end team develops against realistic latency.  If we
        # used 0.1s sleeps, the UI would "work" in dev but break in production
        # when iterations suddenly take 10x longer.  Matching real timing now
        # prevents that class of bugs.
        time.sleep(random.uniform(1.0, 2.0))

    results = _noisy_results(scenario)
    results["convergence"] = {
        "iterations_run": iteration,
        "max_iterations": max_iterations,
        "final_epsilon": round(epsilon, 6),
        "converged_early": epsilon < 0.001,
    }

    return results
