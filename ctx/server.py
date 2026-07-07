"""MCP Server for ctx - Combined Context.

Exposes tools over SSE/stdio transport:
  ctx_get, ctx_strict_get, ctx_update, ctx_note, ctx_history, ctx_link,
  ctx_reset, ctx_list, ctx_map, ctx_search

Uses a bare ASGI app with manual path routing so that the MCP SDK's
SseServerTransport gets direct, unmodified access to scope/receive/send.
"""

import hmac
import json
import logging
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent

from ctx import __version__
from ctx.database import (
    get_project,
    update_project_map,
    atomic_merge_update,
    reset_project,
    list_projects,
    init_project,
    search_projects,
    search_logs,
    search_messages,
    search_bugs,
    add_project_message,
    add_bug,
    set_bug_status,
    list_bugs,
    count_bugs,
    find_project_by_repo_path,
    list_project_history,
    merge_map_content,
    normalize_map_content,
    export_project,
    export_all,
    import_project,
    render_project_markdown,
    set_doc,
    get_doc,
    list_docs,
    delete_doc,
)
from ctx.llm import merge_context
from ctx.git import get_git_summary

logger = logging.getLogger("ctx")

# ---------------------------------------------------------------------------
# MCP Server Setup
# ---------------------------------------------------------------------------

HOW_TO_CTX = """\
one-context is shared, persistent project memory across AI tools (Claude, Cline,
Codex, ...). It stores context locally in SQLite so you don't re-explain a
project every session. Use it like this:

WORKFLOW (every session)
1. At the start, find the project for the current folder:
   call ctx_resolve(repo_path=<workspace root>). If it returns a project, use
   that exact name. If not, ask the user or call ctx_link(project, repo_path).
2. Load context: ctx_get(project, repo_path=<workspace root>).
   - Read the returned `instructions` field FIRST and follow it as project rules.
   - Use view="detailed" when you need the full verbatim history + docs (e.g. an
     implementation plan), or view="brief" when your own context is tight.
3. Do the work.
4. At the end, persist what changed: ctx_update(project, session_summary=...,
   tool_name=<your name>). Keep the summary concrete (what changed, what's done,
   what's next, key files).

WHERE THINGS GO
- ctx_update  -> WHAT/DONE/NOW/MAP buckets. Auto-merged + summarized. Lossy by
  design; good for evolving state, NOT for exact text you must preserve.
- ctx_doc(project, kind, content) -> VERBATIM documents, stored and returned
  exactly. Use kind="instructions" for project rules the model must follow,
  kind="plan" for an implementation plan, kind="context" for a full detailed
  brief. Retrieve with ctx_doc(project, kind) or see them in ctx_get.
- ctx_note   -> a user message pinned to the project.
- ctx_bug    -> known bugs (add / list / mark fixed).
- ctx_map    -> important files and what they do.

RULES OF THUMB
- Use ONE stable project name per repo; link it once with ctx_link.
- Never guess the project name from memory - resolve it from the folder.
- Put anything that must survive verbatim (plans, rules, specs) in ctx_doc, not
  in a bucket.
- Back up / move context with ctx_export / ctx_import.
"""

# Short pointer injected via the MCP server `instructions` capability. Clients
# that support it show this to the model automatically at connect time; the full
# guide is always available on demand via the how_to_ctx tool.
SERVER_INSTRUCTIONS = (
    "one-context provides shared, persistent per-project memory. At the start of "
    "work call ctx_resolve(repo_path) then ctx_get(project), and follow the "
    "returned `instructions` field. Persist changes with ctx_update at the end. "
    "Call how_to_ctx() for the full usage guide."
)

mcp_server = Server("ctx", version=__version__, instructions=SERVER_INSTRUCTIONS)


def _detail_char_budget() -> int:
    """Char budget for the verbatim history in ctx_get(view='detailed')."""
    try:
        return max(2000, int(os.environ.get("CTX_MAX_DETAIL_CHARS", "20000")))
    except ValueError:
        return 20000


