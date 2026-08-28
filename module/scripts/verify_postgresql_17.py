"""격리된 PostgreSQL 17 컨테이너로 실제 migration과 동시성 제약을 검증한다."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

MODULE_ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("SNAPSHOT_POSTGRES_IMAGE", "postgres:17.10-alpine")
CONTAINER_NAME = f"vss-snapshot-pg-verify-{uuid4().hex[:12]}"
DATABASE_USER = "snapshot_test"
DATABASE_NAME = "snapshot_test"
DATABASE_PASSWORD = "snapshot_test_password"


def run(*arguments: str, capture: bool = False, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        list(arguments),
        cwd=MODULE_ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=capture,
        env=env,
    )
    return completed.stdout.strip() if capture else ""


def wait_until_ready() -> None:
    for _ in range(120):
        completed = subprocess.run(
            [
                "docker",
                "exec",
                CONTAINER_NAME,
                "pg_isready",
                "-U",
                DATABASE_USER,
                "-d",
                DATABASE_NAME,
            ],
            cwd=MODULE_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError("검증용 PostgreSQL이 30초 안에 준비되지 않았습니다.")


def mapped_port() -> int:
    output = run("docker", "port", CONTAINER_NAME, "5432/tcp", capture=True)
    port_text = output.rsplit(":", maxsplit=1)[-1]
    if not port_text.isdigit():
        raise RuntimeError("검증용 PostgreSQL host port를 확인하지 못했습니다.")
    return int(port_text)


def verify_no_domain_tables() -> None:
    count = run(
        "docker",
        "exec",
        CONTAINER_NAME,
        "psql",
        "-U",
        DATABASE_USER,
        "-d",
        DATABASE_NAME,
        "-Atc",
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='snapshot' AND table_name <> 'alembic_version';",
        capture=True,
    )
    if count != "0":
        raise RuntimeError("downgrade 뒤 Snapshot domain table이 남아 있습니다.")


def main() -> None:
    print(f"[INFO] PostgreSQL image: {IMAGE}")
    try:
        run(
            "docker",
            "run",
            "--rm",
            "--detach",
            "--name",
            CONTAINER_NAME,
            "--env",
            f"POSTGRES_USER={DATABASE_USER}",
            "--env",
            f"POSTGRES_PASSWORD={DATABASE_PASSWORD}",
            "--env",
            f"POSTGRES_DB={DATABASE_NAME}",
            "--publish",
            "127.0.0.1::5432",
            IMAGE,
        )
        wait_until_ready()
        port = mapped_port()
        database_url = (
            f"postgresql+asyncpg://{DATABASE_USER}:{DATABASE_PASSWORD}"
            f"@127.0.0.1:{port}/{DATABASE_NAME}"
        )
        environment = {
            **os.environ,
            "DATABASE_URL": database_url,
            "SNAPSHOT_TEST_POSTGRES_URL": database_url,
        }

        run(sys.executable, "-m", "alembic", "upgrade", "head", env=environment)
        run(sys.executable, "-m", "pytest", "-q", "tests/postgresql_runtime.py", env=environment)
        run(sys.executable, "-m", "alembic", "downgrade", "base", env=environment)
        verify_no_domain_tables()
        run(sys.executable, "-m", "alembic", "upgrade", "head", env=environment)
        print("[PASS] PostgreSQL migration upgrade/downgrade/re-upgrade")
        print("[PASS] PostgreSQL unique constraint와 Snapshot row lock")
        print("[PASS] PostgreSQL startup recovery advisory lock")
    finally:
        subprocess.run(
            ["docker", "stop", "--timeout", "5", CONTAINER_NAME],
            cwd=MODULE_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


if __name__ == "__main__":
    main()
