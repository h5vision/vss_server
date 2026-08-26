"""
청킹 — 검색 품질을 결정하는 최대 변수.

  code (.py)   AST 단위: 모듈 docstring · 모듈/클래스 상수 · 함수 · 메서드 하나가 청크 하나 (spike v0.5 이식)
               ast_max_chars 를 넘으면 줄 경계를 지키며 분할하고 [part i/n] 을 붙입니다.
  code (기타)  줄 윈도우 + 오버랩 (rag_lab 방식 유지)
  doc  (.md)   헤딩 섹션 단위. fenced code block(```) 안의 `#` 은 헤딩으로 보지 않습니다 (CHUNKING_AUDIT §1 결함 수정)

⚠ 산출 레코드 필드명은 고정입니다 (프론트·평가·BM25가 의존):
   type · path · line_start · line_end · section · enclosing · symbol · text · chunk_index
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Mapping

from . import context_header
from .config import (CODE_EXT, DOC_EXT, SKIP_DIRS, SKIP_FILE_PATTERNS,
                     is_excluded, profile_value)

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def classify(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in DOC_EXT:
        return "doc"
    if ext in CODE_EXT:
        return "code"
    return None


def collect_files(root: str | Path, profile: Mapping | None = None) -> list[Path]:
    """인덱싱 대상 파일 목록. 제외 순서: SKIP_DIRS → SKIP_FILE_PATTERNS → exclude_globs → 확장자 → 크기."""
    root = Path(root).resolve()
    spec = str(profile_value(profile, "exclude_globs") or "")
    max_bytes = int(profile_value(profile, "max_file_bytes"))
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if p.name in SKIP_FILE_PATTERNS:
            continue
        if is_excluded(p.relative_to(root).as_posix(), spec):
            continue
        if classify(p) is None:
            continue
        try:
            if p.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out)


def read_text(path: Path) -> str | None:
    """UTF-8 우선, 실패하면 관대하게. 바이너리로 보이면 None."""
    for enc in ("utf-8", "utf-8-sig", "cp949", "latin-1"):
        try:
            text = path.read_text(encoding=enc)
        except (UnicodeDecodeError, OSError):
            continue
        if "\x00" in text[:4096]:
            return None
        return text
    return None


# ── 긴 조각 분할 (줄 경계 우선) ──────────────────────────────

def split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[tuple[str, int, int]]:
    """(조각, 시작 문자 위치, 끝 문자 위치) 목록."""
    if len(text) <= max_chars:
        return [(text, 0, len(text))]
    pieces = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            newline = text.rfind("\n", start + max_chars // 2, end)
            if newline > start:
                end = newline + 1
        pieces.append((text[start:end], start, end))
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap_chars)
        newline = text.find("\n", next_start, end)
        start = newline + 1 if newline >= 0 else next_start
    return pieces


# ── 줄 윈도우 (Python 이외 코드 · AST 실패 시 폴백) ────────────

def chunk_code_lines(text: str, rel_path: str, profile: Mapping | None = None) -> list[dict]:
    size_limit = int(profile_value(profile, "chunk_size"))
    overlap = int(profile_value(profile, "chunk_overlap"))
    min_chars = int(profile_value(profile, "min_chunk_chars"))
    use_header = bool(profile_value(profile, "context_header"))
    lines = text.splitlines()
    chunks: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        buf: list[str] = []
        size = 0
        j = i
        while j < n and size < size_limit:
            buf.append(lines[j])
            size += len(lines[j]) + 1
            j += 1
        body = "\n".join(buf).strip()
        if len(body) >= min_chars:
            enclosing = context_header.find_enclosing(lines, i) if use_header else []
            if use_header:
                body = context_header.apply(body, context_header.build(rel_path, "code", enclosing=enclosing))
            chunks.append({
                "type": "code", "path": rel_path,
                "line_start": i + 1, "line_end": j,
                "section": None, "enclosing": enclosing or None,
                "symbol": enclosing[-1].split(" ", 1)[-1] if enclosing else None,
                "text": body,
            })
        if j >= n:
            break
        avg = max(1, size // max(1, (j - i)))
        back = max(1, overlap // avg)
        i = max(i + 1, j - back)
    return chunks


# ── Python AST 청커 (spike v0.5 이식) ─────────────────────────

def _docstring_node(body):
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0]
    return None


def _target_names(target) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out = []
        for e in target.elts:
            out.extend(_target_names(e))
        return out
    return []


def _assignment_names(node) -> list[str]:
    if isinstance(node, ast.Assign):
        out = []
        for t in node.targets:
            out.extend(_target_names(t))
        return out
    if isinstance(node, ast.AnnAssign):
        return _target_names(node.target)
    return []


def python_nodes(source: str) -> list[dict]:
    """모듈 최상위·클래스 직계의 의미 단위 목록. 브리핑(함수 헤더 목록)과 청커가 같이 씁니다.

    각 항목: kind(module_doc|assign|function|class_doc|method|class_assign) · symbol · enclosing(list)
             · line_start · line_end · signature · doc(첫 줄)
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    out: list[dict] = []

    def sig_of(node) -> str:
        try:
            args = ast.unparse(node.args)
        except Exception:
            args = "..."
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        ret = ""
        if getattr(node, "returns", None) is not None:
            try:
                ret = " -> " + ast.unparse(node.returns)
            except Exception:
                ret = ""
        return f"{prefix} {node.name}({args}){ret}"

    def first_doc(node) -> str | None:
        d = ast.get_docstring(node, clean=True)
        return d.strip().splitlines()[0] if d else None

    def decorators(node) -> list[str]:
        out_d = []
        for d in getattr(node, "decorator_list", []):
            try:
                out_d.append(ast.unparse(d))
            except Exception:
                pass
        return out_d

    md = _docstring_node(tree.body)
    if md:
        out.append({"kind": "module_doc", "symbol": "(module docstring)", "enclosing": [],
                    "line_start": md.lineno, "line_end": md.end_lineno or md.lineno,
                    "signature": None, "doc": None, "decorators": []})

    def add_callable(node, enclosing: list[str]):
        dec_lines = [d.lineno for d in getattr(node, "decorator_list", [])]
        start = min([node.lineno, *dec_lines])
        out.append({
            "kind": "method" if enclosing else "function",
            "symbol": ".".join(enclosing + [node.name]),
            "enclosing": list(enclosing),
            "line_start": start, "line_end": node.end_lineno or node.lineno,
            "signature": sig_of(node), "doc": first_doc(node),
            "decorators": decorators(node),
        })

    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = _assignment_names(node)
            if names:
                out.append({"kind": "assign", "symbol": ", ".join(names), "enclosing": [],
                            "line_start": node.lineno, "line_end": node.end_lineno or node.lineno,
                            "signature": None, "doc": None, "decorators": []})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_callable(node, [])
        elif isinstance(node, ast.ClassDef):
            cd = _docstring_node(node.body)
            out.append({"kind": "class", "symbol": node.name, "enclosing": [],
                        "line_start": node.lineno, "line_end": node.end_lineno or node.lineno,
                        "signature": f"class {node.name}", "doc": first_doc(node),
                        "decorators": decorators(node),
                        "doc_line_start": cd.lineno if cd else None,
                        "doc_line_end": (cd.end_lineno or cd.lineno) if cd else None})
            for child in node.body:
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    names = _assignment_names(child)
                    if names:
                        out.append({"kind": "class_assign",
                                    "symbol": ", ".join(f"{node.name}.{n}" for n in names),
                                    "enclosing": [node.name],
                                    "line_start": child.lineno,
                                    "line_end": child.end_lineno or child.lineno,
                                    "signature": None, "doc": None, "decorators": []})
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add_callable(child, [node.name])
    return out


