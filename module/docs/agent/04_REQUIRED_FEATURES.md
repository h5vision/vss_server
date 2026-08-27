# 필요한 기능

## P0 — Frontend 수신과 안전 검증

- `POST /v1/workspace-overlays`에서 현재 Frontend JSON을 변경 없이 받습니다.
- 실제 40자리 `base_revision`, `target_revision`을 보존합니다.
- 세 변경 배열이 빈 empty commit도 유효하게 받습니다.
- 모든 응답에 `reason`, `detail`, `retryable`, `X-Request-ID`를 포함합니다.
- DB·materialization·VSS 제출 없이 임시 성공을 반환하지 않습니다.
- 안전한 상대 POSIX 경로만 허용하고 rename destination의 최종 content를 요구합니다.
- 중복·수정/삭제 충돌을 거부하며 content를 diff hunk로 해석하지 않습니다.

## P0 — Repository/Branch/VSS binding

- `frontend_project_id`, `repository_id`, `branch_ref`, `vss_project_id`를 명시적으로
  연결합니다.
- 현재 Frontend 계약에서는 Frontend project당 활성 binding 하나만 허용합니다.
- 없음·중복·비활성 binding이면 materialization/VSS 호출 전에 구조화된 `409`를
  반환합니다.
- VSS `list_projects()`의 exact ID만 기존 project로 확인합니다.
- 서로 독립적인 Branch는 별도 `vss_project_id`를 사용합니다.
- binding 값을 Snapshot 수신 시점에 복사하여 과거 이력을 고정합니다.

## P0 — 전체 revision materialization

VSS는 delta를 받지 않으므로 Backend가 전체 프로젝트 트리를 준비해야 합니다.

```text
base revision tree 확보
→ snapshot 전용 staging 생성
→ added/modified content 기록
→ deleted path 제거
→ rename old path 제거와 new content 확인
→ target revision 정합성 검증
→ immutable revision path로 원자 승격
```

필수 규칙:

- 모든 읽기·쓰기·삭제 경로가 전용 materialization root 내부인지 resolve 후 확인합니다.
- symlink/junction을 통한 root 이탈을 차단합니다.
- 기존 immutable revision 디렉터리를 덮어쓰지 않습니다.
- 실패한 staging과 완성 revision을 구분합니다.
- base tree 제공자가 없으면 `SNAPSHOT_BASE_REVISION_UNAVAILABLE`을 반환합니다.
- 최초 Snapshot에는 Frontend delta 외에 bootstrap full-tree source가 필요합니다.

### revision 보존

현 VSS 완료 commit은 `git_head(project_root)`입니다. 제출 전 다음을 검사합니다.

```text
materialized root Git HEAD == target_revision
```

일치하지 않거나 Git metadata가 없으면 현 기준 SHA에서는 완료 보장이 없습니다.
`extra_meta.requested_revision`을 완료 commit처럼 취급하지 않습니다. local-only commit
지원은 Git object가 Backend에 제공되거나 VSS upstream이 explicit revision을 지원할
때까지 Production GO 차단 항목입니다.

## P0 — VSS module adapter

- `vss.indexer`를 설정 완료 후 lazy import합니다.
- `start_index`, `status`, `exists`, `list_projects`와 signature를 검사합니다.
- `start_index(..., blocking=False)`만 사용해 Frontend 10초 제한 안에 응답합니다.
- `accepted=true`를 완료로 기록하지 않습니다.
- `already_running`, `not_a_directory`, 예외와 invalid result를 구분합니다.
- 청킹, 임베딩, BM25와 Store promotion은 VSS가 소유합니다.
- 시작·상태 반환은 비밀정보를 제거한 `module_result_json`에 보존합니다.

## P1 — Snapshot 영속화

### Snapshot 최소 필드

```text
snapshot_id
request_id
frontend_project_id
vss_project_id
binding_id
repository_id
branch_ref
base_revision
target_revision
source_type
state
materialized_project_root
attempt_count
vss_state
vss_reason
vss_detail
created_at
updated_at
```

`materialized_project_root`는 관리자에게 전체 서버 절대경로로 노출하지 않고 안전한
locator 또는 축약 표시를 사용합니다.

### 변경 파일 레코드

```text
snapshot_id
status
path
encoding
content_or_locator
deleted
old_path
created_at
```

MVP는 PostgreSQL `snapshot` schema에 저장합니다. 대용량 본문은 후속 Object Storage
locator로 교체할 수 있어야 합니다.

### VSS attempt

```text
attempt_id
snapshot_id
request_id
attempt_number
started_at
finished_at
vss_state
vss_reason
vss_detail
retryable
latency_ms
module_result_json
```

VSS module에는 HTTP status code가 없으므로 `upstream_status_code`를 만들지 않습니다.
Backend가 Frontend에 반환한 HTTP status는 별도 API/audit event에 기록할 수 있습니다.

