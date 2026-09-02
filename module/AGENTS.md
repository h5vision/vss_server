# module Agent Guide

이 문서는 `module`에서 Snapshot/VSS 통합을 구현하는 Agent의 필수 진입점입니다.

## 작업 범위

- 구현 저장소: `https://github.com/h5vision/vss_server.git`
- 구현 브랜치: `module`
- 현재 VSS 참조 기준: `pre-rag`의 변경을 병합한 `test-merge`
- VSS `pre-rag` 기준 SHA: `d34bf1ce05bb3fd95cb89cecb35bf7df96e7b202`
- VSS `test-merge` 병합 SHA: `47b85faf01edc33184149b7364835bb4312d76b9`
- Frontend 참조: `https://github.com/h5vision/vision.git`의 `frontend`
- 역할: 사용자가 등록한 Repository와 추적 Branch의 commit SHA 이력을 보존하고 완전한
  revision 디렉터리를 만든 뒤 VSS HTTP API에 공급하며, VSS가 SHA·Git tree 정합성 증거를
  내부 API로 조회할 수 있게 함. 장기적으로는 Branch/Tag/PR/MR의 commit 관계를 보존하여
  VSS가 localhost에서 pull하고 사용자 질의에 사용할 revision과 답변 provenance를 판단할
  수 있는 Revision Context Provider 역할을 함

## 필수 읽기 순서

1. `docs/agent/01_REFERENCE_REPOSITORIES.md`
2. `docs/agent/02_EXTERNAL_CONTRACTS.md`
3. `docs/agent/03_TARGET_STRUCTURE.md`
4. `docs/agent/04_REQUIRED_FEATURES.md`
5. `docs/agent/05_IMPLEMENTATION_PLAN.md`
6. `docs/agent/06_READINESS_AND_VERIFICATION.md`
7. `docs/agent/07_ADMIN_WEB_HANDOFF.md`
8. `docs/agent/08_CODE_REVIEW_AND_CONFORMANCE.md`
9. `docs/agent/09_CURRENT_AND_NEXT_BRIEFING.md`
10. `docs/agent/10_UBUNTU_24_04_VALIDATION.md`
11. `docs/agent/11_VSS_VALIDATOR_HANDOFF.md`
12. `docs/agent/12_POSTGRESQL_RUNTIME_VALIDATION.md`
13. `docs/agent/13_VSS_SOURCE_API.md`
14. `docs/agent/14_UBUNTU_22_04_AWS_COMPATIBILITY.md`
15. `docs/agent/15_REVISION_CONTEXT_PROVIDER.md`

## 현재 구현 단계

2026-09-01 KST 현재 로컬 worktree 기준입니다.

```text
완료       Phase 0R, Phase 1, Phase 2H
로컬 완료  Phase 2V VSS source descriptor·revision 조회 API
로컬 완료  Phase 3A-1 PostgreSQL 영속화 기반
로컬 완료  Phase 3A-2 사용자 선택 Repository·Branch 수집 코어
로컬 완료  Phase 3A-3 포트 4180 Admin API·인증/RBAC·독립 Admin Web
로컬 완료  Phase 3B-1 VSS lifecycle/readiness와 Frontend 조회 proxy
로컬 완료  Phase 4 핵심 materialization과 /v1/workspace-overlays 제출
로컬 완료  Phase 5 상태 동기화·재시작 복구·내부 재시도 서비스
후속 검토  Phase 3A-4 GitHub Webhook
외부 대기  Phase 3B-2 실제 VSS 배포·shared path 검증
로컬 완료  Phase 6A-1 Ubuntu 24.04 로컬 장애·배포 사전 검증
로컬 완료  Phase 6A-2 AWS Ubuntu 22.04.5·Python 3.10 호환 검증
로컬 선행  Phase 6B PostgreSQL 17 migration·제약·재시도 및 복구 잠금 검증
외부 대기  Phase 6B AWS E2E — 실제 systemd·PostgreSQL·VSS 값 필요
다음 설계  Phase 7 PR/MR reference catalog·VSS revision context pull·답변 provenance
```

