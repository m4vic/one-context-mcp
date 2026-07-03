"""MCP Server for ctx — Combined Context.

Exposes six tools over SSE/stdio transport:
  ctx_get, ctx_update, ctx_reset, ctx_list, ctx_map, ctx_search

Uses a bare ASGI app with manual path routing so that the MCP SDK's
SseServerTransport gets direct, unmodified access to scope/receive/send.
"""

import json
import logging

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent

from ctx.database import (
    get_project,
    update_project,
    update_project_map,
    reset_project,
    list_projects,
    init_project,
    search_projects,
    search_logs,
)
from ctx.llm import merge_context
from ctx.git import get_git_summary

logger = logging.getLogger("ctx")

# ---------------------------------------------------------------------------
# MCP Server Setup
# ---------------------------------------------------------------------------

mcp_server = Server("ctx")


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
                    }
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="ctx_update",
            description=(
                "Update the project context with a session summary. "
                "The server will intelligently merge this into the "
                "WHAT/DONE/NOW/MAP buckets using smart merging. "
                "Call this when you finish a task or hit a milestone."
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
                            "Summary of what happened in this session — "
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
                "or config files. Format each entry as 'path — description'."
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
                            "'path/to/file — what it does' (e.g. 'src/main.py — app entry point')"
                        ),
                    },
                    "replace": {
                        "type": "boolean",
                        "description": "If true, replaces the entire MAP. If false (default), appends to existing MAP.",
                    },
                },
                "required": ["project", "files"],
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
            result = get_project(arguments["project"])

            # Enrich with git info if repo_path is set
            if "error" not in result and result.get("repo_path"):
                git_info = get_git_summary(result["repo_path"])
                if git_info:
                    result["git"] = git_info

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

            # Merge (local by default, LLM-enhanced if configured)
            merged = merge_context(
                current_what=current.get("what", ""),
                current_done=current.get("done", ""),
                current_now=current.get("now", ""),
                current_map=current.get("map", ""),
                session_summary=session_summary,
                tool_name=tool_name,
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

            # Get current map
            current = get_project(project)
            if "error" in current:
                # Auto-init
                init_project(project)
                current = {"map": ""}

            if replace:
                new_map = '\n'.join(f'- {f}' for f in files)
            else:
                existing = current.get("map", "")
                new_entries = '\n'.join(f'- {f}' for f in files)
                new_map = (existing.rstrip() + '\n' + new_entries).strip() if existing.strip() else new_entries

            result = update_project_map(project, new_map)

        elif name == "ctx_search":
            query = arguments["query"]
            project_matches = search_projects(query)
            log_matches = search_logs(query)
            result = {
                "query": query,
                "projects": project_matches,
                "history": log_matches,
                "total_matches": len(project_matches) + len(log_matches),
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
# Bare ASGI App — no framework, no wrappers, no conflicts.
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

    # Route: GET /sse — SSE connection endpoint
    if path == "/sse" and method == "GET":
        async with sse_transport.connect_sse(scope, receive, send) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )
        return

    # Route: POST /messages/ — MCP message endpoint
    if path.startswith("/messages") and method == "POST":
        await sse_transport.handle_post_message(scope, receive, send)
        return

    # Route: GET /health — health check
    if path == "/health" and method == "GET":
        await _send_json(send, 200, {
            "status": "ok",
            "server": "ctx",
            "version": "0.2.0",
            "tools": ["ctx_get", "ctx_update", "ctx_map", "ctx_search", "ctx_reset", "ctx_list"],
        })
        return

    # 404 for everything else
    await _send_json(send, 404, {"error": "Not found"})
