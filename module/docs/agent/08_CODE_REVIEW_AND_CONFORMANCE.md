# 코드 리뷰 및 명세 정합성 검토 보고서

**작성일시**: 2026-08-28 KST (Phase 4 핵심 제출 흐름 반영)
**검토 대상 저장소/경로**: `vss_server.git` / `module` 브랜치 / `module/` 경로
**참조 명세 정본**: `docs/agent/05_IMPLEMENTATION_PLAN.md`
**참조 문서**: `docs/agent/01~07_*.md` 및 `AGENTS.md`

---

## 1. 종합 검토 요약

| 영역 | Phase | 정합성 상태 | 범위 및 근거 |
|---|---|:---:|---|
| **FastAPI 골격 & 코어** | 1 | 🟢 **완료** | `X-Request-ID` 미들웨어, 입력값 에코 없는 구조화 에러 핸들러, liveness/readiness 라우터 |
| **Frontend 수신 & 검증** | 2 | 🟢 **완료** | 40자리 Git SHA 필수, `extra='forbid'`, 안전한 POSIX 경로 검증, 디렉터리 탈출 차단 |
| **Admin & Snapshot 스키마** | 2 | 🟢 **완료** | `refs/heads/*` 브랜치 검증, Remote URL 계정정보 차단, `SnapshotState`/`SnapshotSourceType` enum 정의 |
| **VSS HTTP 클라이언트** | 2H | 🟢 **완료** | `VssHttpClient` 구현, Python direct-import adapter 제거, HTTP 경계 테스트 포함 |
| **DB 영속화 계층** | 3A-1/3B-1 | 🟢 **로컬 완료** | ORM 모델 6종, Alembic `0001`~`0003`, project/workspace exact binding 저장소, 부분 유니크·멱등·상태·attempt 제약. 실제 PostgreSQL 적용은 `LIVE-03` 대기 |
| **Admin Mutation & UI** | 3A-2 | ⚪ **대기** | Admin mutation API, RBAC/인증, 독립 Admin Web |
| **VSS runtime 연결** | 3B-1 | 🟢 **로컬 완료** | app lifespan, DB/VSS readiness, fake VSS integration, Frontend projects/models/briefing proxy. 실제 배포·shared path는 3B-2 외부 입력 대기 |
| **Materialization·제출** | 4 | 🟢 **로컬 완료** | Git base tree, staging overlay, target tree/HEAD gate, immutable promotion, Snapshot/attempt와 `/v1/workspace-overlays`→fake VSS. 실제 shared path E2E 대기 |
| **상태 동기화·복구** | 5 | ⚪ **다음 단계** | VSS status와 exact target 완료 판정, 재시작 복구·재시도 |

**테스트**: Contract 40 / Unit 51 / Integration 12, 총 103개 통과. Ruff 오류 0건.
compileall 성공. PostgreSQL offline migration SQL 생성 성공.

현재 FastAPI는 liveness/readiness와 Frontend `/v1/projects`, `/v1/models`,
`/v1/briefing` 조회 proxy 및 `POST /v1/workspace-overlays`를 제공합니다. Index status
동기화와 Admin CRUD는 아직 등록되지 않았으며 local Git/SQLite/fake VSS integration을
실환경 E2E 완료로 해석하지 않습니다.

---

## 2. Phase 3A-1 구현 내역

### (1) 데이터베이스 ORM 모델 (`backend/infrastructure/database/models/`)

- **`Repository` (`repositories`)**:
  - `repository_id` (UUID PK), `canonical_name` (UNIQUE), `display_name`, `provider`, `remote_url`, `default_branch_ref`, `active`
- **`BranchBinding` (`branch_bindings`)**:
  - `binding_id` (UUID PK), `frontend_project_id`, 선택적 `frontend_workspace_name`, `repository_id` (FK), `branch_ref`, `vss_project_id`, `active`, `verified_at`
  - **부분 유니크 인덱스 (`uq_branch_bindings_active_frontend_project`)**: `active = true`인 경우 `frontend_project_id`당 최대 1개 바인딩만 허용
  - **부분 유니크 인덱스 (`uq_branch_bindings_active_workspace_name`)**: 값이 있는 활성 workspace 이름당 최대 1개 바인딩만 허용
- **`Snapshot` (`snapshots`)**:
  - `snapshot_id` (UUID PK), `request_id`, `binding_id` (FK), `frontend_project_id`, `repository_id` (FK), `branch_ref`, `vss_project_id`, `base_revision`, `target_revision`, `source_type`, `state`, `attempt_count`, `materialized_locator`, `vss_state`, `vss_reason`, `vss_detail`
  - **멱등성 유니크 제약 (`uq_snapshots_vss_project_target_revision`)**: `(vss_project_id, target_revision)` 중복 생성 방지
