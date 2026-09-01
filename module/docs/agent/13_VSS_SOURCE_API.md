# VSS Snapshot Source 조회 계약

최종 확인일: 2026-08-28 KST

## 목적

VSS가 `project_id`로 Snapshot 모듈을 조회하여 인덱싱·질의 근거가 되는 Repository,
Branch, commit SHA와 코드 정합성 검증값을 가져가는 내부 HTTP 계약입니다. Frontend는 이
API를 호출하지 않습니다.

```text
Frontend ── POST /v1/chat ──> VSS
                               │
                               └─ GET Snapshot Backend /v1/internal/vss/source
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

## VSS의 필수 독립 검증

VSS는 응답을 그대로 신뢰하지 않고 `index_request.project_root`에서 다음 값을 직접
계산합니다.

```text
git rev-parse HEAD                              == verification.expected_commit_sha
git rev-parse HEAD^{tree}                       == verification.expected_tree_sha
git status --porcelain=v1 --untracked-files=all == empty
verification.expected_commit_sha               == target_revision
index_request.project_id                        == project_id
```

하나라도 다르면 `/index`를 시작하거나 해당 소스를 질의 근거로 선택하지 않습니다. 인덱싱
완료 뒤에는 다음 조건까지 확인합니다.

```text
GET /index/status state == done
GET /index/status index.commit == verification.expected_commit_sha
```

`schema_version`의 major가 지원 범위와 다르거나 필수 필드가 없으면 VSS는 fail closed해야
합니다.

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
파일 본문과 내부 예외 원문을 포함하지 않습니다.

## 호출 소유권

- Snapshot Backend는 Repository/Branch/SHA 이력과 immutable 소스를 소유합니다.
- VSS는 `/v1/chat`, source descriptor 소비, Git 독립 검증과 active index 선택을 소유합니다.
- Frontend는 Snapshot Backend의 내부 VSS API를 호출하지 않습니다.
- VSS가 source descriptor를 읽는 것과 Snapshot Backend가 기존 `POST /index` 제출을 수행하는
  것은 중복 Job을 의미하지 않습니다. VSS가 pull 방식으로 Job 시작까지 소유하도록 바꾸려면
  별도 orchestration mode를 합의한 뒤 한쪽 제출만 활성화합니다.
