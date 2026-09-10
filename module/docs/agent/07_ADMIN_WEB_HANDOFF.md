# 독립 Admin Web 인계 계약

## 2026-09-09 VSS inbound failure observability

VSS? Snapshot Backend? `/v1/internal/vss/*`? ???? ? ??? 200/202? ??? Backend?
warning log? ?? `snapshot.audit_logs`? `vss_inbound_request` ???? ?????. ???? ?? ?
VSS route? 404? ????? VSS ?? ? ??? ??? ? Module ???? ??? ??? ? ????.
?? query ?? redaction?? Admin ?? `GET /v1/admin/vss/request-failures`? Admin Web?
`VSS request failures` ??? ?? ???? ?????. BFF allowlist? admin-only???.

## 2026-09-09 encoded Admin path 서명 호환성 수정 완료

AWS Vector 삭제 실증에서 `@`가 포함된 VSS project ID가 `%40`으로 전달될 때 BFF와 Backend의 canonical path가 달라 401이 발생하는 문제를 확인했습니다. Snapshot Backend는 이제 Admin Web BFF와 동일한 ASGI raw path/query를 HMAC 검증에 사용하고, Browser는 BFF 사용자 세션의 `AUTHENTICATION_REQUIRED`만 로그인 이동으로 처리합니다. 실제 `@` project ID의 encoded DELETE 경로를 회귀 테스트에 고정했으며 full pytest와 module sandbox gate를 통과했습니다.

## 2026-09-07 Admin browser 확인창 호환성 보강

AWS Admin Web을 ChatGPT Windows app 내장 browser에서 실증한 결과, Tracked Branch의 `Index` 버튼은 표시·물리 click까지 정상이어도 native `window.confirm()` 승인 단계에서 진행되지 않아 Admin Web/Backend에 POST가 발생하지 않는 호환성 문제가 확인됐습니다.

- Tracked Branch Index, Snapshot Index, Commit Materialize의 native `window.confirm()`을 모두 제거하고 페이지 내부 `<dialog role="alertdialog">` 확인창으로 통일합니다.
- 확인창의 `Index`/`Materialize` 버튼은 autofocus를 사용하며, 취소·닫기·Escape는 요청을 전송하지 않고 안전하게 종료합니다.
- 확인 이후의 기존 BFF endpoint, RBAC, HMAC signing, timeout, Backend/VSS 계약은 변경하지 않습니다.
- `app.js`와 `styles.css`의 asset version을 `index-confirm-dialog`로 올려 기존 Admin 탭이 이전 JavaScript를 계속 사용하는 가능성을 줄입니다.
- 회귀 계약은 Admin Web 정적 UI 테스트에서 confirm dialog element 존재, `confirmAdminAction` 사용, `window.confirm()` 부재를 고정합니다.

## 2026-09-04 확정 운영 계약

이 절은 이전 문서의 충돌하는 자동 인덱싱·`vss_pull` 우선 표현보다 우선합니다.

