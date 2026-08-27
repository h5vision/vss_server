# Snapshot/VSS 구현 계획

기존 Model 증분 HTTP 계획은 폐기하고 `vss_server` 모듈과 전체 디렉터리 인덱싱 기준으로
재구성합니다.

## 트랙

```text
Backend       Frontend 수신 · Snapshot DB · materialization · VSS module · 상태 복구
Admin Web     독립 브라우저 서버 · Repository/Branch binding · 이력 · 재시도
VSS core      explicit revision · 지원 public API 안정화
```

## Phase 0R — 기준선 재고정

1. Frontend와 `vss_server/main|module` SHA 확인
2. `vss_server/main` SHA 확인과 별도 read-only checkout
3. CHARTER/API/indexer/config/store/test 확인
4. `/index/update/files` 기준 문서·코드·fixture 제거
5. 모듈 public signature와 상태 fixture 고정
6. 통합 packaging 추가와 explicit revision 공백 등록

완료 조건:

```text
Frontend payload fixture 유지
VSS start/status fixture가 기준 SHA 코드와 일치
모든 문서가 vss_server를 현재 권위로 명시
과거 RAG Lab HTTP 설정·schema·fixture가 런타임 계약에 남지 않음
확인되지 않은 revision 지원을 가장하지 않음
```

## Phase 1 — 프로젝트 골격

기존 완료 범위를 유지합니다.

- FastAPI app factory
- 구조화된 오류와 `X-Request-ID`
- liveness/readiness
- 환경변수 설정

재기준화 설정:

```text
DATABASE_URL=
SNAPSHOT_MATERIALIZATION_ROOT=./data/snapshots
VSS_MODULE_NAME=vss.indexer
VSS_SOURCE_REVISION=<설치한 vss_server/main exact commit SHA>
VSS_JOB_STALE_SECONDS=300
VSS_STORE=chroma
VSS_DATA_DIR=./data/vss
VSS_OLLAMA_URL=http://127.0.0.1:11434
VSS_PG_DSN=
VSS_PG_SCHEMA=rag
```

VSS settings는 module import 전에 확정합니다. 내장 방식 초기 실행은 worker 1개입니다.

## Phase 2R — 계약 전환

1. Frontend `WorkspaceOverlayRequest` 유지
2. VSS `start_index/status/project` schema 구현
3. VSS lazy adapter와 public signature 검사
4. materialized root가 주입된 `VssIndexCommand` mapper 구현
5. `vss_project_id` binding 계약으로 이름 변경
6. Snapshot 상태를 materializing/submitting/indexing 중심으로 변경
7. VSS 반환 reason/error와 Backend 구조화 오류 계약 작성
8. empty/local-only revision 제한을 명시
9. Admin fixture를 VSS 명칭·상태로 변경

완료 조건:

```text
현재 Frontend fixture validation 성공
VSS start accepted/already_running/status done/failed fixture validation 성공
start_index kwargs가 기준 SHA signature와 일치
Frontend delta가 VSS 파일 request로 변환되지 않음
expected_revision이 가짜 VSS 인자로 전달되지 않음
done + exact index.commit만 completed
모든 오류에 reason/detail/retryable 정의
```

Phase 2R에서도 DB/materialization 없이 `/v1/workspace-overlays` 성공 route를 만들지
않습니다.

### Phase 2R 완료 기록 — 2026-08-27 KST

- `backend/integrations/rag_lab`과 기존 Model fixture 제거
- `backend/integrations/vss` schema, lazy adapter와 안전한 module 오류 구현
- VSS import 직전 Backend 설정의 `VSS_*` process environment 반영
- `VssIndexCommand`가 전체 `project_root`를 사용하고 Frontend delta를 VSS 인자로
  전달하지 않도록 고정
- Repository/Branch binding과 Snapshot schema를 `vss_project_id`, materialization,
  VSS Job 상태 기준으로 변경
- 기준 SHA의 `start_index` positional/keyword-only signature를 별도 checkout에서 확인
- contract/unit/integration 전체 통과, Ruff와 compileall 통과

## Phase 3A — Repository·Snapshot DB와 Admin API 골격

1. PostgreSQL `snapshot` schema migration
2. Repository, binding, Snapshot, delta, attempt, audit 모델
3. `frontend_project_id`당 활성 binding 하나 constraint
4. `(vss_project_id, target_revision)` 멱등 constraint
5. Repository/Binding CRUD와 soft deactivate
6. Admin 인증/RBAC와 audit
7. 독립 Admin Web 골격과 Repository/Binding UI

완료 조건:

```text
binding 없음·중복 → VSS 미호출 409
binding 수신값 → Snapshot에 불변 복사
독립 Branch → 별도 vss_project_id
mutation → 인증·권한·감사 기록
Admin Web → Backend 관리 API만 호출
```

