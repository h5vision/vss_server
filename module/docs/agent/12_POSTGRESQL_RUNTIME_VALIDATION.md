# PostgreSQL 17 로컬 실DB 검증

최종 확인일: 2026-08-28 KST

## 목적과 범위

SQLite fixture와 offline SQL만으로 확인할 수 없는 PostgreSQL transaction, unique
constraint와 row lock을 격리된 실제 DB에서 검증합니다. 이 검증은 개발자 PC나 CI에서
실행하는 Phase 6B 선행 검증이며 AWS 운영 DB 검증을 대체하지 않습니다.

## 실행

Docker와 프로젝트 dev dependency가 준비된 `module/`에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe scripts\verify_postgresql_17.py
```

Ubuntu 24.04+에서는 같은 Python 환경으로 다음과 같이 실행합니다.

```bash
python3 ./scripts/verify_postgresql_17.py
```

실행기는 기본적으로 `postgres:17.10-alpine`을 사용합니다. 고유한 이름과 동적 localhost
port의 임시 컨테이너를 만들고, 성공·실패와 관계없이 자신이 만든 컨테이너만 종료합니다.
기존 PostgreSQL이나 다른 Docker 컨테이너는 변경하지 않습니다. 이미지 변경이 필요하면
`SNAPSHOT_POSTGRES_IMAGE`에 정확한 image tag를 지정합니다.

## 검증 항목

```text
Alembic upgrade head
snapshot.alembic_version == 0003_workspace_id
snapshot schema의 version table + domain table 6종
동시 동일 (vss_project_id, target_revision) insert 중 한 건만 commit
동일 Snapshot SELECT FOR UPDATE의 실제 대기와 commit 후 상태 가시성
Alembic downgrade base 뒤 domain table 0개
Alembic re-upgrade head
```

실DB 전용 파일은 `tests/postgresql_runtime.py`이며 기본 `pytest` 수집 패턴에 포함되지
않습니다. 접속 정보 없이 직접 실행하지 말고 컨테이너 lifecycle과 환경변수를 관리하는
전용 스크립트를 사용합니다. 스크립트는 DSN과 password를 출력하지 않습니다.

## 2026-08-28 결과

```text
PostgreSQL image                         postgres:17.10-alpine
migration upgrade/downgrade/re-upgrade   PASS
schema/version/table                     PASS
concurrent unique constraint             PASS
Snapshot retry row lock                  PASS
실DB 전용 테스트                         3 passed
```

실제 migration 과정에서 schema 생성이 Alembic transaction 밖에서 실행되어 SQLAlchemy 2의
implicit transaction 종료 시 DDL 전체가 rollback되는 결함을 발견했습니다. schema 생성과
version/table DDL을 같은 Alembic transaction에 포함하도록 수정했고, 새 연결에서 결과가
보존되는지 확인했습니다.

## 아직 증명하지 않은 운영 항목

- 운영 `DATABASE_URL`과 TLS/network 경로
- migration role과 runtime role의 권한 분리
- 기존 운영 데이터가 있는 DB의 backup/restore와 rollback 절차
- 다중 Backend instance의 startup recovery claim/lease
- Backend↔VSS shared path와 실제 VSS 인덱싱
- AWS systemd 재시작, 장애 주입과 관측성

따라서 이 검증의 결과 표기는 `PostgreSQL 로컬 실DB 통과 / AWS Phase 6B 대기`이며
Production GO로 기록하지 않습니다.
