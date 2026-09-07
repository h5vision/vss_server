# Repository Commit History와 Revision 비교

## 2026-09-04 확정 운영 계약

이 절은 이전 문서의 충돌하는 자동 인덱싱·`vss_pull` 우선 표현보다 우선합니다.

- **VSS가 유일한 Indexer입니다.** Snapshot Module은 파일 수집 정책, chunking, embedding, BM25, vector/vector-store build·promote를 구현하거나 복제하지 않습니다. 실제 인덱싱은 `vss_server`의 `POST /index -> indexer.start_index()` 경로만 사용합니다.
- Repository 등록/동기화는 **인덱싱과 분리**합니다. 수집한 Repository는 `SNAPSHOT_REPOSITORY_ROOT=/home/ubuntu/repos` 아래 관리하고, sync는 clone/fetch·ref 관측·commit catalog 갱신까지만 수행하며 VSS `POST /index`를 자동 호출하지 않습니다.
- VSS에 넘길 입력은 mutable working copy가 아니라 `SNAPSHOT_MATERIALIZATION_ROOT=/home/ubuntu/vss-snapshots` 아래의 **검증된 immutable exact Snapshot**입니다. `VSS_REPOS_DIR=/home/ubuntu/repos`는 VSS의 repository 발견/표시 용도로 사용할 수 있지만 Module의 정식 `/index` 입력 경로는 아닙니다.
- 인덱싱 시작은 **Admin의 명시적 Index 요청**이 소유합니다. 목표 Admin API는 `POST /v1/admin/snapshots/{snapshot_id}/index`이며, materialized Snapshot만 대상으로 `project_root`, `project_id`, `force=false`, `briefing`, `note`를 VSS `POST /index`에 전달합니다. VSS의 `remote` clone 기능은 Module 연동 경로에서 사용하지 않습니다.
- Module은 VSS의 `GET /index/status`와 `GET /index/exists`를 관측하고, `state=done`뿐 아니라 `index.commit == snapshot.target_revision`까지 확인한 경우에만 Snapshot을 `completed`로 수렴시킵니다.
- 현재 운영 오케스트레이션 방향은 **`module_push`**이지만 의미는 “sync 시 자동 push”가 아니라 **Admin 요청으로 생성된 IndexCommand를 Module이 VSS에 제출**한다는 뜻입니다. `vss_pull`과 `/v1/internal/vss/*`는 provenance/read-model 및 향후 선택 기능으로 유지하며 현재 pre-rag VSS의 필수 data plane으로 간주하지 않습니다.
- Commit History/Compare는 Admin 분석 기능으로 유지합니다. **비교 결과로 reference commit SHA를 자동 선택하거나 VSS에 전달하는 기능, multi-revision 답변 context는 구현 보류**입니다.


**합의일**: 2026-09-02 KST
**상태**: Phase 7A catalog + Phase 7B-2 Admin history/compare + 7B-3 materialize 로컬 완료; VSS reference SHA 전달은 보류

## 목적

Snapshot module은 Repository의 특정 시점을 재현하는 기능뿐 아니라, 어떤 commit들이
존재했고 Branch/Tag/PR/MR가 어느 commit을 가리켰는지 추적할 수 있어야 합니다. Admin은 이 정보를 사용해 과거 코드와 현재 코드, PR/MR 적용 전후, release와 main의 차이를
비교합니다. VSS가 비교 결과를 reference SHA로 받아 multi-revision 질의에 사용하는 기능은 현재 보류합니다.

현재 `branch_head_history`는 동기화 시점에 관측한 HEAD 변화만 저장하고 `snapshots`는 실제
materialize한 revision만 저장합니다. Branch가 `A -> D`로 이동했을 때 중간 commit `B`,
`C`를 Repository 역사로 조회하거나 비교하는 commit graph catalog는 아직 없습니다.

## 저장 비용 경계

모든 commit을 완전한 Snapshot과 VSS index로 만들지 않습니다.

```text
Commit Catalog
  모든 관측 가능한 commit의 SHA, tree, parent, 시간과 제한된 제목 metadata
  저비용 Repository 역사와 graph

Snapshot
  선택한 commit의 완전한 Git working tree
  재현·검증·비교 또는 VSS 제출이 필요한 revision만 생성

VSS Index
  AI 질의에 실제 사용할 Snapshot만 인덱싱
  done + exact index.commit이 확인된 revision만 answer-eligible
```