def _emit(chunks: list[dict], segment: str, *, rel_path: str, symbol: str,
          enclosing: list[str], base_line: int, profile: Mapping | None,
          kind_label: str | None = None, short: str | None = None) -> None:
    max_chars = int(profile_value(profile, "ast_max_chars"))
    overlap = int(profile_value(profile, "chunk_overlap"))
    min_chars = int(profile_value(profile, "min_chunk_chars"))
    use_header = bool(profile_value(profile, "context_header"))
    parts = split_long_text(segment, max_chars, overlap)
    for part_no, (part, left, right) in enumerate(parts, 1):
        body = part.strip()
        if len(body) < min_chars:
            continue
        start_line = base_line + segment[:left].count("\n")
        end_line = base_line + segment[:right].count("\n")
        if part.endswith("\n"):
            end_line -= 1
        end_line = max(start_line, end_line)
        name = short or symbol
        label = name if len(parts) == 1 else f"{name} [part {part_no}/{len(parts)}]"
        enc_labels = [f"class {e}" for e in enclosing]
        if kind_label:
            enc_labels.append(f"{kind_label} {label}")
        if use_header:
            body = context_header.apply(body, context_header.build(rel_path, "code", enclosing=enc_labels))
        chunks.append({
            "type": "code", "path": rel_path,
            "line_start": int(start_line), "line_end": int(end_line),
            "section": None,
            "enclosing": enc_labels or None,
            "symbol": symbol,
            "text": body,
        })


