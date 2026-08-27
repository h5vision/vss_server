# 코드 리뷰 및 명세 정합성 검토 보고서

**작성일시**: 2026-08-27 KST (Phase 3A-1 영속화 기반 보강 반영)
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
| **DB 영속화 계층** | 3A-1 | 🟢 **로컬 완료** | ORM 모델 6종, Alembic `0001`/`0002`, Repository/Binding 저장소, 부분 유니크·멱등·상태·attempt 제약. 실제 PostgreSQL 적용은 `LIVE-03` 대기 |
| **Admin Mutation & UI** | 3A-2 | ⚪ **대기** | Admin mutation API, RBAC/인증, 독립 Admin Web |
| **VSS runtime 연결** | 3B-1 | ⚪ **다음 검토 단계** | app lifecycle/readiness 연결과 fake VSS integration. 실제 배포·shared path는 3B-2 외부 입력 대기 |
| **Materialization 엔진** | 4 | ⚪ **대기** | Git base tree 확보, staging delta 적용, immutable promotion |

**테스트**: 85개 전체 통과. Ruff 오류 0건. compileall 성공. PostgreSQL offline migration SQL 생성 성공.

현재 외부에서 사용 가능한 FastAPI route는 liveness/readiness 골격뿐입니다. Frontend
overlay, Admin CRUD, VSS runtime readiness, materialization route는 아직 등록되지 않았으며
schema·client·저장소의 존재를 E2E 완료로 해석하지 않습니다.

---

## 2. Phase 3A-1 구현 내역

### (1) 데이터베이스 ORM 모델 (`backend/infrastructure/database/models/`)

- **`Repository` (`repositories`)**:
  - `repository_id` (UUID PK), `canonical_name` (UNIQUE), `display_name`, `provider`, `remote_url`, `default_branch_ref`, `active`
- **`BranchBinding` (`branch_bindings`)**:
  - `binding_id` (UUID PK), `frontend_project_id`, `repository_id` (FK), `branch_ref`, `vss_project_id`, `active`, `verified_at`
  - **부분 유니크 인덱스 (`uq_branch_bindings_active_frontend_project`)**: `active = true`인 경우 `frontend_project_id`당 최대 1개 바인딩만 허용
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
- `DATABASE_URL` 미설정 시 migration을 즉시 중단하며 예제 credential로 접속하지 않음

실제 PostgreSQL upgrade/downgrade는 `LIVE-03`의 DSN과 migration role이 제공된 뒤
검증합니다. 현재 완료 표시는 코드·SQLite ORM test·PostgreSQL offline DDL 기준입니다.

---

## 3. 검증 명령어

```powershell
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check backend tests alembic
.\.venv\Scripts\python.exe -m compileall -q backend alembic tests
```
