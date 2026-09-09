# AWS Ubuntu 22.04.5·Python 3.10 호환 명세

최종 확인일: 2026-08-28 KST

## 확인된 운영 환경

```text
Host                  hancom-team2-5th
OS                    Ubuntu 22.04.5 LTS (jammy)
Module path           /home/ubuntu/vss_server/module
systemd unit          /etc/systemd/system/vss-snapshot.service
Backend bind target   127.0.0.1:8000
VSS target            127.0.0.1:8200
System Python         3.10.12
Module venv Python    3.10.12
Git                   2.34.1
```

운영 기준은 AWS를 따릅니다. 프로젝트 Python 지원 범위는 `>=3.10,<3.15`이며 systemd는
`/home/ubuntu/vss_server/module/.venv/bin/python`을 사용합니다. Ubuntu 22.04의 system
Python과 module venv가 모두 3.10.12인 현재 환경은 지원 범위에 포함됩니다.

## Phase 6A-2 구현 결과

Python 3.10에서 제공되지 않는 다음 API를 호환 구현으로 교체했습니다.

```text
enum.StrEnum                 → str, Enum 다중 상속
typing.Self                  → postponed concrete class annotation
datetime.UTC                 → datetime.timezone.utc
Path.is_junction             → capability 검사 + Windows reparse-point fallback
shutil.rmtree(onexc=...)     → onerror callback
```

추가된 운영·검증 파일:

```text
scripts/preflight_ubuntu_runtime.sh
scripts/verify_ubuntu_runtime.sh
ops/ubuntu22.04/Dockerfile.verify
ops/ubuntu22.04/vss-snapshot.service.example
```

`preflight_ubuntu_24_04.sh`와 `verify_ubuntu_24_04.sh`는 기존 자동화 호환을 위해 유지합니다.
전자는 OS 중립 preflight wrapper이고, 후자는 24.04 gate 뒤 공통 검증기를 실행합니다.

## 검증 결과

```text
Ubuntu 22.04 / Python 3.10.12 / Git 2.34.1 / non-root
Ruff                                              PASS
compileall                                        PASS
Contract + Unit + Integration                     124 passed
preflight loopback·service interpreter fixture    PASS

Ubuntu 24.04 / Python 3.12.3 / non-root
Ruff                                              PASS
compileall                                        PASS
Contract + Unit + Integration                     124 passed
preflight fixture                                 PASS
```

이 결과는 OS·Python 코드 호환성을 증명합니다. 실제 AWS systemd, PostgreSQL, VSS와 shared
path E2E를 대신하지 않습니다.

## systemd 경로 계약

정본 예제는 `ops/ubuntu22.04/vss-snapshot.service.example`입니다.

```ini
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/vss_server/module
EnvironmentFile=/etc/vss-snapshot/module.env
Environment=SNAPSHOT_VSS_API_TOKEN_CONFIG_PATH=/etc/vss-snapshot/module.env
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=SNAPSHOT_SERVICE_PYTHON=/home/ubuntu/vss_server/module/.venv/bin/python
ExecStartPre=/bin/bash /home/ubuntu/vss_server/module/scripts/preflight_ubuntu_runtime.sh
ExecStart=/home/ubuntu/vss_server/module/.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1
ProtectHome=read-only
ReadOnlyPaths=/home/ubuntu/vss_server/module
ReadWritePaths=/srv/vss-snapshots
```

`ProtectHome=true`는 home checkout을 숨기므로 사용할 수 없습니다. `ProtectHome=read-only`와
`PYTHONDONTWRITEBYTECODE=1`로 코드는 읽기 전용으로 두고 materialization root만 쓰기
허용합니다. 실제 `SNAPSHOT_MATERIALIZATION_ROOT`도 `/srv/vss-snapshots`와 일치해야 합니다.
VSS가 token 없이 내부 API를 호출했을 때 token 값 대신 이 config 경로를 안내합니다.

Phase 7A-3 provider/Tag 수집을 활성화할 때만 `module.env`에 다음을 추가합니다.

```text
SNAPSHOT_CHANGE_REQUEST_COLLECTION_ENABLED=true
SNAPSHOT_GITHUB_API_URL=https://api.github.com
SNAPSHOT_GITHUB_API_VERSION=2026-03-10
SNAPSHOT_GITHUB_API_TOKEN=<read-only-token>
SNAPSHOT_GITLAB_API_URL=https://gitlab.example/api/v4
SNAPSHOT_GITLAB_API_TOKEN=<read-only-token>
SNAPSHOT_CHANGE_REQUEST_MAX_PAGES=10
SNAPSHOT_TAG_COLLECTION_ENABLED=true
SNAPSHOT_TAG_MAX_COUNT=5000
```

미사용 provider token은 비워 둘 수 있습니다. token 값은 journal, API와 Admin browser에
출력하지 않습니다.

운영 AWS에서 live VSS/PostgreSQL을 건드리지 않고 module만 검증하려면 다음을 실행합니다.

```bash
cd /home/ubuntu/vss_server/module
bash scripts/verify_module_sandbox.sh --aws-contract
```

이 harness는 sLLM/Ollama 성능을 측정하지 않으며, 실제 PostgreSQL·systemd·provider
credential·VSS shared path 검증을 대체하지 않습니다.

## AWS 적용 전·후 확인

module root에서 현재 venv의 interpreter와 package를 확인하고 최신 코드를 설치합니다.

```bash
cd /home/ubuntu/vss_server/module
./.venv/bin/python --version
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e .
```

운영 담당자가 unit과 환경 파일을 반영한 뒤 다음을 실행합니다.

```bash
sudo systemctl daemon-reload
sudo systemctl start vss-snapshot.service
sudo systemctl status vss-snapshot.service --no-pager
sudo journalctl -u vss-snapshot.service -n 100 --no-pager
curl --fail --silent --show-error http://127.0.0.1:8000/v1/health
curl --fail --silent --show-error http://127.0.0.1:8000/v1/health/ready
```

환경 파일의 token, DSN과 credential은 출력하거나 문서에 복사하지 않습니다. readiness는
HTTP 200만 보지 않고 DB와 VSS dependency별 상태·reason을 확인합니다.

## 현재 판정

```text
Ubuntu 22.04.5 + Python 3.10 compatibility   LOCAL PASS
Ubuntu 24.04 + Python 3.12 regression         PASS
AWS Python/Git version                         SUPPORTED
AWS unit replacement + ExecStartPre            WAIT
AWS service active + liveness/readiness         WAIT
Phase 3B-2 / Phase 6B AWS E2E                  WAIT
```

실제 AWS runtime 검증은 `scripts/verify_aws_runtime.sh`를 사용하고 상세 옵션과 PASS
조건은 `19_AWS_RUNTIME_VERIFICATION.md`를 따릅니다.
