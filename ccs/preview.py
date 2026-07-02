"""Render a session's conversation into a Rich renderable for the preview pane."""
from __future__ import annotations

import json
from pathlib import Path

from rich.text import Text

from . import util
from .indexer import _content_text

_MAX_MSG_CHARS = 1400
_MAX_MESSAGES = 250


def render_session(path: str | Path, fork_note: str | None = None) -> Text:
    path = Path(path)
    out = Text()
    if not path.exists():
        out.append("(session file not found)\n", style="red")
        return out

    messages: list[tuple[str, str | None, str, str | None]] = []
    header: dict = {}
    try:
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
                if obj.get("cwd") and "cwd" not in header:
                    header["cwd"] = obj["cwd"]
                if obj.get("gitBranch") and "branch" not in header:
                    header["branch"] = obj["gitBranch"]
                if typ == "custom-title":
                    header["title"] = obj.get("customTitle")
                if obj.get("isSidechain"):
                    continue
                if typ == "user":
                    msg = obj.get("message") or {}
                    text, _, has_human = _content_text(msg.get("content"), want_human=True)
                    if has_human and text.strip():
                        messages.append(("user", obj.get("timestamp"), text, None))
                elif typ == "assistant":
                    msg = obj.get("message") or {}
                    text, _, _ = _content_text(msg.get("content"), want_human=False)
                    if text.strip():
                        messages.append(("assistant", obj.get("timestamp"), text, msg.get("model")))
    except OSError as e:
        out.append(f"(could not read session: {e})\n", style="red")
        return out

    # Header block
    if header.get("title"):
        out.append(f"{header['title']}\n", style="bold yellow")
    if header.get("cwd"):
        out.append(f"{header['cwd']}", style="dim")
    if header.get("branch"):
        out.append(f"  ({header['branch']})", style="dim cyan")
    out.append("\n")
    if fork_note:
        out.append(f"{fork_note}\n", style="medium_purple2")
    out.append(f"{len(messages)} messages\n\n", style="dim")

    shown = messages
    if len(messages) > _MAX_MESSAGES:
        hidden = len(messages) - _MAX_MESSAGES
        out.append(f"… {hidden} earlier messages hidden …\n\n", style="dim italic")
        shown = messages[-_MAX_MESSAGES:]

    for role, ts, text, model in shown:
        when = util.rel_time(ts)
        if role == "user":
            out.append("▸ you", style="bold cyan")
            out.append(f"  · {when}\n", style="dim")
            body_style = "white"
        else:
            out.append("▸ claude", style="bold green")
            tag = util.short_model(model) if model else ""
            out.append(f"  · {tag} · {when}\n", style="dim")
            body_style = "grey70"
        body = text.strip()
        if len(body) > _MAX_MSG_CHARS:
            body = body[:_MAX_MSG_CHARS] + " …"
        for bline in body.splitlines() or [""]:
            # Tool-call lines produced by the indexer look like "[Bash] cmd=…".
            if bline.startswith("[") and "]" in bline[:40]:
                out.append("  " + bline + "\n", style="dim yellow")
            else:
                out.append("  " + bline + "\n", style=body_style)
        out.append("\n")

    return out
