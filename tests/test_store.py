from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from task_manager.store import DomainError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.call("project.create", key="ECODE", name="Example")

    def call(self, action, actor="alice", **params):
        return self.store.call(action, params, actor)

    def task(self, **params):
        return self.call("task.create", project="ECODE", title=params.pop("title", "Task"), **params)

    def done(self, task, **params):
        return self.call("task.update", id=task["id"], version=task["version"],
                         status="done", result="Implemented and reviewed", evidence=["commit:abc123"],
                         contributions=params.pop("contributions", {"alice": 1}), **params)

    def assertDomain(self, code, action, **params):
        with self.assertRaises(DomainError) as caught:
            self.call(action, **params)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_uuid_records_and_round_trip_snapshot(self):
        task = self.task()
        doc = self.call("doc.create", project="ECODE", title="Reference")
        self.assertRegex(task["id"], r"^ECODE-[0-9a-f]{10}$")
        self.assertRegex(doc["id"], r"^ECODE-D-[0-9a-f]{10}$")
        snapshot = self.call("snapshot")
        self.assertNotIn("children", snapshot["tasks"][0])
        self.assertRegex(snapshot["events"][0]["id"], r"^[0-9a-f]{32}$")
        other = Store()
        self.addCleanup(other.close)
        other.load_snapshot(snapshot)
        self.assertEqual(other.call("task.get", {"id": task["id"]}), task)
        self.assertEqual(other.call("snapshot")["events"], snapshot["events"])

    def test_stale_version_and_audit_rollback(self):
        task = self.task()
        updated = self.call("task.update", id=task["id"], version=1, title="Changed")
        self.assertEqual(updated["version"], 2)
        count = len(self.call("snapshot")["events"])
        self.assertDomain("version_conflict", "task.update", id=task["id"], version=1, title="Stale")
        events = self.call("snapshot")["events"]
        self.assertEqual(len(events), count)
        event = events[-1]
        self.assertEqual(event["before"]["title"], "Task")
        self.assertEqual(event["after"]["title"], "Changed")
        self.assertEqual(event["actor"], "alice")

    def test_dependency_cycles_and_mixed_rollup_deadlock(self):
        first, second = self.task(), self.task()
        first = self.call("task.depend", id=first["id"], version=1, depends_on=[second["id"]])
        self.assertDomain("cycle", "task.depend", id=second["id"], version=1, depends_on=[first["id"]])
        self.assertDomain("cycle", "task.depend", id=second["id"], version=1, depends_on=[second["id"]])
        parent = self.task(kind="story")
        child = self.task(parent=parent["id"])
        self.assertDomain("cycle", "task.depend", id=child["id"], version=1, depends_on=[parent["id"]])
        self.assertEqual(self.call("task.get", id=second["id"])["depends_on"], [])

    def test_hierarchy_and_cross_project_references(self):
        epic = self.task(kind="epic")
        story = self.task(kind="story", parent=epic["id"])
        task = self.task(parent=story["id"])
        sub = self.task(kind="subtask", parent=task["id"])
        self.assertEqual(self.call("task.get", id=task["id"])["children"], [sub["id"]])
        self.assertDomain("validation", "task.create", project="ECODE", title="Invalid", kind="subtask")
        self.assertDomain("validation", "task.update", id=task["id"], version=1, kind="story")
        self.call("project.create", key="OTHER")
        other = self.call("task.create", project="OTHER", title="Other")
        self.assertDomain("validation", "task.depend", id=other["id"], version=1, depends_on=[task["id"]])
        self.assertDomain("validation", "task.create", project="OTHER", title="Child", parent=story["id"])

    def test_completion_requires_evidence_shares_and_finished_dependencies(self):
        blocker, task = self.task(), self.task()
        task = self.call("task.depend", id=task["id"], version=1, depends_on=[blocker["id"]])
        self.assertDomain("completion_gate", "task.update", id=task["id"], version=2, status="done")
        self.assertDomain("validation", "task.update", id=task["id"], version=2,
                          contributions={"alice": 0.6, "bob": 0.3})
        with self.assertRaises(DomainError) as caught:
            self.done(task)
        self.assertEqual(caught.exception.code, "blocked")
        self.assertDomain("blocked", "task.claim", id=task["id"], version=2)
        blocker = self.done(blocker)
        complete = self.done(task, contributions={"alice": 0.25, "bob": 0.75})
        self.assertEqual(complete["status"], "done")
        self.assertDomain("blocked", "task.update", id=blocker["id"], version=blocker["version"], status="backlog")

    def test_completion_rollup_and_cancelled_children(self):
        parent = self.task(kind="story", weight=100, estimate=100)
        child = self.task(parent=parent["id"], weight=2, estimate=3)
        cancelled = self.task(parent=parent["id"], weight=100)
        with self.assertRaises(DomainError):
            self.done(parent)
        self.done(child, contributions={"alice": 0.25, "bob": 0.75})
        self.call("task.update", id=cancelled["id"], version=1, status="cancelled")
        self.done(parent)
        report = self.call("report", project="ECODE")
        project = report["projects"][0]
        self.assertEqual(project["task_count"], 3)
        self.assertEqual(project["leaf_count"], 1)
        self.assertEqual(project["cancelled_leaf_count"], 1)
        self.assertEqual(project["total_weight"], 6)
        self.assertEqual(project["completed_weight"], 6)
        self.assertEqual(project["progress"], 1)
        amounts = {row["actor"]: row["allocated_weight"] for row in report["contributions"]}
        self.assertEqual(amounts, {"alice": 1.5, "bob": 4.5})

    def test_weighted_progress(self):
        done = self.task(weight=2, estimate=3)
        self.task(weight=1, estimate=2)
        self.done(done)
        report = self.call("report")["projects"][0]
        self.assertEqual(report["total_weight"], 8)
        self.assertEqual(report["progress"], 0.75)

    def test_schedule_warnings_allow_same_day_and_do_not_reschedule(self):
        prerequisite = self.task(due_date="2026-11-02")
        later = self.task(start_date="2026-11-02")
        later = self.call("task.depend", id=later["id"], version=1, depends_on=[prerequisite["id"]])
        self.assertEqual(self.call("report")["schedule_warnings"], [])
        self.call("task.update", id=later["id"], version=later["version"], start_date="2026-11-01")
        report = self.call("report")
        self.assertEqual(len(report["schedule_warnings"]), 1)
        self.assertEqual(report["schedule_warnings"][0]["task_id"], later["id"])
        self.assertEqual(report["projects"][0]["schedule_warnings"], report["schedule_warnings"])
        self.assertEqual(self.call("task.get", id=later["id"])["start_date"], "2026-11-01")

    def test_claim_owner_guard_release_and_work_queue(self):
        task = self.task()
        self.assertEqual([t["id"] for t in self.call("task.list", ready_only=True)], [task["id"]])
        claim = self.call("task.claim", id=task["id"], version=1)
        self.assertEqual((claim["assignee"], claim["status"]), ("alice", "in_progress"))
        self.assertEqual(self.call("task.list", ready_only=True), [])
        self.assertDomain("claimed", "task.update", actor="bob", id=task["id"], version=2, title="Take over")
        self.assertDomain("claimed", "task.depend", actor="bob", id=task["id"], version=2, depends_on=[])
        self.assertDomain("claimed", "task.release", actor="bob", id=task["id"], version=2)
        released = self.call("task.release", id=task["id"], version=2)
        self.assertIsNone(released["assignee"])
        self.assertEqual(released["status"], "backlog")
        self.call("task.claim", actor="bob", id=task["id"], version=3)

    def test_update_cannot_bypass_claim_ownership(self):
        assigned = self.task(assignee="alice")
        self.assertDomain("claimed", "task.update", actor="bob", id=assigned["id"], version=1,
                          assignee="bob", status="in_progress")
        self.assertDomain("claimed", "task.update", actor="alice", id=assigned["id"], version=1,
                          assignee="bob", status="in_progress")
        self.assertEqual(self.call("task.get", id=assigned["id"])["version"], 1)
        # Reassignment before a claim remains an explicit, audited coordination step.
        reassigned = self.call("task.update", actor="bob", id=assigned["id"], version=1, assignee="bob")
        event = self.call("snapshot")["events"][-1]
        self.assertEqual((event["actor"], event["before"]["assignee"], event["after"]["assignee"]),
                         ("bob", "alice", "bob"))
        claimed = self.call("task.update", actor="bob", id=reassigned["id"], version=2, status="in_progress")
        self.assertEqual(claimed["status"], "in_progress")
        unassigned = self.task()
        self.assertDomain("claimed", "task.update", actor="alice", id=unassigned["id"], version=1,
                          assignee="bob", status="in_progress")
        claimed = self.call("task.update", actor="bob", id=unassigned["id"], version=1,
                            assignee="bob", status="in_progress")
        self.assertEqual(claimed["assignee"], "bob")

    def test_task_and_event_id_collisions_retry_without_overwriting(self):
        original = self.task(title="Original")
        old_event = self.call("snapshot")["events"][-1]
        candidates = [original["id"].split("-")[-1] + "0" * 22, "f" * 32,
                      old_event["id"], "e" * 32]
        with patch("task_manager.store.uuid4", side_effect=[SimpleNamespace(hex=value) for value in candidates]):
            created = self.task(title="New")
        self.assertEqual(created["id"], "ECODE-" + "f" * 10)
        self.assertEqual(self.call("task.get", id=original["id"]), original)
        snapshot = self.call("snapshot")
        self.assertEqual(len(snapshot["tasks"]), 2)
        self.assertIn(old_event, snapshot["events"])
        self.assertEqual(len(snapshot["events"]), 3)

    def test_document_id_collisions_retry_and_exhaustion_preserves_records(self):
        original = self.call("doc.create", project="ECODE", title="Original")
        collision = SimpleNamespace(hex=original["id"].split("-")[-1] + "0" * 22)
        with patch("task_manager.store.uuid4", side_effect=[collision, SimpleNamespace(hex="a" * 32), SimpleNamespace(hex="b" * 32)]):
            created = self.call("doc.create", project="ECODE", title="New")
        self.assertNotEqual(original["id"], created["id"])
        before = self.call("snapshot")
        with patch("task_manager.store.uuid4", return_value=collision):
            self.assertDomain("identity_collision", "doc.create", project="ECODE", title="Never overwrite")
        after = self.call("snapshot")
        self.assertEqual(after["docs"], before["docs"])
        self.assertEqual(after["events"], before["events"])

    def test_event_collision_exhaustion_rolls_back_creation(self):
        existing_event = self.call("snapshot")["events"][0]
        before = self.call("snapshot")
        with patch("task_manager.store.uuid4", side_effect=[SimpleNamespace(hex="a" * 32)] + [SimpleNamespace(hex=existing_event["id"])] * 8):
            self.assertDomain("identity_collision", "task.create", project="ECODE", title="Rollback on audit failure")
        after = self.call("snapshot")
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["events"], before["events"])

    def test_concurrent_local_claims_have_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite"
            first, second = Store(path), Store(path)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            first.call("project.create", {"key": "ECODE"})
            task = first.call("task.create", {"project": "ECODE", "title": "Race"})
            barrier = threading.Barrier(2)

            def claim(store, actor):
                barrier.wait(timeout=5)
                try:
                    return store.call("task.claim", {"id": task["id"], "version": 1}, actor)
                except DomainError as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(claim, first, "alice"), pool.submit(claim, second, "bob")]
                outcomes = [f.result(timeout=10) for f in futures]
            self.assertEqual(sum(isinstance(outcome, dict) for outcome in outcomes), 1)
            error = next(outcome for outcome in outcomes if isinstance(outcome, DomainError))
            self.assertEqual(error.code, "version_conflict")
            self.assertEqual(len(first.call("snapshot")["events"]), 3)

    def test_document_tree_cycles_and_context_inheritance(self):
        canonical = self.call("doc.create", project="ECODE", title="Conventions", body="USE TRANSACTIONS", canonical=True)
        ancestor_doc = self.call("doc.create", project="ECODE", title="Architecture", body="SYSTEM OVERVIEW")
        linked = self.call("doc.create", project="ECODE", title="Implementation", body="DOMAIN KNOWLEDGE",
                           parent=ancestor_doc["id"], tags=["sqlite"])
        self.assertDomain("cycle", "doc.update", id=ancestor_doc["id"], version=1, parent=linked["id"])
        epic = self.task(kind="epic", doc_ids=[linked["id"]], prompt="EPIC PROMPT")
        child = self.task(parent=epic["id"], prompt="SELECTED PROMPT", plan="IMPLEMENT STEPS")
        context = self.call("task.context", id=child["id"])
        self.assertFalse(context["truncated"])
        for expected in ("SELECTED PROMPT", "IMPLEMENT STEPS", "EPIC PROMPT", "USE TRANSACTIONS", "DOMAIN KNOWLEDGE", "SYSTEM OVERVIEW"):
            self.assertIn(expected, context["text"])
        source_ids = [source["id"] for source in context["sources"]]
        self.assertEqual(len(source_ids), len(set(source_ids)))
        self.assertIn(canonical["id"], source_ids)
        self.assertEqual(self.call("doc.list", query="sqlite")[0]["id"], linked["id"])

    def test_context_budget_and_exclusion_of_unlinked_documents(self):
        unrelated = self.call("doc.create", project="ECODE", title="Private unrelated", body="UNRELATED BODY")
        task = self.task(prompt="Work " * 1000)
        context = self.call("task.context", id=task["id"], max_chars=1000)
        self.assertTrue(context["truncated"])
        self.assertLessEqual(len(context["text"]), 1000)
        self.assertIn("Context truncated", context["text"])
        self.assertNotIn(unrelated["id"], [s["id"] for s in context["sources"]])
        self.assertDomain("validation", "task.context", id=task["id"], max_chars=64001)
        self.assertDomain("validation", "task.context", id=task["id"], max_chars=True)

    def test_context_prioritizes_selected_prompt_over_long_background_and_title(self):
        self.call("project.create", key="LONG", description="BACKGROUND " * 500)
        task = self.call("task.create", project="LONG", title="T" * 500,
                         prompt="SELECTED TASK PROMPT", plan="SELECTED TASK PLAN",
                         description="TASK BACKGROUND " * 500)
        context = self.call("task.context", id=task["id"], max_chars=1000)
        self.assertTrue(context["truncated"])
        self.assertLessEqual(len(context["text"]), 1000)
        self.assertIn("SELECTED TASK PROMPT", context["text"])
        self.assertIn("SELECTED TASK PLAN", context["text"])
        self.assertNotIn("T" * 500, context["text"])
        self.assertEqual(context["text"].count("Title:"), 1)
        self.assertIn("Available reference index", context["sources_note"])

    def test_import_validation_is_atomic(self):
        task = self.task()
        original = self.call("snapshot")
        invalid = deepcopy(original)
        invalid["tasks"][0]["depends_on"] = ["ECODE-0000000000"]
        with self.assertRaises(DomainError):
            self.store.load_snapshot(invalid)
        self.assertEqual(self.call("task.get", id=task["id"]), task)
        self.assertEqual(self.call("snapshot")["events"], original["events"])
        invalid = deepcopy(original)
        invalid["tasks"].append(deepcopy(invalid["tasks"][0]))
        with self.assertRaises(DomainError):
            self.store.load_snapshot(invalid)
        response_snapshot = deepcopy(original)
        response_snapshot["tasks"][0] = task
        self.store.load_snapshot(response_snapshot)
        self.assertNotIn("children", self.call("snapshot")["tasks"][0])

    def test_invalid_inputs_raise_domain_errors(self):
        bad_creates = [
            {"priority": True}, {"weight": -1}, {"estimate": float("inf")}, {"weight": 10 ** 1000},
            {"start_date": "2026-02-30"}, {"start_date": "2026-11-02", "due_date": "2026-11-01"},
            {"kind": []}, {"doc_ids": "not-list"}, {"assignee": ""}, {"extra": 1},
        ]
        for params in bad_creates:
            with self.subTest(params=params), self.assertRaises(DomainError):
                self.task(**params)
        task = self.task()
        self.assertDomain("validation", "task.update", id=task["id"], version=True, title="No")
        self.assertDomain("claimed", "task.update", id=task["id"], version=1, status="in_progress")
        self.assertDomain("validation", "doc.create", project="ECODE", title="Doc", canonical="yes")
        with self.assertRaises(DomainError):
            self.store.call("no.such.action", {})
        self.assertEqual(len(self.call("snapshot")["tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
