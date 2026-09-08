# vss_server

## 이 프로젝트는

vss_server는 VSCode Extension(VSsVscodeEX)의 백엔드 서버로, 레포지토리를 인덱싱하여 질문에 출처와 함께 답변하고 프로젝트 브리핑을 생성하는 RAG 시스템입니다. [1]
개발자는 팀 GPU 노드(EC2)에서 이 서버를 실행하여 코드베이스를 검색 및 분석하며, VSCode Extension은 이 서버의 API를 호출하여 기능을 제공합니다. [1]
외부 의존성은 `chromadb`와 `psycopg`뿐이며, 표준 라이브러리 HTTP 서버로 동작합니다. [1]

## README 요약

vss_server는 VSCode 확장 프로그램(VSsVscodeEX)을 위한 백엔드 서버로, 코드 저장소를 인덱싱하여 질문에 출처를 포함해 답변하는 RAG 시스템을 제공합니다. 팀 GPU 노드에서 Ollama와 PostgreSQL(pgvector)을 활용하여 임베딩 및 생성 모델을 처리하며, 표준 라이브러리 HTTP 서버로 동작합니다. 주요 사용자는 코드베이스를 분석하고 문서화된 답변을 필요로 하는 개발자 및 팀입니다.

## README에서 찾은 핵심 기능

### 코드 인덱싱 및 RAG 검색
레포지토리를 AST 청킹 및 BM25 방식으로 인덱싱하여, 질문에 대한 관련 코드 조각을 검색하고 출처와 함께 답변을 생성합니다. Python 파일은 AST 단위로 청킹되며, 임베딩은 bge-m3 모델을 사용합니다. 저장소는 Chroma 또는 pgvector를 지원합니다.
- 코드 검색어: `AST 청킹`, `bge-m3`, `Chroma`, `pgvector`, `BM25`, `RAG`, `인덱싱`
- 시작점: `vss/chunker.py:L290` (chunk_code_ast) - Python 파일을 AST 단위로 청킹하는 핵심 함수
- 핵심 구현: `vss/chunker.py:L290` (chunk_code_ast) - AST 기반 청킹 로직 구현, `vss/embedder.py:L1` ((module docstring)) - bge-m3 임베딩 호출 로직 설명, `vss/config.py:L114` (Config) - 임베딩 모델 및 저장소 설정 정의
- 다음에 읽을 파일: `vss/embedder.py:L1` ((module docstring)) - 임베딩 생성 로직 상세 확인, `vss/config.py:L114` (Config) - 전체 설정 구조 확인
- 처리 흐름: Python 파일을 AST 단위로 청킹 (chunk_code_ast) -> bge-m3 모델을 사용하여 임베딩 생성 -> Chroma 또는 pgvector에 인덱싱 저장 -> BM25 및 벡터 검색을 통해 관련 코드 조각 검색
- 관련 reference file: [R1] `vss/chunker.py`, [R2] `docs/RAG_BASELINE_20260827.md`, [R3] `README.md`, [R4] `vss/embedder.py`, [R5] `vss/config.py`, [R6] `SALVAGE.md`, [R7] `CHARTER.md`, [R8] `vss/store/chroma.py`

