# AWS Runtime 검증

## 목적

`scripts/verify_aws_runtime.sh`는 `hancom-team2-5th`의 실제 Ubuntu 22.04.5 환경에서
module 운영 경계를 검증합니다. sLLM/Ollama 성능은 module 책임이 아니므로 측정하지
않습니다.

기본 실행은 다음을 확인합니다.

```text
Ubuntu 22.04
module venv Python 3.10
module sandbox full 검증
vss-snapshot.service / vss-admin-web.service active
systemd ExecStartPre preflight
Backend/Admin/VSS listen socket
Backend liveness/readiness
VSS health/store/embed dependency
Alembic 0008_repository_tags
```

`--project-id`를 주면 실제 Backend 내부 pull과 VSS status까지 확인합니다. `--run-sync`를
명시했을 때만 지정 Repository의 remote Git fetch, Snapshot/VSS 제출과 완료 polling을
수행합니다.

## 안전 경계

| 옵션 | 영향 |
|---|---|
| 기본 실행 | 읽기 전용 health/status 검증, migration 변경 없음 |
| `--project-id` | 내부 revision/PR·MR/source 조회, 데이터 변경 없음 |
| `--migrate` | 실제 PostgreSQL에 `alembic upgrade head` 수행 |
| `--restart` | Backend/Admin systemd 재시작과 짧은 연결 중단 |
| `--run-sync` | 지정 Repository의 remote sync와 Snapshot/VSS 작업 시작 |

Token, DSN, provider response body, server-local `project_root`와 Git stderr는 출력하지
않습니다. module/admin 환경 파일은 root 권한으로 읽으며 token 값을 명령행 인자로 받지
않습니다. 실행 전 `sudo -v`로 인증을 유지합니다.

## 실행 순서

module을 배포한 직후에는 먼저 migration과 service reload를 포함해 실행합니다.

```bash
cd /home/ubuntu/vss_server/module
sudo -v
bash scripts/verify_aws_runtime.sh --migrate --restart
```

`--migrate`와 `--restart`는 각각 실제 DB 변경과 service 중단을 포함하므로 명시적으로
사용합니다. 재실행 시 migration은 head 상태라면 no-op입니다.

기존 Snapshot/VSS project를 읽기 전용으로 검증합니다.

```bash
bash scripts/verify_aws_runtime.sh \
  --project-id '<exact-vss-project-id>'
```

실제 Repository sync부터 VSS exact commit 완료까지 검증합니다.

```bash
bash scripts/verify_aws_runtime.sh \
  --project-id '<exact-vss-project-id>' \
  --repository-id '<repository-uuid>' \
  --run-sync \
  --poll-seconds 120
```

`--run-sync`는 정확한 Repository UUID와 VSS project ID를 요구합니다. Repository sync는
해당 Repository에 등록된 tracked Branch를 대상으로 하며, provider/Tag 수집은
`module.env`의 opt-in 설정에 따릅니다.

AWS 호환 Python을 별도로 지정해야 하는 경우:

```bash
SNAPSHOT_SERVICE_PYTHON=/home/ubuntu/vss_server/module/.venv/bin/python \
  bash scripts/verify_aws_runtime.sh --aws-contract
```

스크립트는 현재 module 경로와 다음 환경 파일을 기본값으로 사용합니다.

```text
/home/ubuntu/vss_server/module
/etc/vss-snapshot/module.env
/etc/vss-snapshot/admin-web.env
```

필요하면 `SNAPSHOT_AWS_MODULE_ROOT`, `SNAPSHOT_AWS_MODULE_ENV`,
`SNAPSHOT_AWS_ADMIN_ENV`, `SNAPSHOT_AWS_BACKEND_URL`, `SNAPSHOT_AWS_VSS_URL`로
명시합니다.

## PASS 기준

```text
AWS RUNTIME VERIFICATION: PASS
```

`--run-sync`를 사용한 경우 추가로 다음이 PASS여야 합니다.

```text
signed Repository sync request
VSS Snapshot revisions pull
VSS done + exact target commit
VSS exact source descriptor and Git verification
```

VSS가 `running`이면 기본 project check에서는 `[WAIT]`로 남을 수 있습니다. 실제 sync
검증에서는 `--poll-seconds` 동안 polling하고, failed/aborted 또는 다른
`index.commit`이면 실패합니다.

## 검증하지 않는 것

- 실제 PostgreSQL transaction/row lock/advisory lock 장애 주입
- 실제 GitHub/GitLab private credential과 rate limit 정책
- Admin Web 외부 TLS/VPN/보안 그룹
- 모든 실패 시 이전 active index 보존
- sLLM 모델, prompt, prefill, top-k와 tok/s

이 항목들은 Phase 6B 운영 검증과 Phase 7C VSS 소비자 E2E에서 별도로 처리합니다.
