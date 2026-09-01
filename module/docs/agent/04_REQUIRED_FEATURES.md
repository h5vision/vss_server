# 필요한 기능

## 현재 구현 대조

| 요구 영역 | 현재 상태 | 남은 연결 |
|---|---|---|
| Frontend 입력 계약·안전 검증 | 실제 `/v1/workspace-overlays`까지 로컬 완료 | 실 Frontend E2E 대기 |
| Repository/Branch/VSS binding | project/workspace exact schema·DB 제약·overlay 해석 완료 | 인증된 CRUD API는 Phase 3A-3 |
| Snapshot 영속화 | ORM·Alembic·값/멱등/retention 제약 및 격리 PostgreSQL 17 적용 완료 | 운영 role/DSN과 전체 요청 E2E |
| VSS HTTP runtime | client·app lifecycle·DB/VSS readiness 로컬 완료 | 실제 배포·shared path 검증은 Phase 3B-2 |
| Frontend 조회 호환 | projects/models/briefing/index status 로컬 완료 | 실제 Frontend E2E 대기 |
| 전체 revision materialization | Git clone·delta·target tree/HEAD·immutable promote 로컬 완료 | shared path와 10초 E2E 대기 |
| 상태 동기화·복구·재시도 | exact 동기화·startup 복구·재시도와 PostgreSQL 단일 복구 조정자 잠금 완료 | AWS 다중 instance 실증과 인증 Admin route 대기 |
| Admin 관리 경계 | 내부 Backend 착수 가능 | service/router/test 먼저 구현, 독립 Web·IdP/RBAC·외부 공개는 결정 대기 |
| VSS source 조회 | source descriptor·revision 이력과 Git 검증값 로컬 완료 | VSS main 소비 코드·AWS loopback E2E |
| Repository/Branch 수집 | Phase 3A-2 로컬 완료 | Admin route/scheduler와 AWS remote Git E2E는 후속 |
| AWS Ubuntu 22.04.5 runtime | Python 3.10.12 non-root 전체 124개 통과 | 실제 systemd·health smoke |

Phase 3A-2 추가 뒤 Windows에서는 POSIX 권한 전용 1개를 제외한 130개가 통과합니다.
Ubuntu 22.04/Python 3.10.12와 Ubuntu 24.04/Python 3.12 non-root의 기존 기준은 각각
124개 통과이며 추가 수집 회귀는 이번 변경 검증 결과에서 별도로 갱신합니다.
격리 PostgreSQL 17의 migration·unique·Snapshot retry row lock·복구 advisory lock과
Repository sync claim 직렬화는 별도 5개 테스트로 통과했습니다. 운영
PostgreSQL role/DSN, VSS, shared filesystem을 함께 사용한 검증은 아직 완료되지 않았으므로
아래 요구사항 전체를 구현 완료로 해석하지 않습니다.

## P0 — Repository·Branch·commit SHA 수집

- Admin이 Repository를 등록하면 remote 접근 가능 여부와 기본 Branch를 검증합니다.
- `git ls-remote --heads`로 실제 Branch ref와 HEAD commit SHA를 수집합니다.
- 사용자가 선택한 Branch만 추적 대상으로 활성화합니다.
- bare mirror/cache를 fetch하여 선택 Branch에서 접근 가능한 Git object를 보존합니다.
- Branch별 현재 HEAD와 이전 HEAD 관측 이력을 append-only로 저장합니다.
- fast-forward, rewind/force-push, Branch 삭제·재생성을 구분합니다.
- 동일 HEAD 재수집은 Snapshot과 VSS Job을 중복 생성하지 않습니다.
- 새 HEAD는 exact commit 전체 tree로 materialize하고 Branch별 exact `vss_project_id`에 게시합니다.
- remote credential, Git stderr와 mirror 절대경로를 API·로그에 노출하지 않습니다.

모든 commit을 각각 전체 디렉터리로 복제하지 않습니다. Git mirror가 선택 Branch의 commit
object를 보존하고 DB가 Branch HEAD 관측 이력을 보존하며, VSS에 게시할 revision만 immutable
디렉터리로 materialize합니다.

