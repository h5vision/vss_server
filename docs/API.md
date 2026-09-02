# vss_server API 계약 — Extension(K·Y) · 스냅샷(P) 연동용

- 기본 주소 `http://<EC2>:8200`. 인증은 `.env` 의 `VSS_TOKEN` 이 비어 있지 않을 때만 `X-VSS-Token: <token>` (또는 `Authorization: Bearer`).
- 모든 응답은 JSON(UTF-8). 스트리밍만 `text/event-stream`.
- **`project_id` 는 레포 이름을 보냅니다** (`api_test`, `rag_lab`). 어느 인덱스가 그 답을 내는지는 서버가 정합니다 —
  RAG 를 개선해 인덱스를 갈아타도 클라이언트는 고치지 않습니다. 서버가 실제로 검색한 인덱스는 응답의 `index_id` 로 확인할 수 있습니다.
  고르는 순서는 셋입니다 (응답의 `resolved_by` 로 어느 쪽이었는지 알 수 있습니다).
  1. `alias` — `.env` 의 `VSS_PROJECT_ALIASES` 가 손으로 고정한 것. 언제나 이깁니다 (`GET /health` 에서 확인)
  2. `exact` — 그 이름의 인덱스가 실제로 있음 (`cli--ast-v2` 처럼 인덱스를 직접 지목한 경우)
  3. `auto` — `<레포이름>--…` 인덱스 중 **청커 세대가 가장 새것**. 같으면 `indexed_at` 최신
  그래서 `cli` 만 보내면 서버가 `cli--ast-v2` 를 고릅니다. **새 인덱스를 만들면 설정을 고치지 않아도 그쪽으로 옮겨 갑니다.**
  지금 어느 레포 이름이 어느 인덱스에 닿는지는 `GET /projects` 의 `repos` 에 있습니다 (후보 목록까지).
  후보가 하나도 없으면 `project_not_found` 입니다 — `__auto__`·유사 이름 fallback 은 없습니다.
  이 선택은 **질의 경로 전용**입니다. `POST /index` 와 평가는 인덱스 이름을 그대로 씁니다.

## 질의 — `POST /v1/chat`

요청
```json
{
  "project_id": "api_test",                 // 레포 이름. 서버가 현재 최선의 인덱스로 보냅니다
  "message": "결제 요청은 어디서 처리되나요?",
  "stream": true,
  "context": "def pay(req): ...",          // 선택. 에디터에서 선택한 코드(문자열 또는 [{path,text}])
  "history": [],                          // 받지만 프롬프트에 넣지 않음 (0턴)
  "top_k": 4, "threshold": 0.54,          // 선택. 생략 시 서버 기본값
  "model_id": "qwen2.5-coder:7b",         // 선택. 서버가 허용할 때만 교체
  "rag": true                             // false 면 검색 없이 모델만 (발표용 비교)
}
```

`stream: false` 응답 (한 번에)
```json
{
  "answer": "결제는 PaymentService.process 에서 처리됩니다 [1]. 검증은 _validate 가 합니다 [2].",
  "no_evidence": false,
  "has_evidence": true,
  "cited": [1, 2],
  "references": [                         // 청크 단위. n 은 답변의 [N] 과 1:1 (재부여 없음)
    {"n": 1, "path": "src/payment.py", "type": "code", "line": 7, "line_start": 7, "line_end": 10,
     "section": null, "score": 0.71, "cited": true}
  ],
  "reference_files": [                    // 파일 단위. 화면 하단 "출처" 목록용
    {"path": "src/payment.py", "type": "code", "citations": [1, 2], "lines": [[7, 10], [12, 14]],
     "line": 7, "chunk_count": 2, "best_score": 0.71, "cited": true}
  ],
  "sources": [ {"path": "...", "type": "code", "line_start": 7, "line_end": 10, "section": null, "score": 0.71, "symbol": "PaymentService.process"} ],
  "source": [ {"file": "src/payment.py", "chunk": "def process(self, req): ...", "score": 0.71} ],   // P 의 옛 형식 호환
  "stage": {"retrieved": 4, "files": 2, "top_score": 0.71, "threshold": 0.54, "label": "근거 4건 확인 (2개 파일)"},
  "metadata": {
    "request_id": "…", "status": "completed", "rag_provider": "vss",
    "project_id": "api_test", "index_id": "api-test--ast",     // 보낸 이름 / 실제로 검색한 인덱스
    "model": "qwen2.5-coder:7b", "has_evidence": true, "reason": "ok", "top_score": 0.71, "threshold": 0.54,
    "history_used": 0,
    "timing": {"embed_ms": 210, "search_ms": 12, "bm25_ms": 4, "prompt_ms": 18, "pre_llm_ms": 240,
               "ttft_ms": 480, "gen_ms": 5200, "total_ms": 5500, "decode_tok_s": 48.3}
  }
}
```

