# Vision Snapshot Backend Module

> **"VSS 서버에 신뢰할 수 있는 Git 소스코드를 정확하고 안전하게 공급하는 백엔드 엔진"**

이 디렉터리는 `vss_server/main`의 VSS(Vector Search Server) 런타임과 소스코드가 섞이지 않는 **독립 Snapshot Backend 모듈**입니다.  
원격 Git 저장소의 브랜치를 추적(Fetch)하여 최신 Commit SHA를 수집하고, 전체 소스 트리를 불변(Immutable) 디렉터리로 Materialize한 후 VSS 서버의 인덱싱(`POST /index`)에 공급하는 역할을 담당합니다.

---

## 🎯 1. 시스템 설계 철학 (Architectural Principles)

본 시스템은 **"누가 보아도 쉽게 이해하고 안전하게 유지보수할 수 있는 엔터프라이즈급 아키텍처"**를 목표로 설계되었습니다:

1. **유지보수성 (방 나누기와 관심사 분리)**:
   * 5대 계층(인프라, 도메인, DB 영속화, VSS 연동, 공통 코어)이 완전히 분리되어 있어 한쪽을 수정해도 다른 쪽에 부수 효과(Side-effect)가 발생하지 않습니다.
2. **생산성 유지와 일관성 (레고 블록 구조)**:
   * 모든 입출력에 strict Pydantic 검증(`extra="forbid"`)과 표준화된 `ApiError` 응답을 적용하여 새로운 기능 확장이 용이합니다.
3. **무중단 안정성과 견고함 (Fail-Closed & 멱등성)**:
   * `(vss_project_id, target_revision)` DB 유니크 제약과 `os.replace` 원자적(Atomic) 파일 승격으로 네트워크 지연 및 장애 상황에서도 데이터 무결성을 보장합니다.
4. **자동화 테스트 기반 품질 보증 (123+ Automated Tests)**:
   * 15초 이내에 계약/단위/통합 테스트를 완벽히 통과하는 테스트 스위트를 구비하여 부실공사를 원천 차단합니다.

---

## 🏛️ 2. 시스템 아키텍처 다이어그램 (System Architecture)

### 2.1 시스템 배치도 (System Topology - Mermaid)

```mermaid
flowchart TB
    subgraph External["외부 클라이언트 및 원격 저장소"]
        GitRemote["Git Remote Repositories\n(GitHub / GitLab / etc.)"]
        Frontend["VS Code Frontend\n(개발자 클라이언트)"]
        AdminWeb["Admin Web / Browser\n(:4180)"]
    end

    subgraph AWSInstance["AWS EC2 단일 인스턴스 (Ubuntu 22.04 / 24.04)"]
        subgraph SnapshotBackend["Snapshot Backend Module (:8000)"]
            direction TB
            API["FastAPI App\n(/v1/workspace-overlays, /v1/health)"]
            Collector["Collection Engine\n(git ls-remote, TrackedBranch Sync)"]
            Materializer["Materialization Engine\n(Git Tree Checkout & Atomic Promote)"]
            VSSClient["VssHttpClient\n(httpx2 기반 비동기 통신)"]
            DescriptorAPI["VSS Source Descriptor API\n(/v1/internal/vss/source)"]
        end

        subgraph VSSServer["VSS Server (:8200)"]
            direction TB
            VSSIndex["VSS Indexer Engine\n(POST /index, GET /index/status)"]
            VSSChat["VSS Chat/RAG Engine\n(POST /v1/chat, /prompt)"]
            Ollama["Ollama Embed/Chat (:11434)\n(bge-m3, qwen2.5-coder)"]
        end

        subgraph Storage["로컬 저장소 및 데이터베이스"]
            PostgreSQL[("PostgreSQL (:5432)\n- snapshot schema (Backend)\n- rag schema (VSS)")]
            SharedDisk[("공유 파일시스템\n/home/ubuntu/vss-snapshots/\n(Immutable Git Revision Trees)")]
        end
    end

    %% External Connections
    GitRemote <-->|"git ls-remote / fetch"| Collector
    Frontend -->|"POST /v1/workspace-overlays"| API
    Frontend -->|"POST /v1/chat"| VSSChat
    AdminWeb -->|"HTTP / REST API (:4180)"| API

    %% Internal Module Flow
    API --> Materializer
    Collector --> Materializer
    Materializer -->|"불변 디렉터리 생성"| SharedDisk
    SnapshotBackend <-->|"SQLAlchemy / asyncpg"| PostgreSQL
    VSSClient -->|"POST /index\nGET /index/status"| VSSIndex
    DescriptorAPI <-->|"검증된 SHA/경로 조회"| VSSIndex

    %% VSS Internal Flow
    VSSIndex -->|"동일 경로 읽기"| SharedDisk
    VSSIndex <--> Ollama
    VSSChat <--> PostgreSQL
```

