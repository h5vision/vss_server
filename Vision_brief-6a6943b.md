# vss_server

## 이 프로젝트는

- vss_server는 CLI, HTTP 서버, 평가 실행 모듈로 구성된 시스템으로, 인덱싱과 질의 기능을 제공하며 브리핑 결과를 파일로 저장합니다. [1][75][77][79][8]
- 관리자 웹 프록시는 서비스 토큰과 HMAC을 사용하여 백엔드 API를 호출하며, Argon2 해시를 통한 인증을 지원합니다. [84][86]
- 백엔드와 관리자 웹은 각각 컨테이너와 HTTP 클라이언트를 lifespan에 연결하여 초기화되며, 보안 헤더 미들웨어가 적용됩니다. [87][88]
- 데이터베이스 스키마는 Alembic 마이그레이션을 통해 관리되며, PostgreSQL 전용 제약이 있는 단계가 존재합니다. [91][94]
- 서비스 배포는 systemd와 preflight 스크립트를 통해 Python 버전 및 인터프리터 경로를 검증합니다. [97][98]

## 기능 목록

- CLI는 argparse 기반 하위 명령을 처리하며, index 명령은 --project 인자가 필수입니다. [75]
- HTTP 서버는 8200포트에서 ThreadingHTTPServer로 요청을 처리하며, VSS_TOKEN이 설정된 경우 토큰 검사를 수행합니다. [79][80]
- 평가 모듈은 sweep 명령을 통해 임계값 범위를 지정하여 실행합니다. [77]
- 관리자 웹은 클라이언트 헤더를 제거하고 서비스 토큰 및 HMAC 서명을 추가하여 백엔드 API를 프록시합니다. [84]
- 백엔드 애플리케이션은 설정을 해결하고 로깅을 구성한 후 컨테이너를 빌드하여 app.state에 연결합니다. [87]
- 데이터베이스 마이그레이션은 DATABASE_URL이 미설정된 경우 중단되며, 0002 마이그레이션은 PostgreSQL 전용입니다. [92][94]
- preflight 스크립트는 Python 3.10~3.14를 검증하며, SNAPSHOT_SERVICE_PYTHON이 설정되지 않으면 .venv/bin/python을 기본값으로 사용합니다. [98]

## 주요 실행 흐름

- CLI의 main 함수는 argparse로 인자를 파싱한 후 해당 하위 명령의 함수를 호출합니다. [75]
- 서버 기동 시 죽은 브리핑 lock과 running 상태를 정리한 후, 워밍업이 비활성화되지 않았다면 인덱스 로드와 모델 준비를 수행합니다. [79]
- ThreadingHTTPServer가 지정된 호스트와 포트에서 요청을 처리하며 무한 루프로 실행됩니다. [79]
- 클라이언트는 /api/auth/session을 호출하여 인증 상태를 확인하고, 인증되지 않으면 로그인 화면을 표시합니다. [85]
- 관리자 웹은 백엔드 API 요청을 프록시할 때, 클라이언트 헤더를 제거하고 서비스 토큰 및 HMAC 서명을 추가하여 전달합니다. [84]
- 백엔드 create_app은 설정을 해결하고 로깅을 구성한 후, lifespan 컨텍스트 매니저에서 컨테이너를 빌드하고 app.state에 연결합니다. [87]
- admin_web create_app은 설정을 로드하고 사용자 파일을 읽은 후, 백엔드 HTTP 클라이언트를 생성하여 lifespan에 연결합니다. [88]

## 처음 읽을 순서

- vss/cli.py를 읽으면 CLI 하위 명령과 인자 구조를 확인할 수 있습니다. [75]
- admin_web/app.py는 로그인 요청 모델과 프록시 로직을 포함하고 있어 인증 및 백엔드 호출 방식을 이해하는 데 필요합니다. [81][84]
- module/backend/app.py는 백엔드 애플리케이션의 진입점과 컨테이너 초기화 로직을 포함하고 있습니다. [87]
- 0001 파일은 repositories, branch_bindings, snapshots 테이블의 초기 DDL과 인덱스 생성 로직을 확인하기 위해 읽습니다. [91]
- systemd 서비스 정의와 preflight 검증 로직을 확인하기 위해 해당 파일을 읽습니다. [97][98]

## 기능·주제별 상세 설명

### 실행과 진입점

