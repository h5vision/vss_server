# vss_server

VSsVscodeEX 의 서버다. 레포를 인덱싱하고(AST 청킹, bge-m3, Chroma 또는 pgvector, BM25), 질문에 **출처와 함께** 답하고(Ollama 스트리밍),
인덱싱이 끝나면 **프로젝트 브리핑**(Markdown)을 만든다. 표준 라이브러리 HTTP 서버 하나로 동작하고, 외부 의존성은 `chromadb` 와 `psycopg` 뿐이다.
2026-08-26 에 `rag_lab` 을 대체하는 새 레포로 시작했다(가져온 것은 `SALVAGE.md`). 목표, 범위, 불변 조건은 `CHARTER.md` 에 있다.

이 README 는 **인수인계 문서**다. 구성, 코드 구조, 구현 현황(어디까지 됐고 다음이 무엇인지), 사용법을 이 파일 하나로 파악할 수 있게 유지한다.

## 구성 — 어디서 무엇이 도는가

- **서버 한 대**: 팀 GPU 노드 EC2 `hancom-team2-5th`. 주소와 토큰은 md 가 팀 채널로 공유하고 이 파일에는 적지 않는다. 포트 **8200**, `vss-server` systemd 서비스.
  같은 머신의 Ollama(11434)가 임베딩(`bge-m3`, 1024차원)과 생성(`qwen3.8:27b`, 17 GB. 대안 `gpt-oss:20b`. qwen2.5-coder 는 2026-09-06 에 폐기)을 맡는다.
  **서버는 요청 경로에서 모델을 올리지 않는다**(2026-09-05~06). 생성 모델은 Ollama 에 이미 올라온 completion 모델 중에서 고르고 없으면 `503 model_not_loaded` 다.
  모델 상태를 바꾸는 자리는 기동 때 한 번뿐이다 — `bge-m3` 와 `.env` 의 `VSS_CHAT_MODEL` 중 없는 것만 `keep_alive=-1` 로 올리고, 다른 모델은 내리지 않는다. 24 GB GPU 에서 두 모델을 올리면 약 2.5 GiB 가 남는다(2026-09-06 `nvidia-smi`).
- **저장소**: **PostgreSQL + pgvector**(스키마 `rag`)를 쓴다. 8/27 EC2 에서 pgvector 0.8.6, `CREATE EXTENSION`, 왕복 테스트 10/10 을 확인하고 정했다. Chroma(`data/index/`)는 코드에 남아 있고 `VSS_STORE=chroma` 로 언제든 돌아갈 수 있다.
  스냅샷 서비스(P)는 같은 DB 의 `snapshot` 스키마를 쓴다. 두 저장소 모두 새 인덱스를 다 만든 뒤에 바꿔 끼우는 방식(promote)이라, 인덱싱 중에도 기존 인덱스가 서비스된다.
- **데모 코퍼스**: EC2 `~/repos/` 아래에 둔다. 지금은 `api_test`(앱형)와 `fastapi-cli`(61문항 gold, 비교용)가 올라가 있고, **`rag_lab`(문서가 많은 레포)은 아직 올리지 않았다**.
  인덱스 이름은 `<repo>--lines`(기계적 청킹, 비교용)와 `<repo>--ast`(현행)처럼 청킹 방식을 붙인다. 왜 만든 인덱스인지는 `--note` 로 인덱스 자신에 적는다.
  코퍼스 제외 규칙(8/27 확정): api_test 는 `tests,admin/**,.snapshot-admin-backup/**` 를 빼고, 나머지 레포는 공통 기본 제외만 적용한다. 상세와 gold 문항 규칙은 `evaluation/README.md`.
- **클라이언트**: VSCode Extension(K, Y)은 `POST /v1/chat`(SSE) 하나만 부른다. **보내는 `project_id` 는 레포 이름**(`api_test`)이다. 어느 인덱스가 답할지는 서버가 정하고, 응답의 `index_id` 로 알려 준다.
  그래서 RAG 를 개선해 인덱스를 갈아타도 Extension 은 고치지 않는다. 계약은 `docs/API.md`. 브랜치를 나눈 인덱스(`<레포>@<브랜치>--<청커>`)는 `<레포>@<브랜치>` 로 물어야 한다(2026-09-08).
  인덱싱을 넣는 길은 둘이다 — 스냅샷 서비스(P)는 완성된 트리를 `project_root` 로 `POST /index` 에 넘기고, Extension 은 같은 라우트에 git URL(`remote`)+`branch` 를 넘겨 서버가 clone 한다. 여기서는 인덱스 이름을 그대로 쓴다.
  같은 이름으로 다시 넣으면 서버가 **바뀐 파일만** 다시 임베딩한다(증분, 2026-09-08). 넘기는 쪽이 더 보낼 것은 없다.
- **작업 방식**: 코드는 노트북에서 고치고 커밋해서 GitHub 에 올린다. EC2 는 `git pull` 로 받아서 실행만 한다. **EC2 에서 파일을 직접 고치지 않는다.** 고치면 다음 pull 때 충돌하고, 어느 코드로 잰 수치인지 알 수 없게 된다.
  **EC2 는 GitHub 에 push 하지 않는다**(자격증명을 두지 않는다). EC2 가 만드는 것은 둘이고 둘 다 WinSCP 로 노트북에 내려받는다 — 측정 결과 `data/evaluation/`(run 과 report, 수치의 원본. 노트북이 커밋한다)과
  인덱스 목록 `data/ec2/projects.json`(`vss.cli projects --json` 의 출력. **git 밖**이다 — 2026-09-07 결정. 노트북의 같은 경로에 두면 README 상태 구역이 여기서 만들어진다). 절차는 「5. 결과를 노트북으로 가져오기」.
  `.env`(주소, 토큰, DSN)는 git 에 올리지 않고 EC2 에만 둔다. 팀원과 EC2 는 이 README 와 `CHARTER.md` 만 보면 된다.

<!-- config:begin -->

### 코드에서 뽑은 사실 (자동 생성)

| 종류 | 값 |
|---|---|
| HTTP 엔드포인트 | `GET /` · `GET /health` · `GET /v1/health` · `GET /projects` · `GET /v1/projects` · `GET /v1/models` · `GET /index/status` · `GET /index/exists` · `GET /briefing/status` · `GET /briefing` · `GET /briefing.md` · `POST /v1/chat` · `POST /chat` · `POST /search` · `POST /v1/search` · `POST /prompt` · `POST /finalize` · `POST /index` · `POST /briefing` · `POST /bm25` |
| CLI (`python -m vss.cli`) | `health` · `projects` · `index` · `status` · `search` · `ask` · `briefing` · `bm25` · `repair` · `doctor` |
| 평가 (`python -m vss.eval`) | `validate` · `run` · `report` · `runs` · `sweep` |

| 구분 | 환경변수 | 기본값 | 메모 |
|---|---|---|---|
| 임베딩 (불변 조건: bge-m3 · cosine · 폴백 없음) | `VSS_OLLAMA_URL` | `http://127.0.0.1:11434` |  |
|  | `VSS_EMBED_MODEL` | `bge-m3:latest` |  |
|  | `VSS_EMBED_BATCH` | `16` |  |
|  | `VSS_EMBED_TIMEOUT` | `120` |  |
| 생성 모델 (LLM 호출은 이 서버가 직접 합니다) | `VSS_CHAT_MODEL` | `qwen3.8:27b` |  |
|  | `VSS_BRIEFING_MODEL` | `(없음)` | 비면 chat_model |
|  | `VSS_NUM_CTX` | `8192` |  |
|  | `VSS_CHAT_TIMEOUT` | `180` |  |
|  | `VSS_ALLOW_MODEL_OVERRIDE` | `True` |  |
|  | `VSS_THINK` | `(없음)` |  |
|  | `VSS_BRIEFING_THINK` | `false` |  |
|  | `VSS_BRIEFING_TIME_BUDGET` | `600` |  |
|  | `VSS_BRIEFING_DOC_BATCHES` | `6` |  |
|  | `VSS_BRIEFING_KEEP_RUNS` | `3` |  |
|  | `VSS_BRIEFING_CHARS_PER_TOKEN_ASCII` | `2.8` |  |
|  | `VSS_BRIEFING_TOKENS_PER_CHAR_OTHER` | `0.6` |  |
| 청킹 (fingerprint) | `VSS_CHUNKER` | `ast-v3` | ast-v3 / ast-v2 / ast-v1 / line-window-v1 |
|  | `VSS_CHUNK_SIZE` | `1200` |  |
|  | `VSS_CHUNK_OVERLAP` | `150` |  |
|  | `VSS_MIN_CHUNK` | `80` |  |
|  | `VSS_AST_MAX_CHARS` | `3500` |  |
|  | `VSS_CONTEXT_HEADER` | `True` |  |
|  | `VSS_MAX_FILE_BYTES` | `1_000_000` |  |
|  | `VSS_EXCLUDE_GLOBS` | `(없음)` |  |
|  | `VSS_USE_BM25` | `True` |  |
| 검색 (런타임 설정 — 재인덱싱 불필요) | `VSS_TOP_K` | `8` |  |
|  | `VSS_THRESHOLD` | `0.54` |  |
|  | `VSS_FUSION_POOL` | `20` |  |
|  | `VSS_RRF_K` | `60` |  |
|  | `VSS_SYMBOL_BOOST` | `False` |  |
|  | `VSS_SYMBOL_POOL` | `100` |  |
|  | `VSS_RERANK` | `auto` | auto / on / off |
|  | `VSS_PER_FILE_CAP` | `2` | 파일당 앞자리 청크 수. 0 = 무제한 |
|  | `VSS_DEMOTE_GLOBS` | `tests,test,__tests__,test_*.*,*_test.*,*.test.*,*.spec.*` | 뒤로 보낼 경로 |
| 저장 | `VSS_STORE` | `chroma` | chroma / pgvector |
|  | `VSS_DATA_DIR` | `./data` |  |
|  | `VSS_PG_DSN` | `postgresql://vss_rag:vss_rag@127.0.0.1:5432/vss` |  |
|  | `VSS_PG_SCHEMA` | `rag` |  |
|  | `VSS_PG_EXACT` | `False` | 검증용 정확 검색 |
| 서버 | `VSS_TOKEN` | `(없음)` |  |
|  | `VSS_PROJECT_ALIASES` | `(없음)` | 질의 전용: api_test=api-test--ast,... |
|  | `VSS_REPOS_DIR` | `(없음)` |  |
|  | `VSS_QUERYLOG_DSN` | `(없음)` |  |

