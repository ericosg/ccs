"""CLI entry point for ccs.

    ccs                 launch the TUI
    ccs index [--rebuild]   (re)build the index and exit
    ccs search "<desc>"     run the AI advanced search from the shell
    ccs stats               quick summary of the index
"""
from __future__ import annotations

import argparse
import sys

from . import __version__, db, indexer


def cmd_index(args) -> int:
    conn = db.connect()

    def prog(i, t):
        print(f"\r  indexed {i}/{t}", end="", flush=True)

    changed = indexer.index_all(conn, changed_only=not args.rebuild, progress_cb=prog)
    print()
    n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print(f"done — {len(changed)} sessions (re)indexed, {n} total")
    return 0


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
    return 0


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

    args = p.parse_args(argv)
    if args.cmd == "search" and args.model is None:
        from .aisearch import DEFAULT_MODEL
        args.model = DEFAULT_MODEL
    if not getattr(args, "func", None):
        return cmd_tui(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
