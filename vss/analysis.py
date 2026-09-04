"""
결정적 분석 — LLM 없이 레포에서 뽑는 사실: 진입점 · 함수 헤더 · 라우트 표 · 문서 목록.

브리핑의 "진입점 목록 / 진입점별 함수 목록 / 아키텍처 구조도"는 전부 여기서 나옵니다.
LLM 은 요약(문서 요약·기능 목록·한 줄 정의)만 맡습니다. 그래야 README 가 부실한 레포에서도 형태가 유지됩니다.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

from .chunker import collect_files, python_nodes, read_text
from .config import DOC_EXT

ENTRY_NAMES = {
    "main.py", "app.py", "__main__.py", "manage.py", "wsgi.py", "asgi.py", "server.py", "run.py", "cli.py",
    "index.js", "index.ts", "main.js", "main.ts", "server.js", "server.ts", "app.js", "app.ts", "index.tsx", "App.tsx",
    "Main.java", "Application.java", "main.go", "main.rs", "Program.cs",
}
ENTRY_MARKERS = [
    # 문자열 리터럴은 스캔 전에 지워지므로 "__main__" 부분은 마커에 넣지 않는다 (두 따옴표 변형을 하나로 커버)
    "if __name__ ==", "def main(", "func main(",
    "public static void main", "createApp(", "create_app(", "FastAPI(", "Flask(", "express(", "app.listen(",
    "typer.Typer(", "click.group(", "argparse.ArgumentParser(",
]
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs"}
README_NAMES = ["README.md", "readme.md", "README.MD", "README.rst", "README.txt", "README"]
CONFIG_NAMES = ["pyproject.toml", "package.json", "setup.py", "requirements.txt", "go.mod", "Cargo.toml",
                "pom.xml", "build.gradle", "composer.json", "Gemfile", "tsconfig.json"]

_ROUTE_METHOD_NAMES = {"get", "post", "put", "delete", "patch", "options", "head", "trace",
                       "route", "websocket", "websocket_route", "api_route"}


def _strip_py_literals(text: str) -> str:
    """문자열 리터럴·주석 토큰을 공백으로 지운 사본. docstring 예시 코드가 마커·라우트로 오탐되는 것을 막는다.
    tokenize 가 실패하면(문법 깨진 파일) 원문을 그대로 돌려준다."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except Exception:
        return text
    drop = {tokenize.STRING, tokenize.COMMENT}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        t = getattr(tokenize, name, None)
        if t is not None:
            drop.add(t)
    lines = text.splitlines()
    for tok in toks:
        if tok.type not in drop:
            continue
        (r1, c1), (r2, c2) = tok.start, tok.end
        if r1 == r2:
            line = lines[r1 - 1]
            lines[r1 - 1] = line[:c1] + " " * (c2 - c1) + line[c2:]
        else:
            lines[r1 - 1] = lines[r1 - 1][:c1]
            for r in range(r1, r2 - 1):
                lines[r] = ""
            lines[r2 - 1] = " " * c2 + lines[r2 - 1][c2:]
    return "\n".join(lines)


def _dec_path(call: ast.Call) -> str | None:
    """라우트 데코레이터 호출에서 경로 인자(첫 위치 인자 또는 path=/rule=). f-string 은 리터럴+{식} 근사."""
    node = call.args[0] if call.args else None
    if node is None:
        for kw in call.keywords:
            if kw.arg in ("path", "rule"):
                node = kw.value
                break
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                try:
                    parts.append("{" + ast.unparse(v.value) + "}")
                except Exception:
                    parts.append("{?}")
        return "".join(parts)
    return None


def _dec_methods(call: ast.Call) -> str | None:
    """methods= 값을 사람이 읽을 수 있는 문자열로 보존합니다.

    정적 문자열은 대문자로 정규화하고, 동적 요소는 일부만 버리지 않고 식 원문을 남깁니다.
    """
    for kw in call.keywords:
        if kw.arg != "methods":
            continue
        if isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
            vals = []
            for elem in kw.value.elts:
                if isinstance(elem, ast.Constant) and isinstance(elem.value, str):
                    vals.append(elem.value.upper())
                else:
                    try:
                        vals.append(ast.unparse(elem))
                    except Exception:
                        vals.append("?")
            if vals:
                return ",".join(vals)
            return None                 # methods=[] — 명시된 method 가 없으니 기본값 폴백으로
        try:
            return ast.unparse(kw.value)
        except Exception:
            return "?"
    return None


_ROUTE_CONSTRUCTORS = {"FastAPI", "Flask", "APIRouter", "Blueprint"}
_ROUTE_OWNER_NAMES = {"app", "api", "router", "bp", "blueprint"}
_ROUTE_OWNER_SUFFIXES = ("_app", "_api", "_router", "_bp", "_blueprint")
_NON_ROUTE_MODULES = {"pook", "pytest", "mock", "unittest.mock", "responses"}


