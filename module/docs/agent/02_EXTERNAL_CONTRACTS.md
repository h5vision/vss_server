# 외부·모듈 통신 계약

## 기본 원칙

- Frontend HTTP request는 현재 TypeScript 계약 그대로 받습니다.
- Backend가 받은 delta를 영속화하고 완전한 revision 디렉터리로 materialize합니다.
- VSS에는 파일 delta JSON을 보내지 않고 Python 모듈로 디렉터리 경로를 전달합니다.
- 상대 규약에 없는 값을 Frontend 필수 입력으로 만들지 않습니다.
- 성공·접수·거부·실패는 HTTP status와 구조화된 `reason`, `detail`, `retryable`로
  구분합니다.

## Frontend → Backend

```http
POST /v1/workspace-overlays
Content-Type: application/json
```

Frontend 기본값이 `http://192.168.0.7/v1`이면 실제 주소는 다음과 같습니다.

```text
http://192.168.0.7/v1/workspace-overlays
```

### Request

```json
{
  "project_id": "h5vision/vision",
  "base_revision": "1111111111111111111111111111111111111111",
  "target_revision": "2222222222222222222222222222222222222222",
  "files": [
    {
      "status": "modified",
      "path": "vision/src/services/gitService.ts",
      "content": "변경 후 파일 전체 문자열",
      "encoding": "utf-8"
    }
  ],
  "deleted_paths": ["vision/src/obsolete.ts"],
  "renames": [
    {"old_path": "vision/src/old.ts", "new_path": "vision/src/new.ts"}
  ]
}
```

| 필드 | 필수 | 의미 |
|---|---:|---|
| `project_id` | O | Frontend 프로젝트 힌트, Admin binding 조회 키 |
| `base_revision` | O | 이전 실제 40자리 Git commit SHA |
| `target_revision` | O | 새 실제 40자리 Git commit SHA |
| `files` | O | 추가·수정 파일의 변경 후 전체 문자열 |
| `deleted_paths` | O | 삭제할 프로젝트 상대 POSIX 경로 |
| `renames` | O | 이전·새 상대경로 쌍 |
| `snapshot_id` | X | Backend 내부에서 생성 |
| `branch` | X | 활성 Admin binding으로 확정 |
| `content_sha256`, `size_bytes` | X | Frontend에 요구하지 않음 |

세 변경 배열이 모두 비어 있어도 실제 empty commit일 수 있으므로 유효합니다. 로컬에만
있는 실제 commit SHA도 입력 검증 단계에서는 허용합니다. 다만 현재 VSS가 revision을
직접 받지 않으므로 실제 인덱싱 가능 여부는 materialization/revision gate에서 별도로
판정합니다.

### 경로 규칙

- `/` 구분자를 사용한 정규화된 상대 POSIX 경로만 허용합니다.
- 절대경로, drive prefix, 역슬래시, NUL, 빈 segment, `.`과 `..`를 거부합니다.
- rename destination은 `files[]`에 최종 content가 있어야 합니다.
- 같은 경로의 수정·삭제 중복을 거부합니다.
- 파일 content를 diff patch로 해석하거나 변환하지 않습니다.

## Backend 내부 처리 계약

```text
schema·업무 검증
→ 활성 Repository/Branch/VSS binding 확정
→ Snapshot·delta DB commit
→ base revision 전체 트리 확보
→ staging 디렉터리에 delta 적용
→ target revision 정합성 확인
→ immutable revision 디렉터리 promote
→ VSS module 비동기 제출
→ module result와 Job 상태 저장
→ Frontend 구조화 응답
```

DB 최초 commit 전에 VSS를 호출하지 않습니다. materialization은 설정된 전용 root 아래
staging에서 수행하고 성공 후 원자적으로 revision 경로에 승격합니다. Snapshot ID는
내부 UUID이며 Git SHA와 다릅니다.

## Backend → VSS Python 모듈

HTTP `/index/update/files`는 사용하지 않습니다. 기준 SHA의 실제 호출은 다음입니다.

```python
from vss.indexer import start_index

result = start_index(
    project_root="/srv/snapshots/vss-server--module/<target-sha>",
    project_id="vss-server--module",
    profile={"context_header": True, "use_bm25": True},
    blocking=False,
    force=False,
    extra_meta={
        "snapshot_id": "<backend-internal-id>",
        "requested_revision": "<target-sha>"
    },
)
```

`extra_meta.requested_revision`은 추적 정보일 뿐 VSS 완료 commit을 강제로 지정하지
않습니다. 현 VSS는 최종 commit을 `git_head(project_root)`에서 다시 읽습니다.

### 시작 반환값