def chunk_code_ast(text: str, rel_path: str, profile: Mapping | None = None) -> list[dict]:
    """Python 파일을 AST 단위로 청킹. 파싱 실패 시 줄 윈도우로 폴백합니다."""
    try:
        nodes = python_nodes(text)
    except (SyntaxError, ValueError, RecursionError):
        return chunk_code_lines(text, rel_path, profile)
    lines = text.splitlines(keepends=True)
    chunks: list[dict] = []
    for nd in nodes:
        if nd["kind"] == "class":
            if nd.get("doc_line_start"):
                seg = "".join(lines[nd["doc_line_start"] - 1:nd["doc_line_end"]])
                _emit(chunks, seg, rel_path=rel_path, symbol=f"{nd['symbol']}.__doc__",
                      enclosing=[nd["symbol"]], base_line=nd["doc_line_start"], profile=profile,
                      kind_label="docstring")
            continue
        seg = "".join(lines[nd["line_start"] - 1:nd["line_end"]])
        kind_label = {"module_doc": "docstring", "assign": "const", "class_assign": "const",
                      "function": "def", "method": "def"}.get(nd["kind"], "def")
        if nd["kind"] in ("method", "class_assign"):
            short = ", ".join(s.split(".")[-1] for s in nd["symbol"].split(", "))
        else:
            short = nd["symbol"]
        _emit(chunks, seg, rel_path=rel_path, symbol=nd["symbol"], enclosing=nd["enclosing"],
              base_line=nd["line_start"], profile=profile, kind_label=kind_label, short=short)
    if not chunks and text.strip():
        return chunk_code_lines(text, rel_path, profile)
    return chunks


# ── 마크다운 (fence 인식) ─────────────────────────────────────

def _doc_blocks(lines: list[str]) -> list[tuple[str, int, list[str]]]:
    """(섹션 제목, 시작 줄(1-based), 줄 목록). ``` 안의 # 는 헤딩이 아닙니다."""
    blocks = []
    title, start, cur = "(intro)", 1, []
    in_fence = False
    fence_mark = None
    for idx, line in enumerate(lines, start=1):
        fm = _FENCE.match(line)
        if fm:
            mark = fm.group(1)
            if not in_fence:
                in_fence, fence_mark = True, mark
            elif mark == fence_mark:
                in_fence, fence_mark = False, None
            cur.append(line)
            continue
        m = None if in_fence else _HEADING.match(line)
        if m:
            if any(s.strip() for s in cur):
                blocks.append((title, start, cur))
            title, start, cur = m.group(2).strip(), idx, [line]
        else:
            cur.append(line)
    if any(s.strip() for s in cur):
        blocks.append((title, start, cur))
    return blocks


def chunk_doc(text: str, rel_path: str, profile: Mapping | None = None) -> list[dict]:
    size_limit = int(profile_value(profile, "chunk_size"))
    min_chars = int(profile_value(profile, "min_chunk_chars"))
    use_header = bool(profile_value(profile, "context_header"))
    lines = text.splitlines()
    chunks: list[dict] = []
    pending_parent: str | None = None      # 본문 없는 상위 제목은 다음 섹션 경로에 붙입니다
    for title, start, body_lines in _doc_blocks(lines):
        body = "\n".join(body_lines).strip()
        if len(body) < min_chars:
            if title != "(intro)":
                pending_parent = title
            continue
        section = f"{pending_parent} > {title}" if pending_parent and title != "(intro)" else title
        pending_parent = None
        if len(body) <= size_limit * 1.5:
            if use_header:
                body = context_header.apply(body, context_header.build(rel_path, "doc", section=section))
            chunks.append({"type": "doc", "path": rel_path, "line_start": start,
                           "line_end": start + len(body_lines) - 1, "section": section,
                           "enclosing": None, "symbol": None, "text": body})
        else:
            for sub in chunk_code_lines(body, rel_path, {**dict(profile or {}), "context_header": False}):
                sub_text = sub["text"]
                if use_header:
                    sub_text = context_header.apply(sub_text, context_header.build(rel_path, "doc", section=section))
                chunks.append({"type": "doc", "path": rel_path,
                               "line_start": start + sub["line_start"] - 1,
                               "line_end": start + sub["line_end"] - 1,
                               "section": section, "enclosing": None, "symbol": None,
                               "text": sub_text})
    return chunks


# ── 진입점 ───────────────────────────────────────────────────

def chunk_text(text: str, rel_path: str, profile: Mapping | None = None) -> list[dict]:
    kind = classify(Path(rel_path))
    if kind is None or not isinstance(text, str) or not text.strip():
        return []
    if kind == "doc":
        chunks = chunk_doc(text, rel_path, profile)
    elif rel_path.lower().endswith(".py") and str(profile_value(profile, "chunker")).startswith("ast"):
        chunks = chunk_code_ast(text, rel_path, profile)
    else:
        chunks = chunk_code_lines(text, rel_path, profile)
    for i, c in enumerate(chunks):
        c["chunk_index"] = i
    return chunks


def chunk_file(path: Path, root: Path, profile: Mapping | None = None) -> list[dict]:
    text = read_text(path)
    if not text or not text.strip():
        return []
    return chunk_text(text, path.relative_to(root).as_posix(), profile)


def chunk_repo(root: str | Path, profile: Mapping | None = None) -> tuple[list[dict], list[Path]]:
    root = Path(root).resolve()
    files = collect_files(root, profile)
    chunks: list[dict] = []
    for f in files:
        chunks.extend(chunk_file(f, root, profile))
    return chunks, files
