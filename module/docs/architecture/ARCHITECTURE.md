# VSS Snapshot / Revision Context Provider Architecture

## 2026-09-04 확정 운영 계약

이 절은 이전 문서의 충돌하는 자동 인덱싱·`vss_pull` 우선 표현보다 우선합니다.

- **VSS가 유일한 Indexer입니다.** Snapshot Module은 파일 수집 정책, chunking, embedding, BM25, vector/vector-store build·promote를 구현하거나 복제하지 않습니다. 실제 인덱싱은 `vss_server`의 `POST /index -> indexer.start_index()` 경로만 사용합니다.
- Repository 등록/동기화는 **인덱싱과 분리**합니다. 수집한 Repository는 `SNAPSHOT_REPOSITORY_ROOT=/home/ubuntu/repos` 아래 관리하고, sync는 clone/fetch·ref 관측·commit catalog 갱신까지만 수행하며 VSS `POST /index`를 자동 호출하지 않습니다.
- VSS에 넘길 입력은 mutable working copy가 아니라 `SNAPSHOT_MATERIALIZATION_ROOT=/home/ubuntu/vss-snapshots` 아래의 **검증된 immutable exact Snapshot**입니다. `VSS_REPOS_DIR=/home/ubuntu/repos`는 VSS의 repository 발견/표시 용도로 사용할 수 있지만 Module의 정식 `/index` 입력 경로는 아닙니다.
- 인덱싱 시작은 **Admin의 명시적 Index 요청**이 소유합니다. 목표 Admin API는 `POST /v1/admin/snapshots/{snapshot_id}/index`이며, materialized Snapshot만 대상으로 `project_root`, `project_id`, `force=false`, `briefing`, `note`를 VSS `POST /index`에 전달합니다. VSS의 `remote` clone 기능은 Module 연동 경로에서 사용하지 않습니다.
- Module은 VSS의 `GET /index/status`와 `GET /index/exists`를 관측하고, `state=done`뿐 아니라 `index.commit == snapshot.target_revision`까지 확인한 경우에만 Snapshot을 `completed`로 수렴시킵니다.
- 현재 운영 오케스트레이션 방향은 **`module_push`**이지만 의미는 “sync 시 자동 push”가 아니라 **Admin 요청으로 생성된 IndexCommand를 Module이 VSS에 제출**한다는 뜻입니다. `vss_pull`과 `/v1/internal/vss/*`는 provenance/read-model 및 향후 선택 기능으로 유지하며 현재 pre-rag VSS의 필수 data plane으로 간주하지 않습니다.
- Commit History/Compare는 Admin 분석 기능으로 유지합니다. **비교 결과로 reference commit SHA를 자동 선택하거나 VSS에 전달하는 기능, multi-revision 답변 context는 구현 보류**입니다.


> Target: `h5vision/vss_server` — `module` branch  
> Architecture style: **Modular Monolith + Hexagonal Architecture + Durable Background Jobs**  
> Primary responsibility: **Exact Git Revision → Immutable Snapshot → VSS Index → Revision Context Provenance**

---

## 1. Purpose

Snapshot Module은 Chat 또는 RAG 검색 엔진을 소유하지 않는다.

이 시스템의 책임은 다음 네 가지다.

1. Repository의 Branch, Tag, PR/MR 및 Commit 관계를 관측한다.
2. 특정 Git commit을 재현 가능한 immutable Snapshot으로 보존한다.
3. 해당 Snapshot과 VSS index 상태의 정확한 관계를 관리한다.
4. VSS가 특정 revision의 출처와 사용 가능 상태를 결정론적으로 조회할 수 있게 한다.

핵심 invariant는 다음과 같다.

```text
Repository Identity
      +
Exact Git Commit
      +
Immutable Source Tree
      +
VSS Index State
      =
Revision Context
```

Snapshot Module은 사용자의 자연어 질문이나 RAG 검색 정책을 소유하지 않는다.

---

## 2. Architecture Principles

### 2.1 Exact revision first

Branch, Tag, PR/MR은 mutable reference다.

시스템 내부 정본은 항상 다음 형태로 변환한다.

```text
Mutable Ref
    ↓ resolve
40-char Git Commit SHA
    ↓
Immutable Revision
```

모든 Snapshot, VSS indexing 및 provenance는 exact commit SHA를 기준으로 한다.

---

### 2.2 Git metadata와 Snapshot을 분리한다

