"""Filesystem-backed ADR store at ``<repo>/docs/adr/``.

Each ADR is a Markdown file named ``NNNN-slug.md``. The first ``# `` heading is
the title; the first ``**Status:** value`` line is the status. Anything that
does not parse is reported with empty fields rather than raising.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_ADR_DIRNAME = ("docs", "adr")
_ADR_NAME_RE = re.compile(r"^(\d{4,})-(.+)\.md$")
_STATUS_RE = re.compile(r"\*\*Status:\*\*\s*([A-Za-z][A-Za-z\- ]*)")
_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WORD_SPLIT = re.compile(r"[^a-zA-Z0-9]+")


@dataclass(frozen=True)
class AdrRecord:
    id: str          # zero-padded number from filename, e.g. "0036"
    title: str
    status: str
    path: str        # repo-relative POSIX path
    content: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "path": self.path,
            "content": self.content,
        }


def _adr_root(repo: Path) -> Path:
    return repo / _ADR_DIRNAME[0] / _ADR_DIRNAME[1]


def _parse_file(repo: Path, path: Path) -> AdrRecord | None:
    name = path.name
    match = _ADR_NAME_RE.match(name)
    if not match:
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    title = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
    status_match = _STATUS_RE.search(content)
    status = status_match.group(1).strip().lower() if status_match else ""
    rel = path.relative_to(repo).as_posix()
    return AdrRecord(
        id=match.group(1),
        title=title,
        status=status,
        path=rel,
        content=content,
    )


def _iter_records(repo: Path) -> list[AdrRecord]:
    adr_dir = _adr_root(repo)
    if not adr_dir.is_dir():
        return []
    records = [
        _parse_file(repo, p)
        for p in sorted(adr_dir.iterdir())
        if p.is_file()
    ]
    return [r for r in records if r is not None]


def list_adrs(repo: Path | str) -> list[dict]:
    """Return every parseable ADR under ``<repo>/docs/adr/``, sorted by id."""
    repo = Path(repo).resolve()
    records = sorted(_iter_records(repo), key=lambda r: r.id)
    return [r.to_dict() for r in records]


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD_SPLIT.split(text) if len(t) >= 2]


def _score(query: str, record: AdrRecord) -> int:
    q_tokens = set(_tokens(query))
    if not q_tokens:
        return 0
    title_tokens = Counter(_tokens(record.title))
    content_tokens = Counter(_tokens(record.content))
    score = 0
    for term in q_tokens:
        score += 3 * title_tokens.get(term, 0)
        score += content_tokens.get(term, 0)
    return score


def read_adr(
    repo: Path | str,
    query: str | None = None,
    path: str | None = None,
    top_k: int = 5,
) -> list[dict]:
    """Read ADRs from ``<repo>/docs/adr/``.

    - ``path`` given: return that single ADR (empty list if missing/unparseable).
    - ``query`` given: rank ADRs by token overlap on title+content, return top_k.
    - neither: return all ADRs.
    """
    repo = Path(repo).resolve()
    if path:
        target = repo / path
        if not target.is_file():
            return []
        record = _parse_file(repo, target)
        return [record.to_dict()] if record else []
    if not query:
        return list_adrs(repo)
    scored = [(r, _score(query, r)) for r in _iter_records(repo)]
    scored = sorted((p for p in scored if p[1] > 0), key=lambda p: -p[1])
    return [r.to_dict() for r, _ in scored[:top_k]]


def _slugify(title: str) -> str:
    slug = _SLUG_NON_ALNUM.sub("-", title.lower()).strip("-")
    return slug or "adr"


def _next_id(adr_dir: Path) -> str:
    max_id = 0
    if adr_dir.is_dir():
        for path in adr_dir.iterdir():
            match = _ADR_NAME_RE.match(path.name)
            if match:
                max_id = max(max_id, int(match.group(1)))
    return f"{max_id + 1:04d}"


def write_adr(repo: Path | str, title: str, content: str) -> dict:
    """Persist a new ADR; assign the next sequential id; return ``{id, path}``."""
    repo = Path(repo).resolve()
    adr_dir = _adr_root(repo)
    adr_dir.mkdir(parents=True, exist_ok=True)
    new_id = _next_id(adr_dir)
    slug = _slugify(title)
    filename = f"{new_id}-{slug}.md"
    target = adr_dir / filename
    if not content.lstrip().startswith("#"):
        content = f"# {title}\n\n{content}"
    target.write_text(content, encoding="utf-8")
    return {"id": new_id, "path": target.relative_to(repo).as_posix()}
