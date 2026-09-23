"""CLI entry point for ccs.

    ccs                 launch the TUI
    ccs index [--rebuild]   (re)build the index and exit
    ccs search "<desc>"     run the AI advanced search from the shell
    ccs stats               quick summary of the index
    ccs doctor              check Claude Code compatibility / format drift
"""
from __future__ import annotations

import argparse
import sys

import os
import re
import shutil
import subprocess

from . import __version__, ccformat, db, indexer, live


def cmd_index(args) -> int:
    conn = db.connect()

    def prog(i, t):
        print(f"\r  indexed {i}/{t}", end="", flush=True)

    changed = indexer.index_all(conn, changed_only=not args.rebuild, progress_cb=prog)
    print()
    n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print(f"done — {len(changed)} sessions (re)indexed, {n} total")
    _drift_hint(conn)
    return 0


def _drift_hint(conn) -> None:
    d = indexer.collect_drift(conn)
    if d.unknown_count() or d.newer_than_verified():
        print("⚠ transcripts contain format elements ccs doesn't know yet — run `ccs doctor`")


def cmd_search(args) -> int:
    from . import aisearch

    conn = db.connect()
    query = " ".join(args.query)
    try:
        matches = aisearch.ai_search(conn, query, model=args.model)
    except aisearch.AISearchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not matches:
        print("no matches")
        return 0
    for m in matches:
        date = (m.last_ts or "")[:10]
        print(f"[{m.score:>3}] {m.project or '?':<16} {date}  {m.id}")
        print(f"      {m.title or ''}")
        print(f"      → {m.reason}")
    return 0


def cmd_stats(args) -> int:
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    print(f"sessions: {n}   indexed messages: {fts}")
    print("\ntop projects:")
    for r in conn.execute(
        "SELECT project, COUNT(*) c FROM sessions GROUP BY project ORDER BY c DESC LIMIT 12"
    ):
        print(f"  {r['c']:>4}  {r['project']}")
    _drift_hint(conn)
    return 0


def _claude_version() -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True,
                             text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else ""


def cmd_doctor(args) -> int:
    """Scan every transcript fresh and report what ccs doesn't understand."""
    ok = True
    print(f"ccs {__version__} · parser v{ccformat.PARSER_VERSION} · "
          f"format verified against Claude Code {ccformat.VERIFIED_CC_VERSION}\n")

    # 1. the claude CLI
    if not shutil.which("claude"):
        print("✗ `claude` not on PATH — resume and AI search won't work")
        ok = False
    else:
        v = _claude_version()
        newer = ccformat.version_key(v) > ccformat.version_key(ccformat.VERIFIED_CC_VERSION)
        print(f"{'!' if newer else '✓'} claude CLI {v or '(unknown version)'}"
              + ("  — newer than verified; formats may have changed" if newer else ""))
        try:
            helptext = subprocess.run(["claude", "--help"], capture_output=True,
                                      text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            helptext = ""
        for flag in ("--resume", "--print", "--output-format", "--model",
                     "--no-session-persistence"):
            if helptext and flag not in helptext:
                print(f"✗ `claude --help` no longer lists {flag} (used by ccs)")
                ok = False

    # 2. the live-session registry
    if os.path.isdir(live.SESSIONS_DIR):
        n = len(live.open_sessions())
        print(f"✓ live registry {live.SESSIONS_DIR} ({n} open interactive session(s))")
    else:
        print(f"! no live registry at {live.SESSIONS_DIR} — using the legacy process heuristic")

    # 3. transcript format drift
    drift = ccformat.Drift()
    examples: dict[tuple[str, str], str] = {}
    files = list(indexer.iter_session_files(db.DEFAULT_PROJECTS_DIR))
    for _, path in files:
        before = {(k, n) for k in ("line_types", "system_subtypes", "block_types",
                                   "wrapper_tags") for n in getattr(drift, k)}
        for obj in ccformat.iter_records(path):
            drift.observe(obj)
        for k in ("line_types", "system_subtypes", "block_types", "wrapper_tags"):
            for name in getattr(drift, k):
                if (k, name) not in before:
                    examples.setdefault((k, name), str(path))
    print(f"✓ scanned {len(files)} transcripts (newest written by Claude Code "
          f"{drift.max_version or '?'})")
    labels = {"line_types": "line type", "system_subtypes": "system subtype",
              "block_types": "content block", "wrapper_tags": "user-text wrapper tag"}
    if drift.unknown_count():
        ok = False
        print("\nUnknown format elements (not understood by ccs yet):")
        for k, label in labels.items():
            for name, cnt in sorted(getattr(drift, k).items(), key=lambda x: -x[1]):
                print(f"  {label:<22} {name!s:<32} ×{cnt:<6} e.g. {examples.get((k, name), '')}")
        print("\nTeach ccs about them in ccs/ccformat.py (KNOWN_* sets / classify_user /"
              "\ntool_summary), bump PARSER_VERSION if parsed output changes, then"
              "\nupdate VERIFIED_CC_VERSION.")
    else:
        print("✓ no unknown line types, system subtypes, block types or wrapper tags")
    print("\n" + ("all good" if ok else "attention needed"))
    return 0 if ok else 1


def cmd_tui(args) -> int:
    from .tui import CCSApp

    CCSApp().run()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ccs", description="Find, browse and resume Claude Code sessions.")
    p.add_argument("--version", action="version", version=f"ccs {__version__}")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("index", help="(re)build the session index")
    pi.add_argument("--rebuild", action="store_true", help="reparse every session, not just changed")
    pi.set_defaults(func=cmd_index)

    ps = sub.add_parser("search", help="AI advanced search from the shell")
    ps.add_argument("query", nargs="+")
    ps.add_argument("--model", default=None)
    ps.set_defaults(func=cmd_search)

    pt = sub.add_parser("stats", help="summary of the index")
    pt.set_defaults(func=cmd_stats)

    pd = sub.add_parser("doctor", help="check Claude Code compatibility / format drift")
    pd.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    if args.cmd == "search" and args.model is None:
        from .aisearch import DEFAULT_MODEL
        args.model = DEFAULT_MODEL
    if not getattr(args, "func", None):
        return cmd_tui(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
