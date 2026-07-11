"""Concurrent writers must never lose updates (the 0.3.0 race-condition fix)."""

import threading

from ctx.database import atomic_merge_update, get_project, init_project, merge_project_map


def test_no_lost_updates_under_concurrent_writers(fresh_db):
    init_project("race", repo_path="")

    n_workers = 40

    def worker(i: int):
        def merge(cur: dict) -> dict:
            return {
                "what": cur["what"],
                "done": (cur["done"] + f"\nX{i}").strip(),
                "now": cur["now"],
                "map": cur["map"],
            }

        atomic_merge_update("race", merge, tool_name=f"t{i}", summary=f"s{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    done = get_project("race")["done"]
    markers = sorted(int(line[1:]) for line in done.splitlines() if line.startswith("X"))
    assert markers == list(range(n_workers)), "concurrent writes were lost"


def test_no_lost_map_entries_under_concurrent_writers(fresh_db):
    # The ctx_update files= fold goes through merge_project_map; concurrent
    # registrations must all survive.
    init_project("maprace", repo_path="")

    n_workers = 20
    threads = [
        threading.Thread(target=merge_project_map, args=("maprace", f"- src/f{i}.py - part {i}"))
        for i in range(n_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final_map = get_project("maprace")["map"]
    for i in range(n_workers):
        assert f"src/f{i}.py" in final_map, "concurrent MAP merge lost an entry"


def test_atomic_update_unknown_project(fresh_db):
    r = atomic_merge_update("ghost", lambda cur: cur, tool_name="t", summary="s")
    assert "error" in r


def test_atomic_update_logs_each_write(fresh_db):
    init_project("p", repo_path="")
    for i in range(3):
        atomic_merge_update(
            "p",
            lambda cur: {**cur, "done": cur["done"] + f"\n- item {i}"},
            tool_name="t",
            summary=f"update {i}",
        )
    from ctx.database import list_project_history

    h = list_project_history("p")
    assert len(h["updates"]) == 3
