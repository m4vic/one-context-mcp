"""MCP Server for ctx - Combined Context.

Exposes 5 tools over SSE/stdio transport:
  ctx_get, ctx_update, ctx_doc, ctx_search, how_to_ctx

Uses a bare ASGI app with manual path routing so that the MCP SDK's
SseServerTransport gets direct, unmodified access to scope/receive/send.
"""

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, ToolAnnotations, TextContent

from ctx import __version__
from ctx.database import (
    get_project,
    merge_project_map,
    atomic_merge_update,
    init_project,
    search_projects,
    search_logs,
    search_messages,
    search_bugs,
    add_project_message,
    list_bugs,
    count_bugs,
    find_project_by_repo_path,
    list_project_history,
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
Codex, Antigravity, ...). It stores context locally in SQLite so you don't
re-explain a project every session, and so the next tool can pick up exactly
where the last one left off. There are 5 tools. Use them like this:

WORKFLOW (every session)
1. Load context for the current folder - let the folder resolve the project,
   don't guess the name:
     ctx_get(repo_path=<workspace root>)
   Read the returned `instructions` field FIRST and follow it as project rules.
   Pass view="detailed" for the full verbatim history + notes + docs (a complete
   handoff or an implementation plan); view="brief" if your own context is tight.
2. Do the work.
3. Persist what changed so the next tool/session continues smoothly:
     ctx_update(repo_path=<workspace root>, tool_name=<your name>,
                session_summary="what changed, what's done, what's next, files")

SWITCHING TOOLS (the handoff)
- Before you switch (e.g. Codex -> Antigravity): ctx_update with a concrete
  summary, and for a fuller narrative put it in ctx_doc(project, "handoff", ...).
- In the next tool: ctx_get(repo_path=<workspace root>) returns everything,
  including the handoff doc. Seconds, no re-explaining.

THE 5 TOOLS
- ctx_get(project?, repo_path?, view?)   -> load context. Omit project to resolve
  it from repo_path. view: full | brief | detailed.
- ctx_update(project?, session_summary?, tool_name, repo_path?, author?, files?)
  -> persist state (merged into WHAT/DONE/NOW/MAP). repo_path also links the
  project. author=<name> records a verbatim user note. files=[...] registers
  important files. Empty summary + repo_path just links/creates the project.
- ctx_doc(project, kind, content?, action?) -> VERBATIM documents (no merge).
  kind="instructions" (project rules, surfaced at top of ctx_get), "plan",
  "context", "handoff", or free-form.
- ctx_search(query, project?)            -> search context/history/notes.
- how_to_ctx()                           -> this guide.

RULES OF THUMB
- Buckets (WHAT/DONE/NOW/MAP) are auto-merged and summarized - good for evolving
  state, lossy. Anything that must survive word-for-word goes in ctx_doc.
- One stable project per repo; resolve it from the folder, never from memory.
- Always pass repo_path as an ABSOLUTE workspace root (a relative path would
  resolve against the server's cwd, not yours) so cross-project mixing is
  prevented.
- Backup/restore is a CLI job: `ctx export|import`.
"""

# Short pointer injected via the MCP server `instructions` capability. Clients
# that support it show this to the model automatically at connect time; the full
# guide is always available on demand via the how_to_ctx tool.
SERVER_INSTRUCTIONS = (
    "one-context provides shared, persistent per-project memory across AI tools. "
    "At the start of work call ctx_get(repo_path=<workspace root>) (omit project - "
    "the folder resolves it) and follow the returned `instructions` field. Persist "
    "changes with ctx_update(repo_path=..., tool_name=..., session_summary=...) "
    "before finishing or switching tools. Call how_to_ctx() for the full guide."
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
    proceeds.
    """
    if not requested_repo_path:
        return None

    if not stored_repo_path:
        return {
            "warning": "Project is not linked yet; link it with ctx_update(project, repo_path=...).",
            "project": project,
            "requested_repo_path": requested_repo_path,
        }

    if not _repo_paths_match(stored_repo_path, requested_repo_path):
        payload = {
            "project": project,
            "stored_repo_path": stored_repo_path,
            "requested_repo_path": requested_repo_path,
            "hint": "Use the correct project name, or re-link via the CLI (`ctx init <project> --path <new path>`) if the project folder was intentionally moved.",
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
                f"Use project '{owner['project']}' for this folder, or link "
                f"'{project}' to its own folder with ctx_update(project='{project}', "
                f"repo_path=<other path>) if this is genuinely a new project."
            ),
        }
    return None


