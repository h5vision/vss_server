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
  "model_id": "qwen3.8:27b",              // 선택. Ollama 에 **올라온** 모델만 (GET /v1/models). 없으면 503 model_not_loaded
  "rag": true,                            // false 면 검색 없이 모델만 (발표용 비교)
  "client_request_id": "ui-20260902-0001" // 선택. 그대로 request_id 가 되어 서버 로그에 남습니다 (아래 「질의 로그」)
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
    "model": "qwen3.8:27b", "has_evidence": true, "reason": "ok", "top_score": 0.71, "threshold": 0.54,
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

`meta.search_profile.rerank` 는 휴리스틱 재정렬(같은 파일 청크 상한·`tests/` 경로 뒤로)이 이 응답에 적용됐는지입니다 (2026-09-07).
인덱스 청커가 `ast-v3` 이상이면 자동으로 `true` 이고, 그때 `per_file_cap`·`demote_globs` 값이 같이 실립니다. 순서만 바뀌고 `top_score`·`has_evidence` 는 그대로입니다. 프론트가 할 일은 없습니다.

오류 코드: `bad_request`(400) · `project_not_found`(404) · `retrieval_failed`(503, 임베딩 서버) · `model_not_loaded`(503, 아래) · `llm_failed`(502, Ollama 접속·생성 실패).

**서버는 모델을 올리지 않습니다** (2026-09-05). 모델 이름을 Ollama 에 보내는 것이 곧 로드 요청이고, VRAM 이 모자라면 상주 모델이 내려갑니다.
그래서 생성 모델은 지금 Ollama 메모리에 올라온 completion 모델(`GET /v1/models`) 중에서만 고릅니다 — 순서는
① `model_id` 가 있으면 그것(올라와 있지 않으면 **다른 모델로 바꾸지 않고** 실패) ② 없으면 서버 기본 모델 ③ 그것도 안 올라와 있으면 올라온 것 중 첫 번째.
하나도 없으면 `model_not_loaded` 이고 `data` 에 `requested`(요청값 또는 null)·`loaded`(올라온 목록)가 실립니다. 모델을 띄우고 내리는 것은 운영자가 합니다.

**⚠ 이 표는 `stream: false` 응답에만 적용됩니다.** `stream: true` 요청은 오류든 아니든 **항상 HTTP 200** 이고,
오류는 `event: error` 의 `code` 로 옵니다(값은 위 표와 같습니다). SSE 헤더가 처리보다 먼저 나가기 때문입니다.
스트리밍 클라이언트는 HTTP 상태가 아니라 `event: error` 를 봐야 합니다.

### 질의 로그 — "그 질문이 서버까지 왔나"

서버는 `POST /v1/chat` 요청 하나를 DB 한 행으로 남깁니다 (`rag.query_log`). **요청·응답 형식은 바뀌지 않습니다** — 프론트가 고칠 것은 없습니다.
다만 문의할 때 이걸 쓰면 원인 찾는 시간이 줄어듭니다.

- 요청에 `client_request_id` 를 실으면 그 값이 그대로 `request_id` 가 되고 응답(`metadata.request_id`)과 DB 행에 **같은 값**으로 남습니다.
  화면에 안 보여도 되니 로그에만 남겨 두고, "이 질문이 안 됩니다" 라고 할 때 그 값을 같이 주십시오.
- 남는 것: `request_id` · `project_id` · `index_id` · `resolved_by` · `model` · 질문 본문 · `outcome` · `has_evidence` · `top_score` · `threshold` · `reason` · `error_code` · `timing`.
  **답변 본문은 남기지 않습니다.**
- `outcome` 이 요청의 결말입니다.

| `outcome` | 뜻 | 프론트에서 보이는 모습 |
|---|---|---|
| `answered` | 답이 나갔다 | 정상 |
| `no_evidence` | 검색이 임계값을 못 넘어 **LLM 을 부르지 않았다** | `answer: "NO_EVIDENCE"`, `no_evidence: true` |
| `error` | 중간에 실패 (`error_code` 에 이유) | `event: error` 또는 4xx·5xx |