Snapshot 기본 생성 대상:

- 사용자가 추적하는 Branch의 관측 HEAD
- PR/MR base, head와 실제 merge commit
- 사용자가 pin한 중요 commit 또는 release Tag
- VSS가 historical context로 요청하고 운영 정책이 허용한 commit
- Admin operator가 명시적으로 materialize한 commit

commit catalog에 있다는 이유만으로 Snapshot이나 VSS Job을 자동 생성하지 않습니다.

## Commit Catalog 최소 모델

### `repository_commits`

```text
repository_commit_id
repository_id
commit_sha                     40자리 SHA-1
tree_sha                       40자리 SHA-1
author_name                    nullable, 길이 제한
authored_at                    timezone timestamp
committed_at                   timezone timestamp
subject                        첫 줄만, 길이 제한
first_seen_at / last_seen_at
```

- `(repository_id, commit_sha)`는 unique입니다.
- commit body, patch, 파일 본문과 author email은 기본 catalog에 저장하지 않습니다.
- `tree_sha`와 parent graph는 bare cache의 Git object에서 읽고 provider payload만 신뢰하지
  않습니다.
- commit object가 없는 SHA를 임의 metadata로 등록하지 않습니다.

### `repository_commit_parents`

```text
repository_commit_id
parent_commit_id
parent_sha                      parent가 catalog 밖이어도 보존
parent_order                   0부터 시작, merge parent 순서 보존
parent_missing_reason          scan_truncated | shallow_history | object_unavailable
```

- root commit은 parent가 없습니다.
- merge commit의 모든 parent와 순서를 보존합니다.
- 같은 Repository 안의 commit만 연결합니다.
- bounded/shallow scan으로 parent commit이 catalog에 없으면 `parent_commit_id`는 null로 두고
  `parent_sha`와 `parent_missing_reason`을 보존합니다.

### `commit_catalog_runs`

Repository별 실행은 별도 run과 lease로 직렬화합니다.

```text
run_id / request_id / repository_id
state / reason / detail / retryable
roots_json / unavailable_roots_json
max_commits / discovered_count / persisted_count
truncated / shallow / history_complete
started_at / lease_expires_at / finished_at
```

만료되지 않은 run이 있으면 `COMMIT_CATALOG_ALREADY_RUNNING`, 만료 run은
`COMMIT_CATALOG_LEASE_EXPIRED`로 실패 전환한 뒤 새 실행을 시작합니다. 재실행은 commit SHA와
parent order unique 제약을 사용해 멱등하게 upsert합니다.

### 기존 이력과의 연결

새 catalog를 정본 graph로 사용하되 기존 테이블의 SHA 이력을 제거하거나 덮어쓰지 않습니다.

```text
branch_head_history.previous/observed_head_sha -> repository_commits
change_request_revisions.base/head/merge_sha   -> repository_commits
snapshots.base/target_revision                  -> repository_commits
향후 Tag observation                            -> repository_commits
```

초기 catalog migration은 기존 SHA 컬럼에 즉시 FK를 추가하지 않아 배포 데이터를 깨지
않습니다. catalog backfill과 object 검증을 완료한 뒤 관계 제약 강화 여부를 결정합니다.

## 수집과 보존

1. tracked Branch current/history, 기존 Snapshot과 PR/MR revision SHA의 합집합을 root로 사용
2. 선택 Branch를 fetch하고 root에서 도달 가능한 commit graph를 `git rev-list --stdin`으로 탐색
3. 단일 structured `rev-list --format` 결과에서 SHA·tree·parent·시간·제목 검증
4. batch upsert로 catalog에 저장하고 기존 commit의 `last_seen_at` 갱신
5. Branch HEAD, PR/MR와 Snapshot SHA를 catalog에 연결
6. 관측 SHA용 `refs/vss-history/*` 보존 ref로 force-push 뒤 object 유실 방지

대규모 Repository에서는 전체 역사를 매번 다시 읽지 않습니다. 기존 catalog에 도달하면
탐색을 중단하고, 최초 등록의 최대 commit 수·시간·Git 실행 timeout을 운영 설정으로
제한합니다. shallow history를 완전한 역사로 표시하지 않습니다.

