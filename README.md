# one-context — Combined Context

> One MCP server. Every AI tool. No re-explaining.

![Architecture Diagram](assets/architecture.png)

Claude Code, Cline, Antigravity, Codex, Ollama — they all point to the same place.  
You explain your project once. Every tool knows it forever.

**No API keys needed. No cloud. No accounts. 100% local.**

## The Architecture

Every AI tool keeps its own internal memory. That doesn't change. But they all **read and write to one shared root**:

```mermaid
graph TD
    %% Define styles
    classDef rootNode fill:#0d1117,stroke:#58a6ff,stroke-width:3px,color:#c9d1d9,font-size:16px,font-weight:bold;
    classDef aiNode fill:#161b22,stroke:#3fb950,stroke-width:2px,color:#c9d1d9,font-size:14px;
    classDef bucketNode fill:#21262d,stroke:#8b949e,stroke-dasharray: 5 5,color:#c9d1d9;

    %% Nodes
    A([one-context MCP]):::rootNode
    
    C1[Claude Code]:::aiNode
    C2[Cline]:::aiNode
    C3[Antigravity]:::aiNode
    C4[Codex]:::aiNode
    
    B1[(WHAT\nProject Scope)]:::bucketNode
    B2[(DONE\nHistory)]:::bucketNode
    B3[(NOW\nCurrent Task)]:::bucketNode
    B4[(MAP\nKey Files)]:::bucketNode

    %% Connections
    C1 <-->|reads/writes| A
    C2 <-->|reads/writes| A
    C3 <-->|reads/writes| A
    C4 <-->|reads/writes| A
    
    A --- B1
    A --- B2
    A --- B3
    A --- B4
```

## Install

With `uv` installed, you don't even need to download this repository. Your AI tools will fetch it automatically from PyPI!

## Connect Your AI Tools

### Option A: Command/stdio (recommended)

Works with Claude Desktop, Cline, Codex, and any MCP client. Just add this to your MCP settings file:

```json
{
  "mcpServers": {
    "one-context": {
      "command": "uvx",
      "args": ["one-context", "stdio"]
    }
  }
}
```

No server to start. The AI tool launches it automatically.

### Option B: HTTP/SSE (for network setups)

```bash
uvx one-context serve
```

Then point any MCP client to `http://localhost:7337/sse`.

## The Four Buckets

| Bucket | Contains | Updated when... |
|--------|----------|----------------|
| **WHAT** | Project description, stack, architecture, constraints | Project-level info changes |
| **DONE** | Decisions made, files changed, problems solved | Any tool finishes a task |
| **NOW** | Current task, current state, what's in progress | A new task starts |
| **MAP** | Important file paths and what they do | AI discovers key files |

## Usage in any tool

Start a session:
```
"Load context from one-context for my-project"
```

Finish a session:
```
"Update one-context with what we just did"
```

Register important files:
```
"Use ctx_map to register src/main.py as the entry point"
```

Search across all your projects:
```
"Search one-context for 'SQLite lock error'"
```

## MCP Tools (6 total)

| Tool | Description |
|------|-------------|
| `ctx_get(project)` | Get the full WHAT/DONE/NOW/MAP snapshot + git info |
| `ctx_update(project, session_summary, tool_name)` | Merge a session update into context |
| `ctx_map(project, files)` | Register important files manually |
| `ctx_search(query)` | Search across ALL projects' context and history |
| `ctx_reset(project)` | Wipe project context to empty |
| `ctx_list()` | List all tracked projects |

Projects are auto-created on first `ctx_update` — you don't need `ctx init`.

## Advanced Features

### MAP — Important Files
When an AI tool discovers that `src/main.py` is the entry point, it saves those paths to the MAP bucket. The next AI tool instantly knows which files matter without scanning the entire codebase.

### Git Branch Awareness
Link a project to its git repo:
```bash
ctx init my-project --path /path/to/repo
```
Now `ctx_get` automatically includes the current branch name, last 5 commits, and uncommitted changes.

### Cross-Project Search
```bash
ctx search "SQLite"
```
Searches across ALL projects' context (what/done/now/map) and update history. Find how you solved a problem before.

## Summarization Modes

| Mode | Setup | API Key? |
|------|-------|----------|
| **Local** (default) | Nothing | No |
| **Ollama** | `CTX_OLLAMA_MODEL=llama3.2` | No |
| **Claude** | `ANTHROPIC_API_KEY=...` | Yes |
| **OpenAI / Groq / Together** | `OPENAI_API_KEY=...` | Yes |

Priority: Ollama > Claude > OpenAI > Local fallback. If any provider fails, it silently falls back. **Local always works.**

## CLI Commands

| Command | Description |
|---------|-------------|
| `ctx stdio` | Run via stdio (for MCP clients) |
| `ctx serve` | Start HTTP/SSE server |
| `ctx init <project> [--path /repo]` | Initialize with optional git repo |
| `ctx status [project]` | Show context + git info |
| `ctx search <query>` | Search across all projects |
| `ctx reset <project>` | Reset project context |
| `ctx list` | List all projects |
| `ctx delete <project>` | Delete a project |

## License

MIT
