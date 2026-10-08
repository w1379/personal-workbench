"""Real Windows-spawn concurrency smoke on disposable, synthetic V2 data.

Run directly with --json for a small machine-readable acceptance result.
"""
import json
import multiprocessing
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path

from personal_system import core
from personal_system.search import search


def _writer(root, machine_id, worker, rounds, start, results):
    try:
        start.wait(30)
        conflicts = 0
        for n in range(rounds):
            core.add_note(root, f"parallelmarker worker{worker} record{n}", title=f"Synthetic {worker}/{n}",
                          operation_id=f"synthetic-{worker}-{n}")
            for attempt in range(100):
                current = core.get(root, machine_id)
                state = json.loads(current.get("current_state", {}).get("state_json", "{}"))
                state[str(worker)] = state.get(str(worker), 0) + 1
                try:
                    core.set_state(root, machine_id, state, current["revision"], source_ref="synthetic:test")
                    break
                except core.ConflictError:
                    conflicts += 1
                    time.sleep(0.001 * ((worker + attempt) % 5 + 1))
            else:
                raise AssertionError("State did not converge within bounded conflict retries")
        results.put({"worker": worker, "role": "writer", "operations": rounds, "conflicts": conflicts, "ok": True})
    except Exception as error:
        results.put({"worker": worker, "role": "writer", "ok": False, "error": type(error).__name__ + ": " + str(error)})


def _reader(root, worker, rounds, start, results):
    try:
        start.wait(30)
        last_total = 0
        for _ in range(rounds):
            found = search(root, "parallelmarker", kind="note", history="current", limit=7)
            if found["total"] < last_total:
                raise AssertionError("Append-only notes disappeared between read snapshots")
            last_total = found["total"]
            con = core.connect(root, readonly=True)
            try:
                missing = con.execute("""SELECT COUNT(*) FROM notes n LEFT JOIN search_entries e
                    ON e.item_id=n.item_id AND e.entry_key='main' WHERE e.entry_id IS NULL OR e.text!=n.body""").fetchone()[0]
                if missing:
                    raise AssertionError("Committed note and search projection disagree")
            finally:
                con.close()
            time.sleep(0.002)
        results.put({"worker": worker, "role": "reader", "operations": rounds, "ok": True})
    except Exception as error:
        results.put({"worker": worker, "role": "reader", "ok": False, "error": type(error).__name__ + ": " + str(error)})


def run_smoke(writer_rounds=25, reader_rounds=50):
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="pis-concurrency-") as folder:
        root = Path(folder)
        core.initialize(root)
        machine = core.register(root, "machine", "Synthetic shared state")
        start, results = context.Event(), context.Queue()
        processes = [context.Process(target=_writer, args=(str(root), machine["item_id"], n, writer_rounds, start, results)) for n in range(4)]
        processes += [context.Process(target=_reader, args=(str(root), n, reader_rounds, start, results)) for n in range(4)]
        started = time.perf_counter()
        reports = []
        try:
            for process in processes:
                process.start()
            start.set()
            deadline = time.monotonic() + 60
            while len(reports) < 8:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("Concurrent smoke exceeded its 60-second bound")
                try:
                    reports.append(results.get(timeout=min(remaining, 1)))
                except queue.Empty:
                    if all(not p.is_alive() for p in processes):
                        raise AssertionError("A child process exited without reporting")
            for process in processes:
                process.join(timeout=max(0, deadline - time.monotonic()))
            failures = [r for r in reports if not r["ok"]]
            if failures or any(p.exitcode != 0 for p in processes):
                raise AssertionError({"failed": failures, "exitcodes": [p.exitcode for p in processes]})
            final = core.get(root, machine["item_id"])
            counts = json.loads(final["current_state"]["state_json"])
            if counts != {str(n): writer_rounds for n in range(4)}:
                raise AssertionError("Shared state lost a writer's accepted increments")
            con = core.connect(root, readonly=True)
            try:
                integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
                notes = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                changes = con.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
            finally:
                con.close()
            found = search(root, "parallelmarker", kind="note", history="current")
            if integrity != "ok" or foreign_keys or notes != writer_rounds * 4 or found["total"] != notes or changes != notes:
                raise AssertionError("Final integrity, projection or history count mismatch")
            return {"writers": 4, "readers": 4, "notes_written": notes, "state_updates": changes,
                    "read_operations": reader_rounds * 4, "detected_conflicts": sum(r.get("conflicts", 0) for r in reports),
                    "final_state_revision": final["revision"], "integrity": integrity, "foreign_key_errors": foreign_keys,
                    "all_children_completed": True, "seconds": round(time.perf_counter() - started, 3)}
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            results.close()
            results.join_thread()


class ConcurrentSearchTests(unittest.TestCase):
    def test_four_writers_four_readers(self):
        result = run_smoke()
        self.assertEqual(result["notes_written"], 100)
        self.assertEqual(result["state_updates"], 100)
        self.assertEqual(result["read_operations"], 200)


if __name__ == "__main__":
    if "--json" in sys.argv:
        print(json.dumps(run_smoke(), sort_keys=True))
    else:
        unittest.main()
