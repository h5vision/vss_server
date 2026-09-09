# Module Chat Observability / Conversation Trace

## Status

2026-09-09 KST 기준 신규 Module-only 기능입니다. 이 문서는 Chat 관측 기능의 정본 계약입니다.
로컬 구현은 Alembic/ORM, transparent `/v1/chat` streaming gateway, VSS SSE reducer, Ollama runtime sampling, Admin read API와 3-pane Chat UI까지 완료됐습니다. Gateway는 기본 비활성(`SNAPSHOT_CHAT_OBSERVABILITY_ENABLED=false`)이며 아직 AWS ingress `11500`은 전환하지 않았습니다.

## 1. Ownership boundary

VSS는 계속해서 다음 의미론을 단독 소유합니다.

- `/v1/chat` 요청 처리
- project/index resolve
- query embedding
- vector/BM25/symbol search와 rerank
- evidence selection
- prompt construction
- sLLM/Ollama model selection과 generation
- citation/finalize

Module은 위 의미론을 재구현하거나 바꾸지 않습니다. 다만 사용자 승인에 따라 VSS 앞에 **투명한 Chat Observability Gateway**를 둘 수 있습니다. 이 Gateway의 책임은 다음으로 제한합니다.

- conversation/message/response/trace 식별자 발급·상관관계
- `client_request_id`를 이용한 VSS request ID exact correlation
- VSS HTTP/SSE를 의미 변경 없이 relay
- VSS가 이미 노출하는 metadata/stage/delta/done/error 이벤트 영속화
- Ollama runtime 상태의 보조 관측
- Admin Web의 일반 LLM Chat transcript + response trace/debug inspector 제공

Gateway는 query, context, model, top_k, threshold, RAG 설정, VSS 응답을 임의 변경하지 않습니다. 관측 저장 실패는 Chat 응답을 실패시키지 않는 fail-open이 기본입니다.

## 2. User-visible model

사용자가 보는 상위 단위는 `Conversation`입니다.

```text
Conversation
  ├─ User Message
  ├─ Assistant Message
  │    └─ Response / Trace
  │         ├─ request received
  │         ├─ retrieval metadata
  │         ├─ embedding/search timings
  │         ├─ model runtime observations
  │         ├─ streaming deltas
  │         └─ done/error
  ├─ User Message
  └─ Assistant Message
       └─ Response / Trace
```

한 번의 assistant 생성은 하나의 `Response`이며 하나의 `trace_id`를 가집니다. `response_id`와 `trace_id`는 초기에는 1:1이어도 의미를 분리합니다. 향후 retry/regenerate가 같은 user message에 여러 Response를 만들 수 있기 때문입니다.

## 3. Identity model

VSS 자체는 현재 사람의 identity를 소유하지 않습니다. 따라서 Module은 requester 신뢰 수준을 명시적으로 저장합니다.

`requester_type`:

- `authenticated_user`: 신뢰 가능한 로그인/session actor
- `client_instance`: 안정적인 client instance ID만 식별 가능
- `admin`: Admin Debug Console에서 생성
- `internal`: 내부 smoke/test/automation
- `unknown`: 신뢰 가능한 식별자가 없음

사람의 신원을 IP/User-Agent로 추측하지 않습니다. 네트워크 정보는 필요한 경우 별도 제한·redaction된 diagnostics일 뿐 identity가 아닙니다.

`origin` 예시:

- `vision_frontend`
- `admin_debug`
- `api`
- `internal_test`
- `unknown`

## 4. Exact request correlation

VSS `run_chat()`은 기존에 `client_request_id`를 받으면 그대로 `request_id`로 사용합니다. Gateway는 VSS에 전달하기 전 trace ID를 생성하고 **caller가 보낸 `client_request_id`와 무관하게 Gateway trace ID로 canonicalize**합니다. 관측 경로에서는 Module이 correlation key를 소유해야 VSS request와 DB trace가 항상 1:1이기 때문입니다.

```text
Module trace_id == VSS client_request_id == VSS request_id
```

이 식별자는 사람 identity와 별개입니다.

## 5. Persistence model

Alembic `0011_chat_observability`에서 다음 테이블을 추가합니다.

### `snapshot.chat_conversations`

- `conversation_id` UUID PK
- `title` nullable
- `requester_type`
- `requester_id` nullable
- `client_instance_id` nullable
- `origin`
- `project_id` nullable
- `content_capture_mode`
- `status`
- `created_at`, `updated_at`, `last_message_at`

