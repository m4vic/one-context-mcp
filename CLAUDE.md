# one-context-mcp (PyPI: one-ctx)

Local MCP server giving Claude/Cline/Codex/etc. shared per-project context via one SQLite DB (`~/.ctx/ctx.db`, override with `CTX_DB_PATH`).

## Commands

- Test: `pytest tests/ -q` (in-process, fresh temp DB per test, ~3s)
- Build: `python -m build` then `twine check dist/*`
- Run stdio server: `python -m ctx.cli stdio`
- Run HTTP server: `python -m ctx.cli serve` (localhost:7337; set `CTX_AUTH_TOKEN` before exposing)
- CLI: `python -m ctx.cli {status,init,list,search,reset,delete,export,import}`

## Architecture

- `ctx/server.py` — MCP tool declarations + handlers (`handle_call_tool`), repo-path guards, bare ASGI app for HTTP/SSE with optional bearer auth
- `ctx/database.py` — SQLite layer. Tables: projects (WHAT/DONE/NOW/MAP buckets + repo_path), update_log, project_messages, bugs. All read-merge-write goes through `atomic_merge_update` (BEGIN IMMEDIATE — do NOT bypass it, concurrent tools write here). Size caps via `_apply_caps`
- `ctx/llm.py` — merge strategies. Default is `_local_merge` (rule-based, zero network). Ollama/Anthropic/OpenAI/Gemini opt-in via `CTX_MERGE_MODE`; all fall back to local
- `ctx/cli.py` — Click CLI; Windows needs the selector event loop policy at top
- `ctx/git.py` — subprocess git info, returns None gracefully

## Invariants

- Local merge stays the default: no model calls, no API keys on the default path
- Any bucket write must apply `normalize_map_content` (MAP dedup) and the char caps
- New auto-init paths must call `_auto_init_guard` (prevents typo-split projects)
- Tests are OS-agnostic (CI = Ubuntu + Windows); build path assertions with `os.name` branches
- Never commit or reuse a PyPI version number; user runs `twine upload` and `git push` themselves

## Release checklist

1. Bump version in `ctx/__init__.py` AND `pyproject.toml` (keep in sync)
2. `pytest tests/ -q` green
3. Update README.md + INSTALLATION_GUIDE.md (tool tables, version pins)
4. Commit, tag `vX.Y.Z`, `python -m build`, `twine check dist/*`
5. User: `git push origin main --tags` and `twine upload dist/one_ctx-X.Y.Z*`
