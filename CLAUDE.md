# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ccs` is a Python + Textual **TUI to find, browse, preview and resume every
Claude Code session across all project folders** — a global, searchable,
live `claude --resume`. It is **read-only** over the transcripts (plus a resume
action); it never mutates a session. User-facing docs are in `README.md`.

## Commands

```sh
# Run
ccs                     # launch the TUI (indexes on start, then live-refreshes)
ccs index               # (re)index only changed sessions, then exit
ccs index --rebuild     # reparse every session from scratch
ccs search "<sentence>" # AI advanced search from the shell (prints ranked hits)
ccs stats               # session/message counts + top projects

# Dev (from a source checkout, no install)
python3 -m venv .venv && .venv/bin/pip install textual
PYTHONPATH=. .venv/bin/python -m ccs [index|search|stats]
PYTHONPATH=. .venv/bin/python tests/pilot_test.py   # headless Textual smoke test
./launch.sh             # venv launcher (symlink-safe; can be symlinked to ~/.local/bin/ccs)
```

There is **no build step and no lint/type config** — it's a small pure-Python
package. The only runtime dependency is `textual` (which brings `rich`).
Requires Python 3.9+. Developed against `textual` 8.x; `_fit_columns` touches a
DataTable internal (`_require_update_dimensions`), so re-verify on major Textual
bumps (see `pyproject.toml`'s version cap).

## Design principles

- **Primary jobs: _find_ a past session + _browse/review_.** Analytics and
  cleanup are deliberately deferred (see Roadmap) — easy to add later.
- **Read-only + resume.** No tags/notes/favorites, no deletes. So the SQLite
  index is a **pure disposable cache**: 100% derived from the `.jsonl` files,
  holds nothing precious, safe to `rm` and rebuild. A Claude Code update can
  never cost the user anything here.
- **Live by default:** the list updates in real time; no manual refresh.
- **AI search** feels like "`claude -p` with a preloaded prompt + my sentence"
  but stays cheap (two-stage design below).

## Data model — how Claude Code stores sessions

Session transcripts live under `~/.claude/projects/<project-slug>/<session-id>.jsonl`,
where `<project-slug>` is the cwd path with `/` → `-` (e.g. `-Users-you-projects-app`).

**CRITICAL gotcha — only depth-1 `.jsonl` files are real sessions.** A recursive
`find` over `~/.claude/projects` returns far more files than there are sessions.
The extras are **not** top-level sessions:
- `<session-id>/subagents/agent-*.jsonl` — subagent transcripts of a parent session.
- `<slug>/<plugin>/skill-injections.jsonl`, `journal.jsonl` — plugin artifacts.

`indexer.iter_session_files()` therefore globs `*.jsonl` **only directly inside
each project dir** (depth 1). Do not switch to a recursive walk without
re-filtering, or the index balloons with subagent/plugin noise.

### Per-line JSONL shape (the fields we mine)

Each line is one JSON object with a `type`. Types seen: `user`, `assistant`,
`system`, `summary`, `custom-title`, `last-prompt`, `pr-link`, `agent-name`,
`file-history-snapshot`, `permission-mode`, `queue-operation`, `attachment`.

- `user` / `assistant` lines carry `cwd`, `gitBranch`, `version`, `entrypoint`,
  `userType`, `isSidechain`, `timestamp`, `message`, and (on a forked session's
  first line) `forkedFrom`.
  - `message.content` is a **str** (simple prompt) or a **list of blocks**
    (`text`, `tool_use`, `tool_result`, `thinking`, `image`).
  - `assistant` `message.model` (e.g. `claude-opus-4-8[1m]`) and
    `message.usage.output_tokens` are per-message.
- `custom-title` → `customTitle`; `last-prompt` → `lastPrompt`;
  `pr-link` → `prLink`/`url`.
- `isSidechain: true` marks subagent chatter inline — skipped for counts.

### What the indexer extracts per session (see `indexer.parse_session`)

cwd, project (basename of cwd), first/last timestamp → duration, human **turns**
(user lines whose content has real text and isn't a harness wrapper — see
`_looks_like_prompt`), assistant turns, tool-call count, distinct models,
`git_branches` (all distinct) and `branch` (last-seen git branch), **`forked_from`**
(parent session id — conversation-branch lineage; see below), custom title,
first/last prompt, entrypoint, PR links, summed output tokens, file size +
**mtime** (the incremental-staleness key).

**Conversation branches (`forkedFrom`) — this is what the "branch" column shows.**
When you fork/rewind a chat, the child session's first line carries
`forkedFrom = {sessionId: <parent>, messageUuid: <fork point>}`. `indexer` stores
`forked_from = sessionId`. Parents may themselves be forks (chains) or may have
been pruned (unresolved parent → shown as a short id). Claude Code also appends
`(Branch)` to a forked session's custom title. The table's **branch** column
shows `⑂ <parent title>` (looked up via `_titles_by_id`); the preview header
shows `⑂ forked from: <parent> (shortid)`.

Git branch is *not* a column (it's often just `HEAD` for repos in detached-HEAD
state; only some repos carry real branch names, and one session can span
several). It stays in the preview header and the fuzzy haystack.

### FTS text (what's searchable)

For each user/assistant line we store searchable text in `messages_fts`:
- user **text** blocks (skips `tool_result` — those are huge file dumps),
- assistant **text** blocks,
- `tool_use` blocks flattened to `[ToolName] key=val …` (so you can search for a
  Bash command or a file path).
- `thinking` and `tool_result` are intentionally **excluded** to keep the index
  small (tens of MB). Text truncated to 2000 chars/msg, tool_use to 400.

## Architecture / files

```
ccs/
  db.py         SQLite schema, connect() (+ schema-version auto-rebuild), helpers.
  live.py       open-session detection (~/.claude/sessions registry) + status.
  indexer.py    jsonl parsing; index_all(changed_only=…) → list of changed ids.
  aisearch.py   two-stage natural-language search (FTS prefilter → claude -p rank).
  preview.py    render_session(path) → rich.Text for the preview pane.
  util.py       rel_time / fmt_duration / short_model / day_bucket.
  tui.py        the Textual App (CCSApp).
  __main__.py   argparse CLI (tui / index / search / stats); console entry = main().
launch.sh       venv launcher (symlink-resolving), symlinkable to ~/.local/bin/ccs.
tests/pilot_test.py   headless Textual pilot smoke test.
```

### SQLite (`~/.claude/ccs.db`, WAL mode)

- `sessions` — one row per session, all the metadata above, PK = session id.
- `messages_fts` — FTS5 virtual table (`porter unicode61`), cols
  `session_id UNINDEXED, role UNINDEXED, ts UNINDEXED, text`.
- `meta` — key/value scratch; holds `schema_version`.
- **Schema versioning**: `db.SCHEMA_VERSION` — `connect()` drops & recreates all
  tables when it differs (the cache is disposable). **Bump it on any schema
  change** instead of writing migrations; the next index rebuilds in seconds.
- **Incremental**: `index_all` compares file mtime to the stored `mtime`;
  unchanged files are skipped, vanished files are deleted from both tables. A
  full pass over a few hundred sessions takes a few seconds; incremental is instant.

### Search modes

- **Fuzzy** (`/`): pure in-memory substring over title/project/cwd/prompts/branch.
  Live-filters on every keystroke.
- **Full-text** (`f`): FTS5 `MATCH` (query tokens OR-ed), ranked by `bm25`, with a
  `snippet()` shown in the `match` column.
- **AI** (`a` in TUI, or `ccs search`): `aisearch.ai_search()` —
  1. `fts_candidates()` narrows to ≤40 sessions via an FTS OR-query on salient
     tokens (stopwords/short words dropped); pads with recent sessions if sparse.
  2. Builds compact digests (id, project, date, title, last prompt, snippet) and
     sends them to **`claude -p … --output-format json --model
     claude-haiku-4-5-20251001`**, asking for a ranked JSON array
     `[{id, score, reason}]`. Parsed from the envelope's `result` field.
  - Haiku by default (cheap; plenty for ranking); override with `ccs search
    --model …`. Never dumps the full corpus into a prompt.

### `claude -p` JSON envelope (for reference)

Keys include `result` (the text), `is_error`, `total_cost_usd`, `modelUsage`,
`usage`, `session_id`. We read `result`, then extract the first `[ … ]` array.

### The `●` "open session" dot (`live.py`)

Means **"open in a running interactive `claude` process right now"**, not
"recently wrote to disk", and it's **colored by live status**.

**Source of truth = `~/.claude/sessions/<pid>.json`** — Claude Code keeps one
file per running interactive session:
`{pid, sessionId, cwd, kind:"interactive", status, name, startedAt, …}`.
`live.open_sessions()` reads them, keeps `kind=="interactive"`, verifies the pid
is alive with `os.kill(pid,0)` (files linger after a crash), and returns
`{sessionId: status}` — the **exact** session id per open session, no heuristic.

**Status → dot colour** (`STATUS_STYLE` in tui.py): `busy`→green (model working),
`waiting`→yellow (wants your input/a permission), `idle`→grey, `open`→cyan.

Why not simpler signals (all rejected, don't reintroduce):
- **mtime < N s**: Claude appends-and-closes the `.jsonl`, so open-but-idle
  sessions write nothing for minutes → under-counts.
- **process env / open fds**: no session id in the env, no session-id-bearing
  open fd — a process alone can't be mapped to its exact session.
- **`/private/tmp/claude-<uid>/<slug>/<session-id>/scratchpad`** encodes the id
  but lingers after exit → over-counts.

**Legacy fallback** (`_legacy_open_ids`, only if the registry dir is absent, i.e.
older Claude Code): `pgrep -x claude` → drop headless `-p` → batched `lsof` cwd →
mark the K most-recently-active sessions per cwd. `live.py` degrades to `∅` if
nothing is available — never crashes.

### Live refresh

`CCSApp.on_mount` runs one `reindex()` then `set_interval(2.0, self.reindex)`.
`reindex` is a **`@work(thread=True, exclusive=True, group="index")`** worker.
Each tick it (a) incrementally re-indexes changed files and (b) computes
`live.open_sessions()`, then `call_from_thread`s `_after_reindex(changed,
open_status)`. That reloads `all_sessions` when content changed, re-applies the
`●` dot, and rebuilds the table when content **or** the open/status map changed —
only in browse/fuzzy modes, so a search result set isn't disrupted.

**Threading + SQLite:** the main thread holds `self.conn` (reads). The reindex
worker opens its **own** connection each run and closes it — never share one
sqlite connection across threads. WAL lets reader and writer coexist.

### Preview

`on_data_table_row_highlighted` → 120 ms debounce (`set_timer`) →
`@work(thread=True, exclusive=True, group="preview")` renders
`preview.render_session(file_path, fork_note)` off-thread and pushes a `rich.Text`
into the `RichLog`. Reads the jsonl fresh (reflects an active session's latest
turns). Caps at last 250 messages, 1400 chars/msg.

### Table / grouping internals

Columns (in order): `● | project | title | branch | when | turns | model |
match` (8 — keep separator rows at 8 cells too; the group header goes in the
title cell). Note the DataTable column *key* for the "branch" label is `fork`.

**Dynamic widths (`_fit_columns`)** — DataTable has no native flex column, so we
compute widths from `table.size.width`: every non-title column is fixed
(`_FIXED_COL_WIDTHS`), `match` is 40 in text/AI search and **0 while browsing**,
and **title takes the remaining space** (min 24). Set `col.width`, then
`table._require_update_dimensions = True; table.refresh()`. Re-run on `on_resize`,
`on_mount` (via `call_after_refresh`), preview toggle, and search enter/clear. So
title fills the view and grows further when the preview pane is hidden. **Keep
`_FIXED_COL_WIDTHS` in sync with `add_column`.**

Cell styling (all via `rich.Text` cells):
- **`●` dot** — `status_dot()`, colored by live status.
- **project** — colored by `self._project_colors` (built per load by
  `build_project_colors`): truecolor hex, hues spread by the golden-ratio
  increment over the *sorted* set of projects → every present project is a
  distinct, well-separated color (a small hashed palette collided). Rich
  downsamples the hex on non-truecolor terminals.
- **title** — plain; the wide flex column. The full title string is passed (no
  length cap) so DataTable clips to the exact column width.
- **branch** — `fork_cell()`: `⑂ <parent title>` for a forked conversation
  (looked up via `_titles_by_id`), blank otherwise; colored by the **parent's
  project** (`_project_by_id` → `_project_colors`, falling back to the row's own).

Grouping (`g`: flat/project/day) inserts dim separator rows keyed `__sep__<n>`.
**Guard these everywhere** — highlight and resume both bail if the row key starts
with `SEP_PREFIX`. Project grouping sorts by project first (two stable passes) so
separators stay contiguous.

### Resume (`Enter`) — and a DataTable gotcha

`DataTable` ships its own `Binding("enter", "select_cursor")`, which consumes
Enter while the table is focused — so an app-level `enter` binding never fires.
Fix: a `SessionTable(DataTable)` subclass overrides Enter *on the table* →
`action_resume_row` → `app.action_resume()`. Overriding at the widget (not app)
level also keeps a **mouse click select-only** (highlight + preview) rather than
launching claude. `r` is a secondary app binding; separator rows can't be resumed.

`action_resume` → `with self.suspend():` drop out of the alt-screen →
`subprocess.run(["claude", "--resume", sid], cwd=<session cwd>)` → on exit,
`reindex()`. Falls back to `~` if the original cwd is gone.

## Keybindings

`↑/↓` move (preview follows) · `Enter` (or `r`) resume · `/` fuzzy ·
`f` full-text · `a` AI search · `Esc` clear search · `p` toggle preview ·
`s` sort (recent/turns/duration/tokens) · `g` group (flat/project/day) ·
`R` full re-index · `q` quit.

## Testing

`tests/pilot_test.py` drives the app headlessly via Textual's `run_test()` pilot:
mounts, checks rows populate + preview renders, exercises fuzzy/full-text/sort/
group/preview-toggle. Run: `PYTHONPATH=. .venv/bin/python tests/pilot_test.py`.
It asserts `>=` on row counts, never `==`, because the live re-index can add
sessions mid-test (e.g. a `claude -p` call spawns a new session file). The resume
path and the in-TUI AI-search worker need a **real terminal** and aren't covered.

## Roadmap / deliberately deferred

Kept out of v1 by the read-only + find/browse scope. Add only if asked:
- tags / notes / favorites / archive (would need a separate table so the index
  stays a disposable cache),
- cost & activity analytics (data is already indexed: `out_tokens`, timestamps),
- cleanup/prune view (biggest sessions by `size`),
- fold subagent transcripts into a parent session's FTS,
- export a session to markdown.