폴더 구조 (최상위):

```text
.tmp/  presentation-rag-update
.vscode/  dependency-graph.json
docs/  ACCURACY.md, API.md, briefing_tuning_20260909, BRIEFING_TUNING_20260909.md, JOURNAL.md, RAG_BASELINE_20260827.md, RAG_EVALUATION_20260907.md
evaluation/  GOLD_GUIDE.md, matrices, README.md, schemas, suites, tags.json
module/  admin_web, alembic, alembic.ini, backend, docs, GEMINI.md, main.py, ops …
presentation-assets/  code-rag-evolution.png, final-rag-slides, slide-1-previous-rag.png, slide-2-ast-symbol.png, slide-3-current-rag.png
scripts/  backup_pg.sh, db_init.sql, make_status.py, setup_ec2.sh, vss-server.service
tests/  __init__.py, fakes.py, test_analysis.py, test_briefing_pipeline.py, test_chunker.py, test_llm.py, test_rerank.py, test_roundtrip.py …
vss/  __init__.py, analysis.py, briefing.py, briefing_pipeline.py, briefing_survey.py, briefing_upgrader.py, chat.py, chunker.py …
.gitignore
CHARTER.md
README.md
SALVAGE.md
brief.md
requirements.txt
```

<!-- config:end -->

## 데이터가 어디에 쌓이는가

인덱싱할 레포는 **읽기 전용 입력**이다. 서버는 그 레포에 아무것도 쓰지 않는다. 데이터는 전부 서버 쪽(`data/` 또는 PostgreSQL)에 쌓인다.

```text
~/repos/api_test                     ← 입력. 그냥 소스 폴더다
      │
      │   python -m vss.cli index ~/repos/api_test --project api-test--ast
      │
      ├─ chunker      파일 수집(제외 규칙) → AST 로 함수 단위 자르기
      ├─ embedder     Ollama bge-m3 로 청크마다 1024차원 벡터   ← GPU 를 쓰는 유일한 구간
      └─ store        begin_build → add → promote
                            │
      VSS_STORE=chroma ─────┤─────── VSS_STORE=pgvector
                            ▼                     ▼
    ~/vss_server/data/                  PostgreSQL  vss DB · rag 스키마
      index/       벡터 (Chroma)          rag.projects   프로젝트 1행
      bm25/        키워드 역색인 JSON      rag.revisions  인덱싱 1회 = 1행 (status)
      manifests/   파일 해시 (증분 재료)   rag.chunks     청크 1개 = 1행
      briefings/   브리핑 캐시
      evaluation/  측정 결과 ← git 으로 돌아가는 유일한 것      embedding vector(1024), hnsw cosine
      index_log.jsonl
```

### EC2 어느 경로에 무엇이 있나

서버 코드도 코퍼스도 접속 계정의 홈(`~` = `/home/<계정>`) 아래에 나란히 둔다. 홈 밖(`/srv` 등)에 두면 sudo 와 소유권 손질이 필요해지고 WinSCP 로 바로 올리지 못한다.

| 경로 | 무엇 | 누가 만드나 |
|---|---|---|
| `~/vss_server` | 서버 코드. `git pull` 로만 바뀐다 | 1단계 `git clone` |
| `~/vss_server/data/` | 인덱스, BM25, 브리핑, 측정 결과 (`VSS_DATA_DIR`) | 서버가 자동 생성 |
| `~/vss_server/.env` | 주소, 토큰, DSN. git 에 올리지 않는다 | `setup_ec2.sh` |
| `~/repos/<repo>` | 인덱싱할 레포. 서버 코드와 **형제 폴더**로 갈라 둔다 | `setup_ec2.sh` 가 폴더까지, 내용은 2단계 |
| PostgreSQL `vss` DB, `rag` 스키마 | pgvector 를 쓸 때의 벡터 | `setup_ec2.sh` 의 DB 초기화 |

코퍼스를 서버 코드 **안**이 아니라 형제로 두는 이유: 안에 두면 `git pull` 과 `git status` 가 코퍼스를 건드리고, 서버 레포를 인덱싱할 때 자기 자신이 코퍼스에 섞인다.
`evaluation/matrices/*.json` 의 `repository` 는 `~/repos/<repo>` 로 적혀 있고 실행할 때 `~` 를 푼다. 계정 이름이 `ubuntu` 가 아니어도 그대로 통한다.

### 알아야 할 것 넷

- **`--project` 이름이 키다.** `api-test--ast` 는 폴더가 아니라 인덱스 이름이다.
  같은 레포를 설정만 바꿔 두 번 인덱싱하면(`--lines` 기계적 청킹, `--ast` 함수 단위) 두 인덱스가 남고, 같은 질문 suite 로 비교할 수 있다. 기준선 측정이 하는 일이 이것이다.
  인덱스를 늘려야 하는 축은 **fingerprint 에 든 것뿐**이다(청커, 헤더, 제외 규칙, 임베딩 모델 등). `top_k`, `threshold`, BM25 섞기, 생성 모델은 같은 인덱스에 질의할 때 바꾼다. 그래서 임계값을 다시 잡거나 모델을 바꿔도 인덱스는 늘지 않는다.
  왜 만든 인덱스인지는 `--note` 로 인덱스 자신에 적어 둔다. 어떤 설정이었는지는 이름이 아니라 fingerprint 가 기준이다(`cli status --project <이름>`).
- **프론트는 인덱스 이름을 모른다.** Extension 은 레포 이름(`api_test`)을 보내고 서버가 실제 인덱스를 고른다. 순서는 셋이다 —
  `.env` 의 `VSS_PROJECT_ALIASES`(손으로 고정, 언제나 이김) → 그 이름의 인덱스가 실제로 있으면 그대로 → **`<레포>--…` 중 청커 세대가 가장 새것**(같으면 `indexed_at` 최신).
  그래서 새 인덱스를 만들면 설정을 고치지 않아도 그쪽으로 옮겨 간다. 어느 쪽이었는지는 응답의 `resolved_by`(`alias`·`exact`·`auto`), 실제로 답한 인덱스는 `index_id` 다.
  지금 어느 레포 이름이 어느 인덱스에 닿는지는 `GET /projects` 의 `repos` 에 후보 목록까지 나온다. 미완성 빌드는 후보가 아니다. 인덱싱과 평가는 이 선택을 타지 않는다.
  이름에 `--` 가 없으면(변형 없이 한 번만 인덱싱한 레포) 인덱스 이름이 곧 레포 이름이다.
- **`GET /projects` 는 세 가지 뷰를 낸다.** `?view=repos` 는 레포 단위 축약본(인덱싱 안 된 레포까지, `commits=N` 으로 커밋 목록),
  `?only=current` 는 레포마다 지금 답하는 인덱스만(옛 세대를 숨긴다), `?project_id=&files=1&symbols=1` 은 인덱스에 실제로 들어간 파일 목록이다.
  각 인덱스의 `stale` 은 디스크의 현재 HEAD 와 인덱싱 시점 커밋이 다른가이고, `true` 면 다시 인덱싱할 때다.
- **저장 위치는 `VSS_STORE` 하나가 정한다.** 레포 위치와 무관하고, 인덱싱 명령도 똑같다. Chroma 는 `data/index/` 파일, pgvector 는 DB 행이 된다.
- **다 만든 뒤에 바꿔 끼우기(promote)라서 실패해도 서비스가 깨지지 않는다.** 인덱싱 중에는 `building-<이름>` 에 쌓이고, 임베딩이 전부 성공해야 진짜 이름으로 바뀐다.
  중간에 죽으면 기존 인덱스가 그대로 답하고, 실패한 빌드는 증거로 남는다(자동 삭제하지 않는다. 불변 조건 2). pgvector 에서는 같은 일이 `revisions` 행의 상태로 일어난다. `building` 이 `active` 로 바뀌고, 이전 것은 `retired` 가 된다.

## 코드 구조 — 모듈이 하는 일

두 경로만 이해하면 된다.

- **인덱싱 경로**: `POST /index`(또는 CLI `index`)가 `indexer.start_index` 를 부른다. `chunker` 가 파일을 모아 자르고, `embedder` 가 bge-m3 로 벡터를 만들고, `store` 가 `begin_build`, `add`, `promote` 순서로 저장한다(기존 인덱스를 미리 지우지 않고 한 번에 바꾼다). 그 뒤 `lexical` 이 BM25 역색인을 만들고, 완료 훅이 `briefing` 을 만든다.
  같은 이름·같은 설정의 완성 인덱스가 있고 승격 때 남긴 파일 해시(`data/manifests/`)가 저장소와 맞으면 **증분**이다 — `indexer.plan_incremental` 이 바뀐·지운 경로를 고르고, `store.copy_chunks` 가 나머지 청크·벡터를 새 빌드로 복사하고, 바뀐 파일만 `embedder` 를 탄다. 승격 순서는 같다. 증분 뒤에는 브리핑을 만들지 않는다(`briefing: "always"` 면 만든다). `--force` 는 전체.
