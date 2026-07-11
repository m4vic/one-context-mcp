# Installation Guide

This guide shows how to install `one-context-mcp` and connect it to Claude Desktop, Cline, Codex, or any MCP client.

## Package Names

- GitHub repository: `m4vic/one-context-mcp`
- PyPI package: `one-ctx`
- Main command: `one-context`
- Alternate commands: `ctx`, `one-ctx`

For normal users, the recommended setup is `uvx`. It downloads and runs the latest PyPI package automatically.

---

## Recommended MCP Config

Use this for Claude Desktop, Cline, Codex, and most MCP clients:

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

After changing MCP config, fully restart the client.

---

## Claude Desktop

Config file locations:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add or merge this block:

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

Then fully quit Claude Desktop and reopen it.

---

## Cline / VS Code

Open Cline MCP settings and add:

```json
{
  "mcpServers": {
    "one-context": {
      "command": "uvx",
      "args": ["--from", "one-ctx", "one-context", "stdio"],
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

Reload VS Code after changing the file.

---

## Codex

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.one-context]
command = "uvx"
args = ["--from", "one-ctx", "one-context", "stdio"]
```

Start a fresh Codex session after changing MCP config.

---

## Manual pip Install

If you do not want to use `uvx`:

```bash
pip install -U one-ctx
one-context stdio
```

These commands are equivalent:

```bash
one-context stdio
one-ctx stdio
ctx stdio
```

Install a specific version:

```bash
pip install one-ctx==0.6.0
```

---

## Run From Source

This is for package development only. Normal users should use the `uvx` config above.

```bash
git clone https://github.com/m4vic/one-context-mcp.git
cd one-context-mcp
pip install -e .
one-context stdio
```

Local source MCP config:

```json
{
  "mcpServers": {
    "one-context": {
      "command": "python",
      "args": ["-m", "ctx.cli", "stdio"],
      "cwd": "C:\\path\\to\\one-context-mcp"
    }
  }
}
```

Replace `C:\\path\\to\\one-context-mcp` with the folder where you cloned this repo.

Do not change this `cwd` when switching between your own projects. Pass `repo_path` to `ctx_update` (or run `ctx init <project> --path .`) to link project folders.

---

## First Use

New to the tool? Send this first — the assistant reads the built-in guide and learns the whole workflow:

```text
Call how_to_ctx() and follow that guide for this and every session.
```

Use a stable project name. For example, use `asrt` every time you refer to the ASRT project.

Ask your assistant:

```text
Use one-context. Link project asrt to F:\ASRT, then load context.
```

At the end of a work session:

```text
Update one-context for project asrt with what we changed, what is done, what is next, and important files.
```

To add a direct user note:

```text
Add a one-context note for project asrt: Keep SafetyDiff as priority after MVP.
```

For safe reads, always pass the workspace path — a mismatch is flagged:

```text
Load one-context for the current folder F:\ASRT.
```

`ctx_get(repo_path=...)` resolves the project from the folder and returns a `safety` warning if the folder does not match the linked project.

---

## Project Switching

You do not edit MCP config when switching work folders.

- MCP config starts the server.
- `project` chooses the context namespace.
- A project is bound to a folder the first time you pass `repo_path` to `ctx_update` (or via `ctx init <project> --path .`).

Example (each links itself on first update):

```text
ctx_update(asrt, repo_path=F:\ASRT, ...)
ctx_update(other-project, repo_path=F:\other-project, ...)
```

Then any client can load either project — by name or by folder:

```text
ctx_get(asrt)
ctx_get(other-project)
```

---

## Verify Installation

Check the command:

```bash
uvx --from one-ctx one-context --help
```

Check MCP stdio startup:

```bash
uvx --from one-ctx one-context stdio
```

For a local source checkout:

```bash
python -m compileall ctx
python -m build
python -m twine check dist/*
```

---

## HTTP/SSE Mode

Most local clients should use stdio. HTTP mode is for advanced or network setups.

```bash
ctx serve --port 7337
```