**설명**
- vss/cli.py는 health, projects, index, search, ask, briefing, bm25, repair, doctor 등 하위 명령을 정의하는 main 함수를 포함합니다. [75]
- vss/eval/__main__.py는 validate, run, report, runs, sweep 하위 명령을 처리하는 main 함수를 포함합니다. [77]
- 문서에는 vss/cli.py, vss/server.py, vss/eval/__main__.py가 진입점으로 판정된 근거가 기록되어 있습니다. [78]
- vss/server.py의 main 함수는 표준 라이브러리 기반 HTTP 서버를 시작하며, 기본 포트는 8200입니다. [79][80]
- vss/cli.py는 서버와 동일한 기능을 제공하는 CLI 인터페이스로, health, index, search, ask, briefing, doctor, repair 등의 명령을 지원합니다. [80]
- vss/eval/__main__.py는 matrix와 suite를 기반으로 Hit@k, MRR, no-evidence recall 등 평가 실행을 담당합니다. [80]

**조건·제약**
- vss/cli.py의 index 명령은 --project 인자가 필수이며, --force 또는 --briefing 옵션에 따라 동작이 달라집니다. [75]
- vss/eval/__main__.py의 sweep 명령은 --min, --max, --step 인자를 통해 임계값 범위를 지정합니다. [77]
- VSS_TOKEN이 설정된 경우 모든 요청에 대해 토큰 검사가 수행됩니다. [80]
- --no-warmup 플래그가 지정되면 모델 준비 과정이 건너뛴다. [79][80]
- VSS_QUERYLOG_DSN이 비어 있으면 쿼리 로그 기록이 수행되지 않습니다. [80]

**처리 흐름**
- vss/cli.py의 main 함수는 argparse로 인자를 파싱한 후 해당 하위 명령의 함수를 호출합니다. [75]
- vss/eval/__main__.py의 main 함수는 명령에 따라 runner 또는 sweep_mod 모듈의 함수를 호출합니다. [77]
- 서버 기동 시 죽은 브리핑 lock과 running 상태를 정리한 후, 워밍업이 비활성화되지 않았다면 인덱스 로드와 모델 준비를 수행합니다. [79]
- ThreadingHTTPServer가 지정된 호스트와 포트에서 요청을 처리하며 무한 루프로 실행됩니다. [79]

**읽을 위치**
- vss/cli.py를 읽으면 CLI 하위 명령과 인자 구조를 확인할 수 있습니다. [75]
- vss/eval/__main__.py를 읽으면 평가 실행 및 리포트 생성 진입점을 확인할 수 있습니다. [77]
- vss/server.py는 HTTP API 엔드포인트와 기동 로직을 정의하므로 실행 진입점 분석에 필요합니다. [79][80]
- vss/cli.py는 서버와 동일한 기능을 CLI로 제공하므로 로컬 실행 방법을 이해하는 데 필요합니다. [80]

- 확인 필요: vss/server.py의 실제 코드 내용과 요청 처리 로직은 제공된 증거에 없습니다.
- 확인 필요: module/admin_web/__main__.py와 module/admin_web/app.py의 진입점 및 동작은 제공된 증거에 없습니다.
- 확인 필요: module/backend/app.py의 진입점 및 동작은 제공된 증거에 없습니다.
- 확인 필요: module/admin_web/__main__.py, module/admin_web/app.py, module/backend/app.py 파일의 구체적인 진입점과 동작은 제공된 근거에 포함되어 있지 않습니다.
- 확인 필요: vss/eval/__main__.py의 구체적인 실행 인자나 세부 동작은 제공된 근거에 포함되어 있지 않습니다.
### 관리자 웹 프록시 및 인증

**설명**
- 관리자 웹은 백엔드 API 호출 시 브라우저 토큰을 서비스 토큰으로 교체하고, 요청 헤더에 HMAC 서명을 포함하여 전달합니다. [84][86]
- 로그인 요청은 사용자명과 비밀번호를 검증하며, 비밀번호는 Argon2 해시로 저장된 JSON 파일에서 확인됩니다. [81][86]
- 브라우저 세션은 30분 만료의 Strict 쿠키를 사용하여 관리됩니다. [86]

**조건·제약**
- 로컬 HTTP 개발 환경에서는 Secure 쿠키 설정이 비활성화될 수 있습니다. [86]
- 중복된 서비스 재시작 스코프 요청은 거부됩니다. [83]

