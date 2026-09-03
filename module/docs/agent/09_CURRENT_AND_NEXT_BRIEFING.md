# 현재 구현 및 다음 단계 브리핑

최종 확인일: 2026-09-03 KST

> **[운영/개발 철칙]**
> 1. **진행 사항 문서화 최우선 원칙**: 코드 변경 착수 전 항상 문서 지침을 확인하고, 각 스텝/PR 완료 시 반드시 본 문서 및 주제별 진행 문서(`17_ARCHITECTURE_REFACTORING.md`)를 최우선으로 동기화합니다.
> 2. **스텝별 멈춤·브리핑·허가 대기 원칙**: 임의 진행을 엄격히 금지하며, 스텝 완료 시 작업을 멈추고 코드 레벨 상세 브리핑 후 사용자의 명시적 허가를 대기합니다.

## 한눈에 보는 현재 위치

```text
완료       Phase 0R 참조 기준선, Phase 1 FastAPI 골격, Phase 2H VSS HTTP 계약
로컬 완료  Phase 2V VSS source descriptor·revision 조회 API
로컬 완료  Phase 3A-1 Snapshot PostgreSQL 영속화 기반
로컬 완료  Phase 3A-2 사용자 선택 Branch catalog/fetch/HEAD SHA 이력·VSS 제출
로컬 완료  Phase 3A-3 포트 4180 Admin API·인증/RBAC·UI
로컬 완료  Phase 3B-1 DB/VSS readiness와 Frontend 조회 proxy
로컬 완료  Phase 4 핵심 overlay→materialization→VSS 제출
로컬 완료  Phase 5 상태 동기화·재시작 복구·내부 재시도
로컬 완료  Phase 6A-1 Ubuntu 24.04 로컬 장애·배포 사전 검증
로컬 완료  Phase 6A-2 실제 AWS Ubuntu 22.04.5 + Python 3.10 호환 검증
로컬 선행  Phase 6B PostgreSQL 17 migration·제약·재시도/복구 잠금 검증
부분 통과  Phase 3B-2/6B AWS PostgreSQL→remote Git→shared path→실제 VSS exact commit
후속 검증  Phase 6B 실패·보안·역할 분리·retention과 전체 Production GO 항목
다음 설계  Phase 7 PR/MR reference catalog·VSS revision context pull·답변 provenance
로컬 완료  Phase 7A-1 PR/MR schema·Alembic 0006·append-only observation store
로컬 완료  Phase 7B-1 VSS PR/MR 목록·상세 pull·revision availability
로컬 완료  Phase 7A-2 commit catalog·parent graph·bounded scanner·자동 backfill
로컬 완료  Phase 7A-3 GitHub/GitLab provider·provider-owned ref·Tag 이력
검증 완료  Module sandbox full harness·mock/local 종단 검증
로컬 완료  Phase 7B-1 VSS pull orchestration (vss_pull 모드) & capabilities/refs/context 내부 API
로컬 완료  Phase 7B-2 Admin commit history·compare (Git diff 엔진, REST API, Admin Web UI 완료)
로컬 완료  Phase 7B-3 On-demand Snapshot 승격 (엔드포인트·멱등성·BFF 프록시·UI 완료, 커밋 661520c)
로컬 완료  Architecture Refactoring PR 1 (Admin Router 7개 하위 모듈 물리 분할, 47 tests passed)
로컬 완료  Architecture Refactoring PR 2 (Compare/Materialize UseCase 도입 및 _git_client 제거, 49 tests passed)
로컬 완료  Architecture Refactoring PR 3 (Bootstrap Composition Root 분리 backend/bootstrap/container.py, 224 tests passed)
로컬 완료  Architecture Refactoring PR 4 (Git Ports 인터페이스 정의 및 Legacy Adapter 연결, 53 tests passed)
로컬 완료  Architecture Refactoring PR 5 (하위 공통 GitCommandRunner 추출 및 보안 정책 중앙화, 58 tests passed)
다음 구현  Architecture Refactoring PR 6 (RepositoryGitClient 기능별 모듈 물리 분리)
후속 진행  Phase 7C VSS Context와 Provenance (deterministic revision context pull & provenance)
조건부 후속 Phase 3A-4 GitHub/GitLab Webhook
```

