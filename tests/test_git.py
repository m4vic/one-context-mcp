"""Unit tests for the git porcelain-status parser (2-call git summary)."""

from ctx.git import _parse_status


def test_parse_clean_repo():
    branch, changed = _parse_status("## main...origin/main")
    assert branch == "main"
    assert changed == {"staged": [], "unstaged": [], "untracked": []}


def test_parse_branch_without_upstream():
    branch, _ = _parse_status("## feature-auth")
    assert branch == "feature-auth"


def test_parse_no_commits_yet():
    branch, _ = _parse_status("## No commits yet on main")
    assert branch == "main"


def test_parse_detached_head():
    branch, _ = _parse_status("## HEAD (no branch)")
    assert branch == "HEAD"


def test_parse_ahead_behind_stripped():
    branch, _ = _parse_status("## main...origin/main [ahead 2, behind 1]")
    assert branch == "main"


def test_parse_staged_unstaged_untracked():
    out = "\n".join([
        "## main",
        "M  staged_only.py",     # staged (index modified)
        " M unstaged_only.py",   # unstaged (worktree modified)
        "MM both.py",            # staged + unstaged
        "?? new_file.py",        # untracked
        "A  added.py",           # staged add
    ])
    branch, changed = _parse_status(out)
    assert branch == "main"
    assert "staged_only.py" in changed["staged"]
    assert "added.py" in changed["staged"]
    assert "both.py" in changed["staged"] and "both.py" in changed["unstaged"]
    assert "unstaged_only.py" in changed["unstaged"]
    assert changed["untracked"] == ["new_file.py"]
