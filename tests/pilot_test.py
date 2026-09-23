import asyncio
import json
import sys
from unittest import mock

from ccs.tui import CCSApp


async def main():
    app = CCSApp()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause(0.2)
        table = app.query_one("#table")
        base = table.row_count
        print(f"mounted OK — table rows: {base}")
        assert base > 0, "no rows populated"

        # preview should render for the first row
        await pilot.pause(0.4)
        pv = app.query_one("#preview")
        print(f"preview lines: {len(pv.lines)}  (id={app._preview_id})")
        assert len(pv.lines) > 0, "preview empty"
        assert pv.virtual_size.width <= pv.size.width + 1, "preview wider than its pane"

        # a live table rebuild must not move the cursor or yank the preview
        for _ in range(4):
            await pilot.press("down")
        await pilot.pause(0.5)
        sid = app._cursor_id
        pv.scroll_to(y=0, animate=False)
        await pilot.pause(0.1)
        app.rebuild_table(keep_viewport=True)
        await pilot.pause(0.5)
        print(f"after live rebuild: same row={app._cursor_id == sid} preview y={pv.scroll_y}")
        assert app._cursor_id == sid and pv.scroll_y == 0, "live rebuild disturbed the view"

        # fuzzy filter (any common substring; just needs to narrow the list)
        await pilot.press("slash")
        for ch in "fix":
            await pilot.press(ch)
        await pilot.pause(0.1)
        print(f"fuzzy 'fix' rows: {table.row_count}")
        assert table.row_count <= base
        await pilot.press("escape")
        await pilot.pause(0.1)
        print(f"after escape rows: {table.row_count}  mode={app.mode} fuzzy={app.fuzzy!r}")
        # >= 1, not == base: the live re-index may add new sessions mid-test.
        assert app.mode == "browse" and app.fuzzy == ""
        assert table.row_count >= 1

        # sort + group cycling shouldn't crash
        await pilot.press("s")
        await pilot.pause(0.05)
        print(f"sort -> {app.sort}")
        await pilot.press("g")
        await pilot.pause(0.05)
        print(f"group -> {app.group}  rows(with separators): {table.row_count}")
        await pilot.press("g")
        await pilot.press("g")
        await pilot.pause(0.05)
        print(f"group -> {app.group}")

        # full-text search (any term; count not asserted, just exercises FTS)
        await pilot.press("f")
        for ch in "error":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.3)
        print(f"full-text 'error' rows: {table.row_count}  mode={app.mode}")
        mw = [c.width for c in table.ordered_columns if c.key.value == "match"][0]
        assert mw >= 20, f"match column too narrow ({mw})"
        assert table.virtual_size.width <= table.size.width, "table scrolls sideways"

        # AI search runs in a worker thread (own sqlite conn); claude is mocked.
        await pilot.press("escape")
        some_id = next(iter(app._by_id))
        env = {"result": json.dumps([{"id": some_id, "score": 90, "reason": "mock"}]),
               "is_error": False}
        fake = mock.Mock(returncode=0, stdout=json.dumps(env), stderr="")
        with mock.patch("ccs.aisearch.subprocess.run", return_value=fake):
            await pilot.press("a")
            for ch in "anything":
                await pilot.press(ch)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
        print(f"AI rows: {table.row_count}  mode={app.mode}")
        assert app.mode == "ai" and table.row_count == 1, "AI search path broken"

        # opening a search prompt without submitting must not blank the list
        await pilot.press("escape")
        await pilot.press("f")
        await pilot.pause(0.1)
        assert app.mode == "browse" and table.row_count >= 1

        # toggle preview off/on
        await pilot.press("escape")
        await pilot.press("p")
        await pilot.pause(0.05)
        print(f"preview visible: {app._preview_visible}")
        await pilot.press("p")
        await pilot.pause(0.05)
        # terminal resize: table re-fits and the preview reflows to the new width
        await pilot.resize_terminal(220, 40)
        await pilot.pause(1.0)
        pv = app.query_one("#preview")
        widest = max((l.cell_length for l in pv.lines), default=0)
        print(f"after resize: table {table.size.width} virt {table.virtual_size.width} "
              f"preview {pv.size.width} widest line {widest}")
        assert table.size.width > 96 and table.virtual_size.width <= table.size.width
        assert pv.size.width - 12 <= widest <= pv.size.width, "preview didn't reflow"
        print("all interactions OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
        print("\nPILOT TEST PASSED")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\nPILOT TEST FAILED")
        sys.exit(1)