`로컬 완료`는 SQLite, local Git Repository와 fake VSS HTTP 경계를, PostgreSQL 선행 검증은
격리된 실제 PostgreSQL 17을 사용했다는 뜻입니다. 운영 role/DSN, remote Git, 공유
filesystem과 배포 VSS를 사용한 Production E2E 완료를 뜻하지 않습니다.

## 2026-09-02 AWS happy-path 증거

실제 AWS Ubuntu 22.04.5 host에서 Alembic `0005_reconcile_collection`을 적용하고 Backend와
Admin Web의 systemd active/readiness를 확인했습니다. 등록한 `test-merge` remote Branch는
target `e32f862a4a819f806363a23e176bbbc94bde52f1`로 materialize됐고, 실제 VSS project가
동일 `index.commit`으로 `done`을 반환했습니다. Backend 재시작 뒤 startup recovery가 두
`test-merge` Snapshot을 `completed / done / VSS_INDEX_COMPLETED`로 수렴시켰습니다.

이 증거는 loopback 연결, remote Git, 실제 PostgreSQL migration, shared path, 실제 VSS
인덱싱과 exact revision 복구의 happy path를 통과했다는 의미입니다. migration/runtime DB
role 분리, 실패 시 이전 active index 보존, TLS/VPN, Frontend 실제 Chat/overlay E2E,
retention과 장애 시나리오는 아직 Production GO로 표시하지 않습니다.

## 합의된 장기 목적 — Revision Context Provider

Snapshot은 VSS 인덱싱 이력에 그치지 않고, VSS가 사용자 질의에 사용할 코드 시점을 판단할
수 있는 참고 자료가 되어야 합니다. module은 Repository/Branch/Tag, GitHub PR/GitLab MR의
base/head/merge commit 관계와 exact Snapshot·index 증거를 보존합니다.

VSS가 `/v1/chat`과 자연어 질의 해석을 소유하며 module을 localhost로 pull합니다. module은
Chat을 proxy하거나 답변을 생성하지 않습니다. 모든 commit은 저비용 catalog로 보존하고,
선택 commit만 Snapshot, AI에 필요한 Snapshot만 VSS index로 승격합니다. VSS pull 정본은
`15_REVISION_CONTEXT_PROVIDER.md`, commit history·비교 정본은
`16_COMMIT_HISTORY_AND_COMPARISON.md`입니다.

Phase 7A-1에서는 provider-neutral `change_requests` current state와
`change_request_revisions` append-only 이력, Alembic `0006`과 멱등 store를 구현했습니다.
Phase 7A-2에서는 Repository commit catalog와 parent graph, Alembic `0007`, bounded
`git rev-list --stdin` scanner, run lease와 sync 후 자동 backfill을 구현했습니다. GitHub/GitLab
read-only provider adapter, Tag/ref 연결과 remote Git object 검증은 Phase 7A-3에서
구현했습니다. provider/Tag 수집은 기본 비활성이며 운영자가 환경변수로 opt-in합니다.
VSS 내부 API는 token 누락 시 token 값 대신 `SNAPSHOT_VSS_API_TOKEN`과 승인된 config 경로를
알려주도록 보강했습니다.

Phase 7B-1에서는 VSS가 `project_id`로 PR/MR 목록과 provider/number 상세를 pull하고,
base/head/merge SHA별 Snapshot/VSS 상태와 `eligible_for_answer`를 확인할 수 있습니다.
또한 `SNAPSHOT_ORCHESTRATION_MODE=vss_pull`을 도입하여 Module의 VSS `POST /index` push 호출을
0회로 차단하고, VSS가 당겨갈 수 있는 `capabilities`, `refs`, `context` 내부 API를
로컬 완료했습니다. Admin commit history·compare는 Phase 7B-2에서 이어갑니다.

## 현재 노출된 Backend API