def _canonical_repo_path(path: str | None) -> str:
    """Normalize a repo path for comparison without requiring it to exist."""
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(str(Path(path).expanduser())))


def _repo_paths_match(left: str | None, right: str | None) -> bool:
    left_path = _canonical_repo_path(left)
    right_path = _canonical_repo_path(right)
    return bool(left_path and right_path and left_path == right_path)


def _repo_guard(project: str, stored_repo_path: str, requested_repo_path: str | None, strict: bool = False) -> dict | None:
    """Return a warning/error when a caller's current folder does not match the linked project.

    No requested_repo_path means there is nothing to compare - the call
    proceeds. (Callers that require a path, like ctx_strict_get, enforce
    that in their input schema.)
    """
    if not requested_repo_path:
        return None

    if not stored_repo_path:
        if strict:
            return {
                "error": "Project is not linked to a repo_path.",
                "project": project,
                "hint": "Call ctx_link(project, repo_path) once before using strict access.",
            }
        return {
            "warning": "Project is not linked yet; this repo_path can be stored with ctx_link.",
            "project": project,
            "requested_repo_path": requested_repo_path,
        }

    if not _repo_paths_match(stored_repo_path, requested_repo_path):
        payload = {
            "project": project,
            "stored_repo_path": stored_repo_path,
            "requested_repo_path": requested_repo_path,
            "hint": "Use the correct project name or call ctx_link only if this project was intentionally moved.",
        }
        if strict:
            return {"error": "repo_path mismatch", **payload}
        return {"warning": "repo_path mismatch", **payload}

    return None


def _auto_init_guard(project: str, repo_path: str | None) -> dict | None:
    """Block creating a new project for a folder already linked to another one.

    Without this, a typo in the project name ('pocket-lab' vs 'pocketlab')
    silently creates a twin project and forks the context. If the caller's
    folder is already linked, the write must go to that project or be
    explicitly re-linked first.
    """
    if not repo_path:
        return None
    owner = find_project_by_repo_path(repo_path)
    if owner and owner["project"] != project:
        return {
            "error": "This folder already belongs to another project.",
            "requested_project": project,
            "linked_project": owner["project"],
            "repo_path": owner["repo_path"],
            "hint": (
                f"Use project '{owner['project']}' for this folder, or call "
                f"ctx_link('{project}', <other path>) if this is genuinely a new project."
            ),
        }
    return None


def _with_git_info(result: dict) -> dict:
    if "error" not in result and result.get("repo_path"):
        git_info = get_git_summary(result["repo_path"])
        if git_info:
            result["git"] = git_info
    return result