모든 Commit을 Snapshot으로 만들지 않는다.

```text
Git Commit Catalog
    │
    ├─ metadata only
    ├─ parent graph
    ├─ refs
    └─ availability
          │
          │ selected
          ▼
      Snapshot
```

Repository history는 저비용 metadata graph로 보존한다.

실제 VSS 답변에 필요한 revision만 Snapshot으로 materialize한다.

---

### 2.3 Snapshot은 immutable하다

생성 완료된 revision directory는 수정하지 않는다.

```text
staging/<snapshot-id>
        │
        ├─ checkout
        ├─ overlay
        ├─ tree verification
        └─ HEAD verification
                │
                ▼
revisions/<commit-sha>
```

`revisions/<commit-sha>`는 생성 이후 read-only artifact로 취급한다.

재시도는 기존 immutable revision을 검증한 뒤 재사용한다.

---

### 2.4 VSS와 HTTP contract로만 통신한다

Snapshot Backend에서 다음을 금지한다.

```text
import vss.*
direct VSS Store access
direct Chroma access
VSS internal DB access
```

허용되는 의존성은 명시적 API contract뿐이다.

```text
Snapshot Module
      │
      ├── POST /index
      ├── GET  /index/status
      ├── GET  /index/exists
      └── GET  /health
              │
              ▼
             VSS
```

반대 방향에서도 VSS는 Snapshot DB를 직접 읽지 않는다.

```text
VSS
 │
 └── GET /v1/internal/vss/*   # optional/future provenance read-model
                     │
                     ▼
              Snapshot Module
```

---

## 3. System Context

Filesystem boundary:

```text
/home/ubuntu/repos/<repo>                         mutable managed repository
/home/ubuntu/repos/.repository-cache/<uuid>.git   internal bare Git object cache
/home/ubuntu/vss-snapshots/.../revisions/<sha>    immutable VSS index input
```

`SNAPSHOT_REPOSITORY_ROOT`와 `SNAPSHOT_MATERIALIZATION_ROOT`는 분리합니다. VSS의
`VSS_REPOS_DIR=/home/ubuntu/repos`는 repository discovery/UI에 사용할 수 있지만, Module이
`POST /index`에 전달하는 `project_root`는 immutable Snapshot 경로만 허용합니다.

PR 9.2-A부터 runtime Composition Root는 Git working copy와 bare object cache를 `SNAPSHOT_REPOSITORY_ROOT`에, exact revision artifact를 `SNAPSHOT_MATERIALIZATION_ROOT`에 각각 wiring합니다. 두 root는 동일하거나 서로 중첩될 수 없습니다.

```text
                              ┌───────────────────┐
                              │      Browser      │
                              └─────────┬─────────┘
                                        │
                                        ▼
                              ┌───────────────────┐
                              │     Admin Web     │
                              │      :4180        │
                              │ Session / CSRF    │
                              │ RBAC / BFF HMAC   │
                              └─────────┬─────────┘
                                        │ signed HTTP
                                        ▼
┌──────────────────┐          ┌─────────────────────────┐
│ GitHub / GitLab  │◄────────►│    Snapshot Backend     │
│ Git repositories │          │         :8000           │
└──────────────────┘          └────────────┬────────────┘
                                          │
                     ┌────────────────────┼────────────────────┐
                     │                    │                    │
                     ▼                    ▼                    ▼
              ┌────────────┐      ┌───────────────┐     ┌──────────────┐
              │ PostgreSQL │      │ Snapshot FS   │     │     VSS      │
              │   :5432    │      │ Git cache +   │     │    :8200     │
              │            │      │ revisions     │     │              │
              └────────────┘      └───────────────┘     └──────────────┘
```

운영에서는 Snapshot Backend와 VSS를 loopback에 유지한다.

Admin Browser만 승인된 ingress/TLS/VPN 경계를 통과한다.

---

## 4. Recommended Runtime Topology

HTTP request와 장시간 Git/VSS job의 실행 lifecycle을 분리한다.

```text
                    ┌────────────────────┐
                    │ Snapshot API       │
                    │ FastAPI            │
                    │ :8000              │
                    └─────────┬──────────┘
                              │
                              │ enqueue DB job
                              ▼
                    ┌────────────────────┐
                    │ PostgreSQL         │
                    │ source of truth    │
                    │ + job queue/outbox │
                    └─────────┬──────────┘
                              │
                              ▼
                    ┌────────────────────┐
                    │ Snapshot Worker    │
                    │                   │
                    │ Git / Materialize  │
                    │ Catalog / VSS      │
                    └────────────────────┘
```

