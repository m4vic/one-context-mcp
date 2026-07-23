"""SQLite storage layer for ctx.

Stores project context in four buckets: WHAT, DONE, NOW, MAP.
Everything is local - one .db file per installation.
"""

import sqlite3
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Default DB location: ~/.ctx/ctx.db
DEFAULT_DB_DIR = Path.home() / ".ctx"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "ctx.db"

_MAP_BULLET_RE = re.compile(r"^\s*[-*]\s*")
_MAP_SPLIT_RE = re.compile(r"\s+(?:--|-)\s+")


def _normalize_path_key(path: str) -> str:
    """Return a stable key for deduping file paths."""
    candidate = path.strip().strip('"').strip("'")
    if not candidate:
        return ""
    return candidate.replace("/", "\\").rstrip("\\").lower()


def _parse_map_line(line: str) -> tuple[str, str]:
    """Parse a MAP line into (path, note)."""
    clean = _MAP_BULLET_RE.sub("", line.strip())
    if not clean:
        return "", ""

    parts = _MAP_SPLIT_RE.split(clean, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()

    if "\u2014" in clean:
        left, right = clean.split("\u2014", 1)
        return left.strip(), right.strip()

    return clean.strip(), ""


def normalize_map_content(map_content: str) -> str:
    """Canonicalize MAP content while preserving first-seen order."""
    ordered: dict[str, tuple[str, str]] = {}
    for line in map_content.splitlines():
        path_part, note = _parse_map_line(line)
        key = _normalize_path_key(path_part)
        if not key:
            continue
        if key not in ordered:
            ordered[key] = (path_part, note)
        elif note and not ordered[key][1]:
            ordered[key] = (ordered[key][0], note)

    lines = []
    for path_part, note in ordered.values():
        if note:
            lines.append(f"- {path_part} - {note}")
        else:
            lines.append(f"- {path_part}")
    return "\n".join(lines)


def merge_map_content(existing_map: str, new_map: str) -> str:
    """Merge MAP content with stable ordering and deduplication."""
    if not existing_map.strip():
        return normalize_map_content(new_map)
    if not new_map.strip():
        return normalize_map_content(existing_map)
    return normalize_map_content(existing_map + "\n" + new_map)


def _bucket_cap(env_var: str, default: int) -> int:
    """Read a size cap from the environment, falling back on bad values."""
    raw = os.environ.get(env_var, "")
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def trim_bucket(text: str, max_chars: int) -> str:
    """Trim oldest whole lines until the bucket fits max_chars.

    Character-based (line counts don't bound size - one long line defeats
    them). Keeps the newest lines, which sit at the bottom of every bucket.
    """
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    while lines and len("\n".join(lines)) > max_chars:
        lines.pop(0)
    if lines:
        return "\n".join(lines)
    # Single line larger than the cap: keep its tail
    return text[-max_chars:]


def _apply_caps(what: str, done: str, now: str, map_: str) -> tuple[str, str, str, str]:
    bucket_cap = _bucket_cap("CTX_MAX_BUCKET_CHARS", 12000)
    map_cap = _bucket_cap("CTX_MAX_MAP_CHARS", 4000)
    return (
        trim_bucket(what, bucket_cap),
        trim_bucket(done, bucket_cap),
        trim_bucket(now, bucket_cap),
        trim_bucket(map_, map_cap),
    )


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
    # Wait (instead of erroring) if another tool holds the write lock. This,
    # together with BEGIN IMMEDIATE in atomic_merge_update, is what makes
    # concurrent updates from multiple AI tools safe against lost writes.
    conn.execute("PRAGMA busy_timeout=5000")

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project     TEXT NOT NULL,
            author      TEXT NOT NULL DEFAULT 'user',
            message     TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            FOREIGN KEY (project) REFERENCES projects(name) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bugs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project     TEXT NOT NULL,
            description TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            FOREIGN KEY (project) REFERENCES projects(name) ON DELETE CASCADE
        )
    """)

    # Verbatim, user-authored documents per project (plan, instructions,
    # context, ...). Unlike the WHAT/DONE/NOW/MAP buckets these are NOT merged
    # or keyword-routed - they are stored and returned exactly as written.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_docs (
            project     TEXT NOT NULL,
            kind        TEXT NOT NULL,
            content     TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL,
            updated_by  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (project, kind),
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
    map_ = normalize_map_content(map_)
    what, done, now, map_ = _apply_caps(what, done, now, map_)
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


def atomic_merge_update(
    name: str,
    merge_fn,
    tool_name: str,
    summary: str,
    repo_path: Optional[str] = None,
) -> dict:
    """Atomically read a project's context, merge, and write it back.

    `merge_fn` receives the current context dict (what/done/now/map/repo_path)
    and returns a dict with the merged what/done/now/map. The whole read →
    merge → write runs inside a single BEGIN IMMEDIATE transaction so two AI
    tools calling ctx_update on the same project can't clobber each other
    (no lost updates).

    Note: in local merge mode `merge_fn` is instant, so holding the write
    lock across it is fine. LLM merge modes do network I/O inside the lock;
    that's acceptable because local is the default and LLM modes are opt-in.
    """
    conn = get_connection()
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            conn.close()
            return {"error": f"Project '{name}' not found."}

        current = {
            "what": row["what"],
            "done": row["done"],
            "now": row["now"],
            "map": row["map"],
            "repo_path": row["repo_path"],
        }
        merged = merge_fn(current)
        map_ = normalize_map_content(merged.get("map", current["map"]))
        what, done, now, map_ = _apply_caps(merged["what"], merged["done"], merged["now"], map_)
        ts = datetime.now(timezone.utc).isoformat()

        if repo_path is not None:
            conn.execute(
                """UPDATE projects
                   SET what = ?, done = ?, now = ?, map = ?, repo_path = ?, updated_at = ?
                   WHERE name = ?""",
                (what, done, now, map_, repo_path, ts, name),
            )
        else:
            conn.execute(
                """UPDATE projects
                   SET what = ?, done = ?, now = ?, map = ?, updated_at = ?
                   WHERE name = ?""",
                (what, done, now, map_, ts, name),
            )

        conn.execute(
            "INSERT INTO update_log (project, tool_name, summary, timestamp) VALUES (?, ?, ?, ?)",
            (name, tool_name, summary, ts),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise

    result = get_project(name, conn)
    conn.close()
    return result


def update_project_map(name: str, map_content: str) -> dict:
    """Replace the MAP bucket for a project (normalized + capped)."""
    conn = get_connection()
    map_content = normalize_map_content(map_content)
    map_content = trim_bucket(map_content, _bucket_cap("CTX_MAX_MAP_CHARS", 4000))
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


def merge_project_map(name: str, new_entries: str) -> dict:
    """Atomically merge entries into a project's MAP bucket.

    The read-merge-write runs inside a single BEGIN IMMEDIATE transaction
    (like atomic_merge_update) so two tools registering files concurrently
    cannot lose each other's entries.
    """
    conn = get_connection()
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT map FROM projects WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            conn.close()
            return {"error": f"Project '{name}' not found."}
        merged = merge_map_content(row["map"], new_entries)
        merged = trim_bucket(merged, _bucket_cap("CTX_MAX_MAP_CHARS", 4000))
        ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE projects SET map = ?, updated_at = ? WHERE name = ?",
            (merged, ts, name),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    result = get_project(name, conn)
    conn.close()
    return result


def add_project_message(name: str, message: str, author: str = "user") -> dict:
    """Store a user-authored project note/message."""
    conn = get_connection()
    ts = datetime.now(timezone.utc).isoformat()

    row = conn.execute("SELECT name FROM projects WHERE name = ?", (name,)).fetchone()
    if row is None:
        conn.close()
        return {"error": f"Project '{name}' not found."}

    cursor = conn.execute(
        """INSERT INTO project_messages (project, author, message, timestamp)
           VALUES (?, ?, ?, ?)""",
        (name, author or "user", message, ts),
    )
    conn.commit()

    result = {
        "id": cursor.lastrowid,
        "project": name,
        "author": author or "user",
        "message": message,
        "timestamp": ts,
    }
    conn.close()
    return result


_VALID_BUG_STATUSES = {"open", "fixed"}


def _bug_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "project": row["project"],
        "description": row["description"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def add_bug(name: str, description: str) -> dict:
    """Record a new open bug for a project."""
    description = (description or "").strip()
    if not description:
        return {"error": "Bug description must not be empty."}

    conn = get_connection()
    row = conn.execute("SELECT name FROM projects WHERE name = ?", (name,)).fetchone()
    if row is None:
        conn.close()
        return {"error": f"Project '{name}' not found."}

    ts = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO bugs (project, description, status, created_at, updated_at)
           VALUES (?, ?, 'open', ?, ?)""",
        (name, description, ts, ts),
    )
    conn.commit()
    result = {
        "id": cursor.lastrowid,
        "project": name,
        "description": description,
        "status": "open",
        "created_at": ts,
        "updated_at": ts,
    }
    conn.close()
    return result


