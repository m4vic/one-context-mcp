"""MCP Server for ctx - Combined Context.

Exposes tools over SSE/stdio transport:
  ctx_get, ctx_strict_get, ctx_update, ctx_note, ctx_history, ctx_link,
  ctx_reset, ctx_list, ctx_map, ctx_search

Uses a bare ASGI app with manual path routing so that the MCP SDK's
SseServerTransport gets direct, unmodified access to scope/receive/send.
"""

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
    update_project,
    update_project_map,
    reset_project,
    list_projects,
    init_project,
    search_projects,
    search_logs,
    search_messages,
    add_project_message,
    list_project_history,
    merge_map_content,
    normalize_map_content,
)
from ctx.llm import merge_context
from ctx.git import get_git_summary

logger = logging.getLogger("ctx")

# ---------------------------------------------------------------------------
# MCP Server Setup
# ---------------------------------------------------------------------------

mcp_server = Server("ctx", version=__version__)


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
    """Return a warning/error when a caller's current folder does not match the linked project."""
    if strict and not stored_repo_path:
        return {
            "error": "Project is not linked to a repo_path.",
            "project": project,
            "hint": "Call ctx_link(project, repo_path) once before using strict access.",
        }

    if not requested_repo_path:
        return None

    if not stored_repo_path:
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
                "current branch, recent commits, and changed files."
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
                "If repo_path is supplied and does not match an already linked project, the update is rejected."
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

        elif name == "ctx_strict_get":
            project = arguments["project"]
            result = get_project(project)

            if "error" not in result:
                guard = _repo_guard(project, result.get("repo_path", ""), arguments["repo_path"], strict=True)
                if guard and "error" in guard:
                    result = guard
                else:
                    result = _with_git_info(result)

        elif name == "ctx_update":
            project = arguments["project"]
            session_summary = arguments["session_summary"]
            tool_name = arguments.get("tool_name", "unknown")
            repo_path = arguments.get("repo_path")

            # Get current context
            current = get_project(project)
            if "error" in current:
                # Auto-init the project if it doesn't exist
                logger.info(f"Auto-initializing project '{project}'")
                init_project(project, repo_path=repo_path or "")
                current = {"what": "", "done": "", "now": "", "map": ""}
            else:
                guard = _repo_guard(project, current.get("repo_path", ""), repo_path, strict=True)
                if guard and "error" in guard:
                    result = guard
                    return [TextContent(type="text", text=json.dumps(result, indent=2))]

            # Merge (local by default, LLM-enhanced if configured)
            repo_scope = repo_path or current.get("repo_path", "")
            merged = merge_context(
                current_what=current.get("what", ""),
                current_done=current.get("done", ""),
                current_now=current.get("now", ""),
                current_map=current.get("map", ""),
                session_summary=session_summary,
                tool_name=tool_name,
                repo_path=repo_scope,
            )

            # Save
            result = update_project(
                name=project,
                what=merged["what"],
                done=merged["done"],
                now=merged["now"],
                map_=merged["map"],
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
                    repo_scope = repo_path or current.get("repo_path", "")
                    merged = merge_context(
                        current_what=current.get("what", ""),
                        current_done=current.get("done", ""),
                        current_now=current.get("now", ""),
                        current_map=current.get("map", ""),
                        session_summary=session_summary,
                        tool_name=f"user:{author}",
                        repo_path=repo_scope,
                    )
                    context = update_project(
                        name=project,
                        what=merged["what"],
                        done=merged["done"],
                        now=merged["now"],
                        map_=merged["map"],
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
            result = {
                "query": query,
                "projects": project_matches,
                "history": log_matches,
                "messages": message_matches,
                "total_matches": len(project_matches) + len(log_matches) + len(message_matches),
            }

        elif name == "ctx_reset":
            result = reset_project(arguments["project"])

        elif name == "ctx_list":
            result = list_projects()

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


async def _send_json(send, status: int, body: dict):
    """Helper to send a JSON response via raw ASGI."""
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            [b"content-type", b"application/json"],
            [b"access-control-allow-origin", b"*"],
            [b"access-control-allow-methods", b"GET, POST, OPTIONS"],
            [b"access-control-allow-headers", b"*"],
        ],
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

    # Handle CORS preflight
    if method == "OPTIONS":
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": [
                [b"access-control-allow-origin", b"*"],
                [b"access-control-allow-methods", b"GET, POST, OPTIONS"],
                [b"access-control-allow-headers", b"*"],
                [b"access-control-max-age", b"86400"],
            ],
        })
        await send({"type": "http.response.body", "body": b""})
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

    # Route: GET /health - health check
    if path == "/health" and method == "GET":
        await _send_json(send, 200, {
            "status": "ok",
            "server": "ctx",
            "version": __version__,
            "tools": [
                "ctx_get", "ctx_strict_get", "ctx_update", "ctx_note",
                "ctx_history", "ctx_link", "ctx_map", "ctx_search",
                "ctx_reset", "ctx_list",
            ],
        })
        return

    # 404 for everything else
    await _send_json(send, 404, {"error": "Not found"})
