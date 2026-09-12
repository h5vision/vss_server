# vss_server

## 이 프로젝트는

- vss_server는 인덱싱, 질의, 브리핑의 3가지 핵심 기능을 제공한다고 문서에 명시되어 있습니다. [1]
- vss/cli.py와 vss/eval/__main__.py는 argparse 기반 CLI 진입점이며, vss/server.py는 문서상 진입점으로 기록되어 있습니다. [75][77][78]
- vss/server.py는 8200 포트에서 HTTP 서버를 실행하며, vss/cli.py는 동일한 기능을 CLI로 제공합니다. [79][80]

## 기능 목록

- 브리핑 생성은 동기(CLI) 또는 비동기(HTTP) 방식으로 지원되며, 실행별 기록과 캐시 구조가 정의되어 있다고 문서에 주장됩니다. [8]
- RAG 평가에서 top_k 기본값(8)과 matrix 설정(4)의 차이를 설명하며, top_k=8 결과는 저장 데이터 기반 추정치임을 명시한다고 문서에 주장됩니다. [19]
- Git 비교 로직과 Snapshot 승격 로직을 독립적인 Application UseCase 계층으로 추출하여 라우터의 책임을 축소했다고 문서에 주장됩니다. [39][40]
- Snapshot 상태 전이 검증 및 CAS 메서드 도입이 문서에 주장됩니다. [51][52]
- Git Adapter는 capability 기반 port와 hardened Git process runner를 공유한다고 문서에 주장됩니다. [65]

## 주요 실행 흐름

- vss/cli.py의 main 함수는 argparse로 인자를 파싱한 후 해당 명령의 함수를 호출합니다. [75]
- vss/eval/__main__.py의 main 함수는 명령에 따라 runner 또는 sweep_mod 모듈의 함수를 호출합니다. [77]
- 서버 기동 시 죽은 브리핑 lock과 잔류 running status를 정리한 후, 워밍업이 비활성화되지 않았다면 인덱스 로드와 모델 준비를 수행합니다. [79]
- ThreadingHTTPServer가 지정된 host와 port에서 serve_forever를 호출하여 요청을 처리합니다. [79]

## 처음 읽을 순서

- vss/cli.py를 읽으면 CLI 하위 명령과 인자 구조를 확인할 수 있습니다. [75]

## 기능·주제별 상세 설명

### 실행과 진입점

**설명**
- vss/cli.py는 health, projects, index, search, ask, briefing, bm25, repair, doctor 등 하위 명령을 정의하는 main 함수를 포함합니다. [75]
- vss/eval/__main__.py는 validate, run, report, runs, sweep 명령을 처리하는 main 함수를 포함합니다. [77]
- 문서에는 vss/cli.py, vss/server.py, vss/eval/__main__.py가 진입점으로 판정된 근거가 기록되어 있습니다. [78]
- vss/server.py의 main 함수는 표준 라이브러리 HTTP 서버를 시작하며, 기본 포트는 8200입니다. [79]
- vss/cli.py는 서버와 동일한 기능을 제공하는 CLI 도구로, health, index, search, ask, briefing, doctor, repair 등의 명령을 지원합니다. [80]
- vss/eval/__main__.py는 matrix와 suite 기반의 평가 실행을 담당하며, Hit@k, MRR, no-evidence recall 지표를 계산합니다. [80]

**조건·제약**
- vss/cli.py의 index 명령은 --project 인자가 필수이며, --force 또는 --briefing 옵션에 따라 동작이 달라집니다. [75]
- vss/eval/__main__.py의 sweep 명령은 --min, --max, --step 인자를 통해 임계값 범위를 지정합니다. [77]
- VSS_TOKEN 환경 변수가 설정되거나 --token 인자가 제공되면 모든 HTTP 요청에 대해 토큰 인증이 수행됩니다. [79][80]
- --no-warmup 인자가 제공되면 모델 준비 과정(임베딩, 모델 로드)이 건너뛰어집니다. [79][80]
- VSS_QUERYLOG_DSN 환경 변수가 비어 있으면 /v1/chat 요청 로그가 기록되지 않습니다. [80]

