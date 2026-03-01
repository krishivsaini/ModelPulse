"""
tests/test_task_manager.py — Integration tests for the ModelPulse task lifecycle.

Why these 4 tests cover the critical paths:
-----------------------------------------------
Together they verify the full task state machine (queued → running → completed)
and the negative lookup path:

  1. test_create_task_returns_id_and_queued — confirms the non-blocking contract:
     create_task must return an ID instantly and the initial status must be
     "queued", proving the work was enqueued, not executed synchronously.

  2. test_status_transitions_to_running — after a short wait the background
     thread must have started the convergence loop, so status should be
     "running".  This validates that the ThreadPoolExecutor is actually
     dispatching work.

  3. test_completed_with_results — waits for the full run to finish and checks
     for the terminal "completed" status plus the expected result shape
     (macro + energy_climate).  This is the end-to-end happy path.

  4. test_unknown_task_returns_none — ensures get_task cleanly returns None
     for non-existent IDs rather than raising, which the Flask route depends
     on to return a 404.

Edge cases intentionally *not* tested here (they would go in a dedicated
failure-path suite):
  - Invalid scenario name → "failed" status
  - Executor at max capacity (all 4 workers busy)
  - Callback exceptions

stdlib-only: zero extra dependencies — runs everywhere CI does.
"""

import sys
import os
import time
import unittest

# Ensure the project root is on sys.path so `import task_manager` resolves
# regardless of where the test runner's cwd is.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from task_manager import create_task, get_task  # noqa: E402


class TestTaskManager(unittest.TestCase):
    """Integration tests for the task lifecycle: queued → running → completed."""

    def test_create_task_returns_id_and_queued(self):
        """create_task must return an 8-char hex ID with initial status 'queued'."""
        task_id = create_task("baseline")

        self.assertIsInstance(task_id, str)
        self.assertEqual(len(task_id), 8)

        task = get_task(task_id)
        self.assertIsNotNone(task)
        # Status should be "queued" or already "running" if the executor is
        # very fast, but it must never be None or an unexpected value.
        self.assertIn(task["status"], ("queued", "running"))

    def test_status_transitions_to_running(self):
        """After ~3 s the background thread should be mid-convergence."""
        task_id = create_task("green_transition")
        time.sleep(3)

        task = get_task(task_id)
        self.assertIsNotNone(task)
        # After 3 seconds the first sleep (1–2 s) is over, so at least one
        # iteration callback has fired and status should be "running".
        # It's *possible* (though unlikely) the run already completed on a
        # very fast machine, so we accept "completed" as well.
        self.assertIn(task["status"], ("running", "completed"))

    def test_completed_with_results(self):
        """
        After enough time the task must reach 'completed' with full results.

        We allow up to 45 s (18 iterations × 2 s max + buffer).  In practice
        the mock converges much faster due to early stopping.
        """
        task_id = create_task("fiscal_reform")

        deadline = time.time() + 45
        while time.time() < deadline:
            task = get_task(task_id)
            if task and task["status"] in ("completed", "failed"):
                break
            time.sleep(0.5)

        task = get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "completed")
        self.assertIsNotNone(task["results"])

        # Verify the two required result sections exist
        self.assertIn("macro", task["results"])
        self.assertIn("energy_climate", task["results"])

        # Spot-check that each section has the expected keys
        macro_keys = {"gdp_growth_pct", "employment_rate_pct",
                      "tax_revenue_gdp_pct", "consumption_growth"}
        self.assertTrue(macro_keys.issubset(task["results"]["macro"].keys()))

        energy_keys = {"total_energy_demand_gwh", "renewable_share_pct",
                       "emissions_mtco2", "energy_intensity"}
        self.assertTrue(energy_keys.issubset(task["results"]["energy_climate"].keys()))

    def test_unknown_task_returns_none(self):
        """get_task must return None for IDs that were never created."""
        self.assertIsNone(get_task("does_not_exist"))


if __name__ == "__main__":
    unittest.main()