async def _with_git_info(result: dict) -> dict:
    """Attach git status without blocking the event loop.

    git_summary shells out to git (subprocess). Running that synchronously on
    the asyncio event loop stalls the whole MCP stdio transport for its
    duration - on Windows this manifested as ctx_get 'hanging' in every client.
    Offload it to a worker thread so a slow/stuck git can never freeze the
    protocol loop.
    """
    if "error" not in result and result.get("repo_path"):
        git_info = await asyncio.to_thread(get_git_summary, result["repo_path"])
        if git_info:
            result["git"] = git_info
    return result

@mcp_server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """Declare the 5 focused tools this server exposes."""
    return [
        Tool(
            name="ctx_get",
            description=(
                "Load a project's shared context so another tool (or a new session) "
                "can continue: WHAT (identity/stack/architecture), DONE (decisions, "
                "changes, solved problems), NOW (current task/next steps), MAP "
                "(important files), plus git branch/commits/changes if the project is "
                "linked to a repo. Pass repo_path (the current workspace root) and OMIT "
                "project to auto-resolve which project owns this folder - the folder is "
                "the source of truth, do not guess the name. If both are given and "
                "repo_path does not match the linked repo, a safety warning is returned "
                "(heed it). A project's 'instructions' doc, if set, is returned at the "
                "top - follow it. view: 'full' (default), 'brief' (recent DONE slice "
                "only, for tight context), or 'detailed' (adds full verbatim update "
                "history, notes, and docs - use for a complete handoff or a plan)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name. Omit to auto-resolve from repo_path.",
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Current workspace root. Resolves the project (if project omitted) or detects a mismatch (if project given).",
                    },
                    "view": {
                        "type": "string",
                        "enum": ["full", "brief", "detailed"],
                        "description": "full (default), brief (recent DONE slice), or detailed (adds full verbatim history + notes + docs).",
                    },
                },
            },
            annotations=ToolAnnotations(readOnlyHint=True),
        ),
        Tool(
            name="ctx_update",
            description=(
                "Persist what happened so the next tool or session can pick up smoothly. "
                "Merges session_summary into the WHAT/DONE/NOW/MAP buckets. Call it when "
                "you finish a task, hit a milestone, or before the user switches tools. "
                "Pass repo_path (workspace root) - mismatched updates are rejected, which "
                "prevents cross-project mixing. OMIT project to target the project linked "
                "to repo_path. Optional: author=<name> records this as a verbatim "
                "user-authored note; files=[...] registers important files into MAP; an "
                "empty session_summary with a repo_path just links/creates the project. "
                "For anything that must survive word-for-word (a plan, rules), use ctx_doc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name. Omit to target the project linked to repo_path.",
                    },
                    "session_summary": {
                        "type": "string",
                        "description": "What changed, what's done, what's next, key files. Empty + repo_path = link/create only.",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Name of the AI tool sending this (e.g. 'claude-code', 'antigravity', 'codex').",
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the project's repo. Set once, stored permanently; also links the project.",
                    },
                    "author": {
                        "type": "string",
                        "description": "If set, records session_summary as a verbatim user note from this author.",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Important files to register in MAP, e.g. 'src/main.py - entry point'.",
                    },
                },
                "required": ["tool_name"],
            },
        ),
        Tool(
            name="ctx_doc",
            description=(
                "Read or write a VERBATIM per-project document, stored and returned "
                "exactly as written (no merging or summarizing, unlike the "
                "WHAT/DONE/NOW/MAP buckets). Use this for content that must survive "
                "word-for-word. Common kinds: 'instructions' (project rules the "
                "assistant must follow - surfaced at the top of ctx_get), 'plan' (an "
                "implementation plan), 'context' (a full detailed brief), 'handoff' (a "
                "fuller snapshot when switching tools). Kind is free-form. Provide "
                "'content' to save; omit it to read. action: 'get' (default), 'set', "
                "'list' (index of a project's docs), or 'delete'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name."},
                    "kind": {
                        "type": "string",
                        "description": "Document kind, e.g. instructions, plan, context, handoff. Required except for action='list'.",
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
                    "tool_name": {
                        "type": "string",
                        "description": "Name of the AI tool writing this doc; recorded as updated_by.",
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="ctx_search",
            description=(
                "Search across projects' context, update history, user notes, and bugs. "
                "Pass an optional 'project' to limit the search to just that project."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to search for."},
                    "project": {
                        "type": "string",
                        "description": "Optional: limit results to this project.",
                    },
                },
                "required": ["query"],
            },
            annotations=ToolAnnotations(readOnlyHint=True),
        ),
        Tool(
            name="how_to_ctx",
            description=(
                "Return the usage guide for one-context: the recommended workflow "
                "(resolve -> get -> work -> update), the save/load handoff pattern for "
                "switching tools, and which tool to use for what. Call this if you are "
                "unsure how to use these context tools correctly."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(readOnlyHint=True),
        ),
    ]


@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Route tool calls to the appropriate handler."""

    def _reply(payload: dict) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    try:
        if name == "ctx_get":
            project = arguments.get("project")
            repo_path = arguments.get("repo_path")

            # Resolve the project from the folder if not named (folds ctx_resolve).
            if not project:
                if not repo_path:
                    return _reply({"error": "Pass 'project', or 'repo_path' to resolve it from the folder."})
                owner = find_project_by_repo_path(repo_path)
                if not owner:
                    return _reply({
                        "error": "No project is linked to this folder.",
                        "requested_repo_path": repo_path,
                        "hint": "Link it once with ctx_update(project=..., repo_path=...).",
                    })
                project = owner["project"]

            result = get_project(project)
            if "error" not in result:
                # Mismatch is a soft warning (folds ctx_strict_get); heed it.
                guard = _repo_guard(project, result.get("repo_path", ""), repo_path)
                if guard:
                    result["safety"] = guard
                result = await _with_git_info(result)
                result["bugs"] = list_bugs(project, status="open")
                result["bugs_fixed_count"] = count_bugs(project, "fixed")

                # Surface verbatim docs: the 'instructions' doc is returned in
                # full at the top; other docs appear as an index unless detailed.
                docs_index = list_docs(project)
                view = arguments.get("view")
                if docs_index:
                    result["docs"] = docs_index
                if any(d["kind"] == "instructions" for d in docs_index):
                    doc = get_doc(project, "instructions")
                    if "error" not in doc:
                        result["instructions"] = doc["content"]

                if view == "brief":
                    done = result.get("done", "")
                    brief_cap = 2000
                    if len(done) > brief_cap:
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
                        result["hint"] = "DONE truncated in brief view; use view='detailed' or ctx_search for older entries."
                elif view == "detailed":
                    # Complete handoff: buckets + full verbatim history + notes +
                    # docs. Bounded by a char budget so a huge project can't blow
                    # up the caller's context.
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
                        result["hint"] = "Older updates omitted; ask again with a narrower need."
                    result["docs"] = [
                        {**d, "content": get_doc(project, d["kind"]).get("content", "")}
                        for d in docs_index
                    ]
            return _reply(result)

        elif name == "ctx_update":
            project = arguments.get("project")
            repo_path = arguments.get("repo_path")
            tool_name = arguments.get("tool_name", "unknown")
            session_summary = (arguments.get("session_summary") or "").strip()
            author = arguments.get("author")
            files = arguments.get("files")

            # Resolve the project from the folder if not named.
            if not project and repo_path:
                owner = find_project_by_repo_path(repo_path)
                if owner:
                    project = owner["project"]
            if not project:
                return _reply({"error": "Pass 'project', or a 'repo_path' already linked to one."})

            current = get_project(project)
            if "error" in current:
                guard = _auto_init_guard(project, repo_path)
                if guard:
                    return _reply(guard)
                logger.info(f"Auto-initializing project '{project}'")
                init_project(project, repo_path=repo_path or "")
            elif repo_path and not current.get("repo_path"):
                # Link a repo_path to an as-yet-unlinked project (folds
                # ctx_link) - but never claim a folder that already belongs
                # to another project.
                guard = _auto_init_guard(project, repo_path)
                if guard:
                    return _reply(guard)
                init_project(project, repo_path=repo_path)
            else:
                guard = _repo_guard(project, current.get("repo_path", ""), repo_path, strict=True)
                if guard and "error" in guard:
                    return _reply(guard)

            note_saved = None
            did: dict = {}

            # session_summary -> buckets (as a user note if author is set; folds ctx_note).
            if session_summary:
                if author:
                    note_saved = add_project_message(project, session_summary, author=author)
                    merge_summary = f"User note from {author}: {session_summary}"
                    merge_tool = f"user:{author}"
                else:
                    merge_summary = session_summary
                    merge_tool = tool_name

                def _merge(cur: dict) -> dict:
                    return merge_context(
                        current_what=cur.get("what", ""),
                        current_done=cur.get("done", ""),
                        current_now=cur.get("now", ""),
                        current_map=cur.get("map", ""),
                        session_summary=merge_summary,
                        tool_name=merge_tool,
                        repo_path=repo_path or cur.get("repo_path", ""),
                    )

                atomic_merge_update(
                    name=project, merge_fn=_merge, tool_name=merge_tool,
                    summary=merge_summary, repo_path=repo_path,
                )

            # files -> MAP (folds ctx_map). Atomic read-merge-write so two
            # tools registering files at the same time can't lose entries.
            if files:
                new_entries = "\n".join(f"- {f}" for f in files)
                merge_project_map(project, new_entries)
                did["files_registered"] = len(files)

            result = get_project(project)
            if note_saved is not None:
                result["note"] = note_saved
                result["note_saved"] = "error" not in note_saved
            result.update(did)
            if not session_summary and not files:
                result["status"] = "linked" if repo_path else "no-op"
            return _reply(result)

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
            return _reply(result)

        elif name == "ctx_search":
            query = arguments["query"]
            scope = arguments.get("project")
            # Scope in SQL, not by post-filtering: filtering after a global
            # LIMIT would silently drop scoped matches that fall outside the
            # top-N across all projects.
            projects = search_projects(query, project=scope)
            history = search_logs(query, project=scope)
            messages = search_messages(query, project=scope)
            bugs = search_bugs(query, project=scope)
            result = {
                "query": query,
                "projects": projects,
                "history": history,
                "messages": messages,
                "bugs": bugs,
                "total_matches": len(projects) + len(history) + len(messages) + len(bugs),
            }
            if scope:
                result["scope"] = scope
            return _reply(result)

        elif name == "how_to_ctx":
            return [TextContent(type="text", text=HOW_TO_CTX)]

        else:
            return _reply({"error": f"Unknown tool: {name}"})

    except Exception as e:
        logger.exception(f"Error handling tool {name}")
        return _reply({"error": str(e)})


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
                "ctx_get", "ctx_update", "ctx_doc", "ctx_search", "how_to_ctx",
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
