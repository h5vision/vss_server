# VSS 검증자 인계 지침

최종 확인일: 2026-08-28 KST

## 목적

이 문서는 VSS 측 담당자나 LLM 검증자가 Snapshot Backend를 검증할 때 사용할 단일
진입점입니다. 검증 대상은 `vss_server/module`이며 `vss/`와 main 소유 파일은 수정하지
않습니다. AWS 배포 여부와 운영값은 VSS 운영 측 결정이 정본입니다.

## 검증 전 고정값

```text
구현 저장소/브랜치  https://github.com/h5vision/vss_server.git / module
검증 경로           module/
검증 module SHA      실행 시 git rev-parse HEAD로 기록
VSS 참조             main@97546fbcea6607a29ad0cc10246a7886bb44ceab
Frontend 참조        frontend@ca2a2c6140fc128f2ae892c13228fa9a433e5d8e
확인된 AWS OS        Ubuntu 22.04.5 LTS
확인된 AWS Python    system/venv 모두 3.10.12 — 지원 범위 일치
필수 Python          3.10 이상, 3.15 미만
검증 완료 OS         Ubuntu 22.04 / 24.04
초기 worker          1
```

검증 시작 시 `module/AGENTS.md`, 이 문서, `06_READINESS_AND_VERIFICATION.md`,
`10_UBUNTU_24_04_VALIDATION.md`, `14_UBUNTU_22_04_AWS_COMPATIBILITY.md`,
`12_POSTGRESQL_RUNTIME_VALIDATION.md` 순서로 읽습니다.
VSS가 Snapshot SHA와 소스 검증값을 소비할 때는 이어서 `13_VSS_SOURCE_API.md`를 읽습니다.
문서와 상대 코드가 다르면 VSS main과 Frontend frontend의 실제 코드를 우선하고 차이를
보고합니다.

## 1. 배포 없이 실행할 검증

`module/`을 Docker build context로 사용합니다.

```bash
docker build \
  --file ops/ubuntu22.04/Dockerfile.verify \
  --tag vss-snapshot-module:ubuntu22-python310-verify \
  .

docker build \
  --file ops/ubuntu24.04/Dockerfile.verify \
  --tag vss-snapshot-module:ubuntu24-verify \
  .
```

합격 조건:

```text
Ubuntu 22.04/Python 3.10.12 및 Ubuntu 24.04/Python 3.12 base image
non-root UID 10001
Ruff 통과
compileall 통과
Contract/Unit/Integration 전체 통과
POSIX permission 장애 테스트 통과
```

Docker 사용이 가능한 격리 개발 환경에서는 실제 PostgreSQL 17 검증도 실행합니다.

```bash
python3 ./scripts/verify_postgresql_17.py
```

이 명령의 통과는 migration·DB 제약·동일 Snapshot 재시도 row lock·startup recovery
advisory lock만 증명하며 운영 role/DSN, shared path 또는 VSS E2E를 증명하지 않습니다.

## 2. AWS host 읽기 전용 preflight

운영 담당자가 service user, 환경변수와 materialization root를 준비한 뒤 해당 service
user로 실행합니다. 스크립트는 비밀값을 출력하지 않고 임시 probe만 생성·삭제합니다.

AWS에서는 systemd `ExecStart`와 동일한 `.venv/bin/python`을
`SNAPSHOT_SERVICE_PYTHON`으로 검사합니다. system `python3`가 우연히 통과해도 service
interpreter가 지원 범위 밖이면 preflight는 실패합니다.

```bash
cd /home/ubuntu/vss_server/module
bash ./scripts/preflight_ubuntu_runtime.sh
```

필수 환경변수:

```text
DATABASE_URL
SNAPSHOT_MATERIALIZATION_ROOT
VSS_BASE_URL
VSS_TOKEN                         선택
VSS_EXPECTED_SOURCE_REVISION      배포 SHA 검증 시 필요
SNAPSHOT_VSS_API_TOKEN            VSS → Snapshot Backend 내부 조회 인증
SNAPSHOT_SERVICE_PYTHON           systemd가 실행할 절대경로 Python
```

preflight의 `[PASS]`는 OS, 명령, 설정 형식, materialization root 쓰기·atomic rename과
VSS `/health`만 증명합니다. PostgreSQL 연결과 Backend/VSS shared path는 `[WAIT]`이며 별도
E2E가 필요합니다.

