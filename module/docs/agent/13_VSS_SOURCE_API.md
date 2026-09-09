# VSS Snapshot Source 조회 계약

## 2026-09-04 확정 운영 계약

이 절은 이전 문서의 충돌하는 자동 인덱싱·`vss_pull` 우선 표현보다 우선합니다.

- **VSS is the sole Indexer.** Snapshot Module does not implement chunking, embedding, BM25, vector reuse, or build/promote. Module submits one unified `POST /index`; VSS decides full versus incremental internally from its active fingerprint and file-hash manifest.
- Repository 등록/동기화는 **인덱싱과 분리**합니다. Tracked Branch마다 `SNAPSHOT_REPOSITORY_ROOT=/home/ubuntu/repos` 아래 `.snapshot-worktrees/<repo-basename>/<repository-id>/branches/<safe-branch-component>` working copy를 둡니다. Sync는 없는 working copy만 준비하고 기존 working copy는 refresh하지 않으며, ref/HEAD 관측·object cache·commit catalog·Snapshot readiness만 갱신하고 VSS `POST /index`를 자동 호출하지 않습니다.
- VSS 요청 전에 `SNAPSHOT_MATERIALIZATION_ROOT=/home/ubuntu/vss-snapshots`의 immutable exact Snapshot을 항상 검증 증거로 사용합니다. 그 Snapshot이 현재 활성 Tracked Branch HEAD와 정확히 같으면 Index 직전에 해당 `/home/ubuntu/repos/.snapshot-worktrees/<repo-basename>/<repository-id>/branches/<safe-branch-component>` working copy를 target SHA로 refresh·검증하여 VSS `/index.project_root`로 전달합니다. 과거 commit·비활성 Branch 등 current tracked HEAD가 아닌 Snapshot은 immutable materialized tree를 `project_root`로 사용합니다.
- Index start is owned by the **explicit Admin Index request**. `POST /v1/admin/snapshots/{snapshot_id}/index` verifies the exact Snapshot/project_root and submits `POST /index` with `force=false`. VSS owns the full/incremental decision. Module does not use VSS remote-clone on this path.
- Module은 VSS의 `GET /index/status`와 `GET /index/exists`를 관측하고, `state=done`뿐 아니라 `index.commit == snapshot.target_revision`까지 확인한 경우에만 Snapshot을 `completed`로 수렴시킵니다.
- 현재 운영 오케스트레이션 방향은 **`module_push`**이지만 의미는 “sync 시 자동 push”가 아니라 **Admin 요청으로 생성된 IndexCommand를 Module이 VSS에 제출**한다는 뜻입니다. `vss_pull`과 `/v1/internal/vss/*`는 provenance/read-model 및 향후 선택 기능으로 유지하며 현재 pre-rag VSS의 필수 data plane으로 간주하지 않습니다.
- Commit History/Compare는 Admin 분석 기능으로 유지합니다. **비교 결과로 reference commit SHA를 자동 선택하거나 VSS에 전달하는 기능, multi-revision 답변 context는 구현 보류**입니다.


Final contract review: 2026-09-09 KST

## 목적

VSS 또는 운영 검증자가 `project_id`로 Snapshot 모듈의 provenance/read-model을 조회할 수 있는
내부 HTTP 계약입니다. 현재 pre-rag의 인덱싱 시작에는 이 pull이 필수되지 않으며 Admin explicit
Index submission uses the unified VSS `POST /index` contract. Module verifies exact source identity; VSS performs its own full/incremental eligibility check. Frontend does not call this internal API.


```text
Frontend ── POST /v1/chat ──> VSS

Optional/Future provenance consumer
VSS/Operator ── GET Snapshot Backend /v1/internal/vss/source
                                      ├─ Repository / Branch
                                      ├─ expected commit SHA
                                      ├─ expected Git tree SHA
                                      └─ VSS /index 입력값
```

VSS와 Snapshot Backend가 같은 AWS Ubuntu network namespace에서 실행되는 현재 배치에서는
VSS가 `http://127.0.0.1:8000`으로 호출합니다. reverse proxy에는 `/v1/internal/*`를
노출하지 않습니다.

## 인증

Snapshot Backend와 VSS에 별도의 동일한 내부 token을 배포합니다.

