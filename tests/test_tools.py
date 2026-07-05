"""Happy-path coverage for every MCP tool, exercised in-process."""

import os

REPO_A = os.path.join(os.sep, "work", "projA") if os.name != "nt" else r"C:\work\projA"
REPO_B = os.path.join(os.sep, "work", "projB") if os.name != "nt" else r"C:\work\projB"


def test_link_creates_and_relinks(call_tool):
    r = call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    assert r["project"] == "alpha"
    assert r["repo_path"] == REPO_A

    # Linking again with a new path updates it
    r = call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_B})
    assert r["repo_path"] == REPO_B


def test_get_unknown_project_errors(call_tool):
    r = call_tool("ctx_get", {"project": "ghost"})
    assert "error" in r


def test_update_merges_and_extracts_map(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    r = call_tool("ctx_update", {
        "project": "alpha",
        "session_summary": "Stack: Python + FastAPI. Built endpoints in src/api.py. Currently working on auth in src/auth.py.",
        "tool_name": "test-tool",
        "repo_path": REPO_A,
    })
    assert "error" not in r
    assert "src/api.py" in r["map"]
    assert "src/auth.py" in r["map"]
    assert "FastAPI" in r["what"]  # 'Stack:' keyword routes into WHAT
    assert r["now"]  # 'Currently working on' routes into NOW
    assert "test-tool" in r["done"]


def test_update_auto_inits_unknown_project(call_tool):
    r = call_tool("ctx_update", {
        "project": "brand-new",
        "session_summary": "hello world",
        "tool_name": "t",
    })
    assert "error" not in r
    assert r["project"] == "brand-new"


def test_unlinked_project_supports_repeated_updates(call_tool):
    """Regression: strict guard used to reject the 2nd update of any
    project that had no linked repo_path, even with no repo_path passed."""
    for i in range(3):
        r = call_tool("ctx_update", {
            "project": "no-link",
            "session_summary": f"update {i}",
            "tool_name": "t",
        })
        assert "error" not in r, f"update {i} failed: {r}"
    assert "update 2" in r["done"]


def test_strict_get_match_and_mismatch(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    ok = call_tool("ctx_strict_get", {"project": "alpha", "repo_path": REPO_A})
    assert "error" not in ok

    bad = call_tool("ctx_strict_get", {"project": "alpha", "repo_path": REPO_B})
    assert bad.get("error") == "repo_path mismatch"


def test_strict_get_requires_link(call_tool):
    call_tool("ctx_update", {"project": "unlinked", "session_summary": "x", "tool_name": "t"})
    r = call_tool("ctx_strict_get", {"project": "unlinked", "repo_path": REPO_A})
    assert "error" in r


def test_get_warns_on_mismatch(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    r = call_tool("ctx_get", {"project": "alpha", "repo_path": REPO_B})
    assert r["safety"]["warning"] == "repo_path mismatch"


def test_map_append_and_replace(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    r = call_tool("ctx_map", {"project": "alpha", "files": ["src/a.py - entry", "src/b.py - helper"]})
    assert "src/a.py" in r["map"] and "src/b.py" in r["map"]

    # Append dedupes by path
    r = call_tool("ctx_map", {"project": "alpha", "files": ["src/a.py - entry point"]})
    assert r["map"].count("src/a.py") == 1

    # Replace wipes previous entries
    r = call_tool("ctx_map", {"project": "alpha", "files": ["src/c.py - new"], "replace": True})
    assert "src/c.py" in r["map"] and "src/a.py" not in r["map"]


def test_note_merge_and_no_merge(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    r = call_tool("ctx_note", {"project": "alpha", "message": "Remember the deadline.", "author": "tester"})
    assert r["context_updated"] is True
    assert r["message"]["author"] == "tester"

    r = call_tool("ctx_note", {"project": "alpha", "message": "Just store this.", "merge": False})
    assert r["context_updated"] is False


def test_note_rejects_empty_message(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    r = call_tool("ctx_note", {"project": "alpha", "message": "   "})
    assert "error" in r


def test_history_returns_updates_and_messages(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    call_tool("ctx_update", {"project": "alpha", "session_summary": "did a thing", "tool_name": "t"})
    call_tool("ctx_note", {"project": "alpha", "message": "a note"})
    r = call_tool("ctx_history", {"project": "alpha", "limit": 10})
    assert len(r["updates"]) >= 1
    assert len(r["messages"]) == 1


def test_search_across_projects(call_tool):
    call_tool("ctx_update", {"project": "p1", "session_summary": "Uses PostgreSQL for storage", "tool_name": "t"})
    call_tool("ctx_update", {"project": "p2", "session_summary": "Uses Redis for cache", "tool_name": "t"})
    r = call_tool("ctx_search", {"query": "PostgreSQL"})
    assert r["total_matches"] >= 1
    assert any(m["project"] == "p1" for m in r["history"])


def test_reset_clears_context_and_history(call_tool):
    call_tool("ctx_update", {"project": "alpha", "session_summary": "something", "tool_name": "t"})
    r = call_tool("ctx_reset", {"project": "alpha"})
    assert r["status"] == "reset"
    g = call_tool("ctx_get", {"project": "alpha"})
    assert g["done"] == ""
    h = call_tool("ctx_history", {"project": "alpha"})
    assert h["updates"] == [] and h["messages"] == []


def test_list_returns_projects_with_repo_path(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    r = call_tool("ctx_list", {})
    assert isinstance(r, list)
    entry = next(p for p in r if p["project"] == "alpha")
    assert entry["repo_path"] == REPO_A


def test_bug_lifecycle(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})

    b1 = call_tool("ctx_bug", {"project": "alpha", "description": "crash on empty input"})
    b2 = call_tool("ctx_bug", {"project": "alpha", "description": "slow query"})
    assert b1["status"] == "open" and b2["status"] == "open"

    listed = call_tool("ctx_bug", {"project": "alpha"})
    assert len(listed["bugs"]) == 2

    fixed = call_tool("ctx_bug", {"project": "alpha", "bug_id": b1["id"], "status": "fixed"})
    assert fixed["status"] == "fixed"

    # Open bugs surface on ctx_get; fixed ones are counted
    g = call_tool("ctx_get", {"project": "alpha"})
    assert [b["id"] for b in g["bugs"]] == [b2["id"]]
    assert g["bugs_fixed_count"] == 1

    # Bugs appear in search
    s = call_tool("ctx_search", {"query": "slow query"})
    assert any(b["id"] == b2["id"] for b in s["bugs"])


def test_bug_error_paths(call_tool):
    call_tool("ctx_link", {"project": "alpha", "repo_path": REPO_A})
    r = call_tool("ctx_bug", {"project": "alpha", "bug_id": 999, "status": "fixed"})
    assert "error" in r
    b = call_tool("ctx_bug", {"project": "alpha", "description": "x"})
    r = call_tool("ctx_bug", {"project": "alpha", "bug_id": b["id"], "status": "wontfix"})
    assert "error" in r
    r = call_tool("ctx_bug", {"project": "alpha", "bug_id": b["id"]})
    assert "error" in r  # bug_id without status
    r = call_tool("ctx_bug", {"project": "nope"})
    assert "error" in r  # list on unknown project


def test_unknown_tool(call_tool):
    r = call_tool("ctx_nonsense", {})
    assert "error" in r
