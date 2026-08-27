# 구현 준비와 필수 검증

최종 확인일: 2026-08-27 KST

## 기준선

| 대상 | 기준 |
|---|---|
| Backend module | `vss_server/module`, 분기 기준 `main@e3e706e44c2843da2bf2a004e8d1a27d1b7c7aeb` |
| Frontend | `vision/frontend@8008a06c732f9ca4e895c4fd75d58c4ab9cf6e37` |
| VSS | `vss_server/main@97546fbcea6607a29ad0cc10246a7886bb44ceab` |

VSS 기준 SHA가 바뀌면 `CHARTER.md`, `docs/API.md`, 서버 route, `vss/indexer.py`,
`vss/config.py`, Store와 test를 다시 읽고 fixture와 문서를 먼저 갱신합니다.

## 현재 검증 상태

| 검증 | 상태 |
|---|---|
| Contract 39 / Unit 44 / Integration 7 | 통과, 전체 90개 |
| Ruff / compileall / `git diff --check` | 통과 |
| PostgreSQL Alembic upgrade/downgrade offline SQL | 통과 |
| 실제 PostgreSQL migration | `LIVE-03` 대기 |
| app lifecycle DB/VSS readiness | fake VSS + SQLite 기준 Phase 3B-1 통과 |
| Frontend projects/models/briefing proxy | fake VSS + exact binding 기준 통과 |
| materialization·실제 overlay E2E | Phase 4 미구현 |
| Admin 인증/RBAC/browser E2E | Phase 3A-2 및 `LIVE-13` 대기 |

현재 `tests/integration` 7개는 FastAPI 골격, DB/VSS readiness와 Frontend 조회 proxy를
fake VSS·SQLite로 검증합니다. 아래에 열거한 DB→materializer→VSS 전체 제출 시나리오나
실제 배포 경계가 이미 통과했다는 의미는 아닙니다.

## 확정된 경계

| 주체 | 대상 | 용도 |
|---|---|---|
| Frontend | `http://192.168.0.7/v1/workspace-overlays` | Git delta 전달 |
| Backend | VSS `POST /index` | 완성된 서버 로컬 디렉터리의 비동기 인덱싱 접수 |
| Backend | VSS `GET /index/status?project_id=...` | 진행·완료·실패 동기화 |
| Backend | PostgreSQL `snapshot` schema | Snapshot/binding/attempt 소유 |
| VSS | Chroma 또는 PostgreSQL `rag` schema | active index와 VSS Job 상태 소유 |
| Admin Web | Backend `/v1/admin/*` | Repository/Branch/VSS binding과 이력 |
| Frontend | `127.0.0.1:11500/api/chat` | 기존 AI 진입점, Snapshot과 별개 |

VSS는 별도 HTTP 서버이며 기본 API 포트는 `8200`입니다. Backend는 `VSS_BASE_URL`과,
VSS 인증을 활성화한 배포에서는 `VSS_TOKEN`을 사용합니다. Backend 프로세스가 VSS
Python 모듈이나 Store를 직접 import하거나 process-local Job 자료구조를 읽지 않습니다.

## 구현에 충분히 확정된 항목

- Frontend request 필드와 10초 timeout
- 40자리 Git SHA 및 POSIX 상대경로 검증
- 변경 후 전체 파일 content
- VSS 전체 디렉터리 재인덱싱과 build/promote 경계
- `POST /index`의 `202 accepted`, `409 already_running` 의미
- `GET /index/status` 상태와 `index.commit` 위치
- 접수와 완료의 분리
- 실패 시 이전 active index 보존
- VSS 인증 시 `X-VSS-Token` 또는 Bearer 사용
- `GET /projects`의 `note` 선택 필드와 query-only `VSS_PROJECT_ALIASES`
- Snapshot 인덱싱의 `project_id`에는 alias를 적용하지 않는 exact ID 규칙
- HTTP body에는 `revision`, `snapshot_id`, `requested_revision` 필드가 없다는 점
- 독립 Admin Web과 Branch별 VSS project binding
- 구조화된 성공·실패 reason/detail 응답

## 현재 확정되지 않은 필수 입력값