### `snapshot.chat_messages`

- `message_id` UUID PK
- `conversation_id` FK
- `sequence` monotonically increasing per conversation; transcript order source of truth
- `role`: `user|assistant|system`
- `content` nullable according to capture mode
- `content_sha256` nullable
- `content_length`
- `requester_id` nullable
- `created_at`

### `snapshot.chat_responses`

- `response_id` UUID PK
- `trace_id` UUID unique
- `conversation_id` FK
- `user_message_id` FK
- `assistant_message_id` nullable FK
- `vss_request_id`
- `project_id`, `index_id`, `resolved_by`
- `chat_model`, `embedding_model`
- `status`, `outcome`
- retrieval/evidence summary
- timing/token metrics exposed by VSS
- `error_code`, sanitized `error_detail`
- `started_at`, `first_token_at`, `completed_at`

### `snapshot.chat_trace_events`

Append-only trace event stream.

- `event_id` UUID PK
- `trace_id` FK to response trace
- `sequence` monotonically increasing per trace
- `event_type`: `request|runtime_snapshot|meta|stage|delta_batch|done|error`
- `elapsed_ms`
- `payload` JSON
- `created_at`

Raw delta는 token마다 한 row를 만들지 않고 bounded batch로 저장합니다.

### `snapshot.chat_model_observations`

- `observation_id` UUID PK
- `trace_id` FK
- `role`: `embedding|completion`
- `model_name`
- `stage`
- `runtime_available`
- `resident`
- `observed_at`

## 6. Content capture policy

기본값은 `metadata`입니다.

- `metadata`: 질문/답변/source 본문 저장 안 함. hash/length/reference/timing만 저장
- `question_answer`: user/assistant 최종 본문 저장, source chunk 본문은 저장 안 함
- `full_debug`: 제한된 기간에만 delta/context 등 추가 debug payload 저장

항상 금지:

- token / Authorization / API key / credential / DSN
- embedding vector
- 원본 secret
- 무제한 upstream error body

`full_debug`는 Administrator 전용이며 짧은 retention을 별도 정책으로 둡니다.

### Gateway runtime configuration

- `SNAPSHOT_CHAT_OBSERVABILITY_ENABLED=false` 기본값. `true`일 때만 Module `/v1/chat` relay를 엽니다.
- `SNAPSHOT_CHAT_TRACE_CONTENT_MODE=metadata|question_answer|full_debug`
- `SNAPSHOT_CHAT_REQUEST_MAX_BYTES`는 request body 상한입니다.
- `SNAPSHOT_CHAT_STREAM_READ_TIMEOUT_SECONDS`는 장시간 SSE read timeout입니다.
- `SNAPSHOT_CHAT_DELTA_BATCH_BYTES`는 `full_debug` delta batching 상한입니다.

Module 전용 request fields `conversation_id`, `client_instance_id`, `origin`, `requester_id`는 VSS로 전달하지 않습니다. 인증되지 않은 Chat caller의 `requester_id`는 신뢰하지 않고 폐기하며, stable `client_instance_id`가 있으면 requester type을 `client_instance`로 기록합니다.

Gateway response에는 다음 correlation header를 제공합니다.

- `X-Chat-Conversation-ID`
- `X-Chat-Response-ID`
- `X-Chat-Trace-ID`
- `X-VSS-Request-ID`

## 7. VSS에서 현재 정확히 관측 가능한 값

현재 VSS SSE/metadata로 다음을 exact correlation할 수 있습니다.

- request_id
- project_id/index_id/resolved_by
- chat model
- embedding model/dimension은 serving profile에서 확인
- evidence 여부, top score, threshold
- selected sources/references metadata
- search profile, BM25/symbol/rerank 사용 여부
- embed/search/BM25/symbol/prompt/pre-LLM timing
- TTFT, generation time, total time
- eval_count, decode tok/s
- final answer/references (capture policy에 따라 저장)
- error outcome

현재 외부 계약에 없는 exact raw final prompt, 일부 Ollama raw stats, 독립 rerank latency는 Module이 추측해서 기록하지 않습니다.

## 8. Admin Web UX

기본 화면은 일반 LLM Chat UI입니다.

```text
┌ Conversations ┬ Chat transcript ┬ Trace / Debug inspector ┐
│ session list  │ user/assistant  │ selected response       │
│ search/filter │ messages        │ model/retrieval/timing  │
└───────────────┴─────────────────┴─────────────────────────┘
```

