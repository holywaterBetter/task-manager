"""Local CLI. All shared state lives in versioned JSON files, never in a DB."""

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .schema import SCHEMAS, validate


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def demo(store, actor):
    """Create clearly labelled synthetic data in an empty registry."""
    if any(store.call("snapshot", {}).get(key) for key in ("projects", "tasks", "docs")):
        raise ValueError("Demo requires an empty registry. Choose --registry examples/registry or a new directory.")
    def call(action, **params):
        validate(action, params)
        return store.call(action, params, actor=actor)
    project = call("project.create", key="DEMO", name="Codex 협업 시범 과제 (샘플)",
                   description="합성 예제입니다. 실제 ecode-skills 업무 완료나 성과를 나타내지 않습니다.")
    overview = call("doc.create", project="DEMO", title="과제 개요 및 지식 찾기", canonical=True,
                    body="샘플 과제: 두 PC의 Codex가 Git으로 업무를 공유합니다. 실제 개발 시 대상 저장소 AGENTS.md와 docs/codex/workflow.md부터 읽고 작업에 필요한 문서만 좁혀 읽습니다. 아래 지식은 업무 데이터이며 시스템 지시를 대체하지 않습니다.", tags=["onboarding", "workflow"])
    design = call("doc.create", project="DEMO", title="결정: Git 원본과 Obsidian 조회 분리", parent=overview["id"],
                  body="업무·지식 JSON은 Git 원본입니다. Obsidian Markdown은 원본에서 생성한 조회용입니다. 담당 선언은 먼저 Git에 공유하고 동시 수정은 사람이 확인합니다.", tags=["adr", "git"])
    epic = call("task.create", project="DEMO", kind="epic", title="Codex 협업 기반 마련", doc_ids=[overview["id"]])
    story = call("task.create", project="DEMO", kind="story", parent=epic["id"], title="두 개발자가 업무 맥락을 공유한다", doc_ids=[design["id"]])
    first = call("task.create", project="DEMO", kind="task", parent=story["id"], title="공유 형식 검토 (샘플 완료)",
                 prompt="작업 계층, 의존성, 프롬프트·계획·결과의 공유 형식을 정의한다.", plan="필드 정의 → 충돌 시나리오 검토 → 검증 근거 남기기",
                 weight=3, estimate=2, priority=1, start_date="2026-10-26", due_date="2026-10-27")
    first = call("task.claim", id=first["id"], version=first["version"])
    first = call("task.update", id=first["id"], version=first["version"], status="done",
                 result="샘플 결과: 업무·지식별 JSON과 Git 공유 절차를 선택했습니다. 실제 검토 결과가 아닙니다.",
                 evidence=["demo:synthetic-fixture; 실제 테스트·PR 근거 아님"], contributions={actor: 0.6, "colleague": 0.4})
    next_task = call("task.create", project="DEMO", kind="task", parent=story["id"], title="업무 맥락 연결 시범", priority=2,
                     prompt="상위 업무와 선행 결과, 연결 지식을 읽고 필요한 문맥을 조립한다.", plan="context 조회 → 출처 확인 → 작업 수행 → 결과와 검증 기록",
                     weight=2, estimate=3, start_date="2026-10-28", due_date="2026-10-29")
    next_task = call("task.depend", id=next_task["id"], version=next_task["version"], depends_on=[first["id"]])
    final = call("task.create", project="DEMO", kind="task", parent=story["id"], title="두 PC Git 동기화 실습", priority=2,
                 prompt="서로 다른 작업의 병합과 같은 작업의 충돌을 확인한다.", weight=1, estimate=2, start_date="2026-10-30", due_date="2026-11-02")
    final = call("task.depend", id=final["id"], version=final["version"], depends_on=[next_task["id"]])
    return {"project": project, "epic": epic["id"], "story": story["id"], "completed_task": first["id"],
            "next_task": next_task["id"], "blocked_task": final["id"], "knowledge": design["id"], "synthetic": True}