def set_bug_status(name: str, bug_id: int, status: str) -> dict:
    """Update a bug's status (open/fixed)."""
    if status not in _VALID_BUG_STATUSES:
        return {"error": f"status must be one of {sorted(_VALID_BUG_STATUSES)}."}

    conn = get_connection()
    ts = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "UPDATE bugs SET status = ?, updated_at = ? WHERE id = ? AND project = ?",
        (status, ts, bug_id, name),
    )
    if cursor.rowcount == 0:
        conn.close()
        return {"error": f"Bug #{bug_id} not found for project '{name}'."}
    conn.commit()
    row = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_id,)).fetchone()
    conn.close()
    return _bug_row_to_dict(row)


def list_bugs(name: str, status: Optional[str] = None) -> list[dict]:
    """List a project's bugs, optionally filtered by status. Open bugs first."""
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM bugs WHERE project = ? AND status = ? ORDER BY id",
            (name, status),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM bugs WHERE project = ?
               ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id""",
            (name,),
        ).fetchall()
    conn.close()
    return [_bug_row_to_dict(r) for r in rows]


def count_bugs(name: str, status: str) -> int:
    """Count a project's bugs with a given status."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM bugs WHERE project = ? AND status = ?",
        (name, status),
    ).fetchone()
    conn.close()
    return row["n"] if row else 0


