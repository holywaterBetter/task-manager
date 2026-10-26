# TeamTasks — Codex와 함께 쓰는 Git 업무·지식 관리

두 개발자가 각 PC의 Codex로 업무를 만들고 맥락을 읽으며, 요청·계획·결과·검증 근거를 사내 Git으로 공유하는 초기 버전입니다. **Python 3.11 이상만 있으면 실행됩니다.** 외부 패키지나 AI API 키는 필요하지 않습니다.

프로젝트 저장소: [holywaterBetter/task-manager](https://github.com/holywaterBetter/task-manager). 업무 원본도 이 저장소의 `registry/`에 둘 수 있으며, 도구 개발 파일을 수정할 때는 먼저 해당 변경을 정리해 깨끗한 작업 상태로 만듭니다.

기존 도구 조사와 도입 판단은 [조사 결과](docs/research.md), 데이터 구조·운영 원칙은 [설계](docs/design.md)에 정리했습니다. Git만 공유할 수 있다는 조건을 우선 적용했습니다.

## 제공하는 기능

- 프로젝트 → 에픽 → 스토리 → 태스크 → 서브태스크, 별도 선행 업무 그래프
- 담당자, 우선순위, 시작·마감일, 업무 비중·예상 규모
- 프롬프트·계획·결과·검증 근거와 변경 이력
- 문서 트리, 문서 검색, 업무와 지식 연결, 업무 ID별 맥락 조립
- 버전 확인, 현재 PC의 담당 선점, 의존성 순환·잘못된 병합 검사
- 최하위 업무 기준 진척과 합의된 업무 배분 집계
- Codex용 MCP stdio 도구와 팀 스킬, Obsidian 조회용 Markdown 내보내기

**모든 업무 호출은 Git 동기화를 필수로 수행합니다.** 조회·변경 전에 `pull --ff-only`로 최신 상태를 받고, 업무를 처리한 뒤 변경이 있으면 업무 파일만 커밋하고 `push`합니다. 조회도 push가 성공해야 완료 처리하며 빈 커밋은 만들지 않습니다. PC 사이의 실시간 선점은 보장하지 않습니다. [동기화 규칙과 복구](docs/sync.md)

## 바로 실행

이 폴더에서 실행합니다. Windows에서는 `python3` 대신 `python` 또는 `py -3`을 사용합니다.

```bash
python3 -m task_manager --local-only --registry /tmp/team-tasks-demo --actor alice demo
python3 -m task_manager --local-only --registry /tmp/team-tasks-demo list --ready
python3 -m task_manager --local-only --registry /tmp/team-tasks-demo export /tmp/team-tasks-vault
```

`demo`는 빈 폴더에만 **합성 예제**를 만듭니다. 예제 완료 결과는 실제 ecode-skills 작업이나 성과가 아닙니다. 이미 생성한 [예제 업무 파일](examples/registry)과 [Obsidian 시작 문서](examples/vault/TeamTasks/Home.md)도 살펴볼 수 있습니다.

실제 사용 시 두 PC에서 같은 사내 업무 저장소를 clone하고, 각각의 registry 경로를 지정합니다. 업무 전용 저장소를 권장합니다. 기본 모드는 upstream이 설정된 브랜치와 깨끗한 작업 폴더를 요구합니다. Git 작성자도 설정돼 있어야 합니다. `--local-only`는 샘플·개발 테스트용이며 실제 업무의 동기화 실패를 우회하는 용도로 쓰지 않습니다.

```bash
python3 /path/to/task-manager/team_tasks.py --registry /path/to/team-work/registry --actor alice call project.create --data '{"key":"ECODE","name":"ecode-skills"}'
python3 /path/to/task-manager/team_tasks.py --registry /path/to/team-work/registry --actor alice call task.create --data '{"project":"ECODE","kind":"task","title":"공통 컴포넌트 개선","prompt":"요청과 완료 기준을 여기에 기록"}'
python3 /path/to/task-manager/team_tasks.py --registry /path/to/team-work/registry check
```

셸의 따옴표 처리 차이를 피하려면 JSON을 UTF-8 파일로 저장하고 `call task.create --file request.json`을 사용합니다. 업무 ID는 생성 응답에 반환됩니다. 기존 상태 변경에는 직전에 읽은 `version`이 필요합니다.

## 두 PC의 Git 운영

업무 저장소에서 다음 순서를 사용합니다. 프로그램은 업무·문서·이력 JSON만 자동 커밋합니다. 코드 변경은 대상 코드 저장소의 규칙을 따르고, 업무 evidence에 코드 커밋이나 PR 주소를 연결합니다.

1. `list --ready` 또는 Codex로 업무를 찾습니다. 도구가 먼저 pull하고 조회 후 push합니다.
2. `context 업무ID`로 배경을 읽습니다. 이 호출도 동기화합니다.
3. 현재 version으로 `task.claim`을 실행합니다. **담당 선언 commit·push 성공 후** 개발을 시작합니다.
4. 계획·결과·검증 근거를 갱신할 때마다 도구가 다시 pull → 변경 → commit → push합니다.
5. pull 실패 시 변경을 시작하지 않습니다. push 실패 시 로컬 변경·커밋을 보존하고 성공으로 응답하지 않습니다. 새 업무 요청을 반복하지 말고 `sync-retry`로 해당 커밋만 재전송합니다. 원격과 분기됐으면 [충돌 처리](docs/design.md#git-충돌-처리)를 따릅니다.

담당자가 겹치면 push에 성공한 담당 선언부터 함께 확인합니다. 연결이 끊긴 상태에서는 기본 조회·변경을 중단합니다. Git 권한과 접근 통제는 기존 사내 저장소가 담당합니다. 자동 강제 push·stash·merge·rebase는 수행하지 않습니다.

## Codex 연결

`examples/codex-config.toml`의 경로를 본인 PC에 맞게 바꿔 Codex 설정에 추가합니다. MCP 프로세스는 각 PC에서 실행하며 같은 사내 업무 저장소의 **각자 로컬 clone**을 바라봅니다.

```toml
[mcp_servers.team_tasks]
command = "python3"
args = ["/absolute/path/task-manager/team_tasks.py", "--registry", "/absolute/path/team-work/registry", "--actor", "alice", "mcp"]
```

Windows 예시는 [설정 파일](examples/codex-config.toml)에 있습니다. 새 세션에서 연결된 도구를 확인합니다. 기존 Codex 설정은 이 프로젝트가 자동 수정하지 않습니다.

[팀 스킬](codex-skills/team-tasks/SKILL.md)을 팀이 사용하는 Codex 스킬 위치에 복사하거나 대상 프로젝트 `AGENTS.md`에 이 파일을 읽도록 연결합니다. 현재 공식 문서는 저장소의 `.agents/skills/`를 로컬 스킬 위치로 안내합니다. ecode-skills의 기존 `codex-skills/` 수동 배포 관례로도 파일을 배포할 수 있습니다. [공식 스킬 문서](https://learn.chatgpt.com/docs/build-skills), [공식 MCP 설정](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

대상 코드 저장소에 넣을 최소 예시는 [AGENTS 연결 예시](examples/AGENTS.team-tasks.md)입니다. 연결 후에는 이렇게 요청할 수 있습니다.

> ECODE에서 내가 시작할 수 있는 업무를 찾아줘. 관련 지식과 선행 결과를 읽고 계획을 정리해줘.
>
> 이 작업 결과와 테스트 근거를 업무에 기록하고 검토 대기 상태로 바꿔줘.

Codex App의 채팅이나 `/goal`과 이 업무 ID를 자동으로 동기화하는 기능은 포함하지 않습니다. Codex가 MCP 또는 CLI를 호출해 업무 기록을 남깁니다.

## Obsidian 조회

```bash
python3 /path/to/task-manager/team_tasks.py --registry /path/to/team-work/registry export /path/to/obsidian-vault
```

Obsidian에서 해당 vault의 `TeamTasks/Home.md`를 엽니다. 프로젝트·업무·문서 트리, 선후행 링크, 일정표와 담당·상태를 볼 수 있습니다. 원본 snapshot은 Git 동기화 후 읽습니다. vault는 업무 전용 Git 저장소 밖에 두거나 생성 폴더를 Git에서 제외합니다. 생성기는 `TeamTasks/` 안의 자신이 생성한 파일만 갱신하며 기존 노트와 충돌하면 중단합니다. **생성 문서 편집은 원본에 반영되지 않습니다.** 업무는 Codex/CLI에서 수정한 뒤 다시 내보냅니다.

Windows용 Obsidian과 WSL을 함께 쓰면 조회 vault는 Windows 드라이브에 둡니다. 예를 들어 WSL에서 `export /mnt/c/Obsidian/TaskManager`를 실행하고, Obsidian에서는 `C:\Obsidian\TaskManager`를 보관함으로 엽니다. `\\wsl.localhost\...` 폴더를 열 때 `EISDIR ... watch` 오류가 나면 [Windows·WSL 사용 안내](docs/obsidian-windows.md)를 따르세요.

## 검증

```bash
python3 -m unittest discover -s tests -v
```

70개 테스트와 실제 MCP stdio 연결을 통과했고, 스킬은 별도의 Codex 스킬 검증기로도 확인했습니다. [검증 기록과 한계](docs/validation.md)를 참고하세요. 자동 일정 조정, 실시간 공동 편집, 근거의 진위 검증, 인사 평가 기능은 초기 버전 범위에 포함되지 않습니다.