**처리 흐름**
- vss/cli.py의 main 함수는 argparse로 인자를 파싱한 후 해당 명령의 함수를 호출합니다. [75]
- vss/eval/__main__.py의 main 함수는 명령에 따라 runner 또는 sweep_mod 모듈의 함수를 호출합니다. [77]
- 서버 기동 시 죽은 브리핑 lock과 잔류 running status를 정리한 후, 워밍업이 비활성화되지 않았다면 인덱스 로드와 모델 준비를 수행합니다. [79]
- ThreadingHTTPServer가 지정된 host와 port에서 serve_forever를 호출하여 요청을 처리합니다. [79]

**읽을 위치**
- vss/cli.py를 읽으면 CLI 하위 명령과 인자 구조를 확인할 수 있습니다. [75]
- vss/eval/__main__.py를 읽으면 평가 실행 및 리포트 생성 진입점을 확인할 수 있습니다. [77]
- vss/server.py는 HTTP API의 진입점과 기동 로직을 포함하고 있어 실행 방법을 이해하는 데 필요합니다. [79]
- README.md는 각 모듈의 역할과 실행 조건을 요약하여 전체 아키텍처를 파악하는 데 도움이 됩니다. [80]

- 확인 필요: vss/server.py의 실제 코드 내용과 요청 처리 로직은 제공된 증거에 없습니다.
- 확인 필요: module/admin_web/__main__.py와 module/admin_web/app.py의 진입점 구조는 제공된 증거에 없습니다.
- 확인 필요: module/backend/app.py의 진입점 구조는 제공된 증거에 없습니다.
- 확인 필요: module/admin_web/__main__.py와 module/admin_web/app.py의 구체적인 실행 로직과 진입점
- 확인 필요: module/backend/app.py의 실행 방법과 요청 처리 방식
- 확인 필요: vss/eval/__main__.py의 실제 CLI 인자 파싱 및 실행 흐름
### 인덱싱 및 Git 스냅샷 관리

분석 미완료 — 시간 예산으로 일부 조사 생략

### 브리핑 생성 및 캐싱

분석 미완료 — 시간 예산으로 일부 조사 생략

### 관리자 웹 인터페이스 및 인증

분석 미완료 — 시간 예산으로 일부 조사 생략

### 백엔드 API 라우팅 및 통합

분석 미완료 — 시간 예산으로 일부 조사 생략

### 데이터베이스 스키마 및 마이그레이션

분석 미완료 — 시간 예산으로 일부 조사 생략

### 데이터와 외부 의존성

분석 미완료 — 시간 예산으로 일부 조사 생략

### 설정과 제약

분석 미완료 — 시간 예산으로 일부 조사 생략


## 문서 요약