- `rag: false` 요청은 **남기지 않습니다.**
- `stream: true` 로 받다가 도중에 연결을 끊으면 그 요청은 행이 안 남습니다. `stream: false` 는 영향 없습니다.
- 서버 `.env` 에 `VSS_QUERYLOG_DSN` 이 없으면 아무것도 안 남습니다(기본값). 켜고 끄는 것은 서버 쪽 설정이라 클라이언트와 무관합니다.

## 인덱싱

인증이 켜져 있으면 아래 라우트도 전부 `X-VSS-Token` 이 필요합니다 (「스냅샷(P) 연동 · 인증」 참조).

- `POST /index {"project_root": "/srv/snapshots/api_test/<rev>", "project_id": "api-test--ast", "force": false,
  "profile": {"context_header": true, "use_bm25": true, "exclude_globs": "tests,admin/**"}, "briefing": true,
  "note": "8/27 기준선"}`
  → 202 `{accepted: true, state: "running"}` / 409 `{accepted: false, reason: "already_running"}`.
  여기의 `project_id` 는 **만들 인덱스의 이름**입니다 (별칭을 타지 않습니다). `profile` 을 생략하면 서버 기본값(`.env`).
  `note` 는 이 인덱스를 왜 만들었는지 한 줄로, 인덱스 자신의 meta 에 저장되어 `GET /projects` 에 나옵니다.
  인덱싱이 끝나면 브리핑을 자동 생성합니다(`briefing: false` 로 끌 수 있음).
- `POST /index {"remote": "git@github.com:h5vision/api_test.git", "project_id": "api-test--ast"}`
  → `project_root` 대신 `remote` 만 줘도 됩니다. 서버가 `~/repos/<레포이름>` 에 `git clone --depth 1`(이미 있으면 fetch + reset) 하고 그 경로를 `project_root` 로 씁니다.
  ⚠ `--depth 1` 이라 그 레포의 커밋 목록은 1개만 보입니다. 인증이 필요한 remote 는 EC2 에 자격증명이 없어 실패합니다.
- `GET /index/status?project_id=` → `{state: none|running|indexing_lexical|promoting|done|failed|aborted, processed, total, chunk_count, error, briefing, index:{chunks, commit, dirty, fingerprint, indexed_at, project_root, bm25_count}, incomplete[]}`
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

`projects[]` 의 각 항목에 **`current`** 가 있습니다 — "그 레포 이름으로 물으면 지금 이 인덱스가 답한다" 는 뜻입니다.
같은 레포의 옛 세대(`api-test--ast` 옆의 `api-test--ast-v2`)는 `false` 입니다.
**`?only=current` 를 주면 그것만 남습니다** — 목록에서 옛 인덱스를 숨기고 싶을 때 씁니다.

```
GET /projects              api-test--ast(false) · api-test--ast-v2(true) · api-test--lines(false) · vision(true)
GET /projects?only=current api-test--ast-v2 · vision
```


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

## 스냅샷(P) 연동

경계: 스냅샷 서비스가 `POST /index` 를 부릅니다. 서버는 `snapshot` 스키마를 모릅니다 — 같은 PostgreSQL 이지만 우리가 쓰는 것은 `rag` 스키마뿐이고, 서버가 스냅샷 백엔드의 API 를 부르는 일도 없습니다.

**합의된 방식(2026-09-05)**: P 가 `{"remote": "<git URL>", "project_id": "..."}` 로 부르고 **서버가 clone** 합니다. 파일을 미리 풀어 둘 필요가 없습니다.
이미 풀어 둔 디렉터리가 있으면 `project_root` 로 그 경로를 줘도 됩니다. 어느 쪽이든 넘어오는 값은 `project_id` · `revision`(지금은 `note` 로) · 소스 위치 셋입니다.

### 인증

서버 `.env` 에 `VSS_TOKEN` 이 있으면 **모든 라우트**가 헤더를 검사합니다. 비어 있으면 검사하지 않습니다.

```http
X-VSS-Token: <shared-secret>
```

