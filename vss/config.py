"""
설정 — 실험에서 바꾸는 값은 전부 여기 모입니다. 환경변수(VSS_*)로 덮어씁니다.

    VSS_OLLAMA_URL   기본 http://127.0.0.1:11434  (EC2에서 서버와 Ollama가 같은 머신)
    VSS_STORE        chroma | pgvector
    VSS_PG_DSN       pgvector 접속 문자열 (예: postgresql://vss_rag:pw@127.0.0.1:5432/vss)

⚠ fingerprint()에 들어가는 값은 인덱스 내용을 바꿉니다. 바꾸면 재인덱싱이 필요합니다.
   질의 시에는 현재 CFG가 아니라 **그 인덱스가 저장한 fingerprint**를 씁니다 (store.index_fingerprint).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping


def _env(key: str, default):
    v = os.getenv(key)
    if v is None:
        return default
    if isinstance(default, bool):
        return v.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(v)
    if isinstance(default, float):
        return float(v)
    return v


# 인덱싱 대상 확장자 ─────────────────────────────────────────
CODE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala",
    ".sql", ".sh", ".bash", ".yaml", ".yml", ".toml", ".ini",
}
DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc"}

# 제외 디렉터리(이름 단위) ────────────────────────────────────
# ⚠ SKIP_DIRS·SKIP_FILE_PATTERNS 를 바꾸면 같은 설정으로도 코퍼스가 달라진다.
#    바꿀 때는 CORPUS_RULES 버전을 함께 올린다 (fingerprint 에 들어가므로 재인덱싱이 필요해진다).
CORPUS_RULES = "v1"

# 청커 세대 순서. 자동 인덱스 선택(indexer.resolve_index)이 "더 새것" 을 고르고, 재정렬 auto 는 ast-v3 이상에서만 켜진다.
# 청커를 추가하면 여기 같이 넣어야 자동 선택이 그 인덱스를 새것으로 본다. 여기 없는 청커는 0 위(가장 낮음).
CHUNKER_RANK = {"ast-v3": 4, "ast-v2": 3, "ast-v1": 2, "line-window-v1": 1}

SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".env", "dist", "build", "target", ".next", ".nuxt", "out",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage", ".idea",
    ".vscode", "vendor", "migrations", ".tox", "site-packages",
    # rag_lab 데모 코퍼스: 인덱스 데이터·체크포인트가 자기 복제본으로 들어가는 것을 막습니다
    "data", ".snapshot-admin-backup",
}
SKIP_FILE_PATTERNS = {
    "package-lock.json", "yarn.lock", "poetry.lock", "Pipfile.lock",
    "pnpm-lock.yaml", "go.sum", "Cargo.lock",
}


# ── 경로 패턴 제외 (exclude_globs) ───────────────────────────
#   *  는 `/` 를 넘지 않음 · ** 는 넘음 · ? 는 한 글자 · glob 문자가 없으면 "그 경로와 그 아래 전부"
#   예) "tests,docs/ko/**,admin/**"
_GLOB_CHARS = set("*?[")


def _glob_to_re(pat: str) -> str:
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                out.append(".*")
                i += 2
                if pat[i:i + 1] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1
    return "".join(out)


@lru_cache(maxsize=32)
def _compiled(spec: str) -> tuple:
    pats = []
    for raw in spec.split(","):
        p = raw.strip().replace("\\", "/").strip("/")
        if not p:
            continue
        if not (_GLOB_CHARS & set(p)):
            pats.append(re.compile(f"^{re.escape(p)}(/.*)?$"))
        else:
            pats.append(re.compile(f"^{_glob_to_re(p)}$"))
    return tuple(pats)


def is_excluded(rel_path: str, spec: str | None = None) -> bool:
    """프로젝트 루트 기준 posix 상대 경로가 제외 대상인가."""
    s = CFG.exclude_globs if spec is None else spec
    if not s:
        return False
    rel = rel_path.replace("\\", "/").lstrip("./")
    return any(p.match(rel) for p in _compiled(s))


@dataclass
class Config:
    # ── 임베딩 (불변 조건: bge-m3 · cosine · 폴백 없음) ─────────
    ollama_url: str = field(default_factory=lambda: _env("VSS_OLLAMA_URL", "http://127.0.0.1:11434"))
    embed_model: str = field(default_factory=lambda: _env("VSS_EMBED_MODEL", "bge-m3:latest"))
    embed_dim: int = 1024
    embed_batch: int = field(default_factory=lambda: _env("VSS_EMBED_BATCH", 16))
    embed_timeout: int = field(default_factory=lambda: _env("VSS_EMBED_TIMEOUT", 120))

    # ── 생성 모델 (LLM 호출은 이 서버가 직접 합니다) ───────────
    # 기본값은 기동 때 올릴 목표 모델이다 (server._prepare_models → ensure_loaded). 폐기 모델을 두면 부팅 때 그것을 올리려다
    # 상주 모델을 evict 하거나(설치돼 있을 때) 매 부팅 404 를 낸다. qwen2.5-coder 는 2026-09-06 폐기 — 목표는 qwen3.8:27b.
    chat_model: str = field(default_factory=lambda: _env("VSS_CHAT_MODEL", "qwen3.8:27b"))
    briefing_model: str = field(default_factory=lambda: _env("VSS_BRIEFING_MODEL", ""))  # 비면 chat_model
    num_ctx: int = field(default_factory=lambda: _env("VSS_NUM_CTX", 8192))
    chat_timeout: int = field(default_factory=lambda: _env("VSS_CHAT_TIMEOUT", 180))
    allow_model_override: bool = field(default_factory=lambda: _env("VSS_ALLOW_MODEL_OVERRIDE", True))
    # 추론 모델(Qwen3 등)의 thinking. 비면 요청에 아예 싣지 않는다 — 이 필드를 모르는 Ollama·모델을 깨뜨리지 않으려고.
    # 0 이면 끈다. 끄면 답변 앞의 추론 토큰이 없어져 첫 토큰까지의 시간(ttft)이 크게 준다.
    think: str = field(default_factory=lambda: _env("VSS_THINK", ""))
    # 브리핑 전용 thinking (2026-09-09). 비면 VSS_THINK 를 따른다. false/true, 또는 모델이 받는 문자열(gpt-oss 의 low|medium|high).
    # 브리핑은 JSON 을 num_predict 안에 다 받아야 해서 기본은 끈다. 모델이 이 값을 거부하면 브리핑은 think_unsupported 로 즉시 실패한다.
    briefing_think: str = field(default_factory=lambda: _env("VSS_BRIEFING_THINK", "false"))
    # 브리핑 run 전체의 시간 예산(초, 2026-09-09). 넘으면 남은 문서·주제를 건너뛰고 final 만 만든다(부분 결과). 0 = 상한 없음.
    briefing_time_budget: int = field(default_factory=lambda: _env("VSS_BRIEFING_TIME_BUDGET", 600))
    # 문서 요약 배치 상한. 한 파일은 이 절반까지만 — 긴 README 가 다른 문서를 밀어내지 않게.
    briefing_doc_batches: int = field(default_factory=lambda: _env("VSS_BRIEFING_DOC_BATCHES", 6))
    # 인덱스마다 남기는 브리핑 run 폴더 수 (md 결정 2026-09-09). 발행된 run 과 진행 중 run 은 이 수와 무관하게 남긴다.
    briefing_keep_runs: int = field(default_factory=lambda: _env("VSS_BRIEFING_KEEP_RUNS", 3))
    # 토큰 어림 계수 (2026-09-09 EC2 실측: qwen3.8:27b, 호출 15개 최소제곱 → ASCII 3.51자/토큰, 그 밖 0.59토큰/자. 여유 17~19% 를 둔 값).
    # 전에는 2 와 2 로 실제의 약 2배를 잡아 호출마다 근거를 반만 넣었다. 호출 뒤 실제값 검사(prompt_eval_count)는 그대로 있다.
    briefing_chars_per_token_ascii: float = field(default_factory=lambda: _env("VSS_BRIEFING_CHARS_PER_TOKEN_ASCII", 3.0))
    briefing_tokens_per_char_other: float = field(default_factory=lambda: _env("VSS_BRIEFING_TOKENS_PER_CHAR_OTHER", 0.7))

    # ── 청킹 (fingerprint) ──────────────────────────────────
    # ast-v3 = ast-v2 + BOM 파일이 AST 를 탄다 (2026-09-07). v1·v2 는 저장된 지문의 코퍼스를 재현하려고 동결.
    chunker: str = field(default_factory=lambda: _env("VSS_CHUNKER", "ast-v3"))   # ast-v3 | ast-v2 | ast-v1 | line-window-v1
    chunk_size: int = field(default_factory=lambda: _env("VSS_CHUNK_SIZE", 1200))
    chunk_overlap: int = field(default_factory=lambda: _env("VSS_CHUNK_OVERLAP", 150))
    min_chunk_chars: int = field(default_factory=lambda: _env("VSS_MIN_CHUNK", 80))
    ast_max_chars: int = field(default_factory=lambda: _env("VSS_AST_MAX_CHARS", 3500))
    context_header: bool = field(default_factory=lambda: _env("VSS_CONTEXT_HEADER", True))
    max_file_bytes: int = field(default_factory=lambda: _env("VSS_MAX_FILE_BYTES", 1_000_000))
    exclude_globs: str = field(default_factory=lambda: _env("VSS_EXCLUDE_GLOBS", ""))
    use_bm25: bool = field(default_factory=lambda: _env("VSS_USE_BM25", True))

    # ── 검색 (런타임 설정 — 재인덱싱 불필요) ─────────────────
    top_k: int = field(default_factory=lambda: _env("VSS_TOP_K", 8))
    # 근거 없음 판정 임계값. 겹치는 분포에서 balanced accuracy를 근사 최대화하는 잠정값 (분리선이 아님).
    score_threshold: float = field(default_factory=lambda: _env("VSS_THRESHOLD", 0.54))
    fusion_pool: int = field(default_factory=lambda: _env("VSS_FUSION_POOL", 20))
    rrf_k: int = field(default_factory=lambda: _env("VSS_RRF_K", 60))
    # 심볼 재정렬. 기본 off — 켜면 같은 인덱스의 다른 측정 셀이 된다 (search_profile 에 실려 run 에 남는다).
    symbol_boost: bool = field(default_factory=lambda: _env("VSS_SYMBOL_BOOST", False))
    # 심볼이 질문에 있을 때만 넓히는 pool. 벡터가 top-20 밖으로 민 정의를 실제 점수째로 데려온다.
    symbol_pool: int = field(default_factory=lambda: _env("VSS_SYMBOL_POOL", 100))
    # 휴리스틱 재정렬 (vss/rerank.py). auto = 그 인덱스의 청커 세대가 ast-v3 이상이면 on.
    # v2 이전 인덱스는 건드리지 않아 한 run 안에서 옛 셀의 수치가 그대로 재현된다. 켜졌는지는 search_profile.rerank 에 남는다.
    rerank: str = field(default_factory=lambda: _env("VSS_RERANK", "auto"))           # auto | on | off
    per_file_cap: int = field(default_factory=lambda: _env("VSS_PER_FILE_CAP", 2))    # 파일당 앞자리 청크 수. 0 = 무제한
    # `/` 없는 패턴은 경로 조각(디렉터리명·파일명) 하나에 fnmatch, `/` 있는 패턴은 exclude_globs 와 같은 문법으로 전체 경로에.
    demote_globs: str = field(default_factory=lambda: _env("VSS_DEMOTE_GLOBS", "tests,test,__tests__,test_*.*,*_test.*,*.test.*,*.spec.*"))  # 뒤로 보낼 경로

    # ── 저장 ─────────────────────────────────────────────────
    store: str = field(default_factory=lambda: _env("VSS_STORE", "chroma"))        # chroma | pgvector
    data_dir: str = field(default_factory=lambda: _env("VSS_DATA_DIR", "./data"))
    pg_dsn: str = field(default_factory=lambda: _env("VSS_PG_DSN", "postgresql://vss_rag:vss_rag@127.0.0.1:5432/vss"))
    pg_schema: str = field(default_factory=lambda: _env("VSS_PG_SCHEMA", "rag"))
    pg_exact: bool = field(default_factory=lambda: _env("VSS_PG_EXACT", False))     # 검증용 정확 검색

    # ── 서버 ─────────────────────────────────────────────────
    token: str = field(default_factory=lambda: _env("VSS_TOKEN", ""))
    # 프론트가 보내는 레포명 → 실제 인덱스 이름
    project_aliases: str = field(default_factory=lambda: _env("VSS_PROJECT_ALIASES", ""))  # 질의 전용: api_test=api-test--ast,...
    # 레포가 놓이는 디렉터리. 비면 GET /projects 가 "아직 인덱싱 안 된 레포" 를 알려주지 않는다(키 자체가 없다).
    # 스냅샷(P)이 붙으면 이 값이 materialize 경로로 바뀐다.
    repos_dir: str = field(default_factory=lambda: _env("VSS_REPOS_DIR", ""))
    # 질의 로그(vss/querylog.py). 비면 안 남긴다 — 저장소가 chroma 여도 켤 수 있고, pgvector 여도 이 값이 비면 꺼진다.
    # 스키마는 pg_schema 를 따른다 (기본 rag.query_log).
    querylog_dsn: str = field(default_factory=lambda: _env("VSS_QUERYLOG_DSN", ""))

    # 경로 도우미 ─────────────────────────────────────────────
    def data_path(self) -> Path:
        return Path(self.data_dir).resolve()

    def index_dir(self) -> Path:
        return self.data_path() / "index"

    def bm25_dir(self) -> Path:
        return self.data_path() / "bm25"

    def briefings_dir(self) -> Path:
        return self.data_path() / "briefings"

    def eval_dir(self) -> Path:
        return self.data_path() / "evaluation"

    def fingerprint(self) -> dict:
        """인덱스와 함께 저장할 파라미터 지문. 값이 다르면 다른 인덱스입니다."""
        return {
            "embed_model": self.embed_model,
            "embed_dim": self.embed_dim,
            "chunker": self.chunker,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "min_chunk_chars": self.min_chunk_chars,
            "ast_max_chars": self.ast_max_chars,
            "max_file_bytes": self.max_file_bytes,
            "context_header": self.context_header,
            "use_bm25": self.use_bm25,
            "exclude_globs": self.exclude_globs,
            "corpus_rules": CORPUS_RULES,
        }

    def to_dict(self) -> dict:
        return asdict(self)


CFG = Config()

# 구 인덱스에 없는 키는 그 키가 도입되기 전의 기본값으로 채웁니다 (현재 CFG 값을 쓰면 안 됩니다).
LEGACY_FINGERPRINT_DEFAULTS = {
    "embed_model": "bge-m3:latest",
    "embed_dim": 1024,
    "chunker": "line-window-v1",
    "chunk_size": 1200,
    "chunk_overlap": 150,
    "min_chunk_chars": 80,
    "ast_max_chars": 3500,
    "max_file_bytes": 1_000_000,
    "context_header": False,
    "use_bm25": False,
    "exclude_globs": "",
    "corpus_rules": "v1",       # 키 도입(2026-08-27) 전 인덱스도 같은 하드코딩 규칙으로 만들어졌다
}


def normalize_fingerprint(fp: Mapping | None) -> dict | None:
    if not fp:
        return None
    out = dict(LEGACY_FINGERPRINT_DEFAULTS)
    out.update(dict(fp))
    if out.get("exclude_globs") is None:
        out["exclude_globs"] = ""
    return out


# ── project_id 별칭 (질의 경로 전용) ──────────────────────────
#   프론트는 레포명 하나(`api_test`)만 알고, 어느 인덱스가 그 답을 내는지는 서버가 정합니다.
#   개선된 인덱스로 갈아탈 때 `.env` 의 VSS_PROJECT_ALIASES 한 줄만 바꾸면 클라이언트는 그대로입니다.
#
#   ⚠ 인덱싱(`cli index`·POST /index)과 평가(`vss.eval`)는 별칭을 쓰지 않습니다 — 실제 인덱스 이름 그대로입니다.
#     측정이 별칭을 타면 "어느 인덱스를 쟀는가" 가 흐려집니다 (불변 조건 6).

def _norm_pid(s: str) -> str:
    return s.strip().lower().replace("_", "-")


@lru_cache(maxsize=8)
def _alias_map(spec: str) -> tuple:
    out = []
    for pair in spec.split(","):
        k, sep, v = pair.partition("=")
        k, v = k.strip(), v.strip()
        if k and sep and v:
            out.append((_norm_pid(k), v))
    return tuple(out)


def alias_map() -> dict:
    """현재 유효한 별칭 표 (진단·/health 표시용)."""
    return dict(_alias_map(CFG.project_aliases))


def resolve_project_id(project_id: str | None) -> str | None:
    """요청이 보낸 project_id 를 실제 인덱스 이름으로 바꿉니다. 별칭이 없으면 받은 그대로.

    `api_test` 와 `api-test` 는 같은 것으로 봅니다(언더스코어/하이픈, 대소문자).
    별칭이 없는 인덱스를 가리키면 여기서 감추지 않고 search 가 기존 예외를 냅니다 — 조용한 폴백은 두지 않습니다.
    """
    if not project_id:
        return project_id
    key = _norm_pid(project_id)
    for k, v in _alias_map(CFG.project_aliases):
        if k == key:
            return v
    return project_id


def profile_value(profile: Mapping | None, key: str):
    """명시적 인덱스 프로필의 값. 없을 때만 현재 CFG를 사용합니다."""
    if profile is not None and key in profile:
        return profile[key]
    return getattr(CFG, key)


def resolve_profile(overrides: Mapping | None = None) -> dict:
    """요청 본문의 부분 프로필을 현재 CFG 위에 얹어 완전한 fingerprint로 만듭니다."""
    fp = CFG.fingerprint()
    for k, v in (overrides or {}).items():
        if k in fp and v is not None:
            fp[k] = type(fp[k])(v) if not isinstance(fp[k], bool) else bool(v)
    return normalize_fingerprint(fp) or fp
