# VSS Snapshot Source 조회 계약

## 2026-09-04 확정 운영 계약

이 절은 이전 문서의 충돌하는 자동 인덱싱·`vss_pull` 우선 표현보다 우선합니다.

- **VSS가 유일한 Indexer입니다.** Snapshot Module은 파일 수집 정책, chunking, embedding, BM25, vector/vector-store build·promote를 구현하거나 복제하지 않습니다. 실제 인덱싱은 `vss_server`의 `POST /index -> indexer.start_index()` 경로만 사용합니다.
- Repository 등록/동기화는 **인덱싱과 분리**합니다. 수집한 Repository는 `SNAPSHOT_REPOSITORY_ROOT=/home/ubuntu/repos` 아래 관리하고, sync는 clone/fetch·ref 관측·commit catalog 갱신까지만 수행하며 VSS `POST /index`를 자동 호출하지 않습니다.
- VSS에 넘길 입력은 mutable working copy가 아니라 `SNAPSHOT_MATERIALIZATION_ROOT=/home/ubuntu/vss-snapshots` 아래의 **검증된 immutable exact Snapshot**입니다. `VSS_REPOS_DIR=/home/ubuntu/repos`는 VSS의 repository 발견/표시 용도로 사용할 수 있지만 Module의 정식 `/index` 입력 경로는 아닙니다.
- 인덱싱 시작은 **Admin의 명시적 Index 요청**이 소유합니다. 목표 Admin API는 `POST /v1/admin/snapshots/{snapshot_id}/index`이며, materialized Snapshot만 대상으로 `project_root`, `project_id`, `force=false`, `briefing`, `note`를 VSS `POST /index`에 전달합니다. VSS의 `remote` clone 기능은 Module 연동 경로에서 사용하지 않습니다.
- Module은 VSS의 `GET /index/status`와 `GET /index/exists`를 관측하고, `state=done`뿐 아니라 `index.commit == snapshot.target_revision`까지 확인한 경우에만 Snapshot을 `completed`로 수렴시킵니다.
- 현재 운영 오케스트레이션 방향은 **`module_push`**이지만 의미는 “sync 시 자동 push”가 아니라 **Admin 요청으로 생성된 IndexCommand를 Module이 VSS에 제출**한다는 뜻입니다. `vss_pull`과 `/v1/internal/vss/*`는 provenance/read-model 및 향후 선택 기능으로 유지하며 현재 pre-rag VSS의 필수 data plane으로 간주하지 않습니다.
- Commit History/Compare는 Admin 분석 기능으로 유지합니다. **비교 결과로 reference commit SHA를 자동 선택하거나 VSS에 전달하는 기능, multi-revision 답변 context는 구현 보류**입니다.


최종 확인일: 2026-09-04 KST

## 목적

VSS 또는 운영 검증자가 `project_id`로 Snapshot 모듈의 provenance/read-model을 조회할 수 있는
내부 HTTP 계약입니다. 현재 pre-rag의 인덱싱 시작에는 이 pull이 필수되지 않으며 Admin explicit
Index 경로가 VSS `POST /index`를 직접 호출합니다. Frontend는 이
API를 호출하지 않습니다.

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

## 오케스트레이션 모드와 기능 안내

현재 pre-rag 운영 계약은 `module_push`입니다. 단, `module_push`는 Repository sync/Overlay가
자동으로 인덱싱한다는 뜻이 아닙니다. **Admin의 명시적 Index 요청만** IndexCommand를 만들고
Module이 VSS `POST /index`를 호출합니다. Repository sync와 Snapshot materialization은 여기서
분리되어 VSS side effect를 만들지 않습니다.

- `module_push` (현재 운영): Admin `POST /v1/admin/snapshots/{snapshot_id}/index` -> Module -> VSS `POST /index`.
- `vss_pull` (향후 선택 capability): `/v1/internal/vss/*` read model은 유지하지만 현재 pre-rag VSS의 필수 caller/data plane으로 간주하지 않습니다.

Module -> VSS `/index` 요청은 `project_root`만 사용하며 VSS의 `remote` clone 기능을 사용하지
않습니다. 실제 파일 수집, chunking, embedding, BM25, vector store build/promote, briefing은
VSS `server.py`/`indexer.py`의 책임입니다.

```http
GET /v1/internal/vss/capabilities
X-Snapshot-Token: <shared-secret>
```

응답 예시:
```json
{
  "ok": true,
  "schema_version": "1.0",
  "orchestration_mode": "module_push",
  "index_start_owner": "module",
  "supported_apis": ["source", "revisions", "change-requests", "refs", "context"],
  "request_id": "..."
}
```

