"""Context merging for ctx - Combined Context.

Four merge modes (selected with CTX_MERGE_MODE):

1. LOCAL (default) - Smart rule-based merge. No API keys, no models,
   no internet. Always works. This is the foundation.

2. OLLAMA (local LLM) - Uses any model running on your local Ollama.
   Set CTX_OLLAMA_MODEL=llama3.2 (or any model you have pulled).
   Still 100% local, still zero API keys.

3. CLOUD API (optional) - Claude, OpenAI, or any OpenAI-compatible API.
   Set ANTHROPIC_API_KEY or OPENAI_API_KEY + OPENAI_MODEL.
   For custom endpoints: OPENAI_BASE_URL=http://your-api/v1

Set CTX_MERGE_MODE=auto to try Ollama > Anthropic > OpenAI > local fallback.
"""

import os
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError


_VALID_MERGE_MODES = {"local", "auto", "ollama", "anthropic", "openai"}


def _get_merge_mode() -> str:
    """Return the configured merge mode, defaulting to fully local behavior."""
    mode = os.environ.get("CTX_MERGE_MODE", "local").strip().lower()
    if mode not in _VALID_MERGE_MODES:
        print(f"[ctx] Invalid CTX_MERGE_MODE={mode!r}; using local merge.")
        return "local"
    return mode


MERGE_SYSTEM_PROMPT = """\
You are a context merger for a software project. You receive:
1. The CURRENT context snapshot (WHAT, DONE, NOW, MAP buckets)
2. A NEW session summary from a developer tool

Your job: merge the new information into the four buckets cleanly.

Rules:
- WHAT = project description, stack, architecture, constraints. Only update if the new info changes the project's nature.
- DONE = decisions made, files changed, problems solved. Append new items, deduplicate, keep concise. Remove items that are superseded.
- NOW  = current task, current state, what's in progress. REPLACE with whatever is current. Old "now" items move to DONE if completed, or get dropped if abandoned.
- MAP  = important file paths and what they do. Only add files that are genuinely important (entry points, core modules, config files). Format as "- path/to/file - description". When repo_path is known, prefer file paths under that root and ignore unrelated paths. Remove stale entries if the summary says files were deleted or moved.
- Be concise. Each bucket should be a clean bulleted list, not a wall of text.
- Preserve important details (file paths, decisions, constraints) but drop chatter.
- If the new summary contradicts old info, the new summary wins.

Respond with ONLY a JSON object (no markdown fences, no explanation):
{"what": "...", "done": "...", "now": "...", "map": "..."}
"""


def _build_user_message(
    current_what: str, current_done: str, current_now: str,
    current_map: str, session_summary: str, tool_name: str, repo_path: str = "",
) -> str:
    project_root = f"\n### PROJECT ROOT\n{repo_path}" if repo_path else ""
    return f"""## Current Context

### WHAT (project description, stack, architecture)
{current_what or '(empty -- new project)'}

### DONE (decisions made, files changed, problems solved)
{current_done or '(nothing yet)'}

### NOW (current task, current state, in progress)
{current_now or '(nothing yet)'}

### MAP (important files and their purpose)
{current_map or '(no files mapped yet)'}{project_root}

---

## New Session Summary
**From tool:** {tool_name}

{session_summary}"""


