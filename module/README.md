# Snapshot Backend module

이 디렉터리는 `vss_server/main`의 VSS 런타임과 섞이지 않는 독립 Snapshot Backend
모듈입니다. 사용자가 선택한 Repository/Branch의 commit SHA를 수집하고, 완전한 revision
디렉터리로 materialize한 뒤 VSS 서버의 `POST /index`를 호출하는 것이 목표 경계입니다.
기존 `vision/frontend` overlay route는 현재 구현 호환 경계로 유지합니다.

현재 완료 범위는 Phase 0R, Phase 1 골격, Phase 2H HTTP 계약 전환, Phase 3A-1
PostgreSQL 영속화 기반, Phase 3B-1 로컬 런타임 연결과 Phase 4 핵심 제출 흐름입니다.
Phase 5의 exact revision 상태 동기화, startup 복구와 동일 Snapshot 내부 재시도도 로컬
완료했습니다.
VSS 연동은
`VSS_BASE_URL` 기반 HTTP client와 exact request/response schema를 사용하며 Python
direct-import adapter와 VSS 내부 설정 소유권은 제거했습니다. PostgreSQL `snapshot`
schema의 ORM·Alembic migration과 Repository/Branch binding 저장소가 준비됐고, app
lifespan/readiness, Frontend용 `/v1/projects`·`/v1/models`·`/v1/briefing` 조회 proxy와
실제 `POST /v1/workspace-overlays`를 연결했습니다. Overlay는 DB에 먼저 저장하고 Git
base tree에 적용한 뒤 target tree/HEAD가 정확할 때만 immutable 경로로 승격하여 VSS에
제출합니다. `/v1/index/status`는 VSS `done`만으로 완료 처리하지 않고
`index.commit == target_revision`까지 확인합니다. 운영 DB/VSS/shared path 검증과 Admin
인증 API는 이후 페이즈에서 연결합니다.

VSS는 인증된 `GET /v1/internal/vss/source`와 `/v1/internal/vss/revisions`로 최신/특정
Snapshot의 commit SHA, Git tree SHA, clean working tree 증거, server-local
`project_root`와 exact `/index` body를 조회할 수 있습니다. inbound
`SNAPSHOT_VSS_API_TOKEN`은 outbound `VSS_TOKEN`과 분리하며 자세한 계약은
`docs/agent/13_VSS_SOURCE_API.md`를 따릅니다.

## 디렉터리 경계

```text
vss_server/
├─ vss/                 # main 소유, 이 모듈에서 수정하지 않음
└─ module/              # Snapshot Backend 변경분 전용
   ├─ backend/
   ├─ docs/agent/
   ├─ tests/
   ├─ AGENTS.md
   ├─ main.py
   └─ pyproject.toml
```

이 프로젝트는 내부 Python package인 `backend*`만 설치합니다. VSS는 별도 서버로
배포하고 `VSS_BASE_URL`과 선택적 `VSS_TOKEN`으로 연결합니다. materialized
`project_root`는 VSS 서버 프로세스에서도 같은 경로로 읽을 수 있어야 합니다.

## 개발 검증

```powershell
cd module
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m compileall -q backend alembic tests scripts
.venv\Scripts\python.exe -m ruff check backend tests alembic scripts
.venv\Scripts\python.exe -m pytest -q
# Docker가 준비된 개발 환경에서 실제 PostgreSQL 17 migration·동시성 검증
.venv\Scripts\python.exe scripts\verify_postgresql_17.py
```

필수 계약과 다음 페이즈는 `AGENTS.md` 및 `docs/agent/` 문서를 따릅니다.
AWS Ubuntu 24.04+ 호환 검증은 `docs/agent/10_UBUNTU_24_04_VALIDATION.md`를 따릅니다.
VSS 측 검증자는 `docs/agent/11_VSS_VALIDATOR_HANDOFF.md`를 단일 실행 진입점으로 사용합니다.
실제 PostgreSQL 로컬 검증의 범위와 운영 미검증 경계는
`docs/agent/12_POSTGRESQL_RUNTIME_VALIDATION.md`를 따릅니다.

## 동일 AWS 인스턴스 주소 경계

일반 Linux service로 함께 실행하는 Snapshot Backend, VSS와 PostgreSQL은 각각
`127.0.0.1:8000`, `127.0.0.1:8200`, `127.0.0.1:5432`를 사용합니다. Backend는 외부
인터페이스에 직접 bind하지 않으며 Frontend와 Admin Browser는 HTTPS reverse proxy를
통해 접근합니다. 외부 클라이언트의 `127.0.0.1`은 해당 클라이언트 자신이므로 AWS 서버
주소로 사용하지 않습니다.
