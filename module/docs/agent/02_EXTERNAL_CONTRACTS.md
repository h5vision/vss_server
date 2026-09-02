# 외부 HTTP 통신 계약

## 기본 원칙

- Snapshot 모듈의 정본 입력은 Admin이 등록한 Repository와 사용자가 선택한 추적 Branch입니다.
- 모듈은 remote HEAD commit SHA를 수집·보존하고 완전한 revision 디렉터리로 materialize합니다.
- VSS에는 파일 delta JSON을 보내지 않고 HTTP `POST /index`로 완성된 디렉터리 경로를
  전달합니다.
- VSS는 내부 source API로 exact commit/tree SHA와 `/index` 입력값을 조회할 수 있습니다.
- 장기적으로 VSS는 같은 loopback 경계에서 Branch/Tag/PR/MR와 Snapshot 관계를 pull하여
  사용자 질의에 사용할 revision과 답변 provenance의 참고 자료로 사용합니다.
- 기존 Frontend HTTP request는 호환 경계로 보존하지만 신규 수집 구조의 정본이 아닙니다.
- 상대 규약에 없는 값을 Frontend 필수 입력으로 만들지 않습니다.
- 성공·접수·거부·실패는 HTTP status와 구조화된 `reason`, `detail`, `retryable`로
  구분합니다.

## 현재 구현 범위

```text
완료       Frontend/VSS/Admin Pydantic 계약, VSS HTTP client
로컬 완료  VSS → Backend source descriptor·revision 조회와 Git 독립 검증값
로컬 완료  PostgreSQL Snapshot ORM·migration, Repository/Binding 저장소
로컬 완료  DB/VSS readiness, /v1/projects·/v1/models·/v1/briefing proxy
로컬 완료  /v1/workspace-overlays, Git materialization, VSS 접수·attempt 저장
로컬 완료  /v1/index/status, VSS 완료 동기화, startup 복구·내부 재시도
로컬 완료  사용자 선택 Repository·Branch catalog/fetch/HEAD SHA 수집 코어
로컬 완료  인증된 /v1/admin/*와 독립 Admin Web
```

아래 Backend 내부 처리 순서와 접수·거부 HTTP 응답은 Phase 4, 완료 상태 조회와 startup
복구는 Phase 5 로컬 구현에 연결됐습니다. 재시도는 인증된 Admin route가 아니라 내부
서비스로만 제공합니다.

Phase 3A-2 수집 코어는 아직 public HTTP route가 아니라 app lifespan에 준비되는 내부
`RepositoryCollectionService`입니다. `manual|periodic` trigger가 같은 저장소 lease와
동기화 함수를 사용하며 Webhook trigger는 Phase 3A-4 전까지 허용하지 않습니다.

## VSS → Snapshot Backend

VSS는 Frontend를 경유하지 않고 같은 인스턴스의 loopback에서 Snapshot 소스를 조회합니다.

```http
GET /v1/internal/vss/source?project_id=<exact-vss-project-id>&revision=<optional-sha>
X-Snapshot-Token: <SNAPSHOT_VSS_API_TOKEN>

GET /v1/internal/vss/revisions?project_id=<exact-vss-project-id>&limit=100
X-Snapshot-Token: <SNAPSHOT_VSS_API_TOKEN>
```

source 응답은 `repository_id`, `branch_ref`, `target_revision`, `expected_commit_sha`,
`expected_tree_sha`, clean working tree 판정과 exact VSS `/index` body를 반환합니다. VSS는
server-local `project_root`에서 HEAD, tree SHA와 clean 상태를 독립 검증한 뒤 사용합니다.
상세 schema, 호출 예시와 실패 reason은 `13_VSS_SOURCE_API.md`가 정본입니다.

`SNAPSHOT_VSS_API_TOKEN`은 inbound 전용이며 Backend outbound `VSS_TOKEN`과 분리합니다.
외부 ingress는 `/v1/internal/*`를 공개하지 않습니다.

