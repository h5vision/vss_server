# vss_server

VSsVscodeEX 의 서버. 레포를 인덱싱하고(AST 청킹 · bge-m3 · Chroma 또는 pgvector · BM25), 질문에 **출처와 함께** 답하고(Ollama 스트리밍),
인덱싱이 끝나면 **프로젝트 브리핑**(Markdown)을 만든다. 표준 라이브러리 HTTP 서버 하나로 동작하고, 외부 의존성은 `chromadb` 와 `psycopg` 뿐이다.
2026-08-26 에 `rag_lab` 을 대체하는 새 레포로 시작했다(가져온 것은 `SALVAGE.md`). 목표·범위·불변 조건은 `CHARTER.md` 가 앵커다.

이 README 는 **인수인계 문서**다 — 구성 · 코드 구조 · 구현 현황(어디까지 됐고 다음이 무엇인지) · 사용법을 이 파일 하나로 파악할 수 있게 유지한다.

## 구성 — 어디서 무엇이 도는가

- **서버 한 대**: 팀 GPU 노드 EC2 `hancom-team2-5th`(주소·토큰은 md 가 팀 채널로 공유, 이 파일에는 적지 않는다). 포트 **8200**, `vss-server` systemd 서비스.
  같은 머신의 Ollama(11434)가 임베딩(`bge-m3`, 1024차원)과 생성(`qwen2.5-coder:7b`, 9/1 bake-off 로 교체 검토)을 맡는다.
- **저장소**: **PostgreSQL + pgvector**(스키마 `rag`) 로 간다 — 8/27 EC2 에서 pgvector 0.8.6 · `CREATE EXTENSION` · 왕복 테스트 10/10 을 확인하고 결정했다. Chroma(`data/index/`)는 코드에 남아 있고 `VSS_STORE=chroma` 로 언제든 돌아갈 수 있다.
  스냅샷 서비스(P)는 같은 DB 의 `snapshot` 스키마를 쓴다. 두 저장소 모두 "빌드 → 승격" 방식이라 인덱싱 중에도 기존 인덱스가 서비스된다.
- **데모 코퍼스**: EC2 `~/repos/` 아래에 둔다 — 현재 `api_test`(앱형) · `fastapi-cli`(61문항 gold, 측정 대조군)가 올라가 있고 **`rag_lab`(문서 풍부)은 아직 미배치**다.
  인덱스 이름은 `<repo>--lines`(기계적 청킹 대조군) / `<repo>--ast`(현행) 처럼 청킹 방식을 붙이고, 왜 만든 인덱스인지는 `--note` 로 인덱스 자신에 적는다.
  코퍼스 제외 규칙(8/27 확정): api_test 는 `tests,admin/**,.snapshot-admin-backup/**` 제외, 나머지 레포는 공통 기본 제외만 — 상세와 gold 문항 규칙은 `evaluation/README.md`.
- **클라이언트**: VSCode Extension(K·Y)은 `POST /v1/chat`(SSE) 하나만 부른다. **보내는 `project_id` 는 레포 이름**(`api_test`)이고, 어느 인덱스가 답할지는 서버가 정해 응답 `index_id` 로 알려 준다 —
  RAG 를 개선해 인덱스를 갈아타도 Extension 은 고치지 않는다. 계약은 `docs/API.md`. 스냅샷 서비스(P)는 파일을 풀어 놓고 `POST /index` 를 부른다(여기는 인덱스 이름을 그대로 쓴다).
- **작업 방식**: 코드는 한 방향으로만 흐른다 — 노트북에서 작성·커밋·push → GitHub → EC2 에서 `git pull` 후 실행. **EC2 에서 파일을 직접 고치지 않는다**(고치면 다음 pull 에서 충돌하고, 어느 트리에서 나온 수치인지 추적이 끊긴다).
  반대 방향으로 돌아오는 것은 **EC2 가 커밋하는 두 가지**뿐이다: 측정 결과 `data/evaluation/`(run·report — 수치의 정본) 과 인덱스 현황 `data/ec2/projects.json`(`vss.cli projects --json` 출력, README 상태 구역의 원본).
  `.env`(주소·토큰·DSN)는 git 에 올리지 않고 EC2 에만 둔다. 팀원과 EC2 는 이 README 와 `CHARTER.md` 만 보면 된다.

<!-- config:begin -->