def list_project_history(name: str, limit: int = 20) -> dict:
    """Return recent update summaries and user messages for one project."""
    conn = get_connection()
    project = get_project(name, conn)
    if "error" in project:
        conn.close()
        return project

    safe_limit = max(1, min(int(limit or 20), 100))
    updates = conn.execute(
        """SELECT tool_name, summary, timestamp FROM update_log
           WHERE project = ?
           ORDER BY timestamp DESC
           LIMIT ?""",
        (name, safe_limit),
    ).fetchall()
    messages = conn.execute(
        """SELECT id, author, message, timestamp FROM project_messages
           WHERE project = ?
           ORDER BY timestamp DESC
           LIMIT ?""",
        (name, safe_limit),
    ).fetchall()
    conn.close()

    return {
        "project": name,
        "updates": [
            {
                "tool_name": r["tool_name"],
                "summary": r["summary"],
                "timestamp": r["timestamp"],
            }
            for r in updates
        ],
        "messages": [
            {
                "id": r["id"],
                "author": r["author"],
                "message": r["message"],
                "timestamp": r["timestamp"],
            }
            for r in messages
        ],
    }


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

    # Also clear the log, notes, bugs, and docs
    conn.execute("DELETE FROM update_log WHERE project = ?", (name,))
    conn.execute("DELETE FROM project_messages WHERE project = ?", (name,))
    conn.execute("DELETE FROM bugs WHERE project = ?", (name,))
    conn.execute("DELETE FROM project_docs WHERE project = ?", (name,))
    conn.commit()
    conn.close()
    return {"status": "reset", "project": name}


# --- Per-project documents (verbatim plan / instructions / context) ----------

def _doc_cap() -> int:
    """Max chars stored per document. Larger than buckets - docs are intentional."""
    try:
        return max(1000, int(os.environ.get("CTX_MAX_DOC_CHARS", "50000")))
    except ValueError:
        return 50000