근거가 임계값을 못 넘으면 LLM 을 부르지 않고 `answer: "NO_EVIDENCE"`, `no_evidence: true`, `references: []` 로 답합니다 (FN-B06).
모델이 스트리밍 중 `NO_EVIDENCE` 만 출력한 경우도 `done` 에서 `no_evidence: true` 가 됩니다. 이때 화면은 "근거 없음" 전용 표시로 전환합니다.

### `timing` 읽는 법

**구간이 겹칩니다. 더하지 마십시오.** `pre_llm_ms` 하나가 `embed_ms`·`search_ms`·`bm25_ms` 를 이미 포함합니다.

| 키 | 재는 구간 |
|---|---|
| `embed_ms` · `search_ms` · `bm25_ms` | 질의 임베딩 · 벡터 검색 · BM25 융합 (각각) |
| `pre_llm_ms` | **요청 시작 ~ LLM 호출 직전 누적** — 위 셋을 포함 |
| `prompt_ms` | **프롬프트 조립만** (보통 수 ms) |
| `ttft_ms` · `gen_ms` | 첫 토큰까지 · 생성 전체 |
| `total_ms` | 요청 시작 ~ 응답 완료 |

화면에 "검색에 걸린 시간"을 쓰려면 `pre_llm_ms` 입니다. `prompt_ms` 가 아닙니다.
근거를 못 찾아 LLM 을 안 부른 응답에는 프롬프트를 만들지 않으므로 **`prompt_ms` 키가 아예 없습니다**(0 이 아니라 부재).
SSE 의 `meta` 이벤트는 프롬프트 조립 **전**에 나가므로 거기에도 `prompt_ms` 가 없습니다 — `done` 의 `metadata.timing` 에 있습니다.

**⚠ 화면 분기는 `no_evidence` 로 하십시오. `metadata.has_evidence` 가 아닙니다.** 둘은 서로 다른 단계를 말합니다.

| 필드 | 뜻 | 언제 false 인가 |
|---|---|---|
| `metadata.has_evidence` | **검색** 단계 판정 (`top_score >= threshold`) | 검색이 임계값을 못 넘었을 때 |
| `no_evidence` (최상위) | **최종** 결과 — 답을 못 냈는가 | 위 경우 **또는** 검색은 됐는데 모델이 `NO_EVIDENCE` 를 낸 경우 |

그래서 `{"metadata": {"has_evidence": true}, "no_evidence": true}` 조합이 정상적으로 나옵니다 —
"근거는 찾았지만 그것으로 답할 수 없었다" 는 뜻입니다. 모순이 아닙니다.
`has_evidence` 로 분기하면 이 경우에 "근거 없음" 화면이 뜨지 않습니다.

`stream: true` — Server-Sent Events. 순서는 고정입니다.
```
event: meta      data: {request_id, project_id, index_id, model, rag, has_evidence, top_score, threshold, reason,
                        stage, sources, references(미리보기, cited=null), reference_files, timing}
event: stage     data: {"label": "답변 생성 중..."}
event: delta     data: {"text": "결제는 "}         ← 여러 번
event: done      data: {answer, references(인용된 것만), reference_files, cited, no_evidence, source, sources, metadata}
event: error     data: {"code": "llm_failed", "message": "...", "partial": "…"}
```
`meta` 의 출처는 생성 **전** 미리보기(검색된 전부)라 회색으로 그렸다가 `done` 의 출처로 교체하면 자연스럽습니다.
`has_evidence=false` 면 `meta` 다음에 바로 `done`(NO_EVIDENCE) 이 옵니다.

`rag: false` 로 부르면 `meta` 에 검색 관련 키(`index_id`·`top_score`·`threshold`·`reason`·`search_profile`·
`serving_profile`·`bm25_active`)가 **없고** `stage` 에는 `label` 만 있습니다. 두 형태를 모두 방어하십시오.

오류 코드: `bad_request`(400) · `project_not_found`(404) · `retrieval_failed`(503, 임베딩 서버) · `llm_failed`(502).

