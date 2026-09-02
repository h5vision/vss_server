# VSS Revision Context Provider

**합의일**: 2026-09-02 KST
**상태**: Phase 7 구현 기준, 현재 계약 제안 단계

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
GET /v1/internal/vss/source?project_id=<id>&revision=<optional-sha>
GET /v1/internal/vss/revisions?project_id=<id>&limit=<n>
X-Snapshot-Token: <SNAPSHOT_VSS_API_TOKEN>
```

Phase 7 제안 API이며 현재 구현된 계약으로 간주하지 않습니다.

```http
GET /v1/internal/vss/refs?project_id=<id>
GET /v1/internal/vss/change-requests?project_id=<id>&state=<optional-state>
GET /v1/internal/vss/change-requests/{provider}/{number}?project_id=<id>
GET /v1/internal/vss/context?project_id=<id>&revision=<sha>
GET /v1/internal/vss/context?project_id=<id>&branch_ref=<exact-ref>
GET /v1/internal/vss/context?project_id=<id>&change_request=<provider:number>
```

`context`는 자연어 질의를 처리하는 LLM endpoint가 아닙니다. exact selector를 Git 관계,
Snapshot과 VSS 상태에 연결하는 결정론적 조회입니다. query text 기반 선택이 필요하면 VSS가
catalog를 읽어 판단하고, module에는 실제 선택한 selector만 요청합니다.

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

### Phase 7A - Change Request 계약과 영속화

- provider-neutral PR/MR schema와 append-only revision 관측 이력
- Repository/provider credential 소유권과 권한 경계
- base/head/merge SHA 검증 및 Snapshot 연결
- manual/periodic collector 경로와 멱등성

### Phase 7B - VSS Revision Context Pull API

- ref, change request와 deterministic context 내부 API
- 기존 source/revisions API와 같은 loopback token 경계
- pagination, unavailable reason, credential/path/content redaction
- 완료된 exact index와 미완료 후보의 명시적 구분

### Phase 7C - VSS 소비와 Answer Provenance E2E

- VSS가 localhost API를 pull하여 explicit commit/Branch/PR/MR 질의를 처리
- base/head 비교와 merge commit 질의 검증
- 답변 commit과 VSS `index.commit` 일치 확인
- Frontend까지 provenance가 손실되지 않는지 검증

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
