-- vss 데이터베이스 초기화 — EC2 에서 postgres 슈퍼유저로 한 번 실행합니다.
--   sudo -u postgres psql -v rag_pw="'바꿀비밀번호'" -v snap_pw="'바꿀비밀번호'" -f - < scripts/db_init.sql
--   ⚠ `-f 파일경로` 로 주면 안 됩니다 — postgres 사용자는 /home/<계정> 을 통과할 수 없어 파일을 못 읽습니다.
--     `-f -` 로 두고 호출하는 셸이 파일을 읽어 stdin 으로 넘깁니다. 이 스크립트는 여러 번 돌려도 안전합니다.
-- 스키마 둘: rag (벡터·인덱스 — md 소유) / snapshot (레포 스냅샷 — P 소유). 서로의 스키마를 건드리지 않습니다.

\set ON_ERROR_STOP on

SELECT 'CREATE ROLE vss_rag LOGIN PASSWORD ' || :'rag_pw'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vss_rag') \gexec
SELECT 'CREATE ROLE vss_snapshot LOGIN PASSWORD ' || :'snap_pw'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vss_snapshot') \gexec

SELECT 'CREATE DATABASE vss OWNER vss_rag'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'vss') \gexec

\connect vss

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS rag      AUTHORIZATION vss_rag;
CREATE SCHEMA IF NOT EXISTS snapshot AUTHORIZATION vss_snapshot;

-- 서로 읽기는 허용, 쓰기는 소유자만
GRANT USAGE ON SCHEMA rag      TO vss_snapshot;
GRANT USAGE ON SCHEMA snapshot TO vss_rag;
ALTER DEFAULT PRIVILEGES FOR ROLE vss_rag      IN SCHEMA rag      GRANT SELECT ON TABLES TO vss_snapshot;
ALTER DEFAULT PRIVILEGES FOR ROLE vss_snapshot IN SCHEMA snapshot GRANT SELECT ON TABLES TO vss_rag;

-- vector 타입은 public 스키마에 있으므로 두 역할 모두 public 사용 권한 필요
GRANT USAGE ON SCHEMA public TO vss_rag, vss_snapshot;

-- rag 스키마의 테이블은 vss/store/pgvector.py 의 ensure_schema() 가 만듭니다 (DDL 은 그 파일이 정본).

\echo '--- 확인'
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
SELECT nspname, pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname IN ('rag', 'snapshot');