def set_doc(project: str, kind: str, content: str, updated_by: str = "") -> dict:
    """Store a verbatim document for a project. Overwrites the same kind."""
    kind = (kind or "").strip().lower()
    if not kind:
        return {"error": "doc 'kind' is required (e.g. plan, instructions, context)."}
    conn = get_connection()
    row = conn.execute("SELECT name FROM projects WHERE name = ?", (project,)).fetchone()
    if row is None:
        conn.close()
        return {"error": f"Project '{project}' not found. Create it first with ctx_update(project=..., repo_path=...)."}
    raw = content or ""
    original_chars = len(raw)
    content = trim_bucket(raw, _doc_cap())
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO project_docs (project, kind, content, updated_at, updated_by)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(project, kind) DO UPDATE SET
             content = excluded.content,
             updated_at = excluded.updated_at,
             updated_by = excluded.updated_by""",
        (project, kind, content, ts, updated_by or ""),
    )
    conn.commit()
    conn.close()
    result = {"status": "saved", "project": project, "kind": kind, "chars": len(content)}
    if len(content) < original_chars:
        # "Verbatim" must never trim silently - tell the caller it was cut.
        result["truncated"] = True
        result["original_chars"] = original_chars
        result["hint"] = f"Content exceeded CTX_MAX_DOC_CHARS ({_doc_cap()}); stored the last {len(content)} chars."
    return result


def get_doc(project: str, kind: str) -> dict:
    """Return one verbatim document."""
    kind = (kind or "").strip().lower()
    conn = get_connection()
    row = conn.execute(
        "SELECT content, updated_at, updated_by FROM project_docs WHERE project = ? AND kind = ?",
        (project, kind),
    ).fetchone()
    conn.close()
    if row is None:
        return {"error": f"No '{kind}' doc for project '{project}'."}
    return {
        "project": project,
        "kind": kind,
        "content": row["content"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def list_docs(project: str) -> list[dict]:
    """Return an index (kind + size + timestamp) of a project's docs, no content."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT kind, length(content) AS chars, updated_at, updated_by
           FROM project_docs WHERE project = ? ORDER BY kind""",
        (project,),
    ).fetchall()
    conn.close()
    return [
        {"kind": r["kind"], "chars": r["chars"], "updated_at": r["updated_at"], "updated_by": r["updated_by"]}
        for r in rows
    ]


def delete_doc(project: str, kind: str) -> dict:
    """Delete one document."""
    kind = (kind or "").strip().lower()
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM project_docs WHERE project = ? AND kind = ?", (project, kind)
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return {"error": f"No '{kind}' doc for project '{project}'."}
    return {"status": "deleted", "project": project, "kind": kind}


def get_docs_map(project: str, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Return {kind: content} for all of a project's docs (used by export/get)."""
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT kind, content FROM project_docs WHERE project = ?", (project,)
    ).fetchall()
    if should_close:
        conn.close()
    return {r["kind"]: r["content"] for r in rows}