- **질의 경로**: `POST /v1/chat` 이 `chat.run_chat` 을 부른다. `search` 가 벡터 top-k 를 뽑고(선택적으로 BM25 결과를 RRF 로 섞고, `use_symbols` 면 질문에 나온 심볼을 앞으로 당긴다) 임계값으로 근거 유무를 판정한다. 섞기와 당기기는 **순서만** 바꾸므로 `top_score` 와 근거 유무는 달라지지 않는다. `prompt.render_prompt` 가 근거에 `[N]` 번호를 붙이고, `llm.chat_stream` 이 Ollama 로 스트리밍한다. 마지막에 `prompt.finalize` 와 `references` 가 답 속 `[N]` 을 읽어 출처를 확정한다.

| 모듈 | 책임 | 알아야 할 규칙 |
|---|---|---|
| `vss/config.py` | 모든 설정(`VSS_*` 환경변수)과 인덱스 fingerprint | "청킹" 구분 값을 바꾸면 재인덱싱, "검색" 구분은 재시작만 |
| `vss/chunker.py` | 대상 파일 수집(제외 규칙), AST 청킹(.py), 줄 윈도우(기타 코드), 마크다운 섹션(fence 인식) | `ast-v1` 은 과거 코퍼스 호환용으로 동결, `ast-v2` 는 제어문 아래와 중첩 함수, 클래스까지 수집, `ast-v3` 은 v2 + BOM 파일도 AST 를 탄다(기본). 산출 레코드 필드명은 고정 |
| `vss/rerank.py` | 휴리스틱 재정렬 — 같은 파일 청크는 앞자리에 2개까지, `tests/` 경로는 뒤로 | 순서만 바꾼다. 인덱스 청커가 `ast-v3` 이상이면 자동으로 켜지고(`VSS_RERANK=auto`), 켜진 사실이 `search_profile.rerank` 에 남는다 |
| `vss/context_header.py` | 청크 머리에 경로와 심볼 헤더 부착 (`VSS_CONTEXT_HEADER`) | |
| `vss/embedder.py` | Ollama bge-m3 임베딩 호출 | **폴백 없음.** 실패는 예외로 드러난다 |
| `vss/store/` | `chroma.py`, `pgvector.py`, 공통 계약은 `base.py` | `begin_build`, `add`, `promote` 순서만. `copy_chunks` 는 active 의 청크·벡터를 빌드로 복사만 한다(증분 재료). 인덱스 상태는 저장소 자신이 기준. 청크 메타는 `path`·`type`·`line_*`·`section`·`symbol`·`kind`·`enclosing` |
| `vss/lexical.py` | BM25 역색인(순수 표준 라이브러리)과 RRF 섞기 | 섞기는 순서만 바꾼다. 판정은 벡터 점수 |
| `vss/symbols.py` | 질문에서 심볼 이름 줍기, `symbol` → 청크 색인, 재정렬 (`VSS_SYMBOL_BOOST`) | BM25 와 같다 — 순서만 바꾸고 점수는 건드리지 않는다. 재인덱싱 불필요 |
| `vss/indexer.py` | 인덱싱 파이프라인(전체·**증분**), 진행률(메모리), `data/index_log.jsonl`, `repair`, **레포 이름 → 인덱스 선택**(`resolve_index`·`repo_map`), 목록 재료(`repo_list`·`index_files`·`unindexed_repos`·`git_log`) | 실패한 빌드는 자동 삭제하지 않는다 (증거). 선택은 승격된 인덱스만 후보로 본다. 증분은 같은 이름·같은 fingerprint 의 완성 인덱스 + `data/manifests/` 파일 해시가 저장소와 맞을 때만, 아니면 전체. `--force` 는 전체 |
| `vss/search.py` | 벡터 검색 + BM25 섞기 + 임계값 판정 | `top_score >= threshold` 이면 `has_evidence`. 질의 임베딩은 인덱스가 저장한 fingerprint 의 모델을 쓴다 |
| `vss/prompt.py` | 프롬프트 형식의 기준, NO_EVIDENCE 판정 | `[N]` 은 contexts 인덱스+1 과 1:1. 정렬, 필터, 번호 다시 매기기 금지 |
| `vss/references.py` | 답변의 `[N]` 을 읽어 `references`(청크 단위)와 `reference_files`(파일 단위)를 만든다 | 파일로 묶어도 `n` 은 원래 값 유지 |
| `vss/llm.py` | Ollama `/api/chat` 호출과 스트리밍. `pick_model` — 요청 경로는 **올라온** completion 모델 중에서만 고른다(없으면 `ModelNotLoaded`). `ensure_loaded` — 기동 전용, 없는 목표 모델을 올린다 | 모든 payload 에 `keep_alive=-1`. `.env` 모델 이름이 Ollama 로 가는 자리는 `ensure_loaded` 하나 |
| `vss/chat.py` | `/v1/chat` 오케스트레이션(검색, 프롬프트, LLM, 출처 순서), SSE 이벤트 `meta`, `delta`, `done`, `error` | 히스토리는 받아도 프롬프트에 넣지 않는다(0턴). 근거 없으면 LLM 을 부르지 않는다 |
| `vss/querylog.py` | `/v1/chat` 요청 하나를 `rag.query_log` 한 행으로 (`VSS_QUERYLOG_DSN` 이 비면 아무것도 안 함) | 저장 계층과 분리돼 있다. 기록이 실패해도 답변은 그대로 나간다(stderr 한 줄). `rag:false` 는 남기지 않는다 |
| `vss/server.py` | 표준 라이브러리 HTTP 서버, 전 엔드포인트. 기동 시 `_prepare_models` — Ollama 대기(60초) → `bge-m3` 임베딩 1회 → `ensure_loaded` → 올라온 모델 한 줄 로그. 실패해도 뜬다 | `VSS_TOKEN` 설정 시 전 요청 토큰 검사. `--no-warmup` 은 모델 준비 전체를 건너뛴다 |
| `vss/cli.py` | 서버와 같은 기능의 CLI (`health`, `index`, `search`, `ask`, `briefing`, `doctor`, `repair` 등) | |
| `vss/briefing.py`, `briefing_survey.py`, `briefing_pipeline.py` | 브리핑: 원문 조사(진입점·라우트는 `analysis.py` 의 AST 추출), 문서 요약, 주제별 근거 분석과 보완, 마지막 개요 생성 | 근거 ID 는 문장 단위로 검증(틀린 문장만 제외 → `partial`), 시간 예산 600초, 토큰 어림 계수는 실측값(설정), lock 은 죽은 소유자만 자동 복구, 실행 기록은 최근 3개 + 발행 run. 2026-09-09 조정 기록은 [docs/BRIEFING_TUNING_20260909.md](docs/BRIEFING_TUNING_20260909.md) |
| `vss/briefing_upgrader.py` | (안 쓰는 파일) test-merge 쪽 브리핑 구현이 9/9 merge 로 들어온 것 | 어디서도 import 하지 않는다. 브리핑은 위 세 파일이 맡는다. 지울지는 md 결정 |
| `vss/eval/` | matrix×suite 평가 실행, Hit@k, MRR, no-evidence recall, `data/evaluation/runs`, `reports`, `sweep`(임계값 표) | run 에 fingerprint, commit, suite hash 가 기록된다. 같을 때만 비교한다. `sweep` 은 값을 바꾸지 않는다 |
| `tests/` | 가짜 임베더와 LLM 으로 왕복 테스트, 분석기와 청커 회귀 테스트 (Ollama 불필요) | |
| `scripts/` | `setup_ec2.sh`(EC2 설치), `db_init.sql`, `make_status.py`(STATUS.md 생성), `backup_pg.sh`, systemd 유닛 | |

## 구현 현황과 다음 작업