## Phase 3B — VSS package·runtime adapter

1. `module/pyproject.toml`로 Snapshot Backend `backend*` package 설치
2. `vss_server/main`의 `vss*`를 exact SHA의 별도 package로 공급
3. 설치된 VSS source revision 검증
4. `VSS_*` import 이전 설정
5. `start_index/status/exists/list_projects` 실제 adapter
6. Store health와 단일 process ownership 검사
7. fake Store/embedder 기반 module integration test
8. explicit revision upstream 지원 또는 Git worktree 제한 확정

완료 조건:

```text
깨끗한 환경에서 VSS module import 가능
설치 revision이 VSS_SOURCE_REVISION과 일치
public signature drift → readiness 실패
동일 project 동시 submit → already_running 의미 보존
module 예외 → 안전한 구조화 오류
```

## Phase 4 — materialization과 VSS 제출

1. base tree source interface 구현
2. 전용 root/path boundary 구현
3. staging에 added/modified/deleted/rename 적용
4. target revision gate 구현
5. immutable revision promote와 locator 기록
6. `start_index(..., blocking=False)` 제출
7. 접수·거부·예외 attempt 저장
8. `/v1/workspace-overlays` 실제 route 연결
9. Snapshot 목록·상세 API와 Admin UI 연결

완료 조건:

```text
VSS가 변경 파일 묶음이 아닌 완성 tree를 수집
path traversal/symlink escape 차단
기존 revision 디렉터리 덮어쓰기 없음
VSS accepted → Frontend 202 VSS_INDEX_ACCEPTED
already_running → 409과 원인 표시
not_a_directory → 내부 materialization 오류로 분류
동일 target → 중복 Snapshot/Job 없음
Frontend 10초 안에 구조화 응답
```

## Phase 5 — 상태 동기화·복구·재시도

1. Backend `/v1/index/status`
2. VSS `status()` 동기화
3. done + `index.commit==target_revision` 검증
4. failed/aborted/revision_mismatch 전이와 이유 보존
5. 재시작 후 Store/DB 대조
6. 동일 Snapshot 재시도와 attempt 증가
7. Admin 상태·실패 이유·재시도 UI

완료 조건:

```text
accepted를 completed로 오판하지 않음
running/indexing_lexical/promoting → indexing
done + exact target → completed
done + null/다른 commit → failed revision mismatch
failed/aborted → error와 incomplete build 보존
재시도 → 새 Snapshot 없이 attempt만 증가
```

## Phase 5C — Chat 통합 선택 트랙

VSS의 `/v1/chat`/SSE를 Backend가 소유하기로 별도 합의한 경우에만 수행합니다.

- module 기반 chat public API가 있는지 upstream과 확정
- 없으면 VSS standalone HTTP 소유권 유지
- Frontend의 현 `127.0.0.1:11500/api/chat` 변경은 Frontend 팀 계약으로 분리
- Snapshot phase 완료 조건에 Chat 경로 변경을 포함하지 않음

## Phase 6 — 실환경·보안·장애 검증

- PostgreSQL `snapshot`/`rag` schema role 분리
- 선택 Store의 실제 연결과 migration
- Ollama/embed model availability
- worker 수와 process ownership
- 서버 재시작/daemon thread 중단 복구
- 인덱싱 실패 시 이전 active index 유지
- 최초 bootstrap, 삭제-only, rename, empty commit
- remote commit과 local-only commit
- Unicode, 대용량, 경로 traversal와 symlink
- disk full, DB 실패, Store 실패, module drift
- Admin TLS/CORS/RBAC/audit/credential 비노출
- retention과 orphan staging 정리

## 테스트 구조

```text
tests/contract
  Frontend JSON, VSS module result/status, Admin JSON

tests/unit
  validation, mapper, adapter signature, state, paths, idempotency

tests/integration
  FastAPI → DB → materializer → fake/real VSS module → Admin API
```

검증 명령:

```powershell
python -m compileall -q backend
python -m pytest -q tests/contract
python -m pytest -q tests/unit
python -m pytest -q tests/integration
python -m pytest -q
```

## 최초 Snapshot E2E GO

```text
실제 Frontend payload 수신
활성 Repository/Branch/VSS binding 확정
Snapshot/delta DB 저장
전체 target tree materialization
target revision 정합성 증명
VSS module accepted 기록
VSS done + exact index.commit 확인
동일 target 멱등성
재시작 복구
Frontend 10초 내 status와 성공·실패 이유 반환
Admin에서 Branch 이력·attempt·재시도 확인
로그/API/browser에 파일 본문·credential 없음
```

현재 VSS 기준 SHA에서 local-only commit을 full tree와 exact revision으로 보존하는 외부
계약이 없으므로, explicit revision 지원 또는 Git object 전달 방식이 확정되기 전에는
해당 시나리오를 GO로 표시하지 않습니다.