def _expr_source(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def _known_route_objects(tree: ast.AST) -> dict[tuple, dict[str, str]]:
    """프레임워크 라우트 객체와 명시적인 테스트/mock 객체 이름을 scope 별로 모읍니다.

    반환은 {scope: {이름: "owner" | "rejected"}}. scope 는 모듈이 빈 튜플이고
    함수 안은 바깥부터 쌓인 함수 노드 id 튜플이다. 다른 함수 안의 대입이
    모듈 레벨 라우트 객체를 지우지 않도록 판정은 scope 체인으로만 한다.
    같은 scope 에서 양쪽으로 대입된 이름은 rejected 로 남는다(보수적)."""
    constructor_names = set(_ROUTE_CONSTRUCTORS)
    non_route_bindings = set(_NON_ROUTE_MODULES)
    scopes: dict[tuple, dict[str, str]] = {(): {}}

    def bind(scope: tuple, name: str, verdict: str) -> None:
        table = scopes.setdefault(scope, {})
        if table.get(name) != "rejected":
            table[name] = verdict

    def visit_imports(container: ast.AST, scope: tuple) -> None:
        for node in ast.iter_child_nodes(container):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit_imports(node, scope + (id(node),))
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if alias.name in _NON_ROUTE_MODULES or root in _NON_ROUTE_MODULES:
                        bound = alias.asname or root
                        non_route_bindings.add(bound)
                        bind(scope, bound, "rejected")
                    else:
                        # import myapp.api as r — 모듈 끝 이름이 라우터처럼 보이면 별칭도 owner 후보
                        tail = alias.name.rsplit(".", 1)[-1].lower()
                        if tail in _ROUTE_OWNER_NAMES or tail.endswith(_ROUTE_OWNER_SUFFIXES):
                            bind(scope, alias.asname or alias.name, "owner")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                is_non_route = module in _NON_ROUTE_MODULES or module.split(".", 1)[0] in _NON_ROUTE_MODULES
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if alias.name in _ROUTE_CONSTRUCTORS:
                        constructor_names.add(bound)
                    low = alias.name.lower()
                    if not is_non_route and (low in _ROUTE_OWNER_NAMES or low.endswith(_ROUTE_OWNER_SUFFIXES)):
                        bind(scope, bound, "owner")
                    if is_non_route:
                        non_route_bindings.add(bound)
                        bind(scope, bound, "rejected")
            else:
                visit_imports(node, scope)

    def add_target(target: ast.AST, scope: tuple, verdict: str, prefix: str = "") -> None:
        if isinstance(target, (ast.Name, ast.Attribute)):
            bind(scope, prefix + _expr_source(target), verdict)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elem in target.elts:
                add_target(elem, scope, verdict, prefix)

    def visit_assigns(container: ast.AST, scope: tuple, class_prefix: str = "") -> None:
        for node in ast.iter_child_nodes(container):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit_assigns(node, scope + (id(node),))
                continue
            if isinstance(node, ast.ClassDef):
                # 클래스 속성 라우터는 @Holder.r.get 처럼 qualified 로도 참조된다
                visit_assigns(node, scope, f"{class_prefix}{node.name}.")
                continue
            targets: list[ast.AST] = []
            value = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            if isinstance(value, ast.Call):
                fn = value.func
                constructor = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                if constructor in constructor_names:
                    for target in targets:
                        add_target(target, scope, "owner")
                        if class_prefix:
                            add_target(target, scope, "owner", prefix=class_prefix)
                else:
                    fn_source = _expr_source(fn)
                    root = fn_source.split(".", 1)[0]
                    if root in non_route_bindings or fn_source in non_route_bindings:
                        for target in targets:
                            add_target(target, scope, "rejected")
                            if class_prefix:
                                add_target(target, scope, "rejected", prefix=class_prefix)
            visit_assigns(node, scope, class_prefix)

    visit_imports(tree, ())
    visit_assigns(tree, ())
    return scopes


def _is_route_object(node: ast.AST, scope: tuple, scopes: dict[tuple, dict[str, str]]) -> bool:
    source = _expr_source(node)
    for depth in range(len(scope), -1, -1):
        verdict = scopes.get(scope[:depth], {}).get(source)
        if verdict is not None:
            return verdict == "owner"
    leaf = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
    low = leaf.lower()
    if low.startswith(("mock_", "fake_", "stub_", "dummy_")):
        return False                    # fixture 인자 등 대입 근거 없는 mock 이름은 휴리스틱에서 제외
    return low in _ROUTE_OWNER_NAMES or low.endswith(_ROUTE_OWNER_SUFFIXES)


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


def _is_test_file(rel: Path) -> bool:
    dirs = {part.lower() for part in rel.parts[:-1]}
    name = rel.name.lower()
    stem = rel.stem.lower()
    return ("tests" in dirs or "test" in dirs
            or name.startswith(("test_", "conftest."))
            or stem.endswith(("_test", ".test", ".spec")))


def entry_points(root: Path, files: list[Path], *, limit: int = 6) -> list[dict]:
    cands = []
    for f in files:
        if f.suffix.lower() not in CODE_EXT:        # 문서 파일(README 등)은 마커가 있어도 진입점이 아니다
            continue
        rel = f.relative_to(root)
        score, reasons = 0, []
        if f.name in ENTRY_NAMES:
            score += 10
            reasons.append(f"파일명 규칙({f.name})")
        depth = len(rel.parts) - 1
        if depth <= 1:
            score += 3
            reasons.append("최상위 근처")
        if _is_test_file(rel):
            # 테스트 파일의 if __name__ 꼬리는 애플리케이션 진입점이 아니다 — 마커 가점만큼 감점
            score -= 8
            reasons.append("테스트 파일 감점")
        text = read_text(f) or ""
        # .py 는 문자열·주석을 지운 사본에서 스캔 — docstring/주석 속 'FastAPI(' 오탐 방지. 전문 스캔이라 6000자 컷 없음
        scan = _strip_py_literals(text) if f.suffix.lower() == ".py" else text
        matched = [m for m in ENTRY_MARKERS if m in scan]
        if matched:
            score += 8
            reasons.append("'" + " · ".join(m.strip("(").strip() for m in matched) + "' 포함")
        if score >= 8:
            cands.append({"path": rel.as_posix(), "reason": " · ".join(reasons), "score": score})
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
    """FastAPI/Flask 데코레이터에서 (method, path, handler) 추출. AST 기반 —
    docstring·주석·여러 줄 데코레이터에 강하고, handler 는 데코레이터가 붙은 def 이름 그대로다.
    경로가 '/' 로 시작하지 않는 데코레이터(@mock.patch 등)는 라우트가 아니다."""
    text = read_text(path)
    if not text or path.suffix.lower() != ".py":
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    scopes = _known_route_objects(tree)
    out = []

    def visit(container: ast.AST, scope: tuple) -> None:
        for node in ast.iter_child_nodes(container):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(node, scope)
                continue
            # 데코레이터 식은 함수 자신이 아니라 그것을 둘러싼 scope 에서 평가된다
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                name = dec.func.attr
                if name not in _ROUTE_METHOD_NAMES:
                    continue
                if not _is_route_object(dec.func.value, scope, scopes):
                    continue
                p = _dec_path(dec)
                # 빈 문자열은 prefix 라우터의 root(@router.get(""))라 라우트다. '/' 미시작만 배제(@mock.patch 등)
                if p is None or (p != "" and not p.startswith("/")):
                    continue
                if name == "route":
                    method = _dec_methods(dec) or "GET"
                elif name == "api_route":
                    method = _dec_methods(dec) or "GET"
                elif name in ("websocket", "websocket_route"):
                    method = "WEBSOCKET"
                else:
                    method = name.upper()
                obj = _expr_source(dec.func.value)
                out.append({"method": method, "path": p, "handler": node.name, "object": obj, "line": dec.lineno})
            visit(node, scope + (id(node),))

    visit(tree, ())
    out.sort(key=lambda r: r["line"])
    return out


def router_prefixes(path: Path) -> list[dict]:
    """실제로 호출되는 include_router(...) 만 AST 로 수집 — 주석·docstring 속 예시는 제외.
    prefix 가 리터럴이 아니면(변수·설정값) 그 식을 원문 그대로 남긴다 (조용히 버리지 않는다)."""
    text = read_text(path) or ""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not ((isinstance(fn, ast.Attribute) and fn.attr == "include_router")
                or (isinstance(fn, ast.Name) and fn.id == "include_router")):
            continue
        rarg = node.args[0] if node.args else None
        if rarg is None:
            for kw in node.keywords:
                if kw.arg == "router":
                    rarg = kw.value
                    break
        if rarg is None:
            continue
        try:
            rname = ast.unparse(rarg)
        except Exception:
            rname = "?"
        prefix = ""
        for kw in node.keywords:
            if kw.arg == "prefix":
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    prefix = kw.value.value
                else:
                    try:
                        prefix = ast.unparse(kw.value)
                    except Exception:
                        prefix = ""
        out.append({"router": rname, "prefix": prefix})
    return out


def all_routes(root: Path, files: list[Path]) -> list[dict]:
    out = []
    for f in files:
        if f.suffix.lower() != ".py":
            continue
        for r in routes_of(f, root):
            r["file"] = f.relative_to(root).as_posix()
            out.append(r)
    return out


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
    }
