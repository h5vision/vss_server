# 독립 Admin Web 인계 계약

최종 확인일: 2026-08-28 KST

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
Repository/Branch 수집 코어는 Phase 3A-2, 인증된 Admin mutation과 독립 Web은 Phase
3A-3에서 구현합니다. VSS source 조회는 Phase 2V에서 구현했습니다.

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

## 예정 관리 API

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
| `GET` | `/v1/admin/vss/projects` | VSS exact project catalog | 3A-3 |
| `GET` | `/v1/admin/snapshots` | Branch별 SHA/Snapshot 이력 | 3A-3 |
| `GET` | `/v1/admin/snapshots/{snapshot_id}` | 상세·attempt | 3A-3 |
| `POST` | `/v1/admin/snapshots/{snapshot_id}/retry` | 동일 Snapshot 재시도 | 5 |

Branch에는 `/`가 포함되므로 `branch_ref` query parameter를 사용합니다. 목록은 opaque
cursor 기반이며 UI가 cursor 내부 형식을 해석하지 않습니다.

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

- Snapshot 생성의 정본은 추적 Branch fetch에서 새 remote HEAD를 발견했을 때 시작합니다.
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

## Admin 구현 전 확인값

- Admin Web 저장소와 배포 담당자
- 개발·운영 URL, TLS와 CORS origin
- IdP, 역할, session 만료 정책
- Repository branch catalog 제공 방식
- 초기 Frontend/Repository/Branch/VSS project binding
- materialization locator 공개 범위
- retention과 재시도 권한
- Chat 상태를 Admin 범위에 포함할지 여부

이 값들은 schema/mock test를 막지는 않지만 production mutation 노출 전에 확정합니다.

## 동일 인스턴스 배포 결정과 착수 판정

```text
Browser               HTTPS https://<AWS-REVERSE-PROXY>/admin
Independent Admin Web HTTP  http://127.0.0.1:<ADMIN-PORT>
Snapshot Backend      HTTP  http://127.0.0.1:8000/v1/admin/*
```

Repository/Binding schema, PostgreSQL store와 audit 모델은 준비됐습니다. Phase 3A-2에서
수집 코어를 구현한 뒤 Phase 3A-3 Admin Web을 BFF로 두면 브라우저가 Backend loopback에 직접
접근하지 않으므로 Backend CORS 공개가 필요하지 않습니다. 다만 다음 항목은 route 공개 전
확정해야 합니다.

- Browser 로그인 방식과 최소 `viewer/operator/admin` 역할
- Admin Web server가 Backend에 제시할 service credential
- 감사 로그에 기록할 사용자 identity의 전달·서명 방식
- Admin Web 저장소 또는 `module/admin-web/` 사용 여부
- reverse proxy의 `/admin` HTTPS와 session cookie 정책

따라서 판정은 `Phase 3A-2 수집 코어 착수 가능 / Phase 3A-3 인증 결정 전 외부 공개 불가`입니다.

현재 FastAPI에는 위 예정 관리 route가 아직 등록되지 않았습니다. 내부 저장소나 Phase 5
재시도 서비스가 있다는 이유로 Admin API가 사용 가능하다고 판단하지 않으며 인증/RBAC
없이 mutation을 노출하지 않습니다. Phase 4에서 Snapshot/delta/attempt 저장은 실제
overlay route에, Phase 5에서 상태 동기화는 Frontend 조회 route에 연결됐지만 이력 조회와
수동 재시도는 인증·공개 범위 결정 전에는 Admin route로 노출하지 않습니다.
