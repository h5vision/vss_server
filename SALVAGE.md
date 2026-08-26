---
문서: SALVAGE
상태: 기록 (2026-08-26)
역할: rag_lab 에서 무엇을 어떻게 가져왔는지. md 결정("복사 6개는 수용하되 내역을 따로 기록")에 따른 문서.
---

# SALVAGE — rag_lab 에서 가져온 것

원본: `C:\Pj\rag_lab` (2026-08-23 상태, git tag 로 동결 권장). 아래 표의 "담고 있는 사고"는 rag_lab 문서(AGENTS·DECISIONS)에 기록된 실제 사건입니다.

| 새 파일 | 원본 | 방식 | 담고 있는 사고·계약 | 바꾼 것 |
|---|---|---|---|---|
| `vss/references.py` | `vss_rag/references.py` | 그대로 복사 | `[N]` 파싱, 인용된 것만 남길 때 `n` 재부여 금지(점프 링크 깨짐), 인용 0건이면 전부 유지 | 없음 |
| `vss/prompt.py` | `vss_rag/searcher.py` 의 `render_prompt`·`is_no_evidence`·`finalize` | 발췌 복사 | 근거 헤더 3분기, `NO_EVIDENCE` 한 줄 계약, 근거 없을 때 문구 | 선택 코드 블록 추가, RAG 끔용 시스템 프롬프트 추가, P 호환 `source` 변환 추가 |
| `vss/search.py` | `vss_rag/searcher.py` 의 `search` | 발췌 복사 | "판정은 벡터 점수로만", `top_score` = pool 최대 벡터 점수 불변식, RRF 는 순서만 | retrieval_profile·MMR·reorder 제거, BM25 파일 mtime 캐시 추가, `embed_text` 인자 추가 |
| `vss/chunker.py` (문서 부분) | `vss_rag/chunker.py` 의 `chunk_doc`·`collect_files`·`_read` | 수정 복사 | 헤딩 섹션 분할, 제외 규칙 순서 | fenced code block 안의 `#` 을 헤딩으로 오인하던 결함 수정(CHUNKING_AUDIT §1), 본문 없는 상위 제목을 섹션 경로로 승격(제안 ⓒ) |
| `vss/chunker.py` (코드 부분) | spike 노트북 v0.5 셀 6·7 (`chunk_python_file`) | 이식 | AST 단위(모듈 docstring·상수·함수·메서드·클래스 docstring) 청킹, 3,500자 초과 시 줄 경계 분할 | 헤더 형식은 노트북의 `[path] symbol` 대신 rag_lab `context_header` 형식 유지(비교 변수 하나만 움직이게), `symbol` 필드 추가, Python 이외·파싱 실패는 줄 윈도우 폴백 |
| `vss/context_header.py` | `vss_rag/context_header.py` | 그대로 복사 | 경로·클래스·함수 이름을 임베딩 텍스트에 넣는 규칙 | 없음 |
| `vss/embedder.py` | `vss_rag/embedder.py` | 거의 그대로 | **폴백 금지**(가짜 임베딩이 조용히 저장된 사고), 개수·차원 검증 | 접속 실패 안내 문구만 (터널 → Ollama) |
| `vss/lexical.py` | `vss_rag/lexical.py` | 거의 그대로 | BM25 + 토크나이저(snake/camel 분해) + RRF, 원자 저장, 문서 수·ID 중복 검증 | 저장 경로만 `CFG.bm25_dir()` |
| `vss/store/chroma.py` | `vss_rag/store.py` | 규약 이식·재작성 | 컬렉션 이름이 곧 상태(`building-`·`-prev`), 3단계 승격, 진단은 컬렉션을 만들지 않음, 페이지 순회 검증 | 인터페이스를 `store/base.py` 로 분리, 증분·snapshot·restore 제거 |
| `vss/briefing.py` (수집 부분) | `vss_rag/briefing.py` 의 진입점·README·설정 수집 규칙과 토큰 예산 | 발췌 | 예산 안에서 우선순위대로 채움 | 생성부는 새로 씀(결정적 추출 + 문서별 요약) |
| `vss/eval/suite.py`·`metrics.py` | `vss_rag/experiments/suite.py`·`metrics.py` | 그대로 복사 | 질문 JSONL 계약(태그·gold 검증), Hit@k·MRR·no-evidence recall | 없음 |
| `vss/eval/runner.py` | `vss_rag/experiments/runner.py` | 축소 재작성 | run·report 이력, 비교 규칙(commit·suite hash·fingerprint) | matrix 가 인덱스 프로필 대신 `project_id` 를 명시, 자동 인덱싱 제거 |
| `evaluation/tags.json`·`schemas/` | `rag_lab/evaluation/` | 그대로 복사 | 태그 어휘 | 없음 |
| `evaluation/suites/fastapi-cli-full.jsonl` | `RAG_TEST_fastapi_cli.md` 61문항 | 변환(`vss/eval/convert_gold.py`) | 답 46 · 없음 15, 줄 범위 유지 | 유형 → 태그 근사 |