- **VSS가 유일한 Indexer입니다.** Snapshot Module은 파일 수집 정책, chunking, embedding, BM25, vector/vector-store build·promote를 구현하거나 복제하지 않습니다. 실제 인덱싱은 `vss_server`의 `POST /index -> indexer.start_index()` 경로만 사용합니다.
- Repository sync/registration is separated from indexing. `SNAPSHOT_REPOSITORY_ROOT=/home/ubuntu/repos` stores only the rebuildable `.repository-cache/<repository-id>.git` bare object cache; PostgreSQL is authoritative for Repository/Branch/HEAD/commit state. Sync updates refs, objects, commit catalog and Snapshot readiness and never auto-calls VSS `POST /index`.
- Before every VSS request, Module verifies the immutable exact Snapshot under `SNAPSHOT_MATERIALIZATION_ROOT=/home/ubuntu/vss-snapshots`. Current HEAD and historical commits both use that immutable tree as `/index.project_root`; no mutable Branch working copy is created, refreshed, or exposed.
- 인덱싱 시작은 **Admin의 명시적 Index 요청**이 소유합니다. `POST /v1/admin/tracked-branches/{tracked_branch_id}/index`는 current tracked HEAD Snapshot을 선택해 branch working copy를 exact target SHA로 refresh한 뒤 VSS에 제출하고, 기존 `POST /v1/admin/snapshots/{snapshot_id}/index`는 Snapshot 직접 Index와 historical fallback을 유지합니다. 두 경로 모두 `project_id`, `force=false`, `briefing`, `note` 계약을 유지하며 Browser가 `project_root`나 revision override를 지정할 수 없습니다.
- Index/Retry operations sharing a Tracked Branch are serialized with a Branch row lock. If another Snapshot for the same `vss_project_id` is `submitting`, `accepted`, or `indexing`, the Backend returns 409 before refreshing the working copy, including the DB-submitting to VSS-running observation gap.
- Remote drift is checked before changing the VSS-visible working tree. If the fetched Branch HEAD differs from the recorded target SHA, the request fails with `REPOSITORY_BRANCH_HEAD_MISMATCH` and the existing checkout is preserved.
- Admin Web keeps the ordinary Backend timeout at 30s, uses `ADMIN_WEB_INDEX_TIMEOUT_SECONDS` (default 120s) for Index/Retry mutations, and keeps `ADMIN_WEB_RUNTIME_MODEL_TIMEOUT_SECONDS` (default 210s) for Ollama lifecycle mutations.
- Module은 VSS의 `GET /index/status`와 `GET /index/exists`를 관측하고, `state=done`뿐 아니라 `index.commit == snapshot.target_revision`까지 확인한 경우에만 Snapshot을 `completed`로 수렴시킵니다.
- 현재 운영 오케스트레이션 방향은 **`module_push`**이지만 의미는 “sync 시 자동 push”가 아니라 **Admin 요청으로 생성된 IndexCommand를 Module이 VSS에 제출**한다는 뜻입니다. `vss_pull`과 `/v1/internal/vss/*`는 provenance/read-model 및 향후 선택 기능으로 유지하며 현재 pre-rag VSS의 필수 data plane으로 간주하지 않습니다.
- Commit History/Compare는 Admin 분석 기능으로 유지합니다. **비교 결과로 reference commit SHA를 자동 선택하거나 VSS에 전달하는 기능, multi-revision 답변 context는 구현 보류**입니다.


최종 확인일: 2026-09-02 KST

Admin Web은 VS Code Webview가 아닌 독립 브라우저 애플리케이션입니다. VSS 전환 뒤에도
Backend의 관리 API만 호출합니다.

## 서비스 경계

```text
Browser
  ↓ HTTPS + 관리자 인증
Independent Admin Web
  ↓ JSON
Snapshot Backend /v1/admin/*
  ├─ PostgreSQL snapshot schema
  ├─ materialization metadata
  └─ VSS HTTP status proxy
```

Browser와 Admin Web은 VSS HTTP API, Chroma/pgvector, Ollama, PostgreSQL과 Git
credential에 직접 접근하지 않습니다.

## 목표 계약 구현 위치

Phase 2H에서 `module/backend/integrations/vss/client.py`의 HTTP 경계까지 완료했습니다.
Phase 3A-1에서 PostgreSQL ORM/Alembic과 Repository/Binding 내부 저장소까지 완료했습니다.
Phase 3B-1에서 VSS catalog/runtime dependency와 Frontend 조회 proxy를 연결했습니다.
Repository/Branch 수집 코어는 Phase 3A-2에서 로컬 완료됐고 인증된 Admin mutation과
독립 Web과 인증된 Backend Admin API는 Phase 3A-3 로컬 범위로 구현했습니다. VSS source
조회는 Phase 2V에서 구현했습니다.

