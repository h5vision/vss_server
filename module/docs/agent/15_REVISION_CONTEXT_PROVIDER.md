# VSS Revision Context Provider

## 2026-09-04 확정 운영 계약

이 절은 이전 문서의 충돌하는 자동 인덱싱·`vss_pull` 우선 표현보다 우선합니다.

- **VSS가 유일한 Indexer입니다.** Snapshot Module은 파일 수집 정책, chunking, embedding, BM25, vector/vector-store build·promote를 구현하거나 복제하지 않습니다. 실제 인덱싱은 `vss_server`의 `POST /index -> indexer.start_index()` 경로만 사용합니다.
- Repository 등록/동기화는 **인덱싱과 분리**합니다. 수집한 Repository는 `SNAPSHOT_REPOSITORY_ROOT=/home/ubuntu/repos` 아래 관리하고, sync는 clone/fetch·ref 관측·commit catalog 갱신까지만 수행하며 VSS `POST /index`를 자동 호출하지 않습니다.
- VSS에 넘길 입력은 mutable working copy가 아니라 `SNAPSHOT_MATERIALIZATION_ROOT=/home/ubuntu/vss-snapshots` 아래의 **검증된 immutable exact Snapshot**입니다. `VSS_REPOS_DIR=/home/ubuntu/repos`는 VSS의 repository 발견/표시 용도로 사용할 수 있지만 Module의 정식 `/index` 입력 경로는 아닙니다.
- 인덱싱 시작은 **Admin의 명시적 Index 요청**이 소유합니다. 목표 Admin API는 `POST /v1/admin/snapshots/{snapshot_id}/index`이며, materialized Snapshot만 대상으로 `project_root`, `project_id`, `force=false`, `briefing`, `note`를 VSS `POST /index`에 전달합니다. VSS의 `remote` clone 기능은 Module 연동 경로에서 사용하지 않습니다.
- Module은 VSS의 `GET /index/status`와 `GET /index/exists`를 관측하고, `state=done`뿐 아니라 `index.commit == snapshot.target_revision`까지 확인한 경우에만 Snapshot을 `completed`로 수렴시킵니다.
- 현재 운영 오케스트레이션 방향은 **`module_push`**이지만 의미는 “sync 시 자동 push”가 아니라 **Admin 요청으로 생성된 IndexCommand를 Module이 VSS에 제출**한다는 뜻입니다. `vss_pull`과 `/v1/internal/vss/*`는 provenance/read-model 및 향후 선택 기능으로 유지하며 현재 pre-rag VSS의 필수 data plane으로 간주하지 않습니다.
- Commit History/Compare는 Admin 분석 기능으로 유지합니다. **비교 결과로 reference commit SHA를 자동 선택하거나 VSS에 전달하는 기능, multi-revision 답변 context는 구현 보류**입니다.


**합의일**: 2026-09-02 KST
**상태**: Phase 7A provider·ref catalog와 provenance read model 로컬 완료; VSS pull 소비·multi-revision context는 후속/보류

## 목적

Snapshot module은 VSS가 사용자 질의에 적합한 코드 시점을 판단할 때 참고할 수 있는 내부
Revision Context Provider입니다. 사용자가 선택한 Repository와 Branch의 commit 이력뿐
아니라 Tag, GitHub Pull Request와 GitLab Merge Request의 base/head/merge 관계를 보존하고,
각 commit에 대응하는 Snapshot과 VSS 인덱스 증거를 제공합니다.

module이 AI 답변을 생성하거나 Chat 요청을 proxy하지 않습니다. VSS가 `/v1/chat`과 질의
해석을 소유하고, 같은 AWS 인스턴스의 localhost에서 module 내부 API를 pull하여 사용할
revision 또는 비교 범위를 선택합니다.

```text
사용자 -> VSS /v1/chat
              |
              | localhost + X-Snapshot-Token
              v
          Snapshot module
          - Repository/Branch/Tag commit history
          - PR/MR base, head, merge commit 관계
          - Snapshot/materialization/index.commit 증거
              |
              v
          VSS 검색·비교·답변
          - 사용한 commit과 파일 근거를 답변 provenance로 반환
```

## 책임 경계

### Snapshot module

- provider에서 관측한 Repository, Branch, Tag, PR/MR와 commit 관계를 저장합니다.
- 모든 revision은 검증된 40자리 Git commit SHA로 식별합니다.
- force-push, 삭제, 재생성, PR/MR head 변경을 append-only 관측 이력으로 보존합니다.
- exact commit의 Snapshot, Git tree SHA, materialized source와 VSS 상태를 연결합니다.
- VSS가 답변 근거로 사용할 수 있는 revision 후보와 provenance 필드를 내부 API로 제공합니다.
- 인덱싱이 `done`이고 `index.commit`이 exact target과 같은지 구분하여 반환합니다.

### VSS