현재 운영 설정 기본값:

```text
SNAPSHOT_COMMIT_CATALOG_MAX_COMMITS=10000
SNAPSHOT_COMMIT_CATALOG_BATCH_SIZE=500
SNAPSHOT_COMMIT_CATALOG_TIMEOUT_SECONDS=120
SNAPSHOT_COMMIT_CATALOG_LEASE_SECONDS=600
SNAPSHOT_COMMIT_SUBJECT_MAX_LENGTH=256
```

lease는 scanner timeout보다 길어야 합니다. author name과 subject는 제어문자·개행을
공백으로 정규화하고 길이를 제한하며 author email과 commit body는 저장하지 않습니다.

## 구조화 오류

```text
COMMIT_CATALOG_ROOTS_REQUIRED
COMMIT_CATALOG_ROOT_INVALID
COMMIT_CATALOG_CACHE_UNAVAILABLE
COMMIT_CATALOG_ROOTS_UNAVAILABLE
COMMIT_CATALOG_OBJECT_FORMAT_UNSUPPORTED
COMMIT_CATALOG_GIT_FAILED
COMMIT_CATALOG_GIT_INVALID_RESPONSE
COMMIT_CATALOG_ALREADY_RUNNING
COMMIT_CATALOG_LEASE_EXPIRED
DATABASE_UNAVAILABLE
```

SHA-256 Repository는 현재 40자리 SHA-1 계약과 다르므로 fail closed합니다. unavailable root,
truncation과 shallow history는 성공 결과 안에서도 `history_complete=false`로 명시합니다.

## 비교 계약

비교는 DB에 파일 content를 복제하지 않고 collector-owned bare Git object에서 수행합니다.

```text
base revision 존재·Repository 일치 확인
target revision 존재·Repository 일치 확인
git diff --name-status --find-renames base target
git diff --shortstat base target
필요 시 merge-base 계산
결과를 안전한 경로와 통계로 변환
```

기본 Admin 응답:

```text
base_revision / target_revision / merge_base_revision
ahead_count / behind_count
files_changed / additions / deletions
added / modified / deleted / renamed 파일 경로
base/target Snapshot·VSS availability
```

- 기본 목록 API는 diff hunk와 파일 본문을 반환하지 않습니다.
- traversal, control character, binary와 지나치게 큰 결과를 구조화 reason으로 제한합니다.
- 비교 결과는 cache할 수 있지만 Git SHA와 옵션을 cache key로 사용합니다.
- 서로 다른 Repository commit 비교는 거부합니다.

## Admin API 방향

Phase 7B-2 제안이며 현재 구현된 route가 아닙니다.

```http
GET  /v1/admin/repositories/{repository_id}/commits
GET  /v1/admin/repositories/{repository_id}/commits/{commit_sha}
GET  /v1/admin/repositories/{repository_id}/compare?base_revision=<sha>&target_revision=<sha>
POST /v1/admin/repositories/{repository_id}/commits/{commit_sha}/materialize
```

목록은 cursor pagination과 Branch/Tag/PR/MR/Snapshot 상태 필터를 제공합니다. 비교와
materialize는 operator 이상, 읽기 목록·상세는 viewer 이상 역할을 사용하고 기존 HMAC·audit
경계를 유지합니다.

## Admin UI 방향

Repository 상세에 다음 view를 제공합니다.

- `Branches`: 현재 추적 상태와 관측 HEAD
- `Commit history`: SHA, parent, 시간, 제목과 연결된 ref/change request
- `Revision timeline`: Branch/Tag/PR/MR 관측과 Snapshot/VSS 상태
- `Compare`: base/target 선택과 변경 파일·통계
- `Change requests`: PR/MR base/head/merge와 commit graph 연결

commit 상태는 다음처럼 구분합니다.

```text
Git only       catalog와 bare object만 존재
Materialized   immutable Snapshot source 존재
VSS indexed    completed + done + exact index.commit
Unavailable    object/history가 불완전하거나 검증 실패
```