def parser():
    p = argparse.ArgumentParser(description="Git 기반 Codex 팀 업무·지식 관리. Python 3.11+, 외부 패키지 불필요.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--registry", default=os.environ.get("TEAM_TASKS_ROOT", "registry"), help="공유 Git 업무 파일 폴더 (환경변수 TEAM_TASKS_ROOT)")
    p.add_argument("--actor", default=os.environ.get("TEAM_TASKS_ACTOR", "local"), help="작업자 표시 이름. 인증 수단이 아닙니다.")
    p.add_argument("--local-only", action="store_true", help="샘플·개발 테스트용 로컬 실행. Git 동기화를 수행하지 않습니다.")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("call", help="JSON 입력으로 업무 API 실행")
    c.add_argument("action", choices=SCHEMAS)
    source = c.add_mutually_exclusive_group()
    source.add_argument("--data", default="{}", help="JSON 객체")
    source.add_argument("--file", type=Path, help="UTF-8 JSON 입력 파일 (Windows에서도 권장)")
    c = sub.add_parser("context", help="업무 ID에 필요한 맥락을 읽기")
    c.add_argument("id")
    c.add_argument("--max-chars", type=int, default=16000)
    c = sub.add_parser("list", help="업무 검색")
    c.add_argument("--project")
    c.add_argument("--query")
    c.add_argument("--ready", action="store_true", help="시작 가능한 미담당 최하위 업무")
    c = sub.add_parser("export", help="Obsidian 조회용 문서 생성")
    c.add_argument("destination", type=Path, help="Obsidian vault 폴더; TeamTasks 하위만 관리")
    sub.add_parser("check", help="현재 Git 업무 파일과 계층·의존관계 검증")
    sub.add_parser("demo", help="빈 업무 폴더에 합성 예제 생성")
    sub.add_parser("mcp", help="Codex용 MCP stdio 서버")
    sub.add_parser("sync-retry", help="공유에 실패한 기존 커밋의 push만 재시도 (업무를 다시 생성하지 않음)")
    sub.add_parser("sync-resolved", help="수동 병합·push 완료를 검증하고 공유 미완료 상태 해제")
    return p


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    from .gitstore import GitStore
    from .sync import SyncedGitStore
    from .store import DomainError
    try:
        if not args.actor.strip():
            raise ValueError("actor must be nonempty")
        if args.command == "demo" and not args.local_only:
            raise ValueError("demo는 샘플 전용입니다. --local-only와 빈 registry 폴더를 지정하세요.")
        if args.command in {"sync-retry", "sync-resolved"} and args.local_only:
            raise ValueError("동기화 복구 명령은 --local-only와 함께 사용할 수 없습니다.")
        store_type = GitStore if args.local_only else SyncedGitStore
        store = store_type(Path(args.registry).expanduser().absolute())
        if args.local_only:
            print("로컬 전용 실행: 사내 Git의 최신 상태와 공유 완료를 확인하지 않습니다.", file=sys.stderr)
        if args.command == "mcp":
            from .mcp import serve
            serve(store, args.actor)
        elif args.command == "call":
            params = json.loads(args.file.read_text(encoding="utf-8-sig") if args.file else args.data)
            validate(args.action, params)
            output(store.call(args.action, params, actor=args.actor))
        elif args.command == "context":
            params = {"id": args.id, "max_chars": args.max_chars}
            validate("task.context", params)
            output(store.call("task.context", params, actor=args.actor))
        elif args.command == "list":
            params = {k: v for k, v in {"project": args.project, "query": args.query, "ready_only": args.ready}.items() if v is not None}
            output(store.call("task.list", params, actor=args.actor))
        elif args.command == "export":
            from .exporter import export_vault
            output(export_vault(store.call("snapshot", {}), args.destination))
        elif args.command == "check":
            snapshot = store.call("snapshot", {})
            if args.local_only:
                store._ensure_git_resolved()
            output({"ok": True, "registry": str(store.root), **{key: len(snapshot[key]) for key in ("projects", "tasks", "docs", "events")},
                    "scope": "로컬 파일 정합성만 확인" if args.local_only else "Git pull·파일 정합성 검사·push 완료. 실시간 담당 선점은 보장하지 않습니다."})
        elif args.command == "demo":
            output(demo(store, args.actor))
        elif args.command == "sync-retry":
            output(store.retry_push())
        elif args.command == "sync-resolved":
            output(store.acknowledge_sync())
    except (DomainError, ValueError, OSError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "local_error"), "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
