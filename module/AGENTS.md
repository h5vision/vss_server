# module Agent Guide

이 문서는 `module`에서 Snapshot/VSS 통합을 구현하는 Agent의 필수 진입점입니다.

## 작업 범위

- 구현 저장소: `https://github.com/h5vision/vss_server.git`
- 구현 브랜치: `module`
- 현재 통합 기준: `https://github.com/h5vision/vss_server.git`의 `main`
- VSS 기준 SHA: `aa6aa3e77679e2fb319d2009cfd7726c6ae723be`
- Frontend 참조: `https://github.com/h5vision/vision.git`의 `frontend`
- 역할: Frontend의 Git 변경을 보존하고 완전한 revision 디렉터리를 만든 뒤 VSS HTTP
  API에 인덱싱을 제출하며, 독립 Admin Web을 위한 Repository/Branch별 이력을 관리

## 필수 읽기 순서

1. `docs/agent/01_REFERENCE_REPOSITORIES.md`
2. `docs/agent/02_EXTERNAL_CONTRACTS.md`
3. `docs/agent/03_TARGET_STRUCTURE.md`
4. `docs/agent/04_REQUIRED_FEATURES.md`
5. `docs/agent/05_IMPLEMENTATION_PLAN.md`
6. `docs/agent/06_READINESS_AND_VERIFICATION.md`
7. `docs/agent/07_ADMIN_WEB_HANDOFF.md`

## 규약 권위

1. Frontend `frontend` 브랜치의 실제 TypeScript 요청 코드
2. 이 저장소의 `CHARTER.md`, `docs/API.md`, `vss/indexer.py`, Store 구현과 테스트
3. 이 저장소의 문서, schema와 테스트

상대 코드가 문서와 다르면 실제 코드를 다시 확인하고 기준 SHA와 계약을 갱신합니다.
과거 `vision/model`의 `/index/update/files` 증분 HTTP 계약은 더 이상 권위가 없습니다.

## 변경 금지 원칙

- Frontend 브랜치는 참조 전용입니다.
- Frontend와 `vss_server/main` 참조 코드는 읽기 전용입니다.
- Frontend에 `snapshot_id`, `content_sha256`, `size_bytes`, `branch`를 새 필수값으로
  요구하지 않습니다.
- `base_revision`, `target_revision`은 실제 40자리 Git commit SHA만 받습니다.
- 전달된 파일은 diff hunk가 아니라 변경 후 전체 문자열입니다.
- VSS는 변경 목록이 아니라 완성된 server-local 프로젝트 디렉터리를 인덱싱합니다.
- FastAPI에 VSS의 수집, 청킹, 임베딩, BM25, Chroma/pgvector, promotion을 복제하지
  않습니다.
- VSS `project_id`는 명시적 Repository/Branch binding으로 정하며 유사 문자열로
  추측하지 않습니다.
- 현행 `POST /index`가 받지 않는 `revision`, `snapshot_id` 필드를 지원되는 것처럼
  전송하지 않습니다.
- 완료는 `state=done`과 `index.commit=target_revision`을 함께 확인합니다.

## 구현 경계

```text
VS Code Frontend
    POST /v1/workspace-overlays
             ↓
Snapshot Backend package
    검증 → Snapshot 영속화 → 전체 revision 디렉터리 materialize
             ↓
    POST http://<VSS>:8200/index
             ↓
    GET  http://<VSS>:8200/index/status?project_id=...

독립 Admin Web Server
    /v1/admin/*
             ↓
Snapshot Backend package
    Repository/Branch/VSS project binding · 이력 · 재시도 · 감사
```

Snapshot ID는 Backend 내부 레코드 ID입니다. Git revision과 혼동하지 않습니다.
Frontend payload에는 branch가 없으므로 활성 binding 값을 수신 시점 Snapshot에 복사해
과거 이력을 고정합니다. 독립 Branch는 별도 `vss_project_id` 사용을 원칙으로 합니다.

## VSS HTTP 런타임 원칙

- Backend는 `vss_server/main`에서 실행되는 VSS HTTP 서버의 `POST /index`,
  `GET /index/status`, `GET /index/exists`, `GET /projects`, `GET /health`만 호출합니다.
- `VSS_TOKEN`이 설정된 서버에는 `X-VSS-Token` 또는 Bearer 인증을 사용합니다.
- HTTP `202` 접수는 완료가 아닙니다. `GET /index/status`를 동기화합니다.
- 인덱싱 실패 시 VSS가 이전 active index를 보존하는 경계를 침범하지 않습니다.
- `project_root`는 VSS 서버에서 읽을 수 있는 server-local/shared 경로여야 합니다.
- materialized root의 Git HEAD가 target revision이 아니면 완료 보장이 불가능하므로
  제출 전에 차단합니다. 로컬-only commit 지원에는 upstream의 명시적 revision 계약이
  필요합니다.

## 주소 경계

```text
Frontend Snapshot API 기본값  http://192.168.0.7/v1
VSS Snapshot API              http://<EC2>:8200
Frontend AI 진입점            http://127.0.0.1:11500
Windows portproxy 대상        http://192.168.0.12:11500
VSS 기본 Ollama URL           http://127.0.0.1:11434
```

`127.0.0.1:11500`은 Frontend 호스트의 portproxy 진입점입니다. Snapshot materialization
경로나 VSS 서버 주소로 사용하지 않으며 기존 AI 직접 호출은 별도 합의 전까지 수정하지
않습니다.

## 작업 안전

- 작업 전후 `git status -sb`, `git diff --check`를 확인합니다.
- Frontend 참조 checkout과 이 구현 worktree를 분리합니다.
- materialization 경로는 설정된 전용 root 아래인지 resolve 후 검사합니다.
- 재귀 삭제·이동 전 대상 절대경로가 전용 root 내부인지 재검증합니다.
- contract → unit → integration 순으로 테스트합니다.
- 비밀키, DSN, token, 파일 본문을 문서·fixture·로그에 저장하지 않습니다.
- commit/push는 사용자가 명시적으로 요청한 경우에만 수행합니다.
- commit/push 전후에 `git status -sb`, `git diff --check`를 확인합니다.
- `module` 브랜치의 변경분에 main 프로젝트 파일·문서·폴더 수정을 포함하지 않습니다.
- Snapshot Backend는 최상위 `module/` 경로 안에서만 구성하고 main 파일과 섞지 않습니다.
