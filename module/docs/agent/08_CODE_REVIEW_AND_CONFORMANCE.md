# 코드 리뷰 및 명세 정합성 검토 보고서

**작성일시**: 2026-08-27 KST (Phase 3A-1 DB 모델 및 마이그레이션 구현 완료 반영)
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
| **VSS HTTP 클라이언트** | 2H | 🟢 **완료** | `VssHttpClient`(`client.py`) 178줄 구현, `VssModuleAdapter`(`adapter.py`) 삭제, 삭제 검증 테스트 포함 |
| **DB 영속화 계층** | 3A-1 | 🟢 **완료** | SQLAlchemy ORM 모델 6종, Alembic 마이그레이션(`0001_initial`), 부분 유니크 인덱스, 멱등성 제약 |
| **Admin Mutation & UI** | 3A-2 | ⚪ **대기** | Admin mutation API, RBAC/인증, 독립 Admin Web |
| **Materialization 엔진** | 4 | ⚪ **다음 단계** | Git base tree 확보, staging delta 적용, immutable promotion |

**테스트**: 81개 전체 통과 (`pytest -v`). Ruff 오류 0건. compileall 성공.

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
  - `delta_id` (UUID PK), `snapshot_id` (FK CASCADE), `status`, `path`, `old_path`, `encoding`, `content`, `content_locator`
- **`SnapshotAttempt` (`snapshot_attempts`)**:
  - `attempt_id` (UUID PK), `snapshot_id` (FK CASCADE), `request_id`, `attempt_number`, `upstream_status_code`, `vss_state`, `vss_reason`, `vss_detail`, `retryable`, `latency_ms`, `vss_result_json`
- **`AuditLog` (`audit_logs`)**:
  - `audit_id` (UUID PK), `request_id`, `actor`, `action`, `target_type`, `target_id`, `details`

### (2) DB 엔진 및 세션 관리 (`backend/infrastructure/database/`)

- `base.py`: PostgreSQL `snapshot` schema 기반 `DeclarativeBase`
- `engine.py`: `create_engine_from_url`, `create_sessionmaker`, `get_engine_from_settings` (PostgreSQL/asyncpg, SQLite/aiosqlite 지원)
- `session.py`: `get_db_session` (FastAPI 비동기 의존성 주입)

### (3) Alembic 마이그레이션

- `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`
- `alembic/versions/0001_initial_snapshot_schema.py`: PostgreSQL `snapshot` 스키마 생성, 테이블 6종 생성, 부분 유니크 인덱스 및 멱등 유니크 제약 적용

---

## 3. 검증 명령어

```powershell
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check backend tests alembic
.\.venv\Scripts\python.exe -m compileall -q backend alembic tests
```
