"""Small formatting helpers shared by the TUI and CLI."""
from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def rel_time(ts: str | None, now: datetime | None = None) -> str:
    dt = parse_ts(ts)
    if not dt:
        return "—"
    now = now or datetime.now(timezone.utc)
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
    now = now or datetime.now(timezone.utc)
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
