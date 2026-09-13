# Repository Storage Namespace

## 결정

Module은 Repository/Branch/Commit의 논리적 상태를 파일시스템 디렉터리 이름으로 표현하지 않습니다.
정본은 Snapshot PostgreSQL의 `repositories`, `tracked_branches`, `branch_head_history`,
`repository_commits` 및 `snapshots`입니다.

`SNAPSHOT_REPOSITORY_ROOT`는 영구 working copy 저장소가 아니라 **재생성 가능한 Git object cache**의
상위 디렉터리입니다.

```text
/home/ubuntu/repos/
└─ .repository-cache/
   ├─ <repository-id-A>.git
   └─ <repository-id-B>.git
```

각 cache는 bare Git repository이며 remote object fetch, commit graph 탐색, merge-base/delta 계산,
exact revision materialization에 사용됩니다. cache를 잃어도 DB의 논리적 Repository 상태가 사라지는
것은 아니며 remote에서 다시 구성할 수 있어야 합니다.

## Mutable branch worktree 제거

과거 Module은 다음 namespace에 Branch별 mutable checkout을 유지했습니다.

```text
/home/ubuntu/repos/.snapshot-worktrees/<repo>/<repository-id>/branches/<branch>
```

이 구조는 더 이상 런타임 계약이 아닙니다. Repository Sync는 branch working copy를 만들거나 refresh하지
않고 remote refs, object cache, commit catalog 및 Snapshot readiness만 갱신합니다. Index/Retry 역시
current HEAD를 특별 취급해 mutable checkout으로 바꾸지 않습니다.

따라서 새 코드가 `.snapshot-worktrees`를 생성하지 않으며, 기존 배포에서 남아 있는 디렉터리는 새 버전이
배포되어 참조가 없음을 확인한 뒤 운영 cleanup 대상으로 취급할 수 있습니다.

`/home/ubuntu/repos/<repo-name>` 형태의 예전 일반 clone도 Module DB의 Repository/Branch/Commit 정본이
아니며 새 저장 계약의 필수 요소가 아닙니다.

## Immutable Snapshot

VSS가 읽는 소스는 current HEAD와 historical commit을 구분하지 않고 항상 검증된 immutable exact
Snapshot입니다.

```text
Git remote
   │
   ├─ refs / objects ──> /home/ubuntu/repos/.repository-cache/<repository-id>.git
   │                         │
   │                         ├─ commit graph / delta
   │                         └─ exact revision materialization
   │
   └─────────────────────────────> /home/ubuntu/vss-snapshots/.../revisions/<commit-sha>
                                      │
                                      └─ VSS POST /index project_root
```

VSS 요청 전에 Module은 materialized locator와 target SHA를 검증합니다. 이미 검증된 immutable tree가
`project_root`가 되므로, 원격 Branch가 이후 이동해도 진행 중인 VSS Indexer가 읽는 소스는 변하지
않습니다.

## Identity

내부 정본은 문자열 하나가 아니라 구조화된 값입니다.

```text
repository_id
branch_ref
exact commit SHA
```

새 Module-managed VSS physical index ID는 Repository basename과 Branch에서 한 번만 파생합니다.
VSS PR #57부터 `<repo>@<branch>`는 여러 physical index를 고르는 logical selector이므로 Module이 그 exact
이름을 차지하지 않습니다.

```text
h5vision/vss_server + refs/heads/main
  logical selector -> vss_server@main
  Module physical  -> vss_server@main--module

h5vision/vss_server + refs/heads/module
  logical selector -> vss_server@module
  Module physical  -> vss_server@module--module
```

`feature/login`처럼 URL path나 namespace 구분자와 충돌할 수 있는 Branch는 충돌 방지 hash를 포함한
안전한 component로 정규화합니다. 이미 존재하는 `module-project`, `repo@branch`, `repo--branch` 같은
VSS project ID는 활성 인덱스를 끊을 수 있으므로 자동 데이터 migration으로 이름을 바꾸지 않습니다.
새 Tracked Branch와 새 Branch Binding부터 `--module` physical ID를 사용하며, Binding은 같은
Repository/Branch의 Tracked Branch가 이미 있으면 그 VSS project ID를 재사용합니다.

`vss_server@test-merge@test-merge`처럼 기존 project ID에 Branch를 다시 append하는 방식은 금지합니다.
ID는 언제나 Repository + Branch 원본 필드에서 재계산합니다.

## Storage ownership

```text
PostgreSQL vss_snapshot
  = Repository / Branch / HEAD / Commit / Snapshot 논리 상태의 정본

SNAPSHOT_REPOSITORY_ROOT/.repository-cache
  = 삭제 후 재생성 가능한 bare Git object cache

SNAPSHOT_MATERIALIZATION_ROOT
  = VSS에 공급하는 immutable exact revision source

VSS PostgreSQL / VSS data
  = chunk / embedding / lexical index / briefing 등 VSS가 소유하는 검색 산출물
```

Repository sync와 Snapshot materialization 자체는 VSS `/index`를 자동 호출하지 않습니다. Admin의
명시적 Index 동작만 Module-push 인덱싱을 시작하며 VSS가 유일한 실제 Indexer입니다.