**처리 흐름**
- 클라이언트는 /api/auth/session을 호출하여 인증 상태를 확인하고, 인증되지 않으면 로그인 화면을 표시합니다. [85]
- 관리자 웹은 백엔드 API 요청을 프록시할 때, 클라이언트 헤더를 제거하고 서비스 토큰 및 HMAC 서명을 추가하여 전달합니다. [84]

**읽을 위치**
- admin_web/app.py는 로그인 요청 모델과 프록시 로직을 포함하고 있어 인증 및 백엔드 호출 방식을 이해하는 데 필요합니다. [81][84]
- module/README.md는 관리자 웹의 환경 변수 설정, 세션 관리, 및 백엔드 호출 방식을 설명합니다. [86]

- 확인 필요: HMAC 서명의 정확한 생성 알고리즘과 검증 로직
- 확인 필요: 세션 토큰의 생성 및 저장 방식
- 확인 필요: 백엔드 API의 구체적인 응답 처리 및 오류 처리 로직
### 백엔드 라우터 구성

**설명**
- 백엔드 create_app은 FastAPI 인스턴스를 생성하고 lifespan을 통해 ApplicationContainer를 초기화 및 해제합니다. [87]
- admin_web의 create_app은 백엔드 클라이언트를 생성하고 세션 미들웨어 및 보안 헤더 미들웨어를 추가합니다. [88]
- api_prefix 설정값은 절대 경로여야 하며 루트 경로('/')는 허용되지 않습니다. [89]

**조건·제약**
- 백엔드 create_app에서 기존 라우터 호환성을 위해 app.state에 컨테이너 속성을 매핑합니다. [87]
- admin_web의 보안 헤더 미들웨어는 /api/ 또는 /v1/admin/ 경로일 때 Cache-Control: no-store를 설정합니다. [88]

**처리 흐름**
- 백엔드 create_app은 설정을 해결하고 로깅을 구성한 후, lifespan 컨텍스트 매니저에서 컨테이너를 빌드하고 app.state에 연결합니다. [87]
- admin_web create_app은 설정을 로드하고 사용자 파일을 읽은 후, 백엔드 HTTP 클라이언트를 생성하여 lifespan에 연결합니다. [88]

**읽을 위치**
- module/backend/app.py는 백엔드 애플리케이션의 진입점과 컨테이너 초기화 로직을 포함하고 있습니다. [87]
- module/backend/core/config.py는 api_prefix 검증 로직을 포함하고 있어 라우터 prefix 설정 방식을 이해하는 데 필요합니다. [89]

- 확인 필요: create_app에서 구체적으로 어떤 라우터들이 include_router로 포함되는지 제공된 증거에 명시되지 않았습니다.
- 확인 필요: api_prefix가 라우터 등록 시 실제로 어떻게 적용되는지(예: include_router의 prefix 인자)에 대한 코드가 제공되지 않았습니다.
### 데이터베이스 스키마 관리

**설명**
- 0001 마이그레이션은 PostgreSQL에서 snapshot 스키마를 생성하고 repositories, branch_bindings, snapshots 테이블을 정의합니다. [91][92]
- 0002 마이그레이션은 snapshot 관련 테이블의 외래키를 RESTRICT로 변경하고, revision 길이, 상태, attempt 번호 등 무결성 제약 조건을 추가합니다. [94][92]
- 0003 마이그레이션은 Frontend workspace 조회 키와 활성 partial unique 인덱스를 보강합니다. [92]

**조건·제약**
- DATABASE_URL이 미설정된 경우 마이그레이션은 즉시 중단됩니다. [92]
- 0002 마이그레이션은 PostgreSQL 전용으로 동작하도록 요구합니다. [94]

**처리 흐름**
- 0001에서 기본 테이블 구조를 생성한 후, 0002에서 무결성 제약과 FK 정책을 강화하고, 0003에서 workspace 식별자 인덱스를 추가하는 순서로 스키마가 진화합니다. [91][94][92]

**읽을 위치**
- 0001 파일은 repositories, branch_bindings, snapshots 테이블의 초기 DDL과 인덱스 생성 로직을 확인하기 위해 읽습니다. [91]
- 0002 파일은 snapshot_deltas, snapshot_attempts 테이블의 FK 변경 및 CHECK 제약 조건 추가 내용을 확인하기 위해 읽습니다. [94]

- 확인 필요: 0003_add_workspace_binding_identifier.py 파일의 구체적인 DDL 변경 내용은 제공된 증거에 포함되어 있지 않습니다.
- 확인 필요: SQLAlchemy 모델 구조에 대한 직접적인 코드 증거가 제공되지 않았습니다.
### 서비스 배포 및 런타임 검증

