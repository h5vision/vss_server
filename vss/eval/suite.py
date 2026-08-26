"""질문 suite 검증 (rag_lab experiments/suite.py 복사본 — SALVAGE.md). 사람·LLM 이 같은 계약으로 질문을 씁니다."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ROOT = Path(__file__).resolve().parents[2]   # vss_server/


class ValidationError(ValueError):
    def __init__(self, errors: list[str], warnings: list[str] | None = None):
        self.errors = errors
        self.warnings = warnings or []
        super().__init__("; ".join(errors))


def canonical_hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def allowed_tags() -> dict[str, str]:
    data = json.loads((ROOT / "evaluation" / "tags.json").read_text(encoding="utf-8"))
    return data["tags"]


def load_questions(path: str | Path, *, repository: str | Path | None = None) -> list[dict]:
    p = Path(path).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    questions: list[dict] = []
    seen: set[str] = set()
    tags = allowed_tags()
    repo = Path(repository).resolve() if repository else None

    if not p.is_file():
        raise ValidationError([f"질문 suite가 없습니다: {p}"])
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            q = json.loads(raw)
        except json.JSONDecodeError as e:
            errors.append(f"{p.name}:{lineno} JSON 파싱 실패: {e.msg}")
            continue
        prefix = f"{p.name}:{lineno}"
        required = {"id", "question", "answerable", "gold", "tags"}
        allowed = required | {"type", "note"}
        missing = required - set(q) if isinstance(q, dict) else required
        if not isinstance(q, dict) or missing:
            errors.append(f"{prefix} 필수 필드 누락: {', '.join(sorted(missing))}")
            continue
        unknown_fields = sorted(set(q) - allowed)
        if unknown_fields:
            errors.append(f"{prefix} 알 수 없는 필드: {', '.join(unknown_fields)}")
        qid = q.get("id")
        if not isinstance(qid, str) or not ID_RE.fullmatch(qid):
            errors.append(f"{prefix} 잘못된 id: {qid!r}")
        elif qid in seen:
            errors.append(f"{prefix} 중복 id: {qid}")
        seen.add(qid)
        if not isinstance(q.get("question"), str) or not q["question"].strip():
            errors.append(f"{prefix} question은 비어 있지 않은 문자열이어야 합니다")
        if not isinstance(q.get("answerable"), bool):
            errors.append(f"{prefix} answerable은 boolean이어야 합니다")
        gold = q.get("gold")
        if not isinstance(gold, list):
            errors.append(f"{prefix} gold는 배열이어야 합니다")
            gold = []
        if q.get("answerable") and not gold:
            errors.append(f"{prefix} answerable=true인데 gold가 없습니다")
        if q.get("answerable") is False and gold:
            errors.append(f"{prefix} answerable=false인데 gold가 들어 있습니다")
        qtags = q.get("tags")
        if not isinstance(qtags, list) or not qtags or any(not isinstance(t, str) for t in qtags):
            errors.append(f"{prefix} tags는 하나 이상의 문자열 배열이어야 합니다")
        else:
            unknown = sorted(set(qtags) - set(tags))
            if unknown:
                errors.append(f"{prefix} 등록되지 않은 tag: {', '.join(unknown)}")
            if len(qtags) != len(set(qtags)):
                errors.append(f"{prefix} tag가 중복됐습니다")
        if q.get("answerable") is False and "no_evidence" not in (qtags or []):
            warnings.append(f"{prefix} 답 없는 질문에 no_evidence tag가 없습니다")

        for gi, g in enumerate(gold, 1):
            gp = f"{prefix} gold[{gi}]"
            if not isinstance(g, dict) or not isinstance(g.get("path"), str):
                errors.append(f"{gp} path가 필요합니다")
                continue
            unknown_gold = sorted(set(g) - {"path", "line_start", "line_end", "symbol"})
            if unknown_gold:
                errors.append(f"{gp} 알 수 없는 필드: {', '.join(unknown_gold)}")
            rel = g["path"].replace("\\", "/").lstrip("/")
            g["path"] = rel
            a, b = g.get("line_start"), g.get("line_end")
            if (a is None) != (b is None):
                errors.append(f"{gp} line_start와 line_end는 함께 써야 합니다")
            if a is not None and (not isinstance(a, int) or not isinstance(b, int) or a < 1 or b < a):
                errors.append(f"{gp} 잘못된 줄 범위: {a}-{b}")
            if repo:
                target = (repo / rel).resolve()
                try:
                    target.relative_to(repo)
                except ValueError:
                    errors.append(f"{gp} repository 밖의 경로입니다: {rel}")
                    continue
                if not target.is_file():
                    errors.append(f"{gp} 실제 파일이 없습니다: {rel}")
                    continue
                text = target.read_text(encoding="utf-8", errors="replace")
                if b is not None and b > len(text.splitlines()):
                    errors.append(f"{gp} line_end={b}가 파일 길이를 넘습니다")
                if g.get("symbol") and g["symbol"] not in text:
                    errors.append(f"{gp} symbol이 파일에 없습니다: {g['symbol']}")
        questions.append(q)

    if not questions and not errors:
        errors.append(f"질문이 하나도 없습니다: {p}")
    if errors:
        raise ValidationError(errors, warnings)
    return questions


def is_gold_hit(hit: dict, gold: list[dict]) -> bool:
    hp = str(hit.get("path", "")).replace("\\", "/")
    for g in gold:
        if hp != g["path"]:
            continue
        if g.get("symbol") and g["symbol"] not in str(hit.get("text", "")):
            continue
        ga, gb = g.get("line_start"), g.get("line_end")
        ha, hb = hit.get("line_start"), hit.get("line_end")
        if ga is not None and ha is not None and hb is not None and (hb < ga or ha > gb):
            continue
        return True
    return False


def first_gold_rank(hits: list[dict], gold: list[dict]) -> int | None:
    for rank, hit in enumerate(hits, 1):
        if is_gold_hit(hit, gold):
            return rank
    return None
