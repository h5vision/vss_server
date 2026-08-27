# 목표 폴더 구조

## 설계 원칙

Frontend HTTP, Snapshot 영속화, 전체 트리 materialization, VSS HTTP API, Admin
API를 서로 다른 경계로 둡니다. VSS 내부 검색·인덱싱 코드를 복사하지 않습니다.

```text
VS Code Frontend ── /v1/workspace-overlays ──┐
                                             ├─ Snapshot Backend ── PostgreSQL(snapshot)
Admin Web Server ── /v1/admin/* ─────────────┘          │
                                                        ├─ revision directories
                                                        └─ POST /index, GET /index/status
                                                                   ↓
                                                              VSS Server · Store(rag)
```

Admin Web과 VSS는 별도 서버/프로세스입니다. Snapshot Backend는 VSS 내부 package나
Store를 직접 소유하지 않고 HTTP API만 호출합니다.

## 현재 구조 상태

현재 존재하는 구현은 다음 범위입니다.

```text
backend/core/                              Phase 1 완료
backend/features/workspace_overlays/       Phase 4 실제 route·orchestration 완료
backend/features/materialization/          Phase 4 Git source·경로·revision gate 완료
backend/features/snapshots/store.py        Phase 4 Snapshot/delta/attempt 저장 완료
backend/features/indexing/                 Phase 5 상태 동기화·복구·내부 재시도 완료
backend/integrations/vss/                  Phase 2H HTTP client 완료
backend/infrastructure/database/           Phase 3A-1 ORM·engine·session 완료
backend/features/repositories/store.py     Phase 3A-1 내부 저장소 완료
alembic/versions/0001*, 0002*, 0003*       Phase 3A-1/3B-1 migration 완료
backend/features/frontend_proxy/           Phase 3B-1 조회 proxy 완료
backend/features/health/                   Phase 3B-1 DB/VSS readiness 완료
```

아래 목표 구조 중 Admin router와 독립 Admin Web은 아직 존재하지 않습니다. 최초 제출
orchestration은 `workspace_overlays/service.py`, 상태 동기화·복구·재시도는 `indexing/`이
소유합니다. 실제 PostgreSQL migration과 shared path E2E는 외부 입력을 기다립니다.

## 목표 구조

```text
vss_server/
├─ vss/                       # main 소유 RAG runtime, module에서 수정 금지
├─ README.md                  # main 소유 문서, module에서 수정 금지
├─ CHARTER.md                 # main 소유 문서, module에서 수정 금지
└─ module/                    # Snapshot Backend 변경분 전용 경로
   ├─ AGENTS.md
   ├─ backend/
   │  ├─ app.py
   │  ├─ core/
   │  ├─ features/
   │  │  ├─ health/
   │  │  ├─ workspace_overlays/
   │  │  ├─ frontend_proxy/
   │  │  ├─ snapshots/
   │  │  ├─ materialization/
   │  │  ├─ repositories/
   │  │  ├─ indexing/
   │  │  └─ admin/
   │  ├─ integrations/vss/
   │  └─ infrastructure/database/
   ├─ alembic/
   │  └─ versions/
   ├─ alembic.ini
   ├─ tests/
   │  ├─ fixtures/{frontend,vss,admin}/
   │  ├─ contract/
   │  ├─ unit/
   │  └─ integration/
   ├─ docs/agent/
   ├─ main.py
   ├─ pyproject.toml
   └─ README.md
```

`module/backend/integrations/rag_lab`과 과거 `/index/update/files` client는 사용하지
않습니다. Snapshot 제출용 `integrations/vss/`는 최신 `vss_server/main`의 `/index`,
`/index/status`, `/index/exists`, `/projects`, `/health` HTTP 계약을 구현합니다. Frontend
조회 호환에 필요한 `/briefing`, `/v1/models`는 Phase 3B-1 proxy 범위에서 별도로 추가합니다.

## 파일 책임

