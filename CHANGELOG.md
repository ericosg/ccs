# Changelog

All notable changes to ccs. Versions follow [SemVer](https://semver.org/);
the version lives in both `pyproject.toml` and `ccs/__init__.py`.

## [Unreleased]

## [0.2.0] — 2026-09-23

### Added
- **Readable preview**: header card (title, cwd/branch, local time span and
  duration, prompts/replies/tokens, model, PR links, fork parent, Claude
  Code's session recap), Markdown-rendered replies, tool calls as one-liners
  (failures in red, long runs collapsed), slash commands / `!` commands /
  background-task notices / compaction as quiet markers, day separators.
  `[` / `]` page the preview.
- **Claude Code format-drift detection**: all format knowledge in
  `ccs/ccformat.py`; unknown line types, system subtypes, content blocks and
  wrapper tags are recorded per session and surfaced as a status-bar warning.
- **`ccs doctor`**: rescans all transcripts and reports unknown format
  elements (with example files), checks the `claude` CLI version and flags,
  and the live-session registry.
- `PARSER_VERSION`: parser changes trigger an automatic full re-parse.
- Titles use Claude Code's auto-generated `ai-title` when there's no
  `/rename` title; recaps (`away_summary`) and PR links (`prUrl`) are indexed.
- Resume guard: resuming a session already open in another `claude` needs a
  second Enter.
- Unit test suite (`tests/test_ccs.py`) on a synthetic transcript fixture.

### Changed
- Full-text search ranks sessions, preferring those with *all* words and
  falling back to *any*; works for non-Latin scripts and short words.
- Fuzzy filter AND-s space-separated words.
- Sort applies within project/day groups.
- Dates, times and day groups use local time (were UTC).
- Search results refresh live; the match column gets room on normal widths.

### Fixed
- In-TUI AI search always failed (sqlite connection used across threads).
- Each AI search left a junk session behind (`--no-session-persistence`).
- Turn counts / titles included harness messages (task notifications, `!`
  output, compaction summaries, meta lines).
- Output tokens and assistant turns were over-counted (one message is split
  across several transcript lines).
- Preview text was clipped (RichLog `min_width` 78 > pane width).
- Preview jumped to the bottom on live updates; the table no longer
  scrolls sideways; terminal resizes re-fit the table and reflow the preview.
- Overlapping background index passes; worker errors could quit the app.
- Stale AI results could override a newer search.
- Lines appended during indexing could be missed; duplicate session ids were
  re-parsed every tick; image-only first prompts blanked the title.

## [0.1.0]

Initial version: global session list, fuzzy / full-text / AI search, live
open-session dots, preview, resume.