접수:

```json
{
  "accepted": true,
  "project_id": "vss-server--module",
  "state": "running",
  "fingerprint": {
    "embed_model": "bge-m3:latest",
    "chunker": "ast-v1",
    "use_bm25": true
  }
}
```

동일 project 진행 중:

```json
{
  "accepted": false,
  "reason": "already_running",
  "project_id": "vss-server--module",
  "heartbeat_age_s": 1.4
}
```

잘못된 디렉터리:

```json
{
  "accepted": false,
  "reason": "not_a_directory",
  "path": "/resolved/path"
}
```

VSS 모듈 반환 필드는 attempt의 `module_result_json`에 보존합니다. 파일 본문, DSN,
token은 저장하지 않습니다.

### 상태 조회

```python
status = vss.indexer.status(vss_project_id)
```

```json
{
  "project_id": "vss-server--module",
  "state": "done",
  "processed": 14,
  "total": 14,
  "chunk_count": 83,
  "error": null,
  "index": {
    "chunks": 83,
    "commit": "2222222222222222222222222222222222222222",
    "fingerprint": {},
    "indexed_at": "2026-08-27T02:00:00+00:00",
    "project_root": "/srv/snapshots/vss-server--module/2222...",
    "bm25_count": 83
  },
  "incomplete": []
}
```

완료 판정은 두 조건을 모두 만족해야 합니다.

```text
state == "done"
index.commit == Snapshot target_revision
```

`done`인데 commit이 없거나 다르면 `revision_mismatch` 실패입니다. `failed`와
`aborted`는 `error`와 `incomplete` 정보를 Snapshot에 보존합니다.

## VSS 모듈 설치·import 계약

- Backend 프로세스가 `vss.indexer`를 import할 수 있어야 합니다.
- exact source revision을 배포 manifest 또는 lock에 고정합니다.
- `VSS_*` 환경변수를 설정한 뒤 모듈을 lazy import합니다.
- `start_index`, `status`, `exists`, `list_projects` callable과 `start_index` signature를
  시작 전 검사합니다.
- `module/pyproject.toml`은 Snapshot Backend의 `backend*`만 설치합니다.
- `vss_server/main`의 exact SHA를 별도 package로 공급하고 깨끗한 환경에서 두 package의
  import가 모두 통과하기 전에는 production-ready로 판정하지 않습니다.
- VSS `JOBS`와 Store가 프로세스 전역이므로 초기 Backend/VSS indexing worker는 하나만
  실행합니다.

## Frontend 응답 계약

모든 응답은 `X-Request-ID`와 JSON body를 가집니다. `reason`은 프로그램 분기용 안정
코드, `detail`은 사람이 성공·실패·반환 이유를 알 수 있는 설명입니다.

| HTTP | 상황 | `reason` | `retryable` |
|---:|---|---|---:|
| `200` | target revision이 이미 활성 index | `TARGET_ALREADY_INDEXED` | `false` |
| `202` | Snapshot 저장·materialization·VSS 제출 접수 | `VSS_INDEX_ACCEPTED` | `false` |
| `409` | 활성 binding 없음 | `SNAPSHOT_DESTINATION_REQUIRED` | `false` |
| `409` | 활성 binding 중복 | `SNAPSHOT_DESTINATION_AMBIGUOUS` | `false` |
| `409` | 동일 VSS project 작업 중 | `VSS_INDEX_ALREADY_RUNNING` | `true` |
| `409` | base tree 또는 revision 정합성 없음 | `SNAPSHOT_BASE_REVISION_UNAVAILABLE` | 상황별 |
| `409` | 현 VSS로 target revision 보존 불가 | `VSS_REVISION_CONTRACT_UNSUPPORTED` | `false` |
| `422` | Frontend schema 검증 실패 | `REQUEST_VALIDATION_FAILED` | `false` |
| `500` | Snapshot 최초 DB 저장 실패 | `SNAPSHOT_PERSIST_FAILED` | `true` |
| `500` | materialization 내부 실패 | `SNAPSHOT_MATERIALIZATION_FAILED` | `true` |
| `500` | VSS 결과 DB 기록 실패 | `SNAPSHOT_RESULT_PERSIST_FAILED` | `true` |
| `503` | VSS package/import 불가 | `VSS_MODULE_UNAVAILABLE` | `false` |
| `503` | VSS public API 불일치 | `VSS_MODULE_CONTRACT_MISMATCH` | `false` |
| `503` | VSS Store/Ollama 의존성 실패 | `VSS_DEPENDENCY_UNAVAILABLE` | `true` |
| `500` | VSS 함수가 예외 발생 | `VSS_MODULE_CALL_FAILED` | `true` |

