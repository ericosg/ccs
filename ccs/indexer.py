"""Parse Claude Code ``.jsonl`` session files into the SQLite index.

Incremental: a session is only re-parsed when its file's mtime changes, so
keeping the TUI open and re-indexing every couple of seconds is cheap. All
knowledge of the transcript format lives in ``ccformat``.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import ccformat as cf
from . import db

# Cap stored text so the FTS index stays a sane size over ~1 GB of transcripts.
_MAX_TEXT = 2000
_MAX_TOOL = 400

# path -> mtime of files that failed to parse, so the live tick doesn't retry
# a broken file every 2 s (retried as soon as it changes again).
_failed: dict[str, float] = {}


def iter_session_files(projects_dir: Path) -> Iterable[tuple[str, Path]]:
    """Yield (session_id, path) for every session jsonl under projects_dir.

    Depth 1 only: deeper .jsonl files are subagent transcripts / plugin logs.
    """
    projects_dir = Path(projects_dir)
    if not projects_dir.exists():
        return
    for proj in sorted(projects_dir.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            yield f.stem, f


def _assistant_text(content) -> tuple[str, int]:
    """(searchable text, tool-call count) for an assistant message's content."""
    texts, tools = [], 0
    for b in cf.blocks(content):
        bt = b.get("type")
        if bt == "tool_use":
            tools += 1
            texts.append(cf.tool_search_text(b.get("name", "tool"), b.get("input") or {}, _MAX_TOOL))
        elif bt in ("thinking", "redacted_thinking", "tool_result"):
            continue  # internal / huge; kept out of the index on purpose
        else:
            t = cf.block_text(b)
            if t:
                texts.append(t[:_MAX_TEXT])
    return "\n".join(texts), tools


def parse_session(session_id: str, path: Path, project_dir: str,
                  drift: Optional[cf.Drift] = None) -> tuple[dict, list[dict]]:
    """Parse one session file into (session_row, fts_messages)."""
    cwd = entrypoint = custom_title = ai_title = agent_name = recap = None
    last_prompt_line = first_prompt = last_branch = forked_from = None
    timestamps: list[str] = []
    models: list[str] = []
    branches: list[str] = []
    pr_links: list[str] = []
    turns = tool_calls = 0
    # Claude Code writes one assistant message as several lines (one per
    # content block), each repeating the usage → count/sum per message id.
    tokens_by_msg: dict[str, int] = {}
    anon_assistant = 0
    fts: list[dict] = []
    version = ""

    for obj in cf.iter_records(path):
        if drift is not None:
            drift.observe(obj)
        typ = obj.get("type")
        ts = obj.get("timestamp")
        if ts and isinstance(ts, str):
            timestamps.append(ts)
        if obj.get("cwd") and not cwd:
            cwd = obj["cwd"]
        if obj.get("entrypoint") and not entrypoint:
            entrypoint = obj["entrypoint"]
        v = obj.get("version")
        if isinstance(v, str) and cf.version_key(v) > cf.version_key(version):
            version = v
        if not forked_from:
            forked_from = cf.forked_from(obj)
        gb = obj.get("gitBranch")
        if gb:
            last_branch = gb  # ends as the branch the session was last on
            if gb not in branches:
                branches.append(gb)

        if typ == "custom-title":
            custom_title = obj.get("customTitle") or custom_title
        elif typ == "ai-title":
            ai_title = obj.get("aiTitle") or ai_title
        elif typ == "agent-name":
            agent_name = obj.get("agentName") or agent_name
        elif typ == "last-prompt":
            last_prompt_line = obj.get("lastPrompt") or last_prompt_line
        elif typ == "pr-link":
            link = cf.pr_link(obj)
            if link and link not in pr_links:
                pr_links.append(link)
        elif typ == "system":
            if obj.get("subtype") == "away_summary" and isinstance(obj.get("content"), str):
                recap = obj["content"].strip() or recap
        elif typ == "user":
            if obj.get("isSidechain"):
                continue
            entry = cf.classify_user(obj)
            if entry.is_prompt:
                turns += 1
                if first_prompt is None:
                    first_prompt = entry.text[:500]
                fts.append({"role": "user", "ts": ts, "text": entry.text[:_MAX_TEXT]})
        elif typ == "assistant":
            if obj.get("isSidechain"):
                continue
            msg = obj.get("message") or {}
            model = msg.get("model")
            if model and model != "<synthetic>" and model not in models:
                models.append(model)
            out = (msg.get("usage") or {}).get("output_tokens") or 0
            mid = msg.get("id")
            if mid:
                tokens_by_msg[mid] = out  # last line of a message wins
            else:
                anon_assistant += 1
                tokens_by_msg[f"__anon{anon_assistant}"] = out
            text, tc = _assistant_text(msg.get("content"))
            tool_calls += tc
            if text.strip():
                fts.append({"role": "assistant", "ts": ts, "text": text})

    timestamps.sort()
    first_ts = timestamps[0] if timestamps else None
    last_ts = timestamps[-1] if timestamps else None
    duration_s = None
    if first_ts and last_ts:
        try:
            a = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            b = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_s = int((b - a).total_seconds())
        except ValueError:
            pass

    last_prompt = last_prompt_line
    if not last_prompt:
        for m in reversed(fts):
            if m["role"] == "user":
                last_prompt = m["text"][:500]
                break

    # Titles/recap are searchable too (role "meta" — not shown in counts).
    meta_text = "\n".join(t for t in (custom_title, ai_title, agent_name, recap) if t)
    if meta_text:
        fts.append({"role": "meta", "ts": last_ts, "text": meta_text[:_MAX_TEXT]})

    stat = path.stat()
    project = os.path.basename(cwd.rstrip("/")) if cwd else project_dir
    row = {
        "id": session_id,
        "file_path": str(path),
        "project_dir": project_dir,
        "cwd": cwd,
        "project": project,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "duration_s": duration_s,
        "turns": turns,
        "assistant_turns": len(tokens_by_msg),
        "tool_calls": tool_calls,
        "models": ",".join(models),
        "git_branches": ",".join(branches),
        "branch": last_branch,
        "forked_from": forked_from,
        "custom_title": custom_title,
        "ai_title": ai_title,
        "agent_name": agent_name,
        "recap": recap,
        "cc_version": version or None,
        "first_prompt": first_prompt,
        "last_prompt": last_prompt,
        "entrypoint": entrypoint,
        "pr_links": ",".join(pr_links),
        "out_tokens": sum(tokens_by_msg.values()),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }
    return row, fts


