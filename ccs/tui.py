"""The ccs Textual app: browse, search, preview and resume Claude Code sessions."""
from __future__ import annotations

import colorsys
import os
import subprocess
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from . import aisearch, ccformat, db, indexer, live, preview, search, util

SORTS = ["recent", "turns", "duration", "tokens"]
GROUPS = ["flat", "project", "day"]
SEP_PREFIX = "__sep__"
RESUME_CONFIRM_S = 4.0

# Open-session dot colour by live status from ~/.claude/sessions/<pid>.json.
STATUS_STYLE = {
    "busy": "bold green",       # model actively working
    "waiting": "bold yellow",   # waiting for your input / a permission
    "idle": "grey58",           # open but inactive
    "open": "cyan",             # open, status unknown
}

_SORT_KEYS = {
    "recent": lambda r: r.get("last_ts") or "",
    "turns": lambda r: r.get("turns") or 0,
    "duration": lambda r: r.get("duration_s") or 0,
    "tokens": lambda r: r.get("out_tokens") or 0,
}


def status_dot(status: str | None) -> str | Text:
    if not status:
        return ""
    return Text("●", style=STATUS_STYLE.get(status, "cyan"))


def build_project_colors(projects) -> dict[str, str]:
    """Assign every project a distinct truecolor hex.

    Hues are spread by the golden-ratio increment over the *sorted* set of
    projects, so consecutive projects land far apart on the wheel and no two
    present projects share a hue (guaranteed unique separation, unlike a small
    hashed palette where two projects can collide on the same entry). Alphabetical
    order keeps a project's colour stable unless the set of projects changes.
    Terminals without truecolor have rich downsample the hex to 256-colour.
    """
    ordered = sorted({p for p in projects if p})
    step = 0.6180339887498949  # golden ratio conjugate
    out: dict[str, str] = {}
    for i, p in enumerate(ordered):
        h = (i * step) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.65, 0.92)
        out[p] = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
    return out


def fork_cell(parent_id: str | None, titles: dict[str, str],
              style: str = "grey62", width: int = 18) -> str | Text:
    """Show a session's conversation-branch parent: '⑂ <parent title>'."""
    if not parent_id:
        return ""
    name = titles.get(parent_id) or f"{parent_id[:8]}?"
    label = "⑂ " + name.replace("\n", " ")
    if len(label) > width:
        label = label[: width - 1] + "…"
    return Text(label, style=style)


class SessionTable(DataTable):
    """DataTable whose Enter resumes instead of the built-in `select_cursor`.

    DataTable binds Enter → select_cursor, which would otherwise swallow the
    key before the app sees it. Overriding it here (rather than app-globally)
    also keeps mouse clicks as select-only — a click shouldn't launch claude.
    """

    BINDINGS = [Binding("enter", "resume_row", "resume", show=False)]

    def action_resume_row(self) -> None:
        self.app.action_resume()


