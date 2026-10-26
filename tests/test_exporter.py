import copy
import json
from pathlib import Path
import re
import tempfile
import unittest

from task_manager.exporter import export_vault


def sample():
    return {
        "projects": [{"key": "ECODE", "name": "팀 개발", "description": "공유 프로젝트"}],
        "tasks": [
            {"id": "ECODE-epic123abc", "project": "ECODE", "kind": "epic", "parent": None, "title": "에픽", "status": "todo", "weight": 100, "estimate": 100},
            {"id": "ECODE-story123ab", "project": "ECODE", "kind": "story", "parent": "ECODE-epic123abc", "title": "스토리", "status": "todo", "weight": 100},
            {"id": "ECODE-task123abc", "project": "ECODE", "kind": "task", "parent": "ECODE-story123ab", "title": "완료 업무", "status": "done", "assignee": "개발자", "priority": "high", "weight": 2, "estimate": 3, "prompt": "요청 원문", "plan": "작업 계획", "result": "완료 결과", "evidence": ["검증 통과"], "doc_ids": ["ECODE-D-doc123abcd"], "due_date": "2026-10-30"},
            {"id": "ECODE-next123abc", "project": "ECODE", "kind": "task", "parent": "ECODE-story123ab", "title": "후속 업무", "status": "todo", "weight": 1, "estimate": 2, "depends_on": ["ECODE-task123abc"]},
        ],
        "docs": [
            {"id": "ECODE-D-doc123abcd", "project": "ECODE", "parent": None, "title": "개발 규칙", "body": "팀 규칙 원문", "canonical": True},
            {"id": "ECODE-D-child123ab", "project": "ECODE", "parent": "ECODE-D-doc123abcd", "title": "검증 규칙", "body": "검증 항목"},
        ],
        "events": [],
    }


class ExportVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"

    def tearDown(self):
        self.temp.cleanup()

    def test_export_links_resolve_and_original_snapshot_is_preserved(self):
        snapshot = sample()
        original = copy.deepcopy(snapshot)
        result = export_vault(snapshot, self.vault)
        self.assertEqual(snapshot, original)
        self.assertEqual(result["progress"]["ECODE"]["percent"], 75.0)
        root = self.vault / "TeamTasks"
        for file in root.rglob("*.md"):
            body = file.read_text(encoding="utf-8")
            self.assertTrue(body.startswith("---\n"))
            self.assertIn("조회 전용", body)
            for target in re.findall(r"\]\(([^)]+\.md)\)", body):
                self.assertTrue((file.parent / target).is_file(), (file, target))
        task = (root / "Tasks/ECODE-task123abc.md").read_text(encoding="utf-8")
        self.assertIn("ECODE-next123abc.md", task)
        self.assertIn("ECODE-story123ab.md", task)
        self.assertIn("ECODE-D-doc123abcd.md", task)
        self.assertIn("요청 원문", task)
        self.assertIn("작업 계획", task)
        self.assertIn("완료 결과", task)
        self.assertIn("검증 통과", task)

    def test_repeated_export_is_stable_and_preserves_unrelated_notes(self):
        self.vault.mkdir()
        personal = self.vault / "내 노트.md"
        personal.write_text("개인 기록", encoding="utf-8")
        export_vault(sample(), self.vault)
        root = self.vault / "TeamTasks"
        before = {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}
        result = export_vault(sample(), self.vault)
        after = {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["files_written"], 0)
        self.assertEqual(personal.read_text(encoding="utf-8"), "개인 기록")

    def test_allocation_and_dependency_schedule_are_visible(self):
        data = sample()
        data["tasks"][0]["contributions"] = {"parent-must-not-count": 1}
        data["tasks"][2]["contributions"] = {"alice": 0.6, "bob": 0.4}
        data["tasks"][3]["start_date"] = "2026-10-29"
        export_vault(data, self.vault)
        root = self.vault / "TeamTasks"
        home = (root / "Home.md").read_text(encoding="utf-8")
        self.assertIn("| alice | 3.6 |", home)
        self.assertIn("| bob | 2.4 |", home)
        self.assertNotIn("parent-must-not-count", home)
        schedule = (root / "Schedule.md").read_text(encoding="utf-8")
        self.assertIn("마감 2026-10-30가 시작 2026-10-29보다 늦습니다", schedule)

    def test_canonical_paths_and_yaml_title_escaping(self):
        snapshot = sample()
        snapshot["tasks"][2]["title"] = '../escape\nstatus: done\n"quoted": [test] | bad'
        export_vault(snapshot, self.vault)
        root = self.vault / "TeamTasks"
        task = (root / "Tasks/ECODE-task123abc.md").read_text(encoding="utf-8")
        yaml_title = next(line.removeprefix("title: ") for line in task.splitlines() if line.startswith("title: "))
        self.assertEqual(json.loads(yaml_title), snapshot["tasks"][2]["title"])
        self.assertFalse((self.vault / "escape.md").exists())
        bad = sample()
        bad["projects"][0]["key"] = "../../escape"
        with self.assertRaises(ValueError):
            export_vault(bad, self.vault)

    def test_cycles_and_missing_links_fail_before_writes(self):
        mutations = [
            lambda data: data["tasks"][0].update(parent="ECODE-task123abc"),
            lambda data: data["tasks"][2].update(depends_on=["ECODE-next123abc"]),
            lambda data: data["docs"][0].update(parent="ECODE-D-child123ab"),
            lambda data: data["tasks"][2].update(doc_ids=["ECODE-D-missing"]),
            lambda data: data["tasks"][2].update(depends_on=["ECODE-missing"]),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                data = sample()
                mutation(data)
                with self.assertRaises(ValueError):
                    export_vault(data, self.vault)
                self.assertFalse(self.vault.exists())

    def test_existing_user_directory_and_replaced_note_are_protected(self):
        root = self.vault / "TeamTasks"
        root.mkdir(parents=True)
        note = root / "Home.md"
        note.write_text("내 기존 기록", encoding="utf-8")
        with self.assertRaises(ValueError):
            export_vault(sample(), self.vault)
        self.assertEqual(note.read_text(encoding="utf-8"), "내 기존 기록")
        note.unlink()
        export_vault(sample(), self.vault)
        note.write_text("수동 교체 노트", encoding="utf-8")
        old_task = (root / "Tasks/ECODE-task123abc.md").read_bytes()
        data = sample()
        data["tasks"][2]["title"] = "업데이트"
        with self.assertRaises(ValueError):
            export_vault(data, self.vault)
        self.assertEqual(note.read_text(encoding="utf-8"), "수동 교체 노트")
        self.assertEqual((root / "Tasks/ECODE-task123abc.md").read_bytes(), old_task)

    def test_symlink_destinations_files_and_manifest_are_rejected(self):
        target = Path(self.temp.name) / "elsewhere"
        target.mkdir()
        self.vault.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            export_vault(sample(), self.vault)
        self.assertEqual(list(target.iterdir()), [])
        self.vault.unlink()
        export_vault(sample(), self.vault)
        task = self.vault / "TeamTasks/Tasks/ECODE-task123abc.md"
        task.unlink()
        protected = target / "protected.md"
        protected.write_text("외부 기록", encoding="utf-8")
        task.symlink_to(protected)
        with self.assertRaises(ValueError):
            export_vault(sample(), self.vault)
        self.assertEqual(protected.read_text(encoding="utf-8"), "외부 기록")
        task.unlink()
        manifest = self.vault / "TeamTasks/.manifest.json"
        manifest.unlink()
        manifest.symlink_to(protected)
        with self.assertRaises(ValueError):
            export_vault(sample(), self.vault)

    def test_stale_cleanup_removes_only_owned_files(self):
        data = sample()
        export_vault(data, self.vault)
        root = self.vault / "TeamTasks"
        personal = root / "Tasks/personal.md"
        personal.write_text("사용자 노트", encoding="utf-8")
        data["tasks"] = [task for task in data["tasks"] if task["id"] != "ECODE-next123abc"]
        result = export_vault(data, self.vault)
        self.assertEqual(result["files_removed"], 1)
        self.assertFalse((root / "Tasks/ECODE-next123abc.md").exists())
        self.assertEqual(personal.read_text(encoding="utf-8"), "사용자 노트")

    def test_manifest_cannot_delete_outside_projection(self):
        export_vault(sample(), self.vault)
        manifest = self.vault / "TeamTasks/.manifest.json"
        parsed = json.loads(manifest.read_text(encoding="utf-8"))
        parsed["files"].append("../../outside.md")
        manifest.write_text(json.dumps(parsed), encoding="utf-8")
        with self.assertRaises(ValueError):
            export_vault(sample(), self.vault)

    def test_cancelled_leaves_and_parent_weights_do_not_count_twice(self):
        data = sample()
        data["tasks"][3]["status"] = "cancelled"
        result = export_vault(data, self.vault)
        self.assertEqual(result["progress"]["ECODE"], {"leaf_tasks": 1, "total_weight": 6.0, "done_weight": 6.0, "percent": 100.0})


if __name__ == "__main__":
    unittest.main()