---

### 2.2 패키지 및 컴포넌트 구조도 (PlantUML Component Architecture)

```plantuml
@startuml
skinparam componentStyle rectangle
skinparam packageStyle rectangle
skinparam shadowing false

package "module.backend" {

    package "core" {
        class Settings << (C,#ADD1B2) >> {
            + database_url: SecretStr
            + vss_base_url: str
            + vss_token: SecretStr
            + snapshot_materialization_root: Path
        }
        class ApiError << (E,#FF7777) >> {
            + reason: str
            + detail: str
            + retryable: bool
            + request_id: UUID
        }
    }

    package "infrastructure.database" {
        class DatabaseEngine {
            + create_engine_from_url()
            + get_db_session()
        }
        package "models" {
            entity Repository
            entity TrackedBranch
            entity BranchHeadHistory
            entity RepositorySyncRun
            entity Snapshot
            entity SnapshotDelta
            entity SnapshotAttempt
            entity AuditLog
        }
    }

    package "features.collection" {
        class GitRemoteClient {
            + list_remote_heads(remote_url: str) : dict[str, str]
        }
        class CollectionMaterializer {
            + materialize_revision(repo_url: str, commit_sha: str, dest_dir: Path) : Path
        }
        class CollectionSyncService {
            + sync_repository(repo_id: UUID) : SyncResult
            + sync_all() : list[SyncResult]
            - _observe_branch_heads()
            - _queue_snapshot()
            - _materialize_and_submit()
        }
        class CollectionRouter << (R,#LightBlue) >> {
            + POST /v1/collection/repositories/{id}/sync
            + POST /v1/collection/repositories/{id}/track
            + GET /v1/collection/repositories/{id}/history
        }
    }

    package "features.materialization" {
        interface TreeSource {
            + open_tree()
            + read_blob()
        }
        class Materializer {
            + promote_clean_tree()
            + materialize_overlay()
        }
    }

    package "features.snapshots" {
        class SnapshotStore {
            + create_snapshot()
            + transition_state()
        }
        class DescriptorRouter << (R,#LightBlue) >> {
            + GET /v1/internal/vss/source
            + GET /v1/internal/vss/revisions
        }
    }

    package "integrations.vss" {
        class VssHttpClient {
            + start_index(req: VssIndexRequest) : VssStartIndexResponse
            + get_index_status(project_id: str) : VssIndexStatusResponse
            + get_health() : VssHealthResponse
            + get_projects() : list[str]
        }
    }
}

' Relationships
CollectionRouter --> CollectionSyncService
CollectionSyncService --> GitRemoteClient
CollectionSyncService --> CollectionMaterializer
CollectionSyncService --> SnapshotStore
CollectionSyncService --> VssHttpClient
CollectionSyncService --> DatabaseEngine : uses session
DescriptorRouter --> SnapshotStore
SnapshotStore --> models
Materializer ..|> TreeSource
VssHttpClient --> Settings : reads config

@enduml
```

---

## ⚡ 3. 함수 아키텍처 및 상세 실행 플로우 (UML Sequence & Flow)

### 3.1 원격 브랜치 수집 ➔ 디스크 승격 ➔ VSS 인덱싱 플로우 (PlantUML Sequence)