### LLM 기반 스트리밍 답변
Ollama를 통해 LLM 모델을 호출하여 질문에 대한 답변을 SSE(서버-사이드 이벤트)로 스트리밍 전송합니다. qwen2.5-coder:7b 모델을 기본 생성 모델로 사용하며, Ollama API의 /api/chat 엔드포인트를 통해 요청을 처리합니다.
- 코드 검색어: `Ollama`, `qwen2.5-coder`, `SSE`, `스트리밍`, `POST /v1/chat`
- 시작점: `vss/llm.py:L22` (_request) - Ollama의 /api/chat 엔드포인트로 요청을 보내는 핵심 함수
- 핵심 구현: `vss/llm.py:L22` (_request) - Ollama API 호출 및 에러 처리, `vss/config.py:L117` (Config.ollama_url) - Ollama 서버 URL 설정, `vss/config.py:L124` (Config.chat_model) - 사용할 LLM 모델 이름 설정
- 관련 설정: `vss/config.py:L117` (Config.ollama_url) - Ollama 서버 주소 지정, `vss/config.py:L124` (Config.chat_model) - 사용할 LLM 모델 지정
- 다음에 읽을 파일: `vss/llm.py:L22` (_request) - LLM 요청 처리 로직 확인
- 처리 흐름: Ollama 서버 URL과 모델 이름을 설정에서 가져옴 -> Ollama의 /api/chat 엔드포인트로 HTTP 요청을 보냄 -> 응답을 스트리밍으로 처리하여 전송
- 관련 reference file: [R4] `vss/embedder.py`, [R9] `vss/llm.py`, [R5] `vss/config.py`, [R10] `scripts/setup_ec2.sh`, [R11] `vss/cli.py`, [R7] `CHARTER.md`, [R12] `vss/lexical.py`, [R13] `tests/test_roundtrip.py`, [R3] `README.md`, [R14] `vss/server.py`

### 프로젝트 브리핑 생성
인덱싱이 완료된 후 프로젝트에 대한 요약 브리핑을 Markdown 형식으로 생성합니다. 결정적 추출(analysis.py)과 LLM 요약을 결합하여 '이 프로젝트는', '문서 요약', '진입점', '기능 목록', '아키텍처(Mermaid)' 등의 섹션을 구성합니다. 생성된 브리핑은 JSON 또는 Markdown 원문으로 API를 통해 제공되며, 캐시된 결과가 있으면 즉시 반환합니다.
- 코드 검색어: `프로젝트 브리핑`, `Markdown`, `briefing`, `POST /briefing`
- 시작점: `vss/briefing.py:L290` (build) - 브리핑 생성의 핵심 로직을 수행하는 함수로, 수집, 분석, LLM 요약, 조립, 저장을 담당합니다., `vss/indexer.py:L158` (_run) - 인덱싱 완료 후 on_done 훅을 통해 브리핑 생성을 트리거하는 진입점입니다.
- 핵심 구현: `vss/briefing.py:L290` (build) - 수집된 자료를 바탕으로 LLM을 호출하여 개요 및 문서 요약을 생성하고 최종 Markdown을 조립합니다., `vss/briefing.py:L101` (SYSTEM) - LLM에게 한국어 Markdown으로 브리핑을 작성하도록 지시하는 시스템 프롬프트를 정의합니다., `vss/server.py:L66` (Handler._send_text) - 생성된 Markdown 브리핑을 HTTP 응답으로 전송하는 역할을 합니다., `vss/analysis.py:L22` (ENTRY_MARKERS) - 진입점 탐지를 위해 사용되는 마커 목록을 정의하여 결정적 분석에 기여합니다.
- 처리 흐름: 인덱싱 완료 후 on_done 훅이 발동되어 브리핑 생성이 시작됩니다. -> collect()를 통해 프로젝트 자료(materials)와 분석 결과를 수집합니다. -> LLM을 호출하여 프로젝트 개요(overview)를 생성합니다. -> 각 문서에 대해 LLM을 호출하여 문서 요약(doc summaries)을 생성합니다. -> assemble()를 통해 수집된 데이터와 LLM 생성 텍스트를 결합하여 Markdown 브리핑을 조립합니다. -> save()를 통해 생성된 브리핑을 저장하고 결과를 반환합니다.
- 관련 reference file: [R15] `vss/briefing.py`, [R16] `docs/API.md`, [R7] `CHARTER.md`, [R17] `vss/indexer.py`, [R14] `vss/server.py`, [R18] `vss/analysis.py`, [R13] `tests/test_roundtrip.py`, [R11] `vss/cli.py`