### Phase 7 Revision Context 확장 방향

VSS가 `/v1/chat`과 자연어 질의 해석을 계속 소유합니다. Snapshot Backend는 Chat을 proxy하거나
질의를 받아 LLM으로 commit을 선택하지 않습니다. 대신 VSS가 localhost에서 pull할 수 있도록
Repository/Branch/Tag/PR/MR의 exact commit 관계, Snapshot 상태와 `index.commit` 증거를
결정론적 내부 조회로 제공합니다.

다음 route는 Phase 7 제안이며 현재 구현된 API가 아닙니다.

```http
GET /v1/internal/vss/refs?project_id=<exact-vss-project-id>
GET /v1/internal/vss/change-requests?project_id=<exact-vss-project-id>
GET /v1/internal/vss/change-requests/{provider}/{number}?project_id=<exact-vss-project-id>
GET /v1/internal/vss/context?project_id=<exact-vss-project-id>&revision=<sha>
```

Branch, Tag와 change request selector는 하나만 명시하며, 모호한 selector를 최신 active
index로 임의 해석하지 않습니다. PR/MR 변경 질의에는 base/head SHA, 병합 결과 질의에는
실제 merge SHA를 구분해서 제공합니다. 전체 계약 방향과 완료 조건은
`15_REVISION_CONTEXT_PROVIDER.md`가 정본입니다.

## Frontend → Backend 레거시 호환 경계

```http
POST /v1/workspace-overlays
Content-Type: application/json
```

기준 Frontend SHA의 설정 기본값은 역사적으로 `http://192.168.0.7/v1`입니다. 현재 AWS
동일 인스턴스 배포에서는 이 값을 그대로 사용하지 않고 VSS 운영 측이 승인한 외부 ingress
주소로 설정해야 합니다. Nginx는 필수 계약이 아닙니다.

```text
https://<AWS-INGRESS>/v1/workspace-overlays
```

이 기본값은 `vision/package.json`의 `vision.endpoint`에서 옵니다. `APIService.ts`의
`http://127.0.0.1:5000`은 설정 스키마를 읽을 수 없을 때의 Frontend PC fallback이며 AWS
Backend를 가리키지 않습니다.

### Frontend 보조 API 호환 경계

현재 Sidebar는 같은 endpoint에 `/models`, `/projects`, `/briefing`, `/index/status`도
호출합니다. Backend는 앞의 세 경로를 각각 `/v1/models`, `/v1/projects`,
`/v1/briefing` proxy로 제공하고 VSS 응답을 Frontend 형식으로 변환합니다.
`/v1/index/status`는 Phase 5에서 Snapshot DB 상태와 VSS 완료 revision을 동기화하도록
제공합니다.

Frontend는 overlay에는 remote 기반 `project_id`(예: `h5vision/vision`)를 보내지만
briefing/status 조회에는 workspace 이름(예: `vision`)을 보냅니다. 두 값은 binding의
`frontend_project_id`, `frontend_workspace_name`으로 각각 exact match하고 동일
`vss_project_id`로 변환합니다. 문자열 포함·접두사 같은 유사 매칭은 금지합니다.

`RemoveRAGTEST`의 `/index/update/files`와 초기 인덱싱의
`/v1/documents/ingest-with-metadata`는 레거시 호출입니다. 최신 VSS `/index`에 delta를
직접 전달하는 호환 처리는 금지하며, Frontend 변경 또는 별도 adapter 계약이 확정되기
전에는 지원 완료로 표시하지 않습니다.

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
→ VSS HTTP `POST /index` 제출
→ HTTP result와 index 상태 저장
→ Frontend 구조화 응답
```

DB 최초 commit 전에 VSS를 호출하지 않습니다. materialization은 설정된 전용 root 아래
staging에서 수행하고 성공 후 원자적으로 revision 경로에 승격합니다. Snapshot ID는
내부 UUID이며 Git SHA와 다릅니다.

현재 base source는 binding branch의 read-only Git clone입니다. base와 target commit
object가 모두 확인되고, overlay 적용 결과의 staged tree가 target commit tree와 정확히
같아야 합니다. 이후 Git HEAD를 target으로 이동하고 clean working tree를 다시 확인합니다.
target object가 없는 local-only commit, tree 불일치, `.git` 경로 변경과 symlink/junction
tree는 VSS 호출 전에 차단합니다.

## Backend → VSS HTTP 서버

과거 `/index/update/files`는 사용하지 않습니다. 최신 `vss_server/main`의 실제 호출은
다음입니다.

```http
POST /index
Content-Type: application/json
X-VSS-Token: <설정된 경우>