```text
Snapshot Backend  SNAPSHOT_VSS_API_TOKEN=<shared-secret>
VSS caller        X-Snapshot-Token: <shared-secret>
```

`Authorization: Bearer <shared-secret>`도 허용합니다. Backend가 VSS를 호출할 때 사용하는
`VSS_TOKEN`과 VSS가 Backend를 호출할 때 사용하는 `SNAPSHOT_VSS_API_TOKEN`은 권한 방향이
다르므로 재사용하지 않습니다. token 미설정은 `503 VSS_SOURCE_API_NOT_CONFIGURED`, 누락·
불일치는 `401 VSS_SOURCE_AUTH_REQUIRED`입니다.

## 최신 또는 특정 Snapshot 소스 조회

```http
GET /v1/internal/vss/source?project_id=vss-server--module
X-Snapshot-Token: <shared-secret>
```

특정 SHA가 필요하면 실제 40자리 Git commit SHA를 지정합니다.

```http
GET /v1/internal/vss/source?project_id=vss-server--module&revision=<40-char-sha>
```

`revision`을 생략하면 해당 exact `vss_project_id`의 가장 최근 materialized Snapshot을
반환합니다. alias, prefix, repository 이름 유사 매칭은 하지 않습니다.

### 성공 응답

```json
{
  "ok": true,
  "schema_version": "1.0",
  "reason": "VSS_SOURCE_READY",
  "detail": "VSS가 독립 검증 후 인덱싱할 수 있는 Snapshot 소스입니다.",
  "retryable": false,
  "request_id": "11111111-1111-4111-8111-111111111111",
  "project_id": "vss-server--module",
  "repository_id": "22222222-2222-4222-8222-222222222222",
  "repository_name": "h5vision/vss_server",
  "branch_ref": "refs/heads/module",
  "snapshot_id": "33333333-3333-4333-8333-333333333333",
  "snapshot_state": "materialized",
  "source_type": "remote_clone",
  "base_revision": "1111111111111111111111111111111111111111",
  "target_revision": "2222222222222222222222222222222222222222",
  "verification": {
    "expected_commit_sha": "2222222222222222222222222222222222222222",
    "expected_tree_sha": "3333333333333333333333333333333333333333",
    "object_format": "sha1",
    "git_metadata_present": true,
    "working_tree_clean": true,
    "verified_at": "2026-08-28T12:00:00Z",
    "verification_commands": [
      "git rev-parse HEAD",
      "git rev-parse HEAD^{tree}",
      "git status --porcelain=v1 --untracked-files=all"
    ]
  },
  "index_request": {
    "project_root": "/srv/vss-snapshots/<binding>/revisions/<target-sha>",
    "project_id": "vss-server--module",
    "profile": null,
    "force": false,
    "briefing": true,
    "note": "snapshot 2222222222222222222222222222222222222222"
  }
}
```

`project_root`는 외부 사용자용 값이 아니라 동일 서버의 VSS가 읽기 위해 인증된 내부
응답에만 포함합니다. `expected_tree_sha`는 파일 내용뿐 아니라 Git이 추적하는 경로와 mode를
포함한 tree object 정합성 증거입니다. untracked 파일은 tree SHA에 포함되지 않으므로 clean
working tree 조건을 별도로 요구합니다.

## Module 사전 검증과 VSS 완료 증거

현재 `pre-rag/vss/server.py`와 `vss/indexer.py`는 `/index` 입력의 `project_root`를 받아
VSS 자체 pipeline을 실행하고, 인덱싱 메타데이터에 `git rev-parse HEAD` 결과(`commit`)와
working tree `dirty` 상태를 기록합니다. 현재 pre-rag가 `expected_tree_sha`를 입력으로 받아
독립 비교하는 계약은 구현되어 있지 않습니다. 따라서 정합성 책임을 다음처럼 나눕니다.

**Module은 VSS 제출 전에** immutable Snapshot에서 다음을 검증합니다.

```text
git rev-parse HEAD                              == target_revision
git rev-parse HEAD^{tree}                       == verification.expected_tree_sha
git status --porcelain=v1 --untracked-files=all == empty
```

**VSS는 자신의 기존 `/index` pipeline으로 인덱싱**하고 `commit`/`dirty`를 active index 메타데이터에
기록합니다. Module Reconciler는 완료 뒤 다음을 확인합니다.