class CCSApp(App):
    TITLE = "ccs"
    SUB_TITLE = "Claude Code sessions"
    CSS = """
    #body { height: 1fr; }
    #left { width: 3fr; }
    #right { width: 2fr; border-left: solid $panel; padding: 0 0 0 1; }
    #right.hidden { display: none; }
    #left.full { width: 1fr; }
    #preview { height: 1fr; scrollbar-size-vertical: 1; }
    DataTable { height: 1fr; }
    #search { display: none; dock: top; }
    #search.visible { display: block; }
    #status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    """

    BINDINGS = [
        Binding("slash", "search('fuzzy')", "filter", key_display="/"),
        Binding("f", "search('text')", "full-text"),
        Binding("a", "search('ai')", "AI search"),
        Binding("escape", "clear_search", "clear"),
        Binding("enter", "resume", "resume", show=True),
        Binding("r", "resume", "resume", show=False),
        Binding("p", "toggle_preview", "preview"),
        Binding("left_square_bracket", "preview_page(-1)", "preview ↑", key_display="["),
        Binding("right_square_bracket", "preview_page(1)", "preview ↓", key_display="]"),
        Binding("s", "cycle_sort", "sort"),
        Binding("g", "cycle_group", "group"),
        Binding("R", "reindex", "re-index"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, db_path=db.DEFAULT_DB_PATH, projects_dir=db.DEFAULT_PROJECTS_DIR):
        super().__init__()
        self.db_path = db_path
        self.projects_dir = projects_dir
        self.conn = db.connect(db_path)
        self.all_sessions: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self.mode = "browse"          # browse | fuzzy | text | ai  (what the table shows)
        self._input_kind: str | None = None   # search kind the open input is for
        self.sort = "recent"
        self.group = "flat"
        self.fuzzy = ""
        self.search_ids: list[str] = []     # result ids for text/ai modes (ranked)
        self.reasons: dict[str, str] = {}   # session id -> snippet/reason
        self._last_query = ""
        self._cursor_id: str | None = None
        self._preview_id: str | None = None
        self._preview_timer = None
        self._preview_visible = True
        self._resize_timer = None
        self._resume_armed: tuple[str, float] | None = None
        self._open_status: dict[str, str] = {}   # session id -> busy|waiting|idle|open
        self._project_colors: dict[str, str] = {}   # project -> hex color
        self._titles_by_id: dict[str, str] = {}     # session id -> title (for fork parent)
        self._drift = ccformat.Drift()
        self._shown_count = 0

    # ---- layout -------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Input(id="search", placeholder="filter…")
                yield SessionTable(id="table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                # min_width default (78) is wider than the pane → clipped text.
                yield RichLog(id="preview", wrap=True, markup=False,
                              highlight=False, min_width=20)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_column("", key="active", width=1)
        table.add_column("project", key="project", width=14)
        table.add_column("title", key="title", width=50)
        table.add_column("branch", key="fork", width=18)
        table.add_column("when", key="when", width=6)
        table.add_column("turns", key="turns", width=5)
        table.add_column("model", key="model", width=8)
        table.add_column("", key="match")
        table.focus()
        self._drift = ccformat.Drift.from_json(db.get_meta(self.conn, "drift"))
        self.load_all()
        self.rebuild_table()
        self.call_after_refresh(self._fit_columns)
        # Catch anything new/changed since the DB was last written, then poll.
        self.reindex()
        self.set_interval(2.0, self.reindex)
        self.set_interval(30.0, self._refresh_times)

    # ---- data ---------------------------------------------------------
    def load_all(self) -> None:
        rows = self.conn.execute("SELECT * FROM sessions").fetchall()
        self.all_sessions = [dict(r) for r in rows]
        self._by_id = {r["id"]: r for r in self.all_sessions}
        self._project_colors = build_project_colors(r["project"] for r in self.all_sessions)
        self._titles_by_id = {r["id"]: util.display_title(r) for r in self.all_sessions}
        self._apply_active()

    def _display_rows(self) -> list[dict]:
        if self.mode in ("text", "ai"):
            # Ranked result order; re-resolved each time so live data is fresh.
            return [self._by_id[s] for s in self.search_ids if s in self._by_id]
        rows = self.all_sessions
        if self.fuzzy:
            needle = self.fuzzy.lower()
            rows = [r for r in rows if self._matches_fuzzy(r, needle)]
        rows = sorted(rows, key=_SORT_KEYS[self.sort], reverse=True)
        if self.group == "project":
            # Stable sort keeps the chosen order within each project.
            return sorted(rows, key=lambda r: (r.get("project") or "").lower())
        if self.group == "day":
            # Buckets by recency, chosen sort within each bucket.
            by_recent = sorted(rows, key=_SORT_KEYS["recent"], reverse=True)
            order: dict[str, int] = {}
            for r in by_recent:
                order.setdefault(util.day_bucket(r.get("last_ts")), len(order))
            return sorted(rows, key=lambda r: order[util.day_bucket(r.get("last_ts"))])
        return rows

    @staticmethod
    def _matches_fuzzy(r: dict, needle: str) -> bool:
        hay = " ".join(
            str(r.get(k) or "")
            for k in ("custom_title", "ai_title", "agent_name", "project", "cwd",
                      "first_prompt", "last_prompt", "git_branches")
        ).lower()
        return all(w in hay for w in needle.split())

    # ---- table --------------------------------------------------------
    def rebuild_table(self, keep_cursor: bool = True, keep_viewport: bool = False) -> None:
        table = self.query_one("#table", DataTable)
        prev_id = self._cursor_id if keep_cursor else None
        rows = self._display_rows()
        scroll_y = table.scroll_y
        table.clear()

        show_match = self.mode in ("text", "ai")
        last_group = None
        target_row_index = None
        idx = 0
        for r in rows:
            if self.mode in ("browse", "fuzzy"):
                gval = self._group_value(r)
                if gval is not None and gval != last_group:
                    last_group = gval
                    table.add_row(
                        "", "", Text(f"── {gval} ──", style="bold"), "", "", "", "", "",
                        key=f"{SEP_PREFIX}{idx}",
                    )
                    idx += 1
            match = ""
            if show_match:
                match = (self.reasons.get(r["id"]) or "").replace("\n", " ")[:200]
            proj = (r.get("project") or "")
            project = Text(proj[:14], style=self._project_colors.get(proj, "white"))
            parent_id = r.get("forked_from")
            fork_proj = (self._by_id.get(parent_id, {}).get("project") or proj) if parent_id else proj
            fork = fork_cell(parent_id, self._titles_by_id,
                             style=self._project_colors.get(fork_proj, "grey62"))
            table.add_row(
                status_dot(r.get("open_status")),
                project,
                self._titles_by_id.get(r["id"]) or util.display_title(r),
                fork,
                util.rel_time(r.get("last_ts")),
                str(r.get("turns") or 0),
                util.short_model(r.get("models")),
                match,
                key=r["id"],
            )
            if r["id"] == prev_id:
                target_row_index = idx
            idx += 1

        self._update_status(len(rows))
        if idx:
            row = target_row_index if target_row_index is not None else self._first_session_row()
            if row is not None:
                if keep_viewport and target_row_index is not None:
                    # Live refresh: keep the viewport where the user left it.
                    table.move_cursor(row=row, scroll=False)
                    table.scroll_to(y=scroll_y, animate=False)
                else:
                    table.move_cursor(row=row)

    def _first_session_row(self) -> int | None:
        table = self.query_one("#table", DataTable)
        for i, key in enumerate(table.rows):
            if not str(key.value).startswith(SEP_PREFIX):
                return i
        return None

    def _refresh_times(self) -> None:
        """Keep the relative 'when' column honest even when nothing changes."""
        table = self.query_one("#table", DataTable)
        for key in list(table.rows):
            sid = key.value
            r = self._by_id.get(sid)
            if not r:
                continue
            new = util.rel_time(r.get("last_ts"))
            try:
                if table.get_cell(key, "when") != new:
                    table.update_cell(key, "when", new)
            except Exception:
                pass

    # Fixed content widths of every non-title column (must match add_column).
    _FIXED_COL_WIDTHS = {"active": 1, "project": 14, "fork": 18, "when": 6,
                         "turns": 5, "model": 8}

    def _fit_columns(self) -> None:
        """Grow `title` to fill the current view; keep `match` short (search only)."""
        try:
            table = self.query_one("#table", DataTable)
        except Exception:
            return
        avail = table.size.width
        if not avail:
            return
        pad = 2 * table.cell_padding
        n_cols = len(self._FIXED_COL_WIDTHS) + 2  # + title + match
        fixed = sum(self._FIXED_COL_WIDTHS.values()) + pad * n_cols + 2
        free = avail - fixed
        # Title keeps >= 24; in search modes `match` takes up to 40 of the rest
        # (and shrinks first on narrow terminals, so nothing scrolls sideways).
        match_w = max(0, min(40, free - 24)) if self.mode in ("text", "ai") else 0
        title_w = max(24, free - match_w)
        changed = False
        for col in table.ordered_columns:
            kv = col.key.value
            if kv == "title" and col.width != title_w:
                col.width = title_w
                changed = True
            elif kv == "match" and col.width != match_w:
                col.width = match_w
                col.label = Text("match" if match_w else "")
                changed = True
        if changed:
            table._require_update_dimensions = True
            table.refresh()

    def on_resize(self, event) -> None:
        self._fit_columns()
        # The preview is pre-rendered at a fixed width → re-render to reflow.
        if self._resize_timer is not None:
            self._resize_timer.stop()
        if self._preview_id:
            sid = self._preview_id
            self._resize_timer = self.set_timer(0.3, lambda: self._render_preview(sid, keep_scroll=True))

    def _group_value(self, r: dict) -> str | None:
        if self.group == "project":
            return r.get("project") or "unknown"
        if self.group == "day":
            return util.day_bucket(r.get("last_ts"))
        return None

    def _update_status(self, n: int | None = None) -> None:
        if n is None:
            n = self._shown_count
        self._shown_count = n
        mode = {
            "browse": f"{n} sessions",
            "fuzzy": f"filter '{self.fuzzy}' — {n}",
            "text": f"full-text '{self._last_query}' — {n}",
            "ai": f"AI '{self._last_query}' — {n}",
        }.get(self.mode, "")
        extra = f"sort:{self.sort}  group:{self.group}"
        warn = ""
        if self._drift.unknown_count() or self._drift.newer_than_verified():
            warn = "    ⚠ Claude Code format changed — run `ccs doctor`"
        self.query_one("#status", Static).update(f"{mode}    {extra}{warn}")

    # ---- selection / preview -----------------------------------------
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value if event.row_key else None
        if not key or str(key).startswith(SEP_PREFIX):
            return
        if key != self._cursor_id:
            self._resume_armed = None
        self._cursor_id = key
        self._schedule_preview(key)

    def _schedule_preview(self, sid: str) -> None:
        if not self._preview_visible:
            return
        if self._preview_timer is not None:
            self._preview_timer.stop()
        self._preview_timer = self.set_timer(0.12, lambda: self._render_preview(sid))

    def _render_preview(self, sid: str, keep_scroll: bool = False) -> None:
        row = self._by_id.get(sid)
        if not row or not self._preview_visible:
            return
        self._preview_id = sid
        fork_note = None
        parent = row.get("forked_from")
        if parent:
            ptitle = self._titles_by_id.get(parent) or "(not indexed)"
            fork_note = f"{ptitle}  ({parent[:8]})"
        words = search.tokens(self._last_query) if self.mode == "text" else []
        self._do_render(sid, row.get("file_path"), fork_note, words, keep_scroll)

    @work(thread=True, exclusive=True, group="preview")
    def _do_render(self, sid: str, file_path: str, fork_note: str | None,
                   words: list[str], keep_scroll: bool) -> None:
        renderable = preview.render_session(file_path, fork_note=fork_note, highlight=words)
        self.call_from_thread(self._show_preview, sid, renderable, keep_scroll)

    def _show_preview(self, sid: str, renderable, keep_scroll: bool) -> None:
        if sid != self._preview_id:
            return
        log = self.query_one("#preview", RichLog)
        # Live re-render of the same session: don't yank the user back to the
        # bottom if they scrolled up to read.
        at_end = log.scroll_y >= log.max_scroll_y - 1
        prev_y = log.scroll_y
        log.clear()
        if keep_scroll and not at_end:
            log.write(renderable, scroll_end=False)
            log.call_after_refresh(log.scroll_to, y=prev_y, animate=False)
        else:
            log.write(renderable, scroll_end=True)

    def action_preview_page(self, direction: int) -> None:
        log = self.query_one("#preview", RichLog)
        (log.scroll_page_down if direction > 0 else log.scroll_page_up)(animate=False)

    # ---- search actions ----------------------------------------------
    def action_search(self, kind: str) -> None:
        # Text/AI only switch the table's mode once a query is submitted, so
        # an open-but-empty prompt keeps showing (and live-updating) the list.
        self._input_kind = kind
        if kind == "fuzzy":
            self.mode = "fuzzy"
        inp = self.query_one("#search", Input)
        inp.add_class("visible")
        placeholders = {
            "fuzzy": "filter title/project/branch/prompts (live; words AND-ed)…",
            "text": "full-text search — Enter to run…",
            "ai": "describe the session — Enter to ask Claude…",
        }
        inp.placeholder = placeholders.get(kind, "search…")
        inp.value = ""
        inp.focus()

    def action_clear_search(self) -> None:
        inp = self.query_one("#search", Input)
        inp.value = ""
        inp.remove_class("visible")
        self._input_kind = None
        was_search = self.mode != "browse"
        self.mode = "browse"
        self.fuzzy = ""
        self._last_query = ""
        self.search_ids = []
        self.reasons = {}
        self.query_one("#table", DataTable).focus()
        self.rebuild_table()
        self.call_after_refresh(self._fit_columns)  # match column gone → regrow title
        if was_search and self._cursor_id:
            self._schedule_preview(self._cursor_id)  # drop search highlights

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._input_kind == "fuzzy":
            self.fuzzy = event.value
            self.rebuild_table(keep_cursor=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if self._input_kind == "fuzzy":
            self.query_one("#table", DataTable).focus()
            return
        if not query:
            return
        self._last_query = query
        if self._input_kind == "text":
            self.run_text_search(query)
        elif self._input_kind == "ai":
            self.run_ai_search(query)
        self.query_one("#table", DataTable).focus()

    def _show_results(self, mode: str, ids: list[str]) -> None:
        self.mode = mode
        self.fuzzy = ""
        self.search_ids = ids
        self.rebuild_table(keep_cursor=False)
        self.call_after_refresh(self._fit_columns)  # match column now shown

    def run_text_search(self, query: str) -> None:
        try:
            hits, how = search.text_search(self.conn, query)
        except Exception as e:
            self.notify(f"search error: {e}", severity="error")
            return
        hits = [h for h in hits if h.id in self._by_id]
        self.reasons = {h.id: h.snippet for h in hits}
        self._show_results("text", [h.id for h in hits])
        if how == "none":
            self.notify("nothing to search for", severity="warning")
        elif how == "any" and hits:
            self.notify(f"no session has all the words — {len(hits)} match any of them")
        else:
            self.notify(f"{len(hits)} sessions matched")

    @work(thread=True, exclusive=True, group="aisearch")
    def run_ai_search(self, query: str) -> None:
        self.call_from_thread(self.notify, "asking Claude…", timeout=30)
        # sqlite connections are per-thread: this worker needs its own.
        conn = db.connect(self.db_path)
        try:
            matches = aisearch.ai_search(conn, query)
        except aisearch.AISearchError as e:
            self.call_from_thread(self.notify, f"AI search failed: {e}", severity="error")
            return
        except Exception as e:  # never let a worker error take the app down
            self.call_from_thread(self.notify, f"AI search error: {e!r}", severity="error")
            return
        finally:
            conn.close()
        self.call_from_thread(self._apply_ai_results, matches)

    def _apply_ai_results(self, matches) -> None:
        self.reasons = {}
        ids = []
        for m in matches:
            if m.id in self._by_id and m.id not in self.reasons:
                self.reasons[m.id] = f"[{m.score}] {m.reason}"
                ids.append(m.id)
        self._show_results("ai", ids)
        self.notify(f"AI found {len(ids)} sessions")

    # ---- other actions ------------------------------------------------
    def action_toggle_preview(self) -> None:
        self._preview_visible = not self._preview_visible
        self.query_one("#right").set_class(not self._preview_visible, "hidden")
        self.query_one("#left").set_class(not self._preview_visible, "full")
        self.call_after_refresh(self._fit_columns)  # table width changed
        if self._preview_visible and self._cursor_id:
            self._schedule_preview(self._cursor_id)

    def action_cycle_sort(self) -> None:
        self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
        if self.mode in ("text", "ai"):
            self.notify("search results stay in relevance order")
        self.rebuild_table()

    def action_cycle_group(self) -> None:
        self.group = GROUPS[(GROUPS.index(self.group) + 1) % len(GROUPS)]
        self.rebuild_table()

    def action_resume(self) -> None:
        sid = self._cursor_id
        if not sid or str(sid).startswith(SEP_PREFIX):
            return
        row = self._by_id.get(sid)
        if not row:
            return
        status = self._open_status.get(sid)
        if status:
            armed = self._resume_armed
            if not (armed and armed[0] == sid and time.monotonic() - armed[1] < RESUME_CONFIRM_S):
                self._resume_armed = (sid, time.monotonic())
                self.notify(
                    f"This session is already open in another claude ({status}). "
                    "Press Enter again to resume it here anyway.",
                    severity="warning", timeout=RESUME_CONFIRM_S,
                )
                return
        self._resume_armed = None
        cwd = row.get("cwd") or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            self.notify(f"cwd gone ({cwd}); resuming from ~", severity="warning")
            cwd = os.path.expanduser("~")
        with self.suspend():
            os.system("clear")
            print(f"↻ resuming {util.display_title(row)[:70]}\n  {sid}\n  in {cwd}\n")
            try:
                subprocess.run(["claude", "--resume", sid], cwd=cwd)
            except FileNotFoundError:
                print("`claude` not found on PATH")
                input("press Enter to return to ccs…")
        self.reindex()

    @work(thread=True, exclusive=True, group="index")
    def reindex(self, full: bool = False) -> None:
        conn = db.connect(self.db_path)
        try:
            changed = indexer.index_all(conn, self.projects_dir, changed_only=not full)
            open_status = live.open_sessions(conn)
            drift = db.get_meta(conn, "drift")
        finally:
            conn.close()
        # Always call back: open sessions can appear/disappear (or change
        # status) without any transcript on disk changing.
        self.call_from_thread(self._after_reindex, changed, open_status, drift)

    def action_reindex(self) -> None:
        self.notify("re-indexing…")
        self.reindex(full=True)

    def _after_reindex(self, changed: list[str], open_status: dict[str, str],
                       drift_json: str | None = None) -> None:
        open_changed = open_status != self._open_status
        self._open_status = open_status
        drift = ccformat.Drift.from_json(drift_json)
        drift_changed = drift.to_json() != self._drift.to_json()
        self._drift = drift
        if changed:
            self.load_all()          # reloads rows + re-applies active dots
        else:
            self._apply_active()
        # Rebuild when content changed or the open/status set moved. Search
        # modes keep their result ids, so rebuilding just refreshes the rows.
        if changed or open_changed:
            self.rebuild_table(keep_viewport=True)
        elif drift_changed:
            self._update_status()
        if changed and self._preview_id in changed:
            self._render_preview(self._preview_id, keep_scroll=True)

    def _apply_active(self) -> None:
        for r in self.all_sessions:
            r["open_status"] = self._open_status.get(r["id"])
