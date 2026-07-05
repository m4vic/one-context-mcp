"""Database-layer unit tests: MAP normalization, bug CRUD, path lookup."""

import os

from ctx.database import (
    add_bug,
    count_bugs,
    find_project_by_repo_path,
    init_project,
    list_bugs,
    merge_map_content,
    normalize_map_content,
    set_bug_status,
)


# --- MAP normalization -----------------------------------------------------

def test_normalize_dedupes_paths_keeps_first_note():
    raw = "- src/a.py - entry\n- src/a.py - duplicate note\n- src/b.py"
    out = normalize_map_content(raw)
    assert out.count("src/a.py") == 1
    assert "entry" in out and "duplicate note" not in out


def test_normalize_backfills_missing_note():
    raw = "- src/a.py\n- src/a.py - the real note"
    out = normalize_map_content(raw)
    assert out == "- src/a.py - the real note"


def test_normalize_handles_bullet_styles_and_separators():
    raw = "* src/a.py -- double dash\n- src/b.py — em dash"
    out = normalize_map_content(raw)
    assert "double dash" in out and "em dash" in out


def test_normalize_dedupes_across_slash_direction_and_case():
    raw = "- src/a.py - one\n- SRC\\A.PY - two"
    out = normalize_map_content(raw)
    # Same file either way it's written; first entry wins
    assert len(out.splitlines()) == 1
    assert "one" in out and "two" not in out


def test_merge_map_content_empty_sides():
    assert merge_map_content("", "- a.py - x") == "- a.py - x"
    assert merge_map_content("- a.py - x", "") == "- a.py - x"
    merged = merge_map_content("- a.py - x", "- b.py - y")
    assert "a.py" in merged and "b.py" in merged


# --- Bugs ------------------------------------------------------------------

def test_bug_crud(fresh_db):
    init_project("p")
    b = add_bug("p", "it breaks")
    assert b["status"] == "open"

    bugs = list_bugs("p")
    assert len(bugs) == 1

    updated = set_bug_status("p", b["id"], "fixed")
    assert updated["status"] == "fixed"
    assert count_bugs("p", "fixed") == 1
    assert count_bugs("p", "open") == 0

    # open-first ordering
    b2 = add_bug("p", "another")
    ordered = list_bugs("p")
    assert ordered[0]["id"] == b2["id"]  # open before fixed


def test_bug_rejects_empty_description(fresh_db):
    init_project("p")
    assert "error" in add_bug("p", "   ")


def test_bug_unknown_project(fresh_db):
    assert "error" in add_bug("ghost", "x")
    assert "error" in set_bug_status("ghost", 1, "fixed")


def test_bug_invalid_status(fresh_db):
    init_project("p")
    b = add_bug("p", "x")
    assert "error" in set_bug_status("p", b["id"], "maybe")


# --- Reverse path lookup ---------------------------------------------------

def test_find_project_by_repo_path(fresh_db):
    root = r"C:\repos\alpha" if os.name == "nt" else "/repos/alpha"
    init_project("alpha", repo_path=root)

    hit = find_project_by_repo_path(root)
    assert hit["project"] == "alpha"

    sub = os.path.join(root, "src", "deep")
    assert find_project_by_repo_path(sub)["project"] == "alpha"

    assert find_project_by_repo_path("") is None
    other = r"C:\elsewhere" if os.name == "nt" else "/elsewhere"
    assert find_project_by_repo_path(other) is None


def test_find_project_ignores_prefix_sibling(fresh_db):
    """/repos/alpha must not match /repos/alphabet (prefix but not ancestor)."""
    root = r"C:\repos\alpha" if os.name == "nt" else "/repos/alpha"
    sibling = root + "bet"
    init_project("alpha", repo_path=root)
    assert find_project_by_repo_path(sibling) is None