접수 예시:

```json
{
  "ok": true,
  "reason": "VSS_INDEX_ACCEPTED",
  "detail": "전체 프로젝트 디렉터리 인덱싱을 접수했습니다. 완료 상태 확인이 필요합니다.",
  "retryable": false,
  "snapshot_id": "22222222-2222-4222-8222-222222222222",
  "project_id": "vss-server--module",
  "state": "accepted",
  "target_revision": "2222222222222222222222222222222222222222"
}
```

거부 예시:

```json
{
  "ok": false,
  "reason": "VSS_INDEX_ALREADY_RUNNING",
  "detail": "같은 VSS project의 인덱싱이 진행 중이어서 새 작업을 제출하지 않았습니다.",
  "retryable": true,
  "snapshot_id": "22222222-2222-4222-8222-222222222222",
  "vss_reason": "already_running"
}
```

Module exception 문자열에는 내부 경로·credential이 포함될 수 있으므로 Frontend에는
안전한 요약만 반환하고 원문도 비밀정보 redaction 후 내부 attempt에 저장합니다.

## Backend 상태 조회 API

```http
GET /v1/index/status?project_id=<frontend-project-id>
```

Backend가 binding을 찾고 동일 프로세스/전용 worker의 VSS `status()`를 호출합니다.
응답에는 최소한 다음을 포함합니다.

```json
{
  "ok": true,
  "reason": "VSS_INDEX_STATUS_READ",
  "detail": "VSS 인덱싱 상태를 조회했습니다.",
  "retryable": false,
  "snapshot_id": "...",
  "project_id": "vss-server--module",
  "state": "indexing",
  "target_revision": "2222222222222222222222222222222222222222",
  "vss": {
    "state": "running",
    "processed": 4,
    "total": 14,
    "error": null
  }
}
```

상태 조회 자체가 성공해도 Snapshot이 실패했을 수 있으므로 HTTP `200`과 body의
`state/reason/detail`을 함께 읽습니다.

## Project ID·Branch binding

```text
Frontend project_id = h5vision/vision
VSS project_id      = vss-server--module
```

명시적 binding 예시:

```json
{
  "frontend_project_id": "h5vision/vision",
  "repository_id": "55555555-5555-4555-8555-555555555555",
  "branch_ref": "refs/heads/module",
  "vss_project_id": "vss-server--module",
  "active": true
}
```

- 수신 시점의 `binding_id`, `repository_id`, `branch_ref`, `vss_project_id`를 Snapshot에
  복사합니다.
- `VSS_PROJECT_ALIASES`는 Chat·조회 전용이므로 binding의 `vss_project_id`에는 적용하지
  않습니다. 인덱싱에는 exact VSS index ID를 전달합니다.
- 현재 Frontend 계약에서는 `frontend_project_id`당 활성 binding 하나만 허용합니다.
- binding 변경은 과거 Snapshot을 다시 쓰지 않습니다.
- 독립 Branch를 같은 `vss_project_id`에 자동 연결하지 않습니다.
- VSS `list_projects()`의 exact ID만 확인된 기존 index로 인정합니다.

## Admin Web → Backend

```http
GET    /v1/admin/repositories
POST   /v1/admin/repositories
PATCH  /v1/admin/repositories/{repository_id}
DELETE /v1/admin/repositories/{repository_id}
GET    /v1/admin/repositories/{repository_id}/branches

GET    /v1/admin/branch-bindings
POST   /v1/admin/branch-bindings
PATCH  /v1/admin/branch-bindings/{binding_id}
DELETE /v1/admin/branch-bindings/{binding_id}

GET    /v1/admin/snapshots?repository_id=...&branch_ref=...
GET    /v1/admin/snapshots/{snapshot_id}
POST   /v1/admin/snapshots/{snapshot_id}/retry
```

Branch에는 `/`가 포함될 수 있으므로 query parameter를 사용합니다. DELETE는 초기에는
`active=false` 비활성화입니다. Admin Web은 PostgreSQL, VSS Store, Ollama와 Git
credential에 직접 접근하지 않으며 모든 mutation은 인증·권한·감사 대상입니다.

## 주소 구분

```text
192.168.0.7/v1        Frontend → Snapshot Backend
127.0.0.1:11500       Frontend AI/Ollama portproxy 진입점
192.168.0.12:11500    위 portproxy 실제 대상
127.0.0.1:11434       VSS 코드의 기본 Ollama URL
<EC2>:8200             VSS standalone HTTP 예시, 모듈 호출에는 사용하지 않음
```

이 주소들을 서로 대체하지 않습니다.
