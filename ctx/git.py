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


def _parse_status(output: str) -> tuple[str, dict]:
    """Parse `git status --porcelain=v1 --branch` output.

    Returns (branch, {"staged": [...], "unstaged": [...], "untracked": [...]}).
    """
    branch = "unknown"
    staged, unstaged, untracked = [], [], []
    for line in output.splitlines():
        if line.startswith("## "):
            head = line[3:]
            # "## No commits yet on main" | "## HEAD (no branch)" | "## main...origin/main [ahead 1]"
            if head.startswith("No commits yet on "):
                branch = head[len("No commits yet on "):].strip()
            elif head.startswith("HEAD "):
                branch = "HEAD"
            else:
                branch = head.split("...", 1)[0].split(" ", 1)[0].strip() or "unknown"
            continue
        if len(line) < 3:
            continue
        x, y, path = line[0], line[1], line[3:]
        if x == "?" and y == "?":
            untracked.append(path)
            continue
        if x not in (" ", "?"):
            staged.append(path)
        if y not in (" ", "?"):
            unstaged.append(path)
    return branch, {"staged": staged, "unstaged": unstaged, "untracked": untracked}


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


def get_git_summary(repo_path: str) -> Optional[dict]:
    """Get a complete git status summary for a repo, in just 2 subprocess calls.

    Returns None if the path is not a git repo (or git is unavailable).
    Otherwise returns: {
        "branch": "main",
        "recent_commits": [...],
        "changed_files": {"staged": [...], "unstaged": [...], "untracked": [...]},
    }
    """
    # Escape hatch: let users turn git inspection off entirely.
    if os.environ.get("CTX_DISABLE_GIT"):
        return None
    if not repo_path or not Path(repo_path).exists():
        return None

    # One call gives branch + all changes AND doubles as the "is this a repo?"
    # check (returns None on non-zero exit, e.g. not a git repo).
    status = _run_git(repo_path, "status", "--porcelain=v1", "--branch")
    if status is None:
        return None

    branch, changed_files = _parse_status(status)
    return {
        "branch": branch,
        "recent_commits": get_recent_commits(repo_path, n=5) or [],
        "changed_files": changed_files,
    }