```text
GET /index/status state == done
GET /index/status index.commit == target_revision
GET /index/status dirty == false  # 제공되는 경우 추가 증거
```

`expected_tree_sha`는 Module provenance/사전 검증 증거로 유지하며, VSS에 존재하지 않는
독립 tree-SHA 검증 기능을 현재 계약인 것처럼 요구하지 않습니다.

## SHA 이력 조회

```http
GET /v1/internal/vss/revisions?project_id=vss-server--module&limit=100
X-Snapshot-Token: <shared-secret>
```

응답의 `items[]`에는 다음 값이 포함됩니다.

```text
snapshot_id
repository_id
branch_ref
base_revision
target_revision
snapshot_state
materialized
vss_state
created_at / updated_at
```

현재 구현의 이력은 이미 생성된 Snapshot 이력입니다. 원격 Repository의 추적 Branch HEAD
관측 이력은 Phase 3A-2 수집 코어의 `tracked_branches`, `branch_head_history`,
`repository_sync_runs`에 별도로 저장하며, 이 VSS 내부 API는 materialized Snapshot 이력만
반환합니다. Branch 관측 이력 조회는 Phase 3A-3 Admin API에서 제공합니다.

## Repository catalog와 Commit graph 조회

pre-rag/VSS가 Module이 관리하는 Repository/Branch/commit tree를 직접 filesystem scan으로
추론하지 않도록 별도 read-only catalog를 제공합니다. `.snapshot-worktrees`와
`.repository-cache`는 Module 내부 namespace로 유지하며 VSS가 직접 순회하지 않습니다.

```http
GET /v1/internal/vss/repositories
X-Snapshot-Token: <shared-secret>
```

각 Repository 항목은 `repository_id`, `repository_name`, `display_name`, `provider`,
`default_branch_ref`와 tracked Branch 목록을 반환합니다. Branch에는 `tracked_branch_id`,
`branch_ref`, exact `project_id`, `current_head_sha`, `is_default`, `observed_at`이 포함됩니다.
VSS는 `project_id` 문자열을 다시 `--` 규칙으로 parsing하지 않고 이 명시적 mapping을 우선
사용할 수 있습니다.

```http
GET /v1/internal/vss/repositories/<repository-id>/commit-graph?limit=100
GET /v1/internal/vss/repositories/<repository-id>/commit-graph?limit=100&cursor=<40-char-sha>
```

응답의 `items[]`는 Module `RepositoryCommit`/`RepositoryCommitParent` catalog를 그대로
read model로 변환한 `commit_sha`, `tree_sha`, ordered `parent_shas`, author/time/subject를
포함합니다. `branches[]`의 HEAD와 commit parent edge를 조합하면 pre-rag가 branch별 commit
tree를 그릴 수 있습니다. `catalog_state`, `history_complete`, `truncated`, `shallow`는 최근
commit catalog run의 완전성 증거이며, `next_cursor`가 있으면 같은 Repository의 다음 page를
조회합니다.

이 catalog는 Git history metadata만 전달합니다. 실제 인덱싱 source는 기존대로 Module이
검증한 clean Git checkout/worktree를 VSS `/index.project_root`로 넘깁니다. 즉 계약을 분리합니다.

```text
source files / exact checkout  -> project_root
repository / branch / parents  -> /internal/vss/repositories + /commit-graph
```

## Incremental indexing contract: VSS-managed behind unified POST /index

The frozen pre-rag contract keeps `module_push` as the orchestration direction, but **not** as a delta-push protocol.
Module owns Repository/Branch/commit truth and prepares a verified exact target `project_root`. It then calls only:

```http
POST /index
X-VSS-Token: <vss-token>
Content-Type: application/json
```

```json
{
  "project_root": "/home/ubuntu/repos/.snapshot-worktrees/.../branches/module",
  "project_id": "<exact-vss-index-id>",
  "force": false,
  "briefing": true,
  "note": "branch <target-sha>"
}
```

`profile` remains an optional `/index` field. Module currently omits it on the default Snapshot path, so VSS resolves its
configured profile and compares the resulting fingerprint with the active index.

