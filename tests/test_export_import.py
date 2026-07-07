"""Export/import roundtrip: the durability guarantee."""

import json
import os

REPO = r"C:\work\exp" if os.name == "nt" else "/work/exp"


def _populate(call_tool):
    call_tool("ctx_link", {"project": "exp", "repo_path": REPO})
    call_tool("ctx_update", {
        "project": "exp",
        "session_summary": "Stack: Python. Built src/core.py. Currently working on export.",
        "tool_name": "t",
        "repo_path": REPO,
    })
    call_tool("ctx_note", {"project": "exp", "message": "important note", "author": "u"})
    call_tool("ctx_bug", {"project": "exp", "description": "known issue"})


def test_export_single_project_json(call_tool):
    _populate(call_tool)
    data = call_tool("ctx_export", {"project": "exp"})
    assert data["project"] == "exp"
    assert data["repo_path"] == REPO
    assert "src/core.py" in data["map"]
    assert len(data["bugs"]) == 1
    assert len(data["messages"]) == 1
    assert len(data["update_log"]) >= 1


def test_export_unknown_project(call_tool):
    r = call_tool("ctx_export", {"project": "ghost"})
    assert "error" in r


def test_export_all(call_tool):
    _populate(call_tool)
    call_tool("ctx_update", {"project": "second", "session_summary": "x", "tool_name": "t"})
    data = call_tool("ctx_export", {})
    assert data["format"] == "one-context-export"
    names = {p["project"] for p in data["projects"]}
    assert {"exp", "second"} <= names


def test_export_markdown_readable(call_tool):
    _populate(call_tool)
    import asyncio
    from ctx.server import handle_call_tool
    result = asyncio.run(handle_call_tool("ctx_export", {"project": "exp", "format": "markdown"}))
    text = result[0].text
    assert "# exp" in text
    assert "## WHAT" in text and "## BUGS" in text
    assert "- [ ] known issue" in text


def test_roundtrip_export_wipe_import(call_tool):
    _populate(call_tool)
    before = call_tool("ctx_get", {"project": "exp"})
    exported = call_tool("ctx_export", {"project": "exp"})

    call_tool("ctx_reset", {"project": "exp"})
    wiped = call_tool("ctx_get", {"project": "exp"})
    assert wiped["done"] == "" and wiped["bugs"] == []

    r = call_tool("ctx_import", {"data": json.dumps(exported), "mode": "replace"})
    assert "error" not in r

    after = call_tool("ctx_get", {"project": "exp"})
    for bucket in ("what", "done", "now", "map"):
        assert after[bucket] == before[bucket], f"bucket {bucket} not restored"
    assert len(after["bugs"]) == 1
    h = call_tool("ctx_history", {"project": "exp"})
    assert len(h["messages"]) == 1


def test_import_merge_does_not_overwrite(call_tool):
    _populate(call_tool)
    exported = call_tool("ctx_export", {"project": "exp"})

    # Change WHAT locally after export
    call_tool("ctx_update", {
        "project": "exp",
        "session_summary": "Stack: switched to Go.",
        "tool_name": "t",
        "repo_path": REPO,
    })
    current_what = call_tool("ctx_get", {"project": "exp"})["what"]
    assert "Go" in current_what

    r = call_tool("ctx_import", {"data": json.dumps(exported), "mode": "merge"})
    assert "error" not in r
    # merge keeps existing non-empty buckets
    assert call_tool("ctx_get", {"project": "exp"})["what"] == current_what


def test_import_is_idempotent(call_tool):
    _populate(call_tool)
    exported = json.dumps(call_tool("ctx_export", {"project": "exp"}))

    call_tool("ctx_import", {"data": exported, "mode": "merge"})
    r = call_tool("ctx_import", {"data": exported, "mode": "merge"})
    stats = r["imported"]
    assert stats["updates"] == 0 and stats["messages"] == 0 and stats["bugs"] == 0

    g = call_tool("ctx_get", {"project": "exp"})
    assert len(g["bugs"]) == 1  # no duplicates


def test_import_creates_missing_project(call_tool):
    _populate(call_tool)
    exported = call_tool("ctx_export", {"project": "exp"})
    exported["project"] = "restored-elsewhere"
    r = call_tool("ctx_import", {"data": json.dumps(exported)})
    assert r["project"] == "restored-elsewhere"
    assert "src/core.py" in r["map"]


def test_import_bad_payloads(call_tool):
    r = call_tool("ctx_import", {"data": "not json"})
    assert "error" in r
    r = call_tool("ctx_import", {"data": "[1, 2, 3]"})
    assert "error" in r
    r = call_tool("ctx_import", {"data": "{}"})
    assert "error" in r  # no project field
    r = call_tool("ctx_import", {"data": json.dumps({"project": "x"}), "mode": "sideways"})
    assert "error" in r


def test_import_all_document(call_tool):
    _populate(call_tool)
    call_tool("ctx_update", {"project": "second", "session_summary": "y", "tool_name": "t"})
    doc = call_tool("ctx_export", {})

    call_tool("ctx_reset", {"project": "exp"})
    r = call_tool("ctx_import", {"data": json.dumps(doc), "mode": "replace"})
    assert set(r["imported_projects"]) >= {"exp", "second"}


def test_import_accepts_object_not_just_string(call_tool):
    """The export result can be passed straight back as an object.

    This is what assistants naturally do (ctx_export returns an object), and
    requiring a hand-stringified JSON was the top import-failure cause.
    """
    _populate(call_tool)
    exported = call_tool("ctx_export", {"project": "exp"})  # a dict

    call_tool("ctx_reset", {"project": "exp"})
    # Pass the object directly - no json.dumps.
    r = call_tool("ctx_import", {"data": exported, "mode": "replace"})
    assert "error" not in r, r
    assert r["project"] == "exp"
    assert "src/core.py" in call_tool("ctx_get", {"project": "exp"})["map"]


def test_import_all_document_as_object(call_tool):
    """The all-projects export object also imports without stringifying."""
    _populate(call_tool)
    call_tool("ctx_update", {"project": "second", "session_summary": "y", "tool_name": "t"})
    doc = call_tool("ctx_export", {})  # {format, projects:[...]}

    r = call_tool("ctx_import", {"data": doc, "mode": "replace"})
    assert set(r["imported_projects"]) >= {"exp", "second"}
