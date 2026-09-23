"""Advanced (natural-language) search: describe a session in a sentence and let
Claude pick it out of the history.

Two stages keep it fast and cheap instead of stuffing 45 MB into context:
  1. FTS5 prefilter narrows ~300 sessions to a few dozen candidates.
  2. `claude -p` ranks those candidate digests against the description.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from . import search, util

# Haiku is more than capable of ranking short digests and keeps latency/cost low.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class AISearchError(RuntimeError):
    pass


@dataclass
class AIMatch:
    id: str
    score: int
    reason: str
    project: str | None = None
    last_ts: str | None = None
    title: str | None = None


def _query_tokens(query: str) -> list[str]:
    return search.tokens(query, min_len=3) or search.tokens(query)


def fts_candidates(conn, query: str, limit: int = 40) -> list[dict]:
    """Return candidate session rows (with a matched snippet) via FTS prefilter."""
    tokens = _query_tokens(query)
    rows_by_session: dict[str, dict] = {}
    if tokens:
        match = search.match_expr(tokens)
        try:
            cur = conn.execute(
                """
                SELECT session_id,
                       bm25(messages_fts) AS rank,
                       snippet(messages_fts, 3, '«', '»', '…', 14) AS snip
                FROM messages_fts
                WHERE messages_fts MATCH ?
                ORDER BY rank
                LIMIT 400
                """,
                (match,),
            )
            for r in cur.fetchall():
                sid = r["session_id"]
                if sid not in rows_by_session:
                    rows_by_session[sid] = {"snip": r["snip"]}
                if len(rows_by_session) >= limit:
                    break
        except Exception as e:  # malformed FTS expression, etc.
            raise AISearchError(f"FTS query failed: {e}")

    # If the prefilter came up short, pad with the most recent sessions so the
    # model still has material to reason over.
    if len(rows_by_session) < 8:
        for r in conn.execute(
            "SELECT id FROM sessions ORDER BY last_ts DESC LIMIT ?", (limit,)
        ):
            rows_by_session.setdefault(r["id"], {"snip": None})

    ids = list(rows_by_session)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    meta = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT * FROM sessions WHERE id IN ({placeholders})", ids
        )
    }
    out = []
    for sid in ids:
        m = meta.get(sid)
        if not m:
            continue
        out.append(
            {
                "id": sid,
                "project": m["project"],
                "last_ts": m["last_ts"],
                "title": util.display_title(dict(m)),
                "recap": m["recap"],
                "first_prompt": m["first_prompt"],
                "last_prompt": m["last_prompt"],
                "snip": rows_by_session[sid]["snip"],
            }
        )
    return out


def _build_digest(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        date = (c["last_ts"] or "")[:10]
        title = c["title"]
        parts = [f"id={c['id']}", f"project={c['project']}", f"date={date}"]
        lines.append("- " + " | ".join(parts))
        if title:
            lines.append(f"    title/first: {title[:120]}")
        if c.get("recap"):
            lines.append(f"    recap: {c['recap'][:160]}")
        if c["last_prompt"]:
            lines.append(f"    last: {c['last_prompt'][:120]}")
        if c["snip"]:
            lines.append(f"    match: {c['snip'][:180]}")
    return "\n".join(lines)


_PROMPT_TEMPLATE = """You are helping locate ONE or a few specific past Claude Code sessions.

The user describes what they remember about the session:
"{query}"

Below is a list of candidate sessions with their id, project, date, title/first \
message, last message, and a snippet where search terms matched. Identify the \
sessions that best match the description.

Candidates:
{digest}

Return ONLY a JSON array (no prose, no code fence) of the matching sessions, \
best match first. Each element: {{"id": "<session id>", "score": <0-100 \
confidence>, "reason": "<short why-it-matches>"}}. Include only plausible \
matches (score >= 40). If nothing matches, return [].
"""


def _extract_json_array(text: str):
    text = (text or "").strip()
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return v
    except json.JSONDecodeError:
        pass
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def ai_search(
    conn,
    query: str,
    *,
    model: str = DEFAULT_MODEL,
    max_candidates: int = 40,
    timeout: int = 120,
) -> list[AIMatch]:
    """Run the two-stage advanced search. Raises AISearchError on failure."""
    candidates = fts_candidates(conn, query, limit=max_candidates)
    if not candidates:
        return []
    prompt = _PROMPT_TEMPLATE.format(query=query, digest=_build_digest(candidates))
    try:
        r = subprocess.run(
            # --no-session-persistence: otherwise every search leaves a new
            # junk session behind that then shows up in the list.
            ["claude", "-p", prompt, "--output-format", "json", "--model", model,
             "--no-session-persistence"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise AISearchError("`claude` CLI not found on PATH")
    except subprocess.TimeoutExpired:
        raise AISearchError(f"claude -p timed out after {timeout}s")
    if r.returncode != 0:
        raise AISearchError(f"claude -p exited {r.returncode}: {r.stderr[:200]}")
    try:
        envelope = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise AISearchError("could not parse claude -p output envelope")
    if envelope.get("is_error"):
        raise AISearchError(f"claude reported an error: {envelope.get('result')}")
    parsed = _extract_json_array(envelope.get("result", ""))
    if parsed is None:
        raise AISearchError("model did not return a JSON array")

    by_id = {c["id"]: c for c in candidates}
    matches: list[AIMatch] = []
    for item in parsed:
        if not isinstance(item, dict) or "id" not in item:
            continue
        c = by_id.get(item["id"], {})
        try:
            score = int(float(item.get("score", 0)))
        except (TypeError, ValueError):
            score = 0
        matches.append(
            AIMatch(
                id=str(item["id"]),
                score=score,
                reason=str(item.get("reason", "")),
                project=c.get("project"),
                last_ts=c.get("last_ts"),
                title=c.get("title"),
            )
        )
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches
