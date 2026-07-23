"""Cross-tool merge provenance: per-entry source tags + NOW conflict surfacing.

This is the 0.7.0 headline - the behavior no other memory MCP has. Tests cover
the pure merge (ctx.llm._local_merge) and the conflict surfaced by ctx_get.
"""

import re

from ctx.llm import _local_merge, _parse_entry, now_source_tools

TAG_RE = re.compile(r"^-\s*\[[^\]]+\]")


# --- tag parsing -------------------------------------------------------------

def test_parse_entry_tagged_and_untagged():
    assert _parse_entry("- [codex @ 2026-07-23 14:20] did X") == ("codex", "did X")
    assert _parse_entry("- [claude] working on Y") == ("claude", "working on Y")
    # Untagged/legacy line: no source, text preserved (incl. bullet).
    assert _parse_entry("- some legacy task") == (None, "- some legacy task")


def test_now_source_tools_distinct_first_seen():
    now = "- [claude @ t] a\n- [codex @ t] b\n- [claude @ t] c\n- untagged"
    assert now_source_tools(now) == ["claude", "codex"]
    assert now_source_tools("") == []


# --- NOW gets per-entry provenance -------------------------------------------

def test_now_entries_are_tagged_with_the_writing_tool():
    r = _local_merge("", "", "", "", "Currently working on auth.", "codex")
    assert r["now"], "expected a NOW entry"
    for line in r["now"].splitlines():
        assert TAG_RE.match(line), f"NOW entry not provenance-tagged: {line!r}"
    assert "codex" in r["now"]


# --- multi-tool NOW model ----------------------------------------------------

def test_other_tools_now_is_kept_not_clobbered():
    # claude has an active NOW; codex adds its own -> both survive.
    claude_now = "- [claude @ 2026-07-23 10:00] working on auth"
    r = _local_merge("", "", claude_now, "", "Currently working on billing.", "codex")
    assert "auth" in r["now"], "claude's active NOW was clobbered"
    assert "billing" in r["now"], "codex's new NOW missing"
    assert set(now_source_tools(r["now"])) == {"claude", "codex"}


def test_same_tool_rotates_its_own_now_into_done():
    own_now = "- [codex @ 2026-07-23 10:00] working on auth"
    r = _local_merge("", "", own_now, "", "Currently working on billing.", "codex")
    # The tool moved on: its previous task is history, not still-current.
    assert "auth" in r["done"]
    assert "auth" not in r["now"]
    assert "billing" in r["now"]
    assert now_source_tools(r["now"]) == ["codex"]  # no conflict, single tool


def test_no_signal_update_leaves_now_untouched():
    now = "- [claude @ t] working on auth"
    r = _local_merge("", "", now, "", "Fixed a typo in the README.", "codex")
    assert r["now"] == now  # unchanged, still claude's


def test_legacy_untagged_now_rotates_on_first_tagged_write():
    r = _local_merge("", "", "- old ambiguous task", "", "Currently working on auth.", "codex")
    assert "old ambiguous task" in r["done"]
    assert "old ambiguous task" not in r["now"]
    assert now_source_tools(r["now"]) == ["codex"]


# --- conflict surfaced by ctx_get --------------------------------------------

def test_ctx_get_flags_now_conflict_across_tools(call_tool):
    call_tool("ctx_update", {"project": "multi", "session_summary": "Currently working on auth.", "tool_name": "claude"})
    call_tool("ctx_update", {"project": "multi", "session_summary": "Currently working on billing.", "tool_name": "codex"})
    g = call_tool("ctx_get", {"project": "multi"})
    assert "now_conflict" in g, g.get("now")
    assert set(g["now_conflict"]["tools"]) == {"claude", "codex"}


def test_ctx_get_no_conflict_for_single_tool(call_tool):
    call_tool("ctx_update", {"project": "solo", "session_summary": "Currently working on auth.", "tool_name": "claude"})
    call_tool("ctx_update", {"project": "solo", "session_summary": "Currently working on billing.", "tool_name": "claude"})
    g = call_tool("ctx_get", {"project": "solo"})
    assert "now_conflict" not in g