## Snapshot 상태 전이

| 현재 | 다음 | 조건 |
|---|---|---|
| `received` | `validated` | schema·업무 검증 성공 |
| `received` | `rejected` | 복구 불가능 request 오류 |
| `validated` | `binding_required` | 활성 binding 없음·중복 |
| `validated` | `materializing` | binding과 base source 확정 |
| `materializing` | `materialized` | 전체 tree 승격·revision gate 성공 |
| `materializing` | `failed` | materialization 또는 revision 검증 실패 |
| `materialized` | `submitting` | VSS 호출 직전 DB commit |
| `submitting` | `accepted` | VSS `accepted=true` |
| `submitting` | `rejected` | `already_running`, `not_a_directory` 등 거부 |
| `submitting` | `failed` | module import/call/result 저장 실패 |
| `accepted` | `indexing` | running/indexing_lexical/promoting 확인 |
| `accepted/indexing` | `completed` | done + index.commit=target |
| `accepted/indexing` | `failed` | failed 또는 revision mismatch |
| `accepted/indexing` | `aborted` | aborted 확인 |

재시도는 같은 `snapshot_id`에 attempt만 추가합니다. 금지된 역전이를 허용하지 않으며
materialized tree는 target revision과 동일한 경우 재사용할 수 있습니다.

## 멱등성

```text
(vss_project_id, target_revision) UNIQUE
(repository_id, branch_ref, target_revision) history identity
```

- binding 전에는 `(frontend_project_id, target_revision)`로 임시 중복을 막습니다.
- 완료된 exact target이면 새 VSS Job 없이 `TARGET_ALREADY_INDEXED`를 반환합니다.
- accepted/indexing 중이면 중복 제출하지 않습니다.
- 동시 요청은 DB unique constraint와 transaction으로 하나만 생성합니다.
- `force=true`는 관리자 재시도 정책과 VSS Job 상태 확인 없이 자동 적용하지 않습니다.

## DB·filesystem·VSS 순서

```text
request 검증
→ binding 확정
→ Snapshot/delta DB commit
→ materializing 상태 commit
→ staging 작성과 immutable promote
→ materialized 상태·locator commit
→ submitting 상태 commit
→ VSS start_index
→ attempt/result·accepted/rejected commit
→ Frontend 응답
```

- DB 최초 저장 실패 시 filesystem/VSS 작업을 시작하지 않습니다.
- materialization 성공 뒤 DB 기록 실패는 고아 경로로 기록하고 recovery 대상으로 둡니다.
- VSS 접수 뒤 DB 결과 기록 실패는 성공으로 가장하지 않습니다.
- 로그에는 파일 수, 경로 수, revision, request/snapshot ID와 상태만 기록합니다.

## 시작·재시작 복구

- migration, 전용 root, VSS package/signature, Store를 readiness에서 검사합니다.
- 프로세스 재시작으로 `JOBS`가 사라져도 VSS Store와 Backend Snapshot DB를 대조합니다.
- Store가 target commit을 보유하면 completed로 복구합니다.
- Store가 기존 commit이고 incomplete build가 있으면 failed/aborted로 보존합니다.
- 상태를 확인할 수 없으면 자동 force 재제출하지 않습니다.
- Chroma/VSS 초기 운영은 단일 프로세스입니다.

## 독립 Admin Web

- Repository CRUD와 soft deactivate
- Branch/VSS project binding CRUD
- Snapshot 목록·상세·attempt·materialization/VSS 상태 조회
- 동일 Snapshot 수동 재시도
- 관리자 인증, RBAC, CORS, mutation 감사
- browser에 DB/VSS/Ollama/Git credential 비노출
- retention 확정 전 Snapshot 물리 삭제 금지

## 비기능 요구사항

- 모든 설정은 환경변수 기반이며 비밀값은 소스에 넣지 않습니다.
- Windows/WSL/container/server-local 경로를 구분합니다.
- `VSS_*`는 VSS import 전에 설정합니다.
- 전체 프로젝트 복사 비용과 디스크 사용량을 관측합니다.
- 파일·request·materialized size 제한은 실데이터로 확정합니다.
- VSS Job/Store와 같은 process ownership을 보장합니다.
- Store `rag`와 Backend `snapshot` schema의 write role을 분리합니다.
- Admin Web과 Backend는 독립 배포합니다.

## 구현하지 않을 것

- 과거 `/index/update/files` 호환 호출
- FastAPI 자체 청킹·임베딩·BM25·promotion
- 임의 revision 또는 파일 hash를 Git SHA로 사용
- delta 디렉터리를 전체 프로젝트인 것처럼 VSS에 제출
- 유사 project ID 자동 선택
- 여러 worker에서 동일 in-process VSS를 무조정 호출
- 보존 정책 전 자동 물리 삭제
- 별도 합의 없는 Frontend AI `11500` 경로 변경