- `/v1/chat`, 사용자 질의 해석, 검색과 최종 답변 생성을 소유합니다.
- module의 localhost 내부 API를 호출하여 revision 후보와 변경 요청 관계를 조회합니다.
- 명시된 commit/Branch/Tag/PR/MR를 우선하고 모호한 질의는 임의의 최신 commit으로
  확정하지 않습니다.
- 선택한 revision의 VSS active index가 module의 exact commit 증거와 같은지 확인합니다.
- 답변에 실제 사용한 Repository, ref/change request, commit과 파일 근거를 provenance로
  포함합니다.

### Frontend

- Snapshot module 내부 API를 직접 호출하지 않습니다.
- 기존 VSS Chat 경로를 유지하고 VSS가 반환한 provenance를 표시할 수 있습니다.
- PR/MR 또는 commit 선택 UI가 필요해도 module 내부 token을 브라우저에 전달하지 않습니다.

## Revision 선택 참고 규칙

module은 결정론적 Git 관계와 사용 가능 상태를 제공하고, 자연어 의도에 대한 최종 선택은
VSS가 수행합니다.

| 질의 문맥 | VSS가 우선 참고할 revision |
|---|---|
| exact commit SHA | 해당 commit의 exact Snapshot |
| 현재 Branch | 해당 Branch에서 가장 최근에 완료되고 exact `index.commit`이 확인된 Snapshot |
| 특정 Tag | Tag가 가리킨 exact commit |
| PR/MR 변경 내용 | `base_sha`와 현재 `head_sha` 범위 |
| PR/MR 적용 결과 | 실제 `merge_sha`; 미병합이면 head를 merge 결과로 가장하지 않음 |
| PR/MR 적용 전후 비교 | base와 head, 병합 후 질문이면 필요에 따라 merge commit도 함께 사용 |
| 과거 시점 | 해당 시점까지 관측된 ref와 Snapshot 이력 |
| 모호하거나 인덱스 미완료 | 후보와 unavailable reason을 반환하고 최신 commit을 임의 선택하지 않음 |

PR/MR 번호는 Repository와 provider 범위 안에서만 식별합니다. GitHub PR과 GitLab MR의
명칭 차이는 외부 표현으로 유지하되 내부 모델은 provider-neutral change request로 다룰 수
있습니다.

## Change Request 최소 데이터

```text
provider                       github | gitlab
repository_id
external_number
kind                           pull_request | merge_request
state                          open | closed | merged
base_ref / base_sha
head_ref / head_sha
merge_sha                      nullable
observed_at / updated_at / merged_at
snapshot_id_by_revision        base/head/merge 각각 nullable
```

- Webhook payload의 SHA를 정본으로 바로 저장하지 않고 provider fetch와 Git object 검증을
  거칩니다.
- PR/MR head가 변경되면 이전 head 관측을 덮어쓰지 않습니다.
- merge commit이 없는 squash/rebase/미병합 상태를 명시적으로 구분합니다.
- fork PR/MR credential과 접근 권한은 Repository credential 정책을 따릅니다.

## 내부 Pull API 방향

현재 구현된 API:

```http
GET /v1/internal/vss/capabilities
GET /v1/internal/vss/source?project_id=<id>&revision=<optional-sha>
GET /v1/internal/vss/revisions?project_id=<id>&limit=<n>
GET /v1/internal/vss/change-requests?project_id=<id>&state=<optional>&limit=<n>
GET /v1/internal/vss/change-requests/{provider}/{number}?project_id=<id>
GET /v1/internal/vss/refs?project_id=<id>
GET /v1/internal/vss/context?project_id=<id>&revision=<sha>
GET /v1/internal/vss/context?project_id=<id>&branch_ref=<exact-ref>
GET /v1/internal/vss/context?project_id=<id>&change_request=<provider:number>
X-Snapshot-Token: <SNAPSHOT_VSS_API_TOKEN>
```

`context`는 자연어 질의를 처리하는 LLM endpoint가 아닙니다. exact selector를 Git 관계,
Snapshot과 VSS 상태에 연결하는 결정론적 조회입니다. query text 기반 선택이 필요하면 VSS가
catalog를 읽어 판단하고, module에는 실제 선택한 selector만 요청합니다.

기존 source/revisions와 향후 Phase 7 API는 token 누락 시 token 값 대신
`SNAPSHOT_VSS_API_TOKEN` 환경변수명과 승인된 config 경로를 구조화 오류로 안내합니다.
기본 경로는 `/etc/vss-snapshot/module.env`이며
`SNAPSHOT_VSS_API_TOKEN_CONFIG_PATH`로 변경할 수 있습니다. 이 경로는 loopback VSS 운영자를
위한 예외이고 materialized source, DB와 credential 경로는 계속 redaction합니다.

응답은 최소한 다음 증거를 제공해야 합니다.