### 멀티스토리지 지원 (PostgreSQL/Chroma)
이 기능은 기본적으로 PostgreSQL + pgvector를 벡터 저장소로 사용하며, 환경변수를 통해 Chroma로 전환할 수 있는 유연한 구조를 제공합니다. PostgreSQL 저장 계층은 `rag` 스키마를 기반으로 상태 관리(빌딩, 활성, 은퇴, 실패)를 수행합니다. Chroma는 코드에 남아있어 `VSS_STORE=chroma` 설정으로 언제든 복귀할 수 있습니다.
- 코드 검색어: `PostgreSQL`, `pgvector`, `Chroma`, `VSS_STORE`, `저장소`
- 시작점: `vss/store/pgvector.py:L67` (PgVectorStore) - PostgreSQL + pgvector 저장 계층의 핵심 클래스로, 벡터 저장 및 검색 로직을 구현합니다.
- 핵심 구현: `vss/store/pgvector.py:L67` (PgVectorStore) - PostgreSQL + pgvector 기반 저장소 구현체, `vss/store/pgvector.py:L63` (_vec) - 벡터 값을 pgvector 형식의 문자열로 변환하는 유틸리티 함수, `vss/store/pgvector.py:L84` (PgVectorStore._conn) - PostgreSQL 데이터베이스 연결을 관리하는 메서드, `scripts/setup_ec2.sh:L1` - PostgreSQL 및 pgvector 확장 설치와 DB 초기화를 수행하는 설정 스크립트
- 관련 테스트: `README.md:L396` - PostgreSQL이 떠 있을 때 `VSS_TEST_STORE=pgvector` 환경변수를 사용하여 왕복 테스트를 실행하는 방법을 안내합니다.
- 다음에 읽을 파일: `vss/store/pgvector.py:L67` (PgVectorStore) - 저장소 클래스의 구체적인 메서드 구현과 상태 관리 로직을 확인하기 위해
- 처리 흐름: PostgreSQL + pgvector 저장 계층이 `rag` 스키마를 사용하여 벡터 데이터를 관리합니다. -> 상태는 `building`, `active`, `retired`, `failed` 중 하나로 유지되며, `promote()`는 트랜잭션 하나로 상태를 승격합니다. -> 환경변수 `VSS_STORE=chroma`를 설정하면 Chroma 저장소로 전환하여 사용할 수 있습니다.
- 관련 reference file: [R10] `scripts/setup_ec2.sh`, [R19] `vss/store/pgvector.py`, [R3] `README.md`, [R2] `docs/RAG_BASELINE_20260827.md`, [R20] `docs/JOURNAL.md`, [R8] `vss/store/chroma.py`

### 평가 및 측정 도구
확인 필요: 검색된 코드 근거가 없습니다.
- 코드 검색어: `vss.eval`, `평가`, `측정`, `fastapi-cli`, `gold 문항`
- 관련 reference file: [R21] `vss/eval/suite.py`, [R22] `vss/eval/sweep.py`, [R3] `README.md`, [R23] `vss/eval/runner.py`, [R2] `docs/RAG_BASELINE_20260827.md`, [R24] `scripts/make_status.py`

## 코드에서 확인되지 않은 README 기능

- 평가 및 측정 도구: 확인 필요

## Reference files