| 경계 | 위치 |
|---|---|
| Frontend overlay | `module/backend/features/workspace_overlays/schemas.py` |
| materialized VSS command | `module/backend/features/workspace_overlays/mapper.py` |
| VSS HTTP result/status | `module/backend/integrations/vss/schemas.py` |
| VSS HTTP client | `module/backend/integrations/vss/client.py` |
| Frontend 조회 proxy | `module/backend/features/frontend_proxy/` |
| Repository/Branch/VSS binding | `module/backend/features/repositories/schemas.py` |
| Repository/Binding 저장소 | `module/backend/features/repositories/store.py` |
| Snapshot DB ORM·migration | `module/backend/infrastructure/database/`, `module/alembic/` |
| Snapshot 목록·상세·재시도 | `module/backend/features/snapshots/schemas.py` |
| 공통 Admin 오류·mutation | `module/backend/features/admin/schemas.py` |
| VSS source·revision 조회 | `module/backend/features/vss_sources/` |

fixture는 `tests/fixtures/frontend`, `tests/fixtures/vss`, `tests/fixtures/admin`에 둡니다.
Admin client type은 문서 예시보다 Backend OpenAPI와 fixture를 기준으로 생성합니다.

## 구현된 관리 API

| Method | Path | 화면 동작 | Phase |
|---|---|---|---:|
| `GET` | `/v1/admin/repositories` | Repository 목록 | 3A-3 |
| `POST` | `/v1/admin/repositories` | Repository 등록 | 3A-3 |
| `PATCH` | `/v1/admin/repositories/{repository_id}` | 표시값·기본 Branch 변경 | 3A-3 |
| `DELETE` | `/v1/admin/repositories/{repository_id}` | soft deactivate | 3A-3 |
| `GET` | `/v1/admin/repositories/{repository_id}/branches` | 원격 Branch catalog | 3A-3 |
| `POST` | `/v1/admin/repositories/{repository_id}/sync` | 수동 fetch/HEAD 수집 | 3A-3 |
| `GET/POST` | `/v1/admin/tracked-branches` | 추적 Branch 목록·등록 | 3A-3 |
| `PATCH/DELETE` | `/v1/admin/tracked-branches/{tracked_branch_id}` | 변경·비활성화 | 3A-3 |
| `GET` | `/v1/admin/tracked-branches/{tracked_branch_id}/head-history` | 관측 HEAD 이력 | 3A-3 |
| `POST` | `/v1/admin/tracked-branches/{tracked_branch_id}/index` | current HEAD branch working copy exact 검증 후 VSS Index | 9.2 branch index |
| `GET` | `/v1/admin/repository-sync-runs` | 수동·정기 sync 실행 이력 | 3A-3 |
| `GET/POST` | `/v1/admin/branch-bindings` | Frontend binding 목록·등록 | 3A-3 |
| `PATCH/DELETE` | `/v1/admin/branch-bindings/{binding_id}` | binding 변경·비활성화 | 3A-3 |
| `GET` | `/v1/admin/vss/projects` | VSS exact project catalog | 3A-3 |
| `GET` | `/v1/admin/runtime/models` | Ollama 설치/Running/Stopped/Auto Up 상태 | runtime observability |
| `POST` | `/v1/admin/runtime/models/up` | 설치 모델을 resident 상태로 Up | operator runtime control |
| `POST` | `/v1/admin/runtime/models/down` | 모델을 Down하고 해당 Auto Up을 해제 | operator runtime control |
| `POST` | `/v1/admin/runtime/models/reload` | Auto Up 정책을 유지한 채 모델 Reload | operator runtime control |
| `PUT` | `/v1/admin/runtime/models/auto-up` | 모델별 Auto Up ON/OFF | operator runtime control |
| `POST` | `/v1/admin/runtime/models/run` | 기존 Up/preload 호환 alias | operator compatibility |
| `GET` | `/v1/admin/snapshots` | Branch별 SHA/Snapshot 이력 | 3A-3 |
| `GET` | `/v1/admin/snapshots/{snapshot_id}` | 상세·attempt | 3A-3 |
| `POST` | `/v1/admin/snapshots/{snapshot_id}/retry` | 동일 Snapshot 재시도 | 5 |
| `GET` | `/v1/admin/audit-logs` | mutation·거부·실패 감사 이력 | 3A-3 |

