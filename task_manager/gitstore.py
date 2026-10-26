"""File-per-record Git registry with a disposable, validated SQLite view.

This adapter never synchronizes, stages, or commits Git changes. The directory
lock serializes callers in one checkout; it cannot lock another developer's PC.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .store import DomainError, Store


COLLECTIONS = {"projects": "key", "tasks": "id", "docs": "id", "events": "id"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
MERGE_MARKER = re.compile(r"^(?:<<<<<<<(?: .*)?|=======|>>>>>>>(?: .*)?)\r?$", re.MULTILINE)
MUTATIONS = {"create", "update", "claim", "release", "depend"}


def _conflict(message: str) -> DomainError:
    return DomainError("conflict", message, 409)


def _identifier(value, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise _conflict(f"Unsafe {label}: {value!r}")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON property: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


class GitStore:
    """Expose ``Store.call`` over canonical JSON files in a Git checkout.

    Constructing a store does not create anything. A call creates the registry
    root if necessary so even the first read can take a local directory lock.
    Canonical collection directories are created only when a record is written.
    """

    actions = Store.actions

    def __init__(self, root: Path = Path("registry")):
        self.root = Path(root).expanduser().absolute()

    def close(self):
        """Compatibility with Store; no connection survives a call."""

    @staticmethod
    def _safe_directory(path: Path):
        for item in reversed((path, *path.parents)):
            try:
                mode = item.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise _conflict(f"Registry directory must be a real directory: {item}")

    @contextmanager
    def _locked(self):
        self._safe_directory(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".write-lock"
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise _conflict(
                f"Registry is locked at {lock}. Wait for the active local operation; "
                "remove an abandoned lock manually only after verifying no writer is running."
            ) from exc
        lock_identity = lock.stat()
        try:
            yield
        finally:
            # Never remove a pre-existing lock or follow a replacement symlink.
            try:
                current = lock.lstat()
                if (current.st_dev, current.st_ino) != (lock_identity.st_dev, lock_identity.st_ino) or not stat.S_ISDIR(current.st_mode):
                    raise _conflict(f"Registry lock was replaced during operation: {lock}")
                lock.rmdir()
            except FileNotFoundError:
                pass

    def _read_snapshot(self):
        snapshot = {collection: [] for collection in COLLECTIONS}
        originals = {}
        for collection, key in COLLECTIONS.items():
            folder = self.root / collection
            self._safe_directory(folder)
            if not folder.exists():
                continue
            for path in sorted(folder.iterdir()):
                mode = path.lstat().st_mode
                if not stat.S_ISREG(mode) or path.suffix != ".json":
                    raise _conflict(f"Unexpected registry entry; expected a regular JSON file: {path}")
                identity = _identifier(path.stem, f"{collection} filename")
                try:
                    raw = path.read_bytes()
                    body = raw.decode("utf-8")
                    if MERGE_MARKER.search(body):
                        raise ValueError("unresolved Git merge markers")
                    record = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                except (UnicodeError, ValueError, RecursionError) as exc:
                    raise _conflict(f"Invalid registry JSON in {path}: {exc}") from exc
                if not isinstance(record, dict) or record.get(key) != identity:
                    raise _conflict(f"Registry filename and record {key} disagree: {path}")
                snapshot[collection].append(record)
                originals[path] = raw
        return snapshot, originals

    def _ensure_git_resolved(self):
        """Read the index, because removing markers does not resolve Git's index."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "ls-files", "--unmerged", "--", "."],
                capture_output=True, text=True, timeout=10, check=False,
                env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
            )
        except FileNotFoundError as exc:
            if any((parent / ".git").exists() for parent in (self.root, *self.root.parents)):
                raise _conflict("Git is required to check this checkout for unresolved index conflicts.") from exc
            return
        except subprocess.TimeoutExpired as exc:
            raise _conflict("Timed out checking the local Git index; no records were changed.") from exc
        if result.returncode:
            if "not a git repository" in result.stderr:
                return
            raise _conflict("Cannot inspect the local Git index; resolve repository configuration before writing.")
        if result.stdout.strip():
            raise _conflict("Registry has unresolved Git index entries. Resolve and stage the merge before writing.")

    @staticmethod
    def _records(snapshot):
        indexed = {}
        for collection, key in COLLECTIONS.items():
            for record in snapshot[collection]:
                identity = _identifier(record.get(key), f"{collection} record ID")
                relative = Path(collection) / f"{identity}.json"
                if relative in indexed:
                    raise _conflict(f"Duplicate registry record: {relative}")
                indexed[relative] = record
        return indexed

    @staticmethod
    def _atomic_write(path: Path, raw: bytes):
        GitStore._safe_directory(path.parent)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise _conflict(f"Refusing to replace non-regular registry file: {path}")
        descriptor, temporary = tempfile.mkstemp(prefix=".record-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if path.is_symlink():
                raise _conflict(f"Refusing to replace registry symlink: {path}")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _persist(self, before, after, originals):
        old, new = self._records(before), self._records(after)
        # Existing events are immutable even if a future domain action regresses.
        for relative, record in old.items():
            if relative.parts[0] == "events" and new.get(relative) != record:
                raise _conflict(f"Audit events are append-only: {relative}")
        changed = sorted(relative for relative in old.keys() | new.keys() if old.get(relative) != new.get(relative))
        if not changed:
            return
        encoded = {
            relative: (json.dumps(new[relative], ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
            for relative in changed if relative in new
        }
        created_directories = []
        applied = []
        try:
            for relative in changed:
                path = self.root / relative
                self._safe_directory(path.parent)
                if not path.parent.exists():
                    path.parent.mkdir()
                    created_directories.append(path.parent)
                # Detect an external editor or Git operation ignoring our lock.
                expected = originals.get(path)
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise _conflict(f"Registry file changed during operation: {path}")
                actual = path.read_bytes() if path.exists() else None
                if actual != expected:
                    raise _conflict(f"Registry file changed during operation: {path}")
                applied.append(path)
                if relative in new:
                    self._atomic_write(path, encoded[relative])
                else:
                    path.unlink()
        except Exception as exc:
            rollback_errors = []
            for path in reversed(applied):
                try:
                    if path in originals:
                        self._atomic_write(path, originals[path])
                    elif path.exists() and not path.is_symlink():
                        path.unlink()
                except Exception:
                    rollback_errors.append(str(path))
            if rollback_errors:
                raise _conflict("Local write failed and rollback needs manual recovery: " + ", ".join(rollback_errors)) from exc
            raise
        finally:
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass

    def call(self, action: str, params: dict | None = None, actor: str = "local"):
        with self._locked():
            snapshot, originals = self._read_snapshot()
            store = Store()
            try:
                try:
                    store.load_snapshot(snapshot)
                except (DomainError, ValueError, KeyError, TypeError, RecursionError) as exc:
                    raise _conflict(f"Registry records conflict or contain invalid references: {exc}") from exc
                mutation = isinstance(action, str) and action.rsplit(".", 1)[-1] in MUTATIONS
                if mutation:
                    self._ensure_git_resolved()
                result = store.call(action, params, actor=actor)
                if mutation:
                    after = store.call("snapshot", {}, actor=actor)
                    self._persist(snapshot, after, originals)
                return result
            finally:
                store.close()
