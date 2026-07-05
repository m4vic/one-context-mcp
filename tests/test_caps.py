"""Size caps on buckets and the brief view of ctx_get."""

from ctx.database import trim_bucket


# --- trim_bucket unit tests --------------------------------------------------

def test_trim_noop_under_cap():
    assert trim_bucket("- a\n- b", 100) == "- a\n- b"

def test_trim_drops_oldest_lines_first():
    text = "\n".join(f"- line {i}" for i in range(100))
    out = trim_bucket(text, 200)
    assert len(out) <= 200
    assert "- line 99" in out      # newest kept
    assert "- line 0" not in out   # oldest dropped

def test_trim_single_giant_line_keeps_tail():
    text = "x" * 5000 + "TAIL"
    out = trim_bucket(text, 100)
    assert len(out) == 100
    assert out.endswith("TAIL")


# --- caps applied on writes ----------------------------------------------------

def test_done_bucket_capped_on_update(call_tool, monkeypatch):
    monkeypatch.setenv("CTX_MAX_BUCKET_CHARS", "500")
    call_tool("ctx_update", {"project": "cap", "session_summary": "first entry " + "a" * 300, "tool_name": "t"})
    call_tool("ctx_update", {"project": "cap", "session_summary": "second entry " + "b" * 300, "tool_name": "t"})
    g = call_tool("ctx_get", {"project": "cap"})
    assert len(g["done"]) <= 500
    assert "second entry" in g["done"]   # newest survives
    assert "first entry" not in g["done"]  # oldest trimmed

def test_map_capped_via_ctx_map(call_tool, monkeypatch):
    monkeypatch.setenv("CTX_MAX_MAP_CHARS", "300")
    files = [f"src/module_{i:03d}.py - component number {i}" for i in range(30)]
    r = call_tool("ctx_map", {"project": "cap", "files": files})
    assert len(r["map"]) <= 300

def test_invalid_cap_env_falls_back(call_tool, monkeypatch):
    monkeypatch.setenv("CTX_MAX_BUCKET_CHARS", "banana")
    r = call_tool("ctx_update", {"project": "cap", "session_summary": "works fine", "tool_name": "t"})
    assert "error" not in r


# --- brief view ------------------------------------------------------------------

def test_brief_view_truncates_done(call_tool):
    for i in range(60):
        call_tool("ctx_update", {"project": "brief", "session_summary": f"entry {i} " + "x" * 80, "tool_name": "t"})
    full = call_tool("ctx_get", {"project": "brief"})
    brief = call_tool("ctx_get", {"project": "brief", "view": "brief"})

    assert brief["done_truncated"] is True
    assert len(brief["done"]) <= 2000 < len(full["done"])
    assert "hint" in brief
    # Most recent entry is retained
    assert "entry 59" in brief["done"]
    # WHAT/NOW untouched by brief view
    assert brief["now"] == full["now"]

def test_brief_view_small_project_not_truncated(call_tool):
    call_tool("ctx_update", {"project": "tiny", "session_summary": "one thing", "tool_name": "t"})
    r = call_tool("ctx_get", {"project": "tiny", "view": "brief"})
    assert "done_truncated" not in r
    assert "one thing" in r["done"]