### 코드에서 뽑은 사실 (자동 생성)

| 종류 | 값 |
|---|---|
| HTTP 엔드포인트 | `GET /` · `GET /health` · `GET /v1/health` · `GET /projects` · `GET /v1/projects` · `GET /v1/models` · `GET /index/status` · `GET /index/exists` · `GET /briefing` · `GET /briefing.md` · `POST /v1/chat` · `POST /chat` · `POST /search` · `POST /v1/search` · `POST /prompt` · `POST /finalize` · `POST /index` · `POST /briefing` · `POST /bm25` |
| CLI (`python -m vss.cli`) | `health` · `projects` · `index` · `status` · `search` · `ask` · `briefing` · `bm25` · `repair` · `doctor` |
| 평가 (`python -m vss.eval`) | `validate` · `run` · `report` · `runs` · `sweep` |

| 구분 | 환경변수 | 기본값 | 메모 |
|---|---|---|---|
| 임베딩 (불변 조건: bge-m3 · cosine · 폴백 없음) | `VSS_OLLAMA_URL` | `http://127.0.0.1:11434` |  |
|  | `VSS_EMBED_MODEL` | `bge-m3:latest` |  |
|  | `VSS_EMBED_BATCH` | `16` |  |
|  | `VSS_EMBED_TIMEOUT` | `120` |  |
| 생성 모델 (LLM 호출은 이 서버가 직접 합니다) | `VSS_CHAT_MODEL` | `qwen2.5-coder:7b` |  |
|  | `VSS_BRIEFING_MODEL` | `(없음)` | 비면 chat_model |
|  | `VSS_NUM_CTX` | `8192` |  |
|  | `VSS_CHAT_TIMEOUT` | `180` |  |
|  | `VSS_ALLOW_MODEL_OVERRIDE` | `True` |  |
| 청킹 (fingerprint) | `VSS_CHUNKER` | `ast-v1` | ast-v1 / line-window-v1 |
|  | `VSS_CHUNK_SIZE` | `1200` |  |
|  | `VSS_CHUNK_OVERLAP` | `150` |  |
|  | `VSS_MIN_CHUNK` | `80` |  |
|  | `VSS_AST_MAX_CHARS` | `3500` |  |
|  | `VSS_CONTEXT_HEADER` | `True` |  |
|  | `VSS_MAX_FILE_BYTES` | `1_000_000` |  |
|  | `VSS_EXCLUDE_GLOBS` | `(없음)` |  |
|  | `VSS_USE_BM25` | `True` |  |
| 검색 (런타임 설정 — 재인덱싱 불필요) | `VSS_TOP_K` | `4` |  |
|  | `VSS_THRESHOLD` | `0.54` |  |
|  | `VSS_FUSION_POOL` | `20` |  |
|  | `VSS_RRF_K` | `60` |  |
| 저장 | `VSS_STORE` | `chroma` | chroma / pgvector |
|  | `VSS_DATA_DIR` | `./data` |  |
|  | `VSS_PG_DSN` | `postgresql://vss_rag:vss_rag@127.0.0.1:5432/vss` |  |
|  | `VSS_PG_SCHEMA` | `rag` |  |
|  | `VSS_PG_EXACT` | `False` | 검증용 정확 검색 |
| 서버 | `VSS_TOKEN` | `(없음)` |  |
|  | `VSS_PROJECT_ALIASES` | `(없음)` | 질의 전용: api_test=api-test--ast,... |

폴더 구조 (최상위):

```text
docs/  API.md, JOURNAL.md, RAG_BASELINE_20260827.md
evaluation/  matrices, README.md, schemas, suites, tags.json
scripts/  backup_pg.sh, db_init.sql, make_status.py, setup_ec2.sh, vss-server.service
tests/  __init__.py, fakes.py, test_roundtrip.py
vss/  __init__.py, analysis.py, briefing.py, chat.py, chunker.py, cli.py, config.py, context_header.py …
.gitignore
CHARTER.md
README.md
SALVAGE.md
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
      briefings/   브리핑 캐시             rag.chunks     청크 1개 = 1행
      evaluation/  측정 결과 ← git 으로 돌아가는 유일한 것      embedding vector(1024), hnsw cosine
      index_log.jsonl
```

### EC2 어느 경로에 무엇이 있나