Phase 3A-2 로컬 구현은 `git ls-remote --heads` 카탈로그와 사용자가 등록한 exact ref만
처리합니다. `.repository-cache/<repository-id>.git` bare cache에 선택 ref를 fetch하고
관측 SHA마다 `refs/vss-history/*` 보존 ref를 추가합니다. 동일 SHA는 이력을 중복 생성하지
않고, 새 SHA와 삭제·재생성만 append-only 이력으로 남깁니다. 수동·정기 trigger는 같은
lease 기반 service를 사용하며 만료된 실행은 실패로 보존한 뒤에만 새 실행을 허용합니다.
Webhook과 public Admin mutation은 포함하지 않습니다.

## P0 — VSS source descriptor

- VSS가 `project_id`와 선택적 40자리 `revision`으로 Snapshot 소스를 조회합니다.
- 응답은 Repository, Branch, Snapshot, expected commit/tree SHA와 exact `/index` body를
  schema version과 함께 반환합니다.
- 조회 직전에 immutable tree의 HEAD, tree SHA, object format과 clean 상태를 재검증합니다.
- VSS는 반환된 값을 server-local Git에서 독립 재검증합니다.
- inbound `SNAPSHOT_VSS_API_TOKEN`을 outbound `VSS_TOKEN`과 분리합니다.
- `/v1/internal/*`는 reverse proxy 외부 공개 대상이 아닙니다.

## P1 — Frontend 수신과 안전 검증 레거시 호환

- `POST /v1/workspace-overlays`에서 현재 Frontend JSON을 변경 없이 받습니다.
- 실제 40자리 `base_revision`, `target_revision`을 보존합니다.
- 세 변경 배열이 빈 empty commit도 유효하게 받습니다.
- 모든 응답에 `reason`, `detail`, `retryable`, `X-Request-ID`를 포함합니다.
- DB·materialization·VSS 제출 없이 임시 성공을 반환하지 않습니다.
- 안전한 상대 POSIX 경로만 허용하고 rename destination의 최종 content를 요구합니다.
- 중복·수정/삭제 충돌을 거부하며 content를 diff hunk로 해석하지 않습니다.

## P1 — 기존 Frontend Repository/Branch/VSS binding 호환

- overlay의 `frontend_project_id`, Sidebar의 선택적 `frontend_workspace_name`,
  `repository_id`, `branch_ref`, `vss_project_id`를 명시적으로 연결합니다.
- 현재 Frontend 계약에서는 Frontend project당 활성 binding 하나만 허용합니다.
- 없음·중복·비활성 binding이면 materialization/VSS 호출 전에 구조화된 `409`를
  반환합니다.
- VSS `GET /projects`의 exact ID만 기존 project로 확인합니다.
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

현재 source는 binding branch를 read-only clone하여 base와 target commit object를 모두
확인합니다. overlay 적용 결과를 `git write-tree`로 계산해 target commit tree와 비교한 뒤
HEAD를 target으로 고정합니다. Git object가 없는 local-only target과 executable bit,
submodule 등 현 Frontend payload만으로 재현할 수 없는 변경은 revision mismatch 또는
unsupported로 차단합니다.

### revision 보존

현 VSS 완료 commit은 `git_head(project_root)`입니다. 제출 전 다음을 검사합니다.

```text
materialized root Git HEAD == target_revision
```

일치하지 않거나 Git metadata가 없으면 현 기준 SHA에서는 완료 보장이 없습니다.
HTTP request의 `note`를 완료 commit처럼 취급하지 않습니다. local-only commit 지원은
Git object가 Backend에 제공되거나 VSS upstream이 explicit revision을 지원할
때까지 Production GO 차단 항목입니다.

## P0 — VSS HTTP client

- `POST /index`, `GET /index/status`, `GET /index/exists`, `GET /projects`, `GET /health`
  계약을 구현합니다.
- `VSS_BASE_URL`, 선택적 `VSS_TOKEN`, connect/read timeout을 환경변수로 관리합니다.
- materialization 뒤 `POST /index`만 호출하며 Frontend 10초 제한은 실환경 prewarmed
  source/cache와 VSS latency를 포함해 검증합니다.
