# vss_server

VSsVscodeEX 의 서버. 레포를 인덱싱하고(AST 청킹 · bge-m3 · Chroma 또는 pgvector · BM25), 질문에 **출처와 함께** 답하고(Ollama 스트리밍),
인덱싱이 끝나면 **프로젝트 브리핑**(Markdown)을 만든다. 표준 라이브러리 HTTP 서버 하나로 동작하고, 외부 의존성은 `chromadb` 와 `psycopg` 뿐이다.
2026-08-26 에 `rag_lab` 을 대체하는 새 레포로 시작했다(가져온 것은 `SALVAGE.md`). 목표·범위·불변 조건은 `CHARTER.md` 가 앵커다.

이 README 는 **인수인계 문서**다 — 구성 · 코드 구조 · 구현 현황(어디까지 됐고 다음이 무엇인지) · 사용법을 이 파일 하나로 파악할 수 있게 유지한다.

## 구성 — 어디서 무엇이 도는가

- **서버 한 대**: 팀 GPU 노드 EC2 `hancom-team2-5th`(주소·토큰은 md 가 팀 채널로 공유, 이 파일에는 적지 않는다). 포트 **8200**, `vss-server` systemd 서비스.
  같은 머신의 Ollama(11434)가 임베딩(`bge-m3`, 1024차원)과 생성(`qwen2.5-coder:7b`, 9/1 bake-off 로 교체 검토)을 맡는다.
- **저장소**: 현재 기본값은 Chroma(`data/index/`). 8/27(목) 점심 go/no-go 를 통과하면 같은 날 오후 PostgreSQL+pgvector(스키마 `rag`)로 바꾼다.
  스냅샷 서비스(P)는 같은 DB 의 `snapshot` 스키마를 쓴다. 두 저장소 모두 "빌드 → 승격" 방식이라 인덱싱 중에도 기존 인덱스가 서비스된다.
- **데모 코퍼스**: EC2 `/srv/repos/api_test`(앱형) · `/srv/repos/rag_lab`(문서 풍부) · `/srv/repos/fastapi-cli`(61문항 gold, 측정 대조군).
  인덱스 이름은 `<repo>--lines`(기준선) / `<repo>--ast`(현행) 처럼 청킹 방식을 붙인다.
  코퍼스 제외 규칙(8/27 확정): api_test 는 `tests,admin/**,.snapshot-admin-backup/**` 제외, 나머지 레포는 공통 기본 제외만 — 상세와 gold 문항 규칙은 `evaluation/README.md`.
- **클라이언트**: VSCode Extension(K·Y)은 `POST /v1/chat`(SSE) 하나만 부른다. 계약은 `docs/API.md`. 스냅샷 서비스(P)는 파일을 풀어 놓고 `POST /index` 를 부른다.
- **작업 방식**: 코드는 한 방향으로만 흐른다 — 노트북에서 작성·커밋·push → GitHub → EC2 에서 `git pull` 후 실행. **EC2 에서 파일을 직접 고치지 않는다**(고치면 다음 pull 에서 충돌하고, 어느 트리에서 나온 수치인지 추적이 끊긴다).
  반대 방향으로 돌아오는 것은 **EC2 가 커밋하는 두 가지**뿐이다: 측정 결과 `data/evaluation/`(run·report — 수치의 정본) 과 인덱스 현황 `data/ec2/projects.json`(`vss.cli projects --json` 출력, README 상태 구역의 원본).
  `.env`(주소·토큰·DSN)는 git 에 올리지 않고 EC2 에만 둔다. 팀원과 EC2 는 이 README 와 `CHARTER.md` 만 보면 된다.

<!-- config:begin -->

### 코드에서 뽑은 사실 (자동 생성)

| 종류 | 값 |
|---|---|
| HTTP 엔드포인트 | `GET /` · `GET /health` · `GET /v1/health` · `GET /projects` · `GET /v1/projects` · `GET /v1/models` · `GET /index/status` · `GET /index/exists` · `GET /briefing` · `GET /briefing.md` · `POST /v1/chat` · `POST /chat` · `POST /search` · `POST /v1/search` · `POST /prompt` · `POST /finalize` · `POST /index` · `POST /briefing` · `POST /bm25` |
| CLI (`python -m vss.cli`) | `health` · `projects` · `index` · `status` · `search` · `ask` · `briefing` · `bm25` · `repair` · `doctor` |
| 평가 (`python -m vss.eval`) | `validate` · `run` · `report` · `runs` |

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

