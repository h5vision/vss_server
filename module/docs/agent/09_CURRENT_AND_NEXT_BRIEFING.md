# 현재 구현 및 다음 단계 브리핑

최종 확인일: 2026-09-01 KST

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
외부 대기  Phase 6B AWS E2E — 실제 systemd·PostgreSQL·VSS 값 필요
후속 검토  Phase 3A-4 GitHub Webhook, Phase 3B-2 실제 배포·shared path
```

`로컬 완료`는 SQLite, local Git Repository와 fake VSS HTTP 경계를, PostgreSQL 선행 검증은
격리된 실제 PostgreSQL 17을 사용했다는 뜻입니다. 운영 role/DSN, remote Git, 공유
filesystem과 배포 VSS를 사용한 Production E2E 완료를 뜻하지 않습니다.

## 현재 노출된 Backend API

| Method | Path | 현재 역할 |
|---|---|---|
| `GET` | `/v1/health` | 프로세스 liveness |
| `GET` | `/v1/health/ready` | DB ping과 VSS `/health`·`/projects` readiness |
| `GET` | `/v1/projects` | VSS project catalog를 Frontend 형식으로 변환·redaction |
| `GET` | `/v1/models` | VSS model 목록을 Frontend model 형식으로 변환 |
| `GET` | `/v1/briefing` | workspace exact binding 후 VSS briefing 조회 |
| `POST` | `/v1/workspace-overlays` | Snapshot 저장, 전체 tree 생성, VSS 인덱싱 접수 |
| `GET` | `/v1/index/status` | 최신 Snapshot과 VSS 상태를 exact revision 기준으로 동기화 |
| `GET` | `/v1/internal/vss/source` | VSS에 latest/exact SHA, tree SHA, project_root와 `/index` 값 제공 |
| `GET` | `/v1/internal/vss/revisions` | exact VSS project의 Snapshot SHA 이력 제공 |

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
Windows 전체 167 passed + POSIX 1 skipped
PostgreSQL 17 실제 migration/unique/retry·recovery·collection lock 5 passed
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

AWS 다중 instance 잠금 장애 실증, 운영 role/DSN, shared path와 배포 VSS는 로컬
PostgreSQL 검증 범위가 아니므로 Phase 6B 외부 대기를 유지합니다.

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
인증/RBAC와 감사 actor까지 로컬 완료했습니다. 다음 구현 후보인 Phase 3A-4 Webhook은 공개 HTTPS,
HMAC-SHA256, delivery 멱등 저장과 비동기 queue가 준비된 경우에만 적용합니다.

VSS 운영 측이 AWS 배포를 결정한 뒤 운영 PostgreSQL·VSS·shared path로 Phase 3B-2/6B
E2E를 수행합니다.
현재 AWS host에서는 기존 Python 3.10.12 `.venv`에 최신 module dependency를 다시 설치하고
`ops/ubuntu22.04/vss-snapshot.service.example`을 검토·반영한 뒤 systemd preflight와
liveness/readiness를 확인해야 합니다.
Phase 3A-3의 외부 mutation은 운영 `4180` 접근/TLS/VPN 경계와 secret 배포를 확인하기
전에는 공개하지 않습니다.
Frontend의
`127.0.0.1:11500` AI 호출은 Windows portproxy를 통한 기존 별도 경계이므로 Snapshot
Phase 완료 조건에 포함하지 않고 변경하지 않습니다.
