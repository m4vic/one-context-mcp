# one-context-mcp

[![PyPI Downloads](https://static.pepy.tech/personalized-badge/one-ctx?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/one-ctx)


> One local MCP server that gives Claude, Cline, Codex, and other AI tools the same project memory.

`one-context-mcp` stops the repeated setup explanation every time you switch AI tools. It stores project context locally in a small SQLite database and exposes it through MCP tools that every connected assistant can read and update.

- PyPI package: `one-ctx`
- Main CLI command: `one-context`
- Alternate CLI commands: `ctx`, `one-ctx`
- GitHub repo: `m4vic/one-context-mcp`
- Default merge mode: local rules only, no model and no API key

---

## Quick Install

Add this to Claude Desktop, Cline, Codex, or any MCP client:

```json
{
  "mcpServers": {
    "one-context": {
      "command": "uvx",
      "args": ["--from", "one-ctx", "one-context", "stdio"]
    }
  }
}
```

Then fully restart the MCP client.

Full setup instructions are in [INSTALLATION_GUIDE.md](INSTALLATION_GUIDE.md).

---

## Why It Exists

Every coding assistant has its own short-term context. When you move from Claude to Cline, from Cline to Codex, or from one IDE session to another, you usually explain the same project again:

> This is a FastAPI backend, PostgreSQL is the database, `src/main.py` is the entry point, we just changed auth, and next we need rate limiting.

`one-context-mcp` makes that explanation persistent and shared.

---

## Architecture

```mermaid
graph TD
    classDef rootNode fill:#0d1117,stroke:#58a6ff,stroke-width:3px,color:#c9d1d9,font-size:16px,font-weight:bold;
    classDef aiNode fill:#161b22,stroke:#3fb950,stroke-width:2px,color:#c9d1d9,font-size:14px;
    classDef bucketNode fill:#21262d,stroke:#8b949e,stroke-dasharray: 5 5,color:#c9d1d9;
    classDef localNode fill:#1f2937,stroke:#f59e0b,stroke-width:2px,color:#f9fafb;

    A([one-context MCP Server]):::rootNode
    DB[(Local SQLite DB)]:::localNode

    C1[Claude Desktop]:::aiNode
    C2[Cline / VS Code]:::aiNode
    C3[Codex]:::aiNode
    C4[Other MCP Clients]:::aiNode

    B1[(WHAT\nProject scope)]:::bucketNode
    B2[(DONE\nHistory and decisions)]:::bucketNode
    B3[(NOW\nCurrent work)]:::bucketNode
    B4[(MAP\nImportant files)]:::bucketNode
    B5[(NOTES\nUser messages)]:::bucketNode

    C1 <-->|ctx_get / ctx_update| A
    C2 <-->|ctx_get / ctx_update| A
    C3 <-->|ctx_get / ctx_update| A
    C4 <-->|MCP tools| A

    A --- DB
    DB --- B1
    DB --- B2
    DB --- B3
    DB --- B4
    DB --- B5
```

Everything is local by default. No account, no cloud service, no vector database, and no LLM API call is required.

---

## First Use

Use a stable project name. For example, use `asrt` every time you refer to the ASRT project.

Ask your assistant:

```text
Use one-context. Link project asrt to F:\ASRT, then load context.
```

At the end of a work session:

```text
Update one-context for project asrt with what we changed, what is done, what is next, and important files.
```

You do not edit MCP config when switching work folders. Link each project once with `ctx_link(project, repo_path)`.

---

## What Gets Stored

| Bucket | Purpose | Example |
|--------|---------|---------|
| `WHAT` | Project identity, stack, architecture, constraints | FastAPI backend with PostgreSQL and async SQLAlchemy |
| `DONE` | Completed work, decisions, solved issues | JWT auth implemented, UUID user IDs chosen |
| `NOW` | Current task and next steps | Working on rate limiting middleware |
| `MAP` | Important files and what they do | `src/auth.py` - auth middleware |
| `BUGS` | Known bugs, open or fixed | Race condition in concurrent writes - fixed |
| `NOTES` | User-authored project messages | Remember to keep ASRT context strict |

MAP entries are normalized and deduplicated. When a project is linked to a `repo_path`, file tracking is scoped to that repo so context from different projects does not get mixed.

---

## MCP Tools

| Tool | Purpose |
|------|---------|
| `ctx_get(project)` | Load WHAT, DONE, NOW, MAP, repo path, and git info |
| `ctx_strict_get(project, repo_path)` | Load context only when the current workspace path matches the linked project |
| `ctx_update(project, session_summary, tool_name)` | Merge a session summary into project context |
| `ctx_map(project, files, replace)` | Register important files manually |
| `ctx_note(project, message, author, merge)` | Store a user-authored note for one project |
| `ctx_history(project, limit)` | Show recent updates and user notes for one project |
| `ctx_link(project, repo_path)` | Create/link a project to a workspace root for strict file scoping |
| `ctx_resolve(repo_path)` | Reverse lookup: which project is linked to this folder (works from subfolders too) |
| `ctx_bug(project, description?, bug_id?, status?)` | Add a bug, mark one fixed, or list a project's bugs |
| `ctx_export(project?, format?)` | Export context as JSON (re-importable) or Markdown; omit project for all |
| `ctx_import(data, mode?)` | Restore/merge context from a ctx_export JSON |
| `ctx_search(query)` | Search all projects, update history, user notes, and bugs |
| `ctx_reset(project)` | Clear one project's context, history, notes, and bugs |
| `ctx_list()` | List tracked projects with their linked folders |

`ctx_get` also accepts `view: "brief"` to return only the recent slice of DONE when the calling session is low on context.

---

## CLI Reference

```bash
ctx status [project]          # View current context or list projects
ctx init <project> --path .   # Create/link project to repo path
ctx search <query>            # Search across projects and history
ctx export <project> -o f.json   # Backup one project (--all for everything, --format md for readable)
ctx import f.json             # Restore/merge an exported backup
ctx reset <project>           # Clear a project's context
ctx delete <project>          # Permanently delete a project
ctx list                      # List all projects
ctx serve --port 7337         # Start HTTP/SSE server (localhost only by default)
ctx stdio                     # Start stdio MCP server
```

---

## Backup & Portability

Your entire context lives in one SQLite file (`~/.ctx/ctx.db`). To back it up, commit it to a repo, or move machines:

```bash
ctx export myproject -o context-backup.json   # per-project backup
ctx export --all -o all-context.json          # everything
ctx import context-backup.json                # merge into existing (safe, idempotent)
ctx import context-backup.json --mode replace # overwrite buckets
```

Any MCP client can do the same via the `ctx_export` / `ctx_import` tools.

---

## Security

- **stdio mode (the default setup) has no network surface.**
- `ctx serve` binds **localhost only** by default (changed in 0.4.0; previously `0.0.0.0`).
- To expose HTTP mode on a network, set a token first: `CTX_AUTH_TOKEN=<secret> ctx serve --host 0.0.0.0`. Clients must then send `Authorization: Bearer <secret>`. Without the token the server warns loudly.
- With auth enabled, `/health` hides the tool list and CORS is restricted to localhost origins.

---

## Documentation

- [Installation Guide](INSTALLATION_GUIDE.md)
- [PyPI package](https://pypi.org/project/one-ctx/)
- [GitHub Releases](https://github.com/m4vic/one-context-mcp/releases)

---

## License

MIT

---

Built to end context amnesia across AI tools.
