"""Everything ccs knows about Claude Code's transcript format, in one place.

Claude Code's ``.jsonl`` format is undocumented and keeps growing (new line
types, content blocks, harness wrappers, tools). To keep ccs adaptable:

* **All format knowledge lives here** — the indexer and the preview both go
  through these helpers, so a format change is a one-file fix.
* **Unknown things degrade gracefully** — an unknown tool still renders as
  ``Tool  <first useful arg>``, an unknown block with a ``text`` field is shown
  as text, an unknown ``<some-tag>`` wrapper is treated as harness noise.
* **Unknown things are recorded** — ``Drift`` collects every line type / system
  subtype / block type / wrapper tag not listed below. The indexer stores it and
  ``ccs doctor`` reports it, so a Claude Code update that changes the format is
  noticed instead of silently producing wrong counts or titles.

When ``ccs doctor`` flags something: decide whether it matters, then either
teach the parser about it or just add it to the matching ``KNOWN_*`` set, and
bump ``PARSER_VERSION`` if parsed output changes (forces a full re-parse).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

# Bump whenever parse output changes → the next index re-parses every session
# (mtime alone would never notice a parser fix). Independent of db.SCHEMA_VERSION.
PARSER_VERSION = "5"

# Newest Claude Code version ccs's format assumptions were checked against.
VERIFIED_CC_VERSION = "2.1.280"

KNOWN_LINE_TYPES = {
    "user", "assistant", "system", "summary", "attachment",
    "custom-title", "ai-title", "agent-name", "agent-color", "last-prompt",
    "pr-link", "file-history-snapshot", "file-history-delta",
    "permission-mode", "mode", "queue-operation", "atis-latch",
    "bridge-session", "cost-state", "frame-link",
    "artifact-autoreact-ledger", "artifact-comment-monitor",
}

KNOWN_SYSTEM_SUBTYPES = {
    None, "turn_duration", "away_summary", "local_command",
    "scheduled_task_fire", "stop_hook_summary", "api_error",
    "compact_boundary", "model_refusal_fallback", "informational",
}

KNOWN_BLOCK_TYPES = {
    "text", "tool_use", "tool_result", "thinking", "redacted_thinking",
    "image", "document", "fallback",
}

# Leading tags Claude Code wraps harness-generated "user" text in.
KNOWN_WRAPPER_TAGS = {
    "task-notification", "command-name", "command-message", "command-args",
    "local-command-stdout", "local-command-stderr", "local-command-caveat",
    "system-reminder", "bash-input", "bash-stdout", "bash-stderr",
    "user-memory-input", "user-prompt-submit-hook",
}

# Tags that look like wrappers but carry text the human actually wrote.
HUMAN_TAGS = {"pasted_content"}

_LEADING_TAG = re.compile(r"^\s*<([A-Za-z][\w-]*)[\s>/]")
_LEGACY_PREFIXES = ("Caveat: The messages below",)


def _tag_inner(text: str, tag: str) -> Optional[str]:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else None


def strip_tags(text: str) -> str:
    return re.sub(r"</?[A-Za-z][\w-]*[^>]*>", "", text).strip()


# ---- records ---------------------------------------------------------------

def iter_records(path: Path) -> Iterator[dict]:
    """Yield each JSON object in a transcript, skipping blank/corrupt lines."""
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def forked_from(obj: dict) -> Optional[str]:
    ff = obj.get("forkedFrom")
    if isinstance(ff, dict):
        return ff.get("sessionId")
    if isinstance(ff, str):
        return ff
    return None


def pr_link(obj: dict) -> Optional[str]:
    for k in ("prUrl", "prLink", "url", "link"):
        if obj.get(k):
            return str(obj[k])
    return None


def blocks(content) -> list[dict]:
    """Normalise ``message.content`` (str | list | None) to a list of blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def block_text(b: dict) -> str:
    """Text of a text-like block — also for unknown block types with a `text`."""
    t = b.get("text")
    return t.strip() if isinstance(t, str) else ""


# ---- user lines: human prompt vs harness noise -----------------------------

