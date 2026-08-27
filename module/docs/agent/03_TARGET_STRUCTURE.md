# 목표 폴더 구조

## 설계 원칙

Frontend HTTP, Snapshot 영속화, 전체 트리 materialization, VSS Python module, Admin
API를 서로 다른 경계로 둡니다. VSS 내부 검색·인덱싱 코드를 복사하지 않습니다.

```text
VS Code Frontend ── /v1/workspace-overlays ──┐
                                             ├─ Snapshot Backend ── PostgreSQL(snapshot)
Admin Web Server ── /v1/admin/* ─────────────┘          │
                                                        ├─ revision directories
                                                        └─ vss Python module
                                                              └─ VSS Store(rag)
```

Admin Web은 별도 서버/컨테이너입니다. VSS는 HTTP upstream이 아니라 Backend 또는 전용
indexing worker에 설치되는 Python module입니다.

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
   │  │  ├─ snapshots/
   │  │  ├─ materialization/
   │  │  ├─ repositories/
   │  │  ├─ indexing/
   │  │  └─ admin/
   │  ├─ integrations/vss/
   │  └─ infrastructure/database/
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

`module/backend/integrations/rag_lab`과 HTTP client는 사용하지 않습니다.
`vss_server/main`의 `vss/`는 module 디렉터리에 복사하지 않고, exact SHA로 별도 설치된
Python package를 adapter가 lazy import합니다.

## 파일 책임

| 위치 | 책임 |
|---|---|
| `workspace_overlays/schemas.py` | Frontend request 정본 |
| `workspace_overlays/validation.py` | Git SHA와 안전한 상대경로 검증 |
| `workspace_overlays/mapper.py` | materialization 이후 VSS 내부 command 생성 |
| `snapshots/*` | Snapshot, delta, attempt, 상태 전이와 영속화 |
| `materialization/paths.py` | 전용 root 경계, revision 경로, traversal 차단 |
| `materialization/source.py` | base tree 제공자 추상화: DB/Object Store/Git worktree |
| `materialization/service.py` | staging 복사, delta 적용, immutable promote |
| `materialization/revision.py` | target Git HEAD 또는 explicit revision 지원 검증 |
| `integrations/vss/schemas.py` | VSS module 인자·반환·상태 정본 |
| `integrations/vss/adapter.py` | lazy import, signature 검사, module 호출 |
| `integrations/vss/health.py` | package SHA, Store와 runtime readiness |
| `indexing/service.py` | 제출·상태 동기화·멱등성 orchestration |
| `indexing/recovery.py` | 재시작 후 accepted/indexing 상태 복구 |
| `admin/*` | 관리 HTTP, 인증·권한, 구조화 오류 |

## 계약 분리

```text
WorkspaceOverlayRequest
    변경 목록을 표현하며 materialized 경로를 모름

Snapshot/materialization record
    base/target revision, delta, 완성 경로와 검증 결과를 보존

VssIndexCommand
    project_root, project_id, expected_revision, profile, snapshot_id

vss.indexer.start_index kwargs
    project_root, project_id, profile, blocking, force, on_done, extra_meta, store
```

`expected_revision`은 Backend 완료 검증 값이며 현행 VSS 인자로 위장하지 않습니다.
Frontend 파일 배열을 VSS request schema에 복사하는 mapper도 만들지 않습니다.

## 프로세스 배치

초기 선택지는 두 가지입니다.

1. FastAPI `--workers 1` 안에 VSS module 내장
2. Backend 전용 indexing worker 하나가 VSS module을 소유

여러 FastAPI worker가 각자 `JOBS`와 singleton Store를 만들게 하지 않습니다. 장기적으로
전용 worker가 유리하지만 queue/IPC가 구현되기 전에는 단일 worker가 안전 기본값입니다.

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

별도 저장소를 우선합니다. 같은 monorepo가 확정된 경우에만 최상위 `admin-web/`을 별도
빌드·배포 단위로 둡니다. React/TypeScript/Vite는 권장안이며 외부 계약은 Backend
OpenAPI와 fixture입니다.

## 피해야 할 구조

- 모든 기능을 `main.py`에 구현
- `utils.py`, `helpers.py`에 경로 삭제와 VSS 호출 혼합
- VSS 소스를 Backend package 안으로 복사
- HTTP `/index/update/files` compatibility layer 유지
- VSS module import를 설정 로딩보다 먼저 수행
- materialized revision 디렉터리를 재사용해 파일을 덮어쓰기
- 여러 worker에서 동일 Chroma/VSS project를 동시에 인덱싱
