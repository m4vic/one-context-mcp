"""Local Markdown mirror (<repo>/.ctx/context.md) + doc-truncation honesty."""

import os

from ctx.database import set_doc
from ctx.mirror import mirror_enabled, mirror_path, write_mirror


def _link_and_fill(call_tool, repo: str, project: str = "proj"):
    call_tool("ctx_update", {
        "project": project, "repo_path": repo, "tool_name": "t",
        "session_summary": "Currently working on rate limiting middleware.",
    })


# --- write_mirror ------------------------------------------------------------

def test_ctx_update_writes_grepable_mirror(call_tool, tmp_path):
    repo = str(tmp_path)
    r = call_tool("ctx_update", {
        "project": "proj", "repo_path": repo, "tool_name": "codex",
        "session_summary": "Currently working on rate limiting middleware.",
    })
    assert r.get("mirror"), "ctx_update did not report a mirror path"
    f = mirror_path(repo)
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    # Grep-able: buckets + the actual content + provenance tag are all present.
    assert "## NOW" in text
    assert "rate limiting middleware" in text
    assert "codex" in text


def test_write_mirror_direct_returns_path(call_tool, tmp_path):
    repo = str(tmp_path)
    _link_and_fill(call_tool, repo)
    path = write_mirror("proj")  # resolve repo_path from the row
    assert path == str(mirror_path(repo))
    assert mirror_path(repo).exists()


def test_mirror_disabled_writes_nothing(call_tool, tmp_path, monkeypatch):
    repo = str(tmp_path)
    monkeypatch.setenv("CTX_MIRROR", "0")
    assert mirror_enabled() is False
    _link_and_fill(call_tool, repo)
    assert write_mirror("proj") is None
    assert not mirror_path(repo).exists()


def test_mirror_skips_nonexistent_repo(call_tool, tmp_path):
    ghost = str(tmp_path / "does_not_exist")
    call_tool("ctx_update", {
        "project": "proj", "repo_path": ghost, "tool_name": "t",
        "session_summary": "hello",
    })
    # No repo root -> nothing written, and the phantom dir is NOT created.
    assert write_mirror("proj") is None
    assert not os.path.exists(ghost)


def test_mirror_leaves_no_temp_file(call_tool, tmp_path):
    repo = str(tmp_path)
    _link_and_fill(call_tool, repo)
    write_mirror("proj")
    leftovers = [p.name for p in (tmp_path / ".ctx").iterdir() if p.name != "context.md"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


# --- doc truncation honesty --------------------------------------------------

def test_set_doc_reports_truncation(call_tool, tmp_path, monkeypatch):
    call_tool("ctx_update", {"project": "proj", "repo_path": str(tmp_path), "tool_name": "t"})
    monkeypatch.setenv("CTX_MAX_DOC_CHARS", "1000")  # the floor
    big = "x" * 2000
    r = set_doc("proj", "plan", big)
    assert r["truncated"] is True
    assert r["original_chars"] == 2000
    assert r["chars"] == 1000


def test_set_doc_no_truncation_flag_when_within_cap(call_tool, tmp_path):
    call_tool("ctx_update", {"project": "proj", "repo_path": str(tmp_path), "tool_name": "t"})
    r = set_doc("proj", "plan", "short plan")
    assert "truncated" not in r