`Git only` commit의 materialize와 index는 서로 다른 명시적 operator 동작입니다. UI 목록 조회나
비교만으로 Snapshot/VSS Job을 자동 생성하지 않습니다. `Materialize`는 immutable Snapshot까지만
만들고, 별도 `Index` 요청이 해당 Snapshot을 VSS `POST /index`에 제출합니다.

## VSS 활용 — 현재 범위와 보류 범위

현재 Commit Catalog/Compare는 **Admin 분석 기능**입니다. 비교 결과로 reference commit SHA를
자동 선정하거나 VSS에 전달하지 않습니다. VSS에 제출되는 것은 Admin이 명시적으로 Index를
요청한 **단일 materialized Snapshot**뿐입니다.

다음 기능은 구현 보류입니다.

```text
현재 코드와 특정 과거 commit을 VSS가 자동 비교
버그가 처음 포함된 revision 범위를 VSS가 자동 탐색
PR/MR base/head를 자동 reference SHA로 전달
release Tag와 main을 multi-revision context로 전달
compare 결과를 답변용 historical context로 자동 구성
```

이 기능을 재개하기 전까지 Admin Compare는 UI/API에서 종료되며 VSS side effect가 없습니다.

## Phase 순서

### Phase 7A-2 - Commit Catalog

- `repository_commits`, `repository_commit_parents`, `commit_catalog_runs` ORM·migration·store
- bare cache commit graph scanner와 batch idempotency
- 기존 Branch/Snapshot/PR/MR SHA backfill
- force-push와 merge parent 보존 검증

로컬 완료 기록 — 2026-09-02 KST:

- ORM 3종과 Alembic `0007_commit_catalog`
- ordered parent와 catalog 밖 parent SHA/missing reason
- bounded `git rev-list --stdin` scanner, unavailable root·truncation·shallow 판정
- Repository별 run lease와 멱등 upsert
- tracked Branch/HEAD history/Snapshot/PR/MR root backfill
- Branch sync 완료 뒤 자동 catalog 실행
- 실제 local merge Git graph integration 통과
- Windows `180 passed, 1 skipped`, Ruff·compileall·offline DDL 통과
- 실제 PostgreSQL 17과 AWS backfill은 배포 검증 대기

### Phase 7A-3 - Ref와 Provider 연결

- Branch/Tag observation과 commit catalog 연결
- GitHub PR/GitLab MR read-only provider adapter
- provider base/head/merge SHA를 Git object로 재검증
- fork PR/MR ref fetch와 credential/rate-limit 정책

로컬 완료 기록 — 2026-09-03 KST:

- GitHub PR과 GitLab MR read-only REST adapter
- GitHub Link/GitLab X-Next-Page pagination과 bounded page 수
- target remote의 provider-owned PR/MR head ref fetch와 SHA 검증
- open PR synthetic merge SHA 배제, merged/squash final commit 구분
- lightweight/annotated Tag commit 정규화와 이동·삭제·재생성 이력
- Tag와 PR/MR revision을 commit catalog root에 포함
- opt-in 설정, token SecretStr·오류 body 비노출과 외부 제목 정규화
- provider page·Tag count 제한과 장시간 수집 중 sync lease 갱신

다음 구현 페이즈는 Phase 7B-3 On-demand Snapshot 승격입니다.

### Phase 7B-2 - Admin History와 Compare

- Repository commit 목록·상세·compare Admin API
- Repository 상세 history/timeline/compare UI
- cursor, RBAC, audit와 결과 크기 제한

로컬 완료 기록 — 2026-09-03 KST:

- Step 1: `RepositoryGitClient.compare_revisions` diff 엔진 (`core.quotepath=false`, path validation, sorted_changes)
- Step 2: REST API (`GET /commits`, `GET /commits/{sha}`, `GET /compare`, Keyset pagination, batch load status, RBAC/audit)
- Step 3: Admin Web UI (BFF proxy rules, `Commits` 탭, 2개 선택 Compare 모달, 4단계 상태 배지, 통계 그리드)
- 단위·통합·프록시·UI 테스트 통과 (전체 회귀 `215 passed, 1 skipped`)

### Phase 7B-3 - On-demand Snapshot 승격

- catalog commit의 object·tree 재검증
- 명시적 `vss_project_id`와 revision 정책
- immutable materialization과 중복 Snapshot 멱등성
- operator action과 attempt/audit 연결