def _parse_llm_response(text: str, current_what: str, current_done: str,
                         current_now: str, current_map: str) -> Optional[dict]:
    """Parse JSON from LLM response, handling markdown fences."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    # Find JSON object in the text
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]
    try:
        result = json.loads(text)
        return {
            "what": result.get("what", current_what),
            "done": result.get("done", current_done),
            "now": result.get("now", current_now),
            "map": result.get("map", current_map),
        }
    except (json.JSONDecodeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# 1. LOCAL MERGE (default - always works, zero dependencies)
# ---------------------------------------------------------------------------

def _deduplicate_lines(text: str) -> str:
    """Remove duplicate bullet points while preserving order."""
    seen = set()
    result = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        normalized = re.sub(r'^[-*]\s*', '', stripped).strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(line)
    return '\n'.join(result)


def _trim_to_max_items(text: str, max_items: int = 50) -> str:
    """Keep only the most recent N bullet items."""
    lines = text.strip().splitlines()
    if len(lines) <= max_items:
        return text
    return '\n'.join(lines[-max_items:])


# Regex to detect file paths in text
_FILE_PATH_RE = re.compile(
    r'(?:'
    r'[A-Za-z]:\\(?:[^\s\\/:*?"<>|]+\\)*[^\s\\/:*?"<>|]+'  # Windows: C:\foo\bar.py
    r'|'
    r'(?:\.{0,2}/)?(?:[a-zA-Z0-9_.-]+/)+[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]+'  # Unix: src/main.py
    r')'
)

# Common file extensions that indicate important code files
_CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs', '.java', '.kt',
    '.c', '.cpp', '.h', '.hpp', '.cs', '.rb', '.php', '.swift', '.vue',
    '.svelte', '.sql', '.toml', '.yaml', '.yml', '.json', '.md',
    '.dockerfile', '.sh', '.bat', '.ps1',
}



def _path_within_repo(path: str, repo_path: str) -> bool:
    """Keep absolute paths scoped to the project root when available."""
    if not repo_path:
        return True

    candidate = Path(path)
    if not candidate.is_absolute():
        return True

    try:
        return candidate.resolve(strict=False).is_relative_to(Path(repo_path).resolve(strict=False))
    except Exception:
        return False


def _extract_file_paths(text: str, repo_path: str = "") -> list[str]:
    """Extract file paths from text that look like real code files."""
    paths = _FILE_PATH_RE.findall(text)
    result = []
    seen = set()
    for p in paths:
        ext = '.' + p.rsplit('.', 1)[-1].lower() if '.' in p else ''
        if ext in _CODE_EXTENSIONS and _path_within_repo(p, repo_path):
            key = p.replace('/', '\\').lower()
            if key not in seen:
                seen.add(key)
                result.append(p)
    return result



def _local_merge(
    current_what: str, current_done: str, current_now: str,
    current_map: str, session_summary: str, tool_name: str, repo_path: str = "",
) -> dict:
    """Smart local merge - no LLM, no API, always works.

    Strategy:
    - WHAT: Update only if summary contains project-level keywords.
    - DONE: Move current NOW to DONE, then add new summary entry.
    - NOW: Extract "in progress" / "next" signals from summary.
    - MAP: Extract file paths mentioned in the summary.
    """
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

    # --- WHAT ---
    new_what = current_what
    what_keywords = [
        'stack:', 'architecture:', 'project:', 'tech stack',
        'built with', 'using python', 'using typescript', 'using rust',
        'using react', 'using next', 'using vue', 'using go',
        'description:', 'constraints:', 'requirements:', 'framework:',
    ]
    summary_lower = session_summary.lower()
    if any(kw in summary_lower for kw in what_keywords):
        entry = f'- [{tool_name}] {session_summary}'
        new_what = (current_what.rstrip() + '\n' + entry).strip() if current_what else entry
        new_what = _deduplicate_lines(new_what)

    # --- DONE ---
    new_done = current_done
    if current_now.strip():
        new_done = (new_done.rstrip() + '\n' + current_now.strip()).strip() if new_done.strip() else current_now.strip()
    entry = f'- [{tool_name} @ {timestamp}] {session_summary}'
    new_done = (new_done.rstrip() + '\n' + entry).strip() if new_done.strip() else entry
    new_done = _deduplicate_lines(new_done)
    new_done = _trim_to_max_items(new_done, max_items=50)

    # --- NOW ---
    now_keywords = [
        'currently', 'in progress', 'working on', 'next step',
        'todo', 'to do', 'needs to', 'will ', 'planning to',
        'starting', 'beginning',
    ]
    new_now = ''
    for sentence in re.split(r'[.\n]', session_summary):
        sentence = sentence.strip()
        if sentence and any(kw in sentence.lower() for kw in now_keywords):
            new_now += f'- {sentence}\n'

    # --- MAP ---
    new_map = current_map
    file_paths = _extract_file_paths(session_summary, repo_path=repo_path)
    if file_paths:
        existing_paths = set()
        for line in current_map.splitlines():
            # Extract path from "- path - description" format
            clean = re.sub(r'^[-*]\s*', '', line.strip())
            if clean:
                path_part = clean.split(' \u2014 ')[0].split(' - ')[0].strip()
                existing_paths.add(path_part.lower())

        for fp in file_paths:
            if fp.lower() not in existing_paths:
                new_map = (new_map.rstrip() + f'\n- {fp}').strip() if new_map.strip() else f'- {fp}'
                existing_paths.add(fp.lower())

        new_map = _trim_to_max_items(new_map, max_items=30)

    return {
        'what': new_what.strip(),
        'done': new_done.strip(),
        'now': new_now.strip(),
        'map': new_map.strip(),
    }


# ---------------------------------------------------------------------------
# 2. OLLAMA MERGE (local LLM - zero API keys, runs on your machine)
# ---------------------------------------------------------------------------

def _ollama_merge(
    current_what: str, current_done: str, current_now: str,
    current_map: str, session_summary: str, tool_name: str, repo_path: str = "",
) -> Optional[dict]:
    """Use a local Ollama model for intelligent merge.

    Config via env vars:
        CTX_OLLAMA_MODEL  - model name (e.g. llama3.2, mistral, gemma2)
        CTX_OLLAMA_URL    - Ollama API URL (default: http://localhost:11434)
    """
    model = os.environ.get("CTX_OLLAMA_MODEL")
    if not model:
        return None

    base_url = os.environ.get("CTX_OLLAMA_URL", "http://localhost:11434")
    url = f"{base_url}/api/chat"

    user_msg = _build_user_message(
        current_what, current_done, current_now, current_map,
        session_summary, tool_name, repo_path,
    )

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": MERGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }).encode()

    try:
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            text = data.get("message", {}).get("content", "")
            result = _parse_llm_response(text, current_what, current_done, current_now, current_map)
            if result:
                return result
            print(f"[ctx] Ollama response wasn't valid JSON, using local merge.")
            return None
    except (URLError, TimeoutError, json.JSONDecodeError, KeyError) as e:
        print(f"[ctx] Ollama merge failed ({e}), using local merge.")
        return None


# ---------------------------------------------------------------------------
# 3. ANTHROPIC MERGE (optional cloud - needs ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

def _anthropic_merge(
    current_what: str, current_done: str, current_now: str,
    current_map: str, session_summary: str, tool_name: str, repo_path: str = "",
) -> Optional[dict]:
    """Use Claude Haiku for merge. Only if ANTHROPIC_API_KEY is set."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        user_msg = _build_user_message(
            current_what, current_done, current_now, current_map,
            session_summary, tool_name, repo_path,
        )
        response = client.messages.create(
            model=os.environ.get("CTX_ANTHROPIC_MODEL", "claude-haiku-4-20250414"),
            max_tokens=2048,
            system=MERGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text
        result = _parse_llm_response(text, current_what, current_done, current_now, current_map)
        if result:
            return result
        print("[ctx] Anthropic response wasn't valid JSON, using local merge.")
        return None
    except Exception as e:
        print(f"[ctx] Anthropic merge failed ({e}), using local merge.")
        return None


# ---------------------------------------------------------------------------
# 4. OPENAI-COMPATIBLE MERGE (any API - OpenAI, Groq, Together, etc.)
# ---------------------------------------------------------------------------

def _openai_merge(
    current_what: str, current_done: str, current_now: str,
    current_map: str, session_summary: str, tool_name: str, repo_path: str = "",
) -> Optional[dict]:
    """Use any OpenAI-compatible API for merge.

    Config via env vars:
        OPENAI_API_KEY   - API key
        OPENAI_MODEL     - model name (default: gpt-4o-mini)
        OPENAI_BASE_URL  - custom endpoint (default: https://api.openai.com/v1)
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    url = f"{base_url}/chat/completions"

    user_msg = _build_user_message(
        current_what, current_done, current_now, current_map,
        session_summary, tool_name, repo_path,
    )

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": MERGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
    }).encode()

    try:
        req = Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            text = data["choices"][0]["message"]["content"]
            result = _parse_llm_response(text, current_what, current_done, current_now, current_map)
            if result:
                return result
            print("[ctx] OpenAI response wasn't valid JSON, using local merge.")
            return None
    except Exception as e:
        print(f"[ctx] OpenAI-compatible merge failed ({e}), using local merge.")
        return None


# ---------------------------------------------------------------------------
# PUBLIC API - tries each provider in priority order, always falls back local
# ---------------------------------------------------------------------------

def merge_context(
    current_what: str, current_done: str, current_now: str,
    current_map: str, session_summary: str, tool_name: str, repo_path: str = "",
) -> dict:
    """Merge a session summary into the context buckets.

    Default mode is local. CTX_MERGE_MODE can be set to auto, ollama,
    anthropic, or openai for model-assisted merging.
    """
    mode = _get_merge_mode()
    if mode == "local":
        return _local_merge(
            current_what, current_done, current_now, current_map,
            session_summary, tool_name, repo_path,
        )

    providers = {
        "ollama": [_ollama_merge],
        "anthropic": [_anthropic_merge],
        "openai": [_openai_merge],
        "auto": [_ollama_merge, _anthropic_merge, _openai_merge],
    }[mode]

    for provider in providers:
        result = provider(
            current_what, current_done, current_now, current_map,
            session_summary, tool_name, repo_path,
        )
        if result is not None:
            return result

    # Local merge always remains the final fallback.
    return _local_merge(
        current_what, current_done, current_now, current_map,
        session_summary, tool_name, repo_path,
    )
