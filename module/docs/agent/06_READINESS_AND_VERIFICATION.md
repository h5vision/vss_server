# 구현 준비와 필수 검증

최종 확인일: 2026-08-27 KST

## 기준선

| 대상 | 기준 |
|---|---|
| Backend module | `vss_server/module`, 분기 기준 `main@e3e706e44c2843da2bf2a004e8d1a27d1b7c7aeb` |
| Frontend | `vision/frontend@8008a06c732f9ca4e895c4fd75d58c4ab9cf6e37` |
| VSS | `vss_server/main@802025884624e855a3d4406937855a61e2092346` |

VSS 기준 SHA의 commit 시각은 `2026-08-27T14:13:38+09:00`입니다. SHA가 바뀌면
`CHARTER.md`, `docs/API.md`, `vss/indexer.py`, `vss/config.py`, Store와 test를 다시
읽고 fixture와 문서를 먼저 갱신합니다.

## 확정된 경계

| 주체 | 대상 | 용도 |
|---|---|---|
| Frontend | `http://192.168.0.7/v1/workspace-overlays` | Git delta 전달 |
| Backend | `vss.indexer.start_index()` | 완성 디렉터리 비동기 인덱싱 |
| Backend | `vss.indexer.status()` | 진행·완료·실패 동기화 |
| Backend | PostgreSQL `snapshot` schema | Snapshot/binding/attempt 소유 |
| VSS | Chroma 또는 PostgreSQL `rag` schema | active index 소유 |
| Admin Web | Backend `/v1/admin/*` | Repository/Branch/VSS binding과 이력 |
| Frontend | `127.0.0.1:11500/api/chat` | 기존 AI 진입점, Snapshot과 별개 |

VSS standalone HTTP `8200`은 참조 저장소가 제공하는 별도 배포 모드입니다. 이번
통합은 Python module 방식이므로 Backend에서 `VSS_BASE_URL`이나 token을 사용하지
않습니다.

## 구현에 충분히 확정된 항목

- Frontend request 필드와 10초 timeout
- 40자리 Git SHA 및 POSIX 상대경로 검증
- 변경 후 전체 파일 content
- VSS 전체 디렉터리 재인덱싱과 build/promote 경계
- `start_index()` 현재 signature와 비동기 기본값
- `status()` 상태와 `index.commit` 위치
- `accepted=true`와 완료의 분리
- 실패 시 이전 active index 보존
- VSS import 시 `VSS_*` 설정 singleton 생성
- VSS `JOBS`/Store의 process-local 성격
- `list_projects().note` 선택 필드와 query-only `VSS_PROJECT_ALIASES`
- Snapshot 인덱싱의 `vss_project_id`에는 alias를 적용하지 않는 exact ID 규칙
- 독립 Admin Web과 Branch별 VSS project binding
- 구조화된 성공·실패 reason/detail 응답

## 현재 확정되지 않은 필수 입력값

| ID | 필요한 값 | 확인 방법 | 미확정 시 동작 |
|---|---|---|---|
| `LIVE-01` | 설치 가능한 VSS package 공급 방식과 exact SHA pin | 깨끗한 환경 install/import | VSS readiness 실패 |
| `LIVE-02` | explicit revision 지원 또는 target Git worktree 공급 방식 | `start_index` signature와 완료 commit E2E | local-only/비-Git materialization 차단 |
| `LIVE-03` | PostgreSQL `DATABASE_URL`, migration 권한 | `snapshot` schema migration dry run | Snapshot 접수 금지 |
| `LIVE-04` | `SNAPSHOT_MATERIALIZATION_ROOT` 절대경로·용량·권한 | resolve/write/atomic rename probe | materialization 금지 |
| `LIVE-05` | Frontend별 Repository/Branch/`vss_project_id` binding | Admin 데이터와 실제 요청 비교 | 구조화된 `409` |
| `LIVE-06` | VSS Store `chroma|pgvector`와 `VSS_DATA_DIR`/`VSS_PG_DSN` | 실제 Store probe | 인덱싱 금지 |
| `LIVE-07` | Ollama URL, bge-m3 model과 embedding dimension | VSS embed smoke test | VSS dependency 실패 |
| `LIVE-08` | process topology: FastAPI 1 worker 또는 전용 worker | 배포 manifest와 동시 Job test | production GO 금지 |
| `LIVE-09` | 최초 base/full-tree bootstrap source | 실제 최초 Snapshot 복원 | 최초 인덱싱 금지 |
| `LIVE-10` | body·파일·단일 파일·materialized tree size 제한 | 실제 repo 측정·VSS fingerprint 합의 | 임의 작은 제한 금지 |
| `LIVE-11` | Snapshot/content/staging/revision retention | 보안·운영 승인 | 자동 삭제 금지 |
| `LIVE-12` | Backend TLS·방화벽·인증 정책 | 배포 토폴로지 검토 | production 공개 금지 |
| `LIVE-13` | Admin 저장소, URL, CORS origin, IdP/RBAC | 브라우저 E2E | Admin production GO 금지 |
| `LIVE-14` | Git provider 접근과 credential 소유권 | 인프라 승인 | remote write 금지 |
| `LIVE-15` | Chat 소유권: Frontend 직결/VSS standalone/Backend | Frontend·VSS 팀 합의 | 기존 `11500` 경로 유지 |

`LIVE-01`~`LIVE-09`는 실제 Snapshot→VSS E2E의 차단 조건입니다. `LIVE-10`~`LIVE-15`는
로컬 contract test를 막지 않지만 Production GO 전에 확정합니다.