| ID | 필요한 값 | 확인 방법 | 미확정 시 동작 |
|---|---|---|---|
| `LIVE-01` | VSS 배포 URL, token 정책과 실제 배포 source SHA | `/health`, 인증 실패·성공, 배포 artifact 확인 | VSS readiness 실패 |
| `LIVE-02` | Backend와 VSS가 함께 읽는 `project_root` 경로·mount 및 target Git worktree 방식 | 양쪽 resolve/read와 완료 commit E2E | materialization/indexing 차단 |
| `LIVE-03` | PostgreSQL `DATABASE_URL`, migration 권한 | `snapshot` schema migration dry run | Snapshot 접수 금지 |
| `LIVE-04` | `SNAPSHOT_MATERIALIZATION_ROOT` 절대경로·용량·권한 | resolve/write/atomic rename probe | materialization 금지 |
| `LIVE-05` | Frontend별 Repository/Branch/`vss_project_id` binding | Admin 데이터와 실제 요청 비교 | 구조화된 `409` |
| `LIVE-06` | VSS Store와 인덱스 저장소 readiness | VSS `/health`, 인덱싱 smoke test | 인덱싱 금지 |
| `LIVE-07` | Ollama URL, bge-m3 model과 embedding dimension | VSS `/health`와 실제 embed smoke test | VSS dependency 실패 |
| `LIVE-08` | Backend→VSS 네트워크, TLS, timeout과 재시도 정책 | 배포 환경 HTTP probe | production GO 금지 |
| `LIVE-09` | 최초 base/full-tree bootstrap source | 실제 최초 Snapshot 복원 | 최초 인덱싱 금지 |
| `LIVE-10` | body·파일·단일 파일·materialized tree size 제한 | 실제 repo 측정·VSS fingerprint 합의 | 임의 작은 제한 금지 |
| `LIVE-11` | Snapshot/content/staging/revision retention | 보안·운영 승인 | 자동 삭제 금지 |
| `LIVE-12` | Backend TLS·방화벽·인증 정책 | 배포 토폴로지 검토 | production 공개 금지 |
| `LIVE-13` | Admin 저장소, URL, CORS origin, IdP/RBAC | 브라우저 E2E | Admin production GO 금지 |
| `LIVE-14` | Git provider 접근과 credential 소유권 | 인프라 승인 | remote write 금지 |
| `LIVE-15` | Chat 소유권: Frontend 직결/VSS standalone/Backend | Frontend·VSS 팀 합의 | 기존 `11500` 경로 유지 |
| `LIVE-16` | Frontend 조회 proxy·레거시 route 소유권과 project ID 매핑 | 실제 Sidebar E2E | 전체 Frontend GO 금지 |

`LIVE-01`~`LIVE-09`는 실제 Snapshot→VSS E2E의 차단 조건입니다. `LIVE-10`~`LIVE-16`는
로컬 contract test를 막지 않지만 Production GO 전에 확정합니다.

## 확인된 VSS 계약 공백

### HTTP 배포와 공유 경로

VSS는 Backend에 설치하는 Python package가 아니라 별도 HTTP 서버입니다. 따라서 다음
두 가지를 배포 계약으로 고정해야 합니다.

```text
Backend가 호출할 VSS_BASE_URL과 인증 방식
VSS가 Backend의 materialized project_root를 동일한 절대경로로 읽을 수 있는 mount
실행 중인 VSS artifact가 기준 main SHA에서 만들어졌다는 증거
```

소스 복사, 임의 `sys.path`, VSS Store 직접 접근으로 HTTP 계약을 우회하지 않습니다.

### Revision

현 VSS는 최종 promotion에서 `git_head(project_root)`를 저장합니다. HTTP `POST /index`
body에는 authoritative revision 필드가 없으므로 `project_root`가 target revision을 checkout한
Git worktree여야 합니다.

필요 증거:

```text
materialized root의 git rev-parse HEAD == target_revision
VSS GET /index/status 응답의 index.commit == target_revision
```

local-only commit을 지원하려면 VSS HTTP 계약에 authoritative revision 필드를 추가하는
상류 합의가 필요합니다.

## 설정 검증

Backend 설정:

```text
DATABASE_URL
SNAPSHOT_MATERIALIZATION_ROOT
VSS_BASE_URL
VSS_TOKEN
VSS_CONNECT_TIMEOUT_SECONDS
VSS_READ_TIMEOUT_SECONDS
VSS_EXPECTED_SOURCE_REVISION
```

다음 설정은 VSS 서버가 소유합니다. Backend가 대신 설정하거나 응답으로 노출하지 않습니다.

```text
VSS_STORE
VSS_DATA_DIR
VSS_PG_DSN
VSS_PG_SCHEMA
VSS_OLLAMA_URL
VSS_EMBED_MODEL
VSS_EMBED_TIMEOUT
VSS_CHAT_MODEL
VSS_EXCLUDE_GLOBS
```

비밀값은 repr, 로그, error body, Admin response에 나오지 않아야 합니다.

## 필수 검증 순서

### 1. 정적 기준선

