# Snapshot/VSS 구현 계획

기존 Model 증분 HTTP 계획은 폐기하고 `vss_server/main`의 `/index` HTTP API와 전체
디렉터리 인덱싱 기준으로
재구성합니다.

## 트랙

```text
Backend       Frontend 수신 · Snapshot DB · materialization · VSS HTTP · 상태 복구
Admin Web     독립 브라우저 서버 · Repository/Branch binding · 이력 · 재시도
VSS core      HTTP API · explicit revision · shared path 안정화
```

## Phase 0R — 기준선 재고정

1. Frontend와 `vss_server/main|module` SHA 확인
2. `vss_server/main` SHA 확인과 별도 read-only checkout
3. CHARTER/API/indexer/config/store/test 확인
4. Frontend의 레거시 `/index/update/files` 활성 호출을 식별하고 호환 소유권 공백 등록
5. `/index`, `/index/status`, `/projects`, `/health` HTTP fixture 고정
6. shared filesystem과 explicit revision 공백 등록

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
VSS_BASE_URL=http://<VSS-SERVER>:8200
VSS_TOKEN=
VSS_CONNECT_TIMEOUT_SECONDS=2
VSS_READ_TIMEOUT_SECONDS=10
VSS_EXPECTED_SOURCE_REVISION=<배포 manifest의 vss_server/main exact SHA>
```

Backend는 VSS Store/Ollama 설정을 소유하지 않습니다. URL과 token을 로그에 노출하지 않고
readiness에서 `/health`와 `/projects`를 확인합니다.

## Phase 2H — HTTP 계약 전환

1. Frontend `WorkspaceOverlayRequest` 유지
2. VSS `/index`, `/index/status`, `/index/exists`, `/projects`, `/health` schema 구현
3. 인증·timeout·HTTP status·JSON shape를 검증하는 client 구현
4. materialized root가 주입된 `VssIndexRequest` mapper 구현
5. `vss_project_id` binding 계약으로 이름 변경
6. Snapshot 상태를 materializing/submitting/indexing 중심으로 변경
7. VSS 반환 reason/error와 Backend 구조화 오류 계약 작성
8. empty/local-only revision 제한을 명시
9. Admin fixture를 VSS 명칭·상태로 변경

완료 조건:

```text
현재 Frontend fixture validation 성공
VSS start accepted/already_running/status done/failed fixture validation 성공
HTTP method/path/body/status가 기준 SHA server 코드와 일치
Frontend delta가 VSS 파일 request로 변환되지 않음
expected_revision/snapshot_id가 지원되지 않는 VSS HTTP 필드로 전달되지 않음
done + exact index.commit만 completed
모든 오류에 reason/detail/retryable 정의
```

Phase 2H에서도 DB/materialization 없이 `/v1/workspace-overlays` 성공 route를 만들지
않습니다.

### Phase 2H 완료 기록 — 2026-08-27 KST

- Python direct-import adapter를 `module/backend/integrations/vss/client.py`로 교체
- Backend 전용 `snapshot_id/expected_revision`과 VSS `POST /index` body를 schema로 분리
- `VSS_BASE_URL`, token, connect/read timeout, expected source SHA 설정으로 전환
- `202 accepted`, `409 already_running`, auth, 4xx/5xx, transport와 invalid JSON 검증
- status/exists/projects/health exact path·query와 완료 revision 검증
- `ruff`, `compileall`, 전체 `73 passed`

### 기존 Phase 2R 기록의 정정 — 2026-08-27 KST

- `module/backend/integrations/rag_lab`과 기존 Model fixture 제거
- Frontend schema, binding, Snapshot/VSS 상태 schema와 기존 테스트는 재사용 가능
- 당시 direct-import adapter/config/schema/test는 최신 Snapshot HTTP 계약과
  불일치했으며 Phase 2H에서 제거됨
- 이 기록은 폐기된 구현을 다시 도입하지 않기 위한 역사로만 유지

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

## Phase 3B — VSS HTTP runtime client

1. app lifecycle에 Phase 2H HTTP client dependency 연결
2. readiness에서 실제 `/health`, `/projects` 확인
3. VSS 서버에서 materialized path 접근 가능 여부 probe
4. fake HTTP server 기반 integration test
5. 배포 manifest의 VSS source revision pin 확인
6. explicit revision upstream 지원 또는 Git worktree 제한 확정
7. Frontend `/v1/projects`, `/v1/briefing`, `/v1/models` proxy 형식 확정
8. workspace 이름과 overlay `project_id`를 exact binding으로 해석하는 규칙 확정

완료 조건:

```text
VSS /health와 /projects contract 확인
배포 revision이 VSS_EXPECTED_SOURCE_REVISION과 일치
HTTP contract drift → readiness 실패
동일 project 동시 submit → already_running 의미 보존
HTTP/auth/timeout 오류 → 안전한 구조화 오류
```

## Phase 4 — materialization과 VSS 제출

1. base tree source interface 구현
2. 전용 root/path boundary 구현
3. staging에 added/modified/deleted/rename 적용
4. target revision gate 구현
5. immutable revision promote와 locator 기록
6. VSS `POST /index` 제출
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
2. VSS `GET /index/status` 동기화
3. done + `index.commit==target_revision` 검증
4. failed/aborted/revision_mismatch 전이와 이유 보존
5. 재시작 후 VSS status/Backend DB 대조
6. 동일 Snapshot 재시도와 attempt 증가
7. Admin 상태·실패 이유·재시도 UI
8. Frontend 조회용 status/project/briefing 응답 변환

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

- VSS `/v1/chat` 소유권을 upstream과 확정
- Frontend의 현 `127.0.0.1:11500/api/chat` 변경은 Frontend 팀 계약으로 분리
- Snapshot phase 완료 조건에 Chat 경로 변경을 포함하지 않음

## Phase 6 — 실환경·보안·장애 검증

- PostgreSQL `snapshot`/`rag` schema role 분리
- VSS `/health`의 Store/Ollama availability
- Backend↔VSS 인증, network와 shared path
- 서버 재시작과 status polling 복구
- 인덱싱 실패 시 이전 active index 유지
- 최초 bootstrap, 삭제-only, rename, empty commit
- remote commit과 local-only commit
- Unicode, 대용량, 경로 traversal와 symlink
- disk full, DB 실패, VSS HTTP/Store 실패, API drift
- Admin TLS/CORS/RBAC/audit/credential 비노출
- retention과 orphan staging 정리

## 테스트 구조

```text
tests/contract
  Frontend JSON, VSS HTTP result/status, Admin JSON

tests/unit
  validation, mapper, HTTP client/error mapping, state, paths, idempotency

tests/integration
  FastAPI → DB → materializer → fake/real VSS HTTP → Admin API
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
VSS HTTP 202 accepted 기록
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
