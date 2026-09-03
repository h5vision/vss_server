"""
질의 로그 — `/v1/chat` 요청 하나가 DB 한 행으로 남습니다.

목적은 "질문이 서버를 통과했나" 를 로그 파일이 아니라 SQL 로 확인하는 것입니다.

    VSS_QUERYLOG_DSN   비면 아무것도 하지 않습니다 (노트북·chroma 환경).
                       보통은 VSS_PG_DSN 과 같은 값을 넣습니다.
    스키마는 VSS_PG_SCHEMA 를 따릅니다 → 기본 `rag.query_log`

원칙 (md 결정 2026-09-02)
  - **기록 실패가 답변을 죽이지 않습니다.** write() 는 예외를 밖으로 내지 않고 False 를 돌려줍니다.
    불변 조건 1 의 "폴백 없음" 은 임베딩 얘기입니다. 로그는 부가 기능이라 조용히 포기하는 쪽이 맞습니다.
  - `rag:false` 요청은 남기지 않습니다. 거르는 것은 호출 쪽입니다 (이 모듈은 받은 것을 그대로 씁니다).
  - 질문 본문을 저장합니다.
  - 지우는 코드는 두지 않습니다. 보존 기간 없음.
  - 저장 계층(store/)과 분리돼 있습니다. VSS_STORE=chroma 여도 켤 수 있고, pgvector 여도 DSN 이 비면 안 남습니다.

⚠ 컬럼을 넣고 뺄 때는 COLUMNS 만 고칩니다. INSERT 의 컬럼·자리·값이 여기서 한 번에 만들어져 서로 어긋날 수 없습니다.
"""

from __future__ import annotations

import json
import sys

from .config import CFG

# 이 순서가 곧 INSERT 의 컬럼 순서이자 _values() 의 값 순서입니다.
COLUMNS = (
    "request_id", "project_id", "index_id", "resolved_by", "model", "question",
    "outcome", "has_evidence", "top_score", "threshold", "reason", "error_code",
    "timing",
)
JSONB_COLUMNS = frozenset({"timing"})

# outcome — 요청이 어디까지 갔는가. 세 값이 run_chat 의 세 출구와 1:1 입니다.
#   answered     LLM 이 답을 냈다
#   no_evidence  검색이 임계값을 못 넘어 LLM 을 부르지 않았다 (chat.py 의 조기 return)
#   error        중간에 터졌다 (error_code 에 이유)
OUTCOMES = ("answered", "no_evidence", "error")

DDL = """
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.query_log (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    request_id   text NOT NULL,
    project_id   text,
    index_id     text,
    resolved_by  text,
    model        text,
    question     text NOT NULL,
    outcome      text NOT NULL CHECK (outcome IN ('answered','no_evidence','error')),
    has_evidence boolean,
    top_score    double precision,
    threshold    double precision,
    reason       text,
    error_code   text,
    -- 답변 본문·길이는 남기지 않는다. 모델이 빈 답을 냈는지는 timing 의 eval_count 로 보인다.
    timing       jsonb NOT NULL DEFAULT '{{}}'::jsonb
);
CREATE INDEX IF NOT EXISTS query_log_created_idx ON {s}.query_log (created_at DESC);
CREATE INDEX IF NOT EXISTS query_log_request_idx ON {s}.query_log (request_id);
"""

_schema_ready = False


def enabled() -> bool:
    return bool(CFG.querylog_dsn)


def insert_sql(schema: str | None = None) -> str:
    s = schema or CFG.pg_schema
    cols = ", ".join(COLUMNS)
    ph = ", ".join("%s::jsonb" if c in JSONB_COLUMNS else "%s" for c in COLUMNS)
    return f"INSERT INTO {s}.query_log ({cols}) VALUES ({ph})"


def _values(record: dict) -> tuple:
    out = []
    for c in COLUMNS:
        v = record.get(c)
        if c in JSONB_COLUMNS:
            v = json.dumps(v or {}, ensure_ascii=False, default=str)
        out.append(v)
    return tuple(out)


def _connect():
    """psycopg 연결 하나. 테스트는 이 함수를 갈아끼웁니다.

    connect_timeout 을 못 박습니다 — 없으면 DB 가 없을 때 130초를 기다립니다(2026-09-01 실측).
    """
    import psycopg
    return psycopg.connect(CFG.querylog_dsn, autocommit=True, connect_timeout=3)


def ensure_schema(conn) -> None:
    conn.execute(DDL.format(s=CFG.pg_schema))


def from_metadata(metadata: dict, *, question: str, outcome: str,
                  error_code: str | None = None) -> dict:
    """chat.py 가 이미 만들어 둔 metadata 를 그대로 한 행으로 옮깁니다.

    새로 계산하는 값은 없습니다 — 응답에 실려 나간 값과 DB 의 값이 다르면 안 됩니다.
    """
    return {
        "request_id": metadata.get("request_id"),
        "project_id": metadata.get("project_id"),
        "index_id": metadata.get("index_id"),
        "resolved_by": metadata.get("resolved_by"),
        "model": metadata.get("model"),
        "question": question,
        "outcome": outcome,
        "has_evidence": metadata.get("has_evidence"),
        "top_score": metadata.get("top_score"),
        "threshold": metadata.get("threshold"),
        "reason": metadata.get("reason"),
        "error_code": error_code,
        "timing": metadata.get("timing") or {},
    }


def write(record: dict) -> bool:
    """한 행을 남깁니다. DSN 이 비면 아무것도 안 하고, 실패해도 예외를 내지 않습니다."""
    global _schema_ready
    if not enabled():
        return False
    try:
        with _connect() as conn:
            if not _schema_ready:
                ensure_schema(conn)
                _schema_ready = True
            conn.execute(insert_sql(), _values(record))
        return True
    except Exception as e:      # 로그가 답변을 죽이지 않습니다
        print(f"[querylog] 기록 실패 (답변은 정상): {type(e).__name__}: {e}", file=sys.stderr)
        return False
