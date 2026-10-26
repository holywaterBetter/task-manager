# 기존 도구 조사와 선택

확인일: 2026-10-26. 공식 문서와 프로젝트 원본을 확인했다. 아래 기능·에디션은 확인 시점의 문서 기준이다. 적합성 판단과 제안은 이 프로젝트의 판단이다.

## 결론

요구와 겹치는 도구가 이미 있다. Jira식 일정 관리가 우선이면 OpenProject, 업무와 문서의 웹 사용성이 우선이면 Plane, Codex 중심의 지속적인 업무 맥락은 Beads가 가까운 선례다. Obsidian의 TaskNotes도 작업을 Markdown 파일로 표현하는 좋은 사례다.

이번 환경은 사내 Git만 공유 경로로 확정됐다. 기성 제품을 사내에 설치·연동할 수 있는지는 별도 확인이 필요하다. 우선 작은 Git 기반 프로토타입으로 업무 모델과 Codex 흐름을 검증하고, 규모가 커지면 기존 제품으로 이전할 수 있도록 업무·지식 ID와 JSON 데이터를 열어 두는 선택을 했다. 기존 도구가 없다는 판단에 따른 개발은 아니다.

## 비교

| 후보 | 확인된 강점 | 이 환경에서 확인할 부분 |
| --- | --- | --- |
| OpenProject | 작업 유형·계층, 선후행·차단 관계, 일정, API, 공식 MCP | 공식 MCP는 Enterprise Professional 이상. Community API에 작은 클라이언트를 붙이는 대안은 가능. 서버 운영 필요 |
| Plane | 부모·하위 업무, 문서 연결, 일정 의존성, Pages, REST API와 공식 MCP, 자체 호스팅 | 중첩 Pages와 일부 Timeline 기능의 플랜 차이. 자체 호스팅 MCP는 로컬 stdio 또는 자체 서버로 연결 |
| Beads | AI 에이전트용 업무 기억, 계층·의존 그래프, ready 조회, claim, Codex 설정, 여러 PC 동기화 | 현재 Dolt 기반. 추가 실행 환경과 동기화 운영을 검토해야 함. Jira식 일정표·업무 배분 보고는 별도 검토 |
| TaskNotes + Obsidian | 업무별 Markdown/YAML, 프로젝트·하위 업무·선후행, HTTP/MCP | API는 각 PC의 loopback에서 실행하며 Obsidian 실행 필요. 공동 동기화·동시 선점은 별도 검증 |
| Taskmaster | PRD에서 업무 분해, 의존성·하위 업무, CLI/MCP, 검증 전략 필드 | 공식 팀 모드는 Hamster 클라우드 중심. 사내 Git만으로 운영하려는 조건과 바로 맞는지 확인 필요 |

OpenProject는 [계층·관계 문서](https://www.openproject.org/docs/user-guide/work-packages/work-package-relations-hierarchies/), [Work Packages API](https://www.openproject.org/docs/api/endpoints/work-packages/), [17.8 릴리스](https://www.openproject.org/blog/openproject-17-8-release/)와 [MCP 관리자 문서](https://www.openproject.org/docs/system-admin-guide/integrations/mcp-server/)를 참고했다.

Plane의 업무 연결 모델은 [Work Items](https://docs.plane.so/work-items/overview), MCP의 자체 호스팅 설정은 [공식 개발 문서](https://developers.plane.so/dev-tools/mcp-server)에 있다. 기능별 조건은 [Nested Pages](https://docs.plane.so/pages/nested-pages)와 [Timeline Dependencies](https://docs.plane.so/core-concepts/issues/timeline-dependency)에 명시돼 있다.

[Beads 원본 저장소](https://github.com/gastownhall/beads)는 기존 steveyegge 경로에서 이동했다. 현재 원본 저장 방식은 Dolt이며 JSONL export를 공용 원본이라고 오해하지 않아야 한다. `bd ready`, claim, Codex 설정, 업무 그래프가 이 설계의 주요 선례다.

[TaskNotes 개요](https://tasknotes.dev/obsidian/)와 [HTTP/MCP API](https://tasknotes.dev/obsidian/HTTP_API/), [런타임 관계 모델](https://tasknotes.dev/obsidian/javascript-api/)을 확인했다. Taskmaster의 차이는 [팀 협업](https://tryhamster.com/docs/taskmaster/team/overview)과 [업무 스키마](https://tryhamster.com/docs/taskmaster/capabilities/task-structure)에 근거한다.

## 참고한 개념

- **WBS와 의존 그래프의 분리**: 부모·자식은 업무 범위를 나누고, 선행·후행은 실행 조건을 나타낸다. 트리 하나에 두 의미를 섞지 않는다.
- **작업 기록과 지속 지식의 분리**: 프롬프트·플랜·결과는 업무에 남기고, 여러 작업에서 재사용할 결정·명세·규칙은 별도 문서로 연결한다.
- **점진적인 맥락 조회**: 작업 시작 시 프로젝트 공통 지식, 상위 목적, 선행 결과, 연결 문서를 제한된 분량으로 가져온다. 본문 전체를 매번 주입하지 않는다.
- **원본과 조회 화면의 분리**: Git JSON을 원본으로 두고 Obsidian은 언제든 다시 만들 수 있는 조회용으로 쓴다.
- **근거와 합의에 따른 집계**: 업무 완료 근거와 비중을 남기고 사람별 배분을 설명 가능한 공식으로 계산한다. 코드량·AI의 인상만으로 개인 점수를 만들지 않는다.

## ecode-skills에서 이어받은 방식

로컬 `ecode-skills/AGENTS.md`, `docs/codex/workflow.md`, `docs/codex/design-handoff-token-efficiency.md`, `docs/codex/design-sources.md`, `codex-skills/README.md`를 읽었다. 해당 파일은 변경하지 않았다.

기존 흐름은 AGENTS.md → 작업 유형별 문서 → 검색 도구 → 필요한 스킬로 좁혀 읽는다. Harvest → Allocate → Develop → Evaluate → Synthesize → Capture 단계에 맞춰 요청·계획·검증·결과·재사용 지식을 연결했다. 같은 경로의 존재만으로 서버 접속이나 자동 설치가 가능하다고 가정하지 않았다.

Codex의 지속 지시는 [AGENTS.md 공식 문서](https://learn.chatgpt.com/docs/agent-configuration/agents-md), 스킬과 도구 연결은 [스킬 문서](https://learn.chatgpt.com/docs/build-skills), [MCP 문서](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)를 확인했다. 이 구현의 stdio 어댑터는 MCP [전송](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports), [수명주기](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle), [도구](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) 규격의 tools-only 부분을 제공한다.