**지금 단계**: 서버가 EC2 에서 돌고 있고(PostgreSQL + pgvector), 데모 레포 2개가 인덱싱돼 **첫 기준선 수치가 나왔다**(2026-08-27).
2026-08-28 에 **프론트 연동 경로가 실제로 통과했다.** Extension 이 레포 이름(`api_test`)을 보내면 서버가 `api-test--ast` 로 검색한다(응답 `index_id` 로 확인).
같은 날 질의 흐름 결함 7건을 닫았다(출처 목록이 비는 문제 등). 노트북 전체 회귀 테스트와 EC2 pgvector 왕복 검증을 통과했다.
2026-08-30 에는 `ast-v1` 의 누락을 고친 `ast-v2` 를 별도 fingerprint 로 추가했다. 코드는 준비됐지만 EC2 재인덱싱과 재측정은 아직이다.
2026-09-01~02 에 심볼 인식 검색(질의 시 토글), 청크 메타 `enclosing`·`kind` 저장, `project_id` 자동 인덱스 선택이 들어갔다.
2026-09-02 에 질의 로그(`rag.query_log`)를 넣었다 — 질문이 서버를 통과했는지를 로그 파일이 아니라 SQL 로 본다. `.env` 에 `VSS_QUERYLOG_DSN` 을 넣고 질의를 한 번 던지면 테이블이 생긴다.
2026-09-04 에 **`api_test` gold 40문항이 도착해 측정 자가 생겼다** — `evaluation/suites/api-test-v1.jsonl`(답 30 + hard negative 10, 커밋 `2dea3d71` 기준).
두 매트릭스(`api-test`·`fastapi-cli`)에 `--ast-v2` 셀을 넣어, 한 번의 run 이 **줄 윈도우 / ast-v1 / ast-v2 × vector / hybrid** 를 나란히 낸다.
2026-09-04 새벽에 EC2 를 `--ast-v2` 로 재인덱싱하고 두 matrix 를 다시 쟀다 — run `20260904T005826Z-2f0879`(api-test) · `20260904T005910Z-5e6307`(fastapi-cli).
**`api_test` 에서 ast-v2 가 확정됐다**(ast-v1 대비 Hit@3 +16.7%p, 노이즈선의 5배). fastapi-cli 는 1문항 차이라 판정이 안 된다.
같은 날 그 측정에 섞인 결함 넷(두 suite 의 채점 자 불일치, matrix `top_k` 4 vs 서빙 8, BOM 파일 19개, gold 라벨 3건)과 보정값, 아직 안 잰 것을 **[docs/ACCURACY.md](docs/ACCURACY.md)** 에 모았다.
2026-09-05 에 "인덱싱하면 모델이 팅기는" 원인을 잡았다 — 브리핑 훅이 `.env` 의 모델 이름을 Ollama 에 던지면 그것이 로드 요청이 되고, VRAM 이 모자라면 Ollama 가 상주 모델(qwen, bge-m3)을 내린다.
2026-09-06 에 그 길을 닫았다 — 어떤 라우트도 모델을 올리지 않고(`llm.pick_model`, 없으면 `503 model_not_loaded`), 기동 때만 `bge-m3` 와 `VSS_CHAT_MODEL` 중 없는 것을 올린다(`server._prepare_models`, `llm.ensure_loaded`, 모든 요청에 `keep_alive=-1`).
가짜 Ollama 로 HTTP 19경우와 기동 5경우를 확인했고 EC2 반영은 커밋 `d06844f` 이후 pull 이다. 기동 로그에 "올라온 모델 / 임베딩 / 생성 / 결과" 네 줄이 찍힌다.
2026-09-07 에 **`ast-v3` 와 휴리스틱 재정렬**을 넣었다. v3 은 v2 와 노드 추출이 같고 BOM 파일(api_test `.py` 19개)이 줄 윈도우로 떨어지던 것을 AST 로 태운다. 재정렬은 같은 파일 청크를 앞자리에 2개까지만 두고 `tests/` 경로를 뒤로 보내며, 인덱스가 v3 이상일 때만 자동으로 켜져 옛 세대 셀의 수치는 한 run 안에서 그대로 재현된다.
근거는 9/4 run 재집계다 — top-5 에 같은 파일이 두 번 이상 든 문항이 api-test 28/30 · fastapi-cli 35/46, fastapi-cli top-3 자리의 40% 가 테스트 파일인데 gold 가 테스트인 문항은 0. EC2 재인덱싱(`--ast-v3` 2개)과 재측정은 「4-2」다.
무엇을 재서 무엇이 증명됐고 왜 그렇게 정했는지는 **[docs/JOURNAL.md](docs/JOURNAL.md)** 와 [docs/RAG_BASELINE_20260827.md](docs/RAG_BASELINE_20260827.md) 에 있다.

**이어받는 사람이 할 일**: 처음이면 아래 「EC2 실행 순서」 1~5번을 그대로 붙여 넣으면 같은 상태가 된다. 이미 돌고 있는 서버를 이어받는다면 남은 것은 여섯이다.
⓪ **`ast-v3` 재인덱싱과 재측정** — 「4-2」의 블록. 끝나면 자동 선택이 `--ast-v3` 로 옮겨 가고 두 matrix 가 v2 ↔ v3(+재정렬) 을 한 표에 낸다.
① **EC2 에 9/6 코드 반영 확인** — `git pull` 후 `sudo systemctl restart vss-server`, `journalctl -u vss-server -n 15` 에 기동 네 줄(올라온 모델 / 임베딩 / 생성 … 이미 올라옴 / 결과)이 나오고 `ollama ps` 의 두 모델이 `Forever` 인지 (9/9 부터는 그 앞에 "브리핑 정리 …" 한 줄이 더 나올 수 있다 — 지난 프로세스가 죽으며 남긴 브리핑 lock 을 치운 것). 그리고 질의 하나 뒤 `rag.query_log` 에 행이 생기는지(`.env` 의 `VSS_QUERYLOG_DSN` 이 `<pw>` placeholder 였던 것을 9/6 에 채웠다).
② 측정 자 고치기 — `metrics` 에 path-level 지표, matrix `top_k` 를 서빙값 8 로, `chunker.py:66` 의 인코딩 순서(`utf-8-sig` 먼저). 그 뒤 두 matrix 재측정.
③ `rag_lab` 배치와 측정(데모 시나리오 S3, S4 가 여기 걸려 있다) ④ 생성 품질 측정(지금까지 잰 것은 검색까지다 — `vss.eval run` 은 LLM 을 부르지 않는다) ⑤ 스냅샷 연동 마무리 — P 는 완성된 트리를 `project_root` 로, Extension 은 `remote`+`branch` 로 `POST /index` 를 부른다(둘 다 유지, 2026-09-08). `project_id` 이름 규칙은 `<레포이름>@<브랜치>--<청커>`(`--` 뒤는 청커 세대라 브랜치를 직접 넣으면 안 된다)로 확정했다([docs/API.md](docs/API.md) 「스냅샷(P) 연동」).
  남은 것은 셋이다. (a) EC2 에서 pgvector 테스트(`VSS_TEST_STORE=pgvector python -m unittest tests.test_roundtrip -q`)와 같은 레포 두 번 인덱싱으로 두 번째의 `GET /index/status` `index.mode` 가 `incremental` 인지 확인. (b) Extension 의 `remote` 경로는 브랜치가 달라도 `~/repos/<레포>` 한 폴더를 같이 써서(`server._clone_repo`) 앞 인덱싱이 도는 중에 다른 브랜치 요청이 폴더를 바꿀 수 있다 — 브랜치별 폴더로 나눠야 한다. (c) `branch` 를 인덱스 meta 에 따로 담기(지금은 이름에만 있다).
2026-09-08 에 RAG 개선을 멈추고 **스냅샷 연동으로 초점을 옮겨 증분 인덱싱을 켰다**(test-merge 계열에서 만들어 2026-09-09 에 이 브랜치로 옮김). 같은 이름으로 다시 `POST /index` 하면 승격 때 남긴 파일 해시와 비교해 바뀐 파일만 임베딩하고 나머지 청크·벡터는 이전 인덱스에서 복사한다(`store.copy_chunks`). 계약은 그대로이고 스냅샷 서비스가 보낼 추가 필드는 없다 — 스냅샷 쪽이 제안한 변경 목록(delta) API 는 받지 않기로 했다.
증분 뒤에는 브리핑을 만들지 않는다(`briefing: true` 는 전체 때만, `"always"` 는 매번). 인덱스 이름 규칙은 `<레포>@<브랜치>--<청커>` 로 확정했고 Extension 은 `<레포>@<브랜치>` 로 묻는다. pgvector 와 EC2 증분 실행은 아직 확인 전이다. 순서와 근거는 [docs/JOURNAL.md](docs/JOURNAL.md) 2026-09-08 항목.
2026-09-09 에 **브리핑을 실제 모델(qwen3.8:27b)로 세 번 돌려 고쳤다.** 같은 600초 예산 안에서 주제 조사가 4개 → 8개 + 보완 라운드로 늘었고 문서 단계는 212초 → 86초다. 토큰 어림 계수는 실측으로 정해 설정(`VSS_BRIEFING_CHARS_PER_TOKEN_ASCII` 2.8, `VSS_BRIEFING_TOKENS_PER_CHAR_OTHER` 0.6)으로 뺐다. 진입점별 함수 헤더를 본문에 되살렸고 Mermaid 는 뺐다(Extension 이 그린다). 테스트 165/165(Chroma). 회차별 변경과 3회 비교표는 [docs/BRIEFING_TUNING_20260909.md](docs/BRIEFING_TUNING_20260909.md). 남은 것은 api_test 표본 1회, 마지막 수정 뒤 실행 1회, 증분 인덱싱 pgvector 테스트다.
정확도 작업(청킹, 임계값, 모델 교체)은 전부 이 기준선과의 비교로 판정한다. **질문 몇 개를 던져 보고 판단하지 않는다.** 문항 하나가 흔드는 폭이 1/n 이다.

**설정이 없으면 기능도 없다**: 코드가 있어도 `.env` 한 줄이 빠지면 그 기능은 없는 것과 같다(8/28 에 `VSS_PROJECT_ALIASES` 로 겪었다).
"안 된다"는 보고를 받으면 `curl -s localhost:8200/health | jq '{store, projects, project_aliases}'` 를 먼저 본다. 저장소, 인덱스, 별칭이 한 번에 나온다.

<!-- status:begin -->

_이 구역은 자동 생성됩니다 (2026-09-10 08:23 UTC+0900). 손으로 고치지 마세요._

**완료** (최근)

- 코퍼스 규칙 확정 — api_test: `tests,admin/**,.snapshot-admin-backup/**` 제외 후보 / rag_lab: `data`(기본 제외)
- 점심 go/no-go (pgvector) — 네 조건 전부 만족해야 go
- 팀원(gold 담당): `api_test` 40문항 초안 시작 — `evaluation/README.md` 의 계약, `python -m vss.eval validate` 로 자가 검증
- (team) EC2 → 레포 결과 반출 경로 확정
- (Claude Code) 브리핑 진입점 후보에서 테스트 파일을 제외하거나 감점
- 팀원 gold 40문항 1차 완료 → `api-test` matrix 를 full suite 로 교체