`Authorization: Bearer <shared-secret>` 도 같게 받습니다. 불일치·누락은 **401 `{"error": "unauthorized"}`** 이고, 라우트별 예외는 없습니다.
이 값은 P → vss_server 방향 전용입니다. 반대 방향(vss_server → 스냅샷 백엔드)의 토큰과 같은 값을 쓰지 마십시오.

### `project_id` 이름 규칙 ⚠

우리는 `<레포이름>--<변형>` 으로 씁니다. **`--` 뒤는 청커 세대**(`ast-v3`·`ast-v2`·`ast-v1`·`line-window-v1`)를 뜻합니다.
질의가 `--` 없는 짧은 이름(`api-test`)으로 오면 서버가 `<레포이름>--*` 중 **청커 세대가 새것**을, 같으면 `indexed_at` 이 최신인 것을 고릅니다(응답 `resolved_by: "auto"`).

그래서 `--` 뒤에 **브랜치 이름을 넣으면 안 됩니다.** `vss-server--main` 과 `vss-server--module` 을 함께 만들면 둘 다 같은 세대로 잡혀
짧은 이름으로 물었을 때 **어느 브랜치가 답할지 시각 순서로 정해집니다**. 브랜치를 구분해야 하면 `@` 로 붙이십시오: `<레포이름>@<브랜치>--<변형>`
(예: `vss_server@main--ast-v2`, `vss_server@module--ast-v2`). `p.split("--", 1)[0]` 이 레포 키를 뽑을 때 `@브랜치`까지 그대로 붙어 나오므로,
`auto` 후보군이 브랜치별로 자동으로 나뉩니다 — `vss_server@main` 으로 물으면 `main` 브랜치의 인덱스만 후보가 됩니다.
이 접두사는 remote clone 이 쓰는 로컬 디렉터리 이름(`<레포이름>@<브랜치>`)과도 같은 문법이라 헷갈리지 않습니다.

### 실패와 재시도

- 불변 조건: **선삭제하지 않습니다.** 빌드 → 임베딩 전부 성공 → 승격. 실패한 빌드는 자동으로 지우지 않습니다.
- 실패해도 **이전 인덱스는 그대로 서비스됩니다.** `state: "failed"` 이고 `error` 에 `"<예외이름>: <메시지>"` 가 들어갑니다.
- 실패한 임시 빌드는 `GET /index/status` 의 `incomplete[]` 에 남습니다. 정리는 서버 쪽에서 `python -m vss.cli repair` 로 하며, **P 가 할 일은 없습니다.**
- 같은 `project_id` 가 이미 도는 중이면 **409** `{accepted: false, reason: "already_running", heartbeat_age_s}`. 재시도는 `state` 가 `done`·`failed`·`aborted` 가 된 뒤에 하십시오.
- heartbeat 가 **300초** 끊기면 `state` 가 `aborted` 로 바뀌고, 그때는 같은 이름으로 다시 시작할 수 있습니다.
- `project_root` 가 디렉터리가 아니면 `{accepted: false, reason: "not_a_directory"}`, 두 값 다 없으면 **400 `project_root, project_id required`**.
- 브리핑 생성이 실패해도 인덱싱은 `done` 입니다 (`briefing: "failed"`, `briefing_error`). 인덱스 성공 판정에 브리핑을 넣지 마십시오.

### 진행률 폴링

`GET /index/status?project_id=` 를 **2~5초** 간격으로 봅니다.

| 필드 | 언제 채워지나 |
|---|---|
| `total` | `running` 진입 직후 = 인덱싱 대상 **파일 수** (청크 수가 아닙니다) |
| `processed` | 임베딩 배치가 끝날 때마다 갱신되는 처리된 파일 수 |
| `chunk_count` | 지금까지 만든 청크 수. 배치마다 갱신됩니다 |
| `index` | 승격이 끝난 뒤에만. 그전에는 **이전 세대의 값**이 보입니다 |

`state` 는 `running` → `indexing_lexical`(BM25) → `promoting` → `done` 순입니다.

### `index.commit` 의 의미

