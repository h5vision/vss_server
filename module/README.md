# Snapshot Backend module

이 디렉터리는 `vss_server/main`의 VSS 런타임과 섞이지 않는 독립 Snapshot Backend
모듈입니다. `vision/frontend`가 전송한 Git 변경을 검증하고, 향후 완전한 revision
디렉터리로 materialize한 뒤 설치된 `vss.indexer` 공개 API를 호출합니다.

현재 완료 범위는 Phase 0R, Phase 1, Phase 2R입니다. 실제
`POST /v1/workspace-overlays` 처리, PostgreSQL 영속화와 materialization은 이후
페이즈에서 연결합니다.

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

`module` 패키지는 `backend*`만 설치합니다. VSS는 `vss_server/main`의 exact SHA를
별도로 설치하거나 배포 환경에서 import 가능하게 제공해야 합니다. 현재 main에는 Python
packaging metadata가 없으므로 공급 방식이 확정될 때까지 VSS readiness는 차단됩니다.

## 개발 검증

```powershell
cd module
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m compileall -q backend
.venv\Scripts\python.exe -m ruff check backend tests
.venv\Scripts\python.exe -m pytest -q
```

필수 계약과 다음 페이즈는 `AGENTS.md` 및 `docs/agent/` 문서를 따릅니다.