**진행 중**

- 데모 레포 배치: `~/repos/api_test`, `~/repos/rag_lab`(동결 사본), `~/repos/fastapi-cli`(대조군)
- 기준선 인덱싱: `<repo>--lines`(줄 윈도우, 헤더 off, BM25 on) 3개
- AST 인덱싱: `<repo>--ast`(ast-v1, 헤더 on, BM25 on) 3개
- 기준선 측정 1회: `python -m vss.eval run evaluation/matrices/{fastapi-cli,rag-lab,api-test}.json --note baseline`
- 코퍼스 동결: 데모 레포 2개의 revision 과 문서 집합 확정, DECISIONS 에 commit 기록. 이후 측정은 이 코퍼스에서만
- 첫 개선 시리즈 보고: baseline → ast+header → hybrid (레포 3개)
- K·Y 에게 `/v1/chat` SSE 계약(docs/API.md) 전달, EC2 주소·토큰 공유
- (Claude Code) 브리핑 v2 를 데모 레포 2개에서 생성해 품질 확인
- (Claude Code) 라우트 표·함수 헤더 목록의 오탐(정규식) 수정222
- 임계값 재보정: 두 레포 hard negative 20건 + 답 있는 문항으로 balanced accuracy 최대점 계산 (0.54 유지/변경 결정은 DECISIONS)
- 질의 로그를 DB 에 남긴다 (질문 통과 확인용)
- `has_evidence=false` 화면·콜드스타트(서버 워밍업)·터널 없는 구조 확인

**다음 작업**

- (team) 다섯 문서 검토·승인 (md) — 완료 조건: CHARTER 와 계획 문서의 "초안" 을 "현행" 으로, 첫 커밋
- (team) gold 담당에게 코퍼스 제외 규칙 전달 (md) — 완료 조건: evaluation/README.md 의 "코퍼스 제외 규칙" 절 링크를 팀 채널에 공유
- 발표에 쓸 "RAG 끔/켬" 비교 질문 3개 고르기 (`rag:false` 플래그)
- (team) `adocs/` 를 노트북 밖에 백업 (md 수동) — 완료 조건: 노트북이 아닌 매체(클라우드·USB·별도 private 레포)에 오늘자 사본이 있다
- 브리핑 결과 Markdown 을 데모 레포 `docs/BRIEFING.md` 로 넣고 재인덱싱 → "사내 문서" 로 검색되는지 확인

**최근 결정** (md 확정)

- 브리핑 실행 기록은 인덱스마다 최근 3개 + 발행 run 을 남기고, 단계 캐시는 현재 소스 digest 폴더만 남긴다. 정적 조사 결과는 `survey.json` 에 한 번만 쓴다: "추천안대로 진행" (md, 대화 2026-09-09).
- 생성 중 소스가 바뀌어도 결과를 버리지 않는다 — 조사 직후 재검사로 섞인 버전을 막고(두 번 연속이면 `source_unstable` 실패), 그 뒤 변경은 `source_changed` 표시로 partial 발행: "추천안대로 진행해줘" (md, 대화 2026-09-09).
- 브리핑 조정은 레포 이름·경로를 보지 않는 일반 규칙으로만 한다: "위 내용이 이 레포에 한해서가 아닌, 전체적인 내용적용을 위해 진행하는게 맞는지" 확인 뒤 "진행" (md, 대화 2026-09-09).

**인덱스** (EC2 `hancom-team2-5th` · store pgvector · 스냅샷 2026-09-09 07:20 UTC)

- `api-test--ast` 1,674청크 · ast-v1 · header on · bm25 on · commit `2dea3d71`
- `api-test--ast-v2` 2,078청크 · ast-v2 · header on · bm25 on · commit `2dea3d71`
- `api-test--ast-v3` 2,130청크 · ast-v3 · header on · bm25 on · commit `2dea3d71`
- `api-test--lines` 1,622청크 · line-window-v1 · header off · bm25 on · commit `2dea3d71`
- `asyncer` 415청크 · ast-v2 · header on · bm25 on · commit `263e33f1`
- `asyncer@main@main` 415청크 · ast-v3 · header on · bm25 on · commit `263e33f1`
- `cli--ast-v2` 1,680청크 · ast-v2 · header on · bm25 on · commit `65fce667`
- `cli--ast-v3` 1,680청크 · ast-v3 · header on · bm25 on · commit `65fce667`
- `cli@master` 1,741청크 · ast-v3 · header on · bm25 on · commit `65fce667`
- `deploy-smoke-runtime-20260909` 0청크 · ast-v3 · header on · bm25 on · commit `b2fdb8f3`
- `deploy-smoke-runtime-chunks-20260909` 8청크 · ast-v3 · header on · bm25 on · commit `f49290cf`
- `fastapi-cli--ast` 306청크 · ast-v1 · header on · bm25 on · commit `10d7e65a`
- `fastapi-cli--ast-v2` 315청크 · ast-v2 · header on · bm25 on · commit `10d7e65a`
- `fastapi-cli--ast-v3` 315청크 · ast-v3 · header on · bm25 on · commit `10d7e65a`
- `fastapi-cli--lines` 250청크 · line-window-v1 · header off · bm25 on · commit `10d7e65a`
- `fastapi-cli@main` 315청크 · ast-v3 · header on · bm25 on · commit `10d7e65a`
- `fastapi-new--ast-v2` 189청크 · ast-v2 · header on · bm25 on · commit `86c34c2a`
- `fastapi-new--ast-v3` 189청크 · ast-v3 · header on · bm25 on · commit `86c34c2a`
- `flask-realworld-example-app--ast-v3` 233청크 · ast-v3 · header on · bm25 on · commit `411a17ff`
- `flask-restplus-server-example` 589청크 · ast-v2 · header on · bm25 on · commit `73ba0b6a`
- `main-project` 19,765청크 · ast-v3 · header on · bm25 on · commit `5c2c2cdd`
- `module-project` 2,468청크 · ast-v3 · header on · bm25 on · commit `224e29e4`
- `sqlalchemy--ast-v2` 14,350청크 · ast-v2 · header on · bm25 on · commit `cbef63a9`
- `sqlalchemy--ast-v3` 49,595청크 · ast-v3 · header on · bm25 on · commit `cbef63a9`
- `test-merge-project` 2,379청크 · ast-v2 · header on · bm25 on · commit `b7d82c2d`
- `vision` 0청크 · ast-v3 · header on · bm25 on · commit `6e760732`
- `vision--ast-v2` 177청크 · ast-v2 · header on · bm25 on · commit `3fff9633`
- `vision--brief-up` 177청크 · ast-v2 · header on · bm25 on · commit `3fff9633`
- `vision@frontend` 194청크 · ast-v3 · header on · bm25 on · commit `d89b28a2`
- `vision@frontend@frontend` 178청크 · ast-v3 · header on · bm25 on · commit `13ddec59`
- `vss_server` 403청크 · ast-v2 · header on · bm25 on · commit `97546fbc`
- `vss_server-brief_upgrader` 2,659청크 · ast-v3 · header on · bm25 on · commit `a3027469`
- `vss_server-main` 403청크 · ast-v2 · header on · bm25 on · commit `97546fbc`
- `vss_server-pre-rag` 798청크 · ast-v3 · header on · bm25 on · commit `203f0612`
- `vss_server-test-merge` 2,835청크 · ast-v3 · header on · bm25 on · commit `3dfb6299`
- `vss_server@test-merge` 2,908청크 · ast-v3 · header on · bm25 on · commit `9906a550`
- `vss_server@test-merge@test-merge` 2,908청크 · ast-v3 · header on · bm25 on · commit `9906a550`

**최근 평가** (`data/evaluation`)

- `20260909T071934Z-bc6c8e` fastapi-cli / ast-v3+header+rerank / vector / retrieval · n=46 · Hit@3 67% · MRR 0.63
- `20260909T071934Z-bc6c8e` fastapi-cli / ast-v3+header+rerank / vector / pipeline · n=46 · Hit@3 54% · MRR 0.51
- `20260909T071934Z-bc6c8e` fastapi-cli / ast-v3+header+rerank / hybrid / retrieval · n=46 · Hit@3 74% · MRR 0.65
- `20260909T071934Z-bc6c8e` fastapi-cli / ast-v3+header+rerank / hybrid / pipeline · n=46 · Hit@3 57% · MRR 0.49

<!-- status:end -->

수치의 원본은 `data/evaluation/runs/*.json` 과 `reports/*.md`(EC2 에서 커밋)다. 그 요약은 `python scripts/make_status.py` 가 만드는 `STATUS.md`(git 제외)다. 문서에 손으로 적은 수치는 없다.

## 문서 (읽는 순서)

| 문서 | 무엇 | 언제 읽나 |
|---|---|---|
| `CHARTER.md` | 목표, 범위, 하지 않는 것, 불변 조건 7개, 마감과 체크포인트 날짜 | 무엇이든 하기 전에 |
| `README.md` (이 파일) | 구성, 코드 구조, 구현 현황, 다음 작업, 사용법 | 매일 |
| `docs/JOURNAL.md` | 회차별 판단 기록. 무엇을 재서 무엇이 나왔고 왜 그렇게 정했나 | **설정값의 근거가 궁금할 때** |
| `docs/RAG_BASELINE_20260827.md` | 8/27 기준선 측정 상세. RAG 기초와 용어부터 시작한다 | RAG 를 처음 볼 때, 수치를 인용하기 전에 |
| `docs/API.md` | `/v1/chat` SSE 계약, `/index`, `/briefing` | Extension, 스냅샷 연동 |
| `evaluation/README.md` | gold 문항(JSONL), matrix, run 기록 규칙 | 문항 작성, 측정 |
| `SALVAGE.md` | `rag_lab` 에서 가져온 파일과 버린 것 | 출처 확인이 필요할 때 |