## Admin explicit Index 계약

```http
POST /v1/admin/snapshots/{snapshot_id}/index
```

요청자는 operator 이상이어야 하며 Snapshot은 이미 `materialized` 상태여야 합니다. Browser는
`project_root`, `remote`, credential을 보내지 않습니다. Backend가 Snapshot DB와 locator를
검증한 뒤 VSS에 다음 body를 생성합니다.

```json
{
  "project_root": "/home/ubuntu/vss-snapshots/.../revisions/<sha>",
  "project_id": "<exact-vss-index-id>",
  "force": false,
  "briefing": true,
  "note": "snapshot <target-sha>"
}
```

VSS `POST /index`가 `202 accepted=true`를 반환해도 완료가 아닙니다. Reconciler가
`GET /index/status`를 조회하여 `done`과 `index.commit == target_revision`을 함께 확인해야
`completed`입니다.

## Branch/Tag/Change-Request Refs 조회

VSS가 프로젝트에 속한 브랜치, 태그, PR/MR의 최신 ref와 커밋 SHA 목록을 일괄 조회합니다.

```http
GET /v1/internal/vss/refs?project_id=<exact-id>
X-Snapshot-Token: <shared-secret>
```

응답의 `items[]`에는 `ref_type` (branch | tag | change_request), `ref_name`, `commit_sha`, `snapshot_id`, `eligible_for_answer` 등이 포함됩니다.

## 결정론적 Revision Context 조회

VSS가 특정 커밋, 브랜치 ref, 또는 PR/MR에 대한 exact Snapshot 및 VSS 완료 상태를 결정론적으로 조회합니다.

```http
GET /v1/internal/vss/context?project_id=<id>&revision=<sha>
GET /v1/internal/vss/context?project_id=<id>&branch_ref=<refs/heads/...>
GET /v1/internal/vss/context?project_id=<id>&change_request=<github|gitlab:number>
X-Snapshot-Token: <shared-secret>
```

응답에는 `context_kind`, `target_revision`, `expected_tree_sha`, `snapshot_state`, `vss_state`, `eligible_for_answer`, `unavailable_reason`이 포함됩니다.

## Phase 7 질의 참고 자료 확장

VSS는 `/v1/chat`과 질의 해석을 소유합니다. `/v1/internal/vss/*`는 provenance/read-model을
위한 optional/future pull capability이며 현재 pre-rag 인덱싱 시작에 필수인 호출은 아닙니다. 기존 source/revisions API는 exact materialized source의
정본으로 유지하고, Phase 7에서 다음 관계를 별도 내부 조회로 확장합니다.

```text
Repository -> Branch/Tag -> commit
Repository -> GitHub PR/GitLab MR -> base/head/merge commit
commit -> Snapshot -> expected tree SHA -> VSS index.commit
```

module은 자연어 질의를 처리하지 않습니다. VSS가 선택한 exact selector를 Git 관계와
Snapshot 상태에 연결하고, 인덱싱이 완료되지 않았거나 commit이 일치하지 않으면
`eligible_for_answer=false`와 안전한 unavailable reason을 반환합니다.
현재 `capabilities`, `change-requests`, `refs`, `context` pull API가 모두 구현 완료되었습니다.
전체 완료 조건은 `15_REVISION_CONTEXT_PROVIDER.md`를 따릅니다.

```http
GET /v1/internal/vss/change-requests?project_id=<id>&state=<optional>&limit=100
GET /v1/internal/vss/change-requests/{github|gitlab}/{number}?project_id=<id>
```

각 current base/head/merge revision은 `snapshot_id`, `snapshot_state`, `vss_state`,
`eligible_for_answer`, `unavailable_reason`과 함께 반환됩니다. 상세 응답은 force-push와 head
변경을 재현할 수 있도록 append-only `observations`를 포함합니다.

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
- Branch/Tag/PR/MR context의 localhost pull과 답변 provenance 소비는 향후 선택 기능입니다.
- Frontend는 Snapshot Backend의 내부 VSS API를 호출하지 않습니다.
- 현재 운영 `module_push`에서는 Admin의 명시적 Index 요청만 VSS `POST /index`를 호출합니다. Repository sync, commit compare, materialize 목록 조회는 VSS Job을 자동 생성하지 않습니다.

Repository commit graph, 과거/current 비교와 `Git only` commit의 Snapshot 승격 정책은
`16_COMMIT_HISTORY_AND_COMPARISON.md`를 따릅니다. commit catalog만 존재하는 revision을
VSS source 또는 answer-eligible index로 가장하지 않습니다.