`index.commit` 은 **인덱싱한 디렉터리에서 `git rev-parse HEAD` 로 읽은 코퍼스 레포의 커밋**입니다. vss_server 자신의 커밋이 아닙니다.
`.git` 이 없거나 git 이 실패하면 `null` 입니다. `dirty` 는 그 시점 워킹트리가 깨끗했는지입니다.

인덱싱이 끝났는지 확인할 때는 `state == "done"` 과 함께 `index.commit` 이 P 가 기대한 revision 과 같은지 보십시오 —
`state` 만 보면 **이전 세대의 인덱스가 남아 있는 경우와 구분되지 않습니다**.

### 재생성

같은 `project_id` 로 다시 부르면 새 빌드가 생기고 성공했을 때만 교체됩니다.
⚠ 여기의 "빌드" 와 스냅샷 경로의 `<revision>` 은 **다른 것**입니다 — 앞은 우리 인덱스 세대, 뒤는 P 가 발급하는 코드 버전입니다.
지금 `POST /index` 는 뒤쪽 `revision` 을 받는 필드가 없습니다 (P 와 합의 대기). 필요하면 `note` 에 `"snapshot <sha>"` 로 넣어 두면 `GET /projects` 에 나옵니다.

브리핑을 나중에 다시 만들 때는 `POST /briefing {project_id}` 로 충분하지만, **인덱싱된 적 없는 이름**이면
`404 {"ok": false, "reason": "project_root_unknown"}` 이므로 `project_root` 를 함께 주십시오.

## 브리핑

- `GET /briefing?project_id=` → JSON `{ok, briefing(Markdown), references, reference_files, structure{entry_points, key_dirs, docs, ...}, routes, mermaid, generated_at, model}` (404 = 아직 없음)
- `GET /briefing.md?project_id=` → Markdown 원문 (`fetch().then(r => r.text())`)
- `POST /briefing {"project_id": "...", "force": true, "model": "..."}` → 재생성 (캐시가 있으면 `cached: true` 로 즉시 반환)
  - `model` 은 `/v1/chat` 의 `model_id` 와 같은 규칙(올라온 모델만). 없으면 `503 {"ok": false, "reason": "model_not_loaded", "requested", "loaded"}` 이고 파일은 쓰지 않습니다.
  - `POST /index` 뒤의 자동 브리핑도 같은 규칙입니다. 모델이 없으면 `GET /index/status` 에 `briefing: "failed"`, `briefing_error: "model_not_loaded"` 로 남고 **인덱스는 done 그대로**입니다.

Markdown 구성: `# 이름` / `## 이 프로젝트는` / `## 문서 요약` / `## 진입점` / `## 진입점별 함수 목록` / `## 기능 목록` / `## 아키텍처 (모듈 import 관계)` (Mermaid) / `## 근거`.

## 디버그·평가용

- `POST /search {query, project_id, top_k?, threshold?, use_bm25?}` → `{has_evidence, contexts[], top_score, threshold, reason, bm25_active, timing}`
- `POST /prompt {query, project_id, context?}` → `{has_evidence, messages[], sources, references, reference_files, timing}` (LLM 호출 없음)
- `POST /finalize {answer, sources}` → `{answer, references, reference_files, cited, no_evidence}` (문자열 처리만)
- `POST /bm25 {project_id}` → 역색인 재구축 · `GET /v1/models` → Ollama 에 **올라온** completion 모델 목록(`/api/ps` 기준. `model_id`·`model` 에 쓸 수 있는 값은 이것뿐) · `GET /health`

## 타임아웃 권장

`/health` `/projects` `/index/*` 10초 · `/search` `/prompt` 60초 · `/v1/chat` 스트리밍 180초(첫 이벤트까지 60초) · `/briefing` POST 300초.
`POST /index` 자체는 즉시 202 로 돌아오므로 10초면 됩니다 — 인덱싱이 끝나기를 기다리는 것은 `GET /index/status` 폴링 쪽입니다.
`GET /v1/models` 는 Ollama 를 동기로 두 번 부르고 각각 30초 타임아웃이라 최악의 경우 60초까지 걸립니다.
