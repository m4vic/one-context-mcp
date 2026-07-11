"""Happy-path coverage for the 5 MCP tools + the behaviors folded into them.

Capabilities that moved to the CLI/DB layer in 0.6.0 (reset, list, bugs,
export/import) are covered directly against ctx.database here and in
test_export_import.py.
"""

import os

from ctx.database import reset_project, list_projects, add_bug, set_bug_status, list_bugs

REPO_A = os.path.join(os.sep, "work", "projA") if os.name != "nt" else r"C:\work\projA"
REPO_B = os.path.join(os.sep, "work", "projB") if os.name != "nt" else r"C:\work\projB"


# --- ctx_update: link / merge / note / map folds -----------------------------

def test_update_links_project(call_tool):
    # Empty summary + repo_path just links/creates the project (folds ctx_link).
    r = call_tool("ctx_update", {"project": "alpha", "repo_path": REPO_A, "tool_name": "t"})
    assert r["project"] == "alpha"
    assert r["repo_path"] == REPO_A
    assert r.get("status") == "linked"


def test_update_links_existing_unlinked_project(call_tool):
    # A project created without a repo_path (e.g. by a summary-only update)
    # must still be linkable later via ctx_update (folds ctx_link).
    call_tool("ctx_update", {"project": "late-link", "session_summary": "hello", "tool_name": "t"})
    r = call_tool("ctx_update", {"project": "late-link", "repo_path": REPO_A, "tool_name": "t"})
    assert "error" not in r
    assert r["repo_path"] == REPO_A

    # ...but it must not claim a folder owned by another project.
    call_tool("ctx_update", {"project": "thief", "session_summary": "hi", "tool_name": "t"})
    r = call_tool("ctx_update", {"project": "thief", "repo_path": REPO_A, "tool_name": "t"})
    assert "error" in r
    assert r["linked_project"] == "late-link"


def test_update_merges_and_extracts_map(call_tool):
    call_tool("ctx_update", {"project": "alpha", "repo_path": REPO_A, "tool_name": "t"})
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
        "project": "brand-new", "session_summary": "hello world", "tool_name": "t",
    })
    assert "error" not in r
    assert r["project"] == "brand-new"


def test_unlinked_project_supports_repeated_updates(call_tool):
    for i in range(3):
        r = call_tool("ctx_update", {
            "project": "no-link", "session_summary": f"update {i}", "tool_name": "t",
        })
        assert "error" not in r, f"update {i} failed: {r}"
    assert "update 2" in r["done"]


def test_update_with_files_registers_map(call_tool):
    call_tool("ctx_update", {"project": "alpha", "repo_path": REPO_A, "tool_name": "t"})
    r = call_tool("ctx_update", {
        "project": "alpha", "tool_name": "t", "repo_path": REPO_A,
        "files": ["src/a.py - entry", "src/b.py - helper"],
    })
    assert r["files_registered"] == 2
    assert "src/a.py" in r["map"] and "src/b.py" in r["map"]

    # Files dedupe by path across calls
    r = call_tool("ctx_update", {
        "project": "alpha", "tool_name": "t", "repo_path": REPO_A,
        "files": ["src/a.py - entry point"],
    })
    assert r["map"].count("src/a.py") == 1


def test_update_with_author_saves_note(call_tool):
    call_tool("ctx_update", {"project": "alpha", "repo_path": REPO_A, "tool_name": "t"})
    r = call_tool("ctx_update", {
        "project": "alpha", "session_summary": "Remember the deadline.",
        "tool_name": "t", "author": "tester", "repo_path": REPO_A,
    })
    assert r["note_saved"] is True
    assert r["note"]["author"] == "tester"
    # The note is also merged into the buckets and appears in detailed history.
    d = call_tool("ctx_get", {"project": "alpha", "view": "detailed"})
    assert any("Remember the deadline" in m["message"] for m in d["notes"])


# --- ctx_get: resolve / mismatch-warning / detailed folds --------------------

def test_get_unknown_project_errors(call_tool):
    r = call_tool("ctx_get", {"project": "ghost"})
    assert "error" in r


def test_get_resolves_project_from_repo_path(call_tool):
    call_tool("ctx_update", {"project": "alpha", "repo_path": REPO_A, "tool_name": "t"})
    # Omit project -> resolve from the folder (folds ctx_resolve).
    r = call_tool("ctx_get", {"repo_path": REPO_A})
    assert r["project"] == "alpha"
    # A subfolder resolves to the same project.
    sub = os.path.join(REPO_A, "src", "deep")
    r = call_tool("ctx_get", {"repo_path": sub})
    assert r["project"] == "alpha"