새로 쓴 것: `vss/store/pgvector.py`, `vss/indexer.py`(전체 인덱싱만, 상태 파일 없음), `vss/llm.py`, `vss/chat.py`(오케스트레이션·SSE),
`vss/server.py`, `vss/cli.py`, `vss/analysis.py`(진입점·함수 헤더·라우트·import 그래프·Mermaid), `scripts/`.

가져오지 않은 것(폐기 목록은 DECISIONS): incremental·payload_incremental·resume·bm25_overlay·retrieval_profiles·profiles·diagnose·
batch_index_profiles·eval.py(구 하네스)·make_measurements.py·audit·bench·repair 스크립트. 문서 17개는 이력으로만 참조하며,
살아 있는 사실은 CHARTER 불변 조건 7개와 `evaluation/` 계약으로 옮겼다.

## spike v0.5 대조표 — 노트북의 9개 변경이 이 레포에 어떻게 들어왔는가 (2026-08-26 확인)

원본: `RAG_spike_v0_5.ipynb` (requests 고정 코퍼스, sentence-transformers bge-m3, Chroma in-memory, gemma3:4b). 셀 2 의 "v0.5 변경" 아홉 항목 기준.

| # | spike v0.5 | 이 레포 | 상태 |
|---|---|---|---|
| 1 | module/class assignment 청킹 (`chunk_python_file`, 3,500자 초과 시 줄 경계 분할, overlap 300) | `vss/chunker.py` `python_nodes` 의 `assign`·`class_assign` + `chunk_code_ast`. 분할 overlap 은 `chunk_overlap`(기본 150, fingerprint 키) | 반영. 헤더는 `[path] symbol` 대신 rag_lab `context_header` 형식 |
| 2 | `resolve_proxies` 를 proxy 질문 gold 로 추가 | requests 코퍼스 전용이라 해당 없음. gold 는 `fastapi-cli-full`(61)·`rag-lab-v1`(36)·`api-test`(작성 중) | 해당 없음 |
| 3 | true-no-answer 20개 easy/hard negative | 문항 계약에 `none` 유형 있음(fastapi-cli 15, rag-lab 10). 20건 확장은 PLAN 9/1 | 구조만 반영 |
| 4 | top-1 score 외 top-1/top-2 margin·lexical overlap 기록 | run 결과에 문항별 `top_score` 만 기록. margin·overlap 없음 | 미반영 |
| 5 | threshold 후보별 confusion matrix 출력(자동 적용 금지) | 없음. PLAN 9/1 "임계값 재보정(balanced accuracy)" 이 이 자리 — `vss/eval` 에 sweep 명령이 필요 | 미반영 |
| 6 | 선택 코드를 prompt 와 `sources[]` 의 SOURCE 1 로 전달, 검색 질의에 코드 앞 500자 | `vss/chat.py`: 프롬프트에 별도 블록, 검색 임베딩은 질문+코드 앞 400자. 번호는 붙이지 않음 — `[N]`↔contexts 1:1 불변 조건 때문(선택 코드는 검색 결과가 아님) | 반영(형태 다름, [제안 D]) |
| 7 | Ollama load/prompt/generation 시간 분리로 cold/warm 판정 | `vss/llm.py` 가 `prompt_eval_*`·`eval_*`·`total_duration` 을 받아 `ttft_ms`·`gen_ms`·`decode_tok_s` 로 냄. `load_duration` 은 받지 않아 cold/warm 판정 없음 | 부분 |
| 8 | 같은 experiment signature 만 정식 비교, 조건 변경 시 transition report | run 에 commit·suite hash·index fingerprint 기록, 같을 때만 비교(규칙은 `evaluation/README.md`). 자동 report 는 없음 | 다른 형태 |
| 9 | hybrid(BGE-M3+BM25 RRF) 는 비교용, 기본 gate 는 dense | BM25+RRF 는 rag_lab 계승, 판정은 벡터 점수(불변 조건). 기본값은 hybrid on([제안 E]) — 8/27 기준선의 vector/hybrid 셀로 효과 확인 | 반영(기본값 반대) |

실행기 차이: spike 는 sentence-transformers(max_seq 1024, normalize) 로 bge-m3 를 돌렸고 이 레포는 Ollama bge-m3 다. 같은 모델이라도 점수 분포가 같다는 보장이 없으므로 spike 의 경계값(answerable 최저 0.51 / no-answer 최고 0.53)과 rag_lab 에서 나온 0.54 를 직접 비교하지 않는다. 9/1 재보정이 필요한 이유다.
생성 옵션 차이: spike `temperature 0.1 · num_predict 512 · seed 42`, 이 레포 `temperature 0.2 · num_predict 없음 · seed 없음`.