초기에는 별도의 Redis, Kafka, RabbitMQ를 도입하지 않는다.

PostgreSQL의 durable job table과 `FOR UPDATE SKIP LOCKED`를 사용한다.

규모가 실제로 PostgreSQL queue 한계를 넘을 때만 message broker를 고려한다.

---

## 5. Bounded Contexts

시스템을 다음 bounded context로 정의한다.

```text
backend/
├─ repository/
├─ revision/
├─ snapshot/
├─ indexing/
├─ context/
├─ admin/
└─ shared/
```

각 context는 자신의 application/domain/adapter 경계를 가진다.

---

## 6. Repository Context

Repository Context는 mutable Git reference 관측을 소유한다.

책임:

```text
Repository registration
Branch catalog
Tracked branch
Branch HEAD observation
Tag observation
PR/MR observation
Provider integration
```

소유하지 않는 것:

```text
Snapshot lifecycle
VSS index state
Commit materialization
RAG query
```

권장 구조:

```text
backend/repository/
├─ domain/
│  ├─ repository.py
│  ├─ ref.py
│  ├─ observation.py
│  └─ errors.py
│
├─ application/
│  ├─ register_repository.py
│  ├─ observe_repository.py
│  ├─ observe_branches.py
│  ├─ observe_tags.py
│  └─ observe_change_requests.py
│
├─ ports/
│  ├─ repository_store.py
│  ├─ remote_ref_reader.py
│  ├─ change_request_provider.py
│  └─ event_sink.py
│
└─ adapters/
   ├─ postgres/
   ├─ git/
   ├─ github/
   └─ gitlab/
```

---

## 7. Revision Context

Revision Context는 Repository의 commit graph와 exact revision 관계를 소유한다.

책임:

```text
Commit metadata
Parent graph
Revision availability
Revision comparison (Admin 분석용; VSS reference SHA 자동 전달은 보류)
Branch → commit
Tag → commit
PR/MR role → commit
```

구조:

```text
backend/revision/
├─ domain/
│  ├─ commit.py
│  ├─ revision.py
│  ├─ comparison.py
│  └─ availability.py
│
├─ application/
│  ├─ catalog_commits.py
│  ├─ resolve_revision.py
│  ├─ compare_revisions.py
│  └─ get_revision_status.py
│
├─ ports/
│  ├─ commit_catalog.py
│  ├─ commit_graph_reader.py
│  └─ revision_comparator.py
│
└─ adapters/
   ├─ postgres/
   └─ git/
```

Admin router가 Git CLI adapter를 직접 호출해서는 안 된다.

항상:

```text
HTTP
 ↓
CompareRevisionsUseCase
 ↓
RevisionComparator
 ↓
Git adapter
```

경로를 따른다.

---

## 8. Git Adapter Architecture

현재 하나의 거대한 Git client 대신 capability 기반 port를 사용한다.

```text
                     ┌──────────────────┐
                     │ GitCommandRunner │
                     └────────┬─────────┘
                              │
      ┌───────────────────────┼────────────────────────┐
      │                       │                        │
      ▼                       ▼                        ▼
RemoteRefReader       GitObjectRepository      CommitGraphReader
      │                       │                        │
      ├─ branches             ├─ fetch                ├─ scan
      ├─ tags                 ├─ preserve             └─ parents
      └─ remote HEAD          └─ verify
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
          RevisionMaterializer   RevisionComparator
```

모든 adapter는 하나의 hardened Git process runner를 공유한다.

`GitCommandRunner`는 다음 정책을 중앙에서 강제한다.

```text
GIT_TERMINAL_PROMPT=0
GIT_CONFIG_NOSYSTEM=1
GIT_CONFIG_GLOBAL=/dev/null
GCM_INTERACTIVE=Never
timeout
stdout/stderr sanitization
allowed exit codes
concurrency limit
```

---

## 9. Snapshot Context

Snapshot Context는 exact commit을 VSS가 읽을 수 있는 immutable artifact로 만드는 책임만 가진다.