## EC2 실행 순서 — 설치부터 기준선 측정까지

**EC2 에서 위에서 아래로 그대로 붙여 넣는다.** 한 번 완주하면 인덱스 6개와 측정 보고서 3개가 생기고, 마지막 단계가 그 결과를 레포로 돌려보낸다.
낱개 명령은 이 절 아래에 따로 있다.

### 1. 서버 설치 (머신당 한 번)

```bash
git clone <repo> ~/vss_server && cd ~/vss_server
bash scripts/setup_ec2.sh              # 패키지 · venv · PostgreSQL+pgvector · DB 초기화 · ~/repos · systemd (SKIP_PG=1 이면 Chroma 만)
source .venv/bin/activate
set -a; source .env; set +a            # VSS_STORE · VSS_PG_DSN · VSS_OLLAMA_URL · VSS_CHAT_MODEL
python -m vss.cli health               # 모델 2개 · dim=1024 · store 확인
```

### 2. 데모 레포 3개 배치

인덱싱 대상은 `~/repos/<repo>` 에 둔다. `evaluation/matrices/*.json` 의 `repository` 가 이 경로로 되어 있다. 폴더명은 언더스코어(`api_test`), 인덱스 이름만 하이픈(`api-test--ast`).

1단계를 돌렸으면 `~/repos`(= `/home/<계정>/repos`)가 이미 만들어져 있다. 홈 아래라 소유자가 접속 계정이므로 **WinSCP 로 그냥 끌어다 놓으면 된다.** sudo 도, 경로 권한 손질도 필요 없다.
`.git` 폴더를 포함해 통째로 올리면 revision 이 유지되고, 그러면 EC2 에 GitHub 자격증명을 둘 필요가 없다.
`rag_lab` 만 `data/`(4.5GB 인덱스 데이터)와 `.venv` 를 빼고 올린다. 실제 코퍼스는 1MB 남짓이다.

원격에서 바로 받고 싶으면 대신 이렇게 한다:

```bash
cd ~/repos
git clone git@github.com:h5vision/api_test.git    api_test
git clone git@github.com:h5vision/fastapi-cli.git fastapi-cli
```

올린 뒤 revision 을 확인한다. 8/28 코퍼스 동결에 기록할 값이다.

```bash
cd ~/repos/rag_lab && git init -q && git add -A && git commit -qm "corpus freeze"   # rag_lab 은 git 레포가 아니라 revision 을 여기서 만든다
for r in api_test rag_lab fastapi-cli; do
  printf '%-14s %s\n' "$r" "$(git -C ~/repos/$r rev-parse HEAD 2>/dev/null || echo '(git 레포 아님)')"
done
```

### 3. 인덱싱 6개

```bash
cd ~/vss_server && source .venv/bin/activate && set -a; source .env; set +a
python -m vss.cli health       # store 가 의도한 저장소인지 먼저 확인 (.env 의 VSS_STORE)
API_EXCLUDE="tests,admin/**,.snapshot-admin-backup/**"          # api_test 확정 제외 규칙 (8/27)

BASE="--chunker line-window-v1 --context-header off --bm25 on --no-briefing --note 8/27기준선-lines"
AST="--chunker ast-v1 --context-header on --bm25 on --note 8/27기준선-ast+header"

# 기준선 — 줄 윈도우 · 헤더 off · BM25 on
python -m vss.cli index ~/repos/api_test    --project api-test--lines    $BASE --exclude "$API_EXCLUDE"
python -m vss.cli index ~/repos/rag_lab     --project rag-lab--lines     $BASE
python -m vss.cli index ~/repos/fastapi-cli --project fastapi-cli--lines $BASE

# 8/27 실험군 — ast-v1 · 헤더 on · BM25 on (역사적 기준선 재현용)
python -m vss.cli index ~/repos/api_test    --project api-test--ast    $AST --exclude "$API_EXCLUDE"
python -m vss.cli index ~/repos/rag_lab     --project rag-lab--ast     $AST
python -m vss.cli index ~/repos/fastapi-cli --project fastapi-cli--ast $AST

python -m vss.cli projects                                      # 6개가 done 인지 · note 가 붙었는지 확인
```

`--note` 는 "이 인덱스를 왜 만들었나" 한 줄이다. 별도 파일이 아니라 **인덱스 자신의 meta** 에 저장돼 이름이 바뀔 때(promote)도 따라가고 `projects` 출력에 나온다.
(공백이 들어가면 따옴표로 감싼다: `--note "8/27 기준선"`)

한 개라도 실패하면 기존 인덱스는 그대로 있고 임시 빌드만 남는다(기존 것을 미리 지우지 않기 때문이다). `python -m vss.cli repair` 로 확인하고 그 한 줄만 다시 돌린다.

### 4. 기준선 측정 3개

```bash
for m in api-test rag-lab fastapi-cli; do
  python -m vss.eval run evaluation/matrices/$m.json --note baseline
done
python -m vss.eval runs                                         # 이력 한 표
```

`run` 은 matrix 를 **하나만** 받는다. 한 줄에 셋을 나열하면 실패한다. matrix 하나가 인덱스 2개 × 검색 프로필 2개 × 모드 2개 = 8셀이다.

### 4-1. ast-v2 재인덱싱과 세대 비교 측정 (2026-09-04 추가)

기존 `--lines`·`--ast` 인덱스는 **지우지 않는다.** 비교 대상이다. `--ast-v2` 를 옆에 만들면 한 번의 run 이 세 세대를 나란히 낸다.

```bash
# 코퍼스가 동결 커밋 그대로인지 먼저 (다르면 세대 비교가 깨진다)
for r in api_test fastapi-cli; do
  printf '%-14s %s  dirty=%s\n' "$r" "$(git -C ~/repos/$r rev-parse --short=8 HEAD)" \
    "$(git -C ~/repos/$r status --porcelain | wc -l)"
done   # api_test 2dea3d71 · fastapi-cli 10d7e65a · dirty 0

python -m vss.cli index ~/repos/api_test --project api-test--ast-v2 \
  --chunker ast-v2 --context-header on --bm25 on \
  --exclude "tests,admin/**,.snapshot-admin-backup/**" --note "ast-v2"
python -m vss.cli index ~/repos/fastapi-cli --project fastapi-cli--ast-v2 \
  --chunker ast-v2 --context-header on --bm25 on --note "ast-v2 대조군"

python -m vss.eval run evaluation/matrices/api-test.json    --note "gold40 + ast-v2"
python -m vss.eval run evaluation/matrices/fastapi-cli.json --note "gold61 + ast-v2"
```

각 matrix 가 6셀 × 2모드 = 측정 12개다. 문항당 0.25초이므로 api-test 약 2분, fastapi-cli 약 3분이다.
오래 걸리는 인덱싱을 걸어 두고 나갈 때는 `nohup bash -c '...' > ~/index.log 2>&1 &` 로 감싸되,
**`cd`·`source .venv/bin/activate`·`source .env` 를 그 안에서 다시 한다** — 새 셸이라 바깥의 activate 를 못 물려받는다.

### 4-2. ast-v3 재인덱싱과 재정렬 측정 (2026-09-07 추가)

`--ast-v2` 옆에 `--ast-v3` 를 만든다. 기존 인덱스는 지우지 않는다. matrix 두 개에 `--ast-v3` 셀이 이미 들어 있어 run 하나가 **줄 윈도우 / ast-v1 / ast-v2 / ast-v3+재정렬** 을 나란히 낸다.
재정렬은 v3 인덱스에서만 자동으로 켜지므로 v2 이전 셀은 9/4 run 과 같은 조건이다.

```bash
cd ~/vss_server && git pull && source .venv/bin/activate && set -a && source .env && set +a
python -m unittest discover tests -q                      # 124개. 실패하면 여기서 멈춘다
grep -n VSS_CHUNKER .env || echo "(없음 → 기본 ast-v3)"   # ast-v2 로 박혀 있으면 지운다. 아래 --chunker 는 명시라 무관하지만 서버 기본값·POST /index 가 v2 로 남는다

python -m vss.cli index ~/repos/api_test --project api-test--ast-v3 \
  --chunker ast-v3 --context-header on --bm25 on \
  --exclude "tests,admin/**,.snapshot-admin-backup/**" --note "ast-v3 (BOM) + rerank"
python -m vss.cli index ~/repos/fastapi-cli --project fastapi-cli--ast-v3 \
  --chunker ast-v3 --context-header on --bm25 on --note "ast-v3 대조군"
python -m vss.cli projects | grep ast-v3                  # 둘 다 done · chunker=ast-v3. api-test 는 v2 의 2,078 보다 청크가 달라야 한다(BOM 19파일)

python -m vss.eval run evaluation/matrices/api-test.json    --note "ast-v3 + rerank"
python -m vss.eval run evaluation/matrices/fastapi-cli.json --note "ast-v3 + rerank"
python -m vss.cli projects --json > data/ec2/projects.json
sudo systemctl restart vss-server                          # 자동 선택이 --ast-v3 로 옮겨 간다 (GET /projects?view=repos 의 index_id 로 확인)
```

각 matrix 가 8셀 × 2모드 = 측정 16개다. 결과는 「5」대로 WinSCP 로 가져온다 — `data/evaluation/runs`·`reports` 의 새 파일 2쌍과 `data/ec2/projects.json`.