- vss_server는 레포를 인덱싱하고 질문에 출처와 함께 답하며, 인덱싱 완료 후 프로젝트 브리핑을 생성하는 서버로 설명된다. [1]
- 인덱싱 경로는 파일 수집, AST 청킹, bge-m3 임베딩, 저장소 저장, BM25 역색인 생성, 브리핑 생성 순서로 진행한다고 문서화되어 있다. [2]
- 질의 경로는 벡터 검색, 선택적 BM25 섞기, 심볼 부스트, 임계값 기반 근거 판정, 프롬프트 렌더링, Ollama 스트리밍, 출처 확정 순서로 동작한다고 문서화되어 있다. [2]
- 브리핑은 파일·설정·진입점·함수 등을 조사하고, 문서와 코드에서 주제를 선정한 뒤 원문을 읽어 분석하며, 마지막에 개요를 생성한다고 설명된다. [7]
- 브리핑 생성 시 토큰 예산, 시간 예산, 근거 검증, 실패 처리, 실행 기록 관리 등 구체적인 운영 규칙이 정의되어 있다. [7]
- EC2 환경에서는 setup_ec2.sh 스크립트를 통해 패키지, venv, PostgreSQL+pgvector, DB 초기화, systemd 설정을 수행한다고 안내되어 있다. [5]
- Python은 AST를 통해 정의와 호출 후보를 추출하며, 다른 언어는 텍스트 조사 방식으로 처리하고 제한 사항을 기록합니다. [8]
- EC2에서 기존 인덱스를 사용하여 브리핑을 생성하려면 `python -m vss.cli briefing` 명령을 사용하고, CLI는 완료까지 기다립니다. [8]
- 비동기 브리핑 요청은 202를 반환하며, `queued`, `running`, `ready`, `failed` 상태를 조회할 수 있습니다. [8]
- 평가 문항은 `suites/*.jsonl`에 저장되며, 실행 매트릭스는 `matrices/*.json`에 정의되어 있습니다. [9]
- Snapshot Backend는 `vss_server/main`의 exact SHA로 배포된 VSS HTTP API를 사용하는 기준 문서 체계를 갖추고 있습니다. [11]
- 브리핑 생성 과정은 실제 모델(qwen3.8:27b)로 3회 실행되며, 시간 예산과 LLM 호출 제한에 따라 결과가 달라지는 것으로 기록되어 있습니다. [15][16]
- RAG 평가 문서에서 config.py의 top_k 기본값이 8인 반면 평가 matrix에는 4가 사용된다고 명시하며, 이는 저장된 실험 결과와 코드 기본값의 차이를 설명한다. [19]
- top_k를 8로 넓혔을 때의 정확도는 저장된 passed_paths와 순위 기반의 추정치이며, 실제 서비스 정확도나 생성 답변 정확도로 인용할 수 없다고 문서에 명시되어 있다. [19]
- 평가 실행 및 보고서 생성을 위한 vss.eval 모듈 명령어와 로컬 상태 파일 생성 스크립트가 문서에 제시되어 있으나, 해당 명령이 작성 작업 중 실제로 실행된 것은 아니다. [20]
- 목표 폴더 구조 문서에 따르면 현재 구현은 backend의 core, features(workspace_overlays, materialization, snapshots, indexing, vss_sources, commit_catalog 등), integrations, infrastructure 및 admin_web 범위로 완료된 상태라고 기술되어 있다. [22]
- materialization 저장 구조는 SNAPSHOT_MATERIALIZATION_ROOT 하위에 safe-vss-project-key, staging, revisions 디렉터리가 구성되며, raw project_id는 직접 사용하지 않고 DB 내부 안전 key를 사용한다고 문서에 정의되어 있다. [24]
- 피해야 할 구조로 main.py에 모든 기능을 구현하거나, utils.py에 경로 삭제와 VSS 호출을 혼합하거나, VSS 소스를 Backend package 안으로 복사하는 것을 금지한다고 문서에 명시되어 있다. [25]
- Docker를 사용하여 Ubuntu 22.04 및 24.04 환경에서 Ruff, compileall, Contract/Unit/Integration 테스트 및 POSIX permission 장애 테스트가 통과하는 것을 검증하는 절차가 문서화되어 있습니다. [32]
- PostgreSQL 17 검증 스크립트는 migration, DB 제약, 동일 Snapshot 재시도 row lock, startup recovery advisory lock을 증명하지만 운영 role/DSN, shared path, VSS E2E는 증명하지 않는다고 명시되어 있습니다. [32]
- PostgreSQL 검증 스크립트는 기본적으로 postgres:17.10-alpine 이미지를 사용하는 임시 컨테이너를 생성하고, 실행 결과와 관계없이 자신이 만든 컨테이너만 종료하며 기존 PostgreSQL이나 다른 컨테이너에는 영향을 주지 않는다고 설명되어 있습니다. [33]
- commit_catalog_runs 테이블은 run_id, state, lease_expires_at, history_complete 등 실행 상태와 lease 정보를 관리하며, 만료되지 않은 run이 있으면 COMMIT_CATALOG_ALREADY_RUNNING, 만료 run은 COMMIT_CATALOG_LEASE_EXPIRED로 처리된다고 문서화되어 있습니다. [34]
- SHA-256 Repository는 40자리 SHA-1 계약과 다르므로 fail closed하며, unavailable root, truncation, shallow history는 성공 결과 안에서도 history_complete=false로 명시된다고 설명되어 있습니다. [35]
- 기존 1,046줄의 backend/features/admin/router.py 단일 파일을 외부 HTTP API 계약 변경 없이 7개의 도메인별 하위 라우터로 물리 분할했다고 문서화되어 있습니다. [37][38]
- Snapshot 상태 변경에 중앙 집중식 상태 머신 검증을 도입하여 유효하지 않은 전이를 차단하고, 동시성 제어를 위해 CAS 기반 상태 전이 메서드를 제공한다고 문서화되어 있습니다. [51][52]
- 분산 환경의 Race Condition을 방지하기 위해 단조 증가 정수형 Fencing Token을 도입하고, DB 스키마에 해당 컬럼을 추가하는 마이그레이션을 수행했다고 명시되어 있습니다. [53][54]
- Repository 운영자는 opt-in sync를 통해 PR/MR의 SHA를 수집하고, VSS는 검증된 revision과 Tag commit만 context 후보로 사용해야 한다고 정의되어 있습니다. [55]
- Chat observability 기능은 기본적으로 비활성화되어 있으며, 활성화 시 trace content mode, request size 상한, retention policy 등 다양한 환경 변수로 구성이 가능합니다. [60]
- Admin service restart 컨트롤러는 systemd 호스트에 설치되며, 설치 후 한 번의 수동 재시작을 통해 새 샌드박스 권한과 BFF allowlist를 활성화한다고 안내되어 있습니다. [61]
- HTTP 요청과 장시간 작업의 라이프사이클을 분리하기 위해 PostgreSQL의 durable job table과 FOR UPDATE SKIP LOCKED를 사용하는 런타임 토폴로지를 권장하고 있습니다. [64]
- Git Adapter는 capability 기반 port를 사용하여 RemoteRefReader, GitObjectRepository, CommitGraphReader로 구성되며, 모든 adapter는 하나의 hardened Git process runner를 공유합니다. [65]
- Admin Web은 Control Plane으로 기능하며, Admin Router의 책임은 HTTP input, 인증/인가, Use-case invocation, HTTP output으로 제한됩니다. [66]
- 설정(Settings)은 Runtime, Database, Git, Snapshot, Collection, Vss, Provider, Security 등 논리적 구성 그룹으로 나뉘어 관리됩니다. [67]
- 최종 아키텍처 결정은 Modular Monolith, Hexagonal Boundaries, PostgreSQL Durable Jobs, Separate API/Worker Processes를 채택하는 것입니다. [68]
- 새로운 worktree 구조는 dot-prefixed directory(.snapshot-worktrees)를 사용하여 기존 VSS Repository discovery와 분리하며, 경로에는 hyphen 없는 UUID.hex를 사용합니다. [70][71]
- 저장 계층은 mutable current Branch working copies, immutable exact-revision materializations, VSS vector/BM25/briefing/index artifacts로 세 영역으로 분리됩니다. [72]

