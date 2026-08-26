"""
결정적 분석 — LLM 없이 레포에서 뽑는 사실: 진입점 · 함수 헤더 · 라우트 표 · import 그래프(Mermaid) · 문서 목록.

브리핑의 "진입점 목록 / 진입점별 함수 목록 / 아키텍처 구조도"는 전부 여기서 나옵니다.
LLM 은 요약(문서 요약·기능 목록·한 줄 정의)만 맡습니다. 그래야 README 가 부실한 레포에서도 형태가 유지됩니다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .chunker import collect_files, python_nodes, read_text
from .config import DOC_EXT

ENTRY_NAMES = {
    "main.py", "app.py", "__main__.py", "manage.py", "wsgi.py", "asgi.py", "server.py", "run.py", "cli.py",
    "index.js", "index.ts", "main.js", "main.ts", "server.js", "server.ts", "app.js", "app.ts", "index.tsx", "App.tsx",
    "Main.java", "Application.java", "main.go", "main.rs", "Program.cs",
}
ENTRY_MARKERS = [
    'if __name__ == "__main__"', "if __name__ == '__main__'", "def main(", "func main(",
    "public static void main", "createApp", "create_app", "FastAPI(", "Flask(", "express(", "app.listen(",
    "typer.Typer(", "click.group(", "argparse.ArgumentParser(",
]
README_NAMES = ["README.md", "readme.md", "README.MD", "README.rst", "README.txt", "README"]
CONFIG_NAMES = ["pyproject.toml", "package.json", "setup.py", "requirements.txt", "go.mod", "Cargo.toml",
                "pom.xml", "build.gradle", "composer.json", "Gemfile", "tsconfig.json"]

_ROUTE_DEC = re.compile(
    r"^(?P<obj>[A-Za-z_][\w.]*)\.(?P<method>get|post|put|delete|patch|options|head|route|websocket|api_route)"
    r"\(\s*(?P<q>['\"])(?P<path>[^'\"]*)(?P=q)")
_ROUTE_METHODS = re.compile(r"methods\s*=\s*\[([^\]]*)\]")
_INCLUDE_ROUTER = re.compile(r"include_router\(\s*(?P<name>[A-Za-z_][\w.]*)(?:[^)]*prefix\s*=\s*['\"](?P<prefix>[^'\"]*)['\"])?")


def walk(root: Path, profile=None) -> tuple[list[Path], dict[str, int]]:
    files = collect_files(root, profile)
    dir_counts: dict[str, int] = {}
    for f in files:
        d = f.relative_to(root).parent.as_posix()
        dir_counts[d] = dir_counts.get(d, 0) + 1
    return files, dir_counts


def find_readme(root: Path) -> Path | None:
    for name in README_NAMES:
        p = root / name
        if p.is_file():
            return p
    return None


def doc_files(root: Path, files: list[Path], *, limit: int = 12) -> list[Path]:
    """마크다운·문서 파일을 우선순위(README → docs/ → 최상위 → 나머지)로 정렬."""
    docs = [f for f in files if f.suffix.lower() in DOC_EXT]

    def rank(p: Path):
        rel = p.relative_to(root).as_posix()
        name = p.name.lower()
        if name.startswith("readme"):
            return (0, rel)
        if rel.startswith("docs/") or rel.startswith("doc/"):
            return (1, rel)
        if "/" not in rel:
            return (2, rel)
        if name in ("changelog.md", "release-notes.md", "license.md", "license"):
            return (9, rel)
        return (3, rel)

    return sorted(docs, key=rank)[:limit]


def entry_points(root: Path, files: list[Path], *, limit: int = 6) -> list[dict]:
    cands = []
    for f in files:
        score, reasons = 0, []
        if f.name in ENTRY_NAMES:
            score += 10
            reasons.append(f"파일명 규칙({f.name})")
        depth = len(f.relative_to(root).parts) - 1
        if depth <= 1:
            score += 3
            reasons.append("최상위 근처")
        if score == 0 and f.suffix.lower() not in (".py", ".js", ".ts", ".go", ".rs", ".java"):
            continue
        head = (read_text(f) or "")[:6000]
        for m in ENTRY_MARKERS:
            if m in head:
                score += 8
                reasons.append(f"'{m.strip('(')}' 포함")
                break
        if score >= 8:
            cands.append({"path": f.relative_to(root).as_posix(), "reason": " · ".join(reasons), "score": score})
    cands.sort(key=lambda x: (-x["score"], x["path"]))
    return cands[:limit]


def symbols_of(path: Path, root: Path) -> list[dict]:
    """Python 파일의 함수·클래스·메서드 헤더. 실패하면 빈 목록."""
    if path.suffix.lower() != ".py":
        return []
    text = read_text(path)
    if not text:
        return []
    try:
        nodes = python_nodes(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    out = []
    for n in nodes:
        if n["kind"] in ("function", "method", "class"):
            out.append({"kind": n["kind"], "symbol": n["symbol"], "signature": n["signature"],
                        "line_start": n["line_start"], "line_end": n["line_end"], "doc": n["doc"],
                        "decorators": n.get("decorators", [])})
    return out


def routes_of(path: Path, root: Path) -> list[dict]:
    """FastAPI/Flask 데코레이터에서 (method, path, handler) 추출. 정규식 근사."""
    text = read_text(path)
    if not text or path.suffix.lower() != ".py":
        return []
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("@"):
            continue
        m = _ROUTE_DEC.match(s[1:])
        if not m:
            continue
        method = m.group("method").upper()
        if method == "ROUTE":
            mm = _ROUTE_METHODS.search(s)
            method = ",".join(x.strip(" '\"") for x in mm.group(1).split(",")) if mm else "GET"
        if method == "API_ROUTE":
            mm = _ROUTE_METHODS.search(s)
            method = ",".join(x.strip(" '\"") for x in mm.group(1).split(",")) if mm else "ANY"
        handler = None
        for j in range(i + 1, min(i + 6, len(lines))):
            t = lines[j].strip()
            fm = re.match(r"(?:async\s+)?def\s+([A-Za-z_]\w*)", t)
            if fm:
                handler = fm.group(1)
                break
        out.append({"method": method, "path": m.group("path"), "handler": handler,
                    "object": m.group("obj"), "line": i + 1})
    return out


def router_prefixes(path: Path) -> list[dict]:
    text = read_text(path) or ""
    return [{"router": m.group("name"), "prefix": m.group("prefix") or ""} for m in _INCLUDE_ROUTER.finditer(text)]


def all_routes(root: Path, files: list[Path]) -> list[dict]:
    out = []
    for f in files:
        if f.suffix.lower() != ".py":
            continue
        for r in routes_of(f, root):
            r["file"] = f.relative_to(root).as_posix()
            out.append(r)
    return out


# ── import 그래프 → Mermaid ──────────────────────────────────

def _module_name(rel: str) -> str:
    return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")


def import_graph(root: Path, files: list[Path], *, level: int = 2, max_nodes: int = 25) -> dict:
    """프로젝트 내부 import 만 모아 상위 `level` 단계 패키지 단위로 접습니다."""
    py = [f for f in files if f.suffix.lower() == ".py"]
    mods = {_module_name(f.relative_to(root).as_posix()) for f in py}
    pkg_roots = {m.split(".")[0] for m in mods}
    edges: dict[tuple[str, str], int] = {}

    def fold(mod: str) -> str:
        parts = mod.split(".")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts[:level]) if parts else mod

    for f in py:
        rel = f.relative_to(root).as_posix()
        src = read_text(f)
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except Exception:
            continue
        here = fold(_module_name(rel))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    base = _module_name(rel).split(".")[:-node.level]
                    targets = [".".join(base + ([node.module] if node.module else []))]
                elif node.module:
                    targets = [node.module]
            for t in targets:
                if t.split(".")[0] not in pkg_roots:
                    continue
                there = fold(t)
                if there and there != here:
                    edges[(here, there)] = edges.get((here, there), 0) + 1
    nodes: dict[str, int] = {}
    for (a, b), w in edges.items():
        nodes[a] = nodes.get(a, 0) + w
        nodes[b] = nodes.get(b, 0) + w
    keep = set(sorted(nodes, key=lambda n: -nodes[n])[:max_nodes])
    kept_edges = {k: w for k, w in edges.items() if k[0] in keep and k[1] in keep}
    return {"nodes": sorted(keep), "edges": [{"from": a, "to": b, "weight": w} for (a, b), w in sorted(kept_edges.items())]}


def mermaid(graph: dict) -> str:
    if not graph.get("edges"):
        return ""
    ids = {n: f"n{i}" for i, n in enumerate(graph["nodes"])}
    lines = ["graph LR"]
    for n, i in ids.items():
        lines.append(f'  {i}["{n}"]')
    for e in graph["edges"]:
        lines.append(f"  {ids[e['from']]} --> {ids[e['to']]}")
    return "\n".join(lines)


def analyze(project_root: str | Path, profile=None) -> dict:
    root = Path(project_root).resolve()
    files, dir_counts = walk(root, profile)
    entries = entry_points(root, files)
    for e in entries:
        p = root / e["path"]
        e["symbols"] = symbols_of(p, root)
        e["routes"] = routes_of(p, root)
        e["routers"] = router_prefixes(p)
    ext_counts: dict[str, int] = {}
    for f in files:
        ext_counts[f.suffix.lower()] = ext_counts.get(f.suffix.lower(), 0) + 1
    graph = import_graph(root, files)
    return {
        "name": root.name,
        "total_files": len(files),
        "total_dirs": len(dir_counts),
        "ext_counts": dict(sorted(ext_counts.items(), key=lambda x: -x[1])[:8]),
        "key_dirs": sorted(({"path": d, "file_count": n} for d, n in dir_counts.items() if d != "."),
                           key=lambda x: -x["file_count"])[:15],
        "readme": find_readme(root).relative_to(root).as_posix() if find_readme(root) else None,
        "docs": [d.relative_to(root).as_posix() for d in doc_files(root, files)],
        "configs": [n for n in CONFIG_NAMES if (root / n).is_file()],
        "entry_points": entries,
        "routes": all_routes(root, files),
        "import_graph": graph,
        "mermaid": mermaid(graph),
    }