```text
backend/snapshot/
├─ domain/
│  ├─ snapshot.py
│  ├─ state.py
│  ├─ attempt.py
│  └─ errors.py
│
├─ application/
│  ├─ request_snapshot.py
│  ├─ materialize_snapshot.py
│  ├─ retry_snapshot.py
│  └─ verify_snapshot.py
│
├─ ports/
│  ├─ snapshot_store.py
│  ├─ revision_source.py
│  └─ snapshot_storage.py
│
└─ adapters/
   ├─ postgres/
   └─ filesystem/
```

---

## 10. Snapshot State Machine

Snapshot transition을 서비스 곳곳에서 직접 수정하지 않는다.

정식 state machine을 사용한다.

```text
              ┌────────────┐
              │  received  │
              └──────┬─────┘
                     ▼
               ┌───────────┐
               │ validated │
               └─────┬─────┘
                     ▼
             ┌───────────────┐
             │ materializing │
             └───────┬───────┘
                     ▼
              ┌──────────────┐
              │ materialized │
              └──────┬───────┘
                     ▼
                 ┌────────┐
                 │ queued │
                 └───┬────┘
                     ▼
              ┌────────────┐
              │ submitting │
              └─────┬──────┘
                    ▼
                ┌──────────┐
                │ accepted │
                └────┬─────┘
                     ▼
                ┌──────────┐
                │ indexing │
                └────┬─────┘
                     ▼
                ┌───────────┐
                │ completed │
                └───────────┘
```

Terminal/error states:

```text
rejected
failed
aborted
already_indexed
```

Transition은 반드시 compare-and-set 방식으로 수행한다.

예:

```sql
UPDATE snapshots
SET state = 'materializing'
WHERE snapshot_id = :id
  AND state = 'validated';
```

영향 row가 0이면 다른 worker 또는 잘못된 transition으로 판단한다.

---

## 11. VSS Index Orchestration Context

이 context는 인덱스를 생성하지 않는다. 검증된 immutable Snapshot을 VSS의 HTTP API에 제출하고
VSS 상태를 관측하는 orchestration만 소유한다. 실제 `collect_files`, AST/line chunking, embedding,
BM25, vector store build/promote, briefing은 `vss_server`가 소유한다.

Repository sync는 IndexCommand를 자동 생성하지 않는다. Admin의 명시적 Index 요청만
`materialized -> queued` 전이를 시작한다.

```text
Materialized Snapshot
    │
    │ Admin POST /v1/admin/snapshots/{snapshot_id}/index
    ▼
IndexCommand
    │
    ▼
VSS Adapter
    │
    ├─ start
    ├─ status
    └─ exists
    │
    ▼
IndexAttempt
```

권장 데이터 모델:

```text
index_commands
--------------
command_id
snapshot_id
state
fencing_token
available_at
attempt_count
created_at
updated_at

index_attempts
--------------
attempt_id
command_id
request_id
started_at
finished_at
http_status
vss_state
reason
retryable
latency_ms
```

---

## 12. Durable Job / Outbox

장시간 작업을 HTTP request lifetime에 묶지 않는다.

권장 job type:

```text
repository.observe
repository.branch.fetch
repository.tags.observe
repository.change_requests.observe
revision.catalog
snapshot.materialize
vss.index.start      # Admin explicit IndexCommand만 생성
vss.index.reconcile
```

Job claim:

```sql
SELECT *
FROM jobs
WHERE state = 'ready'
  AND available_at <= now()
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

worker는 claim 시 fencing token을 증가시킨다.

---

## 13. Lease and Fencing

단순 lease만 사용하지 않는다.

```text
RepositorySyncLease
-------------------
repository_id
sync_run_id
generation
lease_expires_at
```

worker가 상태를 쓸 때마다:

```sql
WHERE sync_run_id = :run
AND generation = :generation
AND lease_expires_at > now()
```

조건을 확인한다.

따라서 만료된 worker A가 늦게 돌아와도 새 worker B의 상태를 덮어쓸 수 없다.

---

## 14. Context Provider

Context Provider는 VSS에 제공되는 read model이다.

Write domain의 내부 ORM을 그대로 노출하지 않는다.

```text
Repository Domain
Revision Domain
Snapshot Domain
Index Domain
       │
       ▼
Context Projection
       │
       ▼
VSS Context API
```

API:

```text
GET /v1/internal/vss/capabilities

GET /v1/internal/vss/source
GET /v1/internal/vss/revisions

GET /v1/internal/vss/refs
GET /v1/internal/vss/context