```plantuml
@startuml
autonumber
actor "Scheduler / Admin" as Caller
participant "CollectionRouter" as Router
participant "CollectionSyncService" as SyncSvc
participant "GitRemoteClient" as GitClient
participant "PostgreSQL\n(snapshot schema)" as DB
participant "CollectionMaterializer" as Materializer
participant "VssHttpClient" as VssClient
participant "VSS Server (:8200)" as VSS

Caller -> Router : POST /v1/collection/repositories/{id}/sync
activate Router

Router -> SyncSvc : sync_repository(repo_id, session)
activate SyncSvc

SyncSvc -> DB : SELECT * FROM tracked_branches WHERE repo_id = :id AND active = true
DB --> SyncSvc : list[TrackedBranch]

SyncSvc -> GitClient : list_remote_heads(repo.remote_url)
activate GitClient
GitClient -> GitClient : run 'git ls-remote --heads <url>'
GitClient --> SyncSvc : dict { "refs/heads/main": "a1b2c3d..." }
delete GitClient

loop 각 추적 브랜치별 HEAD 변경 대조
    alt 새 Commit SHA 발견 (Head Changed)
        SyncSvc -> DB : INSERT INTO branch_head_history\n(previous_sha, observed_sha, change_type)
        SyncSvc -> DB : INSERT INTO snapshots\n(vss_project_id, target_revision, state='submitting')
        
        SyncSvc -> Materializer : materialize_revision(repo_url, commit_sha, dest_dir)
        activate Materializer
        Materializer -> Materializer : git archive / checkout to staging/
        Materializer -> Materializer : atomic promote via os.replace() to /revisions/<sha>
        Materializer --> SyncSvc : materialized_path
        delete Materializer

        SyncSvc -> VssClient : start_index(project_root, project_id)
        activate VssClient
        VssClient -> VSS : POST /index { "project_root": "...", "project_id": "..." }
        VSS --> VssClient : 202 Accepted { "job_id": "...", "status": "accepted" }
        VssClient --> SyncSvc : VssStartIndexResponse
        delete VssClient

        SyncSvc -> DB : INSERT INTO snapshot_attempts\n(attempt=1, upstream_status_code=202)
        SyncSvc -> DB : UPDATE snapshots SET state='indexing'
    else 동일 Commit SHA (No Change)
        SyncSvc -> SyncSvc : 멱등성 유지 (Skip snapshot & VSS call)
    end
end

SyncSvc -> DB : INSERT INTO repository_sync_runs (state='success', outcomes)
SyncSvc --> Router : SyncResponse { ok: true, sync_run_id: ... }
delete SyncSvc

Router --> Caller : 200 OK
delete Router

@enduml
```

---

### 3.2 VSS 소스 디스크립터 역조회 플로우 (PlantUML Sequence)

```plantuml
@startuml
autonumber
actor "VSS Query Engine" as VSS
participant "DescriptorRouter" as Router
participant "SnapshotStore" as Store
participant "PostgreSQL" as DB
participant "Shared Filesystem\n(/home/ubuntu/vss-snapshots)" as FS

VSS -> Router : GET /v1/internal/vss/source?project_id=...&revision=...
note right of VSS : 헤더: X-Snapshot-Token 검증

activate Router
Router -> Router : verify_inbound_token(token)

Router -> Store : get_snapshot_by_revision(project_id, revision)
activate Store
Store -> DB : SELECT * FROM snapshots WHERE vss_project_id = :id AND target_revision = :rev
DB --> Store : Snapshot record
delete Store

Router -> FS : verify_tree_integrity(materialized_path)
activate FS
FS -> FS : git rev-parse HEAD == target_revision
FS -> FS : check clean working tree
FS --> Router : Integrity Verified
delete FS

Router --> VSS : 200 OK {\n  "commit_sha": "a1b2c3d...",\n  "tree_sha": "e5f6g7h...",\n  "project_root": "/home/ubuntu/vss-snapshots/...",\n  "verified_at": "2026-08-31T10:00:00Z"\n}
delete Router

@enduml
```

---

### 3.3 수집 동기화 상태 전이 순서도 (PlantUML Activity Diagram)