def test_get_resolve_miss_errors(call_tool):
    r = call_tool("ctx_get", {"repo_path": REPO_B})
    assert "error" in r


def test_get_warns_on_mismatch(call_tool):
    call_tool("ctx_update", {"project": "alpha", "repo_path": REPO_A, "tool_name": "t"})
    r = call_tool("ctx_get", {"project": "alpha", "repo_path": REPO_B})
    assert r["safety"]["warning"] == "repo_path mismatch"


def test_get_detailed_returns_history(call_tool):
    call_tool("ctx_update", {"project": "alpha", "repo_path": REPO_A, "tool_name": "t"})
    call_tool("ctx_update", {"project": "alpha", "session_summary": "did a thing", "tool_name": "t", "repo_path": REPO_A})
    call_tool("ctx_update", {"project": "alpha", "session_summary": "a note", "tool_name": "t", "author": "u", "repo_path": REPO_A})
    r = call_tool("ctx_get", {"project": "alpha", "view": "detailed"})
    assert len(r["updates"]) >= 1
    assert len(r["notes"]) == 1


# --- ctx_search --------------------------------------------------------------

def test_search_across_projects(call_tool):
    call_tool("ctx_update", {"project": "p1", "session_summary": "Uses PostgreSQL for storage", "tool_name": "t"})
    call_tool("ctx_update", {"project": "p2", "session_summary": "Uses Redis for cache", "tool_name": "t"})
    r = call_tool("ctx_search", {"query": "PostgreSQL"})
    assert r["total_matches"] >= 1
    assert any(m["project"] == "p1" for m in r["history"])


def test_search_scoped_to_project(call_tool):
    call_tool("ctx_update", {"project": "p1", "session_summary": "shared keyword alpha", "tool_name": "t"})
    call_tool("ctx_update", {"project": "p2", "session_summary": "shared keyword alpha", "tool_name": "t"})
    r = call_tool("ctx_search", {"query": "alpha", "project": "p1"})
    assert r["scope"] == "p1"
    assert all(m["project"] == "p1" for m in r["history"])


def test_search_scope_survives_global_limit(call_tool):
    # Scoping happens in SQL: even when another project has enough matches to
    # fill the global LIMIT (20), a scoped search still finds its project's hit.
    for i in range(25):
        call_tool("ctx_update", {"project": "noisy", "session_summary": f"needle variant {i}", "tool_name": "t"})
    call_tool("ctx_update", {"project": "quiet", "session_summary": "needle in quiet", "tool_name": "t"})
    r = call_tool("ctx_search", {"query": "needle", "project": "quiet"})
    assert any("quiet" in h["summary"] for h in r["history"])
    assert all(h["project"] == "quiet" for h in r["history"])


def test_unknown_tool(call_tool):
    r = call_tool("ctx_nonsense", {})
    assert "error" in r


# --- capabilities that moved to CLI/DB (covered against the DB layer) ---------

def test_reset_clears_context(call_tool, fresh_db):
    call_tool("ctx_update", {"project": "alpha", "session_summary": "something", "tool_name": "t"})
    assert reset_project("alpha")["status"] == "reset"
    g = call_tool("ctx_get", {"project": "alpha"})
    assert g["done"] == ""


def test_list_projects_includes_repo_path(call_tool, fresh_db):
    call_tool("ctx_update", {"project": "alpha", "repo_path": REPO_A, "tool_name": "t"})
    entry = next(p for p in list_projects() if p["project"] == "alpha")
    assert entry["repo_path"] == REPO_A


def test_bugs_db_lifecycle_and_ctx_get_surfacing(call_tool, fresh_db):
    call_tool("ctx_update", {"project": "alpha", "repo_path": REPO_A, "tool_name": "t"})
    b1 = add_bug("alpha", "crash on empty input")
    b2 = add_bug("alpha", "slow query")
    assert b1["status"] == "open" and b2["status"] == "open"
    assert len(list_bugs("alpha")) == 2

    assert set_bug_status("alpha", b1["id"], "fixed")["status"] == "fixed"

    # ctx_get still surfaces open bugs + fixed count (read-only display).
    g = call_tool("ctx_get", {"project": "alpha"})
    assert [b["id"] for b in g["bugs"]] == [b2["id"]]
    assert g["bugs_fixed_count"] == 1
