"""Detect which sessions are *currently open* in a running Claude Code process.

Claude Code maintains a live registry at ``~/.claude/sessions/<pid>.json``, one
file per running interactive session, e.g.:

    {"pid":12345, "sessionId":"a1b2c3…", "cwd":"…/my-project",
     "kind":"interactive", "status":"waiting", "name":"…", …}

This is authoritative: it gives the **exact** session id and a live **status**
(`busy` = model working, `waiting` = wants your input, `idle` = open/inactive).
Files can linger after a crash, so we verify the pid is still alive.

If the registry dir is absent (older Claude Code), we fall back to the previous
heuristic: running `claude` processes → cwd → most-recently-active session(s).
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from collections import Counter

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    except (OSError, TypeError):
        return False
    return True


def open_sessions(conn=None) -> dict[str, str]:
    """Return {session_id: status} for sessions open right now.

    status is one of 'busy' | 'waiting' | 'idle' (or 'open' if unspecified).
    """
    if os.path.isdir(SESSIONS_DIR):
        out: dict[str, str] = {}
        for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
            try:
                o = json.load(open(f))
            except (OSError, ValueError):
                continue
            if o.get("kind") != "interactive":
                continue
            sid, pid = o.get("sessionId"), o.get("pid")
            if not sid or not pid or not _pid_alive(pid):
                continue
            out[sid] = o.get("status") or "open"
        return out
    # Fallback for older Claude Code without the registry.
    if conn is not None:
        return {sid: "open" for sid in _legacy_open_ids(conn)}
    return {}


def open_session_ids(conn=None) -> set[str]:
    return set(open_sessions(conn))


# ---- legacy fallback (process cwd + recency) --------------------------------

def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _legacy_open_ids(conn) -> set[str]:
    if not shutil.which("pgrep"):
        return set()
    pids = _run(["pgrep", "-x", "claude"]).split()
    if not pids:
        return set()
    keep = []
    info = _run(["ps", "-o", "pid=,args=", "-p", ",".join(pids)])
    argv = {}
    for line in info.splitlines():
        pid, _, args = line.strip().partition(" ")
        argv[pid] = args
    for pid in pids:
        a = argv.get(pid, "")
        if " -p " in f" {a} " or "--print" in a:
            continue
        keep.append(pid)
    if not keep or not shutil.which("lsof"):
        return set()
    out = _run(["lsof", "-a", "-d", "cwd", "-p", ",".join(keep), "-Fn"])
    cwds = [l[1:] for l in out.splitlines() if l.startswith("n")]
    active: set[str] = set()
    for cwd, k in Counter(cwds).items():
        rows = conn.execute(
            "SELECT id FROM sessions WHERE cwd = ? ORDER BY last_ts DESC LIMIT ?",
            (cwd, k),
        ).fetchall()
        active.update(r["id"] for r in rows)
    return active