로컬 완료 기록 — 2026-09-03 KST:

- Step 1 & 2 (Backend Core & REST API Layer):
  - `POST /v1/admin/repositories/{id}/commits/{sha}/materialize` 엔드포인트 구현 (Operator 이상 인가).
  - Git cache commit object 실재성 재검증(`RepositoryGitClient.has_commit`).
  - DB 기존 Snapshot 멱등성 조회(`created=False`, 중복 디스크 I/O 차단).
  - 신규 Snapshot 레코드 등록 및 `CollectedRevisionMaterializer`를 통한 immutable worktree 승격 (`materialized_locator` 저장).
  - `AuditLog` 테이블에 `action="materialize_commit"` 감사 로그 영속화.
  - 통합 테스트 `test_admin_commit_materialize_api.py` 5종 시나리오 통과 (전체 회귀 `220 passed, 1 skipped`).
- Step 3 (BFF Proxy & Admin Web UI Integration):
  - BFF 프록시(`admin_web/proxy.py`)에 `repositories/{id}/commits/{sha}/materialize` POST 화이트리스트 등록 (`operator`).
  - Admin Web UI(`admin_web/app.js`)의 `Commits` 뷰 테이블에서 `git_only` 상태인 커밋 행에 `Materialize` 액션 버튼 노출.
  - 커밋 상세 모달(`showCommitDetails`) 내부에도 `Materialize Snapshot` 승격 버튼 연동.
  - 클릭 시 확인 다이얼로그 후 비동기 호출, 완료 후 커밋 목록 자동 갱신 및 상태 뱃지 `Materialized` 전환.
  - 단위 테스트(`test_admin_web_proxy.py`, `test_admin_web_ui.py`) 단언문 추가 및 33개 admin 테스트 통과.

다음 구현 우선순위는 Managed Repository root 분리와 Admin explicit VSS Index orchestration입니다. Phase 7C의 multi-revision VSS context는 보류합니다.


### Phase 7C - Provenance Projection / Historical VSS Context 보류

- 유지: refs/commit graph/deterministic context read model
- 유지: exact Snapshot/VSS availability 확인
- 보류: 비교 질의의 base/target/reference SHA 자동 선택 및 VSS 전달
- 보류: multi-revision 답변 commit·파일 provenance E2E

### Phase 7D - 자동 갱신

- periodic Branch/Tag/PR/MR/commit catalog poller
- 조건부 GitHub/GitLab webhook fast signal
- retention, graph GC와 orphan object 정책

## 완료 조건

```text
Repository의 관측 가능한 commit graph와 merge parent를 조회 가능
Branch A -> D 사이 B/C commit을 history에서 확인 가능
force-push 뒤에도 관측된 commit과 parent graph 재현 가능
두 exact commit의 안전한 파일·통계 비교 가능
Commit Catalog, Snapshot, VSS Index 상태를 명확히 구분
Git only 조회가 Snapshot/VSS Job을 자동 생성하지 않음
operator가 과거 commit을 명시적으로 materialize 가능
PR/MR base/head/merge가 같은 commit graph에 연결
VSS 답변에서 사용한 revision과 비교 범위를 재현 가능
파일 본문, credential, Git stderr와 내부 cache path 비노출
```

## Phase 7A-2 테스트 매트릭스

```text
linear history의 중간 commit 보존
merge commit parent 순서
bounded scan의 unresolved parent_sha
unavailable root와 partial history
동일 graph 재실행 멱등성
Repository별 run lease와 만료 복구
Branch/Snapshot/PR/MR root 합집합
commit author/subject control character와 길이 제한
SHA-1 object format gate
Alembic 0007 upgrade/downgrade offline DDL
실제 PostgreSQL 17 migration·동시성은 외부 검증
```

## 비목표

- 모든 commit의 자동 Snapshot/VSS 인덱싱
- DB에 전체 patch나 파일 본문 영속화
- 다른 Repository 사이 commit 비교
- shallow/incomplete history를 전체 역사로 표시
- commit title 또는 author metadata를 AI 답변 근거로 무검증 신뢰
- Admin 목록 조회만으로 materialization이나 VSS index 시작