폴더 구조 (최상위):

```text
docs/  API.md
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
| `vss/eval/` | matrix×suite 평가 실행, Hit@k·MRR·no-evidence recall, `data/evaluation/runs·reports` | run 에 fingerprint·commit·suite hash 가 기록된다 — 같을 때만 비교 |
| `tests/` | 가짜 임베더·LLM 로 인덱싱→검색→채팅→평가 왕복 7 테스트 (Ollama 불필요) | |
| `scripts/` | `setup_ec2.sh`(EC2 설치) · `db_init.sql` · `make_status.py`(STATUS.md 생성) · `backup_pg.sh` · systemd 유닛 | |

## 구현 현황과 다음 작업

**지금 단계**: 코드와 왕복 테스트는 완성(7/7 통과), EC2 는 아직 미배치라 인덱스·실측 수치가 없다.
이어받는 사람은 EC2 에 접속해 ① `bash scripts/setup_ec2.sh` 로 준비 → ② 데모 레포 3개 인덱싱(아래 사용법) → ③ `python -m vss.eval run` 으로 기준선 측정 순서로 진행하면 된다.
③ 이 끝나면 "EC2 → 레포로 결과 돌려보내기" 두 줄을 실행한다 — 그래야 이 README 의 현황 구역과 팀의 `git pull` 에 수치가 반영된다.

<!-- status:begin -->

_이 구역은 자동 생성됩니다 (2026-08-27 10:51 UTC+0900). 손으로 고치지 마세요._

**완료** (최근)

- 새 레포 골격 + 순수 로직 6개 복사 (SALVAGE.md) + AST 청커 이식 + Chroma/pgvector 저장 계층 + 단일 서버 + CLI
- 61문항 gold → `evaluation/suites/fastapi-cli-full.jsonl` 변환, `rag-lab-v1.jsonl` 36문항 초안(파일·심볼 검증 통과)
- 코퍼스 규칙 확정 — api_test: `tests,admin/**,.snapshot-admin-backup/**` 제외 후보 / rag_lab: `data`(기본 제외)

**진행 중**

- (team) GitHub 레포 생성·push, EC2 에 clone (git 제외 폴더가 실제로 빠졌는지 `git status` 로 확인)

**다음 작업**

- (team) 다섯 문서 검토·승인 (md) — 완료 조건: CHARTER 와 계획 문서의 "초안" 을 "현행" 으로, 첫 커밋
- (team) EC2 준비: `bash scripts/setup_ec2.sh` — 완료 조건: `python -m vss.cli health` 에서 모델 2개·dim=1024·store 표시, pgvector 왕복 검증 줄 출력
- 데모 레포 배치: `/srv/repos/api_test`, `/srv/repos/rag_lab`(동결 사본), `/srv/repos/fastapi-cli`(대조군) — 완료 조건: `git rev-parse HEAD` 세 개 기록
- 기준선 인덱싱 (Chroma): `<repo>--lines`(줄 윈도우, 헤더 off, BM25 on) 3개 — 완료 조건: `python -m vss.cli projects` 에 3개 done
- AST 인덱싱: `<repo>--ast`(ast-v1, 헤더 on, BM25 on) 3개

**최근 결정** (md 확정)

- [제안 E] 승인 — 기본 청커 ast-v1 + 맥락 헤더 on + BM25 on: "제안e는 수락, 기존에 순수 벡터검색만으로 인덱싱해놓은 것과 비교도 진행해야 할 수 있으니 감안한 구조" (md, 대화 2026-08-27).
- [제안 F] 승인 — `data/`·`.snapshot-admin-backup` 기본 제외: "안하면 오히려 문제" (md, 대화 2026-08-27).
- api_test 코퍼스 제외 규칙 확정: `VSS_EXCLUDE_GLOBS = "tests,admin/**,.snapshot-admin-backup/**"` (rag_lab·fastapi-cli 는 추가 제외 없음 — `data/`·백업은 F 의 기본 제외가 담당).

**인덱스** (이 머신 · store chroma)

- (인덱스 없음)

**최근 평가** (`data/evaluation`)

- (run 없음)

<!-- status:end -->

수치의 정본은 `data/evaluation/runs/*.json` 과 `reports/*.md`(EC2 에서 커밋), 그 요약은 `python scripts/make_status.py` 가 만드는 `STATUS.md`(git 제외) 다. 문서에 손으로 적은 수치는 없다.

## 문서 (읽는 순서)

| 문서 | 무엇 | 언제 읽나 |
|---|---|---|
| `CHARTER.md` | 목표 · 범위 · 하지 않는 것 · 불변 조건 7 · 관문 날짜 | 무엇이든 하기 전에 |
| `README.md` (이 파일) | 구성 · 코드 구조 · 구현 현황 · 다음 작업 · 사용법 | 매일 |
| `docs/API.md` | `/v1/chat` SSE 계약, `/index`·`/briefing` | Extension · 스냅샷 연동 |
| `evaluation/README.md` | gold 문항(JSONL) · matrix · run 기록 규칙 | 문항 작성 · 측정 |
| `SALVAGE.md` | `rag_lab` 에서 가져온 파일과 버린 것 | 출처 확인이 필요할 때 |

## EC2 에서 처음 시작

```bash
git clone <repo> ~/vss_server && cd ~/vss_server
bash scripts/setup_ec2.sh              # 패키지 · venv · PostgreSQL+pgvector · DB 초기화 · systemd (SKIP_PG=1 이면 Chroma 만)
source .venv/bin/activate
set -a; source .env; set +a            # VSS_STORE · VSS_PG_DSN · VSS_OLLAMA_URL · VSS_CHAT_MODEL
python -m vss.cli health               # 모델 2개 · dim=1024 · store 확인
```

## 인덱싱 · 질문 · 브리핑

```bash
python -m vss.cli index /srv/repos/rag_lab --project rag-lab--ast --context-header on --bm25 on        # 기본 청커 ast-v1, 끝나면 브리핑 자동
python -m vss.cli index /srv/repos/rag_lab --project rag-lab--lines --chunker line-window-v1 --context-header off --no-briefing   # 기준선
python -m vss.cli index --git https://github.com/org/repo --project demo --exclude "tests,docs/ko/**"  # clone 해서 인덱싱
python -m vss.cli index /srv/repos/api_test --project api-test--ast --bm25 on --exclude "tests,admin/**,.snapshot-admin-backup/**"   # api_test 확정 제외 규칙(8/27)
python -m vss.cli projects                                                                             # --json: 스냅샷 출력
python -m vss.cli search "전체 인덱싱에서 선삭제 대신 쓰는 메서드는?" --project rag-lab--ast
python -m vss.cli ask    "전체 인덱싱에서 선삭제 대신 쓰는 메서드는?" --project rag-lab--ast           # 스트리밍 출력
python -m vss.cli ask    "같은 질문" --no-rag                                                          # 발표용 비교 (검색 없이)
python -m vss.cli briefing --project rag-lab--ast --force
python -m vss.cli doctor
```

서버가 떠 있는 동안 인덱싱할 때는 CLI 대신 `POST /index` 를 쓴다 — Chroma 는 한 프로세스만 열어야 안전하다:

```bash
curl -s localhost:8200/index -H 'Content-Type: application/json' \
  -d '{"project_root":"/srv/repos/rag_lab","project_id":"rag-lab--ast"}'
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

## EC2 → 레포로 결과 돌려보내기 (EC2 에서 실행)

인덱싱·측정이 끝나면 이 두 줄로 결과를 레포에 남긴다. 노트북·팀은 `git pull` 로만 현황을 본다 (아무도 EC2 에 접속하지 않아도 된다).

```bash
python -m vss.cli projects --json > data/ec2/projects.json                 # 인덱스 현황 스냅샷 (generated_at 포함)
git add data/ec2 data/evaluation && git commit -m "eval: <run_id>" && git push
```

## 테스트 (Ollama 없이)

```bash
python -m unittest discover tests -v
VSS_TEST_STORE=pgvector python -m unittest tests.test_roundtrip -v      # PostgreSQL 이 떠 있을 때
```

## 설정

모든 설정은 `vss/config.py` 의 환경변수(`.env`)로 바꾼다. 값과 기본값은 위 자동 구역의 표가 정본이다.
"청킹" 구분의 값은 인덱스 fingerprint 에 들어가므로 바꾸면 재인덱싱이 필요하고, "검색" 구분의 값은 서버 재시작만으로 바뀐다.
`VSS_TOKEN` 을 비워 두지 않으면 모든 요청에 `X-VSS-Token`(또는 `Authorization: Bearer`)이 필요하다.
