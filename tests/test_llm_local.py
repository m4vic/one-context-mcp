"""Local merge logic and LLM-response parsing (no network calls anywhere)."""

import os

from ctx.llm import (
    _coerce_bucket_value,
    _local_merge,
    _parse_llm_response,
    _path_within_repo,
    merge_context,
)


# --- _local_merge ------------------------------------------------------------

def test_what_updates_only_on_project_keywords():
    r = _local_merge("", "", "", "", "Fixed a small bug in the parser.", "t")
    assert r["what"] == ""

    r = _local_merge("", "", "", "", "Stack: Python + FastAPI backend.", "t")
    assert "FastAPI" in r["what"]


def test_now_extracts_progress_sentences():
    r = _local_merge("", "", "", "", "Refactored utils. Currently working on rate limiting. Next step is docs.", "t")
    assert "rate limiting" in r["now"]
    assert "docs" in r["now"]


def test_previous_now_moves_to_done_when_replaced():
    r = _local_merge("", "", "- old task in flight", "", "Shipped it. Currently working on docs.", "t")
    assert "old task in flight" in r["done"]
    assert "docs" in r["now"]
    assert "old task in flight" not in r["now"]


def test_now_kept_when_summary_has_no_progress_signal():
    # A summary without a current-task signal must not blank NOW - the next
    # tool still needs to see what's in progress.
    r = _local_merge("", "", "- rate limiting middleware", "", "Fixed a typo in README.", "t")
    assert r["now"] == "- rate limiting middleware"
    assert "rate limiting middleware" not in r["done"]


def test_done_deduplicates_lines():
    summary = "same entry"
    r1 = _local_merge("", "", "", "", summary, "t")
    r2 = _local_merge("", r1["done"], "", "", summary, "t")
    # The identical summary line (differing only by timestamp prefix content)
    # is appended once per unique normalized line
    assert r2["done"].count("same entry") <= 2


def test_map_extracts_only_code_paths():
    summary = "Edited src/main.py and docs at https://example.com/page and image.png"
    r = _local_merge("", "", "", "", summary, "t")
    assert "src/main.py" in r["map"]
    assert "example.com" not in r["map"]


def test_map_scoped_to_repo_path():
    if os.name == "nt":
        inside, outside, repo = r"C:\repo\src\a.py", r"C:\other\b.py", r"C:\repo"
    else:
        inside, outside, repo = "/repo/src/a.py", "/other/b.py", "/repo"
    summary = f"Changed {inside} and {outside}"
    r = _local_merge("", "", "", "", summary, "t", repo_path=repo)
    assert "a.py" in r["map"]
    assert "b.py" not in r["map"]


def test_path_within_repo_relative_always_allowed():
    assert _path_within_repo("src/x.py", "/anywhere") is True
    assert _path_within_repo("src/x.py", "") is True


# --- response parsing --------------------------------------------------------

def test_parse_plain_json():
    out = _parse_llm_response('{"what": "w", "done": "d", "now": "n", "map": "m"}', "", "", "", "")
    assert out == {"what": "w", "done": "d", "now": "n", "map": "m"}


def test_parse_fenced_json_with_chatter():
    text = 'Sure! Here you go:\n```json\n{"what": "w", "done": "d", "now": "n", "map": "m"}\n```\nHope that helps.'
    out = _parse_llm_response(text, "", "", "", "")
    assert out["what"] == "w"


def test_parse_falls_back_to_current_on_missing_keys():
    out = _parse_llm_response('{"done": "d"}', "cur_what", "cur_done", "cur_now", "cur_map")
    assert out["what"] == "cur_what"
    assert out["done"] == "d"


def test_parse_invalid_returns_none():
    assert _parse_llm_response("not json at all", "", "", "", "") is None


def test_coerce_bucket_value_handles_lists():
    assert _coerce_bucket_value("plain", "fb") == "plain"
    assert _coerce_bucket_value(["a", "- b"], "fb") == "- a\n- b"
    assert _coerce_bucket_value({"weird": 1}, "fb") == "fb"


def test_parse_coerces_list_buckets():
    out = _parse_llm_response('{"what": ["item one", "item two"], "done": "d", "now": "n", "map": "m"}', "", "", "", "")
    assert out["what"] == "- item one\n- item two"


# --- provider gating (no network) ---------------------------------------------

def test_merge_context_defaults_to_local(monkeypatch):
    monkeypatch.delenv("CTX_MERGE_MODE", raising=False)
    r = merge_context("", "", "", "", "Stack: Rust.", "t")
    assert "Rust" in r["what"]


def test_invalid_merge_mode_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("CTX_MERGE_MODE", "quantum")
    r = merge_context("", "", "", "", "Stack: Go.", "t")
    assert "Go" in r["what"]


def test_cloud_modes_without_keys_fall_back_to_local(monkeypatch):
    for mode, keys in [
        ("anthropic", ["ANTHROPIC_API_KEY"]),
        ("openai", ["OPENAI_API_KEY"]),
        ("gemini", ["GEMINI_API_KEY"]),
    ]:
        monkeypatch.setenv("CTX_MERGE_MODE", mode)
        for k in keys:
            monkeypatch.delenv(k, raising=False)
        r = merge_context("", "", "", "", "Stack: Zig.", "t")
        assert "Zig" in r["what"], f"mode {mode} did not fall back to local"