## 진입점

- `vss/cli.py`:L243 — 파일명 규칙(cli.py) · 최상위 근처 · 'if __name__ == · def main · argparse.ArgumentParser' 포함
- `vss/server.py`:L507 — 파일명 규칙(server.py) · 최상위 근처 · 'if __name__ == · def main · argparse.ArgumentParser' 포함
- `module/admin_web/__main__.py`:L7 — 파일명 규칙(__main__.py) · 'if __name__ ==' 포함
- `module/admin_web/app.py`:L91 — 파일명 규칙(app.py) · 'create_app · FastAPI' 포함
- `module/backend/app.py`:L37 — 파일명 규칙(app.py) · 'create_app · FastAPI' 포함
- `vss/eval/__main__.py`:L14 — 파일명 규칙(__main__.py) · 'if __name__ == · def main · argparse.ArgumentParser' 포함
- `module/main.py`:L1 — 파일명 규칙(main.py) · 최상위 근처
- `scripts/make_status.py`:L32 — 최상위 근처 · 'if __name__ == · def main · argparse.ArgumentParser' 포함

### `vss/cli.py`

- L32 `def _onoff(v: str | None) -> bool | None:`
- L38 `def cmd_health(a) -> int:`
- L74 `def cmd_projects(a) -> int:`
- L95 `def cmd_index(a) -> int:`
- L125 `def cmd_status(a) -> int:`
- L130 `def cmd_search(a) -> int:`
- L150 `def cmd_ask(a) -> int:`
- L177 `def cmd_briefing(a) -> int:`
- L199 `def cmd_bm25(a) -> int:`
- L204 `def cmd_repair(a) -> int:`
- … 총 12개 중 10개 표시