| Method | Path | 현재 역할 |
|---|---|---|
| `GET` | `/v1/health` | 프로세스 liveness |
| `GET` | `/v1/health/ready` | DB ping과 VSS `/health`·`/projects` readiness |
| `GET` | `/v1/projects` | VSS project catalog를 Frontend 형식으로 변환·redaction |
| `GET` | `/v1/models` | VSS model 목록을 Frontend model 형식으로 변환 |
| `GET` | `/v1/briefing` | workspace exact binding 후 VSS briefing 조회 |
| `POST` | `/v1/workspace-overlays` | Snapshot 저장, 전체 tree 생성 (push 모드 시 VSS 인덱싱 접수, pull 모드 시 VSS 호출 0회) |
| `GET` | `/v1/index/status` | 최신 Snapshot과 VSS 상태를 exact revision 기준으로 동기화 |
| `GET` | `/v1/internal/vss/capabilities` | VSS에 현재 지원 모드(vss_pull) 및 기능 안내 |
| `GET` | `/v1/internal/vss/source` | VSS에 latest/exact SHA, tree SHA, project_root와 `/index` 값 제공 |
| `GET` | `/v1/internal/vss/revisions` | exact VSS project의 Snapshot SHA 이력 제공 |
| `GET` | `/v1/internal/vss/change-requests` | Repository의 PR/MR current revision과 availability |
| `GET` | `/v1/internal/vss/change-requests/{provider}/{number}` | PR/MR 관측 이력 상세 |
| `GET` | `/v1/internal/vss/refs` | 프로젝트 추적 브랜치/태그/PR/MR 최신 refs 일괄 제공 |
| `GET` | `/v1/internal/vss/context` | revision, branch, PR/MR에 대한 결정론적 Snapshot/VSS 상태 조회 |

`/v1/admin/*`는 Repository·추적 Branch·HEAD 이력·Binding·sync run·Snapshot·retry·
VSS project·감사 로그를 제공합니다. 이 route는 브라우저에 직접 공개하는 신뢰 경계가
아니며 독립 Admin Web BFF의 서비스 토큰과 request HMAC, actor/role을 검증합니다.
`/v1/internal/vss/*`는 `SNAPSHOT_VSS_API_TOKEN`이 필요한 loopback 전용 경계이며 외부
ingress에 공개하지 않습니다. scheduler는 아직 후속 범위입니다.

## 현재 구현된 Snapshot 처리 흐름

```text
Frontend payload 검증
→ frontend_project_id exact active binding 조회
→ 동일 (vss_project_id, target_revision) 중복 확인
→ Snapshot과 delta 최초 DB commit
→ binding branch read-only Git clone
→ base commit checkout
→ staging에 added/modified/deleted/rename 적용
→ .git 변경, traversal, symlink/junction 차단
→ 적용 tree hash == target commit tree 검증
→ Git HEAD == target revision, clean working tree 재검증
→ immutable revision 디렉터리 승격과 안전한 locator 저장
→ VSS attempt 선저장
→ VSS POST /index
→ accepted/rejected/failed reason과 안전한 결과 저장
→ 구조화된 HTTP 응답
```

### 성공·거부 의미

| HTTP | reason | 의미 |
|---:|---|---|
| `202` | `VSS_INDEX_ACCEPTED` | VSS가 작업을 접수했으며 완료는 아직 아님 |
| `200` | `TARGET_ALREADY_INDEXED` | DB에서 동일 target의 완료 이력이 확인됨 |
| `409` | `SNAPSHOT_DESTINATION_REQUIRED` | 활성 binding이 없음 |
| `409` | `SNAPSHOT_DESTINATION_AMBIGUOUS` | exact binding이 둘 이상임 |
| `409` | `SNAPSHOT_ALREADY_EXISTS` | 동일 target Snapshot이 있어 중복 제출하지 않음 |
| `409` | `SNAPSHOT_REVISION_MISMATCH` | 적용된 전체 tree가 target commit tree와 다름 |
| `409` | `VSS_REVISION_CONTRACT_UNSUPPORTED` | target Git object가 없어 revision을 보존할 수 없음 |
| `409` | `VSS_INDEX_ALREADY_RUNNING` | 같은 VSS project 작업이 진행 중임 |
| `500` | `SNAPSHOT_MATERIALIZATION_FAILED` | 파일 tree 생성 또는 VSS path 판정 실패 |
| `502/503` | VSS 구조화 reason | 인증·계약·연결·timeout 실패 |

모든 응답은 `reason`, 사람이 이해할 수 있는 `detail`, `retryable`, `X-Request-ID`를
사용합니다. Git stderr, 파일 content, token과 server-local 절대경로는 응답에 포함하지
않습니다.

## 현재 검증 증거