Phase 3A-1에는 ORM 6종, Alembic `0001`~`0003`, Repository/Binding 저장소와 DB
제약이 포함됩니다. Phase 3B-1에는 app lifespan의 DB/VSS dependency, 실제 DB ping과
VSS `/health`·`/projects` readiness, `/v1/projects`·`/v1/models`·`/v1/briefing`
조회 proxy가 포함됩니다. Phase 4 핵심에는 remote Git base tree, 안전한 overlay 적용,
target tree/HEAD 검증, immutable 승격, Snapshot/delta/attempt 영속화와 VSS 접수가
포함됩니다. Phase 5에는 `/v1/index/status`, exact revision 완료 판정, startup 상태 복구와
동일 Snapshot 내부 재시도가 포함됩니다. Phase 3A-2에는 Alembic `0004`와 legacy `0004`
배포 스키마를 보정하는 `0005`, 사용자 선택
`tracked_branches`, append-only `branch_head_history`, lease 기반 `repository_sync_runs`,
선택 ref 전용 bare cache와 collector-owned Snapshot/VSS 제출이 포함됩니다. Windows 기본
회귀 167개와 기존 Ubuntu 24.04 non-root
컨테이너, PostgreSQL offline DDL과 격리된 실제 PostgreSQL 17 migration·제약·row lock·
startup recovery advisory lock 및 Repository sync claim 5개 검증을 통과했습니다. 다만 운영 role/DSN,
shared-path VSS와 AWS E2E는 외부 입력
전까지 완료로 표시하지 않습니다.
실제 AWS host는 Ubuntu 22.04.5, system/venv Python은 모두 3.10.12, Git은 2.34.1로
확인됐습니다. 현 코드의 Python 최소조건은 3.10이며, Ubuntu 22.04/Python 3.10.12
non-root 컨테이너와 Ubuntu 24.04/Python 3.12 컨테이너의 기존 124개 기준을 통과했습니다.
Phase 3A-2 추가 회귀의 두 Ubuntu 재검증 결과는 이번 변경 검증 기록에서 별도로 갱신합니다.
현재 FastAPI는 기존 호환용 `POST /v1/workspace-overlays`, `GET /v1/index/status`와 함께
인증된 `GET /v1/internal/vss/source`, `GET /v1/internal/vss/revisions`를 제공합니다. 내부
VSS route는 SHA·tree SHA·`project_root`와 `/index` 호출값을 제공합니다. `/v1/admin/*`는
독립 `admin_web` BFF의 서비스 토큰, request HMAC, 사용자 역할을 모두 검증한 뒤에만
Repository·Branch·Binding·Snapshot·VSS catalog·감사 기능을 제공합니다.

Phase 7에서 module은 Chat을 proxy하거나 질의를 생성하지 않습니다. VSS가 `/v1/chat`을
소유한 채 localhost 내부 API를 pull하고, module은 Repository/Branch/Tag/PR/MR와 exact
Snapshot·commit 관계를 결정론적 참고 자료로 제공합니다. 제안 계약과 완료 조건은
`docs/agent/15_REVISION_CONTEXT_PROVIDER.md`가 정본입니다.

Phase 6A-1 변경에는 팀 유지보수를 위한 한글 정책 주석, 장애 회귀 테스트, Ubuntu preflight,
읽기 전용 smoke와 VSS 검증자 인계 지침을 포함합니다. 소스 주석은 코드의 동작을 반복하지
않고 exact revision, 자동 재제출 금지, 경로·비밀정보 보호처럼 판단 근거가 필요한 곳에
한글로 작성합니다.

## 규약 권위

1. 이 저장소의 `CHARTER.md`, `docs/API.md`, `vss/server.py`, `vss/indexer.py`, Store 구현과 테스트
2. Snapshot 모듈의 Repository/Branch 수집·VSS source API 계약과 테스트
3. Frontend `frontend` 브랜치는 VSS `/v1/chat` 소비자 및 기존 호환 route 확인용 참조

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
- 현 Git source는 binding branch에서 base/target commit object를 모두 찾을 수 있을 때만
  동작합니다. push되지 않은 local-only target은 임의 revision으로 대체하지 않고 차단합니다.

## 구현 경계

```text
독립 Admin Web / 내부 수집 작업
    Repository 등록 → Branch 선택 → fetch → HEAD SHA 이력
             ↓
Snapshot Backend
    Snapshot 영속화 → 전체 revision 디렉터리 materialize → POST VSS /index
             ↑
VSS
    GET /v1/internal/vss/source?project_id=...&revision=...
    GET /v1/internal/vss/revisions?project_id=...
             ↑
Frontend ── VSS /v1/chat
```

