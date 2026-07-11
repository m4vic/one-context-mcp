"""Project isolation: folder ownership guards and reverse lookup.

In 0.6.0 linking and resolving are folded into ctx_update/ctx_get; explicit
folder linking (the old ctx_link) is done via init_project (also the CLI
`ctx init`).
"""

import os

from ctx.database import init_project

if os.name == "nt":
    LAB = r"F:\lab"
    OTHER = r"F:\other"
    LAB_SUB = r"f:\LAB\src\deep"  # case-insensitive + subfolder on Windows
else:
    LAB = "/work/lab"
    OTHER = "/work/other"
    LAB_SUB = "/work/lab/src/deep"


def _setup(call_tool):
    init_project("lab", repo_path=LAB)
    init_project("other", repo_path=OTHER)


def test_typo_split_blocked_on_update(call_tool):
    _setup(call_tool)
    r = call_tool("ctx_update", {
        "project": "labb",  # typo - unknown project, but folder belongs to 'lab'
        "session_summary": "x", "tool_name": "t", "repo_path": LAB,
    })
    assert "error" in r
    assert r["linked_project"] == "lab"


def test_typo_split_blocked_on_note_and_files(call_tool):
    _setup(call_tool)
    # note fold (author) and map fold (files) both go through ctx_update, so
    # both hit the same auto-init guard.
    r = call_tool("ctx_update", {"project": "la-b", "session_summary": "x", "author": "u", "tool_name": "t", "repo_path": LAB})
    assert r.get("linked_project") == "lab"
    r = call_tool("ctx_update", {"project": "labs", "files": ["a.py - x"], "tool_name": "t", "repo_path": LAB})
    assert r.get("linked_project") == "lab"


def test_get_resolves_exact_and_subfolder(call_tool):
    _setup(call_tool)
    assert call_tool("ctx_get", {"repo_path": LAB})["project"] == "lab"
    assert call_tool("ctx_get", {"repo_path": LAB_SUB})["project"] == "lab"


def test_get_resolve_unknown_folder(call_tool):
    _setup(call_tool)
    unknown = r"C:\nowhere" if os.name == "nt" else "/nowhere"
    r = call_tool("ctx_get", {"repo_path": unknown})
    assert "error" in r


def test_resolve_prefers_deepest_link(call_tool):
    """A repo nested inside another linked folder resolves to the nested project."""
    init_project("outer", repo_path=LAB)
    nested = os.path.join(LAB, "vendor", "inner")
    init_project("inner", repo_path=nested)
    r = call_tool("ctx_get", {"repo_path": os.path.join(nested, "src")})
    assert r["project"] == "inner"


def test_legit_auto_init_in_unlinked_folder(call_tool):
    _setup(call_tool)
    fresh = r"F:\fresh" if os.name == "nt" else "/work/fresh"
    r = call_tool("ctx_update", {
        "project": "fresh", "session_summary": "new project bootstrap",
        "tool_name": "t", "repo_path": fresh,
    })
    assert "error" not in r


def test_correct_project_update_unaffected(call_tool):
    _setup(call_tool)
    r = call_tool("ctx_update", {
        "project": "lab", "session_summary": "normal work", "tool_name": "t", "repo_path": LAB,
    })
    assert "error" not in r


def test_update_rejects_mismatched_folder_for_existing_project(call_tool):
    _setup(call_tool)
    r = call_tool("ctx_update", {
        "project": "lab", "session_summary": "x", "tool_name": "t",
        "repo_path": OTHER,  # lab exists but is linked elsewhere
    })
    assert r.get("error") == "repo_path mismatch"