Then connect MCP clients to:

```text
http://127.0.0.1:7337/sse
```

Since 0.4.0 the server binds localhost only by default. To expose it on a
network, set an auth token first - the server warns if you skip this:

```bash
CTX_AUTH_TOKEN=your-secret ctx serve --host 0.0.0.0
```

Clients must then send an `Authorization: Bearer your-secret` header.

---

## Backup and Restore

All context lives in one SQLite file (`~/.ctx/ctx.db`). Export it to JSON to
back it up, commit it to a repo, or move to another machine:

```bash
ctx export myproject -o backup.json     # one project
ctx export --all -o all-context.json    # everything
ctx export myproject --format md        # human-readable Markdown
ctx import backup.json                  # merge (safe, idempotent)
ctx import backup.json --mode replace   # overwrite buckets
```

Backup/restore is a CLI operation (since 0.6.0 it is not part of the MCP tool surface).

---

## Merge Modes

Local merge is the default and never calls a model or an API. This is the recommended setting for normal use.

| Mode | Enable | Uses model/API? |
|------|--------|-----------------|
| Local | Default or `CTX_MERGE_MODE=local` | No |
| Auto | `CTX_MERGE_MODE=auto` | Tries configured providers, falls back to local |
| Ollama | `CTX_MERGE_MODE=ollama` | Local model. If `CTX_OLLAMA_MODEL` is unset, the first model you have pulled locally is used automatically. |
| Anthropic | `CTX_MERGE_MODE=anthropic` and `ANTHROPIC_API_KEY=...` | Yes (cloud) |
| OpenAI compatible | `CTX_MERGE_MODE=openai` and `OPENAI_API_KEY=...` | Yes (cloud) |
| Gemini | `CTX_MERGE_MODE=gemini` and `GEMINI_API_KEY=...` | Yes (cloud) |

You do not need any API key for normal use. Every mode falls back to local merge automatically if the configured provider is unreachable or misconfigured.

---

## Troubleshooting

### MCP client does not show tools

1. Confirm the config has a top-level `mcpServers` object.
2. Use the exact `uvx` command shown above.
3. Fully restart Claude, Cline, Codex, or the MCP client.
4. Test the command manually:

```bash
uvx --from one-ctx one-context --help
```

### Local source works but PyPI does not

Use:

```bash
pip install -U one-ctx
```

Then restart the MCP client. MCP clients often cache tool lists until a fresh session.

### Project context is mixed

Always pass `repo_path` (the workspace root) so each project stays bound to its
folder, and keep the same project name in every tool.

Example:

```text
Update one-context for project asrt with repo_path F:\ASRT.
```

The server enforces this: a folder linked to one project cannot silently create
or update a different project. Assistants resolve the right project by calling
`ctx_get(repo_path=...)` (which returns a `safety` warning on a folder mismatch)
instead of guessing the name.

### Stuck "loading" on Windows with multiple Pythons

If your MCP config uses a bare `python` command and you have more than one
Python installed (python.org, Microsoft Store stub, multiple versions), the
client may spawn an interpreter that does not have `one-ctx` installed and
hang silently. Pin the full path in your MCP config:

```json
{
  "mcpServers": {
    "one-context": {
      "command": "C:\\Program Files\\Python310\\python.exe",
      "args": ["-m", "ctx.cli", "stdio"],
      "env": { "PYTHONUNBUFFERED": "1" }
    }
  }
}
```

The `uvx` setup avoids this problem entirely.

### `uvx` is not found

Install `uv` first:

```bash
pip install uv
```

Then retry:

```bash
uvx --from one-ctx one-context --help
```

---

## Release Downloads

Latest PyPI release:

```bash
pip install -U one-ctx
```

Specific version:

```bash
pip install one-ctx==0.6.0
```

Install directly from GitHub:

```bash
pip install git+https://github.com/m4vic/one-context-mcp.git
```

GitHub Releases:

```text
https://github.com/m4vic/one-context-mcp/releases
```