def list_projects() -> list[dict]:
    """List all known projects with basic info."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT name, repo_path, updated_at FROM projects ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [
        {"project": r["name"], "repo_path": r["repo_path"], "updated_at": r["updated_at"]}
        for r in rows
    ]


def _canonical_path(path: str) -> str:
    """Normalize a filesystem path for comparison without requiring it to exist."""
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(str(Path(path).expanduser())))


def find_project_by_repo_path(repo_path: str) -> Optional[dict]:
    """Find the project linked to a workspace path (or any of its ancestors).

    This is the reverse lookup that makes the folder - not the caller's memory
    of project names - the source of truth. A path inside a linked repo matches
    that repo's project, so subdirectories resolve correctly.
    """
    candidate = _canonical_path(repo_path)
    if not candidate:
        return None

    conn = get_connection()
    rows = conn.execute(
        "SELECT name, repo_path FROM projects WHERE repo_path != ''"
    ).fetchall()
    conn.close()

    best: Optional[dict] = None
    best_len = -1
    for r in rows:
        linked = _canonical_path(r["repo_path"])
        if not linked:
            continue
        if candidate == linked or candidate.startswith(linked + os.sep):
            # Prefer the deepest (most specific) linked root
            if len(linked) > best_len:
                best = {"project": r["name"], "repo_path": r["repo_path"]}
                best_len = len(linked)
    return best


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
# Export / Import
# ---------------------------------------------------------------------------

EXPORT_FORMAT_VERSION = 1
_EXPORT_LOG_CAP = 200


def export_project(name: str) -> dict:
    """Export one project's full state as a plain dict (re-importable)."""
    conn = get_connection()
    project = get_project(name, conn)
    if "error" in project:
        conn.close()
        return project

    updates = conn.execute(
        """SELECT tool_name, summary, timestamp FROM update_log
           WHERE project = ? ORDER BY timestamp DESC LIMIT ?""",
        (name, _EXPORT_LOG_CAP),
    ).fetchall()
    messages = conn.execute(
        """SELECT author, message, timestamp FROM project_messages
           WHERE project = ? ORDER BY timestamp""",
        (name,),
    ).fetchall()
    bugs = conn.execute(
        "SELECT description, status, created_at, updated_at FROM bugs WHERE project = ? ORDER BY id",
        (name,),
    ).fetchall()
    docs = conn.execute(
        "SELECT kind, content, updated_at, updated_by FROM project_docs WHERE project = ? ORDER BY kind",
        (name,),
    ).fetchall()
    conn.close()

    project["docs"] = [
        {"kind": r["kind"], "content": r["content"], "updated_at": r["updated_at"], "updated_by": r["updated_by"]}
        for r in docs
    ]
    project["update_log"] = [
        {"tool_name": r["tool_name"], "summary": r["summary"], "timestamp": r["timestamp"]}
        for r in reversed(updates)  # chronological order in the export
    ]
    project["messages"] = [
        {"author": r["author"], "message": r["message"], "timestamp": r["timestamp"]}
        for r in messages
    ]
    project["bugs"] = [
        {
            "description": r["description"], "status": r["status"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        }
        for r in bugs
    ]
    return project


def export_all() -> dict:
    """Export every project into one document."""
    names = [p["project"] for p in list_projects()]
    return {
        "format": "one-context-export",
        "version": EXPORT_FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "projects": [export_project(n) for n in names],
    }


def import_project(data: dict, mode: str = "merge") -> dict:
    """Import one exported project dict.

    merge   - fill empty buckets, keep non-empty ones; union bugs/messages
              (skip entries that already exist with identical content).
    replace - overwrite buckets and repo_path; still unions history rows.
    """
    if mode not in ("merge", "replace"):
        return {"error": "mode must be 'merge' or 'replace'."}
    name = data.get("project")
    if not name:
        return {"error": "Export data has no 'project' field."}

    conn = get_connection()
    ts = datetime.now(timezone.utc).isoformat()
    row = conn.execute("SELECT name FROM projects WHERE name = ?", (name,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO projects (name, repo_path, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, data.get("repo_path", ""), data.get("created_at", ts), ts),
        )

    current = get_project(name, conn)
    buckets = {}
    for bucket in ("what", "done", "now", "map"):
        incoming = data.get(bucket, "") or ""
        if mode == "replace":
            buckets[bucket] = incoming
        else:
            buckets[bucket] = current.get(bucket) or incoming

    repo_path = data.get("repo_path", "") if mode == "replace" else (current.get("repo_path") or data.get("repo_path", ""))

    conn.execute(
        """UPDATE projects SET what = ?, done = ?, now = ?, map = ?, repo_path = ?, updated_at = ?
           WHERE name = ?""",
        (buckets["what"], buckets["done"], buckets["now"],
         normalize_map_content(buckets["map"]), repo_path, ts, name),
    )

    imported_updates = 0
    for u in data.get("update_log", []):
        exists = conn.execute(
            "SELECT 1 FROM update_log WHERE project = ? AND timestamp = ? AND summary = ?",
            (name, u.get("timestamp", ""), u.get("summary", "")),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO update_log (project, tool_name, summary, timestamp) VALUES (?, ?, ?, ?)",
                (name, u.get("tool_name", "import"), u.get("summary", ""), u.get("timestamp", ts)),
            )
            imported_updates += 1

    imported_messages = 0
    for m in data.get("messages", []):
        exists = conn.execute(
            "SELECT 1 FROM project_messages WHERE project = ? AND timestamp = ? AND message = ?",
            (name, m.get("timestamp", ""), m.get("message", "")),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO project_messages (project, author, message, timestamp) VALUES (?, ?, ?, ?)",
                (name, m.get("author", "user"), m.get("message", ""), m.get("timestamp", ts)),
            )
            imported_messages += 1

    imported_docs = 0
    for d in data.get("docs", []):
        kind = (d.get("kind") or "").strip().lower()
        if not kind:
            continue
        existing = conn.execute(
            "SELECT 1 FROM project_docs WHERE project = ? AND kind = ?", (name, kind)
        ).fetchone()
        if mode == "replace" or not existing:
            conn.execute(
                """INSERT INTO project_docs (project, kind, content, updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(project, kind) DO UPDATE SET
                     content = excluded.content,
                     updated_at = excluded.updated_at,
                     updated_by = excluded.updated_by""",
                (name, kind, d.get("content", ""), d.get("updated_at", ts), d.get("updated_by", "")),
            )
            imported_docs += 1

    imported_bugs = 0
    for b in data.get("bugs", []):
        exists = conn.execute(
            "SELECT 1 FROM bugs WHERE project = ? AND description = ? AND created_at = ?",
            (name, b.get("description", ""), b.get("created_at", "")),
        ).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO bugs (project, description, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, b.get("description", ""),
                 b.get("status", "open") if b.get("status") in ("open", "fixed") else "open",
                 b.get("created_at", ts), b.get("updated_at", ts)),
            )
            imported_bugs += 1

    conn.commit()
    result = get_project(name, conn)
    conn.close()
    result["imported"] = {
        "mode": mode,
        "updates": imported_updates,
        "messages": imported_messages,
        "bugs": imported_bugs,
        "docs": imported_docs,
    }
    return result


def render_project_markdown(data: dict) -> str:
    """Render an exported project dict as human-readable Markdown."""
    lines = [f"# {data.get('project', 'unknown')}", ""]
    if data.get("repo_path"):
        lines += [f"**Repo:** `{data['repo_path']}`", ""]
    for bucket, title in (("what", "WHAT"), ("done", "DONE"), ("now", "NOW"), ("map", "MAP")):
        lines += [f"## {title}", "", data.get(bucket) or "_(empty)_", ""]
    for d in data.get("docs", []):
        lines += [f"## DOC: {d.get('kind', '').upper()}", "", d.get("content") or "_(empty)_", ""]
    bugs = data.get("bugs", [])
    if bugs:
        lines += ["## BUGS", ""]
        for b in bugs:
            mark = "x" if b.get("status") == "fixed" else " "
            lines.append(f"- [{mark}] {b.get('description', '')}")
        lines.append("")
    messages = data.get("messages", [])
    if messages:
        lines += ["## NOTES", ""]
        for m in messages:
            lines.append(f"- ({m.get('author', 'user')}) {m.get('message', '')}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cross-project search
# ---------------------------------------------------------------------------

def search_projects(query: str, project: Optional[str] = None) -> list[dict]:
    """Search across projects' context buckets (what/done/now/map).

    `project` limits the search to one project; scoping happens in SQL so a
    scoped search can never lose matches to a global LIMIT.
    """
    conn = get_connection()
    pattern = f"%{query}%"
    sql = """SELECT name, what, done, now, map, updated_at FROM projects
             WHERE (what LIKE ? OR done LIKE ? OR now LIKE ? OR map LIKE ?)"""
    params: list = [pattern, pattern, pattern, pattern]
    if project:
        sql += " AND name = ?"
        params.append(project)
    sql += " ORDER BY updated_at DESC"
    rows = conn.execute(sql, params).fetchall()
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


def search_logs(query: str, project: Optional[str] = None) -> list[dict]:
    """Search update history, optionally scoped to one project (in SQL)."""
    conn = get_connection()
    pattern = f"%{query}%"
    sql = """SELECT project, tool_name, summary, timestamp FROM update_log
             WHERE summary LIKE ?"""
    params: list = [pattern]
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY timestamp DESC LIMIT 20"
    rows = conn.execute(sql, params).fetchall()
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


def search_messages(query: str, project: Optional[str] = None) -> list[dict]:
    """Search user-authored notes, optionally scoped to one project (in SQL)."""
    conn = get_connection()
    pattern = f"%{query}%"
    sql = """SELECT project, id, author, message, timestamp FROM project_messages
             WHERE message LIKE ?"""
    params: list = [pattern]
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY timestamp DESC LIMIT 20"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return [
        {
            "project": r["project"],
            "id": r["id"],
            "author": r["author"],
            "message": r["message"],
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]


def search_bugs(query: str, project: Optional[str] = None) -> list[dict]:
    """Search bug descriptions, optionally scoped to one project (in SQL)."""
    conn = get_connection()
    pattern = f"%{query}%"
    sql = "SELECT * FROM bugs WHERE description LIKE ?"
    params: list = [pattern]
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, updated_at DESC LIMIT 20"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_bug_row_to_dict(r) for r in rows]
