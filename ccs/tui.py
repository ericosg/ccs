"""The ccs Textual app: browse, search, preview and resume Claude Code sessions."""
from __future__ import annotations

import colorsys
import os
import subprocess

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from . import aisearch, db, indexer, live, preview, util

SORTS = ["recent", "turns", "duration", "tokens"]
GROUPS = ["flat", "project", "day"]
SEP_PREFIX = "__sep__"

# Open-session dot colour by live status from ~/.claude/sessions/<pid>.json.
STATUS_STYLE = {
    "busy": "bold green",       # model actively working
    "waiting": "bold yellow",   # waiting for your input / a permission
    "idle": "grey58",           # open but inactive
    "open": "cyan",             # open, status unknown
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
    CSS = """
    #body { height: 1fr; }
    #left { width: 3fr; }
    #right { width: 2fr; border-left: solid $panel; padding: 0 1; }
    #right.hidden { display: none; }
    #left.full { width: 1fr; }
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
        self.mode = "browse"          # browse | fuzzy | text | ai
        self.sort = "recent"
        self.group = "flat"
        self.fuzzy = ""
        self.search_rows: list[dict] = []   # rows for text/ai modes
        self.reasons: dict[str, str] = {}   # session id -> snippet/reason
        self._cursor_id: str | None = None
        self._preview_id: str | None = None
        self._preview_timer = None
        self._preview_visible = True
        self._open_status: dict[str, str] = {}   # session id -> busy|waiting|idle|open
        self._project_colors: dict[str, str] = {}   # project -> hex color
        self._titles_by_id: dict[str, str] = {}     # session id -> title (for fork parent)
        self._project_by_id: dict[str, str] = {}    # session id -> project (for fork color)

    # ---- layout -------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Input(id="search", placeholder="filter…")
                yield SessionTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="right"):
                yield RichLog(id="preview", wrap=True, markup=False, highlight=False)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_column("", key="active", width=1)
        table.add_column("project", key="project", width=14)
        table.add_column("title", key="title", width=50)
        table.add_column("branch", key="fork", width=18)
        table.add_column("when", key="when", width=5)
        table.add_column("turns", key="turns", width=5)
        table.add_column("model", key="model", width=8)
        table.add_column("match", key="match")
        table.focus()
        self.load_all()
        self.rebuild_table()
        self.call_after_refresh(self._fit_columns)
        # Catch anything new/changed since the DB was last written, then poll.
        self.reindex()
        self.set_interval(2.0, self.reindex)

    # ---- data ---------------------------------------------------------
    def load_all(self) -> None:
        rows = self.conn.execute("SELECT * FROM sessions").fetchall()
        self.all_sessions = [dict(r) for r in rows]
        self._project_colors = build_project_colors(r["project"] for r in self.all_sessions)
        self._titles_by_id = {r["id"]: self._title_of(r) for r in self.all_sessions}
        self._project_by_id = {r["id"]: r.get("project") for r in self.all_sessions}
        self._apply_active()

    def _sorted(self, rows: list[dict]) -> list[dict]:
        if self.sort == "turns":
            key = lambda r: (r.get("turns") or 0)
        elif self.sort == "duration":
            key = lambda r: (r.get("duration_s") or 0)
        elif self.sort == "tokens":
            key = lambda r: (r.get("out_tokens") or 0)
        else:  # recent
            key = lambda r: (r.get("last_ts") or "")
        return sorted(rows, key=key, reverse=True)

    def _display_rows(self) -> list[dict]:
        if self.mode in ("text", "ai"):
            return self.search_rows
        rows = self.all_sessions
        if self.fuzzy:
            needle = self.fuzzy.lower()
            rows = [r for r in rows if self._matches_fuzzy(r, needle)]
        if self.group == "project":
            # Group key first (so separators are contiguous), recent within group.
            rows = sorted(rows, key=lambda r: r.get("last_ts") or "", reverse=True)
            return sorted(rows, key=lambda r: (r.get("project") or "").lower())
        if self.group == "day":
            # Recent-first already makes day buckets contiguous.
            return sorted(rows, key=lambda r: r.get("last_ts") or "", reverse=True)
        return self._sorted(rows)

    @staticmethod
    def _matches_fuzzy(r: dict, needle: str) -> bool:
        hay = " ".join(
            str(r.get(k) or "")
            for k in ("custom_title", "project", "cwd", "first_prompt", "last_prompt", "git_branches")
        ).lower()
        return needle in hay

    def _title_of(self, r: dict) -> str:
        return (r.get("custom_title") or r.get("first_prompt") or "(no prompt)").replace("\n", " ")

    # ---- table --------------------------------------------------------
    def rebuild_table(self, keep_cursor: bool = True) -> None:
        table = self.query_one("#table", DataTable)
        prev_id = self._cursor_id if keep_cursor else None
        rows = self._display_rows()
        table.clear()

        show_match = self.mode in ("text", "ai")
        last_group = None
        target_row_index = None
        idx = 0
        for r in rows:
            if self.mode == "browse" or self.mode == "fuzzy":
                gval = self._group_value(r)
                if gval is not None and gval != last_group:
                    last_group = gval
                    table.add_row(
                        "", "", f"── {gval} ──", "", "", "", "", "",
                        key=f"{SEP_PREFIX}{idx}",
                    )
                    idx += 1
            active = status_dot(r.get("open_status"))
            match = ""
            if show_match:
                match = (self.reasons.get(r["id"]) or "").replace("\n", " ")[:200]
            proj = (r.get("project") or "")
            project = Text(proj[:14], style=self._project_colors.get(proj, "white"))
            parent_id = r.get("forked_from")
            fork_proj = self._project_by_id.get(parent_id) or proj if parent_id else proj
            fork = fork_cell(parent_id, self._titles_by_id,
                             style=self._project_colors.get(fork_proj, "grey62"))
            table.add_row(
                active,
                project,
                self._title_of(r),
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
                table.move_cursor(row=row)

    def _first_session_row(self) -> int | None:
        table = self.query_one("#table", DataTable)
        for i, key in enumerate(table.rows):
            if not str(key.value).startswith(SEP_PREFIX):
                return i
        return None

    # Fixed content widths of every non-title column (must match add_column).
    _FIXED_COL_WIDTHS = {"active": 1, "project": 14, "fork": 18, "when": 5,
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
        match_w = 40 if self.mode in ("text", "ai") else 0
        used = sum(self._FIXED_COL_WIDTHS.values()) + match_w + pad * n_cols + 2
        title_w = max(24, avail - used)
        changed = False
        for col in table.ordered_columns:
            kv = col.key.value
            if kv == "title" and col.width != title_w:
                col.width = title_w
                changed = True
            elif kv == "match" and col.width != match_w:
                col.width = match_w
                changed = True
        if changed:
            table._require_update_dimensions = True
            table.refresh()

    def on_resize(self, event) -> None:
        self._fit_columns()

    def _group_value(self, r: dict) -> str | None:
        if self.group == "project":
            return r.get("project") or "unknown"
        if self.group == "day":
            return util.day_bucket(r.get("last_ts"))
        return None

    def _update_status(self, n: int) -> None:
        mode = {
            "browse": f"{n} sessions",
            "fuzzy": f"filter '{self.fuzzy}' — {n}",
            "text": f"full-text '{self._last_query}' — {n}",
            "ai": f"AI '{self._last_query}' — {n}",
        }.get(self.mode, "")
        extra = f"sort:{self.sort}  group:{self.group}"
        self.query_one("#status", Static).update(f"{mode}    {extra}")

    _last_query = ""

    # ---- selection / preview -----------------------------------------
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value if event.row_key else None
        if not key or str(key).startswith(SEP_PREFIX):
            return
        self._cursor_id = key
        self._schedule_preview(key)

    def _schedule_preview(self, sid: str) -> None:
        if not self._preview_visible:
            return
        if self._preview_timer is not None:
            self._preview_timer.stop()
        self._preview_timer = self.set_timer(0.12, lambda: self._render_preview(sid))

    def _render_preview(self, sid: str) -> None:
        row = self._row_by_id(sid)
        if not row:
            return
        self._preview_id = sid
        fork_note = None
        parent = row.get("forked_from")
        if parent:
            ptitle = self._titles_by_id.get(parent) or "(not indexed)"
            fork_note = f"⑂ forked from: {ptitle}  ({parent[:8]})"
        self._do_render(sid, row.get("file_path"), fork_note)

    @work(thread=True, exclusive=True, group="preview")
    def _do_render(self, sid: str, file_path: str, fork_note: str | None) -> None:
        text = preview.render_session(file_path, fork_note=fork_note)
        self.call_from_thread(self._show_preview, sid, text)

    def _show_preview(self, sid: str, text) -> None:
        if sid != self._preview_id:
            return
        log = self.query_one("#preview", RichLog)
        log.clear()
        log.write(text)

    def _row_by_id(self, sid: str) -> dict | None:
        for r in self.all_sessions:
            if r["id"] == sid:
                return r
        for r in self.search_rows:
            if r["id"] == sid:
                return r
        return None

    # ---- search actions ----------------------------------------------
    def action_search(self, kind: str) -> None:
        self.mode = kind
        inp = self.query_one("#search", Input)
        inp.add_class("visible")
        placeholders = {
            "fuzzy": "filter title/project/branch (live)…",
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
        self.mode = "browse"
        self.fuzzy = ""
        self.search_rows = []
        self.reasons = {}
        self.query_one("#table", DataTable).focus()
        self.rebuild_table()
        self.call_after_refresh(self._fit_columns)  # match column gone → regrow title

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.mode == "fuzzy":
            self.fuzzy = event.value
            self.rebuild_table(keep_cursor=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if self.mode == "fuzzy":
            self.query_one("#table", DataTable).focus()
            return
        if not query:
            return
        self._last_query = query
        if self.mode == "text":
            self.run_text_search(query)
        elif self.mode == "ai":
            self.run_ai_search(query)
        self.query_one("#table", DataTable).focus()

    def run_text_search(self, query: str) -> None:
        toks = aisearch._query_tokens(query) or [query]
        match = " OR ".join(f'"{t}"' for t in toks)
        ids: list[str] = []
        self.reasons = {}
        try:
            cur = self.conn.execute(
                """
                SELECT session_id,
                       bm25(messages_fts) AS rank,
                       snippet(messages_fts, 3, '«', '»', '…', 12) AS snip
                FROM messages_fts WHERE messages_fts MATCH ? ORDER BY rank LIMIT 500
                """,
                (match,),
            )
            for r in cur.fetchall():
                sid = r["session_id"]
                if sid not in self.reasons:
                    self.reasons[sid] = r["snip"]
                    ids.append(sid)
        except Exception as e:
            self.notify(f"search error: {e}", severity="error")
            return
        self.search_rows = [self._row_by_id(s) for s in ids if self._row_by_id(s)]
        self.rebuild_table(keep_cursor=False)
        self.call_after_refresh(self._fit_columns)  # match column now shown
        self.notify(f"{len(self.search_rows)} sessions matched")

    @work(thread=True, exclusive=True, group="aisearch")
    def run_ai_search(self, query: str) -> None:
        self.call_from_thread(self.notify, "asking Claude…", timeout=30)
        try:
            matches = aisearch.ai_search(self.conn, query)
        except aisearch.AISearchError as e:
            self.call_from_thread(self.notify, f"AI search failed: {e}", severity="error")
            return
        self.call_from_thread(self._apply_ai_results, matches)

    def _apply_ai_results(self, matches) -> None:
        self.reasons = {}
        rows = []
        for m in matches:
            row = self._row_by_id(m.id)
            if row:
                self.reasons[m.id] = f"[{m.score}] {m.reason}"
                rows.append(row)
        self.search_rows = rows
        self.rebuild_table(keep_cursor=False)
        self.call_after_refresh(self._fit_columns)  # match column now shown
        self.notify(f"AI found {len(rows)} sessions")

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
        self.rebuild_table()

    def action_cycle_group(self) -> None:
        self.group = GROUPS[(GROUPS.index(self.group) + 1) % len(GROUPS)]
        if self.group != "flat":
            self.sort = "recent"
        self.rebuild_table()

    def action_resume(self) -> None:
        sid = self._cursor_id
        if not sid or str(sid).startswith(SEP_PREFIX):
            return
        row = self._row_by_id(sid)
        if not row:
            return
        cwd = row.get("cwd") or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            self.notify(f"cwd gone ({cwd}); resuming from ~", severity="warning")
            cwd = os.path.expanduser("~")
        with self.suspend():
            os.system("clear")
            print(f"↻ resuming {sid}\n  in {cwd}\n")
            subprocess.run(["claude", "--resume", sid], cwd=cwd)
        self.reindex()

    @work(thread=True, exclusive=True, group="index")
    def reindex(self, full: bool = False) -> None:
        conn = db.connect(self.db_path)
        try:
            changed = indexer.index_all(conn, self.projects_dir, changed_only=not full)
            open_status = live.open_sessions(conn)
        finally:
            conn.close()
        # Always call back: open sessions can appear/disappear (or change
        # status) without any transcript on disk changing.
        self.call_from_thread(self._after_reindex, changed, open_status)

    def action_reindex(self) -> None:
        self.notify("re-indexing…")
        self.reindex(full=True)

    def _after_reindex(self, changed: list[str], open_status: dict[str, str]) -> None:
        open_changed = open_status != self._open_status
        self._open_status = open_status
        if changed:
            self.load_all()          # reloads rows + re-applies active dots
        else:
            self._apply_active()
        # Rebuild when content changed or the open/status set moved.
        if (changed or open_changed) and self.mode in ("browse", "fuzzy"):
            self.rebuild_table()
        if changed and self._preview_id in changed:
            self._render_preview(self._preview_id)

    def _apply_active(self) -> None:
        for r in self.all_sessions:
            r["open_status"] = self._open_status.get(r["id"])
            r["is_active"] = r["open_status"] is not None
