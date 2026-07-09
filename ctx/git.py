"""Git integration for ctx - Combined Context.

Detects branch, recent commits, and changed files for a project's repo.
All functions return None gracefully if git is unavailable or the path
is not a git repository. Zero dependencies - uses subprocess only.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# On Windows, spawn git without allocating a console window. A windowless GUI
# host (Antigravity, Claude Desktop) spawning python -> git.exe can otherwise
# stall on console allocation.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _run_git(repo_path: str, *args: str, timeout: int = 5) -> Optional[str]:
    """Run a git command in the given repo, return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Never inherit the parent's stdin: for an MCP stdio server, stdin
            # is the JSON-RPC pipe, and a spawned git inheriting it can deadlock
            # the transport. Detach it explicitly.
            stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def is_git_repo(repo_path: str) -> bool:
    """Check if the given path is inside a git repository."""
    return _run_git(repo_path, "rev-parse", "--git-dir") is not None


def get_branch(repo_path: str) -> Optional[str]:
    """Get the current branch name (e.g. 'main', 'feature-auth')."""
    return _run_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")


def get_recent_commits(repo_path: str, n: int = 5) -> Optional[list[dict]]:
    """Get the last N commit messages with hash and author.

    Returns a list of dicts: [{"hash": "abc123", "author": "name", "message": "..."}]
    """
    output = _run_git(
        repo_path, "log", f"-{n}",
        "--format=%h|%an|%s",
        "--no-merges",
    )
    if output is None:
        return None

    commits = []
    for line in output.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({
                "hash": parts[0],
                "author": parts[1],
                "message": parts[2],
            })
    return commits


def get_changed_files(repo_path: str) -> Optional[dict]:
    """Get uncommitted changes (staged + unstaged + untracked).

    Returns: {"staged": [...], "unstaged": [...], "untracked": [...]}
    """
    staged = _run_git(repo_path, "diff", "--cached", "--name-only")
    unstaged = _run_git(repo_path, "diff", "--name-only")
    untracked = _run_git(repo_path, "ls-files", "--others", "--exclude-standard")

    if staged is None and unstaged is None and untracked is None:
        return None

    return {
        "staged": [f for f in (staged or "").splitlines() if f],
        "unstaged": [f for f in (unstaged or "").splitlines() if f],
        "untracked": [f for f in (untracked or "").splitlines() if f],
    }


def get_git_summary(repo_path: str) -> Optional[dict]:
    """Get a complete git status summary for a repo.

    Returns None if the path is not a git repo.
    Otherwise returns: {
        "branch": "main",
        "recent_commits": [...],
        "changed_files": {...},
    }
    """
    # Escape hatch: let users turn git inspection off entirely.
    if os.environ.get("CTX_DISABLE_GIT"):
        return None
    if not repo_path or not Path(repo_path).exists():
        return None

    if not is_git_repo(repo_path):
        return None

    return {
        "branch": get_branch(repo_path) or "unknown",
        "recent_commits": get_recent_commits(repo_path, n=5) or [],
        "changed_files": get_changed_files(repo_path) or {
            "staged": [], "unstaged": [], "untracked": []
        },
    }
