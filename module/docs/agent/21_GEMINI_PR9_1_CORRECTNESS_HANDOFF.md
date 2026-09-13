# Gemini Handoff — PR 9.1 Correctness Gate

최종 갱신: 2026-09-04 KST

## 먼저 알아야 할 상태

2026-09-03 ChatGPT 검수에서 Architecture Refactoring PR 6, PR 8, PR 9에 correctness gap이 확인됐습니다. 교정 코드와 테스트를 적용하고 전체 gate를 재검증한 뒤 2026-09-04 커밋 `60d96d1`로 고정했습니다.

**PR 10 durable job queue를 먼저 구현하지 마십시오.** 다음 구현은 PR 9.2 pre-rag contract alignment이며, Sync/Materialize와 Admin explicit Index 경계를 먼저 분리합니다.

## 적용된 변경 요약

### 1. Repository sync fencing

- `lease_generation`은 repository별로 단조 증가하며, 새 claim과 각 성공한 lease refresh가 다음 token을 발급합니다.
- `claim_sync()`는 repository row lock 아래 이전 generation 최대값 + 1을 새 run에 부여합니다.
- `refresh_lease()`는 DB `UPDATE ... WHERE sync_run_id=:id AND state='running' AND lease_generation=:expected AND lease_expires_at>:now RETURNING lease_generation`으로 atomic CAS 합니다.
- `assert_sync_owner()`는 동일 ownership 조건을 `FOR UPDATE`로 잠그고 검증합니다.
- fencing context를 `SyncRepositoryUseCase -> SyncTrackedBranchUseCase -> CollectedSnapshotPublisher`로 전달합니다.
- Branch HEAD/History와 Snapshot write 전에 ownership을 확인합니다.
- collection-owned VSS `start_index` 직전에 ownership row lock을 획득하고 VSS 결과 저장까지 같은 transaction의 lock을 유지합니다.

주의: 이것은 stale worker overlap을 줄이는 correctness gate입니다. VSS external call의 process-crash window까지 해결하는 durable outbox는 여전히 PR 12 범위입니다.

### 2. Snapshot StateMachine / retry

- 기존 Retry API가 `failed`, `rejected`, `aborted`를 재시도 대상으로 인정하므로 StateMachine도 `rejected`/`aborted -> materializing|submitting|completed|already_indexed` 경로를 허용하도록 계약을 복구했습니다.
- `completed`와 `already_indexed`는 계속 terminal입니다.
- Admin on-demand materialization의 직접 `snapshot.state = ...` 변경은 `SnapshotStore.set_state()`로 교정했습니다.

### 3. Git adapter regression fixes

- `snapshot_git_command_timeout_seconds`를 실제 `GitCommandRunner.default_timeout_seconds`에 wiring합니다.
- `ls-remote --tags` 결과에서 동일 direct/peeled ref duplicate를 거부하고 orphan peeled ref도 거부합니다.
- revision compare `changes`는 다시 path 기준 deterministic sort 후 반환합니다.
- `RevisionTreeMaterializer.checkout_revision()` 계약을 실제 구현과 맞게 `Path` 반환으로, `verify_checkout` 인자를 `expected_revision`으로 정정합니다.

## 검증 재현

변경을 다시 검증할 때 먼저 `git status`와 `git diff -- module/`을 확인해 사용자의 다른 dirty 변경을 보존합니다. 그 다음:

```bash
cd module
python -m ruff check backend admin_web tests alembic scripts
python -m compileall -q backend alembic tests scripts
python -m pytest -q
bash scripts/verify_module_sandbox.sh
```

### PostgreSQL concurrency 필수 케이스

1. 같은 `sync_run_id/generation`으로 두 transaction이 동시에 `refresh_lease`를 시도할 때 ownership semantics를 검증합니다.
2. lease 만료 후 Worker B가 새 generation을 claim한 뒤 Worker A가 `assert_sync_owner`, Branch HEAD/History write, Snapshot publish를 시도하면 `COLLECTION_SYNC_FENCING_TOKEN_INVALID`로 차단되어야 합니다.
3. VSS start 임계구간의 row lock으로 takeover가 overlap하지 않는지 확인합니다. 긴 upstream timeout 시 availability 영향도 기록합니다.
4. `rejected`와 `aborted` Snapshot retry integration test를 실제 `SnapshotRetryService` 경로로 추가/실행합니다.
5. Tag duplicate/orphan peeled response, compare ordering, configured Git timeout tests를 실행합니다.

## 완료 기록

다음 로컬 조건을 충족해 PR 9.1 코드를 커밋했습니다.

- 전체 test suite green
- Ruff/compileall green
- sandbox harness green
- PostgreSQL offline migration DDL green
- 기존 API response schema/URL 회귀 없음
- 사용자의 unrelated dirty files 변경 없음
- 문서의 검증 결과를 실제 숫자로 업데이트

실제 PostgreSQL fencing concurrency test는 로컬 Docker 부재로 실행하지 못했습니다. 같은
generation 동시 refresh와 lease takeover 뒤 stale worker 차단은 AWS 후속 gate입니다.

적용 commit:

```text
60d96d1 fix(refactor): close PR 8-9 correctness gaps before durable jobs
```

PR 9.2와 이후 변경도 `module` branch에서 fast-forward하며 강제 push하지 않습니다.

## 남은 구조적 부채

- `AdminStore.materialize_commit()`가 여전히 application orchestration과 filesystem side effect를 소유합니다. 상태 직접 mutation은 제거했지만 최종 목표는 `MaterializeCommitUseCase`가 orchestration을 소유하는 것입니다.
- `backend/ports/git.py`는 아직 일부 feature-owned DTO(`CommitGraphScanResult`, `RemoteBranchHead`, `RemoteTag`)를 import합니다.
- Snapshot 주요 경로는 StateMachine validation을 사용하지만 `transition_state()` CAS helper의 전면 적용은 별도 검토가 필요합니다.
- VSS external side effect의 crash-safe 전달은 PR 12 durable IndexCommand/outbox에서 해결해야 합니다.