| ID | 파일 | 줄 | 심볼 | 검색어 |
|---|---|---|---|---|
| R1 | `vss/chunker.py` | L1-11, L290-317 | `(module docstring)`, `chunk_code_ast` | `AST 청킹` |
| R2 | `docs/RAG_BASELINE_20260827.md` | L173-184, L134-148 | - | `AST 청킹` |
| R3 | `README.md` | L45-73 | - | `AST 청킹` |
| R4 | `vss/embedder.py` | L1-12 | `(module docstring)` | `bge-m3` |
| R5 | `vss/config.py` | L114-116, L118-118 | `Config`, `Config.embed_model` | `bge-m3` |
| R6 | `SALVAGE.md` | L46-52 | - | `bge-m3` |
| R7 | `CHARTER.md` | L28-39 | - | `bge-m3` |
| R8 | `vss/store/chroma.py` | L32-32, L1-11 | `ChromaStore`, `(module docstring)` | `Chroma` |
| R9 | `vss/llm.py` | L22-33 | `_request` | `Ollama` |
| R10 | `scripts/setup_ec2.sh` | L70-101, L1-34 | - | `Ollama`, `SSE` |
| R11 | `vss/cli.py` | L37-63 | `cmd_health` | `Ollama` |
| R12 | `vss/lexical.py` | L77-84 | `BM25.__init__` | `qwen2.5-coder` |
| R13 | `tests/test_roundtrip.py` | L179-203 | `RoundTrip.test_06_eval_runner` | `qwen2.5-coder` |
| R14 | `vss/server.py` | L72-83 | `Handler._sse` | `SSE` |
| R15 | `vss/briefing.py` | L1-16, L290-307, L101-106 | `(module docstring)`, `build`, `SYSTEM` | `프로젝트 브리핑`, `Markdown` |
| R16 | `docs/API.md` | L89-96 | - | `프로젝트 브리핑` |
| R17 | `vss/indexer.py` | L158-169 | `_run` | `프로젝트 브리핑` |
| R18 | `vss/analysis.py` | L22-26 | `ENTRY_MARKERS` | `Markdown` |
| R19 | `vss/store/pgvector.py` | L1-14, L67-67, L63-64, L84-85, L87-90 | `(module docstring)`, `PgVectorStore`, `_vec`, `PgVectorStore._conn`, `PgVectorStore.ensure_schema` | `PostgreSQL`, `pgvector` |
| R20 | `docs/JOURNAL.md` | L12-35 | - | `PostgreSQL` |
| R21 | `vss/eval/suite.py` | L15-15, L16-19 | `ValidationError`, `ValidationError.__init__` | `vss.eval` |
| R22 | `vss/eval/sweep.py` | L102-103, L28-37, L89-90 | `nearest`, `load_run`, `sweep_cell` | `vss.eval` |
| R23 | `vss/eval/runner.py` | L1-25 | `(module docstring)` | `평가` |
| R24 | `scripts/make_status.py` | L1-6 | `(module docstring)` | `평가` |

## 문서 요약

