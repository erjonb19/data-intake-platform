"""The runbooks (docs/runbooks/*.md) as something an agent can search (Stage 9, S9.3).

Each runbook is split at its `##` headings into sections; `search()` ranks sections with
BM25 over their words. No model, no vector store, no network: ~30 lines of scoring the
platform owns, over documents the platform wrote (the runbooks are ours, not a vendor's,
so their text is not marked untrusted). In the images the files live at
DIP_RUNBOOK_DIR (/app/docs/runbooks); in the repo, docs/runbooks.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dip.contracts import REPO_ROOT

# Codes split into words (SCHEMA.MISSING_COLUMN -> schema missing column), so a reason
# code finds the prose about it.
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)
MAX_SECTION_CHARS = 1500


def runbook_dir() -> Path:
    return Path(os.environ.get("DIP_RUNBOOK_DIR") or REPO_ROOT / "docs" / "runbooks")


@dataclass(frozen=True)
class Section:
    runbook_id: str  # the file stem, e.g. "05-late-file"
    title: str  # the runbook's H1
    heading: str  # the section's H2, or "" for the text before the first one
    text: str


def _tokens(text: str) -> list[str]:
    # A crude plural fold ("instructions" -> "instruction"), enough for runbook prose.
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
            for w in _WORD.findall(text.lower()) if w not in _STOP]  # fmt: skip


def _sections_of(f: Path) -> list[Section]:
    lines = f.read_text(encoding="utf-8").splitlines()
    title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), f.stem)
    out: list[Section] = []
    heading: str = ""
    buf: list[str] = []
    for ln in [*lines, "## "]:  # the sentinel flushes the last section
        if ln.startswith("## "):
            text = "\n".join(buf).strip()
            if text:
                out.append(Section(f.stem, title, heading, text[:MAX_SECTION_CHARS]))
            heading, buf = ln[3:].strip(), []
        elif not ln.startswith("# "):
            buf.append(ln)
    return out


def load(path: Path | None = None) -> list[Section]:
    """Every section of every runbook, in file order. README.md is the index, not a runbook."""
    root = path or runbook_dir()
    return [
        s
        for f in sorted(root.glob("*.md"))
        if f.name.lower() != "readme.md"
        for s in _sections_of(f)
    ]


@lru_cache(maxsize=4)
def _index(root: str) -> tuple[list[Section], list[list[str]], dict[str, int], float]:
    sections = load(Path(root))
    # What a runbook is about (its file name and title) counts three times in each of its
    # sections, so a reason code finds the runbook about it before one that mentions it.
    docs = [_tokens(f"{s.runbook_id} {s.title} " * 3 + f"{s.heading} {s.text}")
            for s in sections]  # fmt: skip
    df: dict[str, int] = {}
    for d in docs:
        for w in set(d):
            df[w] = df.get(w, 0) + 1
    avg = sum(len(d) for d in docs) / max(1, len(docs))
    return sections, docs, df, avg


def search(
    query: str, limit: int = 3, root: Path | None = None, k1: float = 1.5, b: float = 0.75
) -> list[dict]:
    """The best-matching sections for a query (reason codes, a root cause, log words)."""
    sections, docs, df, avg = _index(str(root or runbook_dir()))
    q = _tokens(query)
    if not q or not docs:
        return []
    n = len(docs)
    scored = []
    for s, d in zip(sections, docs, strict=True):
        counts: dict[str, int] = {}
        for w in d:
            counts[w] = counts.get(w, 0) + 1
        score = 0.0
        for w in set(q):
            tf = counts.get(w, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [
        {"runbook_id": s.runbook_id, "title": s.title, "section": s.heading,
         "text": s.text, "score": round(score, 3)}
        for score, s in scored[: max(1, min(limit, 10))]
    ]  # fmt: skip


def ids(root: Path | None = None) -> set[str]:
    return {s.runbook_id for s in _index(str(root or runbook_dir()))[0]}