서버 코드도 코퍼스도 접속 계정의 홈(`~` = `/home/<계정>`) 아래에 나란히 둔다. 홈 밖(`/srv` 등)에 두지 않는 이유는 sudo 와 소유권 손질이 필요해지고 WinSCP 로 바로 올리지 못하기 때문이다.

| 경로 | 무엇 | 누가 만드나 |
|---|---|---|
| `~/vss_server` | 서버 코드. `git pull` 로만 바뀐다 | 1단계 `git clone` |
| `~/vss_server/data/` | 인덱스 · BM25 · 브리핑 · 측정 결과 (`VSS_DATA_DIR`) | 서버가 자동 생성 |
| `~/vss_server/.env` | 주소 · 토큰 · DSN. git 에 올리지 않는다 | `setup_ec2.sh` |
| `~/repos/<repo>` | 인덱싱할 레포. 서버 코드와 **형제 폴더**로 갈라 둔다 | `setup_ec2.sh` 가 폴더까지, 내용은 2단계 |
| PostgreSQL `vss` DB, `rag` 스키마 | pgvector 를 쓸 때의 벡터 | `setup_ec2.sh` 의 DB 초기화 |

코퍼스를 서버 코드 **안**이 아니라 형제로 두는 이유: 안에 두면 `git pull`·`git status` 가 코퍼스를 건드리고, 서버 레포를 인덱싱할 때 자기 자신이 코퍼스에 섞인다.
`evaluation/matrices/*.json` 의 `repository` 는 `~/repos/<repo>` 로 적혀 있고 실행할 때 `~` 를 푼다 — 계정 이름이 `ubuntu` 가 아니어도 그대로 통한다.

### 알아야 할 것 넷

- **`--project` 이름이 키다.** `api-test--ast` 는 폴더가 아니라 인덱스 이름이다.
  같은 레포를 설정만 바꿔 두 번 인덱싱하면(`--lines` 기계적 청킹 / `--ast` 함수 단위) 두 인덱스가 남아 같은 질문 suite 로 비교할 수 있다 — 기준선 측정이 하는 일이 이것이다.
  인덱스를 늘려야 하는 축은 **fingerprint 에 든 것뿐**(청커·헤더·제외 규칙·임베딩 모델 등)이고, `top_k`·`threshold`·BM25 융합·생성 모델은 같은 인덱스에 질의할 때 바꾼다 — 그래서 임계값 재보정이나 모델 교체는 인덱스를 늘리지 않는다.
  왜 만든 인덱스인지는 `--note` 로 인덱스 자신에 적어 둔다. 어떤 설정이었는지의 정본은 이름이 아니라 fingerprint 다(`cli status --project <이름>`).
- **프론트는 인덱스 이름을 모른다.** Extension 은 레포 이름(`api_test`)을 보내고 서버가 `VSS_PROJECT_ALIASES` 로 실제 인덱스를 고른다.
  응답의 `index_id` 가 실제로 답한 인덱스다. 인덱싱·평가는 별칭을 쓰지 않는다.
- **저장 위치는 `VSS_STORE` 하나가 정한다.** 레포 위치와 무관하고, 인덱싱 명령도 똑같다. Chroma 는 `data/index/` 파일, pgvector 는 DB 행이 된다.
- **빌드 → 승격이라 실패해도 서비스가 깨지지 않는다.** 인덱싱 중에는 `building-<이름>` 에 쌓이고 임베딩이 전부 성공해야 진짜 이름으로 승격된다.
  중간에 죽으면 기존 인덱스가 그대로 답하고 실패한 빌드는 증거로 남는다(자동 삭제하지 않는다 — 불변 조건 2). pgvector 에서는 같은 일이 `revisions` 행의 `building → active`, 이전 것은 `retired` 로 일어난다.

## 코드 구조 — 모듈이 하는 일

두 경로만 이해하면 된다.

- **인덱싱 경로**: `POST /index`(또는 CLI `index`) → `indexer.start_index` → `chunker`(파일 수집·청킹) → `embedder`(bge-m3) →
  `store.begin_build → add → promote`(선삭제 없는 원자적 교체) → `lexical`(BM25 역색인) → 완료 훅으로 `briefing` 생성.
- **질의 경로**: `POST /v1/chat` → `chat.run_chat` → `search`(벡터 top-k + 선택적 BM25 RRF 융합 + 임계값 판정) →
  `prompt.render_prompt`(근거 `[N]` 부여) → `llm.chat_stream`(Ollama 스트리밍) → `prompt.finalize` + `references`(인용 `[N]` 파싱 → 출처 확정).