@dataclass
class UserEntry:
    kind: str          # prompt | command | bash | notification | compact | meta | tool_result
    text: str = ""     # display text (prompt text, "/cmd args", summary, …)
    images: int = 0
    tag: Optional[str] = None   # leading wrapper tag, if any

    @property
    def is_prompt(self) -> bool:
        return self.kind == "prompt"


def leading_tag(text: str) -> Optional[str]:
    m = _LEADING_TAG.match(text)
    return m.group(1) if m else None


def _is_wrapper_tag(tag: str) -> bool:
    # Known wrappers, plus any hyphenated tag: Claude Code's wrappers are all
    # kebab-case, so new ones are caught without a code change. Real prompts
    # rarely *start* with a hyphenated tag.
    return tag not in HUMAN_TAGS and (tag in KNOWN_WRAPPER_TAGS or "-" in tag)


def classify_user(obj: dict) -> UserEntry:
    """Classify a ``type: user`` line."""
    if obj.get("isCompactSummary"):
        return UserEntry("compact", "")
    msg = obj.get("message") or {}
    bl = blocks(msg.get("content"))
    texts, images, results = [], 0, 0
    for b in bl:
        bt = b.get("type")
        if bt == "tool_result":
            results += 1
        elif bt in ("image", "document"):
            images += 1
        else:
            t = block_text(b)
            if t:
                texts.append(t)
    text = "\n".join(texts).strip()
    if not text and not images:
        return UserEntry("tool_result" if results else "meta", "")
    tag = leading_tag(text) if text else None
    if obj.get("isMeta"):
        return UserEntry("meta", text, images, tag)
    if tag and _is_wrapper_tag(tag):
        if tag in ("command-name", "command-message", "command-args"):
            name = _tag_inner(text, "command-name") or ""
            if not name:
                name = "/" + (_tag_inner(text, "command-message") or "")
            args = _tag_inner(text, "command-args") or ""
            return UserEntry("command", f"{name} {args}".strip(), images, tag)
        if tag == "bash-input":
            return UserEntry("bash", _tag_inner(text, "bash-input") or "", images, tag)
        if tag == "task-notification":
            summary = (_tag_inner(text, "summary") or _tag_inner(text, "status")
                       or strip_tags(text)[:200])
            return UserEntry("notification", summary, images, tag)
        return UserEntry("meta", text, images, tag)
    if text.startswith(_LEGACY_PREFIXES):
        return UserEntry("meta", text, images)
    return UserEntry("prompt", text, images, tag)


# ---- tool calls ------------------------------------------------------------

def _first_line(s: str, n: int = 120) -> str:
    s = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return s if len(s) <= n else s[: n - 1] + "…"


def short_path(p: str) -> str:
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p and p.startswith(home) else (p or "")


def tool_summary(name: str, inp: dict) -> tuple[str, str]:
    """Human-friendly (label, detail) for a tool call. Never raises.

    Known tools get a tailored one-liner; anything else (new built-ins, MCP
    tools) falls back to the first informative string argument.
    """
    inp = inp if isinstance(inp, dict) else {}
    g = lambda k: inp.get(k) if isinstance(inp.get(k), str) else ""
    try:
        if name == "Bash":
            cmd = _first_line(g("command"), 100)
            desc = g("description")
            return "Bash", f"{cmd}" + (f"   # {desc}" if desc else "")
        if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
            return name, short_path(g("file_path") or g("notebook_path"))
        if name == "Grep":
            where = short_path(g("path"))
            return "Grep", f"/{g('pattern')}/" + (f" in {where}" if where else "")
        if name == "Glob":
            return "Glob", g("pattern") + (f" in {short_path(g('path'))}" if g("path") else "")
        if name == "WebFetch":
            return "Fetch", g("url")
        if name == "WebSearch":
            return "Search", g("query")
        if name in ("Agent", "Task"):
            st = g("subagent_type")
            return "Agent", g("description") + (f"  ({st})" if st else "")
        if name == "Skill":
            return "Skill", g("skill")
        if name == "AskUserQuestion":
            qs = inp.get("questions") or []
            q = qs[0].get("question", "") if qs and isinstance(qs[0], dict) else ""
            return "Ask", _first_line(q)
        if name in ("TaskCreate", "TaskUpdate", "TodoWrite"):
            detail = g("subject") or g("content") or g("status")
            if not detail and isinstance(inp.get("todos"), list):
                detail = f"{len(inp['todos'])} todos"
            return name, _first_line(detail)
        if name.startswith("mcp__"):
            parts = name.split("__")
            server = parts[1] if len(parts) > 1 else "mcp"
            tool = "__".join(parts[2:]) or name
            return f"{server}·{tool}", _generic_detail(inp)
    except Exception:
        pass
    return name or "tool", _generic_detail(inp)