Branch에는 `/`가 포함되므로 `branch_ref` query parameter를 사용합니다. 목록은 opaque
cursor 기반이며 UI가 cursor 내부 형식을 해석하지 않습니다.

## 구현된 화면 동작

- 목록은 25개 단위로 조회하고 응답의 opaque `next_cursor`를 그대로 다음 요청에 전달합니다.
- Tracked Branch와 Frontend Binding 등록은 Repository UUID 수기 입력 대신 Repository
  selector와 원격 Branch catalog의 exact `refs/heads/*` 값을 사용합니다.
- Repository, Tracked Branch와 Frontend Binding은 역할에 따라 `PATCH`와 soft deactivate를
  제공하며 성공 뒤 현재 목록을 다시 읽습니다.
- Snapshot 목록은 revision, Snapshot/VSS 상태, reason과 attempt 수를 표시하고 상세 화면은
  안전한 materialized locator와 전체 attempt 메타데이터를 표시합니다.
- `materialized` Snapshot은 operator 이상에게 **Index** 액션을 노출합니다. Index 클릭 전에는 VSS Job을 만들지 않습니다.
- Index 액션은 Browser가 `project_root`나 `remote`를 보내지 않고 snapshot ID만 보내며 Backend가 immutable locator를 검증해 VSS `/index` body를 생성합니다.
- Snapshot `failed|rejected|aborted`는 operator 이상에게 동일 Snapshot retry를 노출합니다.
- Top bar의 Ollama runtime control은 Browser가 Ollama에 직접 접근하지 않고 Backend만 사용합니다. `GET /v1/admin/runtime/models`가 `/api/tags` 설치 모델과 `/api/ps` resident 모델을 비교해 `Running`/`Stopped`와 `auto_up_models`를 반환합니다. operator 이상은 선택 모델에 **Up / Down / Reload / Auto Up**을 수행할 수 있습니다. Up은 모델 capability를 확인해 completion 모델은 `/api/generate`, embedding-only 모델은 `/api/embed`로 `keep_alive=-1` preload하며, Down은 Ollama unload 계약인 `keep_alive=0`을 사용하고 해당 모델의 Auto Up을 먼저 해제합니다. Reload는 Auto Up 정책을 유지합니다. Auto Up은 Browser polling과 독립된 Backend lifecycle task가 주기적으로 resident 상태를 확인해 설정 모델이 내려가면 다시 Up합니다. 현재 Auto Up 선택은 Backend process-local 정책이므로 Backend 재시작 시 초기화되며, 영속 정책이 필요하면 별도 저장 계약을 추가해야 합니다. 모든 수동 mutation은 operator RBAC/CSRF/BFF allowlist를 통과하고 Audit Log에 기록합니다. Ollama가 lifecycle mutation을 non-200으로 거부하면 Admin API에는 일반화된 오류만 노출하고, 서버 warning log에는 upstream HTTP status와 제한·redaction된 JSON 오류 사유만 기록합니다. 응답 body 전체는 로그에 남기지 않습니다.
- 실패 화면은 구조화된 `reason`, `detail`, `retryable`, `request_id`를 보존하고 binding
  누락·중복 reason이면 Binding 화면으로 이동할 수 있습니다.

## Branch binding

```json
{
  "binding_id": "11111111-1111-4111-8111-111111111111",
  "frontend_project_id": "h5vision/vision",
  "frontend_workspace_name": "vision",
  "repository_id": "55555555-5555-4555-8555-555555555555",
  "branch_ref": "refs/heads/module",
  "vss_project_id": "vss-server--module",
  "active": true
}
```

- `repository_id`는 Backend UUID입니다.
- `frontend_workspace_name`은 Sidebar briefing/status exact 조회 키이며 선택값입니다.
- `branch_ref` 정본은 `refs/heads/...` full ref입니다.
- `vss_project_id`는 exact 문자열이며 유사 이름을 자동 선택하지 않습니다.
- 현재 Frontend payload에 branch가 없으므로 Frontend project당 활성 binding 하나만
  허용합니다.
