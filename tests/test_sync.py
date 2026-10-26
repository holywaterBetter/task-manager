"""Real local remotes exercise mandatory synchronization without network access."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from task_manager.gitstore import GitStore
from task_manager.store import DomainError
from task_manager.sync import SyncedGitStore


@unittest.skipUnless(shutil.which("git"), "Git is required for synchronization tests")
class SyncedGitStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="teamtasks-sync-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.remote = self.base / "remote.git"
        self.alice = self.base / "alice"
        self.bob = self.base / "bob"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update({"LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_TERMINAL_PROMPT": "0"})
        self.environment = environment
        self.patch_environment = patch.dict(os.environ, environment, clear=True)
        self.patch_environment.start()
        self.addCleanup(self.patch_environment.stop)
        self.git(self.base, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.base, "clone", str(self.remote), str(self.alice))
        self.configure(self.alice)
        GitStore(self.alice / "registry").call("project.create", {"key": "ECODE"})
        self.git(self.alice, "add", "registry")
        self.git(self.alice, "commit", "-m", "Seed shared registry")
        self.git(self.alice, "push", "-u", "origin", "main")
        self.git(self.base, "clone", str(self.remote), str(self.bob))
        self.configure(self.bob)
        self.alice_store = SyncedGitStore(self.alice / "registry")
        self.bob_store = SyncedGitStore(self.bob / "registry")

    def git(self, directory, *arguments, success=True):
        result = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True,
                                timeout=20, env=self.environment)
        if success:
            self.assertEqual(result.returncode, 0, f"git {' '.join(arguments)}\n{result.stdout}\n{result.stderr}")
        return result

    def configure(self, checkout):
        for key, value in (("user.name", "Developer"), ("user.email", "developer@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false"), ("core.hooksPath", os.devnull)):
            self.git(checkout, "config", "--local", key, value)

    def head(self, checkout):
        return self.git(checkout, "rev-parse", "HEAD").stdout.strip()

    def marker(self, checkout):
        relative = self.git(checkout, "rev-parse", "--git-path", "teamtasks-sync-pending.json").stdout.strip()
        return checkout / relative

    def task(self, store=None, title="공동 업무"):
        return (store or self.alice_store).call("task.create", {"project": "ECODE", "title": title, "kind": "task"}, actor="alice")

    def assert_failure(self, operation, code="sync_precondition"):
        with self.assertRaises(DomainError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_create_pulls_commits_and_pushes_then_other_pc_reads_latest(self):
        initial = self.head(self.bob)
        task = self.task()
        self.assertEqual(self.head(self.remote), self.head(self.alice))
        self.assertNotEqual(initial, self.head(self.alice))
        self.assertEqual(self.head(self.bob), initial)
        self.assertEqual(self.bob_store.call("task.get", {"id": task["id"]})["title"], "공동 업무")
        self.assertEqual(self.head(self.bob), self.head(self.alice))
        self.assertFalse(self.git(self.alice, "status", "--porcelain").stdout)
        self.assertFalse(self.marker(self.alice).exists())

    def test_reads_still_pull_and_push_without_empty_commits(self):
        original = SyncedGitStore._run
        calls = []

        def observe(directory, *arguments, **kwargs):
            calls.append(arguments)
            return original(directory, *arguments, **kwargs)

        initial = self.head(self.alice)
        with patch.object(SyncedGitStore, "_run", side_effect=observe):
            self.assertEqual(self.alice_store.call("task.list"), [])
            self.alice_store.call("snapshot")
        self.assertEqual(self.head(self.alice), initial)
        self.assertEqual(sum(arguments[0] == "pull" for arguments in calls), 2)
        self.assertEqual(sum(arguments[0] == "push" for arguments in calls), 2)
        self.assertFalse(any(arguments[0] == "commit" for arguments in calls))
        self.assertTrue(all("--ff-only" in arguments for arguments in calls if arguments[0] == "pull"))
        self.assertTrue(all("--no-follow-tags" in arguments and "--recurse-submodules=no" in arguments
                            for arguments in calls if arguments[0] == "push"))

    def test_user_follow_tags_setting_cannot_publish_private_tags(self):
        self.git(self.alice, "config", "push.followTags", "true")
        self.git(self.alice, "tag", "-a", "private-tag", "-m", "Local tag kept private")
        self.task()
        self.assertEqual(self.git(self.remote, "tag", "--list").stdout, "")
        self.assertIn("private-tag", self.git(self.alice, "tag", "--list").stdout)

    def test_dirty_worktree_staged_or_untracked_is_rejected_before_pull_and_mutation(self):
        original = SyncedGitStore._run
        for staged in (False, True):
            with self.subTest(staged=staged):
                unrelated = self.alice / "private.txt"
                unrelated.write_text("Private code change")
                if staged:
                    self.git(self.alice, "add", "private.txt")
                with patch.object(SyncedGitStore, "_run", wraps=original) as observed:
                    self.assert_failure(lambda: self.task())
                self.assertFalse(any(call.args[1] == "pull" for call in observed.call_args_list))
                self.assertEqual(list((self.alice / "registry" / "tasks").glob("*.json")), [])
                self.git(self.alice, "reset", "--", "private.txt")
                unrelated.unlink()

    def test_preexisting_unpublished_commits_are_not_automatically_pushed(self):
        initial = self.head(self.remote)
        (self.alice / "private.txt").write_text("Private local work")
        self.git(self.alice, "add", "private.txt")
        self.git(self.alice, "commit", "-m", "Not reviewed for publication")
        self.assert_failure(lambda: self.alice_store.call("task.list"))
        self.assertEqual(self.head(self.remote), initial)

    def test_detached_head_missing_upstream_or_identity_blocks_changes(self):
        self.git(self.alice, "checkout", "--detach")
        self.assert_failure(lambda: self.task())
        self.git(self.alice, "checkout", "main")
        self.git(self.alice, "branch", "--unset-upstream")
        self.assert_failure(lambda: self.task())
        self.git(self.alice, "branch", "--set-upstream-to=origin/main")
        self.git(self.alice, "config", "--local", "--unset", "user.name")
        self.git(self.alice, "config", "--local", "--unset", "user.email")
        self.git(self.alice, "config", "--local", "user.useConfigOnly", "true")
        self.assert_failure(lambda: self.task())
        self.assertEqual(GitStore(self.alice / "registry").call("task.list"), [])

    def test_non_repository_and_local_upstream_are_rejected(self):
        plain = self.base / "plain"
        plain.mkdir()
        self.assert_failure(lambda: SyncedGitStore(plain / "registry").call("project.list"))
        self.git(self.alice, "config", "branch.main.remote", ".")
        self.assert_failure(lambda: self.task())

    def test_git_operations_or_unmerged_index_block_even_reads(self):
        for name in SyncedGitStore._operations:
            with self.subTest(name=name):
                path = self.alice / self.git(self.alice, "rev-parse", "--git-path", name).stdout.strip()
                path.write_text("in progress")
                self.assert_failure(lambda: self.alice_store.call("task.list"))
                path.unlink()

    def test_unmerged_index_without_merge_metadata_blocks_even_reads(self):
        mode, identity, _ = self.git(self.alice, "ls-files", "--stage", "registry/projects/ECODE.json").stdout.split(maxsplit=2)
        path = "registry/projects/ECODE.json"
        entries = f"0 {'0' * 40}\t{path}\n{mode} {identity} 2\t{path}\n"
        result = subprocess.run(["git", "-C", str(self.alice), "update-index", "--index-info"], input=entries,
                                capture_output=True, text=True, timeout=20, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.git(self.alice, "ls-files", "--unmerged").stdout)
        self.assert_failure(lambda: self.alice_store.call("task.list"))

    def test_failed_pull_never_executes_action_and_diagnostics_hide_credentials(self):
        original = SyncedGitStore._run

        def fail_pull(directory, *arguments, **kwargs):
            if arguments[0] == "pull":
                raise DomainError("git_sync", kwargs["message"], 503)
            return original(directory, *arguments, **kwargs)

        with patch.object(SyncedGitStore, "_run", side_effect=fail_pull):
            self.assert_failure(lambda: self.task(), "git_sync")
        self.assertEqual(GitStore(self.alice / "registry").call("task.list"), [])
        result = subprocess.CompletedProcess([], 1, "", "https://user:secret@example.invalid remote error")
        with patch("task_manager.sync.subprocess.run", return_value=result):
            error = self.assert_failure(lambda: SyncedGitStore._run(self.alice, "push"), "git_sync")
        self.assertNotIn("secret", str(error))
        self.assertNotIn("example.invalid", str(error))

    def test_push_failure_retains_commit_blocks_reexecution_and_retry_only_pushes(self):
        initial = self.head(self.alice)
        original = SyncedGitStore._run

        def fail_push(directory, *arguments, **kwargs):
            if arguments[0] == "push":
                raise DomainError("git_sync", kwargs["message"], 503)
            return original(directory, *arguments, **kwargs)

        with patch.object(SyncedGitStore, "_run", side_effect=fail_push):
            error = self.assert_failure(lambda: self.task(), "git_sync")
        retained = self.head(self.alice)
        self.assertNotEqual(initial, retained)
        pending = json.loads(self.marker(self.alice).read_text())
        self.assertEqual(pending["commit"], retained)
        self.assertEqual(pending["actor"], "alice")
        self.assertIn(retained, str(error))
        self.assertIn(pending["entity_id"], str(error))
        self.assertEqual(self.head(self.remote), initial)
        self.assert_failure(lambda: self.task(title="Should never be created"), "sync_pending")
        self.assert_failure(lambda: self.alice_store.call("task.list"), "sync_pending")
        with patch.object(SyncedGitStore, "_run", wraps=original) as observed:
            recovered = self.alice_store.retry_push()
        self.assertEqual(recovered["commit"], retained)
        self.assertEqual(recovered["entity_id"], pending["entity_id"])
        self.assertEqual(self.head(self.remote), retained)
        self.assertFalse(any(call.args[1] in {"pull", "commit"} for call in observed.call_args_list))
        self.assertEqual(len(GitStore(self.alice / "registry").call("task.list")), 1)
        self.assertFalse(self.marker(self.alice).exists())

    def test_push_response_loss_can_retry_already_accepted_commit(self):
        original = SyncedGitStore._push

        def accepted_then_response_lost(repository, state):
            original(self.alice_store, repository, state)
            raise DomainError("git_sync", "Push response was lost", 503)

        with patch.object(self.alice_store, "_push", side_effect=accepted_then_response_lost):
            self.assert_failure(lambda: self.task(), "git_sync")
        retained = self.head(self.alice)
        self.assertEqual(self.head(self.remote), retained)
        self.assertTrue(self.alice_store.retry_push()["pushed"])
        self.assertEqual(len(GitStore(self.alice / "registry").call("task.list")), 1)

    def test_actual_push_race_keeps_both_local_commits_and_never_forces_remote(self):
        original = SyncedGitStore._push
        bob_tasks = []

        def bob_wins_before_alice_push(repository, state):
            bob_tasks.append(self.task(self.bob_store, title="Bob wins push race"))
            original(self.alice_store, repository, state)

        with patch.object(self.alice_store, "_push", side_effect=bob_wins_before_alice_push):
            self.assert_failure(lambda: self.task(title="Alice retains local commit"), "git_sync")
        self.assertEqual(self.head(self.remote), self.head(self.bob))
        self.assertNotEqual(self.head(self.alice), self.head(self.remote))
        self.assert_failure(self.alice_store.retry_push, "git_sync")
        self.assertEqual(self.head(self.remote), self.head(self.bob))
        self.assertEqual(GitStore(self.alice / "registry").call("task.list")[0]["title"], "Alice retains local commit")
        self.assertTrue(self.marker(self.alice).exists())
        retained = self.head(self.alice)
        self.git(self.alice, "fetch", "origin")
        self.git(self.alice, "merge", "--no-edit", "origin/main")
        merged = self.head(self.alice)
        self.assert_failure(self.alice_store.retry_push)
        self.assert_failure(self.alice_store.acknowledge_sync)
        self.git(self.alice, "push", "origin", "HEAD:refs/heads/main")
        original_run = SyncedGitStore._run
        with patch.object(SyncedGitStore, "_run", wraps=original_run) as observed:
            resolved = self.alice_store.acknowledge_sync()
        self.assertEqual(resolved["commit"], merged)
        self.assertEqual(resolved["retained_commit"], retained)
        self.assertTrue(resolved["resolved"])
        self.assertFalse(any(call.args[1] in {"push", "pull", "commit", "merge"} for call in observed.call_args_list))
        self.assertFalse(self.marker(self.alice).exists())
        self.assertEqual(len(self.alice_store.call("task.list")), 2)

    def test_manual_resolution_refuses_dirty_tree_or_discarded_pending_commit(self):
        initial = self.head(self.alice)
        with patch.object(self.alice_store, "_push", side_effect=DomainError("git_sync", "offline", 503)):
            self.assert_failure(lambda: self.task(), "git_sync")
        unrelated = self.alice / "unrelated.txt"
        unrelated.write_text("Review this edit")
        self.assert_failure(self.alice_store.acknowledge_sync)
        self.assertTrue(self.marker(self.alice).exists())
        unrelated.unlink()
        self.git(self.alice, "reset", "--hard", initial)
        self.assert_failure(self.alice_store.acknowledge_sync)
        self.assertTrue(self.marker(self.alice).exists())

    def test_manual_resolution_checks_actual_push_destination_as_well_as_fetch(self):
        mirror = self.base / "push-destination.git"
        self.git(self.base, "clone", "--bare", str(self.remote), str(mirror))
        self.git(self.alice, "remote", "set-url", "--push", "origin", str(mirror))
        with patch.object(self.alice_store, "_push", side_effect=DomainError("git_sync", "offline", 503)):
            self.assert_failure(lambda: self.task(), "git_sync")
        self.git(self.alice, "push", str(self.remote), "HEAD:refs/heads/main")
        self.assertEqual(self.head(self.remote), self.head(self.alice))
        self.assertNotEqual(self.head(mirror), self.head(self.alice))
        self.assert_failure(self.alice_store.acknowledge_sync)
        self.assertTrue(self.marker(self.alice).exists())
        self.git(self.alice, "push", str(mirror), "HEAD:refs/heads/main")
        self.assertTrue(self.alice_store.acknowledge_sync()["resolved"])
        self.assertFalse(self.marker(self.alice).exists())

    def test_retry_refuses_changed_head_or_destination(self):
        with patch.object(self.alice_store, "_push", side_effect=DomainError("git_sync", "offline", 503)):
            self.assert_failure(lambda: self.task(), "git_sync")
        self.git(self.alice, "remote", "set-url", "--push", "origin", str(self.base / "different.git"))
        self.assert_failure(self.alice_store.retry_push)
        self.git(self.alice, "remote", "set-url", "--push", "origin", str(self.remote))
        self.git(self.alice, "commit", "--allow-empty", "-m", "Unrelated additional commit")
        self.assert_failure(self.alice_store.retry_push)

    def test_validation_failure_has_no_commit_push_or_pending_marker(self):
        initial = self.head(self.alice)
        original = SyncedGitStore._run
        with patch.object(SyncedGitStore, "_run", wraps=original) as observed:
            self.assert_failure(lambda: self.alice_store.call("task.create", {"project": "MISSING", "title": "Invalid", "kind": "task"}), "not_found")
        self.assertEqual(self.head(self.alice), initial)
        self.assertFalse(self.marker(self.alice).exists())
        self.assertFalse(any(call.args[1] in {"push", "commit"} for call in observed.call_args_list))

    def test_registry_ignored_by_git_does_not_succeed_without_publication(self):
        (self.alice / ".git" / "info" / "exclude").write_text("registry/tasks/\nregistry/events/\n")
        self.assert_failure(lambda: self.task())
        self.assertEqual(len(GitStore(self.alice / "registry").call("task.list")), 1)
        self.assertEqual(self.head(self.alice), self.head(self.remote))

    def test_post_commit_hook_unrelated_file_cannot_be_published(self):
        hooks = self.alice / ".git" / "hooks"
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'unrelated' > unexpected.txt\ngit add unexpected.txt\n")
        hook.chmod(0o755)
        self.git(self.alice, "config", "core.hooksPath", str(hooks))
        initial = self.head(self.remote)
        self.assert_failure(lambda: self.task())
        self.assertEqual(self.head(self.remote), initial)
        self.assertNotEqual(self.head(self.alice), initial)
        self.assertFalse(self.marker(self.alice).exists())

    def test_checkout_lock_does_not_dirty_tree_and_existing_lock_is_preserved(self):
        lock = self.alice / self.git(self.alice, "rev-parse", "--git-path", "teamtasks-sync.lock").stdout.strip()
        lock.mkdir()
        self.assert_failure(lambda: self.alice_store.call("project.list"))
        self.assertTrue(lock.is_dir())
        self.assertFalse(self.git(self.alice, "status", "--porcelain").stdout)
        lock.rmdir()
        self.assertEqual(len(self.alice_store.call("project.list")), 1)

    def test_linked_worktree_metadata_and_custom_registry_path(self):
        worktree = self.base / "linked"
        self.git(self.alice, "worktree", "add", "-b", "linked", str(worktree), "main")
        self.git(worktree, "branch", "--set-upstream-to=origin/main", "linked")
        self.assertEqual(len(SyncedGitStore(worktree / "registry").call("project.list")), 1)
        self.assertFalse(self.git(worktree, "status", "--porcelain").stdout)
        custom = SyncedGitStore(worktree / "shared [tasks]" / "registry")
        self.assertEqual(custom.call("project.create", {"key": "OTHER"})["key"], "OTHER")
        self.assertEqual(self.head(self.remote), self.head(worktree))


if __name__ == "__main__":
    unittest.main()
