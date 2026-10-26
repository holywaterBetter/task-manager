"""Git synchronization boundary for every shared registry operation.

The lock is local to one checkout. Git rejects races between PCs; a rejected
push keeps the exact local commit and requires an explicit push-only retry.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from uuid import uuid4

from .gitstore import COLLECTIONS, GitStore, MUTATIONS
from .store import DomainError, Store


def _precondition(message):
    return DomainError("sync_precondition", message, 409)


class SyncedGitStore:
    """Pull, execute, commit changed records, then push one upstream branch.

    No constructor side effects, automatic merges, force pushes, stashes, or
    publishing of pre-existing local commits. An explicit local-only GitStore
    remains available for offline inspection and deliberate manual recovery.
    """

    actions = Store.actions
    sync_required = True
    _operations = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer")

    def __init__(self, root: Path = Path("registry")):
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        self._store = GitStore(self.root)

    def close(self):
        self._store.close()

    @staticmethod
    def _run(directory, *arguments, check=True, message="Git operation failed; check repository access and configuration."):
        try:
            result = subprocess.run(
                ["git", "-C", str(directory), *arguments], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", timeout=30,
                env={**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
                     "GIT_OPTIONAL_LOCKS": "0"},
            )
        except FileNotFoundError as exc:
            raise _precondition("Git is required for shared operations; no records were changed.") from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            # Never expose stderr, command arguments, or remote URLs: these may
            # contain credentials from helpers, hooks, or repository settings.
            raise DomainError("git_sync", message + " The Git operation could not finish.", 503) from exc
        if check and result.returncode:
            raise DomainError("git_sync", message, 503)
        return result

    def _repository(self):
        GitStore._safe_directory(self.root)
        directory = self.root
        while not directory.exists() and directory != directory.parent:
            directory = directory.parent
        result = self._run(directory, "rev-parse", "--show-toplevel", check=False)
        if result.returncode:
            raise _precondition("Shared operations require a Git checkout with a configured upstream branch.")
        repository = Path(result.stdout.strip()).absolute()
        try:
            relative = self.root.relative_to(repository)
        except ValueError as exc:
            raise _precondition("Registry must be inside the Git working tree.") from exc
        if not relative.parts or relative.parts[0] == ".git":
            raise _precondition("Registry must be a subdirectory of the Git working tree, outside Git metadata.")
        return repository

    def _metadata(self, repository, name):
        result = self._run(repository, "rev-parse", "--git-path", name)
        path = Path(result.stdout.strip())
        return path if path.is_absolute() else repository / path

    @contextmanager
    def _locked(self, repository):
        lock = self._metadata(repository, "teamtasks-sync.lock")
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise _precondition(
                "Another shared operation holds the local synchronization lock. "
                "Wait; remove an abandoned Git metadata lock only after verifying no operation is running."
            ) from exc
        identity = lock.stat()
        try:
            yield
        finally:
            try:
                current = lock.lstat()
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino) or not stat.S_ISDIR(current.st_mode):
                    raise _precondition("The local synchronization lock changed; inspect this checkout before continuing.")
                lock.rmdir()
            except FileNotFoundError:
                pass

    def _head(self, repository):
        return self._run(repository, "rev-parse", "--verify", "HEAD").stdout.strip()

    def _state(self, repository):
        branch = self._run(repository, "symbolic-ref", "--quiet", "HEAD", check=False)
        if branch.returncode or not branch.stdout.strip().startswith("refs/heads/"):
            raise _precondition("Switch to a local branch with a configured upstream before shared operations.")
        branch = branch.stdout.strip()
        values = self._run(
            repository, "for-each-ref", "--format=%(refname)%00%(upstream:remotename)%00%(upstream:remoteref)%00%(upstream)", branch,
        ).stdout.rstrip("\n").split("\0")
        if len(values) != 4 or values[0] != branch:
            raise _precondition("Configure one remote upstream branch before shared operations.")
        _, remote, remote_ref, upstream = values
        merges = self._run(repository, "config", "--get-all", f"branch.{branch[11:]}.merge", check=False).stdout.splitlines()
        if (not remote or remote == "." or remote.startswith("-") or remote not in self._run(repository, "remote").stdout.splitlines()
                or not remote_ref.startswith("refs/heads/") or not upstream or merges != [remote_ref]):
            raise _precondition("Configure exactly one remote upstream branch before shared operations.")
        if self._run(repository, "rev-parse", "--verify", "--quiet", upstream + "^{commit}", check=False).returncode:
            raise _precondition("Fetch the configured upstream branch before shared operations.")
        fetch_urls = self._run(repository, "remote", "get-url", "--all", remote).stdout.splitlines()
        push_urls = self._run(repository, "remote", "get-url", "--push", "--all", remote).stdout.splitlines()
        if len(fetch_urls) != 1 or len(push_urls) != 1:
            raise _precondition("The upstream remote must have exactly one fetch and one push destination.")
        # Keep credentials out of metadata and diagnostics while detecting a
        # changed destination before retrying a retained commit.
        destination = hashlib.sha256(json.dumps([fetch_urls, push_urls]).encode()).hexdigest()
        return {"branch": branch, "remote": remote, "remote_ref": remote_ref, "upstream": upstream, "destination": destination}

    def _status(self, repository):
        raw = self._run(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        paths = []
        entries = iter(raw.split("\0"))
        for entry in entries:
            if not entry:
                continue
            if len(entry) < 4 or entry[2] != " ":
                raise _precondition("Cannot interpret Git working tree status safely.")
            paths.append(entry[3:])
            if "R" in entry[:2] or "C" in entry[:2]:
                original = next(entries, None)
                if not original:
                    raise _precondition("Cannot interpret Git rename status safely.")
                paths.append(original)
        return paths

    def _preflight(self, repository, *, allow_ahead=False):
        for operation in self._operations:
            if self._metadata(repository, operation).exists():
                raise _precondition("Finish or abort the active Git merge, rebase, cherry-pick, or revert before shared operations.")
        if self._run(repository, "ls-files", "--unmerged", "-z").stdout:
            raise _precondition("Resolve and stage all Git index conflicts before shared operations.")
        if self._status(repository):
            raise _precondition("The entire Git working tree must be clean. Commit or move existing edits before shared operations.")
        state = self._state(repository)
        if not allow_ahead and self._run(repository, "rev-list", "--count", state["upstream"] + "..HEAD").stdout.strip() != "0":
            raise _precondition("Unpublished local commits exist. Review and synchronize them manually before shared operations.")
        return state

    @staticmethod
    def _records(snapshot):
        return GitStore._records(snapshot)

    def _relative_paths(self, repository, before, after):
        prefix = self.root.relative_to(repository)
        return sorted((prefix / path).as_posix() for path in before.keys() | after.keys() if before.get(path) != after.get(path))

    @staticmethod
    def _same_state(first, second):
        if first != second:
            raise _precondition("The current branch or upstream configuration changed during synchronization; inspect this checkout.")

    def _validate_commit(self, repository, previous, commit, expected):
        parents = self._run(repository, "rev-list", "--parents", "-n", "1", commit).stdout.split()
        changed = self._run(repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", previous, commit, "--").stdout
        if parents != [commit, previous] or sorted(filter(None, changed.split("\0"))) != expected:
            raise _precondition(
                "The new commit differs from the validated registry changes; no push was attempted. "
                "Local changes or commit are retained. Inspect hooks and recover manually; do not repeat the action."
            )

    @staticmethod
    def _pending_error():
        return DomainError(
            "sync_pending", "A previous operation has an unconfirmed push. Local changes or commit are retained; "
            "do not repeat the action or create. Use sync-retry to push the retained commit only.", 409,
        )

    def _write_pending(self, marker, data):
        if marker.exists() or marker.is_symlink():
            raise self._pending_error()
        descriptor, temporary = tempfile.mkstemp(prefix=".teamtasks-pending-", dir=marker.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, marker)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _push(self, repository, state):
        self._run(
            repository, "push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", "--",
            state["remote"], "HEAD:" + state["remote_ref"],
            message="Push was not confirmed. Local changes or commit are retained; do not repeat the action or create. "
                    "Use sync-retry after restoring access. If the remote advanced, resolve the Git conflict manually.",
        )

    def _push_pending(self, repository, state, pending):
        try:
            self._push(repository, state)
        except DomainError as exc:
            detail = f" Retained commit: {pending['commit']}; operation: {pending['operation_id']}."
            if pending.get("entity_id") is not None:
                detail += f" Entity: {pending['entity_id']}."
            raise DomainError(exc.code, str(exc) + detail, exc.status) from exc

    def _read_pending(self, repository, marker):
        try:
            if marker.is_symlink() or not marker.is_file():
                raise ValueError("no regular recovery marker")
            pending = json.loads(marker.read_text(encoding="utf-8"))
            commit_pattern = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
            if (not isinstance(pending, dict) or pending.get("version") != 1
                    or not isinstance(pending.get("commit"), str) or not re.fullmatch(commit_pattern, pending["commit"])
                    or not isinstance(pending.get("previous"), str) or not re.fullmatch(commit_pattern, pending["previous"])
                    or not isinstance(pending.get("operation_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", pending["operation_id"])
                    or pending.get("action") not in self.actions or not isinstance(pending.get("actor"), str)
                    or pending.get("registry") != self.root.relative_to(repository).as_posix()
                    or not isinstance(pending.get("paths"), list)):
                raise ValueError("invalid recovery marker")
            prefix = self.root.relative_to(repository)
            for path in pending["paths"]:
                if not isinstance(path, str):
                    raise ValueError("invalid pending path")
                relative = Path(path)
                record = relative.relative_to(prefix)
                if (len(record.parts) != 2 or record.parts[0] not in COLLECTIONS or record.suffix != ".json"
                        or ".." in relative.parts or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", record.stem)):
                    raise ValueError("unexpected pending path")
            if pending["paths"] != sorted(set(pending["paths"])):
                raise ValueError("invalid pending path order")
            if not pending["paths"] and pending["previous"] != pending["commit"]:
                raise ValueError("invalid pending read commit")
        except (OSError, UnicodeError, ValueError) as exc:
            raise _precondition("No valid pending push exists for this registry; inspect Git state and recover manually.") from exc
        return pending

    def _validate_pending_commit(self, repository, pending):
        if pending["paths"]:
            self._validate_commit(repository, pending["previous"], pending["commit"], pending["paths"])

    def call(self, action: str, params: dict | None = None, actor: str = "local"):
        repository = self._repository()
        with self._locked(repository):
            marker = self._metadata(repository, "teamtasks-sync-pending.json")
            if marker.exists() or marker.is_symlink():
                raise self._pending_error()
            state = self._preflight(repository)
            mutation = isinstance(action, str) and action.rsplit(".", 1)[-1] in MUTATIONS
            if mutation:
                for identity in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
                    if self._run(repository, "var", identity, check=False).returncode:
                        raise _precondition("Configure Git user.name and user.email before changing shared records.")
            self._run(
                repository, "pull", "--ff-only", "--no-rebase", "--", state["remote"], state["remote_ref"],
                message="Pull failed; no business action was attempted. Restore Git access or resolve branch divergence, then retry.",
            )
            self._same_state(state, self._preflight(repository))
            previous = self._head(repository)
            before = self._records(self._store.call("snapshot", actor=actor))
            result = self._store.call(action, params, actor=actor)
            after = self._records(self._store.call("snapshot", actor=actor))
            expected = self._relative_paths(repository, before, after)
            actual = self._status(repository)
            if sorted(actual) != expected:
                raise _precondition(
                    "Working tree changes do not match the validated registry records (check ignored files or external edits). "
                    "Local changes are retained; inspect the checkout before repeating the action."
                )
            self._same_state(state, self._state(repository))
            if self._head(repository) != previous:
                raise _precondition("Git HEAD changed during the action. Local records are retained; inspect the checkout before repeating the action.")
            if expected:
                self._run(repository, "--literal-pathspecs", "add", "--", *expected,
                          message="Could not stage registry records. Local changes are retained; inspect the checkout before repeating the action.")
                staged = self._run(repository, "diff", "--cached", "--name-only", "-z").stdout
                if sorted(filter(None, staged.split("\0"))) != expected:
                    raise _precondition("The index changed during the action. Local records are retained; inspect the checkout before repeating the action.")
                self._run(repository, "commit", "-m", f"teamtasks: {action}",
                          message="Could not commit registry records. Local changes are retained; fix Git identity or hooks and recover manually. Do not repeat the action.")
                commit = self._head(repository)
                self._validate_commit(repository, previous, commit, expected)
                self._same_state(state, self._preflight(repository, allow_ahead=True))
                if self._records(self._store.call("snapshot", actor=actor)) != after:
                    raise _precondition("Registry records changed during commit. The local commit is retained; inspect hooks before publishing or repeating the action.")
            else:
                commit = previous
            pending = {"version": 1, "operation_id": uuid4().hex, "action": action, "actor": actor,
                       "commit": commit, "previous": previous, "paths": expected, "state": state,
                       "registry": self.root.relative_to(repository).as_posix(),
                       "at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
            if isinstance(result, dict):
                pending["entity_id"] = result.get("id", result.get("key"))
            # Write before push: even an accepted push whose response is lost
            # can be retried without executing the business action twice.
            self._write_pending(marker, pending)
            self._push_pending(repository, state, pending)
            marker.unlink()
            return result

    def retry_push(self):
        """Retry the exact retained commit; never rerun a business action."""
        repository = self._repository()
        with self._locked(repository):
            marker = self._metadata(repository, "teamtasks-sync-pending.json")
            pending = self._read_pending(repository, marker)
            state = self._preflight(repository, allow_ahead=True)
            self._same_state(pending.get("state"), state)
            if self._head(repository) != pending["commit"]:
                raise _precondition("HEAD changed since the failed push. Review, merge, and push Git manually, then use sync-resolved. "
                                    "sync-retry will not publish a different commit.")
            self._store.call("snapshot")
            self._validate_pending_commit(repository, pending)
            self._push_pending(repository, state, pending)
            marker.unlink()
            result = {"commit": pending["commit"], "pushed": True, "action": pending.get("action")}
            if pending.get("entity_id") is not None:
                result["entity_id"] = pending["entity_id"]
            return result

    def acknowledge_sync(self):
        """Verify a reviewed manual merge/push, then clear its pending marker.

        This does not push, pull, merge, or commit. Both fetch and push
        destinations must already expose the exact clean local HEAD.
        """
        repository = self._repository()
        with self._locked(repository):
            marker = self._metadata(repository, "teamtasks-sync-pending.json")
            pending = self._read_pending(repository, marker)
            state = self._preflight(repository, allow_ahead=True)
            self._same_state(pending.get("state"), state)
            current = self._head(repository)
            if self._run(repository, "merge-base", "--is-ancestor", pending["commit"], current, check=False).returncode:
                raise _precondition("The retained commit is not part of HEAD. Preserve and reconcile the pending work before sync-resolved.")
            self._validate_pending_commit(repository, pending)
            self._store.call("snapshot")
            fetch_urls = self._run(repository, "remote", "get-url", "--all", state["remote"]).stdout.splitlines()
            push_urls = self._run(repository, "remote", "get-url", "--push", "--all", state["remote"]).stdout.splitlines()
            if len(fetch_urls) != 1 or len(push_urls) != 1:
                raise _precondition("The upstream remote configuration changed; review it before sync-resolved.")
            for destination in dict.fromkeys(fetch_urls + push_urls):
                result = self._run(
                    repository, "ls-remote", "--exit-code", "--refs", "--", destination, state["remote_ref"],
                    message="Could not verify the upstream branch. The pending marker is retained; restore access before sync-resolved.",
                )
                rows = [row.split("\t") for row in result.stdout.splitlines()]
                if rows != [[current, state["remote_ref"]]]:
                    raise _precondition("Fetch and push destinations must both contain the exact local HEAD. "
                                        "Review and push the merged work manually before sync-resolved; the pending marker is retained.")
            self._same_state(state, self._preflight(repository, allow_ahead=True))
            if self._head(repository) != current:
                raise _precondition("HEAD changed while verifying the remote. The pending marker is retained; inspect Git before sync-resolved.")
            marker.unlink()
            result = {"resolved": True, "commit": current, "retained_commit": pending["commit"], "action": pending["action"]}
            if pending.get("entity_id") is not None:
                result["entity_id"] = pending["entity_id"]
            return result
