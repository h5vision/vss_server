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

from .config import CFG, CODE_EXT, DOC_EXT, SKIP_DIRS, SKIP_FILE_PATTERNS, is_excluded

CONFIG_NAMES = {"pyproject.toml", "package.json", "setup.py", "setup.cfg", "requirements.txt",
                "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "composer.json",
                "Dockerfile", "docker-compose.yml", "compose.yaml", "Makefile", "Procfile"}
ENTRY_NAMES = {"main.py", "app.py", "server.py", "cli.py", "__main__.py", "manage.py",
               "main.ts", "index.ts", "main.js", "index.js", "main.go", "main.rs", "Program.cs"}
SECTION_PRIORITY = re.compile(r"install|setup|usage|run|config|architect|overview|feature|limit|"
                              r"실행|설치|사용|설정|구조|개요|기능|제약", re.I)
DEPENDENCY = re.compile(r"sql|database|redis|mongo|postgres|sqlite|http|requests|urllib|queue|"
                        r"kafka|storage|chroma|boto|socket", re.I)


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


def tokens(text: str) -> int:
    """Conservative estimate, NOT a tokenizer: ASCII / 2 and other characters * 2.

    All requests record estimated vs Ollama actual counts. A matching tokenizer
    is not assumed to be installed on an offline deployment.
    """
    ascii_count = sum(ord(c) < 128 for c in text)
    return (ascii_count + 1) // 2 + (len(text) - ascii_count) * 2


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
        self.source_digest = digest({p: f["sha256"] for p, f in self.files.items()})

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
                if name in ENTRY_NAMES and kind != "test":
                    self.entries.append({"path": rel, "line": 1, "reason": "filename_candidate"})
                if config:
                    self._config(rel, text)
                if suffix == ".py":
                    self._python(rel, text)
                elif kind in ("code", "test"):
                    self.limitations.append({"path": rel, "reason": "text_only_language"})
        self.excluded = dict(excluded)

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
                self.symbols.append({"path": rel, "symbol": symbol, "kind": kind,
                                     "line_start": start, "line_end": node.end_lineno,
                                     "signature": self.sources[rel][node.lineno - 1].strip()})
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        expr = ast.unparse(dec.func)
                        args = [a.value for a in dec.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                        if re.search(r"\.(get|post|put|delete|patch|route|command|task|subscribe)$", expr):
                            self.interfaces.append({"path": rel, "line": dec.lineno, "symbol": symbol,
                                                    "registration": expr, "arguments": args[:3], "candidate": True})
                visitor.scope.append(node.name)
                visitor.generic_visit(node)
                visitor.scope.pop()
            def visit_Call(visitor, node):
                target = ast.unparse(node.func)
                self.connections.append({"path": rel, "line": node.lineno,
                                         "owner": ".".join(visitor.scope), "target": target, "kind": "call_candidate"})
                if re.search(r"add_parser|include_router|add_route|add_url_rule|subscribe|create_task", target):
                    self.interfaces.append({"path": rel, "line": node.lineno, "symbol": ".".join(visitor.scope),
                                            "registration": target, "arguments": [], "candidate": True})
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
            for i, (start, heading) in enumerate(starts):
                end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
                if end >= start:
                    out.append({"path": path, "start": start, "end": end, "heading": heading,
                                "priority": (0 if SECTION_PRIORITY.search(heading) else 1,
                                             0 if Path(path).name.lower().startswith("readme") else 1)})
        return sorted(out, key=lambda x: (x["priority"], x["path"], x["start"]))

    def candidates(self, queries: list[str], paths: list[str]) -> list[dict]:
        terms = set(re.findall(r"[\w]+", " ".join(queries).lower())) - {"the", "and", "코드", "기능"}
        ranked = []
        for row in self.symbols:
            hay = (row["path"] + " " + row["symbol"]).lower()
            score = 10 * (row["path"] in paths) + sum(3 for term in terms if len(term) > 1 and term in hay)
            if score:
                ranked.append((score - int(is_test(row["path"])), row))
        for path, lines in self.sources.items():
            # One matching region per file, plus explicit file start; full definitions win on score.
            if path in paths:
                ranked.append((8, {"path": path, "line_start": 1, "line_end": min(60, len(lines))}))
            for i, line in enumerate(lines, 1):
                if any(len(t) > 2 and t in line.lower() for t in terms):
                    ranked.append((1, {"path": path, "line_start": max(1, i - 5), "line_end": i + 30}))
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

    def unchanged(self) -> bool:
        for path, info in self.files.items():
            try:
                if digest((self.root / path).read_bytes()) != info["sha256"]:
                    return False
            except OSError:
                return False
        return git_state(self.root) == self.state