```plantuml
@startuml
start
:수동 또는 정기 동기화 시작 (sync_repository);
:PostgreSQL에서 활성 추적 브랜치 목록 로드;

:git ls-remote --heads로 원격 저장소 브랜치 탐색;

if (원격 Git 연결 성공?) then (yes)
  while (각 추적 브랜치 순회)
    if (HEAD Commit SHA가 이전과 다른가?) then (yes)
      :branch_head_history에 새 SHA 변경 기록;
      :Snapshot 엔티티 생성 (state = 'submitting');
      :staging/ 디렉터리에 전체 파일 트리 체크아웃;
      :불변 디렉터리(/revisions/<sha>)로 원자적 승격 (os.replace);
      :VSS POST /index 호출;
      if (VSS 접수 성공? 202 Accepted) then (yes)
        :Snapshot 상태 'indexing'으로 갱신;
        :SnapshotAttempt 생성 (결과 기록);
      else (no)
        :Snapshot 상태 'failed'로 갱신;
        :에러 원인 및 retryable 플래그 기록;
      endif
    else (no)
      :동일 커밋: 멱등성 유지 (스냅샷/VSS 호출 건너뜀);
    endif
  endwhile
  :repository_sync_runs에 성공 로그 기록;
else (no)
  :네트워크/인증 오류 기록;
  :repository_sync_runs에 실패 로그 기록;
endif

stop
@enduml
```

---

## 📂 4. 레이어별 코드 구조 및 유지보수 가이드

```text
module/
├── backend/
│   ├── core/                      # [공통 인프라] 설정값(config.py), 표준 에러(errors.py)
│   ├── features/                  # [비즈니스 도메인 기능]
│   │   ├── collection/            # Git 원격 브랜치 탐색, 추적 브랜치 관리, 동기화 서비스
│   │   │   ├── git_client.py      # git ls-remote 탐색 클라이언트
│   │   │   ├── materializer.py    # 수집 소스 불변 디스크 승격 엔진
│   │   │   ├── service.py         # 브랜치 동기화 오케스트레이터 (CollectionSyncService)
│   │   │   ├── router.py          # 수집 제어 REST API 라우터
│   │   │   └── schemas.py         # Pydantic 요청/응답 스키마
│   │   ├── materialization/       # Git Tree 소스 복원, Staging 패치, 불변 디스크 승격
│   │   ├── snapshots/             # 스냅샷 상태머신, VSS Source Descriptor API
│   │   └── workspace_overlays/    # 프론트엔드 Delta 수신 및 보안 경로 검증
│   ├── infrastructure/
│   │   └── database/              # DB 연결(engine, session), ORM 모델(models/)
│   └── integrations/
│       └── vss/                   # VSS 서버 전용 HTTP 통신 클라이언트(client.py, schemas.py)
│
├── alembic/                       # PostgreSQL `snapshot` 스키마 마이그레이션 스크립트 (0001~0004)
├── docs/agent/                    # 세부 아키텍처 및 단계별 인계 문서 (01~14)
├── scripts/                       # AWS 운영 및 PostgreSQL 동시성 검증 스크립트
└── tests/                         # 자동화 테스트 스위트 (계약/단위/통합 - 123+ passed)
```

---

## 📚 5. 개발 시 상황별 문서 참조 가이드 (`docs/agent/`)

작업 목적에 따라 다음 정본 문서를 확인하세요:

