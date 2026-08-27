# 현재 구현 및 다음 단계 브리핑

최종 확인일: 2026-08-28 KST

## 한눈에 보는 현재 위치

```text
완료       Phase 0R 참조 기준선, Phase 1 FastAPI 골격, Phase 2H VSS HTTP 계약
로컬 완료  Phase 3A-1 Snapshot PostgreSQL 영속화 기반
로컬 완료  Phase 3B-1 DB/VSS readiness와 Frontend 조회 proxy
로컬 완료  Phase 4 핵심 overlay→materialization→VSS 제출
로컬 완료  Phase 5 상태 동기화·재시작 복구·내부 재시도
다음 구현  Phase 6 로컬 장애·배포 사전 검증
외부 대기  Phase 3A-2 Admin 인증/RBAC/UI, Phase 3B-2 실제 배포·shared path
```

`로컬 완료`는 SQLite, local Git Repository와 fake VSS HTTP 경계로 검증했다는 뜻입니다.
실제 PostgreSQL, remote Git, 공유 filesystem과 배포 VSS를 사용한 Production E2E 완료를
뜻하지 않습니다.

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

아직 노출하지 않는 주요 API는 `/v1/admin/*`입니다. 내부 재시도 서비스도 IdP/RBAC와
독립 Admin Web 배포 경계가 확정되기 전에는 public route로 연결하지 않습니다.

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
Frontend frontend SHA  8008a06c732f9ca4e895c4fd75d58c4ab9cf6e37
VSS main SHA            97546fbcea6607a29ad0cc10246a7886bb44ceab
module Phase 4 SHA      0159cc64c1539132d926b2ba27ae536499f02040

Contract    40 passed
Unit        51 passed
Integration 18 passed
전체        109 passed
Ruff        passed
compileall  passed
Ubuntu 24.04 non-root container passed
Alembic PostgreSQL upgrade/downgrade offline DDL passed
```

Integration test는 실제 local Git commit 두 개를 만들고 base overlay 적용 결과가 target
commit과 정확히 같을 때만 fake VSS가 한 번 호출되는지 확인합니다. Revision mismatch,
binding 없음, `already_running`, `not_a_directory`와 내부 경로 redaction도 검증합니다.

## 아직 완료로 표시하지 않는 부분

1. 실제 PostgreSQL `snapshot` schema migration과 동시 transaction 검증
2. 운영 Git provider credential, remote clone latency와 Frontend 10초 timeout
3. Backend와 VSS가 같은 `project_root`를 읽는 shared mount
4. 배포된 VSS artifact가 기준 main SHA와 같은지 확인
5. 다중 worker/instance startup recovery의 PostgreSQL claim/lease 검증
6. 내부 재시도의 인증된 Admin route 연결
7. 인증된 Snapshot 이력/Admin CRUD/UI
8. retention, orphan staging/revision 정리와 용량 제한

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
- 다중 worker/instance DB claim/lease는 Phase 6 운영 확장 전에 추가 검증

### 3. 재시도

- 새 Snapshot을 만들지 않고 기존 `snapshot_id`에 attempt만 추가
- materialized target과 Git HEAD를 재검증한 뒤 제출
- VSS active commit과 실행 중 Job을 먼저 확인
- 자동 force와 무제한 retry 금지
- Admin 수동 retry route는 IdP/RBAC 확정 뒤 노출

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

## 이후 순서

Phase 6에서 Ubuntu 24.04 기준 장애·배포 사전 검증을 계속하고, VSS 운영 측이 AWS 배포를
결정한 뒤 실제 PostgreSQL·VSS·shared path로 Phase 3B-2/6 E2E를 수행합니다.
Admin API/UI는 인증·RBAC·CORS와 별도 서버 위치가 확정된 뒤 연결합니다. Frontend의
`127.0.0.1:11500` AI 호출은 Windows portproxy를 통한 기존 별도 경계이므로 Snapshot
Phase 완료 조건에 포함하지 않고 변경하지 않습니다.
