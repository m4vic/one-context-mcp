# one-context 0.6.0 — Slim Redesign

> **Canonical plan.** The `ctx_doc(one-context-mcp, "plan")` entry is a published
> mirror of this file for tools that pull context remotely; this file is the
> source of truth. Keep them in sync when the plan changes materially.

## Goal

The tool has exactly one job: **frictionless, correct context handoff between AI
tools** (Codex ↔ Antigravity ↔ Claude Code). "Outstanding" here means
**reliability + invisibility, not feature count.**

Shrink the model-facing tool surface to the essentials. Keep every capability —
fold it into a parameter or move it to the CLI. Fewer tools = fewer wrong calls,
fewer hang surfaces, a one-call handoff.

**Non-goals (explicitly out of scope):** full chat-transcript auto-capture (an
MCP server cannot read the host tool's conversation — hard protocol boundary),
per-tool integrations, more buckets, more tools.

## Guarantees

- **DB schema + CLI: unchanged.** Existing `~/.ctx/ctx.db` works untouched — no
  data migration. All `ctx <cmd>` CLI commands stay.
- **Only the MCP tool layer shrinks.** The DB functions the tools call are kept.
- **Clean break at 0.6.0**, no deprecated tool aliases (aliases would re-add the
  surface we're removing). A loud migration note ships instead. Real risk is low:
  model-facing tools are called by AI (which reads the live tool list +
  `how_to_ctx`), not by scripts.

## The 5 model-facing tools

### 1. `ctx_get(project?, repo_path?, view?="full")`
- `project` given → use it. `project` omitted + `repo_path` → **auto-resolve**
  (folds `ctx_resolve`). Neither → error with a hint.
- `project` given but `repo_path` mismatches the stored one → `safety.warning` in
  the result (folds `ctx_strict_get` as a soft warning the caller heeds).
- `view`: `full` | `brief` | `detailed`. `detailed` returns full verbatim history
  + notes + docs (folds `ctx_history`).
- Returns buckets + git (offloaded to a thread) + the `instructions` doc + a docs
  index.

### 2. `ctx_update(project?, session_summary?, tool_name, repo_path?, author?, files?)`
- Project resolution + `_auto_init_guard` as today.
- `repo_path` → auto-links / inits (folds `ctx_link`). Empty `session_summary` +
  `repo_path` → pure link/init, no bucket write.
- `author` present → also stored as a `project_message` and merged (folds
  `ctx_note`).
- `files` present → registered into MAP (folds `ctx_map`).

### 3. `ctx_doc(project, kind, content?, action?)` — unchanged
- Verbatim per-project docs (`plan` / `instructions` / `context` / `handoff` / …).
- **Handoff convention:** `kind="handoff"` holds a fuller narrative snapshot for
  switching tools — no new tool required.

### 4. `ctx_search(query, project?)`
- Cross-project search; add an optional `project` scope (also serves the later
  privacy-scoping work).

### 5. `how_to_ctx()` — rewritten
- Guide rewritten for the 5-tool workflow, including the **save handoff / load
  handoff** pattern.

## Removed from MCP, kept in the CLI (capability preserved)

`ctx_export`, `ctx_import`, `ctx_reset`, `ctx_list` → human backup/admin ops.
They stay as `ctx export|import|reset|list`; they leave the model's tool list.

## Cut (scope creep)

`ctx_bug` → the tool is removed. The `bugs` table is kept for data continuity +
export, and `ctx_get` still displays existing bug rows if present. Track new bugs
via a note or `ctx_doc(kind="bugs")`.

## Folded away (deleted as standalone tools)

| Old tool | New form |
|----------|----------|
| `ctx_resolve(path)` | `ctx_get(repo_path=path)` |
| `ctx_strict_get(p, path)` | `ctx_get(p, repo_path=path)` + heed `safety.warning` |
| `ctx_link(p, path)` | `ctx_update(p, repo_path=path)` (empty summary) |
| `ctx_note(p, msg, author)` | `ctx_update(p, session_summary=msg, author=author)` |
| `ctx_map(p, files)` | `ctx_update(p, files=[...])` |
| `ctx_history(p)` | `ctx_get(p, view="detailed")` |
| `ctx_bug(...)` | a note, or `ctx_doc(p, "bugs", ...)` |
| `ctx_export/import/reset/list` | CLI: `ctx export\|import\|reset\|list` |

## The handoff (the headline feature — zero new tools)

Two fast steps, identical across all tools because they're just MCP calls:

- **Tool A, when switching:** "save handoff" → one `ctx_update` (current state,
  next step) + optionally one `ctx_doc(kind="handoff")` for the narrative.
- **Tool B, seconds later:** "load handoff" → `ctx_get(repo_path=".")` →
  auto-resolves the project and returns everything, including the handoff doc.

Timestamps are already on every entry; user messages have a verbatim home
(notes). This delivers the instant-switch experience without the impossible
full-transcript capture.

## Implementation steps

1. **`ctx/server.py`** — rewrite `handle_list_tools` to declare only the 5 tools
   with folded params. Rewrite `handle_call_tool`: `get` (resolve + warn),
   `update` (author/files/link folding), `doc`, `search(+project)`, `how_to_ctx`.
   Delete the removed handlers; keep the underlying DB functions.
2. **`ctx/git.py`** — reduce 6 subprocess calls → 2
   (`git status --porcelain=v1 --branch` for branch + changes in one call,
   `git log -5` for commits). Keep the 0.5.2 hardening (stdin=DEVNULL,
   CREATE_NO_WINDOW, thread offload, `CTX_DISABLE_GIT`).
3. **`ctx/llm.py`** — keep local merge; small robustness: stop NOW-extraction from
   splitting on the `.` in version numbers. (Low priority.)
4. **`how_to_ctx` text + `SERVER_INSTRUCTIONS`** — rewrite for the 5-tool flow +
   the handoff pattern.
5. **`ctx/cli.py`** — unchanged; verify `list/export/import/reset` still work.
6. **`tests/`** — rework tool-facing tests to the 5-tool surface; keep DB-layer
   tests. Add fold-behavior tests: `update+author` == old note; `update+files` ==
   old map; `get` without project == resolve; `get` mismatch == warning;
   `get view=detailed` == history.
7. **Docs** — rewrite README + INSTALLATION_GUIDE to a 5-tool table + the
   migration table above. Update `CLAUDE.md` architecture/invariants.
8. **Version 0.6.0.** Build, `twine check`. User publishes + pushes the tag.

## Standing reliability invariant

No blocking I/O on the event loop, ever; tight time budgets; degrade gracefully
(return partial, never hang). Git was the first instance (fixed in 0.5.2); this
is now a rule for every new code path.