| 내가 지금 하려는 작업 | 참고할 문서 |
|---|---|
| **전체 구현 로드맵 및 다음 단계 확인** | [`05_IMPLEMENTATION_PLAN.md`](file:///c:/Users/PC2412/Documents/HancomAI5/vision-backend-p/module/docs/agent/05_IMPLEMENTATION_PLAN.md) ⭐ *(구현 정본)* |
| **API 엔드포인트 경로 및 JSON 스키마 확인** | [`02_EXTERNAL_CONTRACTS.md`](file:///c:/Users/PC2412/Documents/HancomAI5/vision-backend-p/module/docs/agent/02_EXTERNAL_CONTRACTS.md) |
| **VSS에 제공하는 소스 디스크립터 규격 확인** | [`13_VSS_SOURCE_API.md`](file:///c:/Users/PC2412/Documents/HancomAI5/vision-backend-p/module/docs/agent/13_VSS_SOURCE_API.md) |
| **스냅샷 수명주기 및 DB 제약조건 규칙 확인** | [`04_REQUIRED_FEATURES.md`](file:///c:/Users/PC2412/Documents/HancomAI5/vision-backend-p/module/docs/agent/04_REQUIRED_FEATURES.md) |
| **코드베이스 구현 정합성 및 변경 이력 대조** | [`08_CODE_REVIEW_AND_CONFORMANCE.md`](file:///c:/Users/PC2412/Documents/HancomAI5/vision-backend-p/module/docs/agent/08_CODE_REVIEW_AND_CONFORMANCE.md) |
| **관리자 UI (:4180) 연동 규격 확인** | [`07_ADMIN_WEB_HANDOFF.md`](file:///c:/Users/PC2412/Documents/HancomAI5/vision-backend-p/module/docs/agent/07_ADMIN_WEB_HANDOFF.md) |
| **AWS 실서버(Ubuntu 22.04/Python 3.10) 배포 가이드** | [`14_UBUNTU_22_04_AWS_COMPATIBILITY.md`](file:///c:/Users/PC2412/Documents/HancomAI5/vision-backend-p/module/docs/agent/14_UBUNTU_22_04_AWS_COMPATIBILITY.md) |

---

## 🛡️ 6. 시스템을 고장 나지 않게 만드는 3대 안전장치

1. **DB 레벨 멱등성 (`Idempotency`)**:
   * `(vss_project_id, target_revision)` 유니크 제약조건이 걸려 있어, 동일한 커밋에 대한 중복 인덱싱 요청이 들어와도 2번째 요청은 DB 레벨에서 안전하게 튕겨냅니다.
2. **원자적 디스크 승격 (`Atomic Promotion`)**:
   * 소스 파일 복사 도중 서버가 꺼져도 깨진 파일이 VSS에 인덱싱되지 않도록, `staging/`에서 100% 검증이 끝난 후 `os.replace`로 한 번에 불변 경로(`revisions/<sha>`)로 이동시킵니다.
3. **독립 장애 격리 (`Fail-Closed & HTTP Boundary`)**:
   * 백엔드는 VSS 파이썬 모듈을 직접 `import`하지 않고 순수 HTTP로만 통신합니다. VSS가 죽어도 백엔드 프로세스는 뻗지 않고 `VssHttpUnavailable (retryable=True)`로 안전하게 상태를 기록합니다.

---

## 🌐 7. 네트워크 및 포트 맵핑 표

모든 내부 서비스는 AWS EC2 단일 인스턴스 내에서 **루프백(`127.0.0.1`)**을 통해 안전하게 통신합니다:

| 서비스 / 구성요소 | 바인드 주소 / 포트 | 프로토콜 | 설명 |
|---|---|:---:|---|
| **Snapshot Backend** | `127.0.0.1:8000` | HTTP | FastAPI 기반 Snapshot 및 수집 백엔드 |
| **VSS Server** | `127.0.0.1:8200` | HTTP | 벡터 검색 및 RAG 인덱서 서버 |
| **Admin Web / Proxy** | `0.0.0.0:4180` | HTTP(S) | 관리자 웹 인터페이스 / OAuth2-Proxy 포트 |
| **PostgreSQL** | `127.0.0.1:5432` | TCP | `snapshot` 스키마(Backend) & `rag` 스키마(VSS) |
| **Ollama Service** | `127.0.0.1:11434` | HTTP | 임베딩(`bge-m3`) 및 LLM 서빙 |

---

## 🛠️ 8. 개발 및 운영 명령어 (Quickstart & Runbook)

### 💻 로컬 개발 및 테스트
```powershell
cd module

# 1. 가상환경 및 의존성 설치
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 2. 코드 스타일 & 린터 검사 (100% Clean 유지)
.\.venv\Scripts\python.exe -m ruff check backend tests alembic scripts

# 3. 전체 자동화 테스트 실행 (123+ 테스트 약 15초 소요)
.\.venv\Scripts\python.exe -m pytest -q
```

### ☁️ AWS 실서버 서비스 관리 (Ubuntu Systemd)
```bash
# 서비스 상태 확인
sudo systemctl status vss-snapshot.service --no-pager -l

# 서비스 재시작 및 실시간 로그 확인
sudo systemctl restart vss-snapshot.service
sudo journalctl -u vss-snapshot.service -f

# 헬스체크 확인
curl -s http://127.0.0.1:8000/v1/health/ready
```