{
  "project_root": "/srv/snapshots/vss-server--module/<target-sha>",
  "project_id": "vss-server--module",
  "profile": {"context_header": true, "use_bm25": true},
  "force": false,
  "briefing": true,
  "note": "snapshot <target-sha>"
}
```

`project_root`는 VSS 서버 프로세스가 읽을 수 있는 server-local/shared 경로여야 합니다.
현 HTTP request에는 `revision`, `snapshot_id`, `requested_revision` 필드가 없습니다.
완료 commit은 VSS가 `git_head(project_root)`에서 읽으므로 제출 전에 Git HEAD를
검증합니다.

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

VSS HTTP status와 허용된 JSON 반환 필드는 attempt에 보존합니다. 파일 본문, token과
내부 절대경로는 외부 응답이나 로그에 저장하지 않습니다.

### 상태 조회

```http
GET /index/status?project_id=vss-server--module
X-VSS-Token: <설정된 경우>
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

## VSS HTTP 연결 계약

- Backend와 VSS가 같은 AWS Ubuntu network namespace에서 service로 실행되므로
  `VSS_BASE_URL=http://127.0.0.1:8200`을 사용합니다.
- `VSS_TOKEN`이 설정되면 `X-VSS-Token` 또는 `Authorization: Bearer`를 사용합니다.
- readiness에서 `GET /health`, `GET /projects`의 HTTP status와 JSON shape를 검사합니다.
- connect/read timeout을 분리하고 token, 응답의 내부 경로와 원문 예외를 redaction합니다.
- `POST /index`의 `202`는 접수, `409`는 거부입니다. 완료는 반드시 status polling으로
  확인합니다.
- Backend와 VSS가 다른 filesystem namespace이면 shared mount 또는 VSS-local
  materializer가 필요합니다.
- VSS exact source SHA는 배포 manifest/image에서 고정합니다. 현재 health response에는
  source revision이 없으므로 API만으로 pin을 증명할 수 없습니다.

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
| `409` | overlay tree와 target commit tree 불일치 | `SNAPSHOT_REVISION_MISMATCH` | `false` |
| `409` | 동일 target Snapshot 존재 | `SNAPSHOT_ALREADY_EXISTS` | 상태별 |
| `422` | Frontend schema 검증 실패 | `REQUEST_VALIDATION_FAILED` | `false` |
| `500` | Snapshot 최초 DB 저장 실패 | `SNAPSHOT_PERSIST_FAILED` | `true` |
| `500` | materialization 내부 실패 | `SNAPSHOT_MATERIALIZATION_FAILED` | `true` |
| `500` | VSS 결과 DB 기록 실패 | `SNAPSHOT_RESULT_PERSIST_FAILED` | `true` |
| `502` | VSS HTTP 응답/JSON 계약 불일치 | `VSS_HTTP_CONTRACT_MISMATCH` | `false` |
| `502` | VSS가 HTTP 요청 거부 | `VSS_HTTP_REQUEST_REJECTED` | `false` |
| `502` | VSS 인증 실패 | `VSS_AUTH_FAILED` | `false` |
| `503` | VSS 연결·timeout 실패 | `VSS_HTTP_UNAVAILABLE` | `true` |
| `503` | VSS Store/Ollama 의존성 실패 | `VSS_DEPENDENCY_UNAVAILABLE` | `true` |
| `503` | Git source clone 실패 | `SNAPSHOT_SOURCE_UNAVAILABLE` | `true` |

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

