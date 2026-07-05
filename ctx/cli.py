import sys
if sys.platform == 'win32':
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import anyio
import click
from ctx.database import (
    init_project, get_project, reset_project, list_projects,
    search_projects, search_logs,
)
from ctx.server import mcp_server
from ctx.git import get_git_summary
import mcp.server.stdio

@click.group()
def cli():
    """ctx -- Combined Context MCP Server."""
    pass

@cli.command()
@click.option("--port", default=7337, help="Port to run the server on")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Host to bind to. Use 0.0.0.0 to expose on the network "
                   "(set CTX_AUTH_TOKEN if you do).")
def serve(port, host):
    """Start the HTTP/SSE server (binds to localhost only by default)."""
    import os
    import uvicorn
    auth_enabled = bool(os.environ.get("CTX_AUTH_TOKEN"))
    print(f"[ctx] server starting on http://{host}:{port}")
    print(f"   SSE endpoint:     http://localhost:{port}/sse")
    print(f"   Messages endpoint: http://localhost:{port}/messages/")
    print(f"   Health check:      http://localhost:{port}/health")
    print(f"   Auth:              {'Bearer token (CTX_AUTH_TOKEN)' if auth_enabled else 'disabled'}\n")
    if host not in ("127.0.0.1", "localhost") and not auth_enabled:
        print("[!] WARNING: binding beyond localhost without CTX_AUTH_TOKEN -")
        print("    anyone on the network can read and write your context DB.\n")
    print("Add to your MCP config:")
    print(f'   {{"mcpServers": {{"ctx": {{"url": "http://localhost:{port}/sse"}}}}}}\n')

    uvicorn.run("ctx.server:app", host=host, port=port, log_level="info")

@cli.command()
def stdio():
    """Start the stdio server (for direct VS Code / Cline / Codex integration)."""
    async def run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options()
            )
    anyio.run(run)

@cli.command()
@click.argument("project")
@click.option("--path", default="", help="Path to the project's git repo on disk")
def init(project, path):
    """Initialize a new project context."""
    result = init_project(project, repo_path=path)
    if "error" in result:
        print(f"[!] {result['error']}")
        return
    print(f"[OK] Project '{project}' initialized.")
    if path:
        print(f"   Git repo linked: {path}")
    print("   Start any AI tool and call ctx_get to load context.")

@cli.command()
@click.argument("project", required=False)
def status(project):
    """View the context for a project, or list all projects."""
    if not project:
        projects = list_projects()
        if not projects:
            print("No projects found.")
            return
        print("=== Tracked Projects ===")
        for p in projects:
            print(f"- {p['project']} (last updated: {p['updated_at']})")
        return

    data = get_project(project)
    if "error" in data:
        print(f"Error: {data['error']}")
        return

    print(f"=== {project} ===")
    print(f"\n[WHAT]\n{data.get('what', '(empty)') or '(empty)'}")
    print(f"\n[DONE]\n{data.get('done', '(empty)') or '(empty)'}")
    print(f"\n[NOW]\n{data.get('now', '(empty)') or '(empty)'}")
    print(f"\n[MAP]\n{data.get('map', '(empty)') or '(no files mapped)'}")

    # Show git info if repo_path is set
    repo_path = data.get("repo_path", "")
    if repo_path:
        print(f"\n[GIT] repo: {repo_path}")
        git_info = get_git_summary(repo_path)
        if git_info:
            print(f"   Branch: {git_info['branch']}")
            if git_info.get("recent_commits"):
                print(f"   Recent commits:")
                for c in git_info["recent_commits"][:3]:
                    print(f"     {c['hash']} {c['message']}")
            changed = git_info.get("changed_files", {})
            total_changes = len(changed.get("staged", [])) + len(changed.get("unstaged", [])) + len(changed.get("untracked", []))
            if total_changes:
                print(f"   Changed files: {total_changes}")
        else:
            print("   (git not available or not a repo)")

@cli.command()
@click.argument("project")
def reset(project):
    """Reset a project's context back to empty."""
    reset_project(project)
    print(f"[OK] Project '{project}' has been reset to empty.")

@cli.command(name="list")
def list_cmd():
    """List all tracked projects."""
    projects = list_projects()
    if not projects:
        print("No projects found.")
        return
    print("=== Tracked Projects ===")
    for p in projects:
        print(f"- {p['project']} (last updated: {p['updated_at']})")

@cli.command()
@click.argument("project")
def delete(project):
    """Permanently delete a project."""
    from ctx.database import delete_project
    result = delete_project(project)
    if "error" in result:
        print(f"[!] {result['error']}")
    else:
        print(f"[OK] Project '{project}' deleted.")

@cli.command()
@click.argument("project", required=False)
@click.option("--all", "export_all_flag", is_flag=True, help="Export every project")
@click.option("-o", "--output", type=click.Path(), default=None, help="Write to file instead of stdout")
@click.option("--format", "fmt", type=click.Choice(["json", "md"]), default="json", show_default=True)
def export(project, export_all_flag, output, fmt):
    """Export project context (backup / commit to repo / move machines)."""
    import json as _json
    from ctx.database import export_project, export_all, render_project_markdown

    if not project and not export_all_flag:
        print("[!] Give a project name or --all.")
        return
    data = export_all() if export_all_flag else export_project(project)
    if "error" in data:
        print(f"[!] {data['error']}")
        return

    if fmt == "md":
        if "projects" in data:
            text = "\n---\n\n".join(render_project_markdown(p) for p in data["projects"])
        else:
            text = render_project_markdown(data)
    else:
        text = _json.dumps(data, indent=2)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[OK] Exported to {output}")
    else:
        print(text)


@cli.command(name="import")
@click.argument("file", type=click.Path(exists=True))
@click.option("--mode", type=click.Choice(["merge", "replace"]), default="merge", show_default=True)
def import_cmd(file, mode):
    """Import context from a ctx export JSON file."""
    import json as _json
    from ctx.database import import_project

    with open(file, encoding="utf-8") as f:
        try:
            payload = _json.load(f)
        except _json.JSONDecodeError as e:
            print(f"[!] Not valid JSON: {e}")
            return

    projects = payload["projects"] if isinstance(payload, dict) and "projects" in payload else [payload]
    for p in projects:
        result = import_project(p, mode=mode)
        if "error" in result:
            print(f"[!] {result['error']}")
        else:
            stats = result.get("imported", {})
            print(f"[OK] Imported '{result['project']}' ({mode}): "
                  f"+{stats.get('updates', 0)} updates, +{stats.get('messages', 0)} notes, +{stats.get('bugs', 0)} bugs")


@cli.command()
@click.argument("query")
def search(query):
    """Search across all projects' context and history."""
    project_matches = search_projects(query)
    log_matches = search_logs(query)

    if not project_matches and not log_matches:
        print(f"No results found for '{query}'.")
        return

    if project_matches:
        print(f"=== Projects matching '{query}' ===")
        for m in project_matches:
            buckets = ", ".join(m["matched_buckets"])
            print(f"- {m['project']} (found in: {buckets})")

    if log_matches:
        print(f"\n=== History matching '{query}' ===")
        for m in log_matches:
            print(f"- [{m['project']}] [{m['tool_name']} @ {m['timestamp'][:16]}] {m['summary'][:100]}")

    print(f"\nTotal: {len(project_matches)} projects, {len(log_matches)} history entries")

if __name__ == "__main__":
    cli()