| 모듈 | 책임 | 알아야 할 규칙 |
|---|---|---|
| `vss/config.py` | 모든 설정(`VSS_*` 환경변수)과 인덱스 fingerprint | "청킹" 구분 값을 바꾸면 재인덱싱, "검색" 구분은 재시작만 |
| `vss/chunker.py` | 대상 파일 수집(제외 규칙), AST 청킹(.py)·줄 윈도우(기타 코드)·마크다운 섹션(fence 인식) | 산출 레코드 필드명 고정 (`type·path·line_start·…·chunk_index`) |
| `vss/context_header.py` | 청크 머리에 경로·심볼 헤더 부착 (`VSS_CONTEXT_HEADER`) | |
| `vss/embedder.py` | Ollama bge-m3 임베딩 호출 | **폴백 없음** — 실패는 예외로 드러난다 |
| `vss/store/` | `chroma.py`·`pgvector.py`, 공통 계약은 `base.py` | `begin_build→add→promote` 순서만. 인덱스 상태의 정본은 저장소 자신 |
| `vss/lexical.py` | BM25 역색인(순수 표준 라이브러리)과 RRF 융합 | 융합은 순서만 바꾼다 — 판정은 벡터 점수 |
| `vss/indexer.py` | 전체 인덱싱 파이프라인, 진행률(메모리), `data/index_log.jsonl`, `repair` | 실패한 빌드는 자동 삭제하지 않는다 (증거) |
| `vss/search.py` | 벡터 검색 + BM25 융합 + 임계값 판정 | `top_score >= threshold ⟺ has_evidence`. 질의 임베딩은 인덱스가 저장한 fingerprint 의 모델을 쓴다 |
| `vss/prompt.py` | 프롬프트 형식의 정본, NO_EVIDENCE 판정 | `[N]` 은 contexts 인덱스+1 과 1:1 — 정렬·필터·재번호 금지 |
| `vss/references.py` | 답변의 `[N]` 파싱 → `references`(청크 단위)·`reference_files`(파일 단위) | 파일로 묶어도 `n` 은 원래 값 유지 |
| `vss/llm.py` | Ollama `/api/chat` 호출·스트리밍 | |
| `vss/chat.py` | `/v1/chat` 오케스트레이션(검색→프롬프트→LLM→출처), SSE 이벤트 `meta·delta·done·error` | 히스토리는 받아도 프롬프트에 넣지 않는다(0턴). 근거 없으면 LLM 을 부르지 않는다 |
| `vss/server.py` | 표준 라이브러리 HTTP 서버, 전 엔드포인트 | `VSS_TOKEN` 설정 시 전 요청 토큰 검사 |
| `vss/cli.py` | 서버와 같은 기능의 CLI (`health·index·search·ask·briefing·doctor·repair·…`) | |
| `vss/briefing.py`·`analysis.py` | 브리핑: 결정적 추출(AST·정규식) + LLM 요약, `data/briefings/` 캐시 | LLM 은 요약만 — 진입점·함수 헤더는 파서가 뽑는다 |
| `vss/eval/` | matrix×suite 평가 실행, Hit@k·MRR·no-evidence recall, `data/evaluation/runs·reports`, `sweep`(임계값 표) | run 에 fingerprint·commit·suite hash 가 기록된다 — 같을 때만 비교. `sweep` 은 값을 바꾸지 않는다 |
| `tests/` | 가짜 임베더·LLM 로 인덱싱→검색→채팅→평가 왕복 11 테스트 (Ollama 불필요) | |
| `scripts/` | `setup_ec2.sh`(EC2 설치) · `db_init.sql` · `make_status.py`(STATUS.md 생성) · `backup_pg.sh` · systemd 유닛 | |

## 구현 현황과 다음 작업

**지금 단계**: 서버가 EC2 에서 돌고 있고(PostgreSQL + pgvector), 데모 레포 2개가 인덱싱돼 **첫 기준선 수치가 나왔다**(2026-08-27).
2026-08-28 에 **프론트 연동 경로가 실제로 통과했다** — Extension 이 레포 이름(`api_test`)을 보내면 서버가 `api-test--ast` 로 검색한다(응답 `index_id` 로 확인).
같은 날 질의 흐름 결함 7건을 닫았다(출처 목록이 비는 문제 등). 왕복 테스트 노트북 12/12 · EC2 pgvector 10/10.
무엇을 재서 무엇이 증명됐고 왜 그렇게 정했는지는 **[docs/JOURNAL.md](docs/JOURNAL.md)** 와 [docs/RAG_BASELINE_20260827.md](docs/RAG_BASELINE_20260827.md) 에 있다.

