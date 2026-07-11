"""Export/import roundtrip: the durability guarantee.

Export/import moved off the MCP tool surface in 0.6.0 (they are CLI/DB ops),
so these exercise ctx.database directly.
"""

import os

from ctx.database import (
    export_project, export_all, import_project, render_project_markdown,
    get_project, reset_project, add_bug, add_project_message, set_doc, get_doc,
)

REPO = r"C:\work\exp" if os.name == "nt" else "/work/exp"


def _populate(call_tool):
    call_tool("ctx_update", {"project": "exp", "repo_path": REPO, "tool_name": "t"})
    call_tool("ctx_update", {
        "project": "exp",
        "session_summary": "Stack: Python. Built src/core.py. Currently working on export.",
        "tool_name": "t", "repo_path": REPO,
    })
    add_project_message("exp", "important note", author="u")
    add_bug("exp", "known issue")


def test_export_single_project(call_tool):
    _populate(call_tool)
    data = export_project("exp")
    assert data["project"] == "exp"
    assert data["repo_path"] == REPO
    assert "src/core.py" in data["map"]
    assert len(data["bugs"]) == 1
    assert len(data["messages"]) == 1
    assert len(data["update_log"]) >= 1


def test_export_unknown_project(call_tool):
    assert "error" in export_project("ghost")


def test_export_all(call_tool):
    _populate(call_tool)
    call_tool("ctx_update", {"project": "second", "session_summary": "x", "tool_name": "t"})
    data = export_all()
    assert data["format"] == "one-context-export"
    names = {p["project"] for p in data["projects"]}
    assert {"exp", "second"} <= names


def test_export_markdown_readable(call_tool):
    _populate(call_tool)
    text = render_project_markdown(export_project("exp"))
    assert "# exp" in text
    assert "## WHAT" in text and "## BUGS" in text
    assert "- [ ] known issue" in text


def test_roundtrip_export_wipe_import(call_tool):
    _populate(call_tool)
    before = call_tool("ctx_get", {"project": "exp"})
    exported = export_project("exp")

    reset_project("exp")
    wiped = call_tool("ctx_get", {"project": "exp"})
    assert wiped["done"] == "" and wiped["bugs"] == []

    r = import_project(exported, mode="replace")
    assert "error" not in r

    after = call_tool("ctx_get", {"project": "exp"})
    for bucket in ("what", "done", "now", "map"):
        assert after[bucket] == before[bucket], f"bucket {bucket} not restored"
    assert len(after["bugs"]) == 1


def test_import_merge_does_not_overwrite(call_tool):
    _populate(call_tool)
    exported = export_project("exp")

    call_tool("ctx_update", {
        "project": "exp", "session_summary": "Stack: switched to Go.",
        "tool_name": "t", "repo_path": REPO,
    })
    current_what = call_tool("ctx_get", {"project": "exp"})["what"]
    assert "Go" in current_what

    assert "error" not in import_project(exported, mode="merge")
    # merge keeps existing non-empty buckets
    assert call_tool("ctx_get", {"project": "exp"})["what"] == current_what


def test_import_is_idempotent(call_tool):
    _populate(call_tool)
    exported = export_project("exp")

    import_project(exported, mode="merge")
    r = import_project(exported, mode="merge")
    stats = r["imported"]
    assert stats["updates"] == 0 and stats["messages"] == 0 and stats["bugs"] == 0

    assert len(get_project("exp")) and len([b for b in export_project("exp")["bugs"]]) == 1


def test_import_creates_missing_project(call_tool):
    _populate(call_tool)
    exported = export_project("exp")
    exported["project"] = "restored-elsewhere"
    r = import_project(exported)
    assert r["project"] == "restored-elsewhere"
    assert "src/core.py" in r["map"]


def test_import_bad_payloads(call_tool):
    assert "error" in import_project({}, mode="merge")  # no project field
    assert "error" in import_project({"project": "x"}, mode="sideways")  # bad mode


def test_import_all_document(call_tool):
    _populate(call_tool)
    call_tool("ctx_update", {"project": "second", "session_summary": "y", "tool_name": "t"})
    doc = export_all()

    reset_project("exp")
    results = [import_project(p, mode="replace") for p in doc["projects"]]
    assert {r["project"] for r in results} >= {"exp", "second"}


def test_docs_survive_export_import_roundtrip(call_tool):
    _populate(call_tool)
    set_doc("exp", "instructions", "rule A")
    set_doc("exp", "plan", "step 1")

    exported = export_project("exp")
    reset_project("exp")
    assert "error" in get_doc("exp", "plan")  # reset cleared docs

    import_project(exported, mode="replace")
    assert get_doc("exp", "instructions")["content"] == "rule A"
    assert get_doc("exp", "plan")["content"] == "step 1"
