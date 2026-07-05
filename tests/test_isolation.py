"""Project isolation: folder ownership guards and reverse lookup (ctx_resolve)."""

import os

if os.name == "nt":
    LAB = r"F:\lab"
    OTHER = r"F:\other"
    LAB_SUB = r"f:\LAB\src\deep"  # case-insensitive + subfolder on Windows
else:
    LAB = "/work/lab"
    OTHER = "/work/other"
    LAB_SUB = "/work/lab/src/deep"


def _setup(call_tool):
    call_tool("ctx_link", {"project": "lab", "repo_path": LAB})
    call_tool("ctx_link", {"project": "other", "repo_path": OTHER})


def test_typo_split_blocked_on_update(call_tool):
    _setup(call_tool)
    r = call_tool("ctx_update", {
        "project": "labb",  # typo - unknown project, but folder belongs to 'lab'
        "session_summary": "x",
        "tool_name": "t",
        "repo_path": LAB,
    })
    assert "error" in r
    assert r["linked_project"] == "lab"


def test_typo_split_blocked_on_note_and_map(call_tool):
    _setup(call_tool)
    r = call_tool("ctx_note", {"project": "la-b", "message": "x", "repo_path": LAB})
    assert r.get("linked_project") == "lab"
    r = call_tool("ctx_map", {"project": "labs", "files": ["a.py - x"], "repo_path": LAB})
    assert r.get("linked_project") == "lab"


def test_resolve_exact_and_subfolder(call_tool):
    _setup(call_tool)
    r = call_tool("ctx_resolve", {"repo_path": LAB})
    assert r["project"] == "lab"
    r = call_tool("ctx_resolve", {"repo_path": LAB_SUB})
    assert r["project"] == "lab"


def test_resolve_unknown_folder(call_tool):
    _setup(call_tool)
    unknown = r"C:\nowhere" if os.name == "nt" else "/nowhere"
    r = call_tool("ctx_resolve", {"repo_path": unknown})
    assert "error" in r


def test_resolve_prefers_deepest_link(call_tool):
    """A repo nested inside another linked folder resolves to the nested project."""
    call_tool("ctx_link", {"project": "outer", "repo_path": LAB})
    nested = os.path.join(LAB, "vendor", "inner")
    call_tool("ctx_link", {"project": "inner", "repo_path": nested})
    r = call_tool("ctx_resolve", {"repo_path": os.path.join(nested, "src")})
    assert r["project"] == "inner"


def test_legit_auto_init_in_unlinked_folder(call_tool):
    _setup(call_tool)
    fresh = r"F:\fresh" if os.name == "nt" else "/work/fresh"
    r = call_tool("ctx_update", {
        "project": "fresh",
        "session_summary": "new project bootstrap",
        "tool_name": "t",
        "repo_path": fresh,
    })
    assert "error" not in r


def test_correct_project_update_unaffected(call_tool):
    _setup(call_tool)
    r = call_tool("ctx_update", {
        "project": "lab",
        "session_summary": "normal work",
        "tool_name": "t",
        "repo_path": LAB,
    })
    assert "error" not in r


def test_update_rejects_mismatched_folder_for_existing_project(call_tool):
    _setup(call_tool)
    r = call_tool("ctx_update", {
        "project": "lab",
        "session_summary": "x",
        "tool_name": "t",
        "repo_path": OTHER,  # lab exists but is linked elsewhere
    })
    assert r.get("error") == "repo_path mismatch"
