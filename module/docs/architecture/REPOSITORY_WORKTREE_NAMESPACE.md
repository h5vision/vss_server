# Repository Branch Worktree Namespace 설계

## 목적

이 문서는 `module`이 인덱싱 대상 Git Repository의 Branch별 mutable working copy를 `SNAPSHOT_REPOSITORY_ROOT` 아래에 어떻게 저장하는지, 그리고 왜 기존의 `<repo>--<branch>` 평면 네이밍을 더 이상 사용하지 않는지를 설명합니다.

이 변경의 핵심은 단순한 폴더명 정리가 아닙니다. 기존 VSS/pre-rag 구현은 이미 `--`를 **Repository 이름과 index variant를 구분하는 의미 있는 namespace separator**로 사용합니다. 따라서 source working copy에서도 같은 separator를 Branch 구분에 재사용하면, Git source identity와 VSS index identity가 같은 문자열 문법을 공유하게 되어 장기적으로 해석 충돌이 생깁니다.

새 구조는 다음 네 가지 identity를 서로 다른 축으로 분리합니다.

- Repository identity
- Branch identity
- exact Git revision identity
- VSS index/profile identity

## 기존 구조와 문제점

기존 `module` 구현은 Tracked Branch별 working copy를 다음처럼 만들었습니다.

```text
/home/ubuntu/repos/<repo-basename>--<branch-component>
```

예:

```text
/home/ubuntu/repos/vss_server--main
/home/ubuntu/repos/vss_server--module
/home/ubuntu/repos/vss_server--pre-rag
/home/ubuntu/repos/vss_server--feature--login
```

이 방식은 Branch별 working copy를 독립적으로 유지한다는 목적 자체는 달성했습니다. 그러나 pre-rag/VSS 코드를 분석한 결과, `--`는 이미 VSS에서 index variant를 표현하는 문법으로 사용되고 있었습니다.

pre-rag의 `index_candidates()`는 `<repo>--*` 형태를 동일 Repository의 인덱스 후보로 취급합니다. `repo_map()` 역시 `project_id.split("--", 1)[0]`를 Repository 이름으로 간주합니다.

실제 AWS에는 다음과 같은 VSS project/index artifact가 존재합니다.

```text
api-test--ast-v2
api-test--ast-v3
cli--ast-v2
cli--ast-v3
sqlalchemy--ast-v2
sqlalchemy--ast-v3
```

따라서 아래 두 이름은 서로 다른 계층임에도 같은 separator 문법을 사용하게 됩니다.

```text
vss_server--module       # Git source working copy 이름
api-test--ast-v3         # VSS index variant 이름
```

이 상태를 계속 확장하면 다음처럼 의미가 섞인 이름이 나타날 수 있습니다.

```text
vss_server--module--ast-v3
```

이 문자열만 보고는 첫 번째 `--`가 Branch 구분인지, index variant 구분인지 명확히 알 수 없습니다.

## pre-rag의 Repository discovery와 또 다른 제약

pre-rag에는 `VSS_REPOS_DIR` 아래의 직계 자식 디렉터리를 Repository 후보로 스캔하는 코드가 있습니다.

개념적으로 다음과 같습니다.

```python
for d in base.iterdir():
    if not d.is_dir() or d.name.startswith("."):
        continue
```

즉 `/home/ubuntu/repos` 아래에 단순히 다음과 같은 owner/repo 계층을 추가하는 것도 안전하지 않습니다.

```text
/home/ubuntu/repos/h5vision/vss_server/branches/module
```

이 경우 legacy/pre-rag scanner는 `/home/ubuntu/repos/h5vision` 자체를 하나의 Repository처럼 볼 수 있습니다.

반대로 dot-prefixed directory는 명시적으로 건너뜁니다. 이 특성을 이용하면 module이 관리하는 Branch worktree를 기존 VSS Repository discovery와 분리할 수 있습니다.

## 새 정본 구조

새 구조는 다음과 같습니다.

```text
SNAPSHOT_REPOSITORY_ROOT=/home/ubuntu/repos

/home/ubuntu/repos/
├─ api_test/                         # 기존 VSS/pre-rag source repo
├─ cli/
├─ sqlalchemy/
├─ vision/
├─ .repository-cache/                # module bare Git object cache
└─ .snapshot-worktrees/              # module 전용 mutable worktree namespace
   └─ <repo-basename>/
      └─ <repository-id>/
         └─ branches/
            ├─ main/
            ├─ module/
            ├─ pre-rag/
            └─ <safe-branch-component>/
```

`vss_server` Repository ID가 예를 들어 다음이라면:

```text
851c55f9-fe3c-4e34-a32e-cb6c21adc17e
```

실제 경로는 다음 형태가 됩니다.

