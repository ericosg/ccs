"""Unit tests on synthetic transcripts (no real ~/.claude needed).

Run:  PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ccs import aisearch, ccformat as cf, db, indexer, preview, search, util

SID = "11111111-2222-3333-4444-555555555555"
PARENT = "99999999-2222-3333-4444-555555555555"


def _u(content, ts, **kw):
    return {"type": "user", "timestamp": ts, "cwd": "/tmp/proj", "gitBranch": "main",
            "version": "2.1.280", "message": {"role": "user", "content": content}, **kw}


def _a(mid, blocks, ts, out=10, model="claude-opus-5-5"):
    return {"type": "assistant", "timestamp": ts, "cwd": "/tmp/proj",
            "message": {"id": mid, "model": model, "content": blocks,
                        "usage": {"output_tokens": out}}}


LINES = [
    _u("fix the login bug in auth.py", "2026-09-20T10:00:00Z", forkedFrom={"sessionId": PARENT}),
    # one assistant message split over two lines, usage repeated on each
    _a("m1", [{"type": "thinking", "thinking": "hmm"}], "2026-09-20T10:00:05Z", out=50),
    _a("m1", [{"type": "text", "text": "## Plan\nI'll read the file."},
              {"type": "tool_use", "id": "t1", "name": "Read",
               "input": {"file_path": "/tmp/proj/auth.py"}}], "2026-09-20T10:00:06Z", out=50),
    _u([{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}],
       "2026-09-20T10:00:07Z"),
    _a("m2", [{"type": "tool_use", "id": "t2", "name": "mcp__gmail__search_emails",
               "input": {"query": "from:bob"}}], "2026-09-20T10:00:08Z", out=20),
    # harness noise that must not count as prompts
    _u("<command-name>/model</command-name>\n<command-message>model</command-message>\n"
       "<command-args>opus</command-args>", "2026-09-20T10:01:00Z"),
    _u("<local-command-stdout>Set model</local-command-stdout>", "2026-09-20T10:01:01Z"),
    _u("<task-notification>\n<status>completed</status>\n<summary>Build finished</summary>\n"
       "</task-notification>", "2026-09-20T10:02:00Z"),
    _u("<bash-input>git status</bash-input>", "2026-09-20T10:02:30Z"),
    _u("<brand-new-wrapper>x</brand-new-wrapper>", "2026-09-20T10:02:40Z"),
    _u("skill body…", "2026-09-20T10:02:50Z", isMeta=True),
    _u("This session is being continued from a previous conversation…",
       "2026-09-20T10:03:00Z", isCompactSummary=True),
    _u("now add a διόρθωση test", "2026-09-20T11:00:00Z"),
    _a("m3", [{"type": "text", "text": "Done."}], "2026-09-20T11:00:10Z", out=5),
    {"type": "ai-title", "aiTitle": "Fix login bug"},
    {"type": "pr-link", "prNumber": 5, "prUrl": "https://github.com/o/r/pull/5"},
    {"type": "system", "subtype": "away_summary", "content": "Fixed the login bug.",
     "timestamp": "2026-09-20T11:05:00Z"},
    {"type": "shiny-new-line-type", "timestamp": "2026-09-20T11:06:00Z"},
    _u("sidechain chatter", "2026-09-20T11:07:00Z", isSidechain=True),
]


class FixtureMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "projects"
        proj = self.projects / "-tmp-proj"
        proj.mkdir(parents=True)
        self.path = proj / f"{SID}.jsonl"
        self.path.write_text("\n".join(json.dumps(l) for l in LINES) + "\n{corrupt\n")
        # noise that must NOT be indexed as sessions (depth > 1)
        (proj / SID / "subagents").mkdir(parents=True)
        (proj / SID / "subagents" / "agent-x.jsonl").write_text(json.dumps(LINES[0]))
        self.db_path = root / "ccs.db"

    def tearDown(self):
        self.tmp.cleanup()


class TestClassify(unittest.TestCase):
    def kind(self, content, **kw):
        return cf.classify_user(_u(content, "2026-01-01T00:00:00Z", **kw)).kind

    def test_kinds(self):
        self.assertEqual(self.kind("hello"), "prompt")
        self.assertEqual(self.kind("<command-name>/x</command-name>"), "command")
        self.assertEqual(self.kind("<task-notification><summary>s</summary></task-notification>"),
                         "notification")
        self.assertEqual(self.kind("<bash-input>ls</bash-input>"), "bash")
        self.assertEqual(self.kind("<system-reminder>x</system-reminder>"), "meta")
        self.assertEqual(self.kind("<some-future-tag>x</some-future-tag>"), "meta")
        self.assertEqual(self.kind("<pasted_content>my paste</pasted_content>"), "prompt")
        self.assertEqual(self.kind("<div>html question</div>"), "prompt")
        self.assertEqual(self.kind("x", isMeta=True), "meta")
        self.assertEqual(self.kind("x", isCompactSummary=True), "compact")
        self.assertEqual(self.kind([{"type": "tool_result", "content": "r"}]), "tool_result")

    def test_tool_summary_fallbacks(self):
        self.assertEqual(cf.tool_summary("Bash", {"command": "ls -la\nmore", "description": "list"}),
                         ("Bash", "ls -la   # list"))
        self.assertEqual(cf.tool_summary("mcp__srv__do_it", {"query": "q"}), ("srv·do_it", "q"))
        self.assertEqual(cf.tool_summary("FutureTool", {"n": 1, "thing": "abc"}), ("FutureTool", "abc"))
        self.assertEqual(cf.tool_summary("Weird", "not-a-dict"), ("Weird", ""))


class TestIndexer(FixtureMixin, unittest.TestCase):
    def test_parse_and_index(self):
        conn = db.connect(self.db_path)
        changed = indexer.index_all(conn, self.projects)
        self.assertEqual(changed, [SID])  # subagent file ignored
        r = dict(conn.execute("SELECT * FROM sessions").fetchone())
        self.assertEqual(r["turns"], 2)                 # only the two real prompts
        self.assertEqual(r["first_prompt"], "fix the login bug in auth.py")
        self.assertEqual(r["assistant_turns"], 3)       # m1 split lines count once
        self.assertEqual(r["out_tokens"], 75)           # 50 (not 100) + 20 + 5
        self.assertEqual(r["tool_calls"], 2)
        self.assertEqual(r["ai_title"], "Fix login bug")
        self.assertEqual(r["recap"], "Fixed the login bug.")
        self.assertEqual(r["pr_links"], "https://github.com/o/r/pull/5")
        self.assertEqual(r["forked_from"], PARENT)
        self.assertEqual(r["project"], "proj")
        self.assertEqual(util.display_title(r), "Fix login bug")
        drift = cf.Drift.from_json(db.get_meta(conn, "drift"))
        self.assertIn("shiny-new-line-type", drift.line_types)
        self.assertIn("brand-new-wrapper", drift.wrapper_tags)
        # incremental: nothing changed → nothing re-parsed
        self.assertEqual(indexer.index_all(conn, self.projects), [])
        # parser upgrade → full re-parse even though mtime is unchanged
        db.set_meta(conn, "parser_version", "old")
        self.assertEqual(indexer.index_all(conn, self.projects), [SID])
        # vanished file → dropped
        self.path.unlink()
        self.assertEqual(indexer.index_all(conn, self.projects), [SID])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_text_search(self):
        conn = db.connect(self.db_path)
        indexer.index_all(conn, self.projects)
        hits, mode = search.text_search(conn, "διόρθωση")      # non-ASCII
        self.assertEqual((mode, [h.id for h in hits]), ("all", [SID]))
        hits, mode = search.text_search(conn, "login gmail")  # words in different messages
        self.assertEqual(mode, "all")
        hits, mode = search.text_search(conn, "login zzzqqq")  # fall back to any
        self.assertEqual((mode, len(hits)), ("any", 1))
        hits, _ = search.text_search(conn, 'weird "quote OR NOT')  # never an FTS syntax error
        self.assertEqual(search.text_search(conn, "  ")[1], "none")
        hits, _ = search.text_search(conn, "Fix login bug recap")  # titles/recap indexed

    def test_preview_is_readable(self):
        text = preview.plain_text(self.path)
        self.assertIn("Fix login bug", text)                # AI title in header
        self.assertIn("Fixed the login bug.", text)         # recap
        self.assertIn("You", text)
        self.assertIn("Claude", text)
        self.assertIn("✗ Read", text)                        # failed tool marked
        self.assertIn("gmail·search_emails", text)
        self.assertIn("⌘ /model opus", text)
        self.assertIn("⚑ Build finished", text)
        self.assertIn("$ git status", text)
        self.assertIn("conversation compacted", text)
        self.assertIn("Plan", text)                          # markdown heading rendered
        for raw in ("<command-name>", "<task-notification>", "local-command-stdout",
                    "skill body", "sidechain chatter", "## Plan", "tool_result"):
            self.assertNotIn(raw, text)


class TestAISearch(FixtureMixin, unittest.TestCase):
    def test_ai_search_parses_and_skips_persistence(self):
        conn = db.connect(self.db_path)
        indexer.index_all(conn, self.projects)
        envelope = {"result": f'```json\n[{{"id": "{SID}", "score": "87.5", "reason": "login"}}]\n```',
                    "is_error": False}
        fake = mock.Mock(returncode=0, stdout=json.dumps(envelope), stderr="")
        with mock.patch("subprocess.run", return_value=fake) as run:
            matches = aisearch.ai_search(conn, "the login bug")
        argv = run.call_args[0][0]
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual([(m.id, m.score) for m in matches], [(SID, 87)])
        self.assertEqual(matches[0].title, "Fix login bug")


class TestUtil(unittest.TestCase):
    def test_local_day_bucket(self):
        # 22:30Z on the 22nd is already the 23rd in Cyprus (UTC+3).
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Nicosia"
        time.tzset()
        try:
            now = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)
            self.assertEqual(util.day_bucket("2026-09-22T22:30:00Z", now), "Today")
            self.assertEqual(util.day_bucket("2026-09-22T20:30:00Z", now), "Yesterday")
            self.assertEqual(util.clock("2026-09-22T22:30:00Z"), "01:30")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_formatting(self):
        self.assertEqual(util.fmt_duration(3 * 86400 + 5 * 3600), "3d5h")
        self.assertEqual(util.fmt_tokens(1500), "1.5k")
        self.assertEqual(util.short_model("claude-opus-5-5,claude-haiku-4-5"), "opus5.5+")
        self.assertEqual(search.tokens("The UI db ΔΟΚΙΜΗ"), ["ui", "db", "δοκιμη"])


if __name__ == "__main__":
    unittest.main()