## 3. 배포된 Backend 읽기 전용 smoke

```bash
export SNAPSHOT_BACKEND_BASE_URL='http://127.0.0.1:8000'
export SNAPSHOT_TEST_PROJECT_ID='<등록된 frontend project 또는 workspace exact ID>'
python3 ./scripts/smoke_backend_readiness.py
```

이 명령은 AWS Ubuntu 인스턴스 안에서 service user로 실행합니다. 외부 검증자는 Backend
loopback을 직접 열지 않고 승인된 AWS ingress 또는 Session Manager를 사용합니다.

스크립트가 확인하는 API:

```text
GET /v1/health
GET /v1/health/ready
GET /v1/index/status?project_id=...
```

status 조회는 Snapshot이 이미 존재하는 project에서만 실행합니다. 응답 HTTP가 `200`이어도
작업 성공을 의미하지 않으므로 반드시 `state`, `reason`, `detail`, `retryable`을 함께
확인합니다.

### VSS source descriptor smoke

VSS service user와 동일 network/filesystem namespace에서 실행합니다.

```bash
curl --fail --silent --show-error \
  -H "X-Snapshot-Token: ${SNAPSHOT_VSS_API_TOKEN}" \
  "http://127.0.0.1:8000/v1/internal/vss/source?project_id=<exact-id>"
```

응답의 `index_request.project_root`에서 VSS가 직접 확인합니다.

```bash
git -C "<project_root>" rev-parse HEAD
git -C "<project_root>" rev-parse 'HEAD^{tree}'
git -C "<project_root>" status --porcelain=v1 --untracked-files=all
```

HEAD와 tree 결과가 각각 `verification.expected_commit_sha`,
`verification.expected_tree_sha`와 같고 status가 비어 있어야 합니다. VSS 완료 후
`index.commit`도 expected commit과 같아야 PASS입니다. token과 전체 JSON은 로그에 남기지
않습니다.

## 4. 쓰기 E2E 합격 조건

쓰기 E2E는 VSS 운영 측이 테스트 Repository/Branch, DB와 shared path를 지정한 뒤에만
수행합니다.

```text
Frontend payload의 base_revision/target_revision이 실제 40자리 commit SHA
활성 binding이 frontend project/workspace를 exact VSS project_id로 해석
POST /v1/workspace-overlays가 202와 VSS_INDEX_ACCEPTED 반환
materialized Git HEAD == target_revision
VSS가 같은 project_root를 읽음
GET /v1/index/status가 done + exact index.commit에서만 completed 반환
동일 target 재요청이 새 Snapshot과 VSS Job을 중복 생성하지 않음
실패 응답이 reason/detail/retryable/X-Request-ID로 원인을 구분
응답과 로그에 token, DSN, 파일 본문, server-local 절대경로가 없음
```

## 5. 검증 중 금지 사항

- `vss_server/main`, `vss/`와 Frontend 참조 브랜치를 수정하지 않습니다.
- 운영 승인 없이 AWS에 배포하거나 보안 그룹·DNS·token을 변경하지 않습니다.
- 상태를 모른다는 이유로 `force=true`를 사용하지 않습니다.
- 실제 운영 Snapshot, revision, incomplete build를 자동 삭제하지 않습니다.
- VSS `202 accepted`를 완료로 기록하지 않습니다.
- `done`인데 `index.commit`이 다른 경우 성공으로 보정하지 않습니다.
- 테스트 출력에 환경변수 값, URL 계정정보, DSN이나 token을 남기지 않습니다.

## 6. 결과 보고 형식

```text
검증 시각/환경
module commit SHA
VSS artifact commit SHA
실행한 명령
PASS/FAIL/WAIT 항목
HTTP status, reason, retryable, X-Request-ID
target_revision과 VSS index.commit 일치 여부
민감정보를 제거한 실패 원인
Production GO 여부와 남은 LIVE 항목
```

로컬 Docker 통과만으로 Production GO를 선언하지 않습니다. `LIVE-01`~`LIVE-09` 증거가
모두 확보될 때까지 결과는 `로컬 검증 완료 / AWS E2E 대기`로 기록합니다.