- 독립 Branch는 서로 다른 active index가 필요하므로 별도 `vss_project_id`가 원칙입니다.
- binding 변경은 이후 Snapshot에만 적용합니다.

## Snapshot 표시 모델

Admin 목록·상세는 최소한 다음을 표시합니다.

```text
repository / branch
base_revision / target_revision
snapshot state
materialization state
VSS state
성공·실패 reason과 detail
retryable
attempt count
created/updated time
```

서버의 전체 `materialized_project_root`는 노출하지 않고 안전한 locator 또는 revision만
표시합니다. `vss_result_json`도 allowlist된 비밀정보 없는 필드만 전달합니다.

## 화면 상태

| UI 상태 | 판단 | 표시/동작 |
|---|---|---|
| `loading` | 조회 중 | 중복 mutation 금지 |
| `empty` | 결과 없음 | 등록 또는 필터 안내 |
| `ready` | 조회 성공 | 권한별 동작 활성화 |
| `binding_required` | `SNAPSHOT_DESTINATION_REQUIRED` | binding 설정 이동 |
| `binding_ambiguous` | `SNAPSHOT_DESTINATION_AMBIGUOUS` | 활성 binding 정리 |
| `materializing` | Snapshot materializing | 전체 tree 준비 중 표시 |
| `indexing` | accepted/running/indexing_lexical/promoting | 완료가 아님을 표시 |
| `completed` | done + exact target commit | 성공 이유·완료 revision 표시 |
| `failed` | materialization/VSS/revision 실패 | reason/detail/retryable 표시 |
| `aborted` | VSS aborted | 상태 확인 후 재시도 안내 |
| `unauthenticated` | `401` | 로그인 이동 |
| `forbidden` | `403` | 권한 부족, mutation 금지 |
| `unavailable` | `500/503` | request ID와 재시도 가능 여부 표시 |
| `retrying` | retry 접수 중 | 같은 Snapshot 중복 클릭 금지 |

HTTP status만으로 문구를 추측하지 않고 JSON `reason`, `detail`, `retryable`을 사용합니다.
`X-Request-ID`와 body `request_id`를 장애 문의와 감사 화면에 표시합니다.

## Snapshot 변경 제한

- Repository sync의 정본은 remote HEAD 관측과 catalog 갱신입니다. Snapshot materialize와 VSS Index는 별도 operator action으로 분리합니다.
- VS Code `/v1/workspace-overlays`는 기존 구현 호환 경계이며 신규 수집 정본이 아닙니다.
- Admin은 revision, 파일 본문과 materialized tree를 수정하지 않습니다.
- Retry는 같은 `snapshot_id`와 materialized target을 사용하고 attempt만 증가시킵니다.
- retry 전 VSS active commit과 Job 상태를 다시 확인합니다.
- Snapshot/staging/revision 삭제는 retention 확정 전 제공하지 않습니다.
- Repository/Binding DELETE는 초기 `active=false`입니다.
- `force=true`를 단순 UI checkbox로 노출하지 않습니다.

## 보안·감사

- Repository/Binding mutation, retry, deactivate를 감사 기록합니다.
- 관리자 ID, request ID, 대상 ID, 이전/새 값, 시각과 결과를 남깁니다.
- 파일 content, DSN, VSS/Ollama/Git credential은 감사·API 응답에 넣지 않습니다.
- 허용된 Admin origin만 CORS에 등록합니다.
- 인증 만료는 `401`, 권한 부족은 `403`과 구조화된 이유를 반환합니다.

## 운영 공개 전 확인값

- Admin Web 배포 담당자와 운영 사용자 레지스트리 소유자
- 운영 URL, TLS와 허용 origin
- 운영 역할 할당과 30분 session 정책 승인
- Git provider credential과 branch catalog 접근
- 초기 Frontend/Repository/Branch/VSS project binding
- materialization locator 공개 범위
- retention과 재시도 권한
- Chat 상태를 Admin 범위에 포함할지 여부

이 값들은 schema/mock test를 막지는 않지만 production mutation 노출 전에 확정합니다.

## 동일 인스턴스 배포 결정과 착수 판정