def index_all(
    conn,
    projects_dir: Path = db.DEFAULT_PROJECTS_DIR,
    *,
    changed_only: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> list[str]:
    """(Re)index sessions. Returns the list of session ids that changed.

    A parser upgrade (``ccformat.PARSER_VERSION``) forces a full re-parse.
    Unknown format elements seen while parsing are accumulated in the
    ``drift`` meta key (reset on every full pass).
    """
    full = not changed_only or db.get_meta(conn, "parser_version") != cf.PARSER_VERSION
    existing = {
        r["id"]: r["mtime"]
        for r in conn.execute("SELECT id, mtime FROM sessions").fetchall()
    }
    files = list(iter_session_files(projects_dir))
    seen: set[str] = set()
    changed: list[str] = []
    total = len(files)
    drift = cf.Drift() if full else cf.Drift.from_json(db.get_meta(conn, "drift"))
    drift_before = drift.to_json()

    for i, (sid, path) in enumerate(files):
        seen.add(sid)
        if progress_cb and (i % 25 == 0 or i == total - 1):
            progress_cb(i + 1, total)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if not full and sid in existing and abs(existing[sid] - mtime) < 1e-6:
            continue
        if not full and _failed.get(str(path)) == mtime:
            continue
        try:
            row, fts = parse_session(sid, path, path.parent.name, drift)
        except Exception:
            _failed[str(path)] = mtime
            continue
        _failed.pop(str(path), None)
        db.upsert_session(conn, row)
        db.replace_fts(conn, sid, fts)
        changed.append(sid)

    # Drop sessions whose files disappeared.
    for sid in set(existing) - seen:
        db.delete_session(conn, sid)
        changed.append(sid)

    if full:
        db.set_meta(conn, "parser_version", cf.PARSER_VERSION)
    if drift.to_json() != drift_before:
        db.set_meta(conn, "drift", drift.to_json())
    conn.commit()
    return changed