- **`README.md`** — 이 문서는 vss_server 프로젝트의 구성, 코드 구조, 구현 현황, 사용법, 그리고 팀의 작업 방식을 한 파일에 담은 인수인계 문서입니다. [1]
신입 개발자는 이 문서를 통해 서버가 어떻게 동작하는지, 어떤 기술 스택을 사용하는지, 그리고 코드를 수정하고 배포하는 표준 프로세스를 파악할 수 있습니다. [1]
또한 프로젝트의 목표와 불변 조건을 앵커로 삼는 CHARTER.md와의 관계, 그리고 EC2 환경에서의 구체적인 운영 방식을 이해하는 데 필수적인 정보를 제공합니다. [1]
- **`evaluation/README.md`** — 이 문서는 `evaluation/` 디렉토리의 질문 suite와 실험 matrix 구조, 평가 실행 및 결과 커밋 프로세스, 그리고 코퍼스 제외 규칙을 정의합니다 [7]. 신입 개발자는 `api_test` 레포에서 `tests/`와 `admin/` 파일이 인덱스에서 제외되므로 이 영역을 정답으로 하는 문항을 작성하면 안 된다는 제약사항을 이해해야 합니다 [7]. 또한 인덱스 이름 규칙과 검색 프로필 설정을 정확히 적용해야만 실험 결과의 비교가 유효하게 이루어집니다 [7].
- **`docs/API.md`** — 이 문서는 vss_server 의 API 계약, 특히 `POST /v1/chat` 엔드포인트의 요청/응답 구조와 스트리밍 이벤트 순서를 정의하고 있습니다 [8]. 신입 개발자는 `project_id` 가 인덱스 매핑과 별개로 동작하는 방식, 근거 기반 응답의 `references` 및 `metadata` 구조, 그리고 `NO_EVIDENCE` 처리 로직을 이해해야 클라이언트 연동과 디버깅을 정확히 수행할 수 있습니다 [8].
- **`docs/JOURNAL.md`** — 이 문서는 프로젝트의 설정값과 기술적 결정이 왜 그렇게 정해졌는지를 '행동 → 결과 → 판단'의 흐름으로 기록하는 팀용 일지입니다. [9]
신입 개발자는 서버 설정값의 근거나 특정 기술 선택(예: pgvector 전환, 임계값 0.54 유지)의 배경을 이해하고, 향후 정확도 개선 작업의 기준선이 되는 초기 측정 결과를 파악하기 위해 이 문서를 읽어야 합니다. [9]
- **`docs/RAG_BASELINE_20260827.md`** — 이 문서는 2026-08-27 기준 RAG 시스템의 설정값이 왜 그 값인지 근거와 함께 해석한 기록이며, 수치의 정본은 `data/evaluation/runs/*.json` 파일입니다 [10].
신입 개발자는 RAG의 동작 원리, 핵심 용어, 그리고 저장소 선택 및 측정 모드의 판단 근거를 이해하여 프로젝트의 기술적 맥락을 파악할 수 있어야 합니다 [10].
이 문서는 RAG를 처음 보는 팀원을 위해 기초 개념부터 실제 수행한 측정 단계까지 체계적으로 설명하여, 검색 품질과 임계값 설정의 의미를 정확히 이해하는 데 도움을 줍니다 [10].
- **`CHARTER.md`** — 이 문서는 vss_server 프로젝트의 목표, 범위, 불변 조건, 역할 분담, 마감 일정을 정의하는 앵커 문서로, 프로젝트의 방향성과 핵심 제약 사항을 파악하는 데 필수적입니다. [11] 신입 개발자는 이 문서를 통해 프로젝트의 핵심 기능인 RAG 완성도 향상, 스냅샷, 브리핑 생성의 구체적인 범위와 하지 않는 것들을 명확히 이해할 수 있습니다. [11] 또한 임베딩 모델, 인덱싱 방식, 프롬프트 형식 등 변경 시 DECISIONS 문서에 기록해야 하는 불변 조건을 숙지하여 개발 과정에서 핵심 규칙을 위반하지 않도록 하는 기준이 됩니다. [11]
- **`SALVAGE.md`** — SALVAGE.md는 rag_lab 프로젝트에서 현재 레포지토리로 가져온 파일들의 원본, 복사 방식, 그리고 그 안에 담긴 핵심 사고와 계약 사항을 상세히 기록한 문서입니다 [12]. 신입 개발자는 이 문서를 통해 어떤 코드가 기존 프로젝트에서 유래했는지, 어떤 부분이 수정되거나 새로 작성되었는지, 그리고 어떤 기능은 의도적으로 폐기되었는지를 명확히 파악하여 프로젝트의 설계 의도와 변경 이력을 정확히 이해할 수 있습니다 [12].
- (예산 때문에 요약하지 않은 문서: `requirements.txt`)

## 진입점

| 파일 | 판정 근거 |
|---|---|
| `vss/cli.py` | 파일명 규칙(cli.py) · 최상위 근처 · 'if __name__ == · def main · argparse.ArgumentParser' 포함 |
| `vss/server.py` | 파일명 규칙(server.py) · 최상위 근처 · 'if __name__ == · def main · argparse.ArgumentParser' 포함 |
| `vss/eval/__main__.py` | 파일명 규칙(__main__.py) · 'if __name__ == · def main · argparse.ArgumentParser' 포함 |
| `scripts/make_status.py` | 최상위 근처 · 'if __name__ == · def main · argparse.ArgumentParser' 포함 |
| `vss/eval/convert_gold.py` | 'if __name__ == · def main · argparse.ArgumentParser' 포함 |

## 진입점별 함수 목록

### `vss/cli.py`
- L31 `def _onoff(v: str | None) -> bool | None`
- L37 `def cmd_health(a) -> int`
- L66 `def cmd_projects(a) -> int`
- L85 `def cmd_index(a) -> int`
- L114 `def cmd_status(a) -> int`
- L119 `def cmd_search(a) -> int`
- L139 `def cmd_ask(a) -> int`
- L166 `def cmd_briefing(a) -> int`
- L188 `def cmd_bm25(a) -> int`
- L193 `def cmd_repair(a) -> int`
- L204 `def cmd_doctor(a) -> int`
- L229 `def main(argv=None) -> int`

### `vss/server.py`
- L42 `def _briefing_hook(model: str | None)`
- L43 **`_briefing_hook.cb`** — `def cb(project_id: str, root: str, commit: str | None) -> dict`
- L48 `class Handler(BaseHTTPRequestHandler)`
- L52 **`Handler._headers`** — `def _headers(self, code: int, ctype: str, length: int | None=None)`
- L60 **`Handler._send`** — `def _send(self, code: int, payload: dict)`
- L66 **`Handler._send_text`** — `def _send_text(self, code: int, text: str, ctype: str='text/markdown')`
- L72 **`Handler._sse`** — `def _sse(self, events)`
- L85 **`Handler._body`** — `def _body(self) -> dict`
- L95 **`Handler._auth_ok`** — `def _auth_ok(self) -> bool`
- L101 **`Handler.log_message`** — `def log_message(self, fmt, *args)`
- L104 **`Handler.do_OPTIONS`** — `def do_OPTIONS(self)`
- L109 **`Handler.do_GET`** — `def do_GET(self)`
- L163 **`Handler.do_POST`** — `def do_POST(self)`
- L267 `def main(argv=None)`

### `vss/eval/__main__.py`
- L14 `def main(argv=None) -> int`

### `scripts/make_status.py`
- L24 `def pct(v)`
- L28 `def num(v)`
- L32 `def main(argv=None) -> int`

### `vss/eval/convert_gold.py`
- L27 `def parse_gold_md(text: str) -> list[dict]`
- L57 `def convert(src: Path, dst: Path, prefix: str) -> int`
- L75 `def main(argv=None) -> int`

## 기능 목록

- 레포지토리를 AST 청킹, bge-m3 임베딩, BM25로 인덱싱하여 저장합니다. [1]
- 질문에 대해 Ollama를 통해 스트리밍 방식으로 출처와 함께 답변을 생성합니다. [1]
- 인덱싱이 완료되면 프로젝트 브리핑(Markdown)을 자동 생성합니다. [1]
- VSCode Extension이 `POST /v1/chat`을 호출하여 대화형 질의응답을 수행합니다. [1]
- `GET /projects`로 완성된 인덱스 목록을 조회하여 프로젝트 ID를 선택합니다. [1]
- `POST /search`를 통해 벡터 및 BM25 기반 검색을 수행합니다. [1]
- `POST /index`를 호출하여 새 레포지토리 또는 변경 사항을 인덱싱합니다. [1]
- `GET /briefing.md`로 생성된 프로젝트 브리핑의 Markdown 원문을 조회합니다. [1]
- `python -m vss.cli` 명령줄 도구를 통해 health, projects, index, search, ask, briefing 등 관리 작업을 수행합니다. [3]
- `python -m vss.eval`을 사용하여 평가 매트릭스 검증, 실행, 보고서 생성 및 임계값 스윕을 수행합니다. [5]