@mcp_server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """Declare the tools this server exposes."""
    return [
        Tool(
            name="ctx_get",
            description=(
                "Get the current context snapshot for a project. "
                "Returns WHAT (project description, stack, architecture), "
                "DONE (decisions made, files changed, problems solved), "
                "NOW (current task, current state, what's in progress), "
                "and MAP (important files and their purpose). "
                "If the project has a linked git repo, also returns the "
                "current branch, recent commits, and changed files. "
                "ALWAYS pass repo_path (the current workspace root) so a "
                "mismatch with the linked project is detected. If you don't "
                "know the project name for the current folder, call "
                "ctx_resolve(repo_path) first instead of guessing. "
                "Pass view='brief' when your own context is tight: it returns "
                "WHAT and NOW in full but only the most recent slice of DONE. "
                "Pass view='detailed' to also get the full verbatim update "
                "history and notes (nothing summarized) in the same call - use "
                "this when you need the complete detailed context, e.g. an "
                "implementation plan, not just the compact buckets."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name / namespace",
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Optional current workspace root. If it differs from the linked repo_path, a warning is returned.",
                    },
                    "view": {
                        "type": "string",
                        "enum": ["full", "brief", "detailed"],
                        "description": "full (default), brief (recent DONE slice only), or detailed (adds full verbatim update history + notes for the complete detailed context).",
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="ctx_strict_get",
            description=(
                "Get context only if the supplied repo_path matches the linked project repo_path. "
                "Use this when a client is inside a workspace and must avoid loading the wrong project."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name / namespace",
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the current workspace root.",
                    },
                },
                "required": ["project", "repo_path"],
            },
        ),
        Tool(
            name="ctx_update",
            description=(
                "Update the project context with a session summary. "
                "The server will intelligently merge this into the "
                "WHAT/DONE/NOW/MAP buckets using smart merging. "
                "Call this when you finish a task or hit a milestone. "
                "ALWAYS pass repo_path (the current workspace root): mismatched "
                "updates are rejected, and creating a new project for a folder "
                "that is already linked to a different project is rejected "
                "(prevents typo-split projects and cross-project mixing)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name / namespace",
                    },
                    "session_summary": {
                        "type": "string",
                        "description": (
                            "Summary of what happened in this session - "
                            "decisions made, files changed, current state."
                        ),
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Name of the AI tool sending this update (e.g. 'claude-code', 'cline', 'antigravity')",
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Optional: absolute path to the project's git repo on disk. Set once, stored permanently.",
                    },
                },
                "required": ["project", "session_summary", "tool_name"],
            },
        ),
        Tool(
            name="ctx_map",
            description=(
                "Register important files for a project. Use this to tell "
                "other AI tools which files are the entry points, core modules, "
                "or config files. Format each entry as 'path - description'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name / namespace",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of important files. Each entry should be "
                            "'path/to/file - what it does' (e.g. 'src/main.py - app entry point')"
                        ),
                    },
                    "replace": {
                        "type": "boolean",
                        "description": "If true, replaces the entire MAP. If false (default), appends to existing MAP.",
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Optional current workspace root. If it differs from the linked repo_path, the map update is rejected.",
                    },
                },
                "required": ["project", "files"],
            },
        ),
        Tool(
            name="ctx_note",
            description=(
                "Add a user-authored note/message to one specific project. "
                "By default the note is also merged into WHAT/DONE/NOW/MAP "
                "so other tools can load it later with ctx_get."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name / namespace",
                    },
                    "message": {
                        "type": "string",
                        "description": "User note or instruction to attach to this project",
                    },
                    "author": {
                        "type": "string",
                        "description": "Optional note author, defaults to 'user'",
                    },
                    "merge": {
                        "type": "boolean",
                        "description": "If true (default), merge the note into project context buckets.",
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Optional current workspace root. If it differs from the linked repo_path, the note merge is rejected.",
                    },
                },
                "required": ["project", "message"],
            },
        ),
        Tool(
            name="ctx_history",
            description=(
                "Get recent update summaries and user notes for one project. "
                "Use this when you need the audit trail for a specific project."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name / namespace",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum updates/messages to return from each list, default 20.",
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="ctx_link",
            description=(
                "Create a project if needed and link it to an absolute repo path. "
                "Use this once per project so future MAP extraction is scoped to "
                "the correct workspace root."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name / namespace",
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the project's git/workspace root",
                    },
                },
                "required": ["project", "repo_path"],
            },
        ),
        Tool(
            name="ctx_search",
            description=(
                "Search across ALL projects' context and history. "
                "Use this to find how a problem was solved before, "
                "or which project uses a certain technology. "
                "Returns matching projects and relevant log entries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term to look for across all projects",
                    }
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="ctx_reset",
            description="Wipe the project context back to empty. Use for a fresh start.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name / namespace",
                    }
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="ctx_list",
            description="List all known projects tracked by the context server.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="ctx_resolve",
            description=(
                "Find which project is linked to a workspace folder. "
                "Call this FIRST when you are inside a workspace and do not "
                "know (or are not sure of) the project name - the folder is "
                "the source of truth, not a remembered name. Paths inside a "
                "linked repo resolve to that repo's project."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path of the current workspace root (or any folder inside it).",
                    },
                },
                "required": ["repo_path"],
            },
        ),
        Tool(
            name="ctx_bug",
            description=(
                "Track bugs for a project. Three uses based on which fields you send:\n"
                "- List bugs: send only 'project' (open bugs first).\n"
                "- Add a bug: send 'project' and 'description' (created as open).\n"
                "- Update a bug: send 'project', 'bug_id', and 'status' "
                "('open' or 'fixed').\n"
                "Open bugs are also surfaced automatically on ctx_get so any AI "
                "tool loading the project sees what is still broken."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name / namespace",
                    },
                    "description": {
                        "type": "string",
                        "description": "Bug description. Provide this to add a new open bug.",
                    },
                    "bug_id": {
                        "type": "integer",
                        "description": "ID of an existing bug to update. Use with 'status'.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "fixed"],
                        "description": "New status when updating an existing bug.",
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="ctx_export",
            description=(
                "Export project context for backup or sharing. Returns JSON "
                "(canonical, re-importable via ctx_import) or Markdown "
                "(human-readable). Omit 'project' to export every project. "
                "Use this to commit context to a repo, move it to another "
                "machine, or take a backup before risky changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project to export. Omit to export all projects.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["json", "markdown"],
                        "description": "Output format, default json. Only json can be re-imported.",
                    },
                },
            },
        ),
        Tool(
            name="ctx_import",
            description=(
                "Import context previously produced by ctx_export. Pass the "
                "value ctx_export returned directly (the object), or a JSON "
                "string of it - both work. "
                "mode 'merge' (default) fills empty buckets and unions "
                "bugs/notes/history without overwriting existing content; "
                "mode 'replace' overwrites the project's buckets."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {
                        "type": ["string", "object"],
                        "description": "The object returned by ctx_export, or a JSON string of it.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["merge", "replace"],
                        "description": "merge (default) or replace.",
                    },
                },
                "required": ["data"],
            },
        ),
        Tool(
            name="ctx_doc",
            description=(
                "Read or write a VERBATIM per-project document, stored and "
                "returned exactly as written (no merging or summarizing, unlike "
                "the WHAT/DONE/NOW/MAP buckets). Use this for content that must "
                "survive word-for-word. Common kinds: 'instructions' (project "
                "rules the assistant must follow - surfaced at the top of "
                "ctx_get), 'plan' (an implementation plan), 'context' (a full "
                "detailed brief). Kind is free-form, so 'runbook', "
                "'architecture', etc. also work. "
                "Provide 'content' to save; omit it to read. "
                "action: 'get' (default), 'set', 'list' (index of a project's "
                "docs), or 'delete'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name."},
                    "kind": {
                        "type": "string",
                        "description": "Document kind, e.g. instructions, plan, context. Required except for action='list'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Verbatim document text. Providing it implies action='set'.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["get", "set", "list", "delete"],
                        "description": "get (default), set, list, or delete.",
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="how_to_ctx",
            description=(
                "Return the usage guide for one-context: the recommended "
                "workflow (resolve -> get -> work -> update), which tool to use "
                "for what, and best practices. Call this if you are unsure how "
                "to use these context tools correctly."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Route tool calls to the appropriate handler."""
    try:
        if name == "ctx_get":
            project = arguments["project"]
            result = get_project(project)

            if "error" not in result:
                guard = _repo_guard(project, result.get("repo_path", ""), arguments.get("repo_path"))
                if guard:
                    result["safety"] = guard
                result = _with_git_info(result)
                result["bugs"] = list_bugs(project, status="open")
                result["bugs_fixed_count"] = count_bugs(project, "fixed")

                # Surface verbatim docs: project instructions are returned in
                # full at the top (follow them as rules); other docs (plan,
                # context, ...) appear as an index unless view='detailed'.
                docs_index = list_docs(project)
                view = arguments.get("view")
                if docs_index:
                    result["docs"] = docs_index
                instr = next((d for d in docs_index if d["kind"] == "instructions"), None)
                if instr:
                    doc = get_doc(project, "instructions")
                    if "error" not in doc:
                        result["instructions"] = doc["content"]

                if view == "brief":
                    done = result.get("done", "")
                    brief_cap = 2000
                    if len(done) > brief_cap:
                        # Keep the newest whole lines (buckets append at the bottom)
                        lines = done.splitlines()
                        kept: list[str] = []
                        size = 0
                        for line in reversed(lines):
                            size += len(line) + 1
                            if size > brief_cap:
                                break
                            kept.append(line)
                        result["done"] = "\n".join(reversed(kept))
                        result["done_truncated"] = True
                        result["hint"] = "DONE is truncated in brief view; use ctx_history or ctx_search for older entries."
                elif view == "detailed":
                    # One call returns the complete picture: the summarized
                    # buckets PLUS the full verbatim update history and notes
                    # (nothing is summarized away). Bounded by a char budget so
                    # a huge project can't blow up the caller's context.
                    history = list_project_history(project, limit=100)
                    budget = _detail_char_budget()
                    updates, spent = [], 0
                    for u in history.get("updates", []):  # newest first
                        spent += len(u.get("summary", "")) + 40
                        if updates and spent > budget:
                            break
                        updates.append(u)
                    result["updates"] = updates
                    result["notes"] = history.get("messages", [])
                    if len(updates) < len(history.get("updates", [])):
                        result["updates_truncated"] = True
                        result["hint"] = "Older updates omitted from detailed view; use ctx_history for the full log."
                    # Full verbatim docs (plan, context, ...) in the same call.
                    result["docs"] = [
                        {**d, "content": get_doc(project, d["kind"]).get("content", "")}
                        for d in docs_index
                    ]

        elif name == "ctx_strict_get":
            project = arguments["project"]
            result = get_project(project)

            if "error" not in result:
                guard = _repo_guard(project, result.get("repo_path", ""), arguments["repo_path"], strict=True)
                if guard and "error" in guard:
                    result = guard
                else:
                    result = _with_git_info(result)
                    result["bugs"] = list_bugs(project, status="open")
                    result["bugs_fixed_count"] = count_bugs(project, "fixed")

        elif name == "ctx_update":
            project = arguments["project"]
            session_summary = arguments["session_summary"]
            tool_name = arguments.get("tool_name", "unknown")
            repo_path = arguments.get("repo_path")

            # Get current context (for guard + auto-init decision)
            current = get_project(project)
            if "error" in current:
                guard = _auto_init_guard(project, repo_path)
                if guard:
                    return [TextContent(type="text", text=json.dumps(guard, indent=2))]
                # Auto-init the project if it doesn't exist
                logger.info(f"Auto-initializing project '{project}'")
                init_project(project, repo_path=repo_path or "")
            else:
                guard = _repo_guard(project, current.get("repo_path", ""), repo_path, strict=True)
                if guard and "error" in guard:
                    result = guard
                    return [TextContent(type="text", text=json.dumps(result, indent=2))]

            # Merge (local by default, LLM-enhanced if configured) inside a
            # single transaction so concurrent updates can't lose each other.
            def _merge(cur: dict) -> dict:
                return merge_context(
                    current_what=cur.get("what", ""),
                    current_done=cur.get("done", ""),
                    current_now=cur.get("now", ""),
                    current_map=cur.get("map", ""),
                    session_summary=session_summary,
                    tool_name=tool_name,
                    repo_path=repo_path or cur.get("repo_path", ""),
                )

            result = atomic_merge_update(
                name=project,
                merge_fn=_merge,
                tool_name=tool_name,
                summary=session_summary,
                repo_path=repo_path,
            )

        elif name == "ctx_map":
            project = arguments["project"]
            files = arguments["files"]
            replace = arguments.get("replace", False)
            repo_path = arguments.get("repo_path")

            # Get current map
            current = get_project(project)
            if "error" in current:
                guard = _auto_init_guard(project, repo_path)
                if guard:
                    return [TextContent(type="text", text=json.dumps(guard, indent=2))]
                # Auto-init
                init_project(project, repo_path=repo_path or "")
                current = {"map": ""}
            else:
                guard = _repo_guard(project, current.get("repo_path", ""), repo_path, strict=True)
                if guard and "error" in guard:
                    result = guard
                    return [TextContent(type="text", text=json.dumps(result, indent=2))]

            new_entries = '\n'.join(f'- {f}' for f in files)
            if replace:
                new_map = normalize_map_content(new_entries)
            else:
                existing = current.get("map", "")
                new_map = merge_map_content(existing, new_entries)

            result = update_project_map(project, new_map)

        elif name == "ctx_note":
            project = arguments["project"]
            message = arguments["message"].strip()
            author = arguments.get("author", "user") or "user"
            merge = arguments.get("merge", True)
            repo_path = arguments.get("repo_path")

            if not message:
                result = {"error": "message must not be empty"}
            else:
                current = get_project(project)
                if "error" in current:
                    guard = _auto_init_guard(project, repo_path)
                    if guard:
                        return [TextContent(type="text", text=json.dumps(guard, indent=2))]
                    init_project(project, repo_path=repo_path or "")
                    current = {"what": "", "done": "", "now": "", "map": "", "repo_path": repo_path or ""}
                else:
                    guard = _repo_guard(project, current.get("repo_path", ""), repo_path, strict=True)
                    if guard and "error" in guard:
                        result = guard
                        return [TextContent(type="text", text=json.dumps(result, indent=2))]

                saved_message = add_project_message(project, message, author=author)
                if "error" in saved_message or not merge:
                    result = {
                        "message": saved_message,
                        "context_updated": False,
                    }
                else:
                    session_summary = f"User note from {author}: {message}"

                    def _merge_note(cur: dict) -> dict:
                        return merge_context(
                            current_what=cur.get("what", ""),
                            current_done=cur.get("done", ""),
                            current_now=cur.get("now", ""),
                            current_map=cur.get("map", ""),
                            session_summary=session_summary,
                            tool_name=f"user:{author}",
                            repo_path=repo_path or cur.get("repo_path", ""),
                        )

                    context = atomic_merge_update(
                        name=project,
                        merge_fn=_merge_note,
                        tool_name=f"user:{author}",
                        summary=session_summary,
                        repo_path=repo_path,
                    )
                    result = {
                        "message": saved_message,
                        "context_updated": "error" not in context,
                        "context": context,
                    }

        elif name == "ctx_history":
            result = list_project_history(
                arguments["project"],
                limit=arguments.get("limit", 20),
            )

        elif name == "ctx_link":
            result = init_project(
                arguments["project"],
                repo_path=arguments["repo_path"],
            )

        elif name == "ctx_search":
            query = arguments["query"]
            project_matches = search_projects(query)
            log_matches = search_logs(query)
            message_matches = search_messages(query)
            bug_matches = search_bugs(query)
            result = {
                "query": query,
                "projects": project_matches,
                "history": log_matches,
                "messages": message_matches,
                "bugs": bug_matches,
                "total_matches": (
                    len(project_matches) + len(log_matches)
                    + len(message_matches) + len(bug_matches)
                ),
            }

        elif name == "ctx_reset":
            result = reset_project(arguments["project"])

        elif name == "ctx_list":
            result = list_projects()

        elif name == "ctx_resolve":
            repo_path = arguments["repo_path"]
            owner = find_project_by_repo_path(repo_path)
            if owner:
                result = {
                    "project": owner["project"],
                    "repo_path": owner["repo_path"],
                    "hint": f"Load context with ctx_get('{owner['project']}').",
                }
            else:
                result = {
                    "error": "No project is linked to this folder.",
                    "requested_repo_path": repo_path,
                    "hint": "Call ctx_link(project, repo_path) to link it, or ctx_list() to see all projects.",
                }

        elif name == "ctx_bug":
            project = arguments["project"]
            description = arguments.get("description")
            bug_id = arguments.get("bug_id")
            status = arguments.get("status")

            if bug_id is not None:
                # Update an existing bug's status
                if status is None:
                    result = {"error": "Provide 'status' ('open' or 'fixed') to update a bug."}
                else:
                    result = set_bug_status(project, int(bug_id), status)
            elif description:
                # Add a new bug (auto-init the project if needed)
                if "error" in get_project(project):
                    init_project(project)
                result = add_bug(project, description)
            else:
                # List bugs for the project
                if "error" in get_project(project):
                    result = {"error": f"Project '{project}' not found."}
                else:
                    result = {
                        "project": project,
                        "bugs": list_bugs(project),
                    }

        elif name == "ctx_export":
            project = arguments.get("project")
            fmt = arguments.get("format", "json")
            if project:
                data = export_project(project)
            else:
                data = export_all()

            if "error" in data:
                result = data
            elif fmt == "markdown":
                if "projects" in data:
                    text = "\n---\n\n".join(render_project_markdown(p) for p in data["projects"])
                else:
                    text = render_project_markdown(data)
                return [TextContent(type="text", text=text)]
            else:
                result = data

        elif name == "ctx_import":
            raw = arguments["data"]
            mode = arguments.get("mode", "merge")
            # Accept the data as a JSON string OR an already-parsed object/list.
            # ctx_export returns an object, so assistants naturally pass it
            # straight back into ctx_import; requiring a hand-stringified JSON
            # string was the most common cause of import failures.
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            payload = None
            if isinstance(raw, (dict, list)):
                payload = raw
            elif isinstance(raw, str):
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as e:
                    result = {"error": f"data is not valid JSON: {e}"}
            else:
                result = {"error": f"data must be a ctx_export JSON object or string (got {type(raw).__name__})."}

            if payload is not None:
                is_collection = (
                    isinstance(payload, dict) and "projects" in payload
                ) or isinstance(payload, list)
                if isinstance(payload, dict) and "projects" in payload:
                    projects = payload["projects"]
                elif isinstance(payload, list):
                    projects = payload
                else:  # single project dict
                    projects = [payload]

                if not projects or not all(isinstance(p, dict) for p in projects):
                    result = {"error": "data must be a ctx_export JSON object (or list of project objects)."}
                elif is_collection:
                    results = [import_project(p, mode=mode) for p in projects]
                    result = {
                        "imported_projects": [
                            r.get("project", r.get("error")) for r in results
                        ],
                        "details": results,
                    }
                else:
                    result = import_project(projects[0], mode=mode)

        elif name == "ctx_doc":
            project = arguments["project"]
            kind = arguments.get("kind")
            content = arguments.get("content")
            action = arguments.get("action")
            if action is None:
                action = "set" if content is not None else ("list" if not kind else "get")
            if action == "list":
                result = {"project": project, "docs": list_docs(project)}
            elif action == "set":
                if content is None:
                    result = {"error": "content is required to set a doc."}
                else:
                    result = set_doc(project, kind or "", content, updated_by=arguments.get("tool_name", ""))
            elif action == "delete":
                result = delete_doc(project, kind or "")
            else:  # get
                result = get_doc(project, kind or "")

        elif name == "how_to_ctx":
            return [TextContent(type="text", text=HOW_TO_CTX)]

        else:
            result = {"error": f"Unknown tool: {name}"}

    except Exception as e:
        logger.exception(f"Error handling tool {name}")
        result = {"error": str(e)}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


# ---------------------------------------------------------------------------
# Bare ASGI App - no framework, no wrappers, no conflicts.
# ---------------------------------------------------------------------------

sse_transport = SseServerTransport("/messages/")


def _cors_origin(scope) -> bytes | None:
    """Decide which CORS origin (if any) to allow for this request.

    Without an auth token the server only binds localhost by default, so the
    historical wildcard stays. With CTX_AUTH_TOKEN set (network exposure is
    intended), only localhost origins are reflected - browser pages from other
    hosts get no CORS grant.
    """
    if not os.environ.get("CTX_AUTH_TOKEN"):
        return b"*"
    origin = b""
    for k, v in scope.get("headers", []):
        if k == b"origin":
            origin = v
            break
    for prefix in (b"http://localhost", b"http://127.0.0.1",
                   b"https://localhost", b"https://127.0.0.1"):
        if origin == prefix or origin.startswith(prefix + b":"):
            return origin
    return None


def _authorized(scope) -> bool:
    """Check the Bearer token when CTX_AUTH_TOKEN is set (timing-safe)."""
    token = os.environ.get("CTX_AUTH_TOKEN", "")
    if not token:
        return True
    expected = f"Bearer {token}".encode()
    supplied = b""
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            supplied = v
            break
    return hmac.compare_digest(supplied, expected)


async def _send_json(send, status: int, body: dict, cors: bytes | None = b"*"):
    """Helper to send a JSON response via raw ASGI."""
    payload = json.dumps(body).encode()
    headers = [[b"content-type", b"application/json"]]
    if cors is not None:
        headers += [
            [b"access-control-allow-origin", cors],
            [b"access-control-allow-methods", b"GET, POST, OPTIONS"],
            [b"access-control-allow-headers", b"*"],
        ]
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers,
    })
    await send({
        "type": "http.response.body",
        "body": payload,
    })


async def app(scope, receive, send):
    """Root ASGI application with manual path routing."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                logger.info("ctx server starting up")
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                logger.info("ctx server shutting down")
                await send({"type": "lifespan.shutdown.complete"})
                return
            else:
                return
        return

    if scope["type"] not in ("http",):
        return

    path = scope.get("path", "")
    method = scope.get("method", "GET")
    cors = _cors_origin(scope)
    auth_enabled = bool(os.environ.get("CTX_AUTH_TOKEN"))

    # Handle CORS preflight (kept open: preflights carry no Authorization)
    if method == "OPTIONS":
        headers = [[b"access-control-max-age", b"86400"]]
        if cors is not None:
            headers += [
                [b"access-control-allow-origin", cors],
                [b"access-control-allow-methods", b"GET, POST, OPTIONS"],
                [b"access-control-allow-headers", b"*"],
            ]
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": b""})
        return

    # Route: GET /health - health check (open, but terse when auth is on)
    if path == "/health" and method == "GET":
        body = {"status": "ok", "server": "ctx", "version": __version__}
        if not auth_enabled:
            body["tools"] = [
                "ctx_get", "ctx_strict_get", "ctx_update", "ctx_note",
                "ctx_history", "ctx_link", "ctx_map", "ctx_search",
                "ctx_reset", "ctx_list", "ctx_bug", "ctx_resolve",
                "ctx_export", "ctx_import",
            ]
        await _send_json(send, 200, body, cors=cors)
        return

    # Everything below reads or writes context - require the token if set.
    if not _authorized(scope):
        await _send_json(send, 401, {
            "error": "Unauthorized",
            "hint": "Send 'Authorization: Bearer <CTX_AUTH_TOKEN>'.",
        }, cors=cors)
        return

    # Route: GET /sse - SSE connection endpoint
    if path == "/sse" and method == "GET":
        async with sse_transport.connect_sse(scope, receive, send) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )
        return

    # Route: POST /messages/ - MCP message endpoint
    if path.startswith("/messages") and method == "POST":
        await sse_transport.handle_post_message(scope, receive, send)
        return

    # 404 for everything else
    await _send_json(send, 404, {"error": "Not found"}, cors=cors)
