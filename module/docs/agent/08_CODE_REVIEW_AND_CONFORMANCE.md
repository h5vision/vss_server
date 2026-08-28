# 코드 리뷰 및 명세 정합성 검토 보고서

**작성일시**: 2026-08-28 KST (Phase 6B PostgreSQL 로컬 선행 검증 반영)
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
| **VSS source 조회** | 2V | 🟢 **로컬 완료** | 인증된 source/revision API, commit/tree SHA·clean tree 검증과 `/index` 호출값 제공 |
| **DB 영속화 계층** | 3A-1/3B-1 | 🟢 **로컬 완료** | ORM 모델 6종, Alembic `0001`~`0003`, exact binding 저장소, 부분 유니크·멱등·상태·attempt 제약. 격리 PostgreSQL 17 적용 통과, 운영 role/DSN은 `LIVE-03` 대기 |
| **Repository/Branch 수집** | 3A-2 | 🔴 **미구현** | remote catalog/fetch, 추적 Branch HEAD SHA·관측 이력과 collector-driven Snapshot 필요 |
| **Admin Mutation & UI** | 3A-3 | 🟡 **후속** | 수집 코어 뒤 CRUD·수동 sync·이력 UI, 인증/RBAC·독립 Admin Web 필요 |
| **VSS runtime 연결** | 3B-1 | 🟢 **로컬 완료** | app lifespan, DB/VSS readiness, fake VSS integration, Frontend projects/models/briefing proxy. 실제 배포·shared path는 3B-2 외부 입력 대기 |
| **Materialization·제출** | 4 | 🟢 **로컬 완료** | Git base tree, staging overlay, target tree/HEAD gate, immutable promotion, Snapshot/attempt와 `/v1/workspace-overlays`→fake VSS. 실제 shared path E2E 대기 |
| **상태 동기화·복구** | 5/6B | 🟢 **로컬 완료** | VSS status와 exact target 완료 판정, startup one-shot 복구·내부 재시도·PostgreSQL 단일 복구 조정자 잠금. AWS 다중 instance·실 VSS는 대기 |
| **장애·배포 사전 검증** | 6A | 🟢 **로컬 완료** | 한글 정책 주석, 장애 fixture, Ubuntu preflight, read-only smoke와 VSS 검증자 인계 |
| **PostgreSQL 실증** | 6B 선행 | 🟢 **로컬 완료** | 실제 upgrade/downgrade/re-upgrade, 동시 unique, 재시도 row lock과 startup recovery advisory lock 검증 |

**테스트**: Ubuntu Contract 40 / Unit 55 / Integration 29, 총 124개 통과. Windows는
123개 통과와 POSIX 권한 전용 1개 skip. Ruff 오류 0건. compileall, Ubuntu 24.04 non-root
컨테이너·preflight fixture와 PostgreSQL offline migration SQL 생성 성공. 별도 격리
PostgreSQL 17 실DB 테스트 4개도 통과했습니다.

현재 FastAPI는 liveness/readiness와 Frontend `/v1/projects`, `/v1/models`,
`/v1/briefing` 조회 proxy, `POST /v1/workspace-overlays`와 `GET /v1/index/status`를
제공합니다. 또한 VSS loopback caller용 `GET /v1/internal/vss/source`,
`GET /v1/internal/vss/revisions`를 제공합니다. Admin CRUD/retry route는 아직 등록되지 않았으며 local Git/SQLite/fake VSS integration을
실환경 E2E 완료로 해석하지 않습니다.

동일 AWS 인스턴스의 Linux service 배포는 Backend `127.0.0.1:8000`, VSS
`127.0.0.1:8200`, PostgreSQL `127.0.0.1:5432`로 고정합니다. 외부 Frontend/Admin
Browser는 HTTPS reverse proxy를 사용하며 서버 내부 loopback을 직접 호출하지 않습니다.
Phase 3A-2의 Repository/Branch 수집 코어는 착수 가능하며 Phase 3A-3 Admin CRUD는 인증과
감사 actor 신뢰 경계가 정해지기 전에는 외부 mutation을 공개할 수 없습니다.

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

격리 PostgreSQL 17의 upgrade/downgrade/re-upgrade와 schema/version/table 생성을
검증했습니다. 운영 DSN의 migration/runtime role 분리와 readiness는 `LIVE-03` 대기입니다.

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
- `/v1/index/status`는 Phase 5에서 Snapshot DB 상태와 exact target revision을 동기화하는
  별도 indexing service로 구현

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
- 운영 PostgreSQL, remote Git latency, Backend/VSS shared mount와 Frontend 10초 제한은
  `LIVE-01`~`LIVE-09` 실환경 검증 대기

---

## 5. Phase 5 핵심 구현 내역

- `backend/features/indexing/service.py`: exact binding의 최신 Snapshot과 VSS 상태 동기화
- `done`과 exact target commit이 함께 확인될 때만 `completed`; mismatch는 비재시도 실패
- VSS `none`은 `/index/exists`로 보완하되 다른 active commit을 성공으로 처리하지 않음
- `recovery.py`: 시작 시 non-terminal Snapshot을 batch 조회해 상태만 수렴하고 자동 재제출 금지
- `recovery_lock.py`: PostgreSQL DB 단위 advisory lock으로 다중 worker의 복구 중복 실행 차단
- `retry.py`: immutable Git tree/HEAD와 VSS Job을 재확인하고 동일 Snapshot attempt만 증가
- 인증 없는 retry route는 의도적으로 미노출; advisory lock의 AWS 다중 instance 장애 실증은
  운영 확장 전 과제

---

## 6. Phase 6A 핵심 구현 내역

- 정책 판단 주석을 한글로 기록하고 VSS failed/aborted/unavailable 상태를 fixture로 고정
- recovery unavailable이 자동 재제출하거나 attempt를 증가시키지 않는지 확인
- 실행 중 재시도, immutable tree 변조, write/permission 실패를 VSS 호출 전에 차단
- `scripts/preflight_ubuntu_24_04.sh`: service user의 환경·경로·VSS health 점검
- `scripts/smoke_backend_readiness.py`: 배포 Backend의 읽기 전용 health/status 계약 점검
- 실제 AWS 값은 `LIVE-01`~`LIVE-09` 대기이며 로컬 통과를 Production GO로 해석하지 않음

---

## 7. 검증 명령어

```powershell
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check backend tests alembic scripts
.\.venv\Scripts\python.exe -m compileall -q backend alembic tests scripts
.\.venv\Scripts\python.exe scripts\verify_postgresql_17.py
```
