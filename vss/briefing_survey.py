"""Read-only repository survey and versioned source evidence for briefings.

No repository code is imported or executed. Static connections are candidates,
not proof of runtime dispatch. Python gets AST extraction; other languages retain
line-addressable text evidence and an explicit analysis limitation.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

from . import analysis
from .config import CFG, CODE_EXT, DOC_EXT, SKIP_DIRS, SKIP_FILE_PATTERNS, is_excluded

CONFIG_NAMES = {"pyproject.toml", "package.json", "setup.py", "setup.cfg", "requirements.txt",
                "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "composer.json",
                "Dockerfile", "docker-compose.yml", "compose.yaml", "Makefile", "Procfile"}
SECTION_PRIORITY = re.compile(r"install|setup|usage|run|config|architect|overview|feature|limit|"
                              r"실행|설치|사용|설정|구조|개요|기능|제약", re.I)
DEPENDENCY = re.compile(r"sql|database|redis|mongo|postgres|sqlite|http|requests|urllib|queue|"
                        r"kafka|storage|chroma|boto|socket", re.I)
INTRO_HEAD_LINES = 40      # README 첫 절에서 먼저 읽는 줄 수. 나머지는 다른 문서의 우선 절 뒤로 (2026-09-09)
# 변경 이력 성격의 문서 — 어떤 절이든 문서 순서 맨 뒤 (2026-09-09 EC2 run 에서 release-notes 의 "Features" 절들이 README 본문보다
# 앞서 문서 근거 38개 중 32개를 차지했다). analysis.doc_files 의 같은 규칙을 survey 에도.
LOW_VALUE_DOC = re.compile(r"^(changelog|release[-_]?notes?|history|news|changes)\b", re.I)


class SourceUnstable(RuntimeError):
    """조사(scan) 도중 소스가 바뀌었고, 다시 읽어도 또 바뀌었다 — 섞인 버전으로 분석하지 않는다 (2026-09-09).
    브리핑은 LLM 호출 전에 이 코드로 실패한다."""
    code = "source_unstable"


def digest(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def git_state(root: Path) -> dict:
    def run(*args):
        try:
            p = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=10)
            return p.stdout.strip() if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {"commit": commit, "dirty": None if status is None else bool(status)}


def is_test(path: str) -> bool:
    p = Path(path)
    return bool(set(p.parts) & {"tests", "test"}) or p.name.startswith("test_") or ".test." in p.name


def char_counts(text: str) -> tuple[int, int]:
    """(ASCII 글자 수, 그 밖 글자 수). 호출마다 metric 에 남겨 실제 prompt_eval_count 와 함께 계수를 푸는 재료 (2026-09-09)."""
    ascii_count = sum(ord(c) < 128 for c in text)
    return ascii_count, len(text) - ascii_count


def tokens(text: str) -> int:
    """어림값이지 tokenizer 가 아니다: ASCII 는 계수로 나누고, 그 밖 글자는 계수를 곱한다 (설정, 2026-09-09 실측으로 정함).

    호출마다 어림값과 Ollama 실제값(prompt_eval_count)이 metric 에 남아 계수를 다시 잴 수 있다. 오프라인 배포에
    tokenizer 가 있다고 가정하지 않는다."""
    ascii_count, other = char_counts(text)
    return int(ascii_count / CFG.briefing_chars_per_token_ascii + other * CFG.briefing_tokens_per_char_other) + 1


class Survey:
    def __init__(self, root: str, profile: dict | None = None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("project_root must be a directory")
        self.profile = profile or {}
        self.files: dict[str, dict] = {}
        self.sources: dict[str, list[str]] = {}
        self.symbols: list[dict] = []
        self.interfaces: list[dict] = []
        self.connections: list[dict] = []
        self.entries: list[dict] = []
        self.commands: list[dict] = []
        self.dependencies: list[dict] = []
        self.limitations: list[dict] = []
        self.evidence: list[dict] = []
        self._evidence_keys: dict[tuple, int] = {}
        self.state = git_state(self.root)
        self._scan()
        # scan 은 몇 초라 그 사이 바뀐 파일(섞인 버전)은 여기서만 생긴다 — 바로 다시 해시해 잡는다. 한 번 다시 읽고, 또 다르면 포기.
        # 그 뒤(생성 중)의 변경은 메모리의 sources 만 쓰므로 분석을 섞지 않고 **오래되게만** 한다 (pipeline 이 표시하고 발행,
        # md 결정 2026-09-09).
        if self.changed_paths():
            self._reset()
            self.state = git_state(self.root)
            self._scan()
            if self.changed_paths():
                raise SourceUnstable("sources changed while being read, twice in a row")
        self._extract_entries_and_routes()
        self.source_digest = digest({p: f["sha256"] for p, f in self.files.items()})

    def _reset(self):
        self.files, self.sources = {}, {}
        self.symbols, self.interfaces, self.connections, self.entries = [], [], [], []
        self.commands, self.dependencies, self.limitations, self.evidence = [], [], [], []
        self._evidence_keys, self.excluded = {}, {}

    def _scan(self):
        excluded = Counter()
        max_bytes = int(self.profile.get("max_file_bytes", CFG.max_file_bytes))
        spec = self.profile.get("exclude_globs", CFG.exclude_globs)
        total_bytes = 0
        for base, dirs, names in os.walk(self.root, followlinks=False):
            kept = []
            for name in sorted(dirs):
                path = Path(base) / name
                rel = path.relative_to(self.root).as_posix()
                if name in SKIP_DIRS | {"adocs"} or name.startswith(".") or path.is_symlink() or is_excluded(rel, spec):
                    excluded["excluded_directory"] += 1
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(names):
                p = Path(base) / name
                rel = p.relative_to(self.root).as_posix()
                if (p.is_symlink() or name in {"AGENTS.md", "CLAUDE.md"} or name.startswith(".") or name.endswith((".pem", ".key"))
                        or any(fnmatch.fnmatch(name, pat) for pat in SKIP_FILE_PATTERNS)
                        or is_excluded(rel, spec)):
                    excluded["excluded_file"] += 1
                    continue
                suffix = p.suffix.lower()
                config = name in CONFIG_NAMES or name.endswith((".service", ".sh"))
                if suffix not in CODE_EXT | DOC_EXT and not config and name.lower() != "readme":
                    excluded["unsupported_extension"] += 1
                    continue
                try:
                    size = p.stat().st_size
                    if size > max_bytes or total_bytes + size > 40_000_000 or len(self.files) >= 4000:
                        self.limitations.append({"path": rel, "reason": "survey_size_limit"})
                        continue
                    raw = p.read_bytes()
                    if b"\x00" in raw:
                        excluded["binary"] += 1
                        continue
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        text = raw.decode("cp949")
                    total_bytes += size
                except (OSError, UnicodeError):
                    self.limitations.append({"path": rel, "reason": "unreadable"})
                    continue
                kind = "config" if config else "doc" if suffix in DOC_EXT or name.lower() == "readme" else "code"
                if is_test(rel):
                    kind = "test"
                self.sources[rel] = text.splitlines()
                self.files[rel] = {"path": rel, "type": kind, "sha256": digest(raw), "bytes": size,
                                   "lines": len(self.sources[rel])}
                if config:
                    self._config(rel, text)
                if suffix == ".py":
                    self._python(rel, text)
                elif kind in ("code", "test"):
                    self.limitations.append({"path": rel, "reason": "text_only_language"})
        self.excluded = dict(excluded)

    def _extract_entries_and_routes(self):
        """진입점·HTTP 라우트는 analysis.py 의 AST 추출을 쓴다 (2026-09-09). 8/29~31 에 테스트 47개로 고정된 것 —
        파일명+마커 점수, 테스트 감점, 라우트 객체 확인(@mock.patch 오탐 없음), api_route·methods=·websocket.
        survey 가 읽은 텍스트(utf-8-sig, BOM 없음)를 넘겨 디스크를 다시 읽지 않는다. 행 모양은 옛 키를 유지하고
        (path·line·symbol·registration·arguments·candidate — result.json 의 routes 계약) method·url·kind·test 를 더한다."""
        texts = {p: "\n".join(lines) for p, lines in self.sources.items()}
        paths = [self.root / p for p in self.files]
        self.entries = []
        for e in analysis.entry_points(self.root, paths, limit=8, texts=texts):
            lines = self.sources.get(e["path"], [])
            line = next((i for i, l in enumerate(lines, 1) if any(m in l for m in analysis.ENTRY_MARKERS)), 1)
            self.entries.append({"path": e["path"], "line": line, "reason": e["reason"], "score": e["score"],
                                 "test": is_test(e["path"])})
        for rel in self.files:
            if not rel.endswith(".py"):
                continue
            test = is_test(rel)
            for r in analysis.routes_of(self.root / rel, self.root, text=texts[rel]):
                self.interfaces.append({"path": rel, "line": r["line"], "symbol": r["handler"],
                                        "registration": f"{r['object']}.{r['decorator']}", "arguments": [r["path"]],
                                        "candidate": True, "kind": "http", "method": r["method"], "url": r["path"],
                                        "test": test})
        self.interfaces.sort(key=lambda r: (r["path"], r["line"]))
        top: dict[str, list[dict]] = {}
        for s in self.symbols:
            if "." not in s["symbol"]:
                top.setdefault(s["path"], []).append(s)
        for e in self.entries:
            e["symbols"] = top.get(e["path"], [])      # 최상위 def·class 전부 (상한 없음 — 본문은 render 가 자른다)

    def _config(self, rel: str, text: str):
        # Preserve line locations; these are command/dependency candidates, not execution.
        pattern = re.compile(r"scripts|entry_points|console_scripts|uvicorn|gunicorn|python\s|"
                             r"CMD|ENTRYPOINT|ExecStart|main\s*=|start\s*[\"':=]", re.I)
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                self.commands.append({"path": rel, "line": i, "text": line[:300]})
            if DEPENDENCY.search(line):
                self.dependencies.append({"path": rel, "line": i, "text": line[:200]})

    def _python(self, rel: str, text: str):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            self.limitations.append({"path": rel, "reason": "python_parse_failed"})
            return

        class Visitor(ast.NodeVisitor):
            def __init__(visitor):
                visitor.scope = []
            def visit_ClassDef(visitor, node):
                visitor.definition(node, "class")
            def visit_FunctionDef(visitor, node):
                visitor.definition(node, "function")
            visit_AsyncFunctionDef = visit_FunctionDef
            def definition(visitor, node, kind):
                symbol = ".".join(visitor.scope + [node.name])
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                src = self.sources[rel]
                # 여러 줄 선언은 본문 첫 줄 앞까지 이어 붙인다 (2026-09-09). 전에는 첫 줄만이라 `def pay(` 로 잘렸다.
                header_end = node.body[0].lineno - 1 if node.body and node.body[0].lineno > node.lineno else node.lineno
                signature = " ".join(l.strip() for l in src[node.lineno - 1:header_end])[:200]
                doc = (ast.get_docstring(node) or "").strip().splitlines()
                self.symbols.append({"path": rel, "symbol": symbol, "kind": kind,
                                     "line_start": start, "line_end": node.end_lineno,
                                     "signature": signature, "doc": doc[0][:120] if doc else ""})
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        expr = ast.unparse(dec.func)
                        args = [a.value for a in dec.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                        # HTTP 라우트(get/post/…/patch/route)는 analysis.routes_of 가 맡는다 — 라우트 객체를 확인하므로
                        # `@mock.patch(...)` 가 등록 구문으로 잡히지 않는다 (2026-09-09). 여기는 그 밖의 등록만.
                        if re.search(r"\.(command|task|subscribe)$", expr):
                            self.interfaces.append({"path": rel, "line": dec.lineno, "symbol": symbol,
                                                    "registration": expr, "arguments": args[:3], "candidate": True,
                                                    "kind": "command", "test": is_test(rel)})
                visitor.scope.append(node.name)
                visitor.generic_visit(node)
                visitor.scope.pop()
            def visit_Call(visitor, node):
                target = ast.unparse(node.func)
                self.connections.append({"path": rel, "line": node.lineno,
                                         "owner": ".".join(visitor.scope), "target": target, "kind": "call_candidate"})
                args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if re.search(r"(^|\.)include_router$", target):
                    # prefix 는 후보로만 — 라우트 url 과 결합하지 않는다 (analysis.router_prefixes 와 같은 규칙)
                    router = ast.unparse(node.args[0]) if node.args else next(
                        (ast.unparse(k.value) for k in node.keywords if k.arg == "router"), "?")
                    prefix = next((k.value.value if isinstance(k.value, ast.Constant) else ast.unparse(k.value)
                                   for k in node.keywords if k.arg == "prefix"), "")
                    self.interfaces.append({"path": rel, "line": node.lineno, "symbol": ".".join(visitor.scope),
                                            "registration": "include_router",
                                            "arguments": [router] + ([str(prefix)] if prefix else []), "candidate": True,
                                            "kind": "router", "router": router, "prefix": str(prefix), "test": is_test(rel)})
                elif re.search(r"add_parser|add_route|add_url_rule|subscribe|create_task", target):
                    self.interfaces.append({"path": rel, "line": node.lineno, "symbol": ".".join(visitor.scope),
                                            "registration": target, "arguments": args[:3], "candidate": True,
                                            "kind": "call", "test": is_test(rel)})
                visitor.generic_visit(node)
            def visit_Import(visitor, node):
                for item in node.names:
                    self.connections.append({"path": rel, "line": node.lineno, "target": item.name,
                                             "alias": item.asname or item.name, "kind": "import_candidate"})
                    if DEPENDENCY.search(item.name):
                        self.dependencies.append({"path": rel, "line": node.lineno, "text": item.name})
            def visit_ImportFrom(visitor, node):
                self.connections.append({"path": rel, "line": node.lineno,
                                         "target": "." * node.level + (node.module or ""),
                                         "names": [a.name for a in node.names], "kind": "import_candidate"})
        try:
            Visitor().visit(tree)
        except (RecursionError, ValueError):
            self.limitations.append({"path": rel, "reason": "ast_walk_incomplete"})

    def add(self, path: str, start: int = 1, end: int | None = None, *, section: str = "",
            max_tokens: int = 1100) -> dict | None:
        path = path.replace("\\", "/")
        lines = self.sources.get(path)
        if not lines or isinstance(start, bool) or not isinstance(start, int):
            return None
        if start < 1 or start > len(lines):
            return None
        end = min(end if isinstance(end, int) and not isinstance(end, bool) else start + 59, len(lines))
        if end < start:
            return None
        selected = []
        for line in lines[start - 1:end]:
            if tokens("\n".join(selected + [line])) > max_tokens:
                break
            selected.append(line)
        if not selected:
            self.limitations.append({"path": path, "line": start, "reason": "line_exceeds_evidence_budget"})
            return None
        actual_end = start + len(selected) - 1
        key = (path, start, actual_end)
        if key in self._evidence_keys:
            return self.evidence[self._evidence_keys[key] - 1]
        row = {"id": len(self.evidence) + 1, "path": path, "line_start": start,
               "line_end": actual_end, "text": "\n".join(selected), "section": section,
               "type": "doc" if self.files[path]["type"] == "doc" else "code", "score": 1.0,
               "source_sha256": self.files[path]["sha256"], "excerpt": actual_end < end}
        self.evidence.append(row)
        self._evidence_keys[key] = row["id"]
        return row

    def resolve_path(self, path) -> str | None:
        """모델이 적은 경로를 survey 의 실제 파일로 (2026-09-09). 구분자·`./`·앞 `/` 정리 → 정확 일치 → 유일한 접미사 일치
        (`main.py` ↔ `app/main.py`). 둘 이상 맞으면 추측하지 않고 None."""
        if not isinstance(path, str):
            return None
        p = path.strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if not p:
            return None
        if p in self.files:
            return p
        hits = [f for f in self.files if f.endswith("/" + p)]
        return hits[0] if len(hits) == 1 else None

    def sections(self) -> list[dict]:
        out = []
        for path, info in self.files.items():
            if info["type"] != "doc":
                continue
            lines = self.sources[path]
            starts = [(1, "intro")]
            fence = None
            for i, line in enumerate(lines, 1):
                m = re.match(r"\s*(`{3,}|~{3,})", line)
                if m:
                    if fence is None:
                        fence = m[1][0]
                    elif m[1][0] == fence:
                        fence = None
                if fence is None and re.match(r"^#{1,6}\s", line):
                    if i == 1:
                        starts = []
                    starts.append((i, line.lstrip("# ")))
            readme = Path(path).name.lower().startswith("readme")
            low_value = bool(LOW_VALUE_DOC.match(Path(path).name))
            for i, (start, heading) in enumerate(starts):
                end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
                if end < start:
                    continue
                pieces = [(start, end, heading)]
                if readme and i == 0 and end - start + 1 > INTRO_HEAD_LINES:
                    # README 첫 절(제목 아래 첫 문단)은 앞 조각만 먼저, 나머지는 다른 문서의 우선 절 뒤로 (2026-09-09)
                    pieces = [(start, start + INTRO_HEAD_LINES - 1, heading),
                              (start + INTRO_HEAD_LINES, end, heading + " (계속)")]
                for s, e, h in pieces:
                    prio = bool(SECTION_PRIORITY.search(h))
                    # 0: README 첫 조각·README 우선 절 / 1: 다른 문서 우선 절 / 2: README 나머지 / 3: 나머지.
                    # 헤딩 우선(긴 README 뒤쪽 Usage)과 파일 다양성(다른 문서의 제약) 둘 다 지킨다.
                    group = 0 if readme and (prio or (i == 0 and s == start)) else 1 if prio else 2 if readme else 3
                    if low_value:
                        group = 4                    # changelog·release-notes 는 절 제목과 무관하게 맨 뒤
                    out.append({"path": path, "start": s, "end": e, "heading": h, "priority": group})
        return sorted(out, key=lambda x: (x["priority"], x["path"], x["start"]))

    def candidates(self, queries: list[str], paths: list[str]) -> list[dict]:
        terms = set(re.findall(r"[\w]+", " ".join(queries).lower())) - {"the", "and", "코드", "기능"}
        # 행마다 origin 을 붙인다 (2026-09-09): path(지정 파일) · symbol(정의 이름) · call(호출 대상 이름) · line(본문 한 줄).
        # gather 는 앞 셋이 하나도 없으면 모델을 부르지 않는다 — line 만으로는 어느 파일이나 걸린다.
        ranked = []
        for row in self.symbols:
            hay = (row["path"] + " " + row["symbol"]).lower()
            matched = sum(3 for term in terms if len(term) > 1 and term in hay)
            score = 10 * (row["path"] in paths) + matched
            if score:
                ranked.append((score - int(is_test(row["path"])), {**row, "origin": "symbol" if matched else "path"}))
        for conn in self.connections:
            # `store.save()`·`user.has_permission()` 처럼 정의·경로가 아니라 호출로 드러나는 근거
            if conn.get("kind") != "call_candidate":
                continue
            name = conn["target"].rsplit(".", 1)[-1].lower()
            if any(len(t) > 2 and t in name for t in terms):
                ranked.append((5 - int(is_test(conn["path"])),
                               {"path": conn["path"], "line_start": max(1, conn["line"] - 5),
                                "line_end": conn["line"] + 30, "origin": "call"}))
        for path, lines in self.sources.items():
            # One matching region per file, plus explicit file start; full definitions win on score.
            if path in paths:
                ranked.append((8, {"path": path, "line_start": 1, "line_end": min(60, len(lines)), "origin": "path"}))
            for i, line in enumerate(lines, 1):
                if any(len(t) > 2 and t in line.lower() for t in terms):
                    ranked.append((1, {"path": path, "line_start": max(1, i - 5), "line_end": i + 30, "origin": "line"}))
                    break
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["path"], pair[1]["line_start"]))
        out, counts = [], Counter()
        for _, row in ranked:
            if counts[row["path"]] >= 2:
                continue
            counts[row["path"]] += 1
            out.append(row)
            if len(out) >= 12:
                break
        return out

    def summary(self) -> dict:
        dirs = Counter(str(Path(p).parent).replace("\\", "/") for p in self.files)
        return {"name": self.root.name, "total_files": len(self.files), "total_dirs": len(dirs),
                "key_dirs": [{"path": p, "file_count": n} for p, n in dirs.most_common(15)],
                "entry_points": self.entries, "docs": [p for p, f in self.files.items() if f["type"] == "doc"],
                "configs": [p for p, f in self.files.items() if f["type"] == "config"],
                "ext_counts": dict(Counter(Path(p).suffix for p in self.files)),
                "interfaces": self.interfaces, "commands": self.commands, "dependencies": self.dependencies,
                "excluded": self.excluded, "limitations": self.limitations, "version": self.state,
                "source_digest": self.source_digest}

    def changed_paths(self) -> list[str]:
        """읽을 때와 지금이 다른 파일(내용 변경·삭제). git 상태(commit·dirty)가 바뀌었으면 "<git>" 을 더한다. 비면 그대로다."""
        out = []
        for path, info in self.files.items():
            try:
                if digest((self.root / path).read_bytes()) != info["sha256"]:
                    out.append(path)
            except OSError:
                out.append(path)
        if git_state(self.root) != self.state:
            out.append("<git>")
        return out

    def unchanged(self) -> bool:
        return not self.changed_paths()
