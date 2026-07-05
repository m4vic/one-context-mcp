"""Shared fixtures for the one-context test suite.

Each test gets a fresh SQLite database via the CTX_DB_PATH env var, which
ctx.database reads on every connection. Tools are exercised in-process
through ctx.server.handle_call_tool - no transport or server required.
"""

import asyncio
import json

import pytest

from ctx.server import handle_call_tool


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point every test at its own empty database."""
    db_path = tmp_path / "ctx_test.db"
    monkeypatch.setenv("CTX_DB_PATH", str(db_path))
    # Force the default local merge mode regardless of the host machine's env.
    monkeypatch.delenv("CTX_MERGE_MODE", raising=False)
    monkeypatch.delenv("CTX_OLLAMA_MODEL", raising=False)
    return db_path


@pytest.fixture
def call_tool():
    """Synchronous helper: invoke an MCP tool and return the parsed JSON result."""

    def _call(name: str, arguments: dict) -> dict | list:
        result = asyncio.run(handle_call_tool(name, arguments))
        return json.loads(result[0].text)

    return _call