**이어받는 사람이 할 일**: 처음이면 아래 「EC2 실행 순서」 1~5번을 그대로 붙여 넣으면 같은 상태가 된다. 이미 돌고 있는 서버를 이어받는다면 남은 것은 넷이다 —
① **답이 나와야 할 질문에 "근거 없음"이 나오는 문제**(코퍼스에 있는 주제를 물어도 `NO_EVIDENCE` 였다. `metadata.model` 이 `null` 이면 검색이 막힌 것, 모델 이름이 있으면 모델이 거절한 것 — 여기부터 가른다)
② `rag_lab` 배치·측정(데모 시나리오 S3·S4 가 여기 걸려 있다) ③ `api_test` gold 40문항(현재 8문항이라 판정 불가) ④ 생성 품질 측정(지금까지 잰 것은 검색까지다).
정확도 작업(청킹·임계값·모델 교체)은 전부 이 기준선과의 비교로 판정한다. **질문 몇 개를 던져 보고 판단하지 않는다** — 문항 하나가 흔드는 폭이 1/n 이다.

**설정이 없으면 기능도 없다**: 코드가 있어도 `.env` 한 줄이 빠지면 그 기능은 없는 것과 같다(8/28 에 `VSS_PROJECT_ALIASES` 로 겪었다).
"안 된다"는 보고를 받으면 `curl -s localhost:8200/health | jq '{store, projects, project_aliases}'` 를 먼저 본다 — 저장소·인덱스·별칭이 한 번에 나온다.

<!-- status:begin -->

_이 구역은 자동 생성됩니다 (2026-08-28 17:34 UTC+0900). 손으로 고치지 마세요._

**완료** (최근)

- 새 레포 골격 + 순수 로직 6개 복사 (SALVAGE.md) + AST 청커 이식 + Chroma/pgvector 저장 계층 + 단일 서버 + CLI
- 61문항 gold → `evaluation/suites/fastapi-cli-full.jsonl` 변환, `rag-lab-v1.jsonl` 36문항 초안(파일·심볼 검증 통과)
- (team) GitHub 레포 생성·push, EC2 에 clone (git 제외 폴더가 실제로 빠졌는지 `git status` 로 확인)
- (team) EC2 준비: `bash scripts/setup_ec2.sh`
- 코퍼스 규칙 확정 — api_test: `tests,admin/**,.snapshot-admin-backup/**` 제외 후보 / rag_lab: `data`(기본 제외)
- 점심 go/no-go (pgvector) — 네 조건 전부 만족해야 go

**진행 중**

- 데모 레포 배치: `~/repos/api_test`, `~/repos/rag_lab`(동결 사본), `~/repos/fastapi-cli`(대조군)
- 기준선 인덱싱: `<repo>--lines`(줄 윈도우, 헤더 off, BM25 on) 3개
- AST 인덱싱: `<repo>--ast`(ast-v1, 헤더 on, BM25 on) 3개
- 기준선 측정 1회: `python -m vss.eval run evaluation/matrices/{fastapi-cli,rag-lab,api-test}.json --note baseline`
- 코퍼스 동결: 데모 레포 2개의 revision 과 문서 집합 확정, DECISIONS 에 commit 기록. 이후 측정은 이 코퍼스에서만
- 첫 개선 시리즈 보고: baseline → ast+header → hybrid (레포 3개)
- K·Y 에게 `/v1/chat` SSE 계약(docs/API.md) 전달, EC2 주소·토큰 공유

**다음 작업**

- (team) 다섯 문서 검토·승인 (md) — 완료 조건: CHARTER 와 계획 문서의 "초안" 을 "현행" 으로, 첫 커밋
- 팀원(gold 담당): `api_test` 40문항 초안 시작 — `evaluation/README.md` 의 계약, `python -m vss.eval validate` 로 자가 검증
- (team) gold 담당에게 코퍼스 제외 규칙 전달 (md) — 완료 조건: evaluation/README.md 의 "코퍼스 제외 규칙" 절 링크를 팀 채널에 공유
- (team) EC2 → 레포 결과 반출 경로 확정 — 완료 조건: EC2 에서 `git push` 가 되거나, 대안이 문서에 적혀 있다
- 발표에 쓸 "RAG 끔/켬" 비교 질문 3개 고르기 (`rag:false` 플래그)

