import sys
if sys.platform == 'win32':
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import sys
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
@click.option("--host", default="0.0.0.0", help="Host to bind to")
def serve(port, host):
    """Start the HTTP/SSE server."""
    import uvicorn
    print(f"[ctx] server starting on http://{host}:{port}")
    print(f"   SSE endpoint:     http://localhost:{port}/sse")
    print(f"   Messages endpoint: http://localhost:{port}/messages/")
    print(f"   Health check:      http://localhost:{port}/health\n")
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
