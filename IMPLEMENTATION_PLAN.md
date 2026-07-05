# one-context-mcp 0.4.0 — Robustness + Durability Implementation Plan

> Note: this work ships together with the isolation guards as a single
> **0.4.0** release. 0.4.0 was never published, so there is no separate 0.5.0.

## Context

one-context-mcp (PyPI `one-ctx`) is at 0.4.0 locally (isolation guards: `ctx_resolve`, typo-split protection — **tested but uncommitted**). Goal: make it trustworthy enough to be the go-to context-sharing MCP server. What it lacks today:

- **No test suite / no CI** — every release so far was hand-verified in-session; nothing guards `main`.
- **Insecure HTTP mode** — `ctx serve` binds `0.0.0.0` by default with CORS `*` and zero auth (`ctx/cli.py:23`, `ctx/server.py:540-556`). Anyone on the LAN can read/write the context DB.
- **Single point of loss** — `~/.ctx/ctx.db` has no export/backup path; users can't commit context to their repo.
- **Unbounded bucket growth** — only line-count trims (`_trim_to_max_items`, `ctx/llm.py`); one long line defeats it, and `ctx_get` always returns everything.

Scope confirmed with user: **robustness + durability** (tests, CI, security, export/import, size caps). FTS5 and Streamable HTTP deferred. Ships as **0.4.0**.

## Step 0 — Commit 0.4.0 baseline

Commit the already-tested isolation work (database.py, server.py, README.md, version files) before new changes, so 0.4.0 and 0.5.0 are separate commits. Also copy this plan into the repo as `IMPLEMENTATION_PLAN.md` (user asked for it in-repo).

## Step 1 — Test suite (pytest, in-process)

New `tests/` directory. Tools are testable in-process via `ctx.server.handle_call_tool` (async) — no server/transport needed. DB isolation via `CTX_DB_PATH` env var, which `_get_db_path()` (`ctx/database.py:79`) reads on every `get_connection()` call — a `monkeypatch.setenv` + `tmp_path` fixture gives each test a fresh DB.

Files:
- `tests/conftest.py` — `fresh_db` fixture (monkeypatch `CTX_DB_PATH` → tmp_path), `call_tool` async helper wrapping `handle_call_tool` + JSON parse.
- `tests/test_tools.py` — every tool happy-path: link, get, strict_get (match/mismatch), update (merge + MAP extraction), map (append/replace), note (merge on/off), history, search, reset, list, resolve, bug (add/list/fix/invalid-status/missing-id). Codify the scenarios already proven in-session.
- `tests/test_isolation.py` — the 6 scenarios from this session: typo-split blocked on update/note/map, resolve exact/subfolder/case-insensitive/unknown, legit auto-init, list includes repo_path.
- `tests/test_concurrency.py` — port the 40-thread `atomic_merge_update` race test (marker-based, asserts zero lost writes).
- `tests/test_database.py` — `normalize_map_content`, `merge_map_content`, `_parse_map_line` edge cases; bug CRUD; export/import roundtrip (Step 3).
- `tests/test_llm_local.py` — `_local_merge` behaviors (WHAT keywords, NOW extraction, MAP path scoping via `_path_within_repo`), `_coerce_bucket_value` (list→string), `_parse_llm_response` fence stripping; providers return `None` without keys/env (no network).

Tests must be OS-agnostic (path assertions via `os.sep`-aware helpers) — CI runs Ubuntu + Windows.

pyproject: add `[project.optional-dependencies] dev = ["pytest>=8", "pytest-asyncio", "build", "twine"]`. Async tests need `pytest-asyncio` (or wrap with `asyncio.run` in sync tests — simpler: use `asyncio.run` inside sync test functions, zero plugin dependency; decide during implementation, prefer no-plugin approach).

Remove `test_mcp.py` from `.gitignore`? No — leave legacy script ignored; new `tests/` is the canonical suite.

## Step 2 — Security hardening