| 위치 | 책임 |
|---|---|
| `workspace_overlays/schemas.py` | Frontend request와 접수 response 정본 |
| `workspace_overlays/validation.py` | Git SHA와 안전한 상대경로 검증 |
| `workspace_overlays/mapper.py` | materialization 이후 VSS HTTP request 생성 |
| `snapshots/store.py` | Snapshot, delta, attempt와 제출 상태 영속화 |
| `repositories/store.py` | Repository/Binding 저장과 project/workspace exact active binding 해석 |
| `infrastructure/database/*` | async engine/session과 Snapshot ORM 6종 |
| `alembic/versions/*` | PostgreSQL `snapshot` schema migration |
| `materialization/paths.py` | 전용 root 경계, revision 경로, traversal 차단 |
| `materialization/source.py` | base tree Protocol, read-only Git clone, target tree/HEAD 검증 |
| `materialization/service.py` | staging 복사, delta 적용, immutable promote |
| `integrations/vss/schemas.py` | VSS HTTP request·response·상태 정본 |
| `integrations/vss/client.py` | auth, timeout, HTTP status/JSON 검증 |
| `features/health/service.py` | DB ping과 VSS `/health`, `/projects` runtime readiness |
| `frontend_proxy/*` | Frontend `/projects`, `/models`, `/briefing` 응답 변환·redaction |
| `indexing/service.py` | VSS 상태 동기화와 exact target 완료 판정 |
| `indexing/recovery.py` | 재시작 후 accepted/indexing 상태 복구 |
| `indexing/retry.py` | immutable tree 재검증 후 동일 Snapshot 내부 재시도 |
| `admin/*` | 관리 HTTP, 인증·권한, 구조화 오류 |

## 계약 분리

```text
WorkspaceOverlayRequest
    변경 목록을 표현하며 materialized 경로를 모름

Snapshot/materialization record
    base/target revision, delta, 완성 경로와 검증 결과를 보존

VssIndexRequest
    project_root, project_id, profile, force, briefing, note

Backend completion expectation
    snapshot_id, expected_revision은 DB에만 보존하고 VSS HTTP 필드로 위장하지 않음
```

`expected_revision`은 Backend 완료 검증 값이며 현행 VSS 인자로 위장하지 않습니다.
Frontend 파일 배열을 VSS request schema에 복사하는 mapper도 만들지 않습니다.

## 서비스·filesystem 배치

Backend와 VSS는 HTTP로 분리합니다. Backend worker 수와 무관하게 동일 target 제출은
DB unique constraint로 한 번만 허용합니다. materialized `project_root`는 VSS 서버에서
같은 경로로 읽을 수 있어야 하므로 shared volume/mount 또는 VSS-local materialization
전달 방식이 필요합니다. VSS의 Chroma 단일 프로세스 제약은 VSS 서버 배포가 소유합니다.

## materialization 저장 구조

```text
SNAPSHOT_MATERIALIZATION_ROOT/
└─ <safe-vss-project-key>/
   ├─ staging/
   │  └─ <snapshot-id>/
   └─ revisions/
      └─ <40-char-target-sha>/
```

- 경로 구성에 raw `project_id`를 직접 사용하지 않고 DB 내부 안전 key를 사용합니다.
- staging은 성공 전 사용자에게 노출하지 않습니다.
- revision 디렉터리는 생성 후 변경하지 않습니다.
- 정리 대상은 resolve한 절대경로가 전용 root 내부인지 확인한 뒤에만 처리합니다.
- retention 확정 전 자동 삭제하지 않습니다.

## Admin Web 위치

별도 저장소를 우선합니다. 같은 monorepo가 확정된 경우에만 `module/admin-web/`을 별도
빌드·배포 단위로 둡니다. React/TypeScript/Vite는 권장안이며 외부 계약은 Backend
OpenAPI와 fixture입니다.

## 피해야 할 구조

- 모든 기능을 `main.py`에 구현
- `utils.py`, `helpers.py`에 경로 삭제와 VSS 호출 혼합
- VSS 소스를 Backend package 안으로 복사
- Frontend delta를 VSS `/index/update/files`로 그대로 전달하는 compatibility layer 유지
- Backend에서 `vss.indexer`를 직접 import하거나 Store에 직접 접근
- materialized revision 디렉터리를 재사용해 파일을 덮어쓰기
- VSS 서버에서 읽을 수 없는 Backend 로컬 경로를 `POST /index`에 전달
