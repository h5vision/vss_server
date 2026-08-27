# 참조 저장소와 기준선

최종 확인일: 2026-08-27 KST

## 구현 대상

| 항목 | 값 |
|---|---|
| 저장소 | `https://github.com/h5vision/vss_server.git` |
| 브랜치 | `module` |
| module 분기 기준 | `main@e3e706e44c2843da2bf2a004e8d1a27d1b7c7aeb` |
| 변경 경로 | 저장소 최상위 `module/` 전용 |
| 역할 | Frontend overlay 보존, 전체 revision materialization, main VSS 모듈 제출과 Admin API |

## Frontend 참조

| 항목 | 값 |
|---|---|
| 저장소 | `https://github.com/h5vision/vision.git` |
| 브랜치 | `frontend` |
| 확인 기준 SHA | `8008a06c732f9ca4e895c4fd75d58c4ab9cf6e37` |

우선 확인 파일:

```text
vision/src/services/gitService.ts
vision/src/services/commitDiffService.ts
vision/src/services/APIService.ts
vision/src/types/git.ts
vision/src/extension.ts
vision/src/chat/chatHandler_RAG_server.ts
vision/src/chat/chatHandler_SSE.ts
vision/package.json
```

확인 계약:

- HEAD 변경 시 이전 commit과 새 commit 사이의 변경을 전송합니다.
- `files[].content`는 변경 후 전체 UTF-8 문자열입니다.
- 삭제와 rename은 별도 배열입니다.
- `snapshot_id`, `content_sha256`, `size_bytes`, `branch`는 보내지 않습니다.
- Snapshot Backend 기본 endpoint는 `http://192.168.0.7/v1`입니다.
- POST 기본 timeout은 10초입니다.
- 기존 활성 AI 호출은 `http://127.0.0.1:11500/api/chat`입니다.
- 2026-08-26 확인 환경에서 Windows portproxy가 `127.0.0.1:11500`을
  `192.168.0.12:11500`으로 전달합니다.

이전 확인 SHA `56b71405e568b059158b1a666fa362f465c6c10a`부터 현재 기준까지
`commitDiffService.ts`, `APIService.ts`, `types/git.ts`의 Snapshot request 계약은
변경되지 않았습니다. 변경은 package version/format과 Chat 연결 실패 표시 개선입니다.

Frontend의 `project_id`는 VSS exact ID가 아니라 힌트일 수 있습니다. Admin binding으로
확정하며 문자열 유사도로 자동 선택하지 않습니다. 현재 `CommitDiffService`는 Backend
응답 body를 UI에 표시하지 않으므로 VS Code 알림이 필요하면 별도 Frontend 변경입니다.

## VSS 참조

| 항목 | 값 |
|---|---|
| 저장소 | `https://github.com/h5vision/vss_server.git` |
| 브랜치 | `main` |
| 기준 SHA | `802025884624e855a3d4406937855a61e2092346` |
| 기준 commit 시각 | `2026-08-27T14:13:38+09:00` |
| Python package/version | `vss`, `0.1.0` |

`main` 브랜치의 `vss/`가 실제 통합 대상입니다. `module/`에는 VSS main 소스를 복사하지
않습니다. 비교가 필요하면 별도 read-only checkout 또는 `git show origin/main:<path>`를
사용합니다.

권위 파일:

```text
CHARTER.md
README.md
docs/API.md
requirements.txt
vss/__init__.py
vss/config.py
vss/indexer.py
vss/store/__init__.py
vss/store/base.py
vss/store/chroma.py
vss/store/pgvector.py
scripts/db_init.sql
tests/test_roundtrip.py
```

확인된 모듈 계약:

```python
start_index(project_root, project_id, *, profile=None, blocking=False,
            force=False, on_done=None, extra_meta=None, store=None) -> dict
status(project_id, store=None) -> dict
exists(project_id, store=None) -> dict
list_projects(store=None) -> list[dict]
repair(store=None, *, apply=False) -> list[dict]
```

- VSS는 전체 디렉터리를 수집하여 새 build를 만들고 성공 시 원자적으로 promote합니다.
- 실패 시 이전 active index를 보존합니다.
- 기본 비동기 실행은 daemon thread이며 진행률은 프로세스 전역 `JOBS`에 있습니다.
- 완료 정본은 Store이며 `status().index.commit`은 `git_head(project_root)`에서 옵니다.
- 상태는 `none|running|indexing_lexical|promoting|done|failed|aborted`입니다.
- `already_running`, `not_a_directory`는 `accepted: false` 반환 이유입니다.
- `list_projects()`는 선택적 `note`를 반환하며 인덱싱 이유를 Store metadata에서 읽습니다.
- `VSS_PROJECT_ALIASES`는 Chat·조회 경로 전용입니다. Snapshot 인덱싱과 binding은 별칭을
  적용하지 않고 exact VSS index ID를 사용합니다.
- VSS 설정 singleton `CFG`는 import 시 `VSS_*` 환경변수를 읽습니다.
- Store는 `chroma|pgvector`이며 초기 Chroma는 한 프로세스 운용이 필요합니다.
- pgvector는 `rag` schema, Snapshot Backend는 `snapshot` schema로 쓰기 권한을
  분리하는 구성이 제공됩니다.

## 확인된 계약 공백

현행 `start_index()`에는 명시적 `revision` 인자가 없습니다. `extra_meta.commit`을 넣어도
promotion 단계가 `git_head(root)`로 commit을 다시 기록합니다. 따라서 다음 중 하나가
필수입니다.

1. materialized root가 target commit을 HEAD로 가진 실제 Git worktree이거나
2. upstream이 검증된 `revision` 인자를 받고 최종 index metadata에 보존해야 합니다.

Frontend delta만으로는 local-only commit의 Git object/부모/메타데이터를 재구성할 수
없습니다. 이 공백을 임의 SHA, 파일 hash 또는 `extra_meta`로 숨기지 않습니다.

통합 시작 기준 SHA의 VSS에는 packaging metadata가 없습니다. `module/pyproject.toml`은
Snapshot Backend의 `backend*`만 설치하며 main의 `vss*`를 복사하거나 함께 package하지
않습니다. VSS package 공급 방식과 실제 배포 commit SHA pin은 별도로 검증해야 합니다.

## Admin Web 기준

- VS Code Webview가 아닌 독립 서버입니다.
- Browser는 Backend `/v1/admin/*`만 호출합니다.
- DB, VSS Store, Ollama, Git credential에 직접 접근하지 않습니다.
- 현재 Frontend payload에 branch가 없으므로 `frontend_project_id`당 활성 binding 하나를
  사용하고 수신 시점 값을 Snapshot에 복사합니다.
- 서로 독립적인 Branch는 동일 VSS project의 active index를 덮지 않도록 별도
  `vss_project_id`를 원칙으로 합니다.

## 기준 갱신 절차

```powershell
git ls-remote https://github.com/h5vision/vision.git refs/heads/frontend
git ls-remote https://github.com/h5vision/vss_server.git `
  refs/heads/main `
  refs/heads/module
```

SHA가 바뀌면 문서보다 실제 TypeScript/Python/API/test를 먼저 읽고 contract fixture를
갱신합니다. Git SHA가 같다는 사실만으로 설치 방식, Store, DB, Ollama와 materialization
경로가 실환경에서 준비됐다고 판단하지 않습니다.