### `vss/server.py`

- L48 `def _prepare_models(wait_s: int = 60) -> None:` — 기동 전용 — 이 서버가 모델 상태를 바꾸는 **유일한** 자리 (md 결정 2026-09-06. 요청 경로는 pick_model 로 올라온 것만 쓴다).
- L87 `def _briefing_hook(model: str | None):`
- L93 `def _briefing_policy(v) -> str | None:` — POST /index 의 `briefing` — true(기본)·"auto": 전체 인덱싱 뒤에만 생성, 증분 뒤에는 이전 브리핑 유지 /
- L105 `def _flag(q: dict, name: str) -> bool:` — ?files=1 · ?files=true · ?files (값 없음) 를 모두 참으로 봅니다. 0·false 는 거짓.
- L117 `def _clone_repo(remote: str, branch: str, base_dir: Path = Path.home() / "repos") -> Path:` — remote 를 base_dir/<repo-name> 에 clone(이미 있으면 fetch+reset)하고 로컬 경로를 반환합니다.
- L154 `def _branch_of(v) -> str | None:`
- L161 `def _current_branch(root: str | Path) -> str | None:` — clone 이 실제로 받아 온 브랜치 이름. 분리 HEAD 면 None.
- L176 `def _index_name(pid: str, branch: str | None, commit: str | None, chunker: str | None) -> str:` — 인덱스 이름 = `<레포>@<브랜치>--<청커>-<sha7>`.
- L191 `class Handler(BaseHTTPRequestHandler):`
- L507 `def main(argv=None):`

### `module/admin_web/__main__.py`

(최상위 함수·클래스 없음 — 모듈 실행 코드)

### `module/admin_web/app.py`

- L28 `class LoginRequest(BaseModel):`
- L33 `def _error( status_code: int, reason: str, detail: str, *, retryable: bool = False, request_id: UUID | None = None, headers: dict[str, str] | None = None, ) -> JSONResponse:`
- L53 `def _session_identity(request: Request, users_file: Path) -> AdminUser | None:`
- L69 `def _verify_mutation(request: Request, settings: AdminWebSettings) -> JSONResponse | None:`
- L84 `def _raw_target(request: Request) -> str:`
- L91 `def create_app( settings: AdminWebSettings | None = None, *, backend_transport: httpx2.AsyncBaseTransport | httpx2.BaseTransport | None = None, clock: Callable[[], float] = time.time, request_id_facto`
- L380 `def create_app_from_environment() -> FastAPI:`

### `module/backend/app.py`

- L37 `def create_app( settings: Settings | None = None, *, vss_transport: httpx2.BaseTransport | None = None, ollama_transport: httpx2.BaseTransport | None = None, materialization_source: TreeSource | None ` — 애플리케이션을 만들고 ApplicationContainer Composition Root를 lifespan에 연결한다.

### `vss/eval/__main__.py`

- L14 `def main(argv=None) -> int:`

### `module/main.py`

(최상위 함수·클래스 없음 — 모듈 실행 코드)

### `scripts/make_status.py`

- L24 `def pct(v):`
- L28 `def num(v):`
- L32 `def main(argv=None) -> int:`

## 라우트·등록