**설명**
- systemd 서비스는 restart_module_services.sh 스크립트를 실행하며, Python 인터프리터를 직접 호출하지 않습니다. [97]
- preflight 스크립트는 SNAPSHOT_SERVICE_PYTHON 환경변수 또는 모듈의 .venv/bin/python을 서비스 Python으로 사용합니다. [98]
- preflight 스크립트는 Ubuntu 22.04 이상, git 존재, Python 3.10 이상 3.15 미만 조건을 검증합니다. [98]

**조건·제약**
- SNAPSHOT_SERVICE_PYTHON이 설정되지 않으면 모듈 루트의 .venv/bin/python을 기본값으로 사용합니다. [98]
- SNAPSHOT_SERVICE_PYTHON은 절대경로여야 하며 실행 권한이 있어야 합니다. [98]

**처리 흐름**
- preflight 스크립트는 OS 정보, git, Python 경로 및 버전을 순서대로 검증합니다. [98]

**읽을 위치**
- systemd 서비스 정의와 preflight 검증 로직을 확인하기 위해 해당 파일을 읽습니다. [97][98]

- 확인 필요: restart_module_services.sh 스크립트 내부에서 어떤 Python 인터프리터가 사용되는지 확인되지 않았습니다.
### CLI 및 서버 진입점

분석 미완료 — 시간 예산으로 일부 조사 생략

### 데이터와 외부 의존성

분석 미완료 — 시간 예산으로 일부 조사 생략

### 설정과 제약

분석 미완료 — 시간 예산으로 일부 조사 생략


## 문서 요약