**⚠ 이 표는 `stream: false` 응답에만 적용됩니다.** `stream: true` 요청은 오류든 아니든 **항상 HTTP 200** 이고,
오류는 `event: error` 의 `code` 로 옵니다(값은 위 표와 같습니다). SSE 헤더가 처리보다 먼저 나가기 때문입니다.
스트리밍 클라이언트는 HTTP 상태가 아니라 `event: error` 를 봐야 합니다.

## 인덱싱

- `POST /index {"project_root": "/srv/snapshots/api_test/<rev>", "project_id": "api-test--ast", "force": false,
  "profile": {"context_header": true, "use_bm25": true, "exclude_globs": "tests,admin/**"}, "briefing": true,
  "note": "8/27 기준선"}`
  → 202 `{accepted: true, state: "running"}` / 409 `{accepted: false, reason: "already_running"}`.
  여기의 `project_id` 는 **만들 인덱스의 이름**입니다 (별칭을 타지 않습니다). `profile` 을 생략하면 서버 기본값(`.env`).
  `note` 는 이 인덱스를 왜 만들었는지 한 줄로, 인덱스 자신의 meta 에 저장되어 `GET /projects` 에 나옵니다.
  인덱싱이 끝나면 브리핑을 자동 생성합니다(`briefing: false` 로 끌 수 있음).
- `GET /index/status?project_id=` → `{state: none|running|indexing_lexical|promoting|done|failed|aborted, processed, total, chunk_count, error, briefing, index:{chunks, commit, fingerprint, indexed_at}}`
- `GET /index/exists?project_id=` → `{exists, chunks, commit}`
- `GET /health` → 아래 `projects` 목록에 더해 `project_aliases`(레포명 → 인덱스), `defaults`, 모델·저장소 정보

### `GET /projects?view=repos` — 프론트용 축약본 (권장)

레포 하나 = **배열 항목 하나**입니다. 무거운 인덱스 목록 없이 필요한 것만 옵니다.

```
GET /projects?view=repos                  전체
GET /projects?view=repos&commits=20       + 최근 커밋 20개
GET /projects?view=repos&project_id=cli   한 레포만
```

```json
{"repos": [
  {"name": "cli", "indexed": true,
   "index_id": "cli--ast-v2", "resolved_by": "auto", "candidates": ["cli--ast-v2"],
   "indexed_commit": "88ffe112…",     // 이 인덱스가 만들어진 시점의 커밋
   "head_commit":    "d65c9185…",     // 지금 디스크의 HEAD
   "stale": true,                     // 둘이 다르다 = 코드가 인덱스보다 앞서 갔다
   "dirty": false, "chunks": 412, "chunker": "ast-v2",
   "indexed_at": "…", "path": "/home/ubuntu/repos/cli",
   "commits": [{"sha": "d65c9185…", "short": "d65c918", "author": "…",
                "date": "2026-09-02T14:00:00+09:00", "message": "세 번째 커밋"}]}
]}
```

- **`name` 이 곧 `project_id`** 입니다 — 이 값을 `POST /v1/chat` 에 그대로 보내십시오.
- **인덱싱 안 된 레포도 같은 배열에** `indexed: false` 로 들어갑니다 (`VSS_REPOS_DIR` 이 설정된 경우).
- `commits` 는 `commits=N` 을 줬을 때만 채워집니다. 기본은 빈 배열이고, 최대 100개입니다.
  ⚠ `POST /index` 의 `remote` 로 clone 된 레포는 `--depth 1` 이라 **커밋이 1개만** 나옵니다.
- `git` 이 없거나 레포가 아니면 `head_commit`·`commits` 는 `null`/빈 배열이고, `stale` 은 `null` 입니다.

### `GET /projects` — 인덱스 단위 전체 (기존)

키는 **더하기만** 합니다. `projects` 배열은 언제나 있고, `project_id` 로 좁히면 한 개짜리가 됩니다.

```
GET /projects                                   전체
GET /projects?project_id=cli                    그 레포로 좁힘
GET /projects?project_id=cli&files=1            + 인덱스에 실제로 들어간 파일 목록
GET /projects?project_id=cli&files=1&symbols=1  + 파일별 심볼 이름
```

