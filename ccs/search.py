"""Local full-text search over the FTS5 index (shared by the TUI and AI search)."""
from __future__ import annotations

import re
from dataclasses import dataclass

# \w is Unicode-aware: Greek, accented Latin, Cyrillic, CJK… all tokenize.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "have", "was", "were",
    "our", "out", "you", "your", "are", "did", "made", "make", "want", "need",
    "where", "when", "what", "which", "how", "some", "into", "about", "can",
    "will", "would", "there", "then", "them", "they", "used", "use", "get",
    "got", "has", "had", "not", "but", "all", "any", "one", "way", "set",
    "session", "sessions", "remember", "something", "thing", "where", "i",
    "a", "an", "of", "to", "in", "on", "it", "is", "we", "me", "my", "at",
    "or", "by", "be", "as", "do", "so", "if",
}


def tokens(query: str, *, min_len: int = 2, drop_stopwords: bool = True) -> list[str]:
    """Distinct lowercase word tokens. Stopwords are dropped only when that
    leaves something to search for."""
    raw = []
    for t in _TOKEN_RE.findall(query.lower()):
        if len(t) >= min_len and t not in raw:
            raw.append(t)
    if not drop_stopwords:
        return raw
    kept = [t for t in raw if t not in STOPWORDS]
    return kept or raw


def match_expr(toks: list[str], op: str = "OR") -> str:
    """FTS5 MATCH expression; every token is a quoted string (so it can't be
    parsed as an operator/column filter), embedded quotes doubled."""
    return f" {op} ".join('"' + t.replace('"', '""') + '"' for t in toks)


@dataclass
class TextHit:
    id: str
    snippet: str
    score: float


def _sessions_with(conn, tok: str) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT DISTINCT session_id FROM messages_fts WHERE messages_fts MATCH ?",
            (match_expr([tok]),),
        )
    }


def text_search(conn, query: str, *, limit: int = 300) -> tuple[list[TextHit], str]:
    """Rank *sessions* for a full-text query.

    Sessions containing every word (anywhere in the session, not necessarily
    the same message) are preferred; if none do, falls back to any word.
    Returns (hits, mode) where mode is 'all' | 'any' | 'none'.
    """
    toks = tokens(query)
    if not toks:
        return [], "none"
    required: set[str] | None = None
    mode = "any"
    if len(toks) > 1:
        sets = [_sessions_with(conn, t) for t in toks]
        both = set.intersection(*sets)
        if both:
            required, mode = both, "all"
    elif toks:
        mode = "all"

    best: dict[str, TextHit] = {}
    hits: dict[str, int] = {}
    cur = conn.execute(
        """
        SELECT session_id, bm25(messages_fts) AS rank,
               snippet(messages_fts, 3, '«', '»', '…', 12) AS snip
        FROM messages_fts WHERE messages_fts MATCH ? ORDER BY rank LIMIT 4000
        """,
        (match_expr(toks),),
    )
    for sid, rank, snip in cur:
        if required is not None and sid not in required:
            continue
        hits[sid] = hits.get(sid, 0) + 1
        if sid not in best:
            best[sid] = TextHit(sid, snip or "", rank)
    for sid in required or ():
        best.setdefault(sid, TextHit(sid, "", 0.0))
    # bm25 is negative (lower = better); more matching messages → a bit better.
    for sid, h in best.items():
        h.score = h.score * (1 + 0.15 * min(hits.get(sid, 0), 10))
    ranked = sorted(best.values(), key=lambda h: h.score)[:limit]
    return ranked, mode