Module does **not** send `branch_ref`, `base_revision`, `target_revision`, tree SHAs, or `changes[]` to VSS.
There is no `POST /index/incremental` call in the current data plane. VSS collects target files, hashes their contents,
compares them with its promoted manifest, checks the active fingerprint, and chooses `full` or `incremental` itself.
`force=true` always requests a full rebuild; Module's normal Snapshot path keeps `force=false`.

`briefing=true` follows the frozen VSS auto policy: full indexing generates briefing, while incremental indexing keeps the
previous briefing. `briefing="always"` is supported by the VSS HTTP contract for callers that explicitly need regeneration.

The active result is observed through `GET /index/status`. VSS can report:

```json
{
  "mode": "incremental",
  "index": {
    "commit": "<target-sha>",
    "mode": "incremental",
    "incremental": {
      "changed_files": 2,
      "deleted_files": 1,
      "unchanged_files": 40,
      "reused_chunks": 350,
      "rebuilt_chunks": 18
    }
  }
}
```

A content-empty commit is handled naturally by VSS: `rebuilt_chunks` can be zero while VSS still promotes a new active
revision whose `index.commit` is the new Git HEAD. Module completion remains strict: `state == "done"` **and**
`index.commit == snapshot.target_revision`.

`GET /v1/internal/vss/delta` remains an optional provenance/debug/future-pull API. Its Git compare data is not consumed by
pre-rag indexing and must not be treated as the VSS indexing data plane.

`project_id` remains an exact persisted identifier at the Module boundary. Existing IDs are not renamed automatically,
because renaming would create a distinct VSS project and detach the existing active index. New registrations may follow the
VSS naming convention `<repo>@<branch>--<chunker>` when the operator chooses it.
## Orchestration mode and capability guidance

Current production direction is `module_push`: an explicit Admin Index command makes Module submit one unified VSS
`POST /index`. Repository sync and Snapshot materialization remain side-effect free with respect to VSS indexing.

- `module_push` (current): Admin Index -> exact target source verification -> `POST /index` -> VSS chooses full/incremental.
- `vss_pull` (optional/future): `/v1/internal/vss/*` read models remain available for provenance and future consumers, but are not required by the frozen pre-rag indexing data plane.

Module -> VSS uses a verified local `project_root`; it does not use VSS `remote` clone. Chunking, embedding, BM25,
vector reuse, manifest comparison, build/promote, and briefing policy are VSS responsibilities.

```http
GET /v1/internal/vss/capabilities
X-Snapshot-Token: <shared-secret>
```

?? ??:
```json
{
  "ok": true,
  "schema_version": "1.0",
  "orchestration_mode": "module_push",
  "index_start_owner": "module",
  "resources": ["source", "revisions", "refs", "context", "repositories", "commit_graph", "delta"],
  "context_selectors": ["revision", "branch"],
  "request_id": "..."
}
```

## Admin explicit Index ??

```http
POST /v1/admin/snapshots/{snapshot_id}/index
```

Caller must be operator-or-higher and the Snapshot must already be `materialized`. Browser clients do not supply
`project_root`, remote credentials, Git delta, or VSS incremental fields. Backend verifies Snapshot DB/locator and VSS
running/idempotency state, resolves the exact target source, then submits the unified VSS `/index` body below.

```json
{
  "project_root": "/home/ubuntu/vss-snapshots/.../revisions/<sha>",
  "project_id": "<exact-vss-index-id>",
  "force": false,
  "briefing": true,
  "note": "snapshot <target-sha>"
}
```

`POST /index` returning `202 accepted=true` means accepted, not completed. Reconciler? `GET /index/status`??
`done`? `index.commit == target_revision`? ?? ???? `completed`? ?????.

## Branch Ref ??

```http
GET /v1/internal/vss/refs?project_id=<exact-id>
X-Snapshot-Token: <shared-secret>
```

?? `refs`? tracked Branch? exact current revision? Snapshot readiness? ?????. Tag/PR/MR catalog?
2026-09-09 ????? ?? VSS runtime/indexing contract? ???? ????.

## ???? Revision Context ??

`revision` ?? `branch_ref` ? ??? ??? ?????.

```http
GET /v1/internal/vss/context?project_id=<id>&revision=<sha>
GET /v1/internal/vss/context?project_id=<id>&branch_ref=<refs/heads/...>
X-Snapshot-Token: <shared-secret>
```

