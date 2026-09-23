"""Small formatting helpers shared by the TUI and CLI."""
from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp into an aware datetime in the *local* timezone."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone()


def clock(ts: str | None) -> str:
    """Local wall-clock time, e.g. '09:41'."""
    dt = parse_ts(ts)
    return dt.strftime("%H:%M") if dt else ""


def long_date(ts: str | None) -> str:
    """e.g. 'Tue 23 Sep 2026'."""
    dt = parse_ts(ts)
    return dt.strftime("%a %d %b %Y") if dt else ""


def fmt_tokens(n: int | None) -> str:
    if not n:
        return "0"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M"


def display_title(r: dict) -> str:
    """Best human title: /rename > Claude's auto title > first prompt."""
    t = (r.get("custom_title") or r.get("ai_title") or r.get("first_prompt")
         or r.get("agent_name") or "(no prompt)")
    return " ".join(str(t).split())


def rel_time(ts: str | None, now: datetime | None = None) -> str:
    dt = parse_ts(ts)
    if not dt:
        return "—"
    now = _now(now)
    delta = now - dt
    secs = delta.total_seconds()
    if secs < 0:
        secs = 0
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)}d"
    # Older than a week: show a calendar date.
    if dt.year == now.year:
        return dt.strftime("%b %d")
    return dt.strftime("%b %Y")


def fmt_duration(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds >= 86400:
        d, h = seconds // 86400, (seconds % 86400) // 3600
        return f"{d}d{h}h" if h else f"{d}d"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m:02d}" if m else f"{h}h"


_MODEL_SHORT = {
    "opus": "opus",
    "sonnet": "son",
    "haiku": "haiku",
    "fable": "fable",
}


def short_model(models: str | None) -> str:
    """'claude-opus-4-8,claude-opus-4-6' -> 'opus4.8+'."""
    if not models:
        return "—"
    parts = [m.strip() for m in models.split(",") if m.strip()]
    if not parts:
        return "—"
    first = parts[0]
    label = first
    for token, short in _MODEL_SHORT.items():
        if token in first:
            # claude-opus-4-8[1m] -> opus4.8
            tail = first.split(token, 1)[1]
            digits = "".join(c if c.isdigit() else "." for c in tail).strip(".")
            digits = ".".join(d for d in digits.split(".") if d)[:3]
            label = f"{short}{digits}" if digits else short
            break
    if len(parts) > 1:
        label += "+"
    return label


def day_bucket(ts: str | None, now: datetime | None = None) -> str:
    dt = parse_ts(ts)
    if not dt:
        return "unknown"
    now = _now(now)
    d = (now.date() - dt.date()).days
    if d <= 0:
        return "Today"
    if d == 1:
        return "Yesterday"
    if d < 7:
        return "This week"
    if d < 30:
        return "This month"
    if dt.year == now.year:
        return dt.strftime("%B")
    return dt.strftime("%B %Y")