VSS HTTP error body에는 내부 경로가 포함될 수 있으므로 Frontend에는 안전한 요약만
반환하고 원문도 비밀정보 redaction 후 내부 attempt에 저장합니다.

## Backend 상태 조회 API

```http
GET /v1/index/status?project_id=<frontend-project-id>
```

Backend가 binding을 찾고 VSS `GET /index/status`를 호출합니다.
응답에는 최소한 다음을 포함합니다.

```json
{
  "ok": true,
  "reason": "VSS_INDEX_IN_PROGRESS",
  "detail": "VSS가 Snapshot target revision을 인덱싱하고 있습니다.",
  "retryable": false,
  "snapshot_id": "...",
  "project_id": "vss-server--module",
  "state": "indexing",
  "target_revision": "2222222222222222222222222222222222222222",
  "vss": {
    "state": "running",
    "processed": 4,
    "total": 14,
    "chunk_count": 20
  }
}
```

상태 조회 자체가 성공해도 Snapshot이 실패했을 수 있으므로 HTTP `200`과 body의
`state/reason/detail`을 함께 읽습니다.

`running|indexing_lexical|promoting`은 `VSS_INDEX_IN_PROGRESS`, exact `done`은
`VSS_INDEX_COMPLETED`, 다른 commit의 `done`은 `VSS_REVISION_MISMATCH`를 반환합니다.
VSS 상태가 `none`이면 `/index/exists`의 active commit을 보조 증거로 사용하며 exact
target이면 `TARGET_ALREADY_INDEXED`, 없으면 `VSS_INDEX_STATUS_MISSING`입니다. 조회 실패는
안전한 `502/503` 오류로 반환하고 upstream 원문·절대경로를 노출하지 않습니다.

## Project ID·Branch binding

```text
Frontend project_id = h5vision/vision
VSS project_id      = vss-server--module
```

명시적 binding 예시:

```json
{
  "frontend_project_id": "h5vision/vision",
  "frontend_workspace_name": "vision",
  "repository_id": "55555555-5555-4555-8555-555555555555",
  "branch_ref": "refs/heads/module",
  "vss_project_id": "vss-server--module",
  "active": true
}
```

- 수신 시점의 `binding_id`, `repository_id`, `branch_ref`, `vss_project_id`를 Snapshot에
  복사합니다.
- `frontend_workspace_name`은 Sidebar 조회용 선택 필드이며 설정된 경우 활성 binding
  전체에서 exact unique입니다.
- `VSS_PROJECT_ALIASES`는 Chat·조회 전용이므로 binding의 `vss_project_id`에는 적용하지
  않습니다. 인덱싱에는 exact VSS index ID를 전달합니다.
- 현재 Frontend 계약에서는 `frontend_project_id`당 활성 binding 하나만 허용합니다.
- binding 변경은 과거 Snapshot을 다시 쓰지 않습니다.
- 독립 Branch를 같은 `vss_project_id`에 자동 연결하지 않습니다.
- VSS `GET /projects`의 exact ID만 확인된 기존 index로 인정합니다.

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
127.0.0.1:8000        VSS/Admin service → Snapshot Backend
127.0.0.1:8200        Snapshot Backend → 같은 인스턴스 VSS HTTP API
127.0.0.1:5432        Snapshot Backend → 같은 인스턴스 PostgreSQL
<AWS HTTPS>/v1        외부 Frontend → 승인된 ingress → Snapshot Backend
<AWS HOST>:4180       외부 Browser → 독립 Admin service 예정
127.0.0.1:11500       Frontend AI/Ollama portproxy 진입점
192.168.0.12:11500    위 portproxy 실제 대상
127.0.0.1:11434       VSS 코드의 기본 Ollama URL
```

AWS 내부 loopback과 Frontend Windows host의 loopback은 서로 다른 network namespace이므로
서로 대체하지 않습니다.