### 5. 결과를 노트북으로 가져오기

EC2 는 GitHub 에 push 하지 않는다. 측정 결과와 인덱스 현황은 **WinSCP 로 노트북에 내려받아 노트북에서 커밋**한다 (2026-09-07 결정).
`data/ec2/projects.json` 은 git 밖이다 — EC2 에서 매번 바뀌는 생성 파일이 tracked 면 EC2 의 다음 `git pull` 을 막는다(9/7 에 실제로 막혔다). `data/evaluation/` 은 수치의 정본이라 tracked 로 두되 노트북이 커밋한다.

```bash
# EC2
mkdir -p data/ec2
python -m vss.cli projects --json > data/ec2/projects.json      # 인덱스 현황 스냅샷 (generated_at 포함). git 이 무시하는 경로다
```
WinSCP 로 `data/ec2/projects.json` 과 새로 생긴 `data/evaluation/runs/*.json`·`data/evaluation/reports/*.md` 를 노트북의 **같은 경로**에 내려받는다.
```bash
# 노트북
git add data/evaluation && git commit -m "eval: <무엇을 쟀나>" && git push
```
```bash
# EC2 — 다음 git pull 전에. EC2 쪽 미커밋·미추적 사본(방금 노트북이 커밋한 같은 파일)이 pull 을 막지 않게 치운다
git stash -u && git pull && git stash drop
```

### 6. 프론트가 부를 이름 정하기 (별칭), 서버 켜기

Extension 은 **레포 이름**(`api_test`)만 보내고, 그 질문에 어느 인덱스가 답할지는 서버가 정한다. 그래야 RAG 를 개선해 인덱스를 갈아탈 때 클라이언트를 고치지 않는다.
`.env` 에 한 줄 넣고 재시작하면 끝이고, 다음에 더 좋은 인덱스가 나오면 이 줄만 바꾼다.

```bash
# 실제로 존재하는 인덱스만 적는다 — 없는 인덱스를 가리키는 별칭은 404 를 만든다 (rag_lab 은 인덱스가 생긴 뒤에 추가)
echo 'VSS_PROJECT_ALIASES=api_test=api-test--ast,fastapi-cli=fastapi-cli--ast' >> .env
sudo systemctl restart vss-server && journalctl -u vss-server -f    # 포트 8200
curl -s localhost:8200/health | jq '.projects, .project_aliases'
python -m vss.cli ask "결제 요청은 어디서 처리되나요?" --project api_test   # [meta] index=... 로 어느 인덱스가 답했는지 보인다
```

별칭은 **질의 경로 전용**이다. 인덱싱(`cli index`, `POST /index`)과 평가(`vss.eval`)는 인덱스 이름을 그대로 쓴다.
측정이 별칭을 타면 "어느 인덱스를 쟀는가" 가 흐려진다(불변 조건 6). 별칭이 없는 이름은 인덱스 이름 그대로 취급하고, 없으면 `project_not_found` 다. 조용한 폴백은 없다.

서버가 뜬 뒤로는 인덱싱을 CLI 로 하지 않는다. Chroma 는 한 프로세스만 열어야 안전하다. 아래 「낱개 명령」의 `POST /index` 를 쓴다.

### 7. ANN 이 recall 을 깎는지 확인 (측정 뒤 한 번)

pgvector 의 hnsw 는 근사 검색이다. 정확 검색으로 **질의만** 다시 해서 같은 수치가 나오는지 본다. 재인덱싱은 필요 없다.

```bash
VSS_PG_EXACT=1 python -m vss.eval run evaluation/matrices/rag-lab.json --note "exact 대조"
python -m vss.eval runs         # 앞의 baseline 셀과 Hit@k 비교 (차이 0 또는 1/n 이내면 hnsw 를 그대로 쓴다)
```

차이가 크면 인덱스가 아니라 검색 파라미터 문제다. `hnsw.ef_search` 를 올리거나 `VSS_PG_EXACT=1` 을 상시로 둔다(데모 규모에서는 정확 검색도 충분히 빠르다).

### 8. 질의 로그 켜기 (선택 — "그 질문이 서버까지 왔나")

`/v1/chat` 요청 하나가 `rag.query_log` 한 행이 된다. 남는 것은 `request_id`·`project_id`·`index_id`·질문 본문·`outcome`·`top_score`·`reason`·`timing` 등이고 **답변 본문은 남지 않는다.**
`rag: false` 요청은 남기지 않는다. 이 값이 비어 있으면 아무것도 남지 않는다(기본값).

```bash
echo 'VSS_QUERYLOG_DSN=postgresql://vss_rag:<pw>@127.0.0.1:5432/vss' >> .env    # VSS_PG_DSN 과 같은 값
sudo systemctl restart vss-server
# 테이블은 첫 질의 때 만들어진다 — pull·재시작만으로는 안 생긴다
curl -s localhost:8200/v1/chat -H 'Content-Type: application/json' \
  -d '{"project_id":"api_test","message":"결제는 어디서 처리되나요","client_request_id":"smoke-1"}' | head -c 200
sudo -u postgres psql vss -c "select request_id, outcome, index_id, top_score, left(question,30) from rag.query_log order by id desc limit 10;"
```

`client_request_id` 를 보내면 그 값이 그대로 `request_id` 가 되어 응답과 DB 에 같이 남는다 — "이 질문이 안 됩니다" 를 그 문자열 하나로 찾는다.
관리자 페이지(P)에서 읽을 때 `GRANT` 는 따로 필요 없다. `scripts/db_init.sql` 의 `ALTER DEFAULT PRIVILEGES` 가 새 테이블에 자동 적용된다.

## 낱개 명령 — 인덱싱, 질문, 브리핑

```bash
python -m vss.cli index ~/repos/rag_lab --project rag-lab--ast --context-header on --bm25 on        # 기본 청커 ast-v3, 끝나면 브리핑 자동
python -m vss.cli index ~/repos/rag_lab --project rag-lab--lines --chunker line-window-v1 --context-header off --no-briefing   # 기준선
python -m vss.cli index --git https://github.com/org/repo --project demo --exclude "tests,docs/ko/**"  # clone 해서 인덱싱
python -m vss.cli index ~/repos/rag_lab --project rag-lab--ast                      # 같은 이름·같은 설정이면 바뀐 파일만 다시 임베딩(증분). 브리핑은 이전 것 유지
python -m vss.cli index ~/repos/rag_lab --project rag-lab--ast --briefing always    # 증분이어도 브리핑을 다시 만든다
python -m vss.cli index ~/repos/rag_lab --project rag-lab--ast --force              # 전체 재인덱싱
python -m vss.cli status --project rag-lab--ast                                     # index.mode(full|incremental) 와 index.incremental(복사·재생성 청크 수)
python -m vss.cli index ~/repos/api_test --project api-test--ast --bm25 on --exclude "tests,admin/**,.snapshot-admin-backup/**"   # api_test 확정 제외 규칙(8/27)
python -m vss.cli projects                                                                             # --json: 스냅샷 출력
python -m vss.cli search "전체 인덱싱에서 선삭제 대신 쓰는 메서드는?" --project rag-lab--ast
VSS_SYMBOL_BOOST=1 python -m vss.cli search "rrf_fuse 가 뭐야" --project rag-lab--ast                  # 심볼 재정렬 켜고 (재인덱싱 불필요)
python -m vss.cli ask    "전체 인덱싱에서 선삭제 대신 쓰는 메서드는?" --project rag_lab                # 별칭으로 (프론트와 같은 경로)
python -m vss.cli ask    "같은 질문" --no-rag                                                          # 발표용 비교 (검색 없이)
python -m vss.cli briefing --project rag-lab--ast --force
python -m vss.cli doctor
```

서버가 떠 있는 동안 인덱싱할 때는 CLI 대신 `POST /index` 를 쓴다. Chroma 는 한 프로세스만 열어야 안전하다:

```bash
curl -s localhost:8200/index -H 'Content-Type: application/json' \
  -d '{"project_root":"~/repos/rag_lab","project_id":"rag-lab--ast"}'
curl -s "localhost:8200/index/status?project_id=rag-lab--ast"          # mode 가 incremental 이면 index.incremental 에 reused_chunks·rebuilt_chunks
curl -s localhost:8200/index -H 'Content-Type: application/json' \
  -d '{"remote":"git@github.com:h5vision/api_test.git","branch":"main","project_id":"api-test--ast-v3"}'   # Extension 경로: 인덱스 이름은 api-test@main--ast-v3 가 된다
```

## 서버

```bash
python -m vss.server --host 0.0.0.0 --port 8200          # 또는 sudo systemctl restart vss-server
curl -s localhost:8200/health | jq .projects
curl -N -s localhost:8200/v1/chat -H 'Content-Type: application/json' \
  -d '{"project_id":"rag-lab--ast","message":"임베딩이 실패하면 폴백이 있나요?","stream":true}'
```

프론트가 쓸 목록 (라우트는 `/projects` 하나, 파라미터로 갈린다):

```bash
curl -s "localhost:8200/projects?view=repos&commits=20"          # 레포 단위. name 을 그대로 project_id 로 쓴다
curl -s "localhost:8200/projects?only=current"                   # 레포마다 지금 답하는 인덱스만
curl -s "localhost:8200/projects?project_id=api_test&files=1"    # 인덱스에 실제로 들어간 파일 (&symbols=1 로 심볼까지)
```

## 평가

```bash
python -m vss.eval validate evaluation/matrices/rag-lab.json
python -m vss.eval run      evaluation/matrices/rag-lab.json --note baseline
python -m vss.eval runs                                   # 이력 한 표
python scripts/make_status.py                             # STATUS.md 생성 (인덱스 + 최근 run)
```