**최근 결정** (md 확정)

- EC2 반영은 보류: P 가 스냅샷 환경 작업 중이라 서버 재시작을 미룬다 (md, 대화 2026-08-28).
- DB 비밀번호는 오늘 바꾸지 않는다: PostgreSQL 이 `127.0.0.1:5432` 에만 바인딩(`ss` 실측 + `listen_addresses` 기본값)돼 외부 도달 경로가 없다 (md 판단 근거 제공, 대화 2026-08-28).
- `adocs/` 백업은 md 가 수동으로 한다: (md, 대화 2026-08-28).

**인덱스** (EC2 `hancom-team2-5th` · store pgvector · 스냅샷 2026-08-27 06:20 UTC)

- `api-test--ast` 1,674청크 · ast-v1 · header on · bm25 on · commit `2dea3d71`
- `api-test--lines` 1,622청크 · line-window-v1 · header off · bm25 on · commit `2dea3d71`
- `fastapi-cli--ast` 306청크 · ast-v1 · header on · bm25 on · commit `10d7e65a`
- `fastapi-cli--lines` 250청크 · line-window-v1 · header off · bm25 on · commit `10d7e65a`

**최근 평가** (`data/evaluation`)

- `20260827T061531Z-165f40` api-test / ast+header / vector / retrieval · n=6 · Hit@3 50% · MRR 0.50
- `20260827T061531Z-165f40` api-test / ast+header / vector / pipeline · n=6 · Hit@3 50% · MRR 0.50
- `20260827T061531Z-165f40` api-test / ast+header / hybrid / retrieval · n=6 · Hit@3 50% · MRR 0.42
- `20260827T061531Z-165f40` api-test / ast+header / hybrid / pipeline · n=6 · Hit@3 50% · MRR 0.42

<!-- status:end -->

수치의 정본은 `data/evaluation/runs/*.json` 과 `reports/*.md`(EC2 에서 커밋), 그 요약은 `python scripts/make_status.py` 가 만드는 `STATUS.md`(git 제외) 다. 문서에 손으로 적은 수치는 없다.

## 문서 (읽는 순서)

| 문서 | 무엇 | 언제 읽나 |
|---|---|---|
| `CHARTER.md` | 목표 · 범위 · 하지 않는 것 · 불변 조건 7 · 관문 날짜 | 무엇이든 하기 전에 |
| `README.md` (이 파일) | 구성 · 코드 구조 · 구현 현황 · 다음 작업 · 사용법 | 매일 |
| `docs/JOURNAL.md` | 회차별 판단 기록 — 무엇을 재서 무엇이 나왔고 왜 그렇게 정했나 | **설정값의 근거가 궁금할 때** |
| `docs/RAG_BASELINE_20260827.md` | 8/27 기준선 측정 상세. RAG 기초·용어부터 시작한다 | RAG 를 처음 볼 때 · 수치를 인용하기 전에 |
| `docs/API.md` | `/v1/chat` SSE 계약, `/index`·`/briefing` | Extension · 스냅샷 연동 |
| `evaluation/README.md` | gold 문항(JSONL) · matrix · run 기록 규칙 | 문항 작성 · 측정 |
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

인덱싱 대상은 `~/repos/<repo>` 에 둔다 — `evaluation/matrices/*.json` 의 `repository` 가 이 경로로 되어 있다. 폴더명은 언더스코어(`api_test`), 인덱스 이름만 하이픈(`api-test--ast`).

1단계를 돌렸으면 `~/repos`(= `/home/<계정>/repos`)가 이미 만들어져 있다. 홈 아래라 소유자가 접속 계정이므로 **WinSCP 로 그냥 끌어다 놓으면 된다** — sudo 도, 경로 권한 손질도 필요 없다.
`.git` 폴더를 포함해 통째로 올리면 revision 이 유지되고, 그러면 EC2 에 GitHub 자격증명을 둘 필요가 없다.
`rag_lab` 만 `data/`(4.5GB 인덱스 데이터)와 `.venv` 를 빼고 올린다 — 실제 코퍼스는 1MB 남짓이다.

