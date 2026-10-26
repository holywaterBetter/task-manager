"""Exercise two real Git clones against an isolated local bare remote.

All remotes are temporary filesystem paths. No network, global Git settings,
production credentials, or existing repositories participate in these tests.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from task_manager.gitstore import GitStore
from task_manager.store import DomainError


@unittest.skipUnless(shutil.which("git"), "Git is required for the local two-PC workflow")
class GitWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="teamtasks-git-workflow-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.remote = self.base / "remote.git"
        self.alice = self.base / "alice"
        self.bob = self.base / "bob"
        # Ignore host Git configuration; identities are configured only in each
        # disposable clone. Every remote URL below is an absolute local path.
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.environment.update({"LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                 "GIT_TERMINAL_PROMPT": "0"})
        self.git(self.base, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.base, "clone", str(self.remote), str(self.alice))
        self.configure(self.alice, "Alice", "alice@example.invalid")
        self.alice_store = GitStore(self.alice / "registry")
        self.alice_store.call("project.create", {"key": "ECODE", "name": "Shared work"}, actor="alice")
        self.commit(self.alice, "Initial shared project")
        self.git(self.alice, "push", "-u", "origin", "main")
        self.git(self.base, "clone", str(self.remote), str(self.bob))
        self.configure(self.bob, "Bob", "bob@example.invalid")
        self.bob_store = GitStore(self.bob / "registry")

    def git(self, directory, *arguments, success=True):
        result = subprocess.run(
            ["git", "-C", str(directory), *arguments], capture_output=True, text=True,
            timeout=20, env=self.environment,
        )
        if success:
            self.assertEqual(result.returncode, 0, f"git {' '.join(arguments)}\n{result.stdout}\n{result.stderr}")
        return result

    def configure(self, checkout, name, email):
        for key, value in (("user.name", name), ("user.email", email), ("commit.gpgsign", "false"),
                           ("core.autocrlf", "false"), ("core.hooksPath", os.devnull)):
            self.git(checkout, "config", "--local", key, value)

    def commit(self, checkout, message):
        self.git(checkout, "add", "registry")
        self.git(checkout, "commit", "-m", message)
        return self.git(checkout, "rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def snapshot(store):
        result = store.call("snapshot")
        result.pop("generated_at", None)
        return result

    @staticmethod
    def files(checkout):
        registry = checkout / "registry"
        return {str(path.relative_to(registry)): path.read_bytes() for path in registry.rglob("*.json")}

    def test_independent_tasks_merge_and_round_trip_with_both_audit_events(self):
        initial_head = self.git(self.bob, "rev-parse", "HEAD").stdout.strip()
        with ThreadPoolExecutor(max_workers=2) as pool:
            alice_future = pool.submit(self.alice_store.call, "task.create", {
                "project": "ECODE", "title": "Alice's task", "kind": "task",
            }, actor="alice")
            bob_future = pool.submit(self.bob_store.call, "task.create", {
                "project": "ECODE", "title": "Bob's task", "kind": "task",
            }, actor="bob")
            alice_task, bob_task = alice_future.result(), bob_future.result()
        self.assertNotEqual(alice_task["id"], bob_task["id"])
        # Registry calls do not commit or push on the user's behalf.
        self.assertEqual(self.git(self.alice, "rev-parse", "HEAD").stdout.strip(), initial_head)
        self.assertEqual(self.git(self.bob, "rev-parse", "HEAD").stdout.strip(), initial_head)
        alice_head = self.commit(self.alice, "Alice adds an independent task")
        bob_head = self.commit(self.bob, "Bob adds an independent task")
        self.git(self.alice, "push", "origin", "main")

        # The other PC has a valid but stale view: the registry never asserts
        # remote freshness and does not fetch during a normal read.
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/main").stdout.strip(), alice_head)
        self.assertEqual(self.git(self.bob, "rev-parse", "origin/main").stdout.strip(), initial_head)
        self.assertEqual([row["id"] for row in self.bob_store.call("task.list")], [bob_task["id"]])
        self.assertEqual(self.git(self.bob, "rev-parse", "origin/main").stdout.strip(), initial_head)

        failed_pull = self.git(self.bob, "pull", "--ff-only", success=False)
        self.assertNotEqual(failed_pull.returncode, 0)
        self.assertEqual(self.git(self.bob, "rev-parse", "HEAD").stdout.strip(), bob_head)
        self.assertFalse(self.git(self.bob, "ls-files", "--unmerged").stdout.strip())
        self.git(self.bob, "merge", "--no-edit", "origin/main")
        merged = self.snapshot(self.bob_store)
        self.assertEqual({row["id"] for row in merged["tasks"]}, {alice_task["id"], bob_task["id"]})
        self.assertEqual(len(merged["events"]), 3)
        creations = [event for event in merged["events"] if event["action"] == "task.create"]
        self.assertEqual({(event["entity_id"], event["actor"]) for event in creations},
                         {(alice_task["id"], "alice"), (bob_task["id"], "bob")})
        self.assertEqual(len({event["id"] for event in merged["events"]}), 3)
        self.git(self.bob, "push", "origin", "main")
        self.git(self.alice, "pull", "--ff-only")
        self.assertEqual(self.snapshot(self.alice_store), self.snapshot(self.bob_store))
        self.assertEqual(self.files(self.alice), self.files(self.bob))
        self.assertFalse(self.git(self.alice, "status", "--porcelain").stdout.strip())
        self.assertFalse(self.git(self.bob, "status", "--porcelain").stdout.strip())

    def test_simultaneous_claims_are_local_and_require_explicit_conflict_resolution(self):
        task = self.alice_store.call("task.create", {"project": "ECODE", "title": "Shared claim target", "kind": "task"}, actor="alice")
        self.commit(self.alice, "Add a shared claim target")
        self.git(self.alice, "push", "origin", "main")
        self.git(self.bob, "pull", "--ff-only")
        version = task["version"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            alice_future = pool.submit(self.alice_store.call, "task.claim", {"id": task["id"], "version": version}, actor="alice")
            bob_future = pool.submit(self.bob_store.call, "task.claim", {"id": task["id"], "version": version}, actor="bob")
            alice_claim, bob_claim = alice_future.result(), bob_future.result()
        # Both operations succeed against the same starting version. A local
        # lock and version check deliberately provide no global claim guarantee.
        self.assertEqual((alice_claim["version"], bob_claim["version"]), (version + 1, version + 1))
        self.assertEqual((alice_claim["assignee"], bob_claim["assignee"]), ("alice", "bob"))
        self.commit(self.alice, "Alice claims the shared task")
        self.commit(self.bob, "Bob also claims the shared task")
        self.git(self.alice, "push", "origin", "main")
        rejected_push = self.git(self.bob, "push", "origin", "main", success=False)
        self.assertNotEqual(rejected_push.returncode, 0)
        self.assertEqual(self.bob_store.call("task.get", {"id": task["id"]})["assignee"], "bob")
        self.git(self.bob, "fetch", "origin")
        merge = self.git(self.bob, "merge", "--no-edit", "origin/main", success=False)
        self.assertNotEqual(merge.returncode, 0)
        conflicting_path = f"registry/tasks/{task['id']}.json"
        self.assertIn(conflicting_path, self.git(self.bob, "diff", "--name-only", "--diff-filter=U").stdout.splitlines())
        task_path = self.bob / conflicting_path
        self.assertIn("<<<<<<<", task_path.read_text())
        before_files = self.files(self.bob)
        before_status = self.git(self.bob, "status", "--porcelain").stdout
        remote_head = self.git(self.remote, "rev-parse", "refs/heads/main").stdout
        with self.assertRaises(DomainError) as caught:
            self.bob_store.call("project.create", {"key": "BLOCKED"}, actor="bob")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.files(self.bob), before_files)
        self.assertEqual(self.git(self.bob, "status", "--porcelain").stdout, before_status)
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/main").stdout, remote_head)
        self.assertTrue(self.git(self.bob, "ls-files", "--unmerged").stdout.strip())
        self.assertFalse((self.bob / "registry" / ".write-lock").exists())
        # Both audit events survived the merge, while the disputed task remains
        # visibly unresolved for the developers to decide; no winner is chosen.
        events = [json.loads(path.read_text()) for path in (self.bob / "registry" / "events").glob("*.json")]
        claims = [event for event in events if event["action"] == "task.claim"]
        self.assertEqual({event["actor"] for event in claims}, {"alice", "bob"})


if __name__ == "__main__":
    unittest.main()