결과를 레포로 돌려보내는 두 줄은 위 「EC2 실행 순서」 5단계에 있다. 측정을 다시 돌릴 때마다 그 단계를 반복한다(`--note` 와 커밋 메시지에 run 을 구분할 이름을 적는다).

## 테스트 (Ollama 없이)

```bash
python -m unittest discover tests -v
VSS_TEST_STORE=pgvector python -m unittest tests.test_roundtrip -v      # PostgreSQL 이 떠 있을 때
```

## 설정

모든 설정은 `vss/config.py` 의 환경변수(`.env`)로 바꾼다. 값과 기본값은 위 자동 구역의 표가 기준이다.
"청킹" 구분의 값은 인덱스 fingerprint 에 들어가므로 바꾸면 재인덱싱이 필요하고, "검색" 구분의 값은 서버 재시작만으로 바뀐다.
`VSS_TOKEN` 을 비워 두지 않으면 모든 요청에 `X-VSS-Token`(또는 `Authorization: Bearer`)이 필요하다.


<!-- briefing-pipeline:begin -->

### 브리핑: 근거 분석 후 최종 개요 생성

브리핑은 파일·설정·진입점·등록·함수·호출 단서를 조사하고, 문서와 코드에서 주제를 선정한 뒤 원문을 읽어 분석합니다. 문서가 없어도 코드에서 확인 가능한 범위로 진행합니다. 마지막 개요는 주제별 분석과 문서 요약을 받아 작성하며, 그림은 생성하지 않습니다.

- 구현: `vss/briefing_survey.py`(읽기 전용 조사·근거), `vss/briefing_pipeline.py`(예산·분석·종합·저장). 기존 `vss/briefing.py`는 API/CLI 호환 진입점입니다.
- 초기 기준: 주제 최대 8개, 대표 흐름 최대 3개, 보완 최대 2개 주제. 주제별 직접 원문 구간 최대 12개, RAG 질의 최대 4개. 생성 요청은 재시도 포함 최대 40회입니다.
- 생성 호출은 순차 실행합니다. 기존 `VSS_NUM_CTX`(기본 8192)와 상주 모델 선택 정책을 사용하며, 브리핑이 모델을 따로 올리거나 컨텍스트를 자동 확대하지 않습니다.
- 입력 예산은 지시문을 포함해 일반 분석 5000, 최종 종합 4500 추정 토큰 이하이며 출력 2000/2500(문서 요약은 1500)과 여유를 예약합니다. 토큰 어림은 실측 계수(영문 2.8자/토큰, 그 밖 0.6토큰/자, 2026-09-09 EC2 3회 최소제곱)를 쓰고 `VSS_BRIEFING_CHARS_PER_TOKEN_ASCII`·`VSS_BRIEFING_TOKENS_PER_CHAR_OTHER` 로 바꿉니다. 호출마다 어림·실제 토큰과 글자 수가 `analysis.json` 의 `calls[]` 에 남아 다시 잴 수 있습니다.
- 최종 종합 입력이 예산을 넘으면 실패 전에 대표 아닌 주제의 읽을 위치 → 문서 claim 2개 → 주제당 조건 3개 → 핵심 사실 1개 순으로 줄이고, 그래도 넘치면 `context_budget_exceeded` 로 기록합니다. 줄인 실행은 `quality_status=partial` 이고 실행 기록의 `problems` 에 `compacted` 와 적용한 단계가 남습니다.
- 전체 시간 예산은 `VSS_BRIEFING_TIME_BUDGET`(기본 600초, 0 = 없음)입니다. 남은 시간이 final 몫 120초 + 60초 아래면 새 문서·주제 호출을 시작하지 않고(`time_budget`), final 은 항상 돕니다. 호출 하나의 timeout 은 `VSS_CHAT_TIMEOUT` 의 4배(720초)와 남은 시간 중 작은 쪽입니다. 시간 초과·전송 오류는 그 항목만 실패로 기록하고 진행합니다(전송 오류는 같은 입력으로 1회 재시도). 최종 종합이 실패하면 이전 브리핑을 보존합니다.
- 브리핑 호출은 `VSS_BRIEFING_THINK`(기본 `false`)로 추론 모드를 끕니다. 모델이 그 값을 거부하면 `think_unsupported` 로 즉시 실패하니 `.env` 를 그 모델이 받는 값(gpt-oss 는 `low|medium|high`)으로 바꿉니다.
- 근거 검증은 문장 단위입니다 — 근거 id 가 틀린 문장만 빼고 `problems` 에 `claims_dropped` 로 남깁니다(→ `partial`). 모델이 지어낸 파일 경로는 그 경로만 뺍니다. 조사 순서는 대표 주제 → 모델이 고른 주제 → 고정 주제(의존성·설정)이고, 변경 이력 문서(changelog·release-notes)는 맨 뒤에 묶음 1개까지만 읽습니다.
- lock: 브리핑 도중 프로세스가 죽어 남은 lock 은 소유자 pid·boot_id 로 죽음을 확인한 것만 서버 기동 때와 다음 요청 때 치웁니다(시각만으로는 안 치움). 실행 기록은 `data/briefings/runs/<인덱스>/` 에 최근 `VSS_BRIEFING_KEEP_RUNS`(기본 3)개 + 발행 run 을 남기고, 단계 캐시는 현재 소스 digest 폴더만 남깁니다.
- RAG는 소스 Git 커밋과 활성 인덱스 커밋이 일치하고 변경사항이 없을 때 사용합니다. 버전을 확인할 수 없는 materialized 디렉터리나 dirty 체크아웃은 직접 원문 조회로 조사합니다. 검색 결과 자체를 원문으로 신뢰하지 않고 조사한 파일의 줄 범위로 다시 읽습니다. 검색은 "읽을 위치 후보" 용도라 `/v1/chat` 의 임계값 판정과 무관하게 상위 5개(`all_hits`)를 쓰고, 질의는 주제의 한국어 질문 하나 + 식별자 하나입니다(2026-09-09).
- 주제 조사는 후보가 경로·정의 이름·호출 대상 일치 또는 검색 적중일 때만 모델을 부릅니다. 본문 한 줄에 낱말만 걸린 후보뿐이면 호출 없이 `weak_candidates` 로 끝냅니다 — "error"·"save" 같은 낱말은 어느 파일에나 있어서.
- Python은 AST로 정의·호출 후보를 추출합니다. 다른 언어는 텍스트 조사로 진행하고 제한을 기록합니다. 동적 연결과 파일명·등록 구문 후보를 확정된 실행 관계로 표시하지 않습니다.

EC2에서 브리핑만 생성하려면 (기존 인덱스를 사용하므로 재인덱싱 불필요):

```bash
python -m vss.cli projects
python -m vss.cli briefing --project <인덱스ID> --force
```

CLI는 완료까지 기다립니다. 생성 모델이 이미 올라와 있어야 합니다. `--model`을 명시하면 그 모델이 없을 때 `model_not_loaded`로 종료합니다. HTTP 프록시 타임아웃을 피하려면 비동기 요청을 사용합니다.

```text
POST /briefing
{"project_id":"<인덱스ID>","force":true,"background":true}

GET /briefing/status?project_id=<인덱스ID>
GET /briefing?project_id=<인덱스ID>
GET /briefing.md?project_id=<인덱스ID>
```

기존 인증 헤더를 그대로 사용합니다. 비동기 요청은 202를 반환하고 `queued/running/ready/failed` 상태를 조회할 수 있습니다. `ready`는 파일 생성 완료이며 의미적 정확성 검증 완료를 뜻하지 않습니다. `quality_status=partial`이면 실행 기록의 실패·제외 범위를 함께 확인합니다. 생성 중 GET은 이전 정상 결과를 계속 반환할 수 있으므로 상태의 `run_id`와 결과의 `run_id`를 비교합니다.

저장 위치:

- `data/briefings/<ID>.json`: 최종 공개 결과. 기존 JSON 필드 유지, `topics`, `coverage`, `rag`, `metrics`, `run_id` 추가.
- `data/briefings/runs/<ID>/<run_id>/`: 조사 원문·분석·진행·실패 기록과 최종 Markdown. 민감한 원문을 포함할 수 있으므로 서버 내부에 보관합니다.
- `data/briefings/stage_cache/<ID>/`: 동일 소스 내용·모델·설정·프롬프트의 성공 단계 재사용. 원문이나 프롬프트가 바뀌면 다른 키를 사용합니다.
- 최종 종합 실패·컨텍스트 초과·소스 변경 시 기존 공개 결과를 보존합니다. `<ID>.md` 고정 파일 대신 JSON의 `md_path`가 가리키는 실행별 파일을 사용하며 GET/CLI는 이를 자동 해석합니다.

서버가 강제 종료되면 `runs/<ID>.lock`이 남을 수 있습니다. 상태와 프로세스를 확인해 실제 작업이 없을 때만 운영자가 해당 잠금 파일을 제거하고 재요청합니다. 자동 삭제·기존 브리핑 선삭제는 하지 않습니다. 실행 기록·캐시는 자동 정리하지 않으므로 운영 중 디스크 사용량을 관리해야 합니다.

검증: `python -m unittest discover tests -q`. 로컬 테스트는 가짜 Ollama로 생성 순서·출처·예산·실패 보존을 검증하며 실제 모델의 한국어 품질·속도·VRAM을 증명하지 않습니다. 결과 검토 시 주요 기능 누락, 조건 보존, 출처 위치, 개요와 상세 설명의 일치를 확인합니다.

<!-- briefing-pipeline:end -->