- **`SnapshotDelta` (`snapshot_deltas`)**:
  - `delta_id` (UUID PK), `snapshot_id` (FK RESTRICT), `status`, `path`, `old_path`, `encoding`, `content`, `content_locator`
- **`SnapshotAttempt` (`snapshot_attempts`)**:
  - `attempt_id` (UUID PK), `snapshot_id` (FK RESTRICT), `request_id`, `attempt_number`, `upstream_status_code`, `vss_state`, `vss_reason`, `vss_detail`, `retryable`, `latency_ms`, `vss_result_json`
- **`AuditLog` (`audit_logs`)**:
  - `audit_id` (UUID PK), `request_id`, `actor`, `action`, `target_type`, `target_id`, `outcome`, `reason`, `detail`, 변경 전/후 JSON, 안전한 부가정보

Snapshot의 delta/attempt FK는 retention 확정 전 물리 삭제를 막도록 `RESTRICT`를
사용합니다. attempt 번호는 Snapshot 안에서 unique이고 상태/source/delta/latency/HTTP
status에는 DB check constraint를 적용합니다.

### (2) DB 엔진 및 세션 관리 (`backend/infrastructure/database/`)

- `base.py`: PostgreSQL `snapshot` schema 기반 `DeclarativeBase`
- `engine.py`: `create_engine_from_url`, `create_sessionmaker`, `get_engine_from_settings` (운영 PostgreSQL/asyncpg, 단위 테스트 SQLite/aiosqlite)
- `session.py`: `get_db_session` (FastAPI 비동기 의존성 주입)
- `features/repositories/store.py`: Repository/Binding create·list·update·soft deactivate와 exact active binding 해석

### (3) Alembic 마이그레이션

- `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`
- `alembic/versions/0001_initial_snapshot_schema.py`: PostgreSQL `snapshot` 스키마와 기본 테이블 6종
- `alembic/versions/0002_harden_snapshot_persistence.py`: retention-safe FK, 값 범위, attempt unique, 감사 필드 보강
- `alembic/versions/0003_add_workspace_binding_identifier.py`: Frontend workspace exact 조회 키와 활성 partial unique 보강
- `DATABASE_URL` 미설정 시 migration을 즉시 중단하며 예제 credential로 접속하지 않음

실제 PostgreSQL upgrade/downgrade는 `LIVE-03`의 DSN과 migration role이 제공된 뒤
검증합니다. 현재 완료 표시는 코드·SQLite ORM test·PostgreSQL offline DDL 기준입니다.

---

## 3. Phase 3B-1 구현 내역

- `backend/app.py`: lifespan에서 VSS client와 선택적 DB engine/sessionmaker를 소유하고
  종료 시 안전하게 정리
- `backend/features/health/`: DB `SELECT 1`과 VSS `/health`·`/projects` 계약을 확인해
  의존성별 구조화 readiness 반환
- `backend/features/frontend_proxy/`: VSS 응답을 실제 Frontend handler 형식으로 변환하는
  `/v1/projects`, `/v1/models`, `/v1/briefing` route 구현
- project catalog의 `project_root`, briefing의 `md_path`, upstream 원문 오류를 외부
  응답에서 제거
- overlay remote ID와 Sidebar workspace 이름을 별도 exact binding 키로 해석하고 fuzzy
  fallback을 사용하지 않음
- `/v1/index/status`는 Snapshot DB 상태 및 exact target revision 동기화가 필요한 Phase 5로
  유지하여 오해를 주는 부분 proxy를 만들지 않음

---

## 4. Phase 4 핵심 구현 내역

- `backend/features/materialization/`: 전용 root, UUID 기반 project key, staging/revision
  경계와 symlink/junction·immutable overwrite 차단
- binding branch를 read-only clone하고 base commit에 overlay를 적용한 뒤 `git write-tree`와
  target commit tree를 비교하여 exact revision만 허용
- target Git object가 없는 local-only commit은 `VSS_REVISION_CONTRACT_UNSUPPORTED`, tree
  불일치는 `SNAPSHOT_REVISION_MISMATCH`로 VSS 호출 전에 차단
- `backend/features/snapshots/store.py`: Snapshot/delta 최초 저장과 VSS attempt/result 저장
- `backend/features/workspace_overlays/service.py`: DB commit→materialize→VSS 순서, 중복
  target 멱등성과 accepted/rejected/error 상태 처리
- VSS 응답의 내부 `path`와 Git/subprocess 원문 오류는 API/attempt에 저장하지 않음
- 실제 PostgreSQL, remote Git latency, Backend/VSS shared mount와 Frontend 10초 제한은
  `LIVE-01`~`LIVE-09` 실환경 검증 대기

---

## 5. 검증 명령어

```powershell
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check backend tests alembic
.\.venv\Scripts\python.exe -m compileall -q backend alembic tests
```
