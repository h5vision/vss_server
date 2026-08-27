# vss_server API 계약 — Extension(K·Y) · 스냅샷(P) 연동용

- 기본 주소 `http://<EC2>:8200`. 인증은 `.env` 의 `VSS_TOKEN` 이 비어 있지 않을 때만 `X-VSS-Token: <token>` (또는 `Authorization: Bearer`).
- 모든 응답은 JSON(UTF-8). 스트리밍만 `text/event-stream`.
- **`project_id` 는 레포 이름을 보냅니다** (`api_test`, `rag_lab`). 어느 인덱스가 그 답을 내는지는 서버가 정합니다 —
  RAG 를 개선해 인덱스를 갈아타도 클라이언트는 고치지 않습니다. 서버가 실제로 검색한 인덱스는 응답의 `index_id` 로 확인할 수 있습니다.
  현재 매핑은 `GET /health` 의 `project_aliases` 에 있고, 매핑이 없는 이름은 인덱스 이름 그대로 취급합니다(`GET /projects` 의 값).
  일치하는 인덱스가 없으면 `project_not_found` 입니다 — `__auto__`·유사 이름 fallback 은 없습니다.
  별칭은 **질의 경로 전용**입니다. `POST /index` 와 평가는 인덱스 이름을 그대로 씁니다.

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
    "timing": {"embed_ms": 210, "search_ms": 12, "prompt_ms": 240, "ttft_ms": 480, "gen_ms": 5200, "total_ms": 5500, "decode_tok_s": 48.3}
  }
}
```

근거가 임계값을 못 넘으면 LLM 을 부르지 않고 `answer: "NO_EVIDENCE"`, `no_evidence: true`, `references: []` 로 답합니다 (FN-B06).
모델이 스트리밍 중 `NO_EVIDENCE` 만 출력한 경우도 `done` 에서 `no_evidence: true` 가 됩니다. 이때 화면은 "근거 없음" 전용 표시로 전환합니다.

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

오류 코드: `bad_request`(400) · `project_not_found`(404) · `retrieval_failed`(503, 임베딩 서버) · `llm_failed`(502).

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
- `GET /projects` → `{projects: [{project_id, chunks, commit, indexed_at, use_bm25, context_header, chunker, note, briefing}], incomplete: [...]}`
- `GET /health` → 위 `projects` 목록에 더해 `project_aliases`(레포명 → 인덱스), `defaults`, 모델·저장소 정보

스냅샷(P) 과의 경계: 스냅샷 서비스가 레포를 `/srv/snapshots/<project_id>/<revision>/` 에 풀어 놓고 위 `/index` 를 부릅니다. 서버는 DB 스키마를 모릅니다.
같은 `project_id` 로 다시 부르면 새 revision 이 빌드되고 성공했을 때만 교체됩니다 (실패하면 이전 인덱스 유지).

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