GET /v1/internal/vss/change-requests
GET /v1/internal/vss/change-requests/{provider}/{number}
```

Context resolution은 항상 deterministic해야 한다.

```text
revision selector
    → exact SHA

branch selector
    → currently observed HEAD SHA

tag selector
    → currently observed target SHA

PR/MR selector
    → base | head | merge SHA
```

응답에는 최소한 다음 provenance를 유지한다.

```text
repository_id
repository_name
selector
selected_revision
commit_sha
tree_sha
snapshot_id
snapshot_state
index_state
eligible_for_answer
```

---

## 15. Answer Eligibility

단순히 commit이 존재하는 것과 VSS가 답변에 사용할 수 있는 것은 구분한다.

권장 상태:

```text
git_only
materialized
indexing
vss_indexed
unavailable
```

최종 answer eligibility:

```text
eligible_for_answer =
    revision_exists
    AND snapshot_verified
    AND vss_index_completed
    AND vss_index_commit == selected_revision
```

`done`이라는 VSS 상태만으로 완료 처리하지 않는다.

---

## 16. Admin Architecture

Admin Web은 Control Plane이다.

```text
Browser
   ↓
Admin Web BFF
   ↓ signed request
Admin API
   ↓
Application Use Cases
```

Admin Router에는 다음을 넣지 않는다.

```text
SQLAlchemy query logic
Git subprocess
VSS client call
private service access
filesystem operation
business state transition
```

Router 책임은 다음으로 제한한다.

```text
HTTP input
Authentication / authorization
Use-case invocation
HTTP output
```

---

## 17. Admin API Modules

하나의 대형 router 대신 영역별 router를 사용한다.

```text
admin/http/
├─ repositories.py
├─ refs.py
├─ commits.py
├─ comparisons.py
├─ snapshots.py
├─ indexing.py
├─ audit.py
└─ dependencies.py
```

최종 등록만 aggregate router가 담당한다.

```python
admin_router.include_router(repositories.router)
admin_router.include_router(commits.router)
admin_router.include_router(snapshots.router)
```

---

## 18. Dependency Injection

DI framework는 필수가 아니다.

명시적 composition root를 사용한다.

```text
backend/bootstrap/
├─ container.py
├─ database.py
├─ git.py
├─ providers.py
├─ vss.py
└─ workers.py
```

개념:

```python
container = Container(
    repositories=...,
    revision_catalog=...,
    revision_comparator=...,
    snapshots=...,
    index_gateway=...,
)

app = create_http_app(container)
```

FastAPI router가 `app.state`의 내부 객체나 다른 service의 private field를 탐색하는 것을 금지한다.

---

## 19. Configuration

단일 Settings namespace를 logical configuration group으로 나눈다.

```text
Settings
├─ RuntimeSettings
├─ DatabaseSettings
├─ GitSettings
├─ SnapshotSettings
├─ CollectionSettings
├─ VssSettings
├─ ProviderSettings
│  ├─ GitHubSettings
│  └─ GitLabSettings
└─ SecuritySettings
```

환경변수 이름은 하위 호환성을 위해 기존 이름을 유지할 수 있다.

---

## 20. Persistence Ownership

테이블을 bounded context별로 분류한다.

```text
Repository
----------
repositories
tracked_branches
branch_head_history
repository_sync_runs
repository_tags
change_requests
change_request_revisions

Revision
--------
repository_commits
repository_commit_parents
commit_catalog_runs

Snapshot
--------
snapshots
snapshot_deltas

Indexing
--------
index_commands
snapshot_attempts

Admin
-----
audit_logs
branch_bindings
```

물리적으로는 동일 PostgreSQL `snapshot` schema를 사용한다.

현재 단계에서는 database-per-service로 나누지 않는다.

---

## 21. Transaction Boundary

하나의 DB transaction 안에서 외부 Git/VSS 요청을 오래 수행하지 않는다.

패턴:

```text
DB claim
COMMIT

external operation

DB compare-and-set
COMMIT
```

외부 side effect가 있는 작업에는 항상 durable command와 recovery path를 둔다.

---

## 22. Git Remote Security

Repository remote URL은 trusted configuration으로 취급하되 production에서는 provider policy를 둔다.

권장:

```text
allowed providers:
- github.com
- approved GitHub Enterprise hosts
- gitlab.com
- approved GitLab hosts
```

---

## 23. Observability

구조화 로그의 공통 context:

```text
request_id
sync_run_id
job_id
repository_id
snapshot_id
vss_project_id
target_revision
provider
operation
elapsed_ms
result
reason
```

---

## 24. Testing Strategy

테스트 pyramid:

```text
                    E2E
                 ──────────
               Integration
            ─────────────────
             Contract Tests
         ───────────────────────
                Unit