- `GET /health` → `health` (module/admin_web/app.py:157)
- `POST /api/auth/login` → `login` (module/admin_web/app.py:161)
- `POST /api/auth/logout` → `logout` (module/admin_web/app.py:214)
- `GET /api/auth/session` → `session` (module/admin_web/app.py:224)
- `GET,POST,PATCH,DELETE,PUT,OPTIONS,HEAD /v1/admin/{admin_path:path}` → `proxy_admin` (module/admin_web/app.py:238)
- `GET /styles.css` → `styles` (module/admin_web/app.py:365)
- `GET /app.js` → `script` (module/admin_web/app.py:369)
- `GET /` → `index` (module/admin_web/app.py:373)
- `GET /audit-logs` → `list_audit_logs` (module/backend/features/admin/routers/audit.py:21)
- `GET /branch-bindings` → `list_branch_bindings` (module/backend/features/admin/routers/bindings.py:34)
- `POST /branch-bindings` → `create_branch_binding` (module/backend/features/admin/routers/bindings.py:57)
- `PATCH /branch-bindings/{binding_id}` → `update_branch_binding` (module/backend/features/admin/routers/bindings.py:90)
- `DELETE /branch-bindings/{binding_id}` → `deactivate_branch_binding` (module/backend/features/admin/routers/bindings.py:130)
- `GET /chat/conversations` → `list_chat_conversations` (module/backend/features/admin/routers/chat.py:114)
- `GET /chat/conversations/{conversation_id}` → `get_chat_conversation` (module/backend/features/admin/routers/chat.py:146)
- `GET /chat/retention` → `get_chat_retention` (module/backend/features/admin/routers/chat.py:173)
- `DELETE /chat/conversations/{conversation_id}` → `delete_chat_conversation` (module/backend/features/admin/routers/chat.py:197)
- `POST /chat/retention/purge` → `purge_chat_retention` (module/backend/features/admin/routers/chat.py:247)
- `GET /chat/responses/{response_id}/trace` → `get_chat_response_trace` (module/backend/features/admin/routers/chat.py:301)
- `GET /repositories/{repository_id}/commits` → `list_repository_commits` (module/backend/features/admin/routers/commits.py:32)
- `GET /repositories/{repository_id}/commits/{commit_sha}` → `get_repository_commit` (module/backend/features/admin/routers/commits.py:68)
- `GET /repositories/{repository_id}/compare` → `compare_repository_commits` (module/backend/features/admin/routers/commits.py:100)
- `POST /repositories/{repository_id}/commits/{commit_sha}/materialize` → `materialize_repository_commit` (module/backend/features/admin/routers/commits.py:120)
- `GET /repositories` → `list_repositories` (module/backend/features/admin/routers/repositories.py:49)
- `GET /repositories/{repository_id}` → `get_repository` (module/backend/features/admin/routers/repositories.py:66)
- `POST /repositories` → `create_repository` (module/backend/features/admin/routers/repositories.py:74)
- `PATCH /repositories/{repository_id}` → `update_repository` (module/backend/features/admin/routers/repositories.py:107)
- `DELETE /repositories/{repository_id}` → `deactivate_repository` (module/backend/features/admin/routers/repositories.py:147)
- `DELETE /repositories/{repository_id}/purge` → `purge_repository` (module/backend/features/admin/routers/repositories.py:179)
- `GET /repositories/{repository_id}/branches` → `list_remote_branches` (module/backend/features/admin/routers/repositories.py:242)
- `POST /repositories/{repository_id}/sync` → `sync_repository` (module/backend/features/admin/routers/repositories.py:258)
- `GET /repository-sync-runs` → `list_sync_runs` (module/backend/features/admin/routers/repositories.py:303)
- `GET /runtime/models` → `get_runtime_models` (module/backend/features/admin/routers/runtime.py:71)
- `GET /runtime/services` → `get_runtime_services` (module/backend/features/admin/routers/runtime.py:90)
- `POST /runtime/services/restart` → `restart_runtime_services` (module/backend/features/admin/routers/runtime.py:104)
- `POST /runtime/models/run` → `run_runtime_model` (module/backend/features/admin/routers/runtime.py:207)
- `POST /runtime/models/up` → `up_runtime_model` (module/backend/features/admin/routers/runtime.py:224)
- `POST /runtime/models/down` → `down_runtime_model` (module/backend/features/admin/routers/runtime.py:240)
- `POST /runtime/models/reload` → `reload_runtime_model` (module/backend/features/admin/routers/runtime.py:270)
- `PUT /runtime/models/auto-up` → `set_runtime_model_auto_up` (module/backend/features/admin/routers/runtime.py:299)
- … 라우트 총 69개 중 40개 표시 (전부는 실행 기록의 routes)
- `include_router`(`health_router`, `resolved_settings.api_prefix`) → `create_app` (module/backend/app.py:195) — 정적 후보
- `include_router`(`chat_observability_router`, `resolved_settings.api_prefix`) → `create_app` (module/backend/app.py:196) — 정적 후보
- `include_router`(`frontend_proxy_router`, `resolved_settings.api_prefix`) → `create_app` (module/backend/app.py:197) — 정적 후보
- `include_router`(`workspace_overlays_router`, `resolved_settings.api_prefix`) → `create_app` (module/backend/app.py:198) — 정적 후보
- `include_router`(`indexing_router`, `resolved_settings.api_prefix`) → `create_app` (module/backend/app.py:199) — 정적 후보
- `include_router`(`vss_sources_router`, `resolved_settings.api_prefix`) → `create_app` (module/backend/app.py:200) — 정적 후보
- `include_router`(`admin_router`, `resolved_settings.api_prefix`) → `create_app` (module/backend/app.py:201) — 정적 후보
- `asyncio.create_task` → `build_container` (module/backend/bootstrap/container.py:214) — 정적 후보
- `asyncio.create_task` → `build_container` (module/backend/bootstrap/container.py:246) — 정적 후보
- `include_router`(`repositories_router`) (module/backend/features/admin/router.py:24) — 정적 후보
- `include_router`(`tracked_branches_router`) (module/backend/features/admin/router.py:25) — 정적 후보
- `include_router`(`bindings_router`) (module/backend/features/admin/router.py:26) — 정적 후보
- `include_router`(`chat_router`) (module/backend/features/admin/router.py:27) — 정적 후보
- `include_router`(`snapshots_router`) (module/backend/features/admin/router.py:28) — 정적 후보
- `include_router`(`commits_router`) (module/backend/features/admin/router.py:29) — 정적 후보
- `include_router`(`vss_router`) (module/backend/features/admin/router.py:30) — 정적 후보
- `include_router`(`runtime_router`) (module/backend/features/admin/router.py:31) — 정적 후보
- `include_router`(`audit_router`) (module/backend/features/admin/router.py:32) — 정적 후보
- `sub.add_parser('health').set_defaults` → `main` (vss/cli.py:246) — 정적 후보
- `sub.add_parser`(`health`) → `main` (vss/cli.py:246) — 정적 후보
- `sub.add_parser`(`projects`) → `main` (vss/cli.py:247) — 정적 후보
- `sub.add_parser`(`index`) → `main` (vss/cli.py:249) — 정적 후보
- `sub.add_parser`(`status`) → `main` (vss/cli.py:258) — 정적 후보
- `sub.add_parser`(`search`) → `main` (vss/cli.py:259) — 정적 후보
- `sub.add_parser`(`ask`) → `main` (vss/cli.py:262) — 정적 후보
- `sub.add_parser`(`briefing`) → `main` (vss/cli.py:266) — 정적 후보
- `sub.add_parser`(`bm25`) → `main` (vss/cli.py:269) — 정적 후보
- `sub.add_parser`(`repair`) → `main` (vss/cli.py:270) — 정적 후보
- `sub.add_parser('doctor').set_defaults` → `main` (vss/cli.py:271) — 정적 후보
- `sub.add_parser`(`doctor`) → `main` (vss/cli.py:271) — 정적 후보
- `sub.add_parser`(`validate`) → `main` (vss/eval/__main__.py:17) — 정적 후보
- `sub.add_parser`(`run`) → `main` (vss/eval/__main__.py:18) — 정적 후보
- `sub.add_parser`(`report`) → `main` (vss/eval/__main__.py:19) — 정적 후보
- `sub.add_parser`(`runs`) → `main` (vss/eval/__main__.py:20) — 정적 후보
- `sub.add_parser`(`sweep`) → `main` (vss/eval/__main__.py:21) — 정적 후보
- 테스트 파일의 라우트·등록 7개는 생략 (전부는 실행 기록의 routes)