```text
/home/ubuntu/repos/.snapshot-worktrees/
  vss_server/
    851c55f9fe3c4e34a32ecb6c21adc17e/
      branches/
        main/
        module/
        pre-rag/
        test-merge/
```

UUID는 DB의 Repository identity와 동일한 값을 사용하며, 경로에는 hyphen 없는 `UUID.hex`를 사용합니다.

## 왜 Repository UUID 디렉터리가 필요한가

`canonical_name`의 basename만으로는 Repository identity가 유일하지 않습니다.

예:

```text
org-one/shared.git
org-two/shared.git
```

둘 다 basename은 `shared`입니다.

기존 평면 구조에서는 둘 다 `shared--main`을 요구하게 되어 collision을 409로 거절해야 했습니다.

새 구조에서는 다음처럼 자연스럽게 공존합니다.

```text
.snapshot-worktrees/shared/<repository-id-A>/branches/main
.snapshot-worktrees/shared/<repository-id-B>/branches/main
```

즉 사람이 읽기 쉬운 basename은 유지하면서, 실제 identity는 Repository UUID가 보장합니다.

## Branch 이름 규칙

Branch 이름은 하나의 filesystem component로 저장합니다.

단순한 Branch는 그대로 유지합니다.

```text
main      -> main
module    -> module
pre-rag   -> pre-rag
```

filesystem normalization이 필요한 Branch는 readable slug와 deterministic hash를 결합합니다.

예:

```text
feature/login     -> feature-login-<hash8>
feature--login    -> feature-login-<different-hash8>
```

규칙은 다음과 같습니다.

1. Git `refs/heads/*` 형식과 `git check-ref-format --branch` 검증은 기존대로 유지합니다.
2. filesystem에서 안전하지 않은 문자는 `-`로 정규화합니다.
3. 연속된 `--`는 단일 `-`로 축약합니다.
4. 원본 Branch 이름에서 변환이 발생했거나 길이 제한으로 잘렸다면 SHA-256 기반 8자리 hash를 붙입니다.
5. 최종 source-worktree 경로 component에는 `--`가 남지 않습니다.

이렇게 하면 VSS의 `--ast-v2`, `--ast-v3` index namespace와 source filesystem namespace를 명확하게 분리할 수 있습니다.

## `--` separator의 예약 범위

새 계약에서는 `--`를 module-managed source worktree naming에 사용하지 않습니다.

```text
Git source filesystem namespace
  .snapshot-worktrees/<repo>/<repository-id>/branches/<branch>

VSS index namespace
  <repo>--<index-variant>
  예: api-test--ast-v3
```

즉 `--`는 VSS project/index variant 쪽 의미로만 남겨 둡니다.

이 변경은 기존 VSS index 이름을 변경하는 작업이 아닙니다. VSS의 `project_id`, BM25 artifact, briefing artifact, vector collection의 기존 이름은 그대로 유지합니다.

## VSS Index 요청과의 관계

VSS `/index`는 `project_root`를 명시적으로 전달받기 때문에 source path가 flat한지 nested인지에 의존하지 않습니다.

current tracked HEAD의 인덱싱 흐름은 다음과 같습니다.

```text
Repository + Tracked Branch
        ↓
immutable Snapshot exact revision 검증
        ↓
.snapshot-worktrees/<repo>/<repository-id>/branches/<branch>
        ↓
remote HEAD == expected target SHA 검증
        ↓
working copy를 exact SHA로 checkout
        ↓
VSS POST /index
  project_root=<위 working copy>
        ↓
기존 VSS chunking / embedding / BM25 / vector build
```

따라서 이번 변경으로 수정되는 것은 **module이 VSS에 넘길 source working-copy 경로를 결정하는 방식**뿐입니다.

다음 기능은 변경하지 않습니다.

- VSS chunker
- embedding model 호출
- BM25 생성
- vector store build/promote
- VSS project/index naming
- immutable Snapshot 저장 구조

## immutable Snapshot 구조는 그대로 유지

historical/exact revision evidence는 계속 별도 root를 사용합니다.

```text
SNAPSHOT_MATERIALIZATION_ROOT=/home/ubuntu/vss-snapshots

/home/ubuntu/vss-snapshots/
  <tracked-branch-id>/
    revisions/
      <commit-sha>/
```

따라서 저장 계층은 다음 세 영역으로 분리됩니다.

```text
/home/ubuntu/repos/.snapshot-worktrees/
    mutable current Branch working copies

/home/ubuntu/vss-snapshots/
    immutable exact-revision materializations

/home/ubuntu/vss_server/data/
    VSS vector/BM25/briefing/index artifacts
```

## Sync와 Index의 기존 concurrency 계약 유지