## 2026-09-09 Phase 7A optional catalog ??

?? ???? PR/MR? Repository Tag ?? ??? ?? ???, provider token? ???, ? ?? DB table?
?? 0 rows?? ?? VSS ????? ?? ??? ??? ??????. ??? PR/MR catalog/provider, Tag
current/history, ?? `/change-requests` API? tag/change-request context selector? ??????.
`0006_change_request_context`? `0008_repository_tags`? ?? ?? migration ???? ????
`0010_remove_unused_phase7a`?? ? table? guarded drop???.

## VSS inbound non-success ??

?? `/v1/internal/vss/*` ??? ?? status? 200/202? ??? ?? ???? ????. ???? ?? ??
VSS route? 404, ?? ?? 401/403, validation 422, server error 5xx ?? ?????.

- journal warning: method/path/status/elapsed/request_id
- `snapshot.audit_logs`: `actor=vss-inbound`, `action=vss_inbound_request`, `reason=HTTP_<status>`
- query? token/secret/password/authorization/credential/api_key ?? ?? `<redacted>`
- Admin ?? ??: `GET /v1/admin/vss/request-failures`
- Admin Web: `VSS request failures`

?? 200/202? ? ?? ??? ???? ????.

## 호출 실패 의미

| HTTP | reason | 의미 | 재시도 |
|---:|---|---|---:|
| `401` | `VSS_SOURCE_AUTH_REQUIRED` | 내부 token 누락·불일치 | X |
| `404` | `VSS_SOURCE_NOT_FOUND` | exact project/revision Snapshot 없음 | X |
| `409` | `VSS_SOURCE_REPOSITORY_INACTIVE` | Repository 비활성·누락 | X |
| `409` | `SNAPSHOT_REVISION_MISMATCH` | HEAD/tree/working tree 불일치 | X |
| `500` | `SNAPSHOT_MATERIALIZATION_FAILED` | Git 검증 실행 실패 | O |
| `503` | `VSS_SOURCE_API_NOT_CONFIGURED` | inbound token 미설정 | X |
| `503` | `DATABASE_UNAVAILABLE` | Snapshot DB 접근 실패 | O |

모든 실패는 `reason`, `detail`, `retryable`, `request_id`를 반환하며 token, DSN, Git stderr,
파일 본문과 내부 예외 원문을 포함하지 않습니다. token 누락 또는 Backend token 미설정
응답에는 token 값 대신 다음 필드를 추가합니다.

```text
warning
token_environment_variable = SNAPSHOT_VSS_API_TOKEN
token_config_path = /etc/vss-snapshot/module.env (기본값)
```

설정 경로는 `SNAPSHOT_VSS_API_TOKEN_CONFIG_PATH`로 변경할 수 있습니다. 이 경로는 loopback
VSS 운영자를 위한 제한된 예외이며, materialized source·credential·DSN 경로는 여전히
노출하지 않습니다. 잘못된 token 값에는 설정 경로 안내를 반환하지 않습니다.

## 호출 소유권

- Snapshot Backend는 Repository/Branch/SHA 이력과 immutable 소스를 소유합니다.
- VSS는 `/v1/chat`, 실제 index pipeline(`collect/chunk/embed/BM25/store promote`)과 active index를 소유합니다.
- Module은 immutable Snapshot의 HEAD/tree/clean 조건을 VSS 호출 전에 검증합니다. 현재 pre-rag VSS는
  인덱싱 결과의 `commit`/`dirty`를 기록하며 Module은 완료 후 exact commit을 다시 대조합니다.
 - `/internal/vss/refs`? `/context`? ?? revision/branch provenance selector? ?????.
- Frontend는 Snapshot Backend의 내부 VSS API를 호출하지 않습니다.
- 현재 운영 `module_push`에서는 Admin의 명시적 Index 요청만 VSS `POST /index`를 호출합니다. Repository sync, commit compare, materialize 목록 조회는 VSS Job을 자동 생성하지 않습니다.

Repository commit graph, 과거/current 비교와 `Git only` commit의 Snapshot 승격 정책은
`16_COMMIT_HISTORY_AND_COMPARISON.md`를 따릅니다. commit catalog만 존재하는 revision을
VSS source 또는 answer-eligible index로 가장하지 않습니다.