## 확인이 필요한 사항

- vss/server.py의 실제 코드 내용과 요청 처리 로직은 제공된 증거에 없습니다.
- module/admin_web/__main__.py와 module/admin_web/app.py의 진입점 구조는 제공된 증거에 없습니다.
- module/backend/app.py의 진입점 구조는 제공된 증거에 없습니다.
- module/backend/app.py의 실행 방법과 요청 처리 방식
- vss/eval/__main__.py의 실제 CLI 인자 파싱 및 실행 흐름
- 인덱싱 및 Git 스냅샷 관리, 브리핑 생성 및 캐싱, 관리자 웹 인터페이스 및 인증, 백엔드 API 라우팅 및 통합, 데이터베이스 스키마 및 마이그레이션, 데이터와 외부 의존성, 설정과 제약에 대한 상세 분석이 시간 예산으로 인해 생략되었습니다.
- 예산 때문에 읽지 않은 문서 절이 580개 있습니다.
- Python 이외 코드 파일 3개는 본문 검색만 했습니다 (구조 추출 없음).
- 부분 결과입니다 — 시간 예산으로 일부 조사 생략. 자세한 내용은 실행 기록(analysis.json)에 있습니다.

## 근거

- [1] `README.md`:L1-8
- [2] `README.md`:L164-183
- [5] `README.md`:L342-351
- [7] `README.md`:L617-631
- [8] `README.md`:L632-665
- [9] `evaluation/README.md`:L1-30
- [11] `module/docs/agent/README.md`:L1-32
- [15] `docs/BRIEFING_TUNING_20260909.md`:L1-9
- [16] `docs/BRIEFING_TUNING_20260909.md`:L95-109
- [19] `docs/RAG_EVALUATION_20260907.md`:L132-144
- [20] `docs/RAG_EVALUATION_20260907.md`:L209-231
- [22] `module/docs/agent/03_TARGET_STRUCTURE.md`:L22-58
- [24] `module/docs/agent/03_TARGET_STRUCTURE.md`:L169-185
- [25] `module/docs/agent/03_TARGET_STRUCTURE.md`:L193-201
- [32] `module/docs/agent/11_VSS_VALIDATOR_HANDOFF.md`:L34-69
- [33] `module/docs/agent/12_POSTGRESQL_RUNTIME_VALIDATION.md`:L12-30
- [34] `module/docs/agent/16_COMMIT_HISTORY_AND_COMPARISON.md`:L98-114
- [35] `module/docs/agent/16_COMMIT_HISTORY_AND_COMPARISON.md`:L155-172
- [37] `module/docs/agent/17_ARCHITECTURE_REFACTORING.md`:L200-202
- [38] `module/docs/agent/17_ARCHITECTURE_REFACTORING.md`:L203-218
- [39] `module/docs/agent/17_ARCHITECTURE_REFACTORING.md`:L228-230
- [40] `module/docs/agent/17_ARCHITECTURE_REFACTORING.md`:L231-242
- [51] `module/docs/agent/17_ARCHITECTURE_REFACTORING.md`:L391-396
- [52] `module/docs/agent/17_ARCHITECTURE_REFACTORING.md`:L397-405
- [53] `module/docs/agent/17_ARCHITECTURE_REFACTORING.md`:L417-423
- [54] `module/docs/agent/17_ARCHITECTURE_REFACTORING.md`:L424-438
- [55] `module/docs/agent/17_PHASE_7A3_TDD_EVIDENCE.md`:L9-18
- [60] `module/docs/agent/23_CHAT_OBSERVABILITY.md`:L173-195
- [61] `module/docs/architecture/ADMIN_SERVICE_RESTART.md`:L112-136
- [64] `module/docs/architecture/ARCHITECTURE.md`:L205-240
- [65] `module/docs/architecture/ARCHITECTURE.md`:L377-416
- [66] `module/docs/architecture/ARCHITECTURE.md`:L727-762
- [67] `module/docs/architecture/ARCHITECTURE.md`:L823-844
- [68] `module/docs/architecture/ARCHITECTURE.md`:L1110-1114
- [70] `module/docs/architecture/REPOSITORY_WORKTREE_NAMESPACE.md`:L63-84
- [71] `module/docs/architecture/REPOSITORY_WORKTREE_NAMESPACE.md`:L85-128
- [72] `module/docs/architecture/REPOSITORY_WORKTREE_NAMESPACE.md`:L235-260
- [75] `vss/cli.py`:L243-273
- [77] `vss/eval/__main__.py`:L14-56
- [78] `brief.md`:L106-115
- [79] `vss/server.py`:L507-546
- [80] `README.md`:L188-194
