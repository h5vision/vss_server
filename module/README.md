# Snapshot Backend module

이 디렉터리는 `vss_server/main`의 VSS 런타임과 섞이지 않는 독립 Snapshot Backend
모듈입니다. `vision/frontend`가 전송한 Git 변경을 검증하고, 완전한 revision
디렉터리로 materialize한 뒤 VSS 서버의 `POST /index`를 호출합니다.

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
```

필수 계약과 다음 페이즈는 `AGENTS.md` 및 `docs/agent/` 문서를 따릅니다.
AWS Ubuntu 24.04+ 호환 검증은 `docs/agent/10_UBUNTU_24_04_VALIDATION.md`를 따릅니다.
VSS 측 검증자는 `docs/agent/11_VSS_VALIDATOR_HANDOFF.md`를 단일 실행 진입점으로 사용합니다.
