# ccs — Claude Code session browser

A terminal UI to **find, browse, preview and resume every [Claude Code](https://claude.com/claude-code)
session across all your project folders** — think `claude --resume`, but global,
searchable, and live.

`claude --resume` only lists sessions for the current directory and shows little
about them. `ccs` indexes *every* session on your machine and lets you search
their full contents (including with natural language), see which are open right
now, preview a conversation before you jump in, and resume any of them in its
original working directory.

It is **read-only** over your transcripts — it never edits or deletes a session.

```
┌ ccs · 312 sessions ──────────────────────────────────────────────┬ preview ─────────────┐
│ project     title                              branch      when turns │ ▸ you · 2h        │
│●app         fix the checkout race condition on…             2h   41   │   the checkout... │
│●api         why is the /orders endpoint 500ing…  ⑂ orders-…  3h   28   │ ▸ claude · o4.8   │
│ web         landing page hero + mobile layout               1d   63   │   Looking at the  │
│ docs        rewrite the getting-started guide               2d   12   │   handler, the... │
│ app         add retry/backoff to the sync worker            3d   50   │                   │
│ …                                                                     │                   │
└───────────────────────────────────────────────────────────────────┴───────────────────────┘
 / filter   f full-text   a AI search   ⏎ resume   p preview   s sort   g group   q quit
```

## Features

- **One list for every session** across all projects — not just the current dir.
- **Three ways to search**
  - `/` fuzzy filter on title / project / prompts / branch (instant, as you type)
  - `f` full-text search across every message body (SQLite FTS5)
  - `a` **natural-language search** — describe a session in a sentence and let
    Claude rank the matches, with a one-line "why it matched" for each
- **Live** — leave it open; new sessions, activity, and status update on their own.
- **Open-session indicator** — a coloured dot shows which sessions are open in a
  running Claude Code process *right now*: 🟢 busy (working) · 🟡 waiting for you
  · ⚪ idle.
- **Readable preview** — a chat-style view of the conversation: a header card
  (title, where, when, how long, model, PRs, Claude's recap of the session),
  your prompts, Claude's replies rendered as Markdown, tool calls as compact
  one-liners (`⏺ Bash  git status`, failures in red), and slash commands /
  background-task notices as quiet markers instead of raw XML.
- **Good titles** — uses your `/rename` title, else Claude Code's own
  auto-generated title, else the first prompt.
- **Resume** any session with `⏎` — launches `claude --resume` in its original cwd.
- **Conversation-branch lineage** — forked chats show `⑂ <parent title>` so you
  can see what a session was branched from.
- **Fast** — everything reads from a local SQLite index, so it stays snappy at
  thousands of sessions.
- **Keeps up with Claude Code** — the transcript format is undocumented and
  changes often; ccs flags anything it doesn't recognise (status-bar warning +
  `ccs doctor`) instead of silently miscounting, and re-parses everything
  automatically when its parser is updated.

## Requirements

- [Claude Code](https://claude.com/claude-code) installed (the `claude` CLI on your `PATH`).
- Python **3.9+**.
- macOS or Linux.

## Install

### With pipx (recommended)

```sh
pipx install git+https://github.com/ericosg/ccs
ccs        # launch
```

`pip install git+https://github.com/ericosg/ccs` works too (ideally into a venv).

### From source

```sh
git clone https://github.com/ericosg/ccs
cd ccs
python3 -m venv .venv
.venv/bin/pip install textual
# run it directly:
./launch.sh
# …or symlink it onto your PATH:
ln -s "$PWD/launch.sh" ~/.local/bin/ccs
```

## Usage

```sh
ccs                     # launch the TUI (indexes on start, then live-refreshes)
ccs index               # (re)index only changed sessions, then exit
ccs index --rebuild     # reparse every session from scratch
ccs search "the session where I set up the CI pipeline"   # AI search from the shell
ccs stats               # session/message counts + top projects
ccs doctor              # check compatibility with your Claude Code version
```

### Keys

| key | action |
|-----|--------|
| `↑` / `↓` | move (preview follows the selection) |
| `Enter` / `r` | **resume** the selected session (`claude --resume` in its original cwd); asks for a second Enter if it's already open elsewhere |
| `/` | live fuzzy filter (title / project / branch / prompts; words AND-ed) |
| `f` | full-text search across all message bodies (all words first, else any) |
| `a` | AI search — describe the session; Claude ranks the matches |
| `Esc` | clear the current search |
| `p` | toggle the preview pane (hiding it widens the list) |
| `[` / `]` | page the preview up / down |
| `s` | cycle sort (recent / turns / duration / tokens) — also within groups |
| `g` | cycle grouping (flat / project / day) |
| `R` | force a full re-index |
| `q` | quit |

## How search works

- **Fuzzy** (`/`) and **full-text** (`f`) hit the local SQLite index instantly.
  Full-text works in any language (Greek, accented, CJK…), prefers sessions
  containing *all* your words, and falls back to *any* of them.
- **AI** (`a`, or `ccs search`) is two-stage so it stays fast and cheap: a
  full-text prefilter narrows your sessions to a few dozen candidates, then a
  single `claude -p` call (Haiku by default) ranks *those* against your
  description. It never stuffs your whole transcript corpus into a prompt, and
  the ranking call isn't saved as a session of its own.

## The open-session dot

Claude Code appends-and-closes its transcript files, so "recently modified" is a
poor signal for "open". Instead, `ccs` reads Claude Code's live session registry
(`~/.claude/sessions/*.json`) to know exactly which sessions are open and their
status (busy / waiting / idle), verifying the process is still alive. On older
Claude Code versions without that registry, it falls back to matching running
`claude` processes to their working directory.

## Data & privacy

- **Everything is local.** `ccs` builds a SQLite index at `~/.claude/ccs.db` from
  the transcripts Claude Code already stores under `~/.claude/projects/`. The
  index is a **disposable cache** — delete it anytime and it rebuilds. `ccs`
  never modifies your sessions.
- **The one exception is AI search.** `ccs search` / the `a` key sends compact
  digests of candidate sessions (titles, first/last prompts, and matched
  snippets) to Anthropic through your local `claude` CLI in order to rank them.
  If you don't want any session text leaving your machine, use `/` and `f`
  instead — those are 100% local.

## When Claude Code updates

If the status bar shows **⚠ Claude Code format changed**, run `ccs doctor`. It
lists every transcript element ccs doesn't understand yet (with an example
file), and checks the `claude` CLI flags and live-session registry ccs relies on.
Everything format-specific lives in `ccs/ccformat.py` — see
[CLAUDE.md](CLAUDE.md#adapting-to-claude-code-changes-read-this-first-when-something-looks-off).

## Development

The architecture, data-model notes, and the non-obvious gotchas are documented
for contributors (and for Claude Code itself) in **[CLAUDE.md](CLAUDE.md)**.

```sh
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v   # unit tests (synthetic fixture)
PYTHONPATH=. .venv/bin/python tests/pilot_test.py                # headless TUI smoke test (your data)
```

## License

[MIT](LICENSE)