- vss_server는 레포를 인덱싱하고 질문에 출처와 함께 답하며, 인덱싱 완료 후 프로젝트 브리핑을 생성하는 서버로 설명됩니다. [1]
- 인덱싱 경로는 파일 수집, AST 청킹, bge-m3 임베딩, 저장소 저장, BM25 역색인 생성, 브리핑 생성 순서로 동작한다고 문서화되어 있습니다. [2]
- 질의 경로는 벡터 검색, 선택적 BM25 섞기, 심볼 부스트, 임계값 기반 근거 판정, 프롬프트 렌더링, Ollama 스트리밍, 출처 확정 순서로 동작한다고 문서화되어 있습니다. [2]
- 브리핑은 파일·설정·진입점·함수 등을 조사하고, 문서와 코드에서 주제를 선정한 뒤 원문을 읽어 분석하며, 마지막에 개요를 생성한다고 설명됩니다. [7]
- 브리핑의 근거 검증은 문장 단위로 수행되며, 근거 ID가 틀린 문장은 제외되고 `partial` 상태로 기록된다고 문서화되어 있습니다. [7]
- EC2 설치 절차는 git clone, setup_ec2.sh 실행, venv 활성화, .env 소싱, health 체크 순서로 제시되어 있습니다. [5]
- Python 코드는 AST를 활용해 정의와 호출 후보를 추출하며, 다른 언어는 텍스트 조사 방식으로 처리하고 제한 사항을 기록합니다. [8]
- EC2에서 기존 인덱스를 사용해 브리핑을 생성하려면 `python -m vss.cli briefing` 명령을 사용하며, 생성 모델이 이미 로드되어 있어야 합니다. [8]
- 비동기 브리핑 요청은 202를 반환하며, `queued`, `running`, `ready`, `failed` 상태를 조회할 수 있습니다. [8]
- 평가 문항은 `suites/*.jsonl`에 저장되며, 실행 매트릭스는 `matrices/*.json`에 정의되어 있습니다. [9]
- Snapshot Backend는 `vss_server/main`의 exact SHA로 배포된 VSS HTTP API를 사용하는 기준 문서 체계를 갖추고 있습니다. [11]
- 브리핑 생성 과정은 실제 모델(qwen3.8:27b)로 3회 실행되며, 시간 예산과 LLM 호출 제한에 따른 실패 및 보완 라운드 진입이 관찰되었습니다. [15][16][17]
- RAG 평가 문서에서 config.py의 top_k 기본값이 8인 반면 평가 matrix에는 4가 사용된다고 명시하며, 이는 저장된 실험 결과와 코드 기본값의 차이를 설명하는 것이다. [19]
- RAG 평가 문서는 top_k를 8로 넓혔을 때의 정확도 수치를 저장된 passed_paths와 순위로 추정한 값이며, 실제 서비스 정확도나 생성 답변 정확도로 인용하지 말라고 경고한다. [19]
- RAG 평가 문서는 원본 결과 확인 및 후속 실행을 위한 vss.eval 명령어와 matrix 실행 방법을 제시하지만, 해당 명령이 작성 작업 중 실제로 실행되었다는 뜻은 아니라고 명시한다. [20]
- 목표 폴더 구조 문서는 현재 존재하는 구현 범위를 Phase 1부터 Phase 7B-2까지의 backend features, admin, alembic migration 등으로 나열하고 있다. [22]
- 목표 폴더 구조 문서는 vss_server/main 소유의 RAG runtime과 문서는 module에서 수정 금지이며, module 경로는 Snapshot Backend 변경분 전용이라고 규정한다. [23]
- materialization 저장 구조는 SNAPSHOT_MATERIALIZATION_ROOT 하위에 safe-vss-project-key, staging, revisions 디렉터리를 사용하며, raw project_id를 직접 사용하지 않는다. [24]

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
- module/admin_web/__main__.py와 module/admin_web/app.py의 진입점 및 동작은 제공된 증거에 없습니다.
- module/backend/app.py의 진입점 및 동작은 제공된 증거에 없습니다.
- HMAC 서명의 정확한 생성 알고리즘과 검증 로직
- 세션 토큰의 생성 및 저장 방식
- 백엔드 API의 구체적인 응답 처리 및 오류 처리 로직
- create_app에서 구체적으로 어떤 라우터들이 include_router로 포함되는지 제공된 증거에 명시되지 않았습니다.
- api_prefix가 라우터 등록 시 실제로 어떻게 적용되는지에 대한 코드가 제공되지 않았습니다.
- 0003_add_workspace_binding_identifier.py 파일의 구체적인 DDL 변경 내용은 제공된 증거에 포함되어 있지 않습니다.
- SQLAlchemy 모델 구조에 대한 직접적인 코드 증거가 제공되지 않았습니다.
- restart_module_services.sh 스크립트 내부에서 어떤 Python 인터프리터가 사용되는지 확인되지 않았습니다.
- CLI 및 서버 진입점, 데이터와 외부 의존성, 설정과 제약에 대한 분석이 미완료되었습니다.
- vss/eval/__main__.py의 구체적인 실행 인자나 세부 동작은 제공된 근거에 포함되어 있지 않습니다.
- 예산 때문에 읽지 않은 문서 절이 580개 있습니다.
- Python 이외 코드 파일 3개는 본문 검색만 했습니다 (구조 추출 없음).
- 부분 결과입니다 — 문서 조사 시간 몫 도달, 시간 예산으로 일부 조사 생략. 자세한 내용은 실행 기록(analysis.json)에 있습니다.

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
- [17] `docs/BRIEFING_TUNING_20260909.md`:L123-130
- [19] `docs/RAG_EVALUATION_20260907.md`:L132-144
- [20] `docs/RAG_EVALUATION_20260907.md`:L209-231
- [22] `module/docs/agent/03_TARGET_STRUCTURE.md`:L22-58
- [23] `module/docs/agent/03_TARGET_STRUCTURE.md`:L59-109
- [24] `module/docs/agent/03_TARGET_STRUCTURE.md`:L169-185
- [75] `vss/cli.py`:L243-273
- [77] `vss/eval/__main__.py`:L14-56
- [78] `brief.md`:L106-115
- [79] `vss/server.py`:L507-546
- [80] `README.md`:L188-194
- [81] `module/admin_web/app.py`:L28-30
- [83] `module/docs/architecture/ADMIN_SERVICE_RESTART.md`:L37-84
- [84] `module/tests/unit/admin/test_admin_web_proxy.py`:L71-106
- [85] `module/admin_web/app.js`:L1988-1991
- [86] `module/README.md`:L81-112
- [87] `module/backend/app.py`:L37-102
- [88] `module/admin_web/app.py`:L91-170
- [89] `module/backend/core/config.py`:L76-82
- [91] `module/alembic/versions/0001_initial_snapshot_schema.py`:L24-99
- [92] `module/docs/agent/08_CODE_REVIEW_AND_CONFORMANCE.md`:L103-115
- [94] `module/alembic/versions/0002_harden_snapshot_persistence.py`:L28-136
- [97] `module/ops/service_restart/vss-module-ops.service`:L1-14
- [98] `module/scripts/preflight_ubuntu_runtime.sh`:L1-42