새 namespace는 경로만 바꿉니다. 기존 concurrency 보호는 그대로 유지합니다.

- Repository Sync는 없는 Branch workspace만 만들 수 있습니다.
- 기존 Branch workspace는 Sync 중 refresh하지 않습니다.
- Index 직전에만 remote HEAD와 expected Snapshot target SHA를 검증합니다.
- drift가 있으면 `REPOSITORY_BRANCH_HEAD_MISMATCH`를 반환하며 기존 VSS-visible working copy를 변경하지 않습니다.
- dirty working copy는 `REPOSITORY_WORKSPACE_DIRTY`로 거절합니다.
- Git config의 `sol.repository-id`, `sol.branch-ref` ownership metadata를 계속 검증합니다.
- 동일 Tracked Branch의 Index/Retry DB row lock과 active Snapshot guard는 유지합니다.

## filesystem 안전성

새 구조가 nested directory를 사용하기 때문에 parent chain도 안전성 검증 대상입니다.

module은 `.snapshot-worktrees/<repo>/<repository-id>/branches`를 단계별로 생성하며, 이미 존재하는 parent가 symlink 또는 Windows junction이면 작업을 거절합니다.

최종 workspace와 temporary clone staging 모두 `SNAPSHOT_REPOSITORY_ROOT` 경계 안에 있어야 합니다.

Windows 테스트에서는 nested final path 내부에 긴 UUID staging 이름을 만들 경우 Git object path가 `Filename too long`으로 실패하는 현상이 실증되었습니다. 따라서 clone staging은 다음처럼 repository root 바로 아래의 숨은 짧은 임시 경로에서 수행합니다.

```text
/home/ubuntu/repos/.snapshot-worktree-<uuid>.tmp
```

clone과 exact SHA 검증이 끝난 뒤 준비된 directory를 최종 nested workspace로 rename합니다. 이 staging directory 역시 dot-prefixed라 legacy `VSS_REPOS_DIR` scanner에 노출되지 않습니다.

## 기존 AWS `vss_server--*` 디렉터리 처리

기존 AWS에는 이전 계약으로 만든 working copy가 남아 있을 수 있습니다.

```text
/home/ubuntu/repos/vss_server--main
/home/ubuntu/repos/vss_server--module
/home/ubuntu/repos/vss_server--pre-rag
/home/ubuntu/repos/vss_server--test-merge
```

새 코드가 배포되더라도 이 디렉터리를 자동 rename, move, delete하지 않습니다.

이유는 기존 VSS metadata 또는 진행 중인 asynchronous Index가 과거 `project_root`를 참조하고 있을 수 있기 때문입니다.

cutover는 다음처럼 진행합니다.

```text
기존 vss_server--* workspace 유지
        ↓
새 Sync/Index가 .snapshot-worktrees/... workspace 생성
        ↓
새 Index 완료 후 VSS metadata project_root가 새 경로를 가리킴
        ↓
모든 active Branch가 새 namespace를 사용함을 검증
        ↓
별도 유지보수 작업에서 legacy workspace 정리 여부 결정
```

즉 이번 변경은 destructive migration이 아니라 **새 요청부터 새 namespace를 사용하는 gradual cutover**입니다.

## 운영 예시

기존:

```text
/home/ubuntu/repos/vss_server--pre-rag
```

새 구조:

```text
/home/ubuntu/repos/.snapshot-worktrees/
  vss_server/
    851c55f9fe3c4e34a32ecb6c21adc17e/
      branches/
        pre-rag/
```

VSS status에서 기대하는 값:

```text
project_root=/home/ubuntu/repos/.snapshot-worktrees/vss_server/851c55f9fe3c4e34a32ecb6c21adc17e/branches/pre-rag
commit=<tracked branch exact HEAD>
dirty=false
```

## 최종 설계 원칙

이 구조의 목적은 각 identity를 문자열 separator로 억지로 합치지 않는 것입니다.

```text
Repository identity
  <repo-basename>/<repository-id>

Branch identity
  branches/<safe-branch-component>

Revision identity
  vss-snapshots/<tracked-branch-id>/revisions/<commit>

VSS index identity
  VSS project_id / fingerprint / chunker profile
```

이 분리를 통해 module의 Branch worktree 관리가 pre-rag/VSS의 기존 index-selection 문법과 충돌하지 않으며, 동일 basename Repository와 특수문자 Branch도 안전하게 공존할 수 있습니다.

## 변경 범위

이 설계 변경의 코드 소유 범위는 `module/**`입니다.

주요 구현 파일:

```text
module/backend/infrastructure/git/workspace.py
```

관련 검증:

```text
module/tests/unit/infrastructure/test_repository_workspace.py
```

VSS/pre-rag root 구현은 변경하지 않습니다.
