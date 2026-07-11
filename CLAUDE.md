# one-context-mcp (PyPI: one-ctx)

Local MCP server giving Claude/Cline/Codex/etc. shared per-project context via one SQLite DB (`~/.ctx/ctx.db`, override with `CTX_DB_PATH`).

## Commands

- Test: `pytest tests/ -q` (in-process, fresh temp DB per test, ~3s)
- Build: `python -m build` then `twine check dist/*`
- Run stdio server: `python -m ctx.cli stdio`
- Run HTTP server: `python -m ctx.cli serve` (localhost:7337; set `CTX_AUTH_TOKEN` before exposing)
- CLI: `python -m ctx.cli {status,init,list,search,reset,delete,export,import,doc}`

## Architecture

- **Model-facing MCP surface is 5 tools** (since 0.6.0): `ctx_get`, `ctx_update`, `ctx_doc`, `ctx_search`, `how_to_ctx`. resolve/strict/link/note/map/history are FOLDED into params of get+update (e.g. omit `project` on `ctx_get` to resolve from `repo_path`; `author=`/`files=` on `ctx_update` are the old note/map). export/import/reset/list are CLI-only; the DB functions still exist. Do NOT re-add tools — the point is a minimal, hang-free surface.
- `ctx/server.py` — MCP tool declarations + handlers (`handle_call_tool`), repo-path guards, bare ASGI app for HTTP/SSE with optional bearer auth. `HOW_TO_CTX` guide + `SERVER_INSTRUCTIONS` (passed to `Server(instructions=...)` so capable clients auto-load a usage pointer). Git status is fetched via `asyncio.to_thread` — NEVER call blocking subprocess/IO directly on the event loop (that was the 0.5.2 hang)
- `ctx/database.py` — SQLite layer. Tables: projects (WHAT/DONE/NOW/MAP buckets + repo_path), update_log, project_messages, bugs, project_docs (verbatim per-project docs: plan/instructions/context, keyed by (project,kind)). Buckets go through `atomic_merge_update` (BEGIN IMMEDIATE — do NOT bypass it, concurrent tools write here) + `_apply_caps`; docs are verbatim via `set_doc`/`get_doc` (cap `CTX_MAX_DOC_CHARS`, no merge)
- `ctx/llm.py` — merge strategies. Default is `_local_merge` (rule-based, zero network). Ollama/Anthropic/OpenAI/Gemini opt-in via `CTX_MERGE_MODE`; all fall back to local
- `ctx/cli.py` — Click CLI; Windows needs the selector event loop policy at top
- `ctx/git.py` — subprocess git info, returns None gracefully

## Invariants

- Local merge stays the default: no model calls, no API keys on the default path
- Any bucket write must apply `normalize_map_content` (MAP dedup) and the char caps
- New auto-init paths must call `_auto_init_guard` (prevents typo-split projects)
- No blocking I/O on the async event loop, ever (subprocess/network → `asyncio.to_thread`); degrade gracefully, never hang
- Tests are OS-agnostic (CI = Ubuntu + Windows); build path assertions with `os.name` branches
- Never commit or reuse a PyPI version number; user runs `twine upload` and `git push` themselves

## Release checklist

1. Bump version in `ctx/__init__.py` AND `pyproject.toml` (keep in sync)
2. `pytest tests/ -q` green
3. Update README.md + INSTALLATION_GUIDE.md (tool tables, version pins)
4. Commit, tag `vX.Y.Z`, `python -m build`, `twine check dist/*`
5. User: `git push origin main --tags` and `twine upload dist/one_ctx-X.Y.Z*`