원격에서 바로 받고 싶으면 대신 이렇게 한다:

```bash
cd ~/repos
git clone git@github.com:h5vision/api_test.git    api_test
git clone git@github.com:h5vision/fastapi-cli.git fastapi-cli
```

올린 뒤 revision 을 확인한다 — 8/28 코퍼스 동결에 기록할 값이다.

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

# 현행 — ast-v1 · 헤더 on · BM25 on (끝나면 브리핑 자동 생성)
python -m vss.cli index ~/repos/api_test    --project api-test--ast    $AST --exclude "$API_EXCLUDE"
python -m vss.cli index ~/repos/rag_lab     --project rag-lab--ast     $AST
python -m vss.cli index ~/repos/fastapi-cli --project fastapi-cli--ast $AST

python -m vss.cli projects                                      # 6개가 done 인지 · note 가 붙었는지 확인
```

`--note` 는 "이 인덱스를 왜 만들었나" 한 줄이다. 별도 파일이 아니라 **인덱스 자신의 meta** 에 저장돼 승격까지 따라가고 `projects` 출력에 나온다.
(공백이 들어가면 따옴표로 감싼다: `--note "8/27 기준선"`)

한 개라도 실패하면 기존 인덱스는 그대로 있고 임시 빌드만 남는다(선삭제 없음) — `python -m vss.cli repair` 로 확인하고 그 한 줄만 다시 돌린다.

### 4. 기준선 측정 3개

```bash
for m in api-test rag-lab fastapi-cli; do
  python -m vss.eval run evaluation/matrices/$m.json --note baseline
done
python -m vss.eval runs                                         # 이력 한 표
```

`run` 은 matrix 를 **하나만** 받는다 — 한 줄에 셋을 나열하면 실패한다. matrix 하나가 인덱스 2개 × 검색 프로필 2개 × 모드 2개 = 8셀이다.

### 5. 결과를 레포로 돌려보내기

노트북·팀은 `git pull` 로만 현황을 본다 (아무도 EC2 에 접속하지 않아도 된다). 이 단계를 빠뜨리면 README 현황 구역과 팀의 `git pull` 에 아무것도 반영되지 않는다.

```bash
mkdir -p data/ec2
python -m vss.cli projects --json > data/ec2/projects.json      # 인덱스 현황 스냅샷 (generated_at 포함)
git add data/ec2 data/evaluation && git commit -m "eval: baseline (chroma)" && git push
```

### 6. 프론트가 부를 이름 정하기 (별칭) · 서버 켜기

Extension 은 **레포 이름**(`api_test`)만 보내고, 그 질문에 어느 인덱스가 답할지는 서버가 정한다. 그래야 RAG 를 개선해 인덱스를 갈아탈 때 클라이언트를 고치지 않는다.
`.env` 에 한 줄 넣고 재시작하면 끝이고, 다음에 더 좋은 인덱스가 나오면 이 줄만 바꾼다.

```bash
# 실제로 존재하는 인덱스만 적는다 — 없는 인덱스를 가리키는 별칭은 404 를 만든다 (rag_lab 은 인덱스가 생긴 뒤에 추가)
echo 'VSS_PROJECT_ALIASES=api_test=api-test--ast,fastapi-cli=fastapi-cli--ast' >> .env
sudo systemctl restart vss-server && journalctl -u vss-server -f    # 포트 8200
curl -s localhost:8200/health | jq '.projects, .project_aliases'
python -m vss.cli ask "결제 요청은 어디서 처리되나요?" --project api_test   # [meta] index=... 로 어느 인덱스가 답했는지 보인다
```

별칭은 **질의 경로 전용**이다 — 인덱싱(`cli index`·`POST /index`)과 평가(`vss.eval`)는 인덱스 이름을 그대로 쓴다.
측정이 별칭을 타면 "어느 인덱스를 쟀는가" 가 흐려진다(불변 조건 6). 별칭이 없는 이름은 인덱스 이름 그대로 취급하고, 없으면 `project_not_found` 다 — 조용한 폴백은 없다.

서버가 뜬 뒤로는 인덱싱을 CLI 로 하지 않는다 — Chroma 는 한 프로세스만 열어야 안전하다. 아래 「낱개 명령」의 `POST /index` 를 쓴다.

### 7. ANN 이 recall 을 깎는지 확인 (측정 뒤 한 번)

pgvector 의 hnsw 는 근사 검색이다. 정확 검색으로 **질의만** 다시 해서 같은 수치가 나오는지 본다 — 재인덱싱은 필요 없다.

```bash
VSS_PG_EXACT=1 python -m vss.eval run evaluation/matrices/rag-lab.json --note "exact 대조"
python -m vss.eval runs         # 앞의 baseline 셀과 Hit@k 비교 (차이 0 또는 1/n 이내면 hnsw 를 그대로 쓴다)
```

차이가 크면 인덱스가 아니라 검색 파라미터 문제다 — `hnsw.ef_search` 를 올리거나 `VSS_PG_EXACT=1` 을 상시로 둔다(데모 규모에서는 정확 검색도 충분히 빠르다).

## 낱개 명령 — 인덱싱 · 질문 · 브리핑

```bash
python -m vss.cli index ~/repos/rag_lab --project rag-lab--ast --context-header on --bm25 on        # 기본 청커 ast-v1, 끝나면 브리핑 자동
python -m vss.cli index ~/repos/rag_lab --project rag-lab--lines --chunker line-window-v1 --context-header off --no-briefing   # 기준선
python -m vss.cli index --git https://github.com/org/repo --project demo --exclude "tests,docs/ko/**"  # clone 해서 인덱싱
python -m vss.cli index ~/repos/api_test --project api-test--ast --bm25 on --exclude "tests,admin/**,.snapshot-admin-backup/**"   # api_test 확정 제외 규칙(8/27)
python -m vss.cli projects                                                                             # --json: 스냅샷 출력
python -m vss.cli search "전체 인덱싱에서 선삭제 대신 쓰는 메서드는?" --project rag-lab--ast
python -m vss.cli ask    "전체 인덱싱에서 선삭제 대신 쓰는 메서드는?" --project rag_lab                # 별칭으로 (프론트와 같은 경로)
python -m vss.cli ask    "같은 질문" --no-rag                                                          # 발표용 비교 (검색 없이)
python -m vss.cli briefing --project rag-lab--ast --force
python -m vss.cli doctor
```

서버가 떠 있는 동안 인덱싱할 때는 CLI 대신 `POST /index` 를 쓴다 — Chroma 는 한 프로세스만 열어야 안전하다:

```bash
curl -s localhost:8200/index -H 'Content-Type: application/json' \
  -d '{"project_root":"~/repos/rag_lab","project_id":"rag-lab--ast"}'