```text
Browser               HTTP  http://<APPROVED-AWS-HOST>:4180
Independent Admin Web HTTP  0.0.0.0:4180 또는 승인된 private interface:4180
Snapshot Backend      HTTP  http://127.0.0.1:8000/v1/admin/*
```

Repository/Binding schema, PostgreSQL store와 audit 모델, Phase 3A-2 수집 코어에
`module/admin_web` BFF를 연결했습니다. 브라우저는 Backend loopback에 직접 접근하지 않아
Backend CORS 공개가 필요하지 않습니다. 로컬 구현 결정은 다음과 같습니다.

- Argon2 hash JSON 사용자와 최소 `viewer/operator/admin` 역할
- Admin Web 전용 service token과 body/path/query/actor/role/request ID HMAC
- 서명된 사용자 identity와 mutation 전후 값을 감사 로그에 기록
- 독립 Python package `module/admin_web/`, 고정 포트 `4180`
- `4180` 보안 그룹/VPN 허용 범위와 session cookie 정책
- 인터넷 공개가 필요할 때 ALB 등 HTTPS/TLS 종단

Nginx는 기본 배포 구성에 포함하지 않습니다. Admin service가 정적 UI와 BFF를 직접
제공하며 공개 HTTPS가 필요한 경우에만 AWS의 승인된 TLS 경계를 앞에 둡니다.

따라서 판정은 `Phase 3A-3 로컬 완료 / 운영 TLS·VPN·secret 확정 전 외부 공개 불가`입니다.
FastAPI에는 `/v1/admin/*`가 등록됐지만 서명된 BFF 요청이 아니면 fail closed합니다.
Ubuntu 22.04/24.04 독립 systemd 예제를 제공하며 실제 AWS 적용은 외부 검증 범위입니다.

## Phase 7B-2 Repository History와 Compare

현재 Admin의 Branch HEAD 이력은 sync 시점의 `previous/observed_head_sha`만 표시하고,
Repository의 전체 commit graph나 두 revision 비교는 제공하지 않습니다. Phase 7A-2 commit
catalog가 준비된 뒤 Repository 상세에 다음 view를 추가합니다.

```text
Branches
Commit history
Revision timeline
Compare
Change requests
```

Commit history는 `Git only | Materialized | VSS indexed | Unavailable` 상태를 구분하고,
Branch/Snapshot ??? cursor pagination? ?????. Compare? ? exact commit?
merge-base, ahead/behind, file status와 통계를 표시하되 기본 응답에 diff hunk나 파일 본문을
포함하지 않습니다. `Git only` commit의 Materialize와 Snapshot의 Index는 서로 다른 operator 명시적 동작이며
목록 조회나 비교만으로 자동 시작하지 않습니다.

상세 API/UI와 비용 경계의 정본은 `16_COMMIT_HISTORY_AND_COMPARISON.md`입니다.

## Phase 3A-4 Webhook 적용 판정

GitHub Webhook은 사용자 선택 Branch의 빠른 변경 알림으로만 사용합니다. Branch 선택,
HEAD 정본과 수동·정기 동기화를 대체하지 않습니다.

- endpoint는 `/webhooks/github`를 사용합니다.
- 공개 HTTPS와 `X-Hub-Signature-256` 검증 없이는 활성화하지 않습니다.
- `X-GitHub-Delivery`를 멱등 key로 보존합니다.
- `push`의 exact Repository와 `refs/heads/*`가 등록된 추적 Branch일 때만 queue에 넣습니다.
- 10초 안에 `202`를 반환하고 실제 fetch는 background worker가 수행합니다. Repository sync는 VSS 제출을 수행하지 않습니다.
- payload의 `after` SHA를 그대로 정본으로 쓰지 않고 Phase 3A-2 fetch로 재검증합니다.

현재 `repository_sync_runs.trigger`는 `manual|periodic`만 허용합니다. Webhook을 구현할 때
delivery 저장소, queue와 별도 migration을 함께 추가하며 단순 문자열 확장만으로 활성화하지
않습니다.