- `ctx/cli.py` serve command: default `--host` → `127.0.0.1` (breaking change, release-noted). Keep `0.0.0.0` possible explicitly.
- Token auth for HTTP mode: if `CTX_AUTH_TOKEN` env is set, `/sse` and `/messages/*` require `Authorization: Bearer <token>`; return 401 JSON otherwise. Implement in the bare ASGI `app` (`ctx/server.py:559`) before routing. `/health` stays open but drops the tools list when auth is on.
- CORS: when auth token set, echo no wildcard (`access-control-allow-origin` only for localhost origins); without token (localhost-only default bind) keep current behavior.
- stdio mode unaffected (no network surface).
- Tests: 401 without token, 200 with token, health behavior (can test the ASGI `app` callable directly with a minimal scope/receive/send harness — no uvicorn needed).
- After implementation, run `/security-review` skill on the branch and fix findings.

## Step 3 — ctx_export / ctx_import (durability)

DB layer (`ctx/database.py`):
- `export_project(name) -> dict` — full project: buckets, repo_path, timestamps, bugs, messages, update_log (capped at last 200 entries).
- `export_all() -> dict` — `{version, exported_at, projects: [...]}`.
- `import_project(data, mode="merge"|"replace") -> dict` — replace: overwrite buckets + insert bugs/messages missing by content+timestamp; merge: bucket-wise merge via existing `merge_map_content`/append + dedupe (reuse `_deduplicate_lines` from llm.py or keep simple: replace buckets only if empty, always append history). Keep semantics simple and documented: **merge = fill empty buckets + union bugs/messages; replace = clobber project**.

MCP tools (`ctx/server.py`): 
- `ctx_export(project?, format="json"|"markdown")` — no project = all. JSON is canonical (re-importable); markdown is human-readable (buckets as sections, bugs as checklist).
- `ctx_import(data, mode)` — accepts the JSON string produced by export.

CLI (`ctx/cli.py`): `ctx export <project> [-o file] [--format md|json]`, `ctx export --all`, `ctx import <file> [--mode merge|replace]`.

Markdown rendering: single helper `render_project_markdown(data)` shared by tool + CLI.

## Step 4 — Size caps + sliced reads

- `ctx/database.py`: char-based cap helper `trim_bucket(text, max_chars)` trimming oldest whole lines; applied in `atomic_merge_update` and `update_project_map`. Config: `CTX_MAX_BUCKET_CHARS` (default 12000), `CTX_MAX_MAP_CHARS` (default 4000). Keeps existing line-count trims as secondary.
- `ctx_get`: new optional `view` param — `"full"` (default, current behavior) or `"brief"` (WHAT + NOW full, DONE last ~2000 chars with `done_truncated: true` flag + hint to use `ctx_history`/`ctx_search`). Tool description updated so assistants know `brief` exists for tight-context sessions.
- Tests for cap behavior + brief view.

## Step 5 — Docs, version, release

- README.md: add ctx_export/ctx_import/`view` rows to tools table; new **Security** section (localhost default, `CTX_AUTH_TOKEN`, upgrade note for 0.4→0.5 host change); Backup section (export/import examples).
- INSTALLATION_GUIDE.md: same security + backup sections; version pins → 0.5.0; add "bare `python` on multi-Python Windows" troubleshooting entry (found this session).
- `CLAUDE.md` for the repo: build/test commands (`pytest`, `python -m build`), architecture map, release checklist.
- `.github/workflows/ci.yml`: matrix {ubuntu-latest, windows-latest} × {3.10, 3.12}; steps: `pip install -e .[dev]` → `pytest` → `python -m build` → `twine check dist/*`.
- Keep `ctx/__init__.py` + `pyproject.toml` at 0.4.0 (single release; 0.4.0 was never published). Tag `v0.4.0`, build dist. **User runs `twine upload` and `git push` themselves** (established flow).

## Verification

1. `pytest` green locally on Windows (full suite, fresh temp DBs).
2. In-process MCP handshake test (existing pattern from this session) shows 14 tools (12 + export + import).
3. E2E: `ctx serve --port 7339` with `CTX_AUTH_TOKEN` set → curl `/sse` without token = 401, with token = stream opens; run adapted `test_mcp.py` flow.
4. Export→wipe temp DB→import→`ctx_get` roundtrip returns identical buckets/bugs.
5. Push branch → GitHub Actions green on both OSes before tagging.
6. `/security-review` run on the diff; findings fixed or explicitly accepted.

## Execution order

Step 0 (commit 0.4.0) → Step 1 (tests for current behavior first — they pin the baseline) → Step 2 (security) → Step 3 (export/import) → Step 4 (caps) → Step 5 (docs + CI + release prep). Each step ends with the full suite green.