```text
repository_id / vss_project_id
context_kind                    revision | branch | tag | change_request
branch_ref 또는 tag_ref
change_request provider/number/state
base_revision / head_revision / merge_revision
selected Snapshot ID와 target_revision
expected_tree_sha
snapshot_state / materialized
vss_state / vss_index_commit
eligible_for_answer
unavailable_reason
observed_at
```

## 답변 Provenance

VSS가 최종 답변에서 반환할 provenance 계약은 VSS 팀과 별도로 확정하되 다음 값은 잃지
않습니다.

```text
repository
ref 또는 PR/MR 식별자
사용한 commit SHA 또는 비교한 base/head SHA
Snapshot ID
VSS project ID와 index.commit
사용한 파일 경로와 가능한 위치 정보
revision 선택 이유
```

답변 시점의 active index가 바뀌더라도 어떤 commit을 사용했는지 재현할 수 있어야 합니다.
`done`이지만 commit이 없거나 다른 인덱스는 답변 근거로 적격 처리하지 않습니다.

## Phase 7 진행 순서

### Phase 7A - Commit graph와 Change Request catalog

- provider-neutral PR/MR schema와 append-only revision 관측 이력
- Repository commit metadata와 merge parent graph
- 기존 Branch/Snapshot/PR/MR SHA의 commit catalog 연결
- Repository/provider credential 소유권과 권한 경계
- base/head/merge SHA 검증 및 Snapshot 연결
- manual/periodic collector 경로와 멱등성

Phase 7A-1에서 PR/MR schema와 store, 7A-2에서 commit catalog와 parent graph,
7A-3에서 GitHub/GitLab adapter, provider-owned head ref 검증과 Tag 이력을 구현했습니다.
PR/MR/Tag commit은 graph root로 연결하지만 자동 Snapshot/VSS index는 만들지 않습니다.

### Phase 7B - Admin History와 Revision Context API

- ref, change request와 deterministic context 내부 API
- 기존 source/revisions API와 같은 loopback token 경계
- pagination, unavailable reason, credential/materialized-path/content redaction
- 완료된 exact index와 미완료 후보의 명시적 구분
- Admin commit history·timeline·compare와 on-demand Snapshot 경계

Phase 7B-1에서 PR/MR 목록·상세, append-only observations와 base/head/merge별
`eligible_for_answer` 조회 및 capabilities/refs/context 내부 API를 구현했습니다. 이 내부 API는
provenance/read-model capability로 유지하며 `vss_pull` caller 연동은 향후 선택 기능입니다.
현재 인덱싱 시작은 Admin explicit Index -> Module -> VSS `/index` 경로를 사용합니다.
Admin commit history·compare와 on-demand Snapshot 승격은 각각 독립 기능이며 compare 결과를
VSS reference SHA로 자동 전달하지 않습니다.

### Phase 7C - Provenance Read Model E2E (범위 축소)

- Repository/ref/commit -> exact Snapshot/VSS 상태 projection 검증
- 단일 indexed revision의 `index.commit == target_revision` 증거 검증
- localhost pull API는 optional/future consumer capability로 유지
- **구현 보류**: compare 기반 reference SHA 자동 선택/전달, base/head multi-revision 질의, 답변용 historical context 자동 구성

### Phase 7D - 자동 갱신 선택 트랙

- periodic poller를 정본 동기화 경로로 먼저 제공
- GitHub Webhook/GitLab webhook은 HTTPS, HMAC/token, delivery 멱등과 queue가 준비된 경우에만
  빠른 알림 경로로 추가
- Webhook이 provider fetch와 Git 검증을 대체하지 않음

## 완료 조건

```text
exact commit 질의 -> 동일 Snapshot과 index.commit 근거
Branch 현재 질의 -> 최신 관측 SHA가 아니라 최신 answer-eligible Snapshot을 구분
PR/MR 변경 질의 -> exact base/head 범위
병합 결과 질의 -> 실제 merge SHA, 미병합 head를 merge로 가장하지 않음
force-push 뒤에도 이전 PR/MR head 이력 조회 가능
미인덱싱/실패 revision은 unavailable reason과 함께 제외 또는 후보로 표시
VSS가 localhost pull만 사용하고 module이 Chat을 proxy하지 않음
답변 provenance로 사용 commit과 파일 근거를 재현 가능
내부 API가 외부 ingress에 노출되지 않고 credential/content/path를 누출하지 않음
```

## 비목표

- module에서 자연어 질의나 답변 생성
- module에 VSS 검색, 청킹, 임베딩 또는 reranking 복제
- PR/MR head를 merge commit으로 추정
- 최신 Branch 또는 active index를 사용자 의도와 무관하게 자동 선택
- Frontend 브라우저에 내부 VSS token 또는 server-local path 노출
- Webhook 단독 이벤트를 Git 상태의 정본으로 사용

Repository 전체 commit history, Snapshot 승격 비용 경계와 Admin compare의 상세 정본은
`16_COMMIT_HISTORY_AND_COMPARISON.md`를 따릅니다.