## 확인된 VSS 계약 공백

### Packaging

`module/pyproject.toml`은 Snapshot Backend의 `backend*`만 설치합니다. 통합 시작 기준의
VSS에는 packaging metadata가 없으므로 `vss_server/main`의 exact SHA를 정상 package로
공급하는 upstream 보완이 필요합니다. main 파일을 module 경로로 복사하거나 임의
`sys.path`를 추가해서 이 공백을 숨기지 않습니다.

필요 증거:

```text
clean environment에서 Snapshot Backend와 VSS 별도 install 성공
import vss.indexer 성공
설치 source revision 확인
start_index/status/list_projects contract test 성공
```
### Revision

현 VSS는 최종 promotion에서 `git_head(project_root)`를 저장합니다. `extra_meta`에 commit을
넣어도 최종 값이 덮입니다.

필요 증거:

```text
materialized root의 git rev-parse HEAD == target_revision
VSS status.index.commit == target_revision
```

local-only commit 지원을 주장하려면 Backend에 Git object를 전달하는 별도 계약 또는
VSS의 authoritative `revision` argument가 필요합니다.

## 설정 검증

Backend 설정:

```text
DATABASE_URL
SNAPSHOT_MATERIALIZATION_ROOT
VSS_MODULE_NAME
VSS_SOURCE_REVISION
VSS_JOB_STALE_SECONDS
```

VSS-owned 설정:

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

비밀값은 repr, 로그, error body, Admin response에 나오지 않아야 합니다. VSS module을
먼저 import한 뒤 환경변수를 바꾸는 테스트는 유효한 설정 검증이 아닙니다.

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
- VSS `accepted=true/running`
- VSS `accepted=false/already_running`
- VSS `accepted=false/not_a_directory`
- VSS `done`과 nested `index.commit`
- VSS `failed/error/incomplete`
- public function과 `start_index` signature
- Admin Repository/Branch/VSS binding
- 모든 Backend 성공·실패 `reason/detail/retryable`

### 3. Unit test

- SHA와 경로·rename·중복 검증
- VSS command가 delta 필드를 포함하지 않음
- `expected_revision`이 현행 VSS 인자로 전달되지 않음
- adapter lazy import와 signature drift
- 상태 전이와 exact completion
- materialization root boundary, symlink/junction escape
- atomic staging/promotion과 immutable path
- binding unique와 멱등성
- redaction과 안전한 error mapping

### 4. Integration test

- FastAPI → DB → fake base source → materializer → fake VSS module
- VSS accepted/rejected/exception/invalid result
- 동시 동일 target 요청에서 VSS call 한 번
- DB 실패 전 VSS 미호출
- materialization 실패 전 VSS 미호출
- VSS accepted 뒤 결과 저장 실패를 성공으로 가장하지 않음
- status done + exact target만 completed
- done + null/다른 commit은 revision mismatch
- failed/aborted error 보존
- process restart 후 Store/DB 대조
- Admin CRUD/RBAC/audit/CORS

### 5. VSS module integration

실제 VSS package와 fake Store/embedder 또는 격리된 test Store로 검증합니다.

```text
VSS_* 설정 후 첫 import
start_index blocking=False 즉시 접수
상태 running → indexing_lexical/promoting → done
실패 build가 이전 active index를 보존
list_projects exact ID
같은 project 동시 시작 already_running
```

### 6. 실환경 End-to-End

다음 증거를 비밀정보 없이 남깁니다.

```text
Frontend payload shape와 target SHA
Backend HTTP status/body/X-Request-ID
Snapshot ID와 상태 전이
materialized locator와 tree 검증 결과
materialized Git HEAD 또는 explicit revision 증거
VSS start result
VSS final state/index.commit/error
동일 target 재전송 결과
재시작 전후 recovery
각 단계 latency와 Frontend 10초 제한
Admin Branch별 이력·attempt·재시도
```

## 즉시 차단 조건

- Frontend 또는 VSS SHA drift를 검토하지 않음
- VSS package/source revision을 확인할 수 없음
- `VSS_*` 설정 뒤 이미 module이 import됨
- 활성 binding이 없거나 둘 이상임
- base 전체 트리를 확보하지 못함
- materialized path가 전용 root 밖이거나 symlink escape 가능
- target revision을 VSS 완료 commit으로 증명할 수 없음
- 여러 worker가 동일 in-process VSS/Chroma를 소유
- DB 최초 저장 또는 materialization 기록 실패
- VSS public function/signature/return shape 불일치
- Admin 인증/RBAC 없이 mutation 노출
- retention 미확정 상태에서 자동 삭제
- 저장 방식 미확정 상태에서 Git remote 쓰기

차단 시 Frontend 필드 추가, 임의 revision, delta-only 디렉터리, 유사 project 자동 선택,
강제 재시도로 우회하지 않습니다.

## Production GO

```text
LIVE-01 ~ LIVE-15 확인
contract/unit/integration/module test 전체 통과
실제 Frontend payload 수신
Snapshot DB와 전체 tree materialization 성공
target revision 증명
VSS accepted 후 done + exact index.commit
인덱싱 실패 시 이전 active index 유지
동일 target 멱등성
프로세스 재시작 복구
Frontend 10초 이내 구조화 응답
로그·DB sample·browser에 content/credential 노출 없음
Admin TLS/RBAC/CORS/audit와 Branch별 이력·재시도
```
