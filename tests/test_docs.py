"""Per-project verbatim docs (ctx_doc) and the how_to_ctx usage guide."""

import asyncio

from ctx.server import handle_call_tool
from ctx.database import reset_project


def _link(call_tool, project="doc"):
    call_tool("ctx_update", {"project": project, "session_summary": "seed", "tool_name": "t"})


def test_doc_set_and_get_is_verbatim(call_tool):
    _link(call_tool)
    plan = "# Implementation Plan\n1. do X\n2. do Y\n\nExact text: keep-me-100%."
    r = call_tool("ctx_doc", {"project": "doc", "kind": "plan", "content": plan})
    assert r["status"] == "saved" and r["kind"] == "plan"

    got = call_tool("ctx_doc", {"project": "doc", "kind": "plan"})
    assert got["content"] == plan  # nothing merged, summarized, or reordered


def test_doc_action_inferred_from_content(call_tool):
    _link(call_tool)
    # content present -> set
    call_tool("ctx_doc", {"project": "doc", "kind": "instructions", "content": "be terse"})
    # content absent -> get
    got = call_tool("ctx_doc", {"project": "doc", "kind": "instructions"})
    assert got["content"] == "be terse"


def test_doc_kind_is_case_insensitive(call_tool):
    _link(call_tool)
    call_tool("ctx_doc", {"project": "doc", "kind": "PLAN", "content": "p"})
    got = call_tool("ctx_doc", {"project": "doc", "kind": "plan"})
    assert got["content"] == "p"


def test_doc_list_index_has_no_content(call_tool):
    _link(call_tool)
    call_tool("ctx_doc", {"project": "doc", "kind": "plan", "content": "x" * 50})
    call_tool("ctx_doc", {"project": "doc", "kind": "context", "content": "y" * 30})
    r = call_tool("ctx_doc", {"project": "doc", "action": "list"})
    kinds = {d["kind"]: d["chars"] for d in r["docs"]}
    assert kinds == {"plan": 50, "context": 30}
    assert all("content" not in d for d in r["docs"])


def test_doc_delete(call_tool):
    _link(call_tool)
    call_tool("ctx_doc", {"project": "doc", "kind": "plan", "content": "p"})
    assert call_tool("ctx_doc", {"project": "doc", "kind": "plan", "action": "delete"})["status"] == "deleted"
    assert "error" in call_tool("ctx_doc", {"project": "doc", "kind": "plan"})


def test_doc_set_on_missing_project_errors(call_tool):
    r = call_tool("ctx_doc", {"project": "ghost", "kind": "plan", "content": "p"})
    assert "error" in r


def test_doc_capped(call_tool, monkeypatch):
    monkeypatch.setenv("CTX_MAX_DOC_CHARS", "1000")
    _link(call_tool)
    r = call_tool("ctx_doc", {"project": "doc", "kind": "context", "content": "z" * 5000})
    assert r["chars"] == 1000


# --- surfacing in ctx_get ------------------------------------------------------

def test_instructions_surfaced_at_top_of_ctx_get(call_tool):
    _link(call_tool)
    call_tool("ctx_doc", {"project": "doc", "kind": "instructions", "content": "follow OWASP first"})
    g = call_tool("ctx_get", {"project": "doc"})
    assert g["instructions"] == "follow OWASP first"
    # docs index present, listing the instructions doc
    assert any(d["kind"] == "instructions" for d in g["docs"])


def test_plan_indexed_but_full_only_in_detailed(call_tool):
    _link(call_tool)
    plan = "PLAN BODY " * 50
    call_tool("ctx_doc", {"project": "doc", "kind": "plan", "content": plan})

    default = call_tool("ctx_get", {"project": "doc"})
    # default: plan is in the index, but not its content
    plan_row = next(d for d in default["docs"] if d["kind"] == "plan")
    assert "content" not in plan_row

    detailed = call_tool("ctx_get", {"project": "doc", "view": "detailed"})
    plan_full = next(d for d in detailed["docs"] if d["kind"] == "plan")
    assert plan_full["content"] == plan


# --- durability ----------------------------------------------------------------

def test_reset_clears_docs(call_tool):
    # export/import roundtrip of docs is covered in test_export_import.py.
    _link(call_tool)
    call_tool("ctx_doc", {"project": "doc", "kind": "plan", "content": "p"})
    reset_project("doc")
    assert call_tool("ctx_doc", {"project": "doc", "action": "list"})["docs"] == []


# --- how_to_ctx ----------------------------------------------------------------

def test_how_to_ctx_returns_guide():
    result = asyncio.run(handle_call_tool("how_to_ctx", {}))
    text = result[0].text
    assert "ctx_get" in text and "ctx_update" in text and "ctx_doc" in text
    assert len(text) > 200
