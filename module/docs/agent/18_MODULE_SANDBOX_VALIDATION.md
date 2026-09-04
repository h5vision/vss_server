# Module Sandbox 검증

## 목적

`scripts/verify_module_sandbox.sh`는 운영 AWS, systemd, PostgreSQL, VSS, Ollama와 외부
GitHub/GitLab API를 변경하지 않고 module의 핵심 계약을 임시 환경에서 반복 검증합니다.
sLLM 모델, prompt, prefill, token/s와 top-k 성능은 module 책임이 아니므로 포함하지 않습니다.

## 격리 범위

```text
임시 디렉터리          mktemp -d, 종료 시 해당 경로만 제거
DB                     테스트별 SQLite 또는 memory SQLite
Git                    테스트가 생성한 local bare remote와 worktree
GitHub/GitLab           httpx2 MockTransport
VSS                    httpx2 MockTransport
PostgreSQL migration   placeholder DSN의 offline SQL 생성만 수행
외부 network           사용하지 않음
systemd                호출하지 않음
```

운영 token을 읽거나 출력하지 않도록 application 환경변수를 전역으로 주입하지 않습니다.
각 테스트가 자체 Settings와 mock token을 명시하고, migration 명령에만 placeholder DSN을
일시 주입합니다. 테스트 fixture의 명시적 fake token만 사용합니다.

## 실행

module root에서:

```bash
chmod +x scripts/verify_module_sandbox.sh
./scripts/verify_module_sandbox.sh
```

Phase 7 집중 검증만 실행:

```bash
./scripts/verify_module_sandbox.sh --quick
```

실제 AWS 호환 조건까지 강제:

```bash
./scripts/verify_module_sandbox.sh --aws-contract
```

`--aws-contract`는 Ubuntu 22.04와 Python 3.10을 요구합니다. 일반 모드는 프로젝트 지원 범위인
Python 3.10 이상, 3.15 미만을 허용합니다. Python 위치를 명시해야 하면 token이나 DSN이
아닌 실행 파일 경로만 전달합니다.

```bash
SNAPSHOT_SANDBOX_PYTHON=/home/ubuntu/vss_server/module/.venv/bin/python \
  ./scripts/verify_module_sandbox.sh --aws-contract
```

## 검증 단계

1. Python/Git/dependency 사전조건
2. compileall
3. Ruff
4. Phase 7 provider·Tag·commit catalog·VSS source 집중 테스트
5. 전체 pytest 회귀, `--quick`에서는 생략
6. Alembic `0008_repository_tags` head
7. PostgreSQL upgrade/downgrade offline DDL
8. Git diff whitespace

모든 단계가 통과하면 마지막에 다음을 출력합니다.

```text
MODULE SANDBOX VERIFICATION: PASS
```

## 보장하지 않는 항목

- 실제 PostgreSQL transaction·row/advisory lock
- 실제 GitHub/GitLab private/fork credential과 rate limit
- AWS shared filesystem과 실제 VSS index
- systemd service user 권한과 ingress/TLS
- VSS의 sLLM inference 성능

이 항목들은 `12_POSTGRESQL_RUNTIME_VALIDATION.md`, `14_UBUNTU_22_04_AWS_COMPATIBILITY.md`와
Phase 6B AWS E2E에서 별도로 검증합니다.

실제 AWS systemd·PostgreSQL·VSS를 확인하는 별도 harness는
`scripts/verify_aws_runtime.sh`와 `19_AWS_RUNTIME_VERIFICATION.md`를 사용합니다.