def _generic_detail(inp: dict) -> str:
    preferred = ("description", "query", "command", "file_path", "path", "url",
                 "prompt", "subject", "name", "pattern")
    for k in preferred:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return _first_line(v)
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return _first_line(v)
    return ""


def tool_search_text(name: str, inp: dict, limit: int = 400) -> str:
    """Flattened ``[Tool] key=val …`` string for full-text search."""
    parts = []
    for k, v in (inp or {}).items():
        if isinstance(v, (str, int, float, bool)):
            parts.append(f"{k}={v}")
        elif isinstance(v, (list, dict)):
            parts.append(f"{k}={json.dumps(v, ensure_ascii=False)[:120]}")
    return f"[{name}] {' '.join(parts)}"[:limit]


# ---- drift -----------------------------------------------------------------

@dataclass
class Drift:
    """Format elements ccs doesn't know about yet, with counts."""
    line_types: dict = field(default_factory=dict)
    system_subtypes: dict = field(default_factory=dict)
    block_types: dict = field(default_factory=dict)
    wrapper_tags: dict = field(default_factory=dict)
    max_version: str = ""

    def _bump(self, bucket: dict, key) -> None:
        bucket[str(key)] = bucket.get(str(key), 0) + 1

    def observe(self, obj: dict) -> None:
        t = obj.get("type")
        v = obj.get("version")
        if isinstance(v, str) and version_key(v) > version_key(self.max_version):
            self.max_version = v
        if t not in KNOWN_LINE_TYPES:
            self._bump(self.line_types, t)
        elif t == "system" and obj.get("subtype") not in KNOWN_SYSTEM_SUBTYPES:
            self._bump(self.system_subtypes, obj.get("subtype"))
        elif t in ("user", "assistant"):
            for b in blocks((obj.get("message") or {}).get("content")):
                if b.get("type") not in KNOWN_BLOCK_TYPES:
                    self._bump(self.block_types, b.get("type"))
            if t == "user":
                c = (obj.get("message") or {}).get("content")
                text = c if isinstance(c, str) else next(
                    (block_text(b) for b in blocks(c) if block_text(b)), "")
                tag = leading_tag(text) if text else None
                if tag and tag not in KNOWN_WRAPPER_TAGS and tag not in HUMAN_TAGS:
                    self._bump(self.wrapper_tags, tag)

    def merge(self, other: "Drift") -> None:
        for name in ("line_types", "system_subtypes", "block_types", "wrapper_tags"):
            mine = getattr(self, name)
            for k, n in getattr(other, name).items():
                mine[k] = mine.get(k, 0) + n
        if version_key(other.max_version) > version_key(self.max_version):
            self.max_version = other.max_version

    def unknown_count(self) -> int:
        return sum(len(getattr(self, n)) for n in
                   ("line_types", "system_subtypes", "block_types", "wrapper_tags"))

    def newer_than_verified(self) -> bool:
        return version_key(self.max_version) > version_key(VERIFIED_CC_VERSION)

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, s: Optional[str]) -> "Drift":
        d = cls()
        try:
            for k, v in (json.loads(s) if s else {}).items():
                if hasattr(d, k):
                    setattr(d, k, v)
        except (ValueError, TypeError):
            pass
        return d


def version_key(v: Optional[str]) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])
