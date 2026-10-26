# Windows Obsidian과 WSL 함께 사용하기

Windows용 Obsidian에서 `\\wsl.localhost\Ubuntu\home\...` 또는 `\\wsl$\...` 폴더를 보관함으로 열면 `EISDIR: illegal operation on a directory, watch` 오류가 발생하는 환경이 있다. Windows 앱이 WSL 파일시스템의 변경 감시를 시작하는 과정에서 발생하는 증상이며, 같은 오류가 [Obsidian 포럼에 보고](https://forum.obsidian.md/t/cant-create-or-open-vault-in-wsl-folder/34688)되어 있다. 이 보고만으로 모든 버전·환경의 동작을 단정하지는 않는다.

task-manager의 Git 원본과 프로그램은 WSL에 두고, Obsidian 조회용 문서만 Windows 드라이브에 내보내면 이 경로를 통한 감시를 피할 수 있다. [Microsoft 파일시스템 안내](https://learn.microsoft.com/en-us/windows/wsl/filesystems)에 따르면 Windows C: 드라이브는 기본 WSL 설정에서 `/mnt/c`로 접근한다.

## 샘플 확인

WSL 터미널에서 `team_tasks.py`와 `examples/registry`가 있는 프로젝트 최상위 폴더로 이동한 뒤 실행한다.

```bash
python3 team_tasks.py --local-only --registry examples/registry export /mnt/c/Obsidian/TaskManager
```

명령 결과에 샘플 `projects: 1`, `tasks: 5`, `docs: 2`가 표시되는지 확인한다. 0개라면 registry 경로와 현재 폴더를 다시 확인한다.

Windows Obsidian에서는 다음과 같이 연다.

1. 오류 화면에서 **다른 보관함 열기**를 누른다.
2. **폴더를 보관함으로 열기**에서 `C:\Obsidian\TaskManager`를 선택한다.
3. 파일 목록의 `TeamTasks/Home`을 연다. 실제 시작 문서는 `C:\Obsidian\TaskManager\TeamTasks\Home.md`다.

`export`는 지정한 폴더 아래에 `TeamTasks`를 자동으로 만든다. 위 예제에서 목적지를 `.../TaskManager/TeamTasks`로 지정할 필요는 없다. 해당 Windows 경로에 쓰기 권한이 없으면 본인 Windows 사용자 폴더 안의 새 폴더를 사용하고 WSL 경로도 대응시킨다.

이 명령의 `--local-only`는 합성 샘플 확인용이다. 실제 팀 업무에는 기본 동기화 모드를 유지한다.

## 실제 업무 내보내기

```bash
python3 /path/to/task-manager/team_tasks.py --registry /path/to/team-work/registry export /mnt/c/Obsidian/TaskManager
```

이 호출은 기존 규칙대로 Git pull·snapshot 검증·push를 완료한 다음 Windows 조회 문서를 만든다. 다시 실행하면 자신이 만든 문서만 갱신한다. 원본 Git을 Windows로 옮기거나 별도의 파일 동기화 플러그인을 설치할 필요는 없다.

기존 WSL 보관함이나 `.obsidian` 설정을 삭제하지 않아도 된다. 새 Windows 보관함을 연 뒤 기존 노트가 필요하면 별도로 확인해 옮긴다. 생성된 `TeamTasks` 문서는 조회용이며, 업무·지식 수정은 task-manager의 CLI/MCP에서 수행한다.
