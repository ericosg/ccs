"""Render a session transcript as a readable conversation for the preview pane.

Reads the ``.jsonl`` fresh (so an active session shows its latest turns) and
turns it into a chat-style view:

* a header card (title, where, when, how long, model, recap, PRs, fork),
* **You** prompts as plain text, **Claude** replies rendered as Markdown,
* each Claude run's tool calls as compact one-liners (``Bash  git status``),
  failed calls marked ✗, long runs collapsed to a summary,
* harness events (slash commands, background-task notices, compaction) as
  dim one-line markers instead of raw XML,
* day separators and local clock times.

Everything format-specific comes from ``ccformat``.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from rich.console import Group, RenderableType
from rich.markdown import Heading, Markdown
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from . import ccformat as cf
from . import util

_MAX_BLOCKS = 200          # most recent conversation blocks shown
_MAX_PROMPT_CHARS = 2500
_MAX_REPLY_CHARS = 4000
_MAX_TOOLS_SHOWN = 6       # per consecutive run of tool calls

YOU = "bold #5fafff"
CLAUDE = "bold #87d787"
DIM = "grey50"
TOOL = "#d7af5f"


class _LeftHeading(Heading):
    LEVEL_ALIGN = {h: "left" for h in ("h1", "h2", "h3", "h4", "h5", "h6")}


class _Markdown(Markdown):
    """Markdown with left-aligned headings (centred boxes look odd in a pane)."""
    elements = {**Markdown.elements, "heading_open": _LeftHeading}


# ---- model -----------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    detail: str
    id: Optional[str] = None


@dataclass
class Block:
    kind: str                     # you | claude | command | bash | notice | compact
    ts: Optional[str] = None
    text: str = ""
    images: int = 0
    model: Optional[str] = None
    # claude blocks: ordered parts, each ("text", str) or ("tools", [ToolCall])
    parts: list = field(default_factory=list)

    def add_text(self, t: str) -> None:
        self.parts.append(("text", t))

    def add_tool(self, call: ToolCall) -> None:
        if self.parts and self.parts[-1][0] == "tools":
            self.parts[-1][1].append(call)
        else:
            self.parts.append(("tools", [call]))


@dataclass
class Transcript:
    blocks: list[Block] = field(default_factory=list)
    failed_tools: set = field(default_factory=set)
    title: Optional[str] = None
    ai_title: Optional[str] = None
    cwd: Optional[str] = None
    branch: Optional[str] = None
    recap: Optional[str] = None
    prs: list = field(default_factory=list)
    models: list = field(default_factory=list)
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    prompts: int = 0
    tokens: dict = field(default_factory=dict)


def load(path: Path) -> Transcript:
    tr = Transcript()
    cur: Optional[Block] = None   # the Claude block being accumulated

    for obj in cf.iter_records(path):
        typ = obj.get("type")
        ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
        if ts:
            tr.first_ts = tr.first_ts or ts
            tr.last_ts = ts
        tr.cwd = tr.cwd or obj.get("cwd")
        if obj.get("gitBranch"):
            tr.branch = obj["gitBranch"]
        if typ == "custom-title":
            tr.title = obj.get("customTitle") or tr.title
        elif typ == "ai-title":
            tr.ai_title = obj.get("aiTitle") or tr.ai_title
        elif typ == "pr-link":
            link = cf.pr_link(obj)
            if link and link not in tr.prs:
                tr.prs.append(link)
        if obj.get("isSidechain"):
            continue

        if typ == "system":
            sub = obj.get("subtype")
            if sub == "away_summary" and isinstance(obj.get("content"), str):
                tr.recap = obj["content"].strip()
            elif sub == "compact_boundary":
                tr.blocks.append(Block("compact", ts))
                cur = None
            elif sub == "api_error":
                tr.blocks.append(Block("notice", ts, "API error — retried"))
            continue

        if typ == "user":
            msg = obj.get("message") or {}
            for b in cf.blocks(msg.get("content")):
                if b.get("type") == "tool_result" and b.get("is_error"):
                    tr.failed_tools.add(b.get("tool_use_id"))
            e = cf.classify_user(obj)
            if e.kind == "prompt":
                tr.prompts += 1
                tr.blocks.append(Block("you", ts, e.text, e.images))
                cur = None
            elif e.kind == "command":
                tr.blocks.append(Block("command", ts, e.text))
                cur = None
            elif e.kind == "bash":
                tr.blocks.append(Block("bash", ts, e.text))
                cur = None
            elif e.kind == "notification":
                tr.blocks.append(Block("notice", ts, e.text))
            elif e.kind == "compact":
                tr.blocks.append(Block("compact", ts))
                cur = None
            continue

        if typ == "assistant":
            msg = obj.get("message") or {}
            model = msg.get("model")
            if model and model != "<synthetic>" and model not in tr.models:
                tr.models.append(model)
            mid = msg.get("id")
            if mid:
                tr.tokens[mid] = (msg.get("usage") or {}).get("output_tokens") or 0
            if cur is None:
                cur = Block("claude", ts, model=model if model != "<synthetic>" else None)
                tr.blocks.append(cur)
            for b in cf.blocks(msg.get("content")):
                bt = b.get("type")
                if bt == "tool_use":
                    label, detail = cf.tool_summary(b.get("name") or "tool", b.get("input") or {})
                    cur.add_tool(ToolCall(label, detail, b.get("id")))
                elif bt in ("thinking", "redacted_thinking", "tool_result"):
                    continue
                else:
                    t = cf.block_text(b)
                    if t:
                        cur.add_text(t)
    # Drop Claude blocks that only had thinking.
    tr.blocks = [b for b in tr.blocks if b.kind != "claude" or b.parts]
    return tr


# ---- rendering -------------------------------------------------------------

def _truncate_md(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if cut.count("```") % 2:      # don't leave a code fence open
        cut += "\n```"
    return cut + "\n\n*… (truncated)*"


def _highlight(t: Text, words: Iterable[str]) -> Text:
    for w in words:
        if len(w) >= 2:
            t.highlight_words([w], style="black on #d7af00", case_sensitive=False)
    return t


def _header(tr: Transcript, fork_note: Optional[str]) -> RenderableType:
    title = tr.title or tr.ai_title
    if not title:
        first = next((b for b in tr.blocks if b.kind == "you" and b.text), None)
        title = " ".join(first.text.split())[:120] if first else "(untitled session)"
    out: list[RenderableType] = [Text(title, style="bold")]

    where = Text()
    if tr.cwd:
        where.append(cf.short_path(tr.cwd), style="#87afd7")
    if tr.branch and tr.branch != "HEAD":
        where.append(f"  ⎇ {tr.branch}", style="#af87d7")
    if where:
        out.append(where)

    facts = Table.grid(padding=(0, 1))
    facts.add_column(style=DIM, no_wrap=True)
    facts.add_column()
    if tr.first_ts:
        span = f"{util.long_date(tr.first_ts)} {util.clock(tr.first_ts)}"
        if tr.last_ts and util.long_date(tr.last_ts) != util.long_date(tr.first_ts):
            span += f" → {util.long_date(tr.last_ts)} {util.clock(tr.last_ts)}"
        elif tr.last_ts:
            span += f"–{util.clock(tr.last_ts)}"
        dur = _duration(tr.first_ts, tr.last_ts)
        facts.add_row("when", span + (f"  ({dur})" if dur else ""))
    stats = f"{tr.prompts} prompts · {sum(1 for _ in tr.tokens)} replies"
    tok = sum(tr.tokens.values())
    if tok:
        stats += f" · {util.fmt_tokens(tok)} tokens out"
    facts.add_row("activity", stats)
    if tr.models:
        facts.add_row("model", ", ".join(util.short_model(m) for m in tr.models))
    for pr in tr.prs[:3]:
        facts.add_row("PR", Text(pr, style="underline #87afd7"))
    if fork_note:
        facts.add_row("fork", Text(fork_note, style="#af87ff"))
    out.append(facts)
    if tr.recap:
        out.append(Text(""))
        out.append(Padding(Text(tr.recap, style="italic #bcbcbc"), (0, 1), style="on #262626"))
    out.append(Rule(style="grey30"))
    return Group(*out)


def _duration(a: Optional[str], b: Optional[str]) -> str:
    da, db_ = util.parse_ts(a), util.parse_ts(b)
    if not da or not db_:
        return ""
    return util.fmt_duration(int((db_ - da).total_seconds()))


def _tools(calls: list[ToolCall], failed: set, words) -> RenderableType:
    rows: list[RenderableType] = []
    shown = calls if len(calls) <= _MAX_TOOLS_SHOWN else calls[: _MAX_TOOLS_SHOWN - 1]
    for c in shown:
        bad = c.id in failed
        t = Text("  ")
        t.append("✗ " if bad else "⏺ ", style="bold red" if bad else TOOL)
        t.append(c.name, style="bold red" if bad else f"bold {TOOL}")
        if c.detail:
            t.append("  " + c.detail, style=DIM)
        t.no_wrap = True
        t.overflow = "ellipsis"
        rows.append(_highlight(t, words))
    rest = calls[len(shown):]
    if rest:
        counts = Counter(c.name for c in rest)
        summary = ", ".join(f"{n}×{k}" if n > 1 else k for k, n in counts.most_common(5))
        rows.append(Text(f"  … +{len(rest)} more: {summary}", style=DIM))
    return Group(*rows)


def _block(b: Block, failed: set, words) -> RenderableType:
    clock = util.clock(b.ts)
    if b.kind == "you":
        head = Text("You", style=YOU)
        head.append(f"  {clock}", style=DIM)
        body = b.text if len(b.text) <= _MAX_PROMPT_CHARS else b.text[:_MAX_PROMPT_CHARS] + " …"
        body_t = _highlight(Text(body), words)
        if b.images:
            body_t.append(f"\n[{b.images} attachment{'s' if b.images > 1 else ''}]", style=DIM)
        return Group(head, Padding(body_t, (0, 0, 1, 2)))
    if b.kind == "claude":
        head = Text("Claude", style=CLAUDE)
        head.append(f"  {clock}", style=DIM)
        if b.model:
            head.append(f" · {util.short_model(b.model)}", style=DIM)
        parts: list[RenderableType] = [head]
        for kind, val in b.parts:
            if kind == "text":
                parts.append(Padding(_Markdown(_truncate_md(val, _MAX_REPLY_CHARS),
                                               code_theme="monokai"), (0, 0, 0, 2)))
            else:
                parts.append(_tools(val, failed, words))
        parts.append(Text(""))
        return Group(*parts)
    if b.kind == "command":
        t = Text("  ⌘ ", style="#af87d7")
        t.append(b.text or "/command", style="#af87d7")
        t.append(f"  {clock}", style=DIM)
        return t
    if b.kind == "bash":
        t = Text("  $ ", style=TOOL)
        t.append(b.text, style=TOOL)
        t.append(f"  {clock}", style=DIM)
        return t
    if b.kind == "notice":
        t = Text(f"  ⚑ {' '.join(b.text.split())}", style=DIM)
        t.append(f"  {clock}", style=DIM)
        return t
    if b.kind == "compact":
        return Rule(f"conversation compacted · {clock}", style="grey30", characters="┄")
    return Text(b.text)


def render_session(path, fork_note: Optional[str] = None,
                   highlight: Iterable[str] = ()) -> RenderableType:
    path = Path(path)
    if not path.exists():
        return Text("(session file not found)", style="red")
    try:
        tr = load(path)
    except OSError as e:
        return Text(f"(could not read session: {e})", style="red")

    words = [w for w in highlight if w]
    out: list[RenderableType] = [_header(tr, fork_note)]
    blocks = tr.blocks
    if len(blocks) > _MAX_BLOCKS:
        out.append(Text(f"… {len(blocks) - _MAX_BLOCKS} earlier items hidden …\n",
                        style="dim italic"))
        blocks = blocks[-_MAX_BLOCKS:]
    if not blocks:
        out.append(Text("(no messages yet)", style=DIM))
    last_day = None
    for b in blocks:
        day = util.long_date(b.ts)
        if day and day != last_day:
            if last_day is not None:
                out.append(Rule(day, style="grey30", characters="─"))
            last_day = day
        out.append(_block(b, tr.failed_tools, words))
    return Group(*out)


def plain_text(path) -> str:
    """Render to plain text (for tests / debugging)."""
    import io

    from rich.console import Console
    c = Console(width=100, record=True, color_system=None, file=io.StringIO())
    c.print(render_session(path))
    return re.sub(r"[ \t]+\n", "\n", c.export_text())
