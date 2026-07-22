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

**First prompt** — paste this once and your assistant teaches itself the tool:

```text
Call how_to_ctx() and follow that guide for this and every session.
```

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

You do not edit MCP config when switching work folders. Each project links itself the first time you pass `repo_path` to `ctx_update` (or run `ctx init <project> --path .`).

---

## What Gets Stored

| Bucket | Purpose | Example |
|--------|---------|---------|
| `WHAT` | Project identity, stack, architecture, constraints | FastAPI backend with PostgreSQL and async SQLAlchemy |
| `DONE` | Completed work, decisions, solved issues | JWT auth implemented, UUID user IDs chosen |
| `NOW` | Current task and next steps | Working on rate limiting middleware |
| `MAP` | Important files and what they do | `src/auth.py` - auth middleware |
| `NOTES` | User-authored messages (`ctx_update` with `author=`) | Remember to keep ASRT context strict |
| `DOCS` | Verbatim per-project documents (plan, instructions, context, ...) | Full implementation plan, kept word-for-word |

The buckets (`WHAT`/`DONE`/`NOW`/`MAP`) are **auto-merged and summarized** — great for evolving state, but lossy. **`DOCS` are stored and returned verbatim** — use them for anything that must survive exactly (an implementation plan, project rules, a detailed brief). MAP entries are normalized and deduplicated. When a project is linked to a `repo_path`, file tracking is scoped to that repo so context from different projects does not get mixed.

---

## MCP Tools

**5 focused tools** (since 0.6.0). The job is a smooth, correct context handoff between tools — not a big tool surface.

| Tool | Purpose |
|------|---------|
| `ctx_get(project?, repo_path?, view?)` | Load a project's context (+ git). Omit `project` to auto-resolve it from `repo_path`. `view`: `full` (default), `brief` (recent DONE only), `detailed` (full verbatim history + notes + docs). A mismatched `repo_path` returns a safety warning. |
| `ctx_update(project?, session_summary?, tool_name, repo_path?, author?, files?)` | Persist state (merged into WHAT/DONE/NOW/MAP). `repo_path` also links the project. `author` records a verbatim user note. `files` registers important files. Empty summary + `repo_path` just links/creates the project. |
| `ctx_doc(project, kind, content?, action?)` | Read/write a **verbatim** doc (`plan`, `instructions`, `context`, `handoff`, …) — stored exactly, never summarized. |
| `ctx_search(query, project?)` | Search context, history, notes, and bugs; optional `project` scope. |
| `how_to_ctx()` | Return the usage guide so any assistant learns the workflow. |

### Migrating from ≤0.5.x

The tool surface was slimmed from 16 to 5. Every capability is preserved — folded into a parameter or moved to the CLI:

| Old tool | Now |
|----------|-----|
| `ctx_resolve(path)` | `ctx_get(repo_path=path)` |
| `ctx_strict_get(p, path)` | `ctx_get(p, repo_path=path)` — heed the `safety` warning |
| `ctx_link(p, path)` | `ctx_update(p, repo_path=path)` |
| `ctx_note(p, msg, author)` | `ctx_update(p, session_summary=msg, author=author)` |
| `ctx_map(p, files)` | `ctx_update(p, files=[...])` |
| `ctx_history(p)` | `ctx_get(p, view="detailed")` |
| `ctx_bug(...)` | a note, or `ctx_doc(p, "bugs", ...)` |
| `ctx_export` / `ctx_import` / `ctx_reset` / `ctx_list` | CLI: `ctx export` / `import` / `reset` / `list` |

---

## Per-Project Instructions & Docs

Buckets are auto-summarized. When you need content kept **exactly** — an implementation plan, project rules, a full brief — use `ctx_doc`:

```text
Save the implementation plan:   ctx_doc(project, "plan", "<full plan text>")
Set project rules:              ctx_doc(project, "instructions", "Always keep ASRT internals private. Prefer SafetyDiff work.")
Read it back verbatim:          ctx_doc(project, "plan")
```

The `instructions` doc is special: `ctx_get` returns it at the top of the snapshot, so every tool that loads context sees the project's rules first.

### Make every AI tool self-instruct from ctx

MCP is pull-based — a server can't push rules into a model. The reliable pattern is to store the rules in ctx (one place, shared across tools) and add **one bootstrap line** to each tool's own rules file, so it always fetches them:

> At the start of work, call `ctx_get(repo_path=<workspace path>)` (omit the project — the folder resolves it), and follow the returned `instructions` field as project rules before doing anything else.

Paste that once per tool:

- **Claude Code / Claude Desktop** → your `CLAUDE.md`
- **Cursor** → `.cursor/rules`
- **Cline** → Custom Instructions
- **Codex** → `~/.codex/config`

After that, per-project rules live in ctx via `ctx_doc(project, "instructions", ...)` and propagate to every tool automatically — change them in one place. Clients that support the MCP server-`instructions` capability also receive a short "how to use ctx" pointer at connect time; any assistant can call `how_to_ctx()` for the full guide.

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

Backup and restore are CLI-only since 0.6.0 — the MCP tool surface stays minimal.

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
