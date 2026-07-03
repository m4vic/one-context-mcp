"""SQLite storage layer for ctx.

Stores project context in four buckets: WHAT, DONE, NOW, MAP.
Everything is local — one .db file per installation.
"""

import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Default DB location: ~/.ctx/ctx.db
DEFAULT_DB_DIR = Path.home() / ".ctx"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "ctx.db"


def _get_db_path() -> Path:
    """Get the database path, respecting CTX_DB_PATH env var."""
    custom = os.environ.get("CTX_DB_PATH")
    if custom:
        return Path(custom)
    return DEFAULT_DB_PATH


def get_connection() -> sqlite3.Connection:
    """Get a SQLite connection, creating the DB and tables if needed."""
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            name        TEXT PRIMARY KEY,
            what        TEXT NOT NULL DEFAULT '',
            done        TEXT NOT NULL DEFAULT '',
            now         TEXT NOT NULL DEFAULT '',
            map         TEXT NOT NULL DEFAULT '',
            repo_path   TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS update_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project     TEXT NOT NULL,
            tool_name   TEXT NOT NULL DEFAULT 'unknown',
            summary     TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            FOREIGN KEY (project) REFERENCES projects(name) ON DELETE CASCADE
        )
    """)

    # --- Migration: add columns for existing databases ---
    try:
        conn.execute("SELECT map FROM projects LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE projects ADD COLUMN map TEXT NOT NULL DEFAULT ''")

    try:
        conn.execute("SELECT repo_path FROM projects LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE projects ADD COLUMN repo_path TEXT NOT NULL DEFAULT ''")

    conn.commit()
    return conn


def init_project(name: str, repo_path: str = "") -> dict:
    """Create a new project entry. Returns the project dict."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO projects (name, repo_path, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, repo_path, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # If project exists and repo_path is being set, update it
        if repo_path:
            conn.execute(
                "UPDATE projects SET repo_path = ?, updated_at = ? WHERE name = ?",
                (repo_path, now, name),
            )
            conn.commit()
        else:
            conn.close()
            return {"error": f"Project '{name}' already exists."}
    result = get_project(name, conn)
    conn.close()
    return result


def get_project(name: str, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Get the full context snapshot for a project."""
    should_close = conn is None
    if conn is None:
        conn = get_connection()

    row = conn.execute(
        "SELECT * FROM projects WHERE name = ?", (name,)
    ).fetchone()

    if should_close:
        conn.close()

    if row is None:
        return {"error": f"Project '{name}' not found."}

    return {
        "project": row["name"],
        "what": row["what"],
        "done": row["done"],
        "now": row["now"],
        "map": row["map"],
        "repo_path": row["repo_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def update_project(
    name: str, what: str, done: str, now: str, map_: str,
    tool_name: str, summary: str, repo_path: Optional[str] = None,
) -> dict:
    """Update a project's context buckets after merge."""
    conn = get_connection()
    ts = datetime.now(timezone.utc).isoformat()

    if repo_path is not None:
        cursor = conn.execute(
            """UPDATE projects
               SET what = ?, done = ?, now = ?, map = ?, repo_path = ?, updated_at = ?
               WHERE name = ?""",
            (what, done, now, map_, repo_path, ts, name),
        )
    else:
        cursor = conn.execute(
            """UPDATE projects
               SET what = ?, done = ?, now = ?, map = ?, updated_at = ?
               WHERE name = ?""",
            (what, done, now, map_, ts, name),
        )

    if cursor.rowcount == 0:
        conn.close()
        return {"error": f"Project '{name}' not found. Run `ctx init {name}` first."}

    # Log the raw update for audit
    conn.execute(
        "INSERT INTO update_log (project, tool_name, summary, timestamp) VALUES (?, ?, ?, ?)",
        (name, tool_name, summary, ts),
    )

    conn.commit()
    result = get_project(name, conn)
    conn.close()
    return result


def update_project_map(name: str, map_content: str) -> dict:
    """Update only the MAP bucket for a project (for ctx_map tool)."""
    conn = get_connection()
    ts = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute(
        "UPDATE projects SET map = ?, updated_at = ? WHERE name = ?",
        (map_content, ts, name),
    )

    if cursor.rowcount == 0:
        conn.close()
        return {"error": f"Project '{name}' not found."}

    conn.commit()
    result = get_project(name, conn)
    conn.close()
    return result


def reset_project(name: str) -> dict:
    """Wipe a project's context back to empty."""
    conn = get_connection()
    ts = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute(
        "UPDATE projects SET what = '', done = '', now = '', map = '', updated_at = ? WHERE name = ?",
        (ts, name),
    )

    if cursor.rowcount == 0:
        conn.close()
        return {"error": f"Project '{name}' not found."}

    # Also clear the log
    conn.execute("DELETE FROM update_log WHERE project = ?", (name,))
    conn.commit()
    conn.close()
    return {"status": "reset", "project": name}


def list_projects() -> list[dict]:
    """List all known projects with basic info."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT name, updated_at FROM projects ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [{"project": r["name"], "updated_at": r["updated_at"]} for r in rows]


def delete_project(name: str) -> dict:
    """Permanently delete a project and its logs."""
    conn = get_connection()
    cursor = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
    if cursor.rowcount == 0:
        conn.close()
        return {"error": f"Project '{name}' not found."}
    conn.commit()
    conn.close()
    return {"status": "deleted", "project": name}


# ---------------------------------------------------------------------------
# Cross-project search
# ---------------------------------------------------------------------------

def search_projects(query: str) -> list[dict]:
    """Search across all projects' context buckets (what/done/now/map)."""
    conn = get_connection()
    pattern = f"%{query}%"
    rows = conn.execute(
        """SELECT name, what, done, now, map, updated_at FROM projects
           WHERE what LIKE ? OR done LIKE ? OR now LIKE ? OR map LIKE ?
           ORDER BY updated_at DESC""",
        (pattern, pattern, pattern, pattern),
    ).fetchall()
    conn.close()

    results = []
    for r in rows:
        # Build a list of which buckets matched
        matches = []
        q_lower = query.lower()
        if q_lower in r["what"].lower():
            matches.append("what")
        if q_lower in r["done"].lower():
            matches.append("done")
        if q_lower in r["now"].lower():
            matches.append("now")
        if q_lower in r["map"].lower():
            matches.append("map")

        results.append({
            "project": r["name"],
            "matched_buckets": matches,
            "updated_at": r["updated_at"],
        })

    return results


def search_logs(query: str) -> list[dict]:
    """Search across all projects' update history."""
    conn = get_connection()
    pattern = f"%{query}%"
    rows = conn.execute(
        """SELECT project, tool_name, summary, timestamp FROM update_log
           WHERE summary LIKE ?
           ORDER BY timestamp DESC
           LIMIT 20""",
        (pattern,),
    ).fetchall()
    conn.close()

    return [
        {
            "project": r["project"],
            "tool_name": r["tool_name"],
            "summary": r["summary"],
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]
