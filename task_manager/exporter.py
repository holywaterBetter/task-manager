"""Read-only Obsidian projections of the Git-backed TeamTasks registry.

The Git registry remains authoritative. Only files listed in this export's
manifest and bearing its generated marker may be replaced or removed.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any


MARKER = '---\nteamtasks_generated: "teamtasks-export-v1"\n'
OWNER = "teamtasks-export-v1"
MANIFEST = ".manifest.json"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
RESERVED_NAME = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
OUTPUT_PATH = re.compile(r"^(?:Home\.md|Schedule\.md|(?:Projects|Tasks|Docs)/[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.md)$")


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or RESERVED_NAME.fullmatch(value):
        raise ValueError(f"Invalid canonical ID: {value!r}")
    return value


def _text(value: Any) -> str:
    return str(value if value is not None else "")


def _inline(value: Any) -> str:
    return _text(value).replace("\\", "\\\\").replace("\n", " ").replace("\r", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")


def _yaml(value: Any) -> str:
    # JSON double-quoted strings are valid YAML scalars and cannot inject keys.
    return json.dumps(_text(value), ensure_ascii=False)


def _number(value: Any, default: float = 1.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Task weight/estimate must be a finite non-negative number")
    return float(value)


def _index(records: Any, key: str) -> dict[str, dict]:
    if not isinstance(records, list):
        raise ValueError("Snapshot collections must be lists")
    result: dict[str, dict] = {}
    portable_names: set[str] = set()
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("Snapshot records must be objects")
        identifier = _identifier(row.get(key))
        if identifier.casefold() in portable_names:
            raise ValueError(f"Duplicate canonical ID: {identifier}")
        portable_names.add(identifier.casefold())
        result[identifier] = row
    return dict(sorted(result.items()))


def _references(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return sorted(set(_identifier(item) for item in value))


def _acyclic(graph: dict[str, list[str]], label: str) -> None:
    # Iterative DFS also handles legitimate long task/document chains.
    complete: set[str] = set()
    for root in graph:
        if root in complete:
            continue
        active: set[str] = set()
        stack = [(root, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                active.remove(node)
                complete.add(node)
            elif node not in complete:
                if node in active:
                    raise ValueError(f"Cycle in {label}: {node}")
                active.add(node)
                stack.append((node, True))
                stack.extend((other, False) for other in reversed(graph[node]))


def _link(folder: str, identifier: str, title: Any, prefix: str = "../") -> str:
    return f"[{_inline(title)}]({prefix}{folder}/{identifier}.md)"


def _frontmatter(record_type: str, identifier: str, title: Any, **fields: Any) -> str:
    properties = {"id": identifier, "title": title, "type": record_type, "read_only": True, **fields}
    lines = [MARKER.rstrip()]
    for key, value in properties.items():
        lines.append(f"{key}: {'true' if value is True else _yaml(value)}")
    lines += ["---", "", f"# {_inline(title)}", "", "> 조회 전용 · 자동 생성. 원본은 Git registry의 JSON 기록입니다. 수정은 프로젝트 CLI/MCP에서 수행하고 Git으로 공유하세요.", ""]
    return "\n".join(lines)


def _body_section(title: str, body: Any) -> str:
    return f"\n## {title}\n\n{_text(body).strip() or '—'}\n"


def _tree(rows: dict[str, dict], folder: str, project: str, prefix: str) -> str:
    children: dict[str | None, list[str]] = {}
    for identifier, row in rows.items():
        if row["project"] == project:
            children.setdefault(row.get("parent"), []).append(identifier)
    lines: list[str] = []
    stack = [(identifier, 0) for identifier in reversed(children.get(None, []))]
    while stack:
        identifier, level = stack.pop()
        row = rows[identifier]
        details = f" · {_inline(row.get('kind', '문서'))} · {_inline(row.get('status', ''))}" if folder == "Tasks" else ""
        lines.append("  " * level + f"- {_link(folder, identifier, row['title'], prefix)}{details}")
        stack.extend((child, level + 1) for child in reversed(children.get(identifier, [])))
    return "\n".join(lines) or "—"


def _assert_safe_chain(path: Path) -> None:
    for item in reversed((path, *path.parents)):
        if item.is_symlink():
            raise ValueError(f"Symlink export paths are not allowed: {item}")
        if item.exists() and not item.is_dir():
            raise ValueError(f"Expected export directory: {item}")


def _owned_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("r", encoding="utf-8") as handle:
        return handle.read(len(MARKER)) == MARKER


def _atomic_write(path: Path, body: str) -> None:
    _assert_safe_chain(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=".teamtasks-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ValueError(f"Symlink export file is not allowed: {path}")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def export_vault(snapshot: dict, destination: Path) -> dict:
    """Export a validated snapshot to ``destination/TeamTasks``.

    Returns counts and leaf-weighted progress. No store mutation or inbound
    sync is performed. Existing unrelated vault files are always preserved.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot must be an object")
    projects = _index(snapshot.get("projects", []), "key")
    tasks = _index(snapshot.get("tasks", []), "id")
    docs = _index(snapshot.get("docs", []), "id")
    for collection, label in ((tasks, "task parent tree"), (docs, "document parent tree")):
        parents: dict[str, list[str]] = {}
        for identifier, row in collection.items():
            project = _identifier(row.get("project"))
            if project not in projects:
                raise ValueError(f"Missing project: {project}")
            parent = row.get("parent")
            if parent is not None:
                parent = _identifier(parent)
                if parent not in collection or collection[parent]["project"] != project:
                    raise ValueError(f"Missing or cross-project parent: {parent}")
            parents[identifier] = [parent] if parent else []
        _acyclic(parents, label)
    dependencies: dict[str, list[str]] = {}
    followers: dict[str, list[str]] = {identifier: [] for identifier in tasks}
    document_tasks: dict[str, list[str]] = {identifier: [] for identifier in docs}
    for identifier, row in tasks.items():
        _number(row.get("weight"))
        _number(row.get("estimate"))
        dependencies[identifier] = _references(row.get("depends_on"), "depends_on")
        for dependency in dependencies[identifier]:
            if dependency not in tasks:
                raise ValueError(f"Missing dependency: {dependency}")
            followers[dependency].append(identifier)
        for doc_id in _references(row.get("doc_ids"), "doc_ids"):
            if doc_id not in docs:
                raise ValueError(f"Missing document: {doc_id}")
            document_tasks[doc_id].append(identifier)
    _acyclic(dependencies, "dependencies")

    parent_ids = {row.get("parent") for row in tasks.values()}
    progress: dict[str, dict] = {}
    allocation_tables: dict[str, str] = {}
    for project in projects:
        leaves = [row for identifier, row in tasks.items() if row["project"] == project and identifier not in parent_ids and row.get("status") != "cancelled"]
        total = sum(_number(row.get("weight")) * _number(row.get("estimate")) for row in leaves)
        done = sum(_number(row.get("weight")) * _number(row.get("estimate")) for row in leaves if row.get("status") == "done")
        progress[project] = {"leaf_tasks": len(leaves), "total_weight": total, "done_weight": done, "percent": round(100 * done / total, 2) if total else 0.0}
        allocations: dict[str, float] = {}
        for row in leaves:
            if row.get("status") != "done":
                continue
            shares = row.get("contributions") or {}
            if not isinstance(shares, dict):
                raise ValueError("contributions must be an object")
            for actor, share in shares.items():
                share = _number(share, 0)
                if share > 1:
                    raise ValueError("Contribution shares must be between 0 and 1")
                if share > 0:
                    allocations[actor] = allocations.get(actor, 0) + _number(row.get("weight")) * _number(row.get("estimate")) * share
        table = "\n### 합의된 완료 업무 배분\n\n"
        if allocations:
            table += "| 작업자 | 완료 배분량 |\n| --- | ---: |\n"
            table += "".join(f"| {_inline(actor)} | {amount:g} |\n" for actor, amount in sorted(allocations.items()))
            table += "\n완료한 최하위 업무의 비중 × 예상 규모 × 합의 비율입니다. 개인 역량 점수가 아닙니다.\n"
        else:
            table += "아직 집계할 완료 배분 기록이 없습니다.\n"
        allocation_tables[project] = table

    files: dict[str, str] = {}
    home = _frontmatter("index", "TeamTasks", "TeamTasks · 팀 업무", snapshot_at=snapshot.get("generated_at"))
    home += "\n[일정과 담당자](Schedule.md)\n\n진행률은 취소되지 않은 최하위 업무의 완료 가중치 ÷ 전체 가중치입니다. 가중치는 중요도(weight) × 예상 작업량(estimate)이며 개인 성과 평가 점수가 아닙니다.\n"
    for key, project in projects.items():
        name = project.get("name", key)
        stats = progress[key]
        project_link = _link("Projects", key, name, "")
        home += f"\n## {project_link}\n\n완료 진행률: **{stats['percent']:g}%** ({stats['done_weight']:g}/{stats['total_weight']:g})\n\n"
        home += _tree(tasks, "Tasks", key, "") + "\n"
        home += allocation_tables[key]
        content = _frontmatter("project", key, name, project=key)
        content += _body_section("설명", project.get("description"))
        content += f"\n완료 진행률: **{stats['percent']:g}%** ({stats['done_weight']:g}/{stats['total_weight']:g})\n"
        content += allocation_tables[key]
        content += "\n## 업무 트리\n\n" + _tree(tasks, "Tasks", key, "../") + "\n"
        content += "\n## 지식 문서 트리\n\n" + _tree(docs, "Docs", key, "../") + "\n"
        files[f"Projects/{key}.md"] = content
    if snapshot.get("report") is not None:
        home += _body_section("저장소 리포트", "```json\n" + json.dumps(snapshot["report"], ensure_ascii=False, indent=2, sort_keys=True) + "\n```")
    files["Home.md"] = home

    schedule = _frontmatter("schedule", "Schedule", "일정과 담당자")
    schedule += "\n| 업무 | 담당자 | 상태 | 우선순위 | 중요 비중 | 예상 규모 | 시작 | 마감 | 선행 업무 |\n| --- | --- | --- | --- | ---: | ---: | --- | --- | --- |\n"
    for identifier, row in sorted(tasks.items(), key=lambda pair: (_text(pair[1].get("due_date")) or "9999", pair[0])):
        links = ", ".join(_link("Tasks", item, item, "") for item in dependencies[identifier]) or "—"
        schedule += "| " + " | ".join([_link("Tasks", identifier, f"{identifier} · {row['title']}", ""), _inline(row.get("assignee")) or "미지정", _inline(row.get("status")), _inline(row.get("priority")), f"{_number(row.get('weight')):g}", f"{_number(row.get('estimate')):g}", _inline(row.get("start_date")) or "—", _inline(row.get("due_date")) or "—", links]) + " |\n"
    schedule += "\n## 일정 확인\n\n선행 업무 마감일과 후행 업무 시작일이 같은 날이면 허용합니다. 자동으로 일정을 변경하지 않습니다.\n\n"
    warnings = []
    for identifier, row in tasks.items():
        if row.get("status") == "cancelled":
            continue
        for dependency in dependencies[identifier]:
            due, start = tasks[dependency].get("due_date"), row.get("start_date")
            if due and start and due > start:
                warnings.append(f"- {_link('Tasks', identifier, identifier, '')}: 선행 {_link('Tasks', dependency, dependency, '')} 마감 {_inline(due)}가 시작 {_inline(start)}보다 늦습니다.")
    schedule += "\n".join(warnings) if warnings else "확인된 선후행 날짜 역전이 없습니다. 날짜가 없는 업무는 비교하지 않습니다.\n"
    files["Schedule.md"] = schedule

    for identifier, row in tasks.items():
        content = _frontmatter("task", identifier, row["title"], project=row["project"], kind=row.get("kind"), status=row.get("status"), assignee=row.get("assignee"), priority=row.get("priority"), weight=row.get("weight", 1), estimate=row.get("estimate", 1), start_date=row.get("start_date"), due_date=row.get("due_date"), version=row.get("version"))
        content += "\n프로젝트: " + _link("Projects", row["project"], projects[row["project"]].get("name", row["project"])) + "\n"
        if row.get("parent"):
            parent = row["parent"]
            content += "\n상위 업무: " + _link("Tasks", parent, tasks[parent]["title"]) + "\n"
        for title, ids in (("하위 업무", [item for item in tasks if tasks[item].get("parent") == identifier]), ("선행 업무", dependencies[identifier]), ("후행 업무", followers[identifier])):
            content += f"\n## {title}\n\n" + ("\n".join("- " + _link("Tasks", item, f"{item} · {tasks[item]['title']}") for item in ids) or "—") + "\n"
        for title, field in (("설명", "description"), ("프롬프트", "prompt"), ("플랜", "plan"), ("결과", "result")):
            content += _body_section(title, row.get(field))
        evidence = row.get("evidence") or []
        if not isinstance(evidence, list):
            raise ValueError("evidence must be a list")
        content += _body_section("검증 근거", "\n".join("- " + (_text(item) if isinstance(item, str) else json.dumps(item, ensure_ascii=False, sort_keys=True)) for item in evidence))
        content += "\n## 관련 지식\n\n" + ("\n".join("- " + _link("Docs", item, docs[item]["title"]) for item in _references(row.get("doc_ids"), "doc_ids")) or "—") + "\n"
        if row.get("contributions"):
            content += _body_section("기여 기록", "```json\n" + json.dumps(row["contributions"], ensure_ascii=False, indent=2, sort_keys=True) + "\n```")
        files[f"Tasks/{identifier}.md"] = content

    for identifier, row in docs.items():
        content = _frontmatter("document", identifier, row["title"], project=row["project"], canonical=row.get("canonical"), version=row.get("version"))
        content += "\n프로젝트: " + _link("Projects", row["project"], projects[row["project"]].get("name", row["project"])) + "\n"
        if row.get("parent"):
            parent = row["parent"]
            content += "\n상위 문서: " + _link("Docs", parent, docs[parent]["title"]) + "\n"
        child_docs = [item for item in docs if docs[item].get("parent") == identifier]
        content += "\n## 하위 문서\n\n" + ("\n".join("- " + _link("Docs", item, docs[item]["title"]) for item in child_docs) or "—") + "\n"
        if row.get("tags"):
            if not isinstance(row["tags"], list):
                raise ValueError("tags must be a list")
            content += _body_section("태그", ", ".join(_inline(item) for item in row["tags"]))
        content += _body_section("본문", row.get("body"))
        content += "\n## 참조 업무\n\n" + ("\n".join("- " + _link("Tasks", item, tasks[item]["title"]) for item in document_tasks[identifier]) or "—") + "\n"
        files[f"Docs/{identifier}.md"] = content

    root = Path(destination).expanduser().absolute() / "TeamTasks"
    _assert_safe_chain(root)
    for folder in ("Projects", "Tasks", "Docs"):
        _assert_safe_chain(root / folder)
    manifest_path = root / MANIFEST
    previous: set[str] = set()
    if manifest_path.is_symlink():
        raise ValueError("Symlink manifest is not allowed")
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ValueError("Invalid export manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("generator") != OWNER or not isinstance(manifest.get("files"), list):
            raise ValueError("Export directory is not owned by TeamTasks")
        for item in manifest["files"]:
            if not isinstance(item, str) or not OUTPUT_PATH.fullmatch(item):
                raise ValueError("Unsafe path in export manifest")
            previous.add(item)
    elif root.exists() and any(root.iterdir()):
        raise ValueError("Refusing to overwrite an existing TeamTasks folder without its manifest")
    # Validate the whole write/delete set before changing any file.
    for name in sorted(set(files) | previous):
        path = root / name
        _assert_safe_chain(path.parent)
        if path.is_symlink() or (path.exists() and (name not in previous or not _owned_file(path))):
            raise ValueError(f"Refusing to overwrite or remove a user-owned file: {path}")
    root.mkdir(parents=True, exist_ok=True)
    for folder in ("Projects", "Tasks", "Docs"):
        (root / folder).mkdir(exist_ok=True)
    written = 0
    for name, content in sorted(files.items()):
        path = root / name
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            _atomic_write(path, content)
            written += 1
    removed = 0
    for name in sorted(previous - set(files)):
        path = root / name
        if path.exists():
            path.unlink()
            removed += 1
    manifest_content = json.dumps({"generator": OWNER, "files": sorted(files)}, ensure_ascii=False, indent=2) + "\n"
    if not manifest_path.exists() or manifest_path.read_text(encoding="utf-8") != manifest_content:
        _atomic_write(manifest_path, manifest_content)
    return {"root": str(root), "files_written": written, "files_removed": removed, "projects": len(projects), "tasks": len(tasks), "docs": len(docs), "progress": progress}