`/v1/workspace-overlays`와 Frontend 조회 proxy는 현재 구현 보존용 호환 경계이지 신규
Repository 수집의 정본 진입점이 아닙니다.

Snapshot ID는 Backend 내부 레코드 ID입니다. Git revision과 혼동하지 않습니다.
Frontend payload에는 branch가 없으므로 활성 binding 값을 수신 시점 Snapshot에 복사해
과거 이력을 고정합니다. 독립 Branch는 별도 `vss_project_id` 사용을 원칙으로 합니다.

## VSS HTTP 런타임 원칙

- Backend는 `vss_server/main`에서 실행되는 VSS HTTP 서버의 `POST /index`,
  `GET /index/status`, `GET /index/exists`, `GET /projects`, `GET /health`,
  `GET /v1/models`, `GET /briefing`만 호출합니다.
- `VSS_TOKEN`이 설정된 서버에는 `X-VSS-Token` 또는 Bearer 인증을 사용합니다.
- VSS는 Backend의 `GET /v1/internal/vss/source`, `GET /v1/internal/vss/revisions`를
  `SNAPSHOT_VSS_API_TOKEN`으로 호출합니다. 응답의 commit/tree SHA와 clean working tree를
  VSS server-local Git에서 다시 검증합니다.
- HTTP `202` 접수는 완료가 아닙니다. `GET /index/status`를 동기화합니다.
- 인덱싱 실패 시 VSS가 이전 active index를 보존하는 경계를 침범하지 않습니다.
- `project_root`는 VSS 서버에서 읽을 수 있는 server-local/shared 경로여야 합니다.
- materialized root의 Git HEAD가 target revision이 아니면 완료 보장이 불가능하므로
  제출 전에 차단합니다. 로컬-only commit 지원에는 upstream의 명시적 revision 계약이
  필요합니다.

## 주소 경계

```text
Backend 내부 bind             http://127.0.0.1:8000
VSS Snapshot API              http://127.0.0.1:8200
PostgreSQL                    127.0.0.1:5432
독립 Admin service 예정       http://<AWS-HOST>:4180
Frontend AI 진입점            http://127.0.0.1:11500
Windows portproxy 대상        http://192.168.0.12:11500
VSS 기본 Ollama URL           http://127.0.0.1:11434
```

Backend, VSS와 PostgreSQL은 같은 AWS Ubuntu 인스턴스의 일반 Linux service로 실행하므로
서버 내부 통신은 `127.0.0.1`만 사용합니다. Admin service 포트는 `4180`으로 고정하며
Nginx는 필수 구성으로 전제하지 않습니다. 외부 브라우저와 Frontend는 자신의 loopback이
아니라 승인된 AWS ingress 주소를 사용합니다. `127.0.0.1:11500`은 Frontend Windows
호스트의 portproxy 진입점이므로 AWS loopback과 구분하며 별도 합의 전까지 수정하지
않습니다.

## 작업 안전

- 작업 전후 `git status -sb`, `git diff --check`를 확인합니다.
- Frontend 참조 checkout과 이 구현 worktree를 분리합니다.
- materialization 경로는 설정된 전용 root 아래인지 resolve 후 검사합니다.
- 재귀 삭제·이동 전 대상 절대경로가 전용 root 내부인지 재검증합니다.
- contract → unit → integration 순으로 테스트합니다.
- VSS 측 검증은 `docs/agent/11_VSS_VALIDATOR_HANDOFF.md`의 PASS/FAIL/WAIT 형식을 사용합니다.
- 비밀키, DSN, token, 파일 본문을 문서·fixture·로그에 저장하지 않습니다.
- commit/push는 사용자가 명시적으로 요청한 경우에만 수행합니다.
- commit/push 전후에 `git status -sb`, `git diff --check`를 확인합니다.
- `module` 브랜치의 변경분에 main 프로젝트 파일·문서·폴더 수정을 포함하지 않습니다.
- Snapshot Backend는 최상위 `module/` 경로 안에서만 구성하고 main 파일과 섞지 않습니다.