curl -s "localhost:8200/index/status?project_id=rag-lab--ast"
```

## 서버

```bash
python -m vss.server --host 0.0.0.0 --port 8200          # 또는 sudo systemctl restart vss-server
curl -s localhost:8200/health | jq .projects
curl -N -s localhost:8200/v1/chat -H 'Content-Type: application/json' \
  -d '{"project_id":"rag-lab--ast","message":"임베딩이 실패하면 폴백이 있나요?","stream":true}'
```

## 평가

```bash
python -m vss.eval validate evaluation/matrices/rag-lab.json
python -m vss.eval run      evaluation/matrices/rag-lab.json --note baseline
python -m vss.eval runs                                   # 이력 한 표
python scripts/make_status.py                             # STATUS.md 생성 (인덱스 + 최근 run)
```

결과를 레포로 돌려보내는 두 줄은 위 「EC2 실행 순서」 5단계에 있다 — 측정을 다시 돌릴 때마다 그 단계를 반복한다(`--note` 와 커밋 메시지에 run 을 구분할 이름을 적는다).

## 테스트 (Ollama 없이)

```bash
python -m unittest discover tests -v
VSS_TEST_STORE=pgvector python -m unittest tests.test_roundtrip -v      # PostgreSQL 이 떠 있을 때
```

## 설정

모든 설정은 `vss/config.py` 의 환경변수(`.env`)로 바꾼다. 값과 기본값은 위 자동 구역의 표가 정본이다.
"청킹" 구분의 값은 인덱스 fingerprint 에 들어가므로 바꾸면 재인덱싱이 필요하고, "검색" 구분의 값은 서버 재시작만으로 바뀐다.
`VSS_TOKEN` 을 비워 두지 않으면 모든 요청에 `X-VSS-Token`(또는 `Authorization: Bearer`)이 필요하다.