```text
Frontend frontend SHA  ca2a2c6140fc128f2ae892c13228fa9a433e5d8e
VSS pre-rag SHA         d34bf1ce05bb3fd95cb89cecb35bf7df96e7b202
VSS test-merge SHA      47b85faf01edc33184149b7364835bb4312d76b9
Windows 전체 199 passed + POSIX 1 skipped
PostgreSQL 17 실제 migration/unique/retry·recovery·collection lock 5 passed
Module sandbox full harness (verify_module_sandbox.sh) passed
Ruff        passed
compileall  passed
Ubuntu 24.04 non-root container passed
Alembic PostgreSQL upgrade/downgrade offline DDL passed
```

## 실제 AWS runtime 확인 — 2026-08-28

```text
Host                hancom-team2-5th
OS                  Ubuntu 22.04.5 LTS
System Python       3.10.12
Module venv Python  3.10.12
Git                 2.34.1
Module path         /home/ubuntu/vss_server/module
기존 systemd 결과   0003 migration 뒤 active (running), readiness 200
Phase 3A-2 배포      0004 migration·새 코드 미적용
```

환경 파일 누락 문제는 해소됐습니다. Python 지원 범위를 3.10 이상으로 조정하고 3.10에서
없는 `StrEnum`, `typing.Self`, `datetime.UTC`, `Path.is_junction`, `shutil.rmtree(onexc)`를
호환 구현으로 교체했습니다. Ubuntu 22.04/Python 3.10.12 전체 회귀는 통과했지만 실제
service unit 반영과 health smoke 전에는 AWS E2E를 완료로 표시하지 않습니다.

Integration test는 실제 local Git commit 두 개를 만들고 base overlay 적용 결과가 target
commit과 정확히 같을 때만 fake VSS가 한 번 호출되는지 확인합니다. Revision mismatch,
binding 없음, `already_running`, `not_a_directory`와 내부 경로 redaction도 검증합니다.

## 아직 완료로 표시하지 않는 부분

1. 운영 PostgreSQL migration/runtime role 분리와 실제 DSN readiness
2. 운영 Git provider credential, remote clone latency와 Frontend 10초 timeout
3. Backend와 VSS가 같은 `project_root`를 읽는 shared mount
4. 배포된 VSS artifact가 기준 main SHA와 같은지 확인
5. PostgreSQL recovery advisory lock의 AWS 다중 instance·연결 장애 실증
6. Admin Web의 운영 TLS/VPN·보안 그룹·secret/user registry 적용
7. retention, orphan staging/revision 정리와 용량 제한

현재 Git source는 binding branch에서 base와 target commit object를 모두 찾을 수 있어야
합니다. push되지 않은 local-only commit, executable bit 또는 submodule처럼 현 Frontend
payload만으로 정확히 재현할 수 없는 변경을 임의 값으로 대체하지 않습니다.

## 로컬 완료 브리핑 — Phase 5

### 1. 상태 조회와 동기화

- `GET /v1/index/status?project_id=<workspace-or-project-id>` 구현
- project/workspace exact binding으로 현재 Snapshot과 VSS project 확정
- VSS `GET /index/status` 결과를 Snapshot 상태와 대조
- `running|indexing_lexical|promoting`은 Backend `indexing`으로 저장
- `done`이면서 `index.commit == target_revision`일 때만 `completed`
- `done`인데 commit이 없거나 다르면 `VSS_REVISION_MISMATCH` 실패
- `failed|aborted`의 안전한 reason/detail 보존
- 조회 성공과 작업 성공을 구분하는 `reason/detail/retryable` 응답

### 2. 재시작 복구

- 시작 시 `accepted|indexing|submitting` Snapshot 후보 조회
- VSS status를 다시 읽어 DB 상태를 멱등하게 수렴
- VSS 상태를 알 수 없을 때 자동 `force=true` 재제출 금지
- 초기 1 worker 기준 one-shot 동기화 완료
- PostgreSQL advisory lock 구현 전 기준이며 AWS 다중 instance 실증은 Phase 6B에서 수행

### 3. 재시도

- 새 Snapshot을 만들지 않고 기존 `snapshot_id`에 attempt만 추가
- materialized target과 Git HEAD를 재검증한 뒤 제출
- VSS active commit과 실행 중 Job을 먼저 확인
- 자동 force와 무제한 retry 금지
- Admin 수동 retry route는 operator 이상 역할과 서명된 BFF 경계로 노출

### Phase 5 핵심 로컬 완료 판정

