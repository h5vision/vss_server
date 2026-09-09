# Ubuntu 24.04+ 배포·검증 기준

최종 확인일: 2026-08-28 KST

> 이 문서는 이미 통과한 Ubuntu 24.04 검증 증거를 보존합니다. 실제 AWS host는 Ubuntu
> 22.04.5, system/venv Python은 3.10.12로 확인됐으며 현재 운영 target 전환 정본은
> `14_UBUNTU_22_04_AWS_COMPATIBILITY.md`입니다. 두 OS 결과는 서로 대체하지 않고 독립
> 회귀 증거로 유지합니다.

## 범위

이 문서는 Snapshot Backend가 Ubuntu 24.04 환경에서 통과한 회귀 기준입니다. 실제 AWS
22.04.5 배포 절차와 systemd smoke 결과는 문서 14에서 별도로 관리합니다.

## 기준 환경

```text
OS                    Ubuntu 24.04 LTS 이상
검증 Python           3.12.3 (지원 범위 3.10 이상, 3.15 미만)
Git                   OS package의 git CLI
Filesystem            POSIX 권한과 atomic rename을 지원하는 동일 filesystem
Process user          전용 non-root service account
Snapshot root 예시    /srv/vss-snapshots
Backend worker        초기 운영 검증은 1 worker
```

Ubuntu 24.04의 기본 Python 3.12는 `pyproject.toml`의 지원 범위에 포함됩니다. Python,
Git과 CA certificate가 없거나 service user가 materialization root를 읽고 쓸 수 없으면
배포 준비 완료로 표시하지 않습니다.

## 로컬 Ubuntu 24.04 검증

저장소 최상위가 아니라 `module/`을 Docker build context로 사용합니다.

```powershell
cd module
docker build `
  --file ops/ubuntu24.04/Dockerfile.verify `
  --tag vss-snapshot-module:ubuntu24-verify `
  .
```

Dockerfile은 `ubuntu:24.04`에서 non-root UID `10001`로 다음을 실행합니다.

```text
editable dev dependency 설치
Ruff
compileall
Contract/Unit/Integration 전체 pytest
local Git clone/checkout/tree/HEAD 검증
POSIX 임시 디렉터리 생성과 정리
fixture VSS를 사용한 service-user preflight
```

실제 Ubuntu 24.04+ 호스트에서는 module root에서 다음 스크립트를 실행할 수 있습니다.

```bash
bash ./scripts/verify_ubuntu_24_04.sh
```

스크립트는 OS와 Python 버전을 확인하고 `mktemp` 전용 경로에 일회용 venv를 생성합니다.
bytecode와 pytest cache도 임시 경로에 격리하므로 checkout을 읽기 전용으로 제공할 수
있습니다. 검증 종료 시 그 임시 경로만 삭제하며 materialization root나 운영 데이터를
삭제하지 않습니다.

## 로컬 검증 결과 — 2026-08-28 KST

```text
Base image  ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517
User        non-root UID 10001
Python      3.12
Git         2.43.0
Ruff        passed
compileall  passed
pytest      124 passed, 1 dependency deprecation warning
preflight   fixture VSS 기준 PASS, PostgreSQL/shared path는 WAIT
```

이 결과는 OS·패키지·POSIX 동작 호환 증거이며 AWS network, shared mount, PostgreSQL과
실제 VSS 서비스 통합을 통과했다는 의미는 아닙니다.

## AWS Ubuntu 배치 전 확인

```text
/etc/vss-snapshot/module.env 권한과 소유자
DATABASE_URL의 snapshot schema migration role
VSS_BASE_URL과 VSS_TOKEN 전달 방식
SNAPSHOT_VSS_API_TOKEN의 VSS→Backend 별도 전달 방식
/srv/vss-snapshots의 소유자·권한·용량
Backend와 VSS 양쪽에서 같은 project_root가 보이는 mount
Git provider outbound HTTPS와 credential helper
보안 그룹의 Frontend→Backend, Backend→VSS 최소 포트
systemd restart와 로그 보존 정책
```

배포 전 service user와 실제 환경변수로 다음을 실행합니다.

```bash
bash ./scripts/preflight_ubuntu_runtime.sh
```

이 스크립트는 환경변수 값을 출력하지 않고 OS, URL 형식, materialization root
쓰기·atomic rename과 인증된 VSS `/health` 계약을 확인합니다. DB 실제 연결과 shared path
양방향 접근은 별도 E2E 대기로 남깁니다.

권장 systemd 실행 경계는 `ops/ubuntu24.04/vss-snapshot.service.example`에 제공합니다.
실제 경로·사용자·포트와 `ReadWritePaths`는 VSS 운영 측 배포안이 정본입니다.

동일 인스턴스 Linux service 배포에서는 다음 loopback 경계를 사용합니다.

```text
Snapshot Backend bind  127.0.0.1:8000
VSS_BASE_URL            http://127.0.0.1:8200
PostgreSQL host         127.0.0.1:5432
외부 공개               승인된 AWS ingress/TLS 경계
```

외부 ingress는 `/v1/internal/*`를 전달하지 않습니다. VSS source 조회는 같은
인스턴스에서 `127.0.0.1:8000`으로만 호출합니다.

예제에는 loopback bind, `ExecStartPre` preflight, non-root 실행, 재시작 정책, 쓰기 허용 경로와 기본
systemd hardening이 포함됩니다. 그대로 운영에 복사하지 말고 실제 설치 경로와 mount를
검토한 뒤 사용합니다.

## Linux filesystem 주의사항

- materialization staging과 revisions는 atomic rename을 위해 같은 filesystem에 둡니다.
- service user 외 쓰기 권한을 최소화하고 VSS에는 필요한 read 권한만 부여합니다.
- Linux checkout에서 발견된 symlink는 현재 보안 정책상 VSS 제출 전에 차단합니다.
- 파일명 대소문자를 구분하므로 Windows Frontend에서 대소문자만 바뀐 경로는 별도 E2E로
  확인합니다.
- Git command는 shell 문자열이 아니라 argument 배열로 실행하며 global/system Git config와
  대화형 credential prompt를 비활성화합니다.
- server-local 절대경로는 VSS request에만 사용하고 Frontend/Admin 응답에는 노출하지
  않습니다.

## Phase 5 운영 설정

```text
SNAPSHOT_RECOVERY_ON_STARTUP=true
SNAPSHOT_RECOVERY_BATCH_SIZE=100
```

startup recovery는 `submitting|accepted|indexing` Snapshot의 VSS 상태만 조회하고 자동
`force=true` 재제출을 하지 않습니다. PostgreSQL DB 단위 advisory lock으로 복구 조정자
하나만 실행되도록 구현했지만, 초기 AWS 검증은 Backend 1 worker로 수행합니다. 여러
worker/instance로 확장하기 전에는 배포 DB에서 잠금 상호 배제와 연결 장애 해제를 다시
검증해야 합니다.

## AWS 실전 검증 대기 항목

- 운영 PostgreSQL migration/runtime role 분리와 실제 DSN readiness
- remote Git clone latency와 Frontend 10초 timeout
- shared mount에서 materialized Git HEAD와 VSS `index.commit` 일치
- VSS 재시작·Backend 재시작 후 상태 수렴
- disk full, permission denied, network timeout, TLS와 token 실패
- systemd restart, graceful shutdown과 recovery batch

이 항목은 AWS 인스턴스가 제공되고 VSS 운영 측이 배포를 승인한 뒤 수행합니다. 현재 로컬
Ubuntu 컨테이너 통과만으로 Production GO를 선언하지 않습니다.
