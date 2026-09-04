# Architecture Refactoring Progress Log (Strangler Alignment)

> 정본 아키텍처 설계 문서: [`docs/architecture/ARCHITECTURE.md`](file:///c:/Users/kaypa/Documents/vss_server/module/docs/architecture/ARCHITECTURE.md)  
> 원칙: **기능을 멈추고 전면 재작성하는 리팩터링이 아닌, 동작을 100% 유지하면서 아키텍처 경계를 점진적으로 교정하는 Strangler 방식**

---

## 1. 전체 PR 로드맵 및 진행 상황

| PR | 작업 내용 | 상태 | DB 변경 | 비고 |
|:---|:---|:---:|:---:|:---|
| **PR 1** | **Admin Router 파일 물리 분할 (7개 하위 모듈 + aggregate router)** | **완료** | 없음 | URL/동작 100% 호환, 47 tests passed |
| **PR 2** | **`CompareRevisionsUseCase`, `MaterializeCommitUseCase` 도입 및 private `_git_client` 제거** | **완료** | 없음 | Router 비즈니스 로직 격리, 49 tests passed |
| **PR 3** | **Bootstrap Composition Root 분리 (`backend/bootstrap/container.py`)** | **완료** | 없음 | `app.py` 323->152줄 축소, 224 tests passed |
| **PR 4** | **Git Ports 인터페이스 정의 및 Legacy Adapter 래퍼 연결** | **완료** | 없음 | `backend/ports/git.py` 5종 포트 정의, 53 tests passed |
| **PR 5** | **하위 공통 `GitCommandRunner` 추출 및 보안 정책 중앙화** | **완료** | 없음 | `backend/infrastructure/git/runner.py`, 58 tests passed |
| **PR 6** | **`RepositoryGitClient` 기능별 모듈 물리 분리 (`refs`, `objects`, `graph`, `comparison`)** | **완료** | 없음 | 37KB 해체, Facade 도입, 233 tests passed |
| **PR 7** | **Repository sync orchestration 분해 (`ObserveRepository`, `SyncTrackedBranch`, `SyncRepository`)** | **완료** | 없음 | UseCase 계층화 및 부분 실패 격리, 235 tests passed |
| **PR 8** | **Snapshot 상태 전이를 중앙 `SnapshotStateMachine`으로 통합** | **완료(초기)** | 없음 | StateMachine validation + CAS helper, 239 tests passed 기록; PR 9.1에서 retry contract 보정 |
| **PR 9** | **Repository sync lease에 fencing token (`generation`) 추가** | **완료(초기)** | 있음 | Alembic 0009, 242 tests passed 기록; PR 9.1에서 semantics 보강 |
| **PR 9.1** | **Correctness gate: fencing/StateMachine/Git 회귀 교정** | **완료** | 없음 | monotonic token + atomic lease CAS + side-effect ownership guard, 246 tests & sandbox passed |
| **PR 10**| PostgreSQL 기반 durable job queue 테이블 추가 (`snapshot.jobs`) | 예정 | 있음 | `SKIP LOCKED` 기반 백그라운드 큐 |
| **PR 11**| Snapshot Worker 프로세스 분리 (`python -m backend.worker`) | 예정 | 없음 | API 프로세스와 실행 라이프사이클 분리 |
| **PR 12**| VSS indexing을 durable `IndexCommand` / outbox로 분리 | 예정 | 있음 | 분산 트랜잭션 복구력 확보 |
| **PR 13**| VSS indexing reconciler 백그라운드 job 도입 | 예정 | 없음 | Status reconciliation 및 누락 복구 |
| **PR 14**| Revision Context (`vss_sources`) 계층 모듈화 및 projection 정리 | 예정 | 없음 | VSS Context Read Model 정립 |
| **PR 15**| Settings 논리 그룹 분리 및 아키텍처 규칙 정적 검사 CI 연결 | 예정 | 없음 | `Settings` 분할, 구조 유지 |

---

## 2. PR 1 완료 내역 (2026-09-03 KST)

### 1) 변경 개요
- 기존 1,046줄의 `backend/features/admin/router.py` 단일 모놀리스 파일을 외부 HTTP API 계약 변경 없이 7개의 도메인별 하위 라우터로 물리 분할했습니다.

### 2) 구조 변경
```text
module/backend/features/admin/
├─ router.py                    # Aggregate Router (include_router 7종 마운트)
├─ common.py                    # 공통 의존성 (DbSession, Viewer, Operator, Administrator) 및 헬퍼
└─ routers/
   ├─ __init__.py
   ├─ repositories.py           # /repositories, /branches, /sync, /repository-sync-runs
   ├─ tracked_branches.py       # /tracked-branches, /head-history
   ├─ bindings.py               # /branch-bindings
   ├─ snapshots.py              # /snapshots, /snapshots/{id}/retry
   ├─ commits.py                # /commits, /commits/{sha}, /compare, /materialize
   ├─ vss.py                    # /vss/projects
   └─ audit.py                  # /audit-logs
```

### 3) 검증 증거
- `ruff check backend/ tests/ admin_web/`: `All checks passed!`
- `compileall -q backend/ tests/ admin_web/`: 오류 없음
- `pytest tests/unit/admin/ tests/integration/test_admin_commit_* tests/integration/test_admin_api.py`: **47 passed in 7.36s (100% 통과)**

---

## 3. PR 2 완료 내역 (2026-09-03 KST)

### 1) 변경 개요
- 라우터(`routers/commits.py`)에 직접 산재해 있던 Git 비교 로직, Snapshot 승격 로직, `coll_svc._git_client` 같은 타 서비스 private 속성 접근, 동적 인스턴스화 fallback을 완전히 걷어내고, 독립적인 Application UseCase 계층으로 추출했습니다.

### 2) 구조 변경
```text
module/backend/features/admin/
├─ use_cases/
│  ├─ __init__.py
│  ├─ compare_revisions.py      # CompareRevisionsUseCase (RevisionComparator 프로토콜 정의)
│  └─ materialize_commit.py     # MaterializeCommitUseCase
├─ dependencies.py              # get_compare_revisions_use_case, get_materialize_commit_use_case
└─ routers/
   └─ commits.py                # 라우터는 271줄 -> 136줄로 축소, 오직 use_case.execute()만 호출
```

### 3) 검증 증거
- `test_admin_use_cases.py`: 신규 UseCase 단위 테스트 2종 추가
- `ruff check backend/ tests/ admin_web/`: `All checks passed!`
- `compileall -q backend/ tests/ admin_web/`: 정상 (0 exit code)
- `pytest tests/unit/admin/ tests/integration/test_admin_commit_* tests/integration/test_admin_api.py`: **49 passed in 7.24s (100% 통과)**

---

## 4. PR 3 완료 내역 (2026-09-03 KST)

### 1) 변경 개요
- 기존 `backend/app.py`의 lifespan에 엉켜 있던 180줄 이상의 거대한 서비스/클라이언트/스토어 초기화 및 조립 코드를 독립적인 Composition Root 모듈인 `backend/bootstrap/container.py`로 분리했습니다.
- `app.py`는 323줄에서 152줄로 절반 이하로 대폭 축소되었습니다.

### 2) 구조 변경
```text
module/backend/
├─ bootstrap/
│  ├─ __init__.py
│  └─ container.py              # ApplicationContainer (dataclass) & build_container() & get_container()
├─ app.py                       # lifespan은 build_container() 호출 및 container.dispose() 단 1줄로 단순화
└─ features/admin/dependencies.py # container 우선 참조 및 하위 호환 mock 보존
```

### 3) 검증 증거
- `test_container.py`: `test_build_container_with_defaults`, `test_container_dispose` 2종 단위 테스트 통과
- Admin 단위/통합 테스트: `49 passed in 7.73s`
- Sandbox 및 모듈 전체 테스트: **224 passed, 1 skipped in 61.24s (100% GREEN)**
- `ruff check backend/ tests/ admin_web/`: `All checks passed!`

---

## 5. PR 4 완료 내역 (2026-09-03 KST)

### 1) 변경 개요
- 37KB에 달하던 God Adapter인 `RepositoryGitClient`를 한 번에 쪼개기 전, 도메인과 유스케이스가 의존할 수 있는 세분화된 역량(Capability) 단위의 **Hexagonal Git Ports 인터페이스**를 정의했습니다.
- `backend/ports/git.py`에 `@runtime_checkable` Protocol 5종(`RemoteRefReader`, `RemoteObjectFetcher`, `CommitGraphReader`, `RevisionTreeMaterializer`, `RevisionComparator`) 및 복합 포트 `GitCapabilities`를 수립했습니다.
- `RepositoryGitClient`가 해당 포트들을 100% 만족함을 단위 테스트로 검증했습니다.

### 2) 구조 변경
```text
module/backend/
├─ ports/
│  ├─ __init__.py
│  └─ git.py                    # RemoteRefReader, RemoteObjectFetcher, CommitGraphReader,
│                               # RevisionTreeMaterializer, RevisionComparator, GitCapabilities
├─ features/admin/use_cases/
│  └─ compare_revisions.py      # 자체 임시 Protocol 대신 backend.ports.git.RevisionComparator 참조
└─ features/admin/
   └─ dependencies.py           # backend.ports.git.RevisionComparator 참조
```

### 3) 검증 증거
- `test_git_ports.py`: `RepositoryGitClient`의 모든 포트 구현 여부(`isinstance`) 및 mock 검증 통과 (2 tests passed)
- Admin 및 Ports 전체 테스트: **53 passed in 7.47s (100% GREEN)**
- `ruff check backend/ tests/ admin_web/`: `All checks passed!`

---

## 6. PR 5 완료 내역 (2026-09-03 KST)

### 1) 변경 개요
- `RepositoryGitClient` 내부에 직접 결합되어 있던 저수준 프로세스 실행, OS 레벨 권한 처리, symlink/junction 탈출 방어, timeout 및 credential 격리 책임을 `backend/infrastructure/git/runner.py`의 `GitCommandRunner`로 추출하고 중앙화했습니다.
- `RepositoryGitClient`는 저수준 `subprocess.run` 호출을 완전히 중단하고 `GitCommandRunner` 인스턴스에 안전하게 위임합니다.

### 2) 구조 변경
```text
module/backend/
├─ infrastructure/
│  └─ git/
│     ├─ __init__.py
│     └─ runner.py              # GitCommandRunner, assert_inside_root, is_link_or_junction,
│                               # is_sha, remove_readonly
└─ features/repository_collection/
   └─ git_client.py             # runner 주입 및 _run/_output/_is_sha 위임
```

### 3) 검증 증거
- `test_git_runner.py`: SHA 검증, path traversal 방어, 환경변수 격리, 타임아웃/에러 처리 5종 단위 테스트 통과
- Admin 및 Ports 전체 테스트: **58 passed in 7.59s (100% GREEN)**
- `ruff check backend/ tests/ admin_web/`: `All checks passed!`
- `compileall -q backend/ tests/ admin_web/`: 정상 (0 exit code)

---

## 7. PR 6 완료 내역 (2026-09-03 KST)

### 1) 변경 개요
- 37KB(1,006줄)에 달하던 단일 거대 클래스 `RepositoryGitClient`를 완전히 해체하고, 세분화된 도메인 역량(Capability)별 전용 어댑터 5종으로 물리 분할했습니다.
- `RepositoryGitClient`는 이제 내부 비즈니스 로직 없이 전용 어댑터들을 조합(Composition)하는 170줄의 초경량 호환성 Facade 클래스로 전환되었습니다.
- 기존 코드 및 테스트(233개 전체)와의 100% 하위 호환성을 완벽하게 보장했습니다.

### 2) 구조 변경
```text
module/backend/
├─ infrastructure/
│  └─ git/
│     ├─ __init__.py
│     ├─ runner.py              # GitCommandRunner (CLI subprocess 격리 및 보안)
│     ├─ layout.py              # GitCacheLayout (bare repository 경로 및 캐시 초기화)
│     ├─ refs.py                # GitRemoteRefAdapter (RemoteRefReader 구현)
│     ├─ objects.py             # GitRemoteObjectAdapter (RemoteObjectFetcher 구현)
│     ├─ graph.py               # GitCommitGraphAdapter (CommitGraphReader 구현)
│     ├─ checkout.py            # GitTreeCheckoutAdapter (RevisionTreeMaterializer 구현)
│     └─ comparison.py          # GitRevisionCompareAdapter (RevisionComparator 구현)
└─ features/repository_collection/
   └─ git_client.py             # 1000줄 -> 170줄 경량 Facade로 축소 (GitCapabilities 구현)
```

### 3) 검증 증거
- `test_git_adapters.py`: 각 개별 어댑터의 Port 구현 여부 및 Facade 합성 단위 테스트 2종 통과
- `test_git_client.py` & `test_git_compare.py`: Git 기능 14종 통합 검증 통과
- 모듈 전체 회귀 테스트 스위트: **233 passed, 1 skipped in 61.88s (100% GREEN)**
- `ruff check backend/ tests/ admin_web/`: `All checks passed!`
- `compileall -q backend/ tests/ admin_web/`: 정상 (0 exit code)

---

## 8. PR 7 완료 내역 (2026-09-03 KST)

### 1) 변경 개요
- `RepositoryCollectionService`(737줄) 내부의 거대했던 단일 동기화 파이프라인을 단일 책임 원칙(SRP)과 Hexagonal Use Case 계층에 따라 전용 Use Case 3종으로 물리 분할했습니다.
- 브랜치 관측, 개별 추적 브랜치 동기화(fetch/상태 전이/Snapshot 승격), 저장소 레벨 Lease 수명 주기 및 하위 서비스(PR/Tag/Catalog) 오케스트레이션 책임을 완전히 격리했습니다.
- `RepositoryCollectionService`는 세 Use Case를 조립(Composition)하고 위임하는 경량 Coordinator로 전환되었으며, 기존 내부 API 및 테스트 훅에 대한 100% 하위 호환성을 완벽하게 보존했습니다.

### 2) 구조 변경
```text
module/backend/features/repository_collection/
├─ use_cases/
│  ├─ __init__.py
│  ├─ observe_repository.py      # ObserveRepositoryUseCase (원격 heads 조회 및 기본 브랜치 유효성 검증)
│  ├─ sync_tracked_branch.py     # SyncTrackedBranchUseCase (단일 브랜치 fetch, diff 판정, snapshot 생성/발행)
│  └─ orchestrate_sync.py        # SyncRepositoryUseCase (분산 lease claim/refresh/finish, 부분 실패 격리)
├─ service.py                    # UseCase들을 조합/위임하는 250줄의 경량 코디네이터 (100% 하위 호환)
└─ ...
```

### 3) 검증 증거
- `tests/unit/repository_collection/test_sync_use_cases.py`: Use Case 분리 단위 테스트 2종 작성 및 통과
- `tests/integration/test_repository_collection_flow.py`: 전체 수집/스냅샷 종단 통합 테스트 통과
- 모듈 전체 회귀 테스트 스위트: **235 passed, 1 skipped in 63.70s (100% GREEN)**
- `ruff check backend/ tests/ admin_web/`: `All checks passed!`
- `compileall -q backend/ tests/ admin_web/`: 정상 (0 exit code)

---

## 9. PR 8 완료 내역 (2026-09-03 KST)

### 1) 변경 개요
- `SnapshotStore`를 경유하는 주요 Snapshot 상태 변경에 중앙의 명시적인 수명 주기 규칙인 `SnapshotStateMachine` validation을 도입했습니다. 후속 검수에서 Admin on-demand materialize 경로의 직접 state assignment가 남아 있음을 확인했고 PR 9.1 작업본에서 `SnapshotStore.set_state()` 경유로 교정했습니다.
- 유효하지 않은 상태 전이(예: 종단 상태 `completed`/`already_indexed`/`rejected`에서 임의 상태로 역행 등) 발생 시 `InvalidStateTransitionError`를 발생시켜 도메인 불변식을 강력히 수호합니다.
- `SnapshotStore`에 Compare-and-Set(CAS) 원자적 상태 전이 메서드 `transition_state`를 추가하여 동시성 제어 기반을 다졌습니다.
- 재시도(Retry) 워크플로우(`failed` -> `materializing` / `submitting` / `completed` / `already_indexed`) 및 멱등적 자기 전이(Self-transition)를 정밀하게 지원합니다.

### 2) 구조 변경
```text
module/backend/features/snapshots/
├─ __init__.py                  # SnapshotStateMachine, InvalidStateTransitionError export
├─ schemas.py                   # SnapshotState, SnapshotSummaryResponse 등
├─ state_machine.py             # SnapshotStateMachine (허용 전이 매트릭스, validate_transition)
└─ store.py                     # set_state 시 StateMachine 검증 강제 및 CAS transition_state 제공
```

### 3) 검증 증거
- `tests/unit/snapshots/test_snapshot_state_machine.py`: 정상 경로, 재시도/종단 규칙, 비정상 전이 차단, `SnapshotStore.set_state` 연동 등 단위 테스트 4종 작성 및 100% 통과
- `tests/integration/test_snapshot_retry.py`: VSS 재시도 종단 통합 테스트 통과
- 모듈 전체 회귀 테스트 스위트: **239 passed, 1 skipped in 67.71s (100% GREEN)**
- `ruff check backend/ tests/ admin_web/`: `All checks passed!`
- `compileall -q backend/ tests/ admin_web/`: 정상 (0 exit code)

---

## 10. PR 9 완료 내역 (2026-09-03 KST)

### 1) 변경 개요
- 분산 Worker 환경에서 GC Pause, 네트워크 지연, 일시적 단절 등으로 Lease가 만료된 이전 프로세스가 뒤늦게 깨어나 완료 쓰기(`finish_sync`)나 Lease 연장(`refresh_lease`)을 시도할 때 발생할 수 있는 Race Condition(Split-Brain / Stale Write)을 원천 차단하기 위해 단조 증가 정수형 Fencing Token(`lease_generation`)을 도입했습니다.
- `RepositorySyncRun` 엔티티 및 DB 스키마에 `lease_generation` 컬럼(기본값 1, `lease_generation >= 1` 제약)을 추가하고, Alembic 마이그레이션 `0009_repository_sync_fencing.py`를 작성했습니다.
- 초기 PR 9에서는 `RepositoryCollectionStore.refresh_lease` 호출마다 generation을 증가시키고 Python 객체 값 비교로 `expected_generation`을 검증했습니다. 이후 검수에서 이 방식은 DB-level atomic CAS와 stale side-effect 차단을 완전히 보장하지 못하는 것으로 판정되어 PR 9.1에서 교정합니다.
- `COLLECTION_SYNC_FENCING_TOKEN_INVALID` (409 Conflict)는 유지하되, PR 9.1에서는 repository별 monotonic fencing token과 DB atomic ownership 검증을 사용합니다.
- `RepositorySyncResult` 및 Admin API 응답 스키마(`RepositorySyncRunItem`)에 `lease_generation`을 포함하여 추적성과 가시성을 확보했습니다.

### 2) 구조 변경
```text
module/
├─ alembic/versions/
│  └─ 0009_repository_sync_fencing.py  # lease_generation 컬럼 및 ck_repository_sync_runs_lease_generation 추가
└─ backend/
   ├─ infrastructure/database/models/
   │  └─ collection.py                 # RepositorySyncRun.lease_generation (Integer, default 1)
   ├─ features/repository_collection/
   │  ├─ schemas.py                    # RepositorySyncResult.lease_generation
   │  ├─ store.py                      # refresh_lease/finish_sync 시 expected_generation 검증
   │  └─ use_cases/orchestrate_sync.py # current_generation 추적 및 _progress/_finish_run 전달
   └─ features/admin/schemas.py        # RepositorySyncRunItem.lease_generation
```

### 3) 검증 증거
- `tests/unit/repository_collection/test_sync_fencing.py`: claim 시 초기화(1), refresh 시 단조 증가(1->2), 오래된 토큰 차단 예외 발생, finish 시 토큰 불일치 차단 등 단위 테스트 3종 작성 및 통과
- 모듈 전체 회귀 테스트 스위트: **242 passed, 1 skipped in 67.94s (100% GREEN)**
- `ruff check backend/ tests/ admin_web/ alembic/`: `All checks passed!`
- `compileall -q backend/ tests/ admin_web/ alembic/`: 정상 (0 exit code)

---

---

## 10-1. PR 9.1 correctness gate 작업본 (2026-09-03 KST)

### 발견된 correctness gap

1. PR 9 token이 refresh마다 증가하고 `SELECT -> Python 비교 -> mutation`에 의존해 동일 generation 동시 refresh를 DB 원자적으로 배제하지 못했습니다.
2. fencing context가 `finish_sync`까지밖에 전달되지 않아 stale worker가 Branch HEAD/History, Snapshot 및 VSS side effect를 실행할 여지가 있었습니다.
3. PR 8 StateMachine은 `rejected`/`aborted`를 terminal로 만들었지만 `SnapshotRetryService`는 두 상태를 재시도 가능으로 유지하여 계약이 충돌했습니다.
4. PR 6 Git adapter 분리에서 timeout wiring, Tag response invariant, compare ordering, materializer port signature가 기존 동작과 어긋났습니다.

### Drive 작업본에 적용한 교정

- `claim_sync`: repository row lock 아래 과거 run의 `max(lease_generation) + 1`을 부여합니다. 새 claim과 각 성공한 lease refresh가 이전 값보다 큰 새 token을 발급합니다.
- `refresh_lease`: `UPDATE repository_sync_runs ... WHERE state='running' AND lease_generation=:expected AND lease_expires_at>:now RETURNING lease_generation` 형태의 atomic CAS를 사용합니다. 0 row이면 즉시 fencing loss입니다.
- `assert_sync_owner`: `sync_run_id + current generation + running + unexpired lease`를 `FOR UPDATE`로 검증해 중요 DB write / external side effect 임계구간을 보호합니다.
- `SyncRepositoryUseCase -> SyncTrackedBranchUseCase -> CollectedSnapshotPublisher`로 fencing context를 전달합니다. Branch/Snapshot 쓰기 전과 VSS `POST /index` 전 ownership을 재확인합니다. VSS 호출 직전 획득한 sync-run row lock은 결과 DB 반영까지 유지해 takeover와 외부 start를 겹치지 않게 합니다.
- `SnapshotStateMachine`의 `rejected`/`aborted` retry transition을 기존 Retry API 계약에 맞춰 복구했습니다. `completed`/`already_indexed`는 계속 terminal입니다.
- Admin materialize의 기존 직접 state assignment를 `SnapshotStore.set_state()` 경유로 변경했습니다. 단, AdminStore가 materialization orchestration까지 소유하는 구조적 부채 자체는 후속 application-layer 정리 대상으로 남습니다.
- `GitCommandRunner.default_timeout_seconds`가 `snapshot_git_command_timeout_seconds` 설정을 실제로 받도록 Composition Root wiring을 수정했습니다.
- remote tag duplicate/orphan peeled-ref 거부, compare change path 정렬, `RevisionTreeMaterializer.checkout_revision -> Path` / `expected_revision` 계약을 복구했습니다.

### 검증 상태

- 수정 Python 파일 `py_compile`: 통과.
- `ruff check backend admin_web tests alembic scripts`: **통과 (`All checks passed!`)**
- `compileall -q backend alembic tests scripts`: **통과 (0 exit code)**
- 전체 `pytest -q`: **246 passed, 1 skipped, 2 warnings in 67.12s (100% GREEN)**
- sandbox harness (`verify_module_sandbox.sh`): **통과 (`MODULE SANDBOX VERIFICATION: PASS`, Alembic 0009 head 및 PostgreSQL DDL 검증 완료)**
- GitHub commit/push: 검증 완료 후 사용자 승인 대기.

### Gemini 다음 행동

PR 9.1 correctness gate 검증이 모두 통과되었으므로 사용자 승인 후 commit/push (`fix(refactor): close PR 8-9 correctness gaps before durable jobs`)를 수행하고 PR 10으로 진행합니다.


## 11. 다음 예정 작업 (PR 10)

- **목표**: PostgreSQL 기반 durable job queue 테이블 추가 (`snapshot.jobs`)
- **세부 내용**:
  1. `backend/infrastructure/database/models/job.py` 구현 (`SnapshotJob` 모델):
     - `job_id`, `job_type`, `payload`, `state` (`pending`, `running`, `completed`, `failed`), `attempt_count`, `run_at`, `locked_at`, `locked_by`
  2. `alembic/versions/0010_durable_job_queue.py` 마이그레이션 스크립트 작성
  3. `backend/features/jobs/` 큐 추상화 및 `FOR UPDATE SKIP LOCKED` 기반 안전한 claim 로직 구축