```text
accepted를 completed로 오판하지 않음
running 계열 상태를 indexing으로 동기화
done + exact target만 completed
done + null/다른 commit은 revision mismatch
failed/aborted 원인과 retryable 보존
프로세스 재시작 뒤 상태 수렴
재시도는 동일 Snapshot에 새 attempt만 생성
Frontend /v1/index/status 응답이 실제 handler 계약과 일치
```

## 로컬 완료 브리핑 — Phase 6A-1

- 핵심 상태·복구·재시도·경로 보안 판단을 한글 유지보수 주석으로 기록
- VSS 진행/실패/중단/연결 실패와 recovery unavailable 회귀 테스트
- 실행 중 VSS Job과 변조된 immutable tree의 재시도 차단 테스트
- disk full 계열 write failure와 Ubuntu POSIX permission denied 테스트
- `preflight_ubuntu_runtime.sh`로 service Python·설정·경로·VSS health 확인
- `smoke_backend_readiness.py`로 배포 Backend의 읽기 전용 health/status 확인
- VSS 담당자·LLM은 `11_VSS_VALIDATOR_HANDOFF.md`를 단일 진입점으로 사용

## 로컬 선행 브리핑 — Phase 6B PostgreSQL

- Alembic schema 생성도 migration transaction 안에서 commit하도록 실제 rollback 결함 수정
- 격리 PostgreSQL 17에서 upgrade/downgrade/re-upgrade와 version/table 생성 확인
- 동시 동일 target insert는 DB unique constraint로 한 건만 확정
- 동일 Snapshot 수동 재시도는 `SELECT ... FOR UPDATE`로 직렬화
- startup recovery는 PostgreSQL DB 단위 advisory lock으로 조정자 하나만 실행
- 잠금용 connection과 VSS 조회 transaction을 분리하고 두 connection의 상호 배제를 실증
- 전용 실행기는 고유 임시 컨테이너만 생성·정리하고 DSN을 출력하지 않음

AWS happy path에서 shared path와 배포 VSS exact commit은 확인했습니다. 다중 instance 잠금
장애 실증, migration/runtime role 분리, 실패 시 이전 active index 보존과 운영 보안은 아직
남아 있으므로 Phase 6B를 부분 통과로 유지합니다.

## Phase 3A-2 완료 브리핑

Repository 등록값과 기본 Branch를 remote catalog로 검증하고 사용자가 선택한 exact
Branch만 `tracked_branches`에 저장합니다. bare cache는 선택 ref만 fetch하며 관측 SHA별
보존 ref를 만들어 force-push와 삭제 뒤에도 object를 유지합니다. HEAD 변화는
`created|fast_forward|rewind|deleted|recreated`로 append-only 저장하고 동일 SHA는 새
Snapshot/VSS Job을 만들지 않습니다. 수동·정기 trigger는 같은 lease service를 사용하며
stale 실행은 실패로 보존합니다. 새 SHA는 collector-owned Snapshot과 immutable full tree,
VSS `/index`로 연결됩니다.

## 이후 순서

Phase 3A-3은 독립 Admin service의 포트 `4180`, 정적 UI, Backend loopback BFF,
인증/RBAC와 감사 actor까지 로컬 완료했습니다. AWS happy path도 실제 PostgreSQL, remote
Git, shared path와 VSS exact commit까지 확인했습니다. Phase 6B의 남은 실패·보안·운영
검증을 닫는 작업과 병행해 Phase 7B-2 Admin history·compare와 VSS refs pull부터 진행합니다.
이후 Phase 7B-3 on-demand Snapshot, Phase 7C VSS context 순서입니다.

Phase 3A-4 Webhook은 Phase 7D의 빠른 알림 수단으로 재배치합니다. 공개 HTTPS,
HMAC/token, delivery 멱등 저장과 비동기 queue가 준비된 경우에만 적용하며 periodic provider
fetch와 Git object 검증을 대체하지 않습니다.

Phase 3A-3의 외부 mutation은 운영 `4180` 접근/TLS/VPN 경계와 secret 배포를 확인하기
전에는 공개하지 않습니다.
Frontend의 `127.0.0.1:11500` AI 호출은 유지합니다. VSS가 `/v1/chat`을 소유하고 module의
내부 revision context API를 localhost로 pull하므로 Frontend가 Snapshot 내부 API를 직접
호출하지 않습니다.
