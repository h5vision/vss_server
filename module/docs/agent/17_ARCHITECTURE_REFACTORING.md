# Architecture Refactoring Progress Log (Strangler Alignment)

> 정본 아키텍처 설계 문서: [`docs/architecture/ARCHITECTURE.md`](file:///c:/Users/kaypa/Documents/vss_server/module/docs/architecture/ARCHITECTURE.md)  
> 원칙: **기능을 멈추고 전면 재작성하는 리팩터링이 아닌, 동작을 100% 유지하면서 아키텍처 경계를 점진적으로 교정하는 Strangler 방식**

---

## 1. 전체 PR 로드맵 및 진행 상황

| PR | 작업 내용 | 상태 | DB 변경 | 비고 |
|:---|:---|:---:|:---:|:---|
| **PR 1** | **Admin Router 파일 물리 분할 (7개 하위 모듈 + aggregate router)** | **완료** | 없음 | URL/동작 100% 호환, 47 tests passed |
| **PR 2** | **`CompareRevisionsUseCase`, `MaterializeCommitUseCase` 도입 및 private `_git_client` 제거** | **완료** | 없음 | Router 비즈니스 로직 격리, 49 tests passed |
| **PR 3** | Bootstrap Composition Root 분리 (`backend/bootstrap/container.py`) | 예정 | 없음 | `app.py` 비대화 해소, 명시적 DI 컨테이너 도입 |
| **PR 4** | Git Ports 인터페이스 정의 및 Legacy Adapter 래퍼 연결 | 예정 | 없음 | `RemoteRefReader`, `RevisionComparator` 등 포트 분리 |
| **PR 5** | 하위 공통 `GitCommandRunner` 추출 및 보안 정책 중앙화 | 예정 | 없음 | Git CLI subprocess 격리 |
| **PR 6** | `RepositoryGitClient` 기능별 모듈 물리 분리 (`refs`, `objects`, `graph`, `comparison`) | 예정 | 없음 | 37KB God Adapter 해체 |
| **PR 7** | Repository sync orchestration 분해 (`ObserveRepository`, `ObserveBranches` 등) | 예정 | 없음 | Sync 부분 실패 격리 |
| **PR 8** | Snapshot 상태 전이를 중앙 `SnapshotStateMachine`으로 통합 | 예정 | 없음 | Compare-and-set 기반 전이 강제 |
| **PR 9** | Repository sync lease에 fencing token (`generation`) 추가 | 예정 | 있음 | Worker race condition 원천 방어 |
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

## 4. 다음 예정 작업 (PR 3)

- **목표**: `backend/app.py`의 Composition Root 분리 및 명시적 DI 컨테이너 도입
- **세부 내용**:
  1. `backend/bootstrap/container.py` 생성 (`ApplicationContainer` dataclass 및 `build_container(settings)`)
  2. `app.py`의 비대한 20개 이상 서비스 조립 코드를 `bootstrap/`으로 이관
  3. `app.state.*` 개별 객체 남발을 방지하고 `app.state.container`로 통합
  4. FastAPI `Depends(get_container)`를 통한 명시적 DI 체계 확립

