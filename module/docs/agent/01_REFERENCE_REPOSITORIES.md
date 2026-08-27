# 참조 저장소와 기준선

최종 확인일: 2026-08-27 KST

## 구현 대상

| 항목 | 값 |
|---|---|
| 저장소 | `https://github.com/h5vision/vss_server.git` |
| 브랜치 | `module` |
| module 분기 기준 | `main@e3e706e44c2843da2bf2a004e8d1a27d1b7c7aeb` |
| 변경 경로 | 저장소 최상위 `module/` 전용 |
| 역할 | Frontend overlay 보존, 전체 revision materialization, main VSS HTTP 제출과 Admin API |

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
vision/src/controller/sidebarController.ts
vision/src/controller/handlers/projectListHandler.ts
vision/src/controller/handlers/projectBriefHandler.ts
vision/src/controller/handlers/modelInfoHandler.ts
vision/src/controller/handlers/RAGTESTHandler.ts
vision/src/controller/handlers/indexingHandler.ts
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
- 이 값은 `vision/package.json`의 `vision.endpoint` 설정 기본값입니다.
  `APIService.ts`의 `http://127.0.0.1:5000`은 설정 스키마가 없을 때의 코드 fallback이며
  일반 설치의 유효 기본값보다 우선하지 않습니다.
- POST 기본 timeout은 10초입니다.
- 기존 활성 AI 호출은 `http://127.0.0.1:11500/api/chat`입니다.
- 2026-08-26 확인 환경에서 Windows portproxy가 `127.0.0.1:11500`을
  `192.168.0.12:11500`으로 전달합니다.

같은 `vision.endpoint`를 사용하는 활성 Sidebar 호출도 존재합니다.

```text
GET  /health
GET  /models
GET  /projects
GET  /briefing?project_id=<workspace-name>
GET  /index/status?project_id=<workspace-name>
POST /index/update/files                 # RemoveRAGTEST 레거시
POST /v1/documents/ingest-with-metadata  # endpoint 기본값과 결합 시 /v1/v1 중복 가능
```

`/projects`, `/briefing`, `/models`, `/index/status`는 Backend proxy 또는 Frontend endpoint
분리 결정이 필요합니다. 레거시 `/index/update/files`를 VSS에 그대로 전달하지 않으며
Frontend를 `/workspace-overlays`로 고치거나 명시적 호환 adapter 범위를 합의해야 합니다.
또한 overlay의 `project_id`는 remote path 형태지만 일부 조회는 workspace 이름을 보내므로
두 값을 exact VSS index ID로 매핑하는 계약이 필요합니다.

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
| 기준 SHA | `aa6aa3e77679e2fb319d2009cfd7726c6ae723be` |
| 기준 commit 시각 | `2026-08-27T14:26:07+09:00` |
| Snapshot 연동 방식 | HTTP `POST /index`, `GET /index/status` |

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

확인된 Snapshot HTTP 계약:

```text
POST /index
GET  /index/status?project_id=<exact-index-id>
GET  /index/exists?project_id=<exact-index-id>
GET  /projects
GET  /health
```

- VSS는 전체 디렉터리를 수집하여 새 build를 만들고 성공 시 원자적으로 promote합니다.
- 실패 시 이전 active index를 보존합니다.
- `POST /index`는 접수 시 `202`, 같은 project가 실행 중이면 `409`를 반환합니다.
- 완료 정본은 `GET /index/status`이며 `index.commit`은 VSS 서버가
  `git_head(project_root)`에서 읽습니다.
- 상태는 `none|running|indexing_lexical|promoting|done|failed|aborted`입니다.
- `already_running`, `not_a_directory`는 `accepted: false` 반환 이유입니다.
- `GET /projects`는 `projects`, `incomplete` wrapper를 반환하고 project에는 선택적
  `note`가 포함됩니다.
- `VSS_PROJECT_ALIASES`는 Chat·조회 경로 전용입니다. Snapshot 인덱싱과 binding은 별칭을
  적용하지 않고 exact VSS index ID를 사용합니다.
- VSS 인증이 켜진 경우 `X-VSS-Token` 또는 Bearer token이 필요합니다.
- Store는 `chroma|pgvector`이며 VSS 서버 프로세스가 Store를 단독 소유합니다.
- pgvector는 `rag` schema, Snapshot Backend는 `snapshot` schema로 쓰기 권한을
  분리하는 구성이 제공됩니다.

## 확인된 계약 공백

현행 `POST /index`에는 명시적 `revision` 필드가 없습니다. promotion 단계는
`git_head(project_root)`를 기록합니다. 따라서 다음 중 하나가
필수입니다.

1. materialized root가 target commit을 HEAD로 가진 실제 Git worktree이거나
2. upstream이 검증된 `revision` 인자를 받고 최종 index metadata에 보존해야 합니다.

Frontend delta만으로는 local-only commit의 Git object/부모/메타데이터를 재구성할 수
없습니다. 이 공백을 임의 SHA, 파일 hash 또는 `extra_meta`로 숨기지 않습니다.

Backend와 VSS가 다른 컨테이너나 호스트라면 Backend가 기록한 `project_root` 문자열이
VSS 서버에서도 동일한 전체 tree를 가리켜야 합니다. shared volume/mount 또는 VSS 서버
로컬 materialization 방식이 확정되지 않으면 인덱싱을 시작할 수 없습니다. VSS 서버의
exact source SHA는 배포 manifest/image에서 고정하며 현재 HTTP health 응답만으로는 이를
증명할 수 없습니다.

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
갱신합니다. Git SHA가 같다는 사실만으로 VSS URL, 인증, Store, DB, Ollama와 materialization
경로가 실환경에서 준비됐다고 판단하지 않습니다.