```powershell
git status -sb
git diff --check
git ls-remote https://github.com/h5vision/vision.git refs/heads/frontend
git ls-remote https://github.com/h5vision/vss_server.git `
  refs/heads/main `
  refs/heads/module
```

- 참조 checkout과 구현 worktree를 분리합니다.
- 사용자 미커밋 변경을 보존합니다.
- 기준 SHA drift가 있으면 문서와 fixture를 갱신합니다.

### 2. Contract test

- Frontend 실제 overlay, Unicode, rename/delete, empty commit, local-only-shaped SHA
- VSS `POST /index`: `202 accepted`, `409 already_running`, 입력 오류와 인증 오류
- VSS `GET /index/status`: running/done/failed와 nested `index.commit`
- VSS `GET /health`, `GET /projects`, `GET /index/exists`
- HTTP method, path, query, JSON field와 token header
- Admin Repository/Branch/VSS binding
- 모든 Backend 성공·실패 `reason/detail/retryable`

### 3. Unit test

- SHA와 경로·rename·중복 검증
- VSS 요청이 Frontend delta 필드나 미지원 revision 필드를 포함하지 않음
- VSS HTTP client timeout, 인증, transport/JSON/contract 오류 매핑
- 상태 전이와 exact completion
- materialization root boundary, symlink/junction escape
- atomic staging/promotion과 immutable path
- binding unique와 멱등성
- redaction과 안전한 error mapping

### 4. Integration test

Phase 3B-1의 app lifecycle/readiness와 조회 proxy는 완료했습니다. 다음은 Phase 4~5에서
추가해야 할 목표 검증입니다.

- FastAPI → DB → fake base source → materializer → fake VSS HTTP server
- VSS accepted/rejected/auth/timeout/invalid response
- 동시 동일 target 요청에서 `POST /index` 한 번
- DB 실패 전 VSS 미호출
- materialization 실패 전 VSS 미호출
- VSS accepted 뒤 결과 저장 실패를 성공으로 가장하지 않음
- status done + exact target만 completed
- done + null/다른 commit은 revision mismatch
- failed/aborted error 보존
- process restart 후 VSS status와 DB 대조
- Admin CRUD/RBAC/audit/CORS

### 5. VSS HTTP integration

fake VSS 서버 뒤 실제 VSS 서버와 격리된 test Store로 검증합니다.

```text
인증 설정별 POST /index 접수
202 접수 후 상태 running → indexing_lexical/promoting → done
실패 build가 이전 active index를 보존
GET /projects exact ID
같은 project 동시 시작 409 already_running
완료 index.commit이 target revision과 일치
```

### 6. 실환경 End-to-End

다음 증거를 비밀정보 없이 남깁니다.

```text
Frontend payload shape와 target SHA
Backend HTTP status/body/X-Request-ID
Snapshot ID와 상태 전이
materialized locator와 tree 검증 결과
materialized Git HEAD 증거
VSS POST /index HTTP status와 안전한 응답 요약
VSS 최종 state/index.commit/error
동일 target 재전송 결과
재시작 전후 recovery
각 단계 latency와 Frontend 10초 제한
Admin Branch별 이력·attempt·재시도
```

## 즉시 차단 조건

- Frontend 또는 VSS SHA drift를 검토하지 않음
- VSS URL, 인증 정책 또는 실행 artifact의 source revision을 확인할 수 없음
- VSS가 materialized `project_root`를 읽을 수 없음
- 활성 binding이 없거나 둘 이상임
- base 전체 트리를 확보하지 못함
- materialized path가 전용 root 밖이거나 symlink escape 가능
- target revision을 VSS 완료 commit으로 증명할 수 없음
- DB 최초 저장 또는 materialization 기록 실패
- VSS HTTP method/path/status/JSON 계약 불일치
- Admin 인증/RBAC 없이 mutation 노출
- retention 미확정 상태에서 자동 삭제
- 저장 방식 미확정 상태에서 Git remote 쓰기

차단 시 Frontend 필드 추가, 미지원 revision 필드, delta-only 디렉터리, 유사 project 자동 선택,
강제 재시도 또는 VSS Python 내부 직접 접근으로 우회하지 않습니다.

## Production GO

```text
LIVE-01 ~ LIVE-16 확인
contract/unit/integration/VSS HTTP test 전체 통과
실제 Frontend payload 수신
Snapshot DB와 전체 tree materialization 성공
target revision 증명
VSS 202 accepted 후 done + exact index.commit
인덱싱 실패 시 이전 active index 유지
동일 target 멱등성
프로세스 재시작 복구
Frontend 10초 이내 구조화 응답
로그·DB sample·browser에 content/credential 노출 없음
Admin TLS/RBAC/CORS/audit와 Branch별 이력·재시도
```
