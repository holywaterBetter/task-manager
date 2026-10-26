"""Git registry behavior across independent developer working copies."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from task_manager.gitstore import GitStore
from task_manager.store import DomainError


class GitStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "registry"
        self.store = GitStore(self.root)

    def create_project(self):
        return self.store.call("project.create", {"key": "ECODE", "name": "개발 도구"}, actor="alice")

    def create_task(self, title="첫 업무", **extra):
        return self.store.call("task.create", {"project": "ECODE", "title": title, "kind": "task", **extra}, actor="alice")

    @staticmethod
    def files(root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.json")}

    def assert_conflict(self, operation):
        with self.assertRaises(DomainError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse((self.root / ".write-lock").exists())

    def test_round_trip_preserves_hierarchy_dependencies_context_and_audit(self):
        self.assertFalse(self.root.exists())
        self.create_project()
        knowledge = self.store.call("doc.create", {"project": "ECODE", "title": "공용 규칙", "body": "결과를 검증한다.", "canonical": True})
        parent = self.create_task("상위 과제", kind="epic")
        first = self.create_task("선행 작업", parent=parent["id"], prompt="구현해줘", plan="1. 구현 2. 검증", doc_ids=[knowledge["id"]])
        second = self.create_task("후행 작업", parent=parent["id"])
        self.store.call("task.depend", {"id": second["id"], "version": second["version"], "depends_on": [first["id"]]})
        claimed = self.store.call("task.claim", {"id": first["id"], "version": first["version"]}, actor="alice")
        self.assertEqual(claimed["assignee"], "alice")
        completed = self.store.call("task.update", {
            "id": first["id"], "version": claimed["version"], "status": "done", "result": "완료",
            "evidence": ["검증 성공"], "contributions": {"alice": 1},
        }, actor="alice")
        fresh = GitStore(self.root)
        read = fresh.call("task.get", {"id": first["id"]})
        self.assertEqual(read["result"], "완료")
        self.assertEqual(read["version"], completed["version"])
        self.assertEqual(read["parent"], parent["id"])
        context = fresh.call("task.context", {"id": second["id"]})
        self.assertIn("완료", json.dumps(context, ensure_ascii=False))
        self.assertIn("공용 규칙", json.dumps(context, ensure_ascii=False))
        snapshot = fresh.call("snapshot")
        self.assertEqual(len(snapshot["events"]), 8)
        self.assertEqual(len(list((self.root / "events").glob("*.json"))), 8)
        self.assertTrue((self.root / "projects" / "ECODE.json").exists())
        canonical = json.loads((self.root / "tasks" / f"{first['id']}.json").read_text())
        self.assertNotIn("children", canonical)
        self.assertNotIn("blocked_by", canonical)
        self.assertNotIn("generated_at", canonical)
        self.assertFalse(list(self.root.rglob("*.db")))

    def test_reads_do_not_rewrite_records_or_audit(self):
        self.create_project()
        task = self.create_task()
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*.json")}
        for action, parameters in (
            ("project.list", {}), ("task.list", {}), ("task.get", {"id": task["id"]}),
            ("task.context", {"id": task["id"]}), ("doc.list", {}), ("report", {}), ("snapshot", {}),
        ):
            self.store.call(action, parameters)
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*.json")}
        self.assertEqual(before, after)

    def test_disjoint_creation_in_two_working_copies_merges_without_id_collisions(self):
        self.create_project()
        other_root = self.base / "other"
        shutil.copytree(self.root, other_root)
        first = self.create_task("PC A 업무")
        second = GitStore(other_root).call("task.create", {"project": "ECODE", "title": "PC B 업무", "kind": "task"}, actor="bob")
        self.assertNotEqual(first["id"], second["id"])
        # A normal Git merge takes independent, newly added record files.
        for path in other_root.rglob("*.json"):
            destination = self.root / path.relative_to(other_root)
            if not destination.exists():
                destination.parent.mkdir(exist_ok=True)
                shutil.copy2(path, destination)
            else:
                self.assertEqual(path.read_bytes(), destination.read_bytes())
        merged = self.store.call("snapshot")
        self.assertEqual({task["title"] for task in merged["tasks"]}, {"PC A 업무", "PC B 업무"})
        self.assertEqual(len(merged["events"]), 3)

    def test_disjoint_dependency_edits_are_revalidated_after_merge(self):
        self.create_project()
        first, second = self.create_task("A"), self.create_task("B")
        other_root = self.base / "other"
        shutil.copytree(self.root, other_root)
        self.store.call("task.depend", {"id": first["id"], "version": 1, "depends_on": [second["id"]]})
        other = GitStore(other_root)
        other.call("task.depend", {"id": second["id"], "version": 1, "depends_on": [first["id"]]})
        shutil.copy2(other_root / "tasks" / f"{second['id']}.json", self.root / "tasks" / f"{second['id']}.json")
        before = self.files(self.root)
        self.assert_conflict(lambda: self.store.call("snapshot"))
        self.assertEqual(before, self.files(self.root))

    def test_stale_lock_is_not_deleted_automatically(self):
        self.create_project()
        lock = self.root / ".write-lock"
        lock.mkdir()
        with self.assertRaises(DomainError) as caught:
            self.store.call("project.list")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertTrue(lock.is_dir())

    def test_merge_markers_invalid_json_duplicate_keys_and_id_mismatch_fail_closed(self):
        self.create_project()
        path = self.root / "projects" / "ECODE.json"
        valid = path.read_bytes()
        for body in (
            b'<<<<<<< HEAD\n{"key":"ECODE"}\n=======\n{}\n>>>>>>> branch\n',
            b'{"key":',
            b'{"key":"ECODE","key":"OTHER"}',
            b'{"key":"OTHER"}',
            valid.replace(b'"version": 1', b'"version": NaN'),
        ):
            with self.subTest(body=body):
                path.write_bytes(body)
                self.assert_conflict(lambda: self.store.call("task.create", {"project": "ECODE", "title": "拒否", "kind": "task"}))
                self.assertEqual(path.read_bytes(), body)
        path.write_bytes(valid)
        self.assertEqual(len(self.store.call("project.list")), 1)

    def test_dangling_knowledge_reference_is_rejected_without_writes(self):
        self.create_project()
        task = self.create_task()
        path = self.root / "tasks" / f"{task['id']}.json"
        data = json.loads(path.read_text())
        data["doc_ids"] = ["ECODE-D-missing"]
        path.write_text(json.dumps(data))
        before = self.files(self.root)
        self.assert_conflict(lambda: self.store.call("project.create", {"key": "NEW"}))
        self.assertEqual(before, self.files(self.root))

    def test_symlink_record_and_collection_are_rejected(self):
        self.create_project()
        external = self.base / "outside.json"
        external.write_text('{"key":"EXTERNAL"}')
        link = self.root / "projects" / "EXTERNAL.json"
        link.symlink_to(external)
        self.assert_conflict(lambda: self.store.call("snapshot"))
        link.unlink()
        (self.root / "tasks").symlink_to(self.base, target_is_directory=True)
        self.assert_conflict(lambda: self.store.call("snapshot"))
        self.assertEqual(external.read_text(), '{"key":"EXTERNAL"}')

    def test_unsafe_filename_and_unexpected_directory_are_rejected(self):
        self.create_project()
        path = self.root / "projects" / "bad name.json"
        path.write_text('{"key":"bad name"}')
        self.assert_conflict(lambda: self.store.call("snapshot"))
        path.unlink()
        (self.root / "projects" / "nested").mkdir()
        self.assert_conflict(lambda: self.store.call("snapshot"))

    def test_failed_multi_file_write_rolls_back_and_releases_lock(self):
        self.create_project()
        task = self.create_task()
        before = self.files(self.root)
        original = GitStore._atomic_write
        writes = 0

        def interrupted(path, body):
            nonlocal writes
            writes += 1
            original(path, body)
            if writes == 2:
                raise OSError("simulated failure after the second replacement")

        with patch.object(GitStore, "_atomic_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.call("task.update", {"id": task["id"], "version": 1, "title": "실패한 변경"})
        self.assertEqual(before, self.files(self.root))
        self.assertFalse((self.root / ".write-lock").exists())
        self.assertEqual(self.store.call("task.get", {"id": task["id"]})["title"], "첫 업무")

    @unittest.skipUnless(shutil.which("git"), "Git is needed for the unresolved index integration test")
    def test_valid_json_is_not_writable_while_git_index_remains_unmerged(self):
        self.create_project()
        task = self.create_task()

        def git(*arguments, success=True):
            result = subprocess.run(
                ["git", "-C", str(self.base), "-c", "user.name=Registry Test", "-c", "user.email=registry@example.invalid", *arguments],
                capture_output=True, text=True,
            )
            if success:
                self.assertEqual(result.returncode, 0, result.stderr)
            return result

        git("init", "-b", "main")
        git("add", "registry")
        git("commit", "-m", "base")
        git("checkout", "-b", "other")
        self.store.call("task.update", {"id": task["id"], "version": 1, "title": "other title"})
        git("add", "registry")
        git("commit", "-m", "other")
        git("checkout", "main")
        self.store.call("task.update", {"id": task["id"], "version": 1, "title": "main title"})
        chosen = (self.root / "tasks" / f"{task['id']}.json").read_bytes()
        git("add", "registry")
        git("commit", "-m", "main")
        self.assertNotEqual(git("merge", "other", success=False).returncode, 0)
        path = self.root / "tasks" / f"{task['id']}.json"
        path.write_bytes(chosen)  # Human resolved the text, but has not staged it.
        before = self.files(self.root)
        self.assert_conflict(lambda: self.create_task("Must wait for index resolution"))
        self.assertEqual(before, self.files(self.root))
        git("add", str(path.relative_to(self.base)))
        self.assertTrue(self.create_task("Now the index is resolved")["id"])


if __name__ == "__main__":
    unittest.main()