- `accepted=true`를 완료로 기록하지 않습니다.
- `401`, `400`, `409`, 연결 실패, timeout과 invalid JSON/result를 구분합니다.
- 청킹, 임베딩, BM25와 Store promotion은 VSS가 소유합니다.
- HTTP status와 시작·상태 반환은 비밀정보를 제거해 attempt에 보존합니다.

## P1 — Frontend 조회 호환

- 같은 `vision.endpoint`로 들어오는 `/v1/projects`, `/v1/briefing`, `/v1/models`,
  `/v1/index/status`의 응답 형식을 실제 Frontend handler와 맞춥니다.
- overlay remote-path `project_id`, workspace 이름과 exact VSS index ID를 명시적
  binding/alias로 해석하며 유사 문자열 fallback은 금지합니다.
- Frontend `/index/update/files` delta를 VSS에 직접 전달하지 않습니다.
- Frontend 변경이 필요한 레거시 경로는 지원된 것처럼 성공 응답을 만들지 않습니다.

Phase 3B-1에서는 `/v1/projects`, `/v1/models`, `/v1/briefing`을 구현했고 Phase 5에서
`/v1/index/status`를 연결했습니다. VSS의
`project_root`, briefing의 `md_path`와 원문 upstream 오류는 Frontend 응답에서 제거합니다.
status도 VSS 원문 오류와 server-local 경로를 노출하지 않습니다.

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
upstream_status_code
vss_result_json
```

VSS HTTP status와 allowlist된 응답 필드는 attempt에 저장합니다. token, 파일 본문과
VSS 내부 절대경로는 저장하지 않습니다. Backend가 Frontend에 반환한 HTTP status는
별도 API/audit event에 기록할 수 있습니다.

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
| `submitting` | `failed` | VSS HTTP/인증/응답 계약 또는 결과 저장 실패 |
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
→ VSS POST /index
→ attempt/result·accepted/rejected commit
→ Frontend 응답
```

- DB 최초 저장 실패 시 filesystem/VSS 작업을 시작하지 않습니다.
- materialization 성공 뒤 DB 기록 실패는 고아 경로로 기록하고 recovery 대상으로 둡니다.
- VSS 접수 뒤 DB 결과 기록 실패는 성공으로 가장하지 않습니다.
- 로그에는 파일 수, 경로 수, revision, request/snapshot ID와 상태만 기록합니다.

## 시작·재시작 복구

- migration, 전용 root, VSS `/health`와 `/projects`를 readiness에서 검사합니다.
- Backend 재시작 뒤 VSS `/index/status`와 Snapshot DB를 대조합니다.
- status가 target commit을 보유하면 completed로 복구합니다.
- status가 기존 commit이고 `failed/aborted`이면 오류와 incomplete 정보를 보존합니다.
- 상태를 확인할 수 없으면 자동 force 재제출하지 않습니다.

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
- VSS URL, token과 timeout은 환경변수로 설정하고 로그/API에서 token을 redaction합니다.
- 전체 프로젝트 복사 비용과 디스크 사용량을 관측합니다.
- 파일·request·materialized size 제한은 실데이터로 확정합니다.
- Backend와 VSS가 동일한 `project_root`를 읽는 filesystem 배치를 보장합니다.
- Backend는 VSS Store `rag` schema에 직접 접근하지 않습니다.
- Admin Web과 Backend는 독립 배포합니다.
- 실제 AWS Ubuntu 22.04.5에서는 Python 3.10 이상, 3.15 미만의 module 전용 venv를
  사용합니다. 현재 확인된 Python 3.10.12가 지원 범위의 정본입니다.
- Ubuntu 24.04 통과 결과를 22.04 호환 증거로 대체하지 않고 두 OS 회귀를 분리합니다.

## 구현하지 않을 것

- Frontend delta를 VSS `/index/update/files`로 직접 전달하는 호환 호출
- FastAPI 자체 청킹·임베딩·BM25·promotion
- 임의 revision 또는 파일 hash를 Git SHA로 사용
- delta 디렉터리를 전체 프로젝트인 것처럼 VSS에 제출
- 유사 project ID 자동 선택
- Backend에서 `vss.indexer` 또는 VSS Store를 직접 import/접근
- 보존 정책 전 자동 물리 삭제
- 별도 합의 없는 Frontend AI `11500` 경로 변경
