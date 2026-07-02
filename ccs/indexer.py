"""Parse Claude Code ``.jsonl`` session files into the SQLite index.

Incremental: a session is only re-parsed when its file's mtime changes, so
keeping the TUI open and re-indexing every couple of seconds is cheap.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import db

# Cap stored text so the FTS index stays a sane size over ~1 GB of transcripts.
_MAX_TEXT = 2000
_MAX_TOOL = 400


def iter_session_files(projects_dir: Path) -> Iterable[tuple[str, Path]]:
    """Yield (session_id, path) for every session jsonl under projects_dir."""
    if not projects_dir.exists():
        return
    for proj in sorted(projects_dir.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            yield f.stem, f


def _flatten_input(inp: dict) -> str:
    parts = []
    for k, v in inp.items():
        if isinstance(v, (str, int, float, bool)):
            parts.append(f"{k}={v}")
        elif isinstance(v, (list, dict)):
            parts.append(f"{k}={json.dumps(v)[:120]}")
    return " ".join(parts)


def _content_text(content, *, want_human: bool) -> tuple[str, int, bool]:
    """Return (searchable_text, tool_use_count, has_human_text).

    want_human distinguishes real human prompts (used for turn counting and
    first/last prompt) from tool-result-only user messages.
    """
    tool_calls = 0
    if content is None:
        return "", 0, False
    if isinstance(content, str):
        return content.strip(), 0, bool(content.strip())
    texts: list[str] = []
    has_human = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = (block.get("text") or "").strip()
            if t:
                texts.append(t[:_MAX_TEXT])
                has_human = True
        elif btype == "tool_use":
            tool_calls += 1
            name = block.get("name", "tool")
            flat = _flatten_input(block.get("input") or {})
            texts.append(f"[{name}] {flat}"[:_MAX_TOOL])
        # tool_result / thinking / image blocks are intentionally skipped:
        # results are huge file dumps and thinking is internal.
    return "\n".join(texts), tool_calls, has_human


def _looks_like_prompt(text: str) -> bool:
    if not text:
        return False
    # Skip harness-injected system reminders / command wrappers.
    stripped = text.lstrip()
    for wrapper in ("<system-reminder", "<command-", "<local-command",
                    "<user-memory", "<session-", "Caveat: The messages below"):
        if stripped.startswith(wrapper):
            return False
    return True


def parse_session(session_id: str, path: Path, project_dir: str) -> tuple[dict, list[dict]]:
    """Parse one session file into (session_row, fts_messages)."""
    cwd = entrypoint = custom_title = last_prompt_line = None
    first_prompt = last_branch = forked_from = None
    timestamps: list[str] = []
    models: list[str] = []
    branches: list[str] = []
    pr_links: list[str] = []
    turns = assistant_turns = tool_calls = out_tokens = 0
    fts: list[dict] = []

    with path.open("r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = obj.get("type")
            ts = obj.get("timestamp")
            if ts:
                timestamps.append(ts)
            if obj.get("cwd") and not cwd:
                cwd = obj["cwd"]
            if obj.get("entrypoint") and not entrypoint:
                entrypoint = obj["entrypoint"]
            if not forked_from:
                ff = obj.get("forkedFrom")
                if isinstance(ff, dict):
                    forked_from = ff.get("sessionId")
                elif isinstance(ff, str):
                    forked_from = ff
            gb = obj.get("gitBranch")
            if gb:
                last_branch = gb  # ends as the branch the session was last on
                if gb not in branches:
                    branches.append(gb)

            if typ == "custom-title":
                custom_title = obj.get("customTitle") or custom_title
            elif typ == "last-prompt":
                last_prompt_line = obj.get("lastPrompt") or last_prompt_line
            elif typ == "pr-link":
                link = obj.get("prLink") or obj.get("url") or obj.get("link")
                if link:
                    pr_links.append(str(link))
            elif typ == "user":
                if obj.get("isSidechain"):
                    continue
                msg = obj.get("message") or {}
                text, _, has_human = _content_text(msg.get("content"), want_human=True)
                if has_human and _looks_like_prompt(text):
                    turns += 1
                    if first_prompt is None:
                        first_prompt = text[:500]
                    if text.strip():
                        fts.append({"role": "user", "ts": ts, "text": text})
            elif typ == "assistant":
                if obj.get("isSidechain"):
                    continue
                assistant_turns += 1
                msg = obj.get("message") or {}
                model = msg.get("model")
                if model and model not in ("<synthetic>",) and model not in models:
                    models.append(model)
                usage = msg.get("usage") or {}
                out_tokens += usage.get("output_tokens") or 0
                text, tc, _ = _content_text(msg.get("content"), want_human=False)
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

    stat = path.stat()
    project = os.path.basename(cwd) if cwd else project_dir
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
        "assistant_turns": assistant_turns,
        "tool_calls": tool_calls,
        "models": ",".join(models),
        "git_branches": ",".join(branches),
        "branch": last_branch,
        "forked_from": forked_from,
        "custom_title": custom_title,
        "first_prompt": first_prompt,
        "last_prompt": last_prompt,
        "entrypoint": entrypoint,
        "pr_links": ",".join(pr_links),
        "out_tokens": out_tokens,
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
    """(Re)index sessions. Returns the list of session ids that changed."""
    existing = {
        r["id"]: r["mtime"]
        for r in conn.execute("SELECT id, mtime FROM sessions").fetchall()
    }
    files = list(iter_session_files(projects_dir))
    seen: set[str] = set()
    changed: list[str] = []
    total = len(files)

    for i, (sid, path) in enumerate(files):
        seen.add(sid)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if changed_only and sid in existing and abs(existing[sid] - mtime) < 1e-6:
            continue
        project_dir = path.parent.name
        try:
            row, fts = parse_session(sid, path, project_dir)
        except Exception:
            continue
        db.upsert_session(conn, row)
        db.replace_fts(conn, sid, fts)
        changed.append(sid)
        if progress_cb and (i % 25 == 0 or i == total - 1):
            progress_cb(i + 1, total)

    # Drop sessions whose files disappeared.
    for sid in set(existing) - seen:
        db.delete_session(conn, sid)
        changed.append(sid)

    conn.commit()
    return changed