```json
{
  "projects": [{"project_id": "cli--ast-v2", "chunks": 412, "commit": "2dea3d71", "dirty": false,
                "indexed_at": "…", "use_bm25": true, "context_header": true, "chunker": "ast-v2",
                "note": "…", "briefing": {…},
                "head_commit": "9f8e7d6c", "stale": true}],
  "incomplete": [],
  "repos":     {"cli": {"index_id": "cli--ast-v2", "resolved_by": "auto", "candidates": ["cli--ast-v2"]}},
  "unindexed": [{"name": "rag_lab", "path": "…", "git": true, "commit": "a1b2…", "dirty": false}],

  "project_id": "cli", "index_id": "cli--ast-v2", "resolved_by": "auto",
  "candidates": ["cli--ast-v2"],
  "files": [{"path": "src/cli/main.py", "type": "code", "chunks": 4, "line_max": 87,
             "symbols": ["main", "App", "App.run"]}]
}
```

- **`repos`** — 프론트가 보낼 짧은 이름 → 지금 그 이름이 닿는 인덱스. 인덱싱만 해도 여기가 따라옵니다.
- **`stale`** — 인덱스의 `commit` 과 디스크의 현재 `head_commit` 이 다른가. **`true` 면 코드가 인덱스보다 앞서 간 것**이라 다시 인덱싱해야 합니다.
  둘 중 하나라도 모르면(`.git` 없음 등) `null` 입니다 — "낡았다" 로 단정하지 마십시오.
- **`unindexed`** — 디스크에는 있는데 인덱스가 하나도 없는 레포. 서버 `.env` 에 `VSS_REPOS_DIR` 이 있을 때만 나갑니다. **없으면 이 키 자체가 없습니다.**
- **`files`** — 인덱스에 **실제로 들어간** 파일만. 제외 규칙(`tests/`·`admin/` 등)에 걸린 파일은 여기 없습니다.
  `symbols` 는 `symbols=1` 일 때만, 그것도 심볼이 있는 파일에만 붙습니다(문서 파일에는 없습니다).
- `project_id` 를 줬는데 그 이름의 인덱스가 없으면 `files=1` 요청은 **404 `project_not_found`** 입니다.

스냅샷(P) 과의 경계: 스냅샷 서비스가 레포를 `/srv/snapshots/<project_id>/<revision>/` 에 풀어 놓고 위 `/index` 를 부릅니다. 서버는 DB 스키마를 모릅니다.
같은 `project_id` 로 다시 부르면 저장소에 새 빌드가 생기고 성공했을 때만 교체됩니다 (실패하면 이전 인덱스 유지).
⚠ 이 문장의 "빌드" 와 스냅샷 경로의 `<revision>` 은 **다른 것**입니다 — 앞은 우리 저장소의 인덱스 세대, 뒤는 P 가 발급하는 코드 버전입니다.
지금 `POST /index` 는 뒤쪽 `revision` 을 받는 필드가 없습니다 (P 와 합의 대기).

## 브리핑

- `GET /briefing?project_id=` → JSON `{ok, briefing(Markdown), references, reference_files, structure{entry_points, key_dirs, docs, ...}, routes, mermaid, generated_at, model}` (404 = 아직 없음)
- `GET /briefing.md?project_id=` → Markdown 원문 (`fetch().then(r => r.text())`)
- `POST /briefing {"project_id": "...", "force": true, "model": "..."}` → 재생성 (캐시가 있으면 `cached: true` 로 즉시 반환)

Markdown 구성: `# 이름` / `## 이 프로젝트는` / `## 문서 요약` / `## 진입점` / `## 진입점별 함수 목록` / `## 기능 목록` / `## 아키텍처 (모듈 import 관계)` (Mermaid) / `## 근거`.

## 디버그·평가용

- `POST /search {query, project_id, top_k?, threshold?, use_bm25?}` → `{has_evidence, contexts[], top_score, threshold, reason, bm25_active, timing}`
- `POST /prompt {query, project_id, context?}` → `{has_evidence, messages[], sources, references, reference_files, timing}` (LLM 호출 없음)
- `POST /finalize {answer, sources}` → `{answer, references, reference_files, cited, no_evidence}` (문자열 처리만)
- `POST /bm25 {project_id}` → 역색인 재구축 · `GET /v1/models` → Ollama 모델 목록 · `GET /health`

## 타임아웃 권장

`/health` `/projects` `/index/*` 10초 · `/search` `/prompt` 60초 · `/v1/chat` 스트리밍 180초(첫 이벤트까지 60초) · `/briefing` POST 300초.