────────────────────────────────────
```

---

## 25. CI / Branch Policy

`module` 또는 향후 기본 개발 branch에는 branch protection을 활성화한다.

Required checks:

```text
ruff
compileall
pytest-unit
pytest-contract
pytest-integration
alembic-upgrade
postgresql-runtime
```

---

## 26. Documentation

phase 문서와 현재 아키텍처 정본을 분리한다.

```text
docs/
├─ architecture/
│  ├─ ARCHITECTURE.md
│  ├─ DATA_MODEL.md
│  ├─ STATE_MACHINES.md
│  └─ SECURITY.md
│
├─ adr/
│  ├─ ADR-001-vss-http-boundary.md
│  ├─ ADR-002-immutable-snapshots.md
│  ├─ ADR-003-postgres-job-queue.md
│  ├─ ADR-004-git-cache.md
│  └─ ADR-005-revision-context.md
│
└─ agent/
   └─ implementation phase documents
```

`architecture/`는 현재 시스템의 정본이다.

`agent/`는 implementation history와 future plan이다.

---

## 27. Recommended Target Directory

```text
module/
├─ backend/
│  │
│  ├─ bootstrap/
│  │  ├─ container.py
│  │  ├─ database.py
│  │  ├─ git.py
│  │  ├─ providers.py
│  │  ├─ vss.py
│  │  └─ workers.py
│  │
│  ├─ repository/
│  │  ├─ domain/
│  │  ├─ application/
│  │  ├─ ports/
│  │  └─ adapters/
│  │
│  ├─ revision/
│  │  ├─ domain/
│  │  ├─ application/
│  │  ├─ ports/
│  │  └─ adapters/
│  │
│  ├─ snapshot/
│  │  ├─ domain/
│  │  ├─ application/
│  │  ├─ ports/
│  │  └─ adapters/
│  │
│  ├─ indexing/
│  │  ├─ domain/
│  │  ├─ application/
│  │  ├─ ports/
│  │  └─ adapters/
│  │
│  ├─ context/
│  │  ├─ application/
│  │  ├─ projections/
│  │  └─ http/
│  │
│  ├─ admin/
│  │  └─ http/
│  │
│  ├─ shared/
│  │  ├─ errors/
│  │  ├─ logging/
│  │  └─ types/
│  │
│  └─ app.py
│
├─ admin_web/
├─ alembic/
├─ tests/
├─ docs/
│  ├─ architecture/
│  ├─ adr/
│  └─ agent/
│
├─ ops/
├─ scripts/
└─ pyproject.toml
```

---

## 28. Dependency Rule

가장 중요한 architecture rule이다.

```text
HTTP / Worker
      ↓
Application
      ↓
Domain
      ↑
Ports
      ↑
Adapters
```

---

## 29. Migration Strategy

전체 rewrite를 하지 않는다.

현재 기능을 보존한 채 strangler 방식으로 이동한다.

- **Stage 1 — Boundary Cleanup**: Admin router 분리, UseCase 생성, private `_git_client` 접근 제거, bootstrap/container 분리.
- **Stage 2 — Git Capability Split**: `RepositoryGitClient`를 `GitCommandRunner`와 개별 Port(`RemoteRefReader`, `GitObjectRepository`, `CommitGraphReader`, `RevisionComparator`, `RevisionMaterializer`)로 분리.
- **Stage 3 — Snapshot State Machine**: `snapshot.state` 직접 수정을 중앙 `SnapshotStateMachine`으로 통합.
- **Stage 4 — Durable Jobs**: Repository sync의 장시간 orchestration을 DB job으로 분리.
- **Stage 5 — Index Outbox**: VSS indexing을 durable `IndexCommand`로 분리.
- **Stage 6 — Read Model**: VSS Context API용 결정론적 projection/read model 도입.

---

## 30. Final Architecture Decision

선택: **Modular Monolith + Hexagonal Boundaries + PostgreSQL Durable Jobs + Separate API/Worker Processes**

> **A Revision Context Provider that turns mutable repository references into verified, immutable and VSS-answerable Git revisions with auditable provenance.**