왼쪽 Conversation을 선택하면 중앙 transcript가 바뀝니다. Assistant message 또는 `Trace`를 선택하면 오른쪽 inspector가 해당 Response만 표시합니다.

Inspector에는 최소 다음을 제공합니다.

- requester/origin/client instance
- project/index/model
- evidence/references
- embedding/search/BM25/rerank 상태
- TTFT/generation/total/decode rate
- ordered trace timeline
- model runtime observations
- sanitized errors

Admin read API는 Administrator 전용입니다.

- `GET /v1/admin/chat/conversations`
- `GET /v1/admin/chat/conversations/{conversation_id}`
- `GET /v1/admin/chat/responses/{response_id}/trace`

Conversation 목록은 `project_id`, `requester_id`, `status`, `chat_model`, `embedding_model` 필터를 지원하므로 모델 → conversation/requester 역조회가 가능합니다. Admin Web session sidebar도 requester/project/최근 sLLM/embedding 정보를 검색하며 Chat view가 열린 동안 3초 주기로 최근 상태를 갱신합니다. 선택한 trace가 완료된 경우에는 사용자의 검사 화면을 강제로 교체하지 않습니다.

## 9. Step plan

1. **Foundation — local complete**: 문서 정본, Alembic 0011, ORM, store/schema, Admin read API와 contract tests
2. **Gateway — local complete**: VSS `/v1/chat` transparent streaming relay, exact trace correlation, fail-open persistence
3. **SSE recorder — local complete**: meta/stage/delta/done/error reducer, capture modes, bounded delta batching, source/reference metadata allowlist
4. **Runtime observation — local complete**: 기존 `OllamaRuntimeClient`를 재사용한 retrieval/first-token/generation-complete sampling
5. **Admin Chat UI — local complete**: conversation sidebar, 일반 LLM transcript, assistant response trace inspector, model/requester 검색, bounded polling
6. **AWS shadow — next**: 운영 11500 경로를 바꾸기 전 별도 port에서 실제 VSS/Ollama SSE E2E
7. **Ingress switch — pending**: shadow 검증 완료 후에만 기존 AI 진입점을 Gateway로 전환

로컬 단계는 AWS mutation 없이 full Module gate로 닫고, AWS shadow와 ingress switch는 별도 검증 단계로 수행합니다.

## 10. MR 문제사항 / 남은 검증

이번 변경을 MR/리뷰에서 반드시 같이 기록할 문제사항과 제한은 다음과 같습니다.

- **운영 ingress 미전환**: Gateway는 기본 `SNAPSHOT_CHAT_OBSERVABILITY_ENABLED=false`이며 기존 11500 Chat 경로는 아직 변경하지 않았습니다. 따라서 로컬 구현 완료가 운영 관측 완료를 의미하지 않습니다.
- **AWS shadow E2E 미완료**: 실제 AWS의 VSS + Ollama 조합에서 SSE byte relay, first-token/total timing, runtime observation, Admin transcript/trace 적재를 별도 shadow 포트로 검증해야 합니다.
- **capture retention 정책 필요**: 기본 `metadata`는 본문을 저장하지 않지만 `question_answer`와 특히 `full_debug`는 사용자/assistant 본문 또는 delta batch를 보존할 수 있습니다. 운영 활성화 전 retention/삭제 정책과 `full_debug` 사용 주체를 확정해야 합니다.
- **runtime observation은 snapshot**: Ollama `/api/ps` 샘플은 해당 시점의 resident 상태 관측이며 단독으로 요청-프로세스 실행을 증명하지 않습니다. 요청별 model attribution의 정본은 VSS SSE metadata와 `trace_id == VSS request_id` correlation입니다.
- **Admin monitor는 polling**: Chat view의 3초 갱신은 운영 편의를 위한 bounded polling이며 server push가 아닙니다. 매우 짧은 요청의 중간 상태는 화면에서 건너뛸 수 있지만 최종 persisted trace는 유지됩니다.
- **기존 테스트 환경 경고**: 전체 pytest에는 기존 Admin AsyncMock warning 2건과 Windows에서 POSIX permission이 필요한 skip 1건이 남아 있습니다. 이번 Chat observability 기능에서 새로 발생한 실패는 아닙니다.

다음 단계의 GO 조건은 별도 AWS shadow에서 실제 요청을 흘린 뒤 VSS 응답 body/status가 direct 호출과 동일하고, trace/request/model/timing이 정확히 상관관계되며, 기존 11500 경로에 영향이 없음을 확인하는 것입니다.
