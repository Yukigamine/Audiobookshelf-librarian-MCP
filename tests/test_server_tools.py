"""Tests for selected MCP tool wrappers in server.py."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from abs_librarian.jail import PathJailError
from abs_librarian.metadata import SeriesCache

os.environ.setdefault("ABS_URL", "http://abs.invalid")
os.environ.setdefault("ABS_TOKEN", "abs-test-token")
os.environ.setdefault("MCP_TOKEN", "mcp-test-token")

from abs_librarian import server as server_module  # noqa: E402


@pytest.mark.asyncio
async def test_health_returns_ok_with_library_count(monkeypatch):
    client = SimpleNamespace(get_libraries=AsyncMock(return_value=[{"id": "1"}, {"id": "2"}]))
    monkeypatch.setattr(server_module, "_client", lambda: client)

    result = await server_module.health()

    assert result == {"status": "ok", "abs_libraries": 2}
    client.get_libraries.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_health_returns_sanitized_error(monkeypatch):
    client = SimpleNamespace(get_libraries=AsyncMock(side_effect=RuntimeError("ABS offline")))
    monkeypatch.setattr(server_module, "_client", lambda: client)

    result = await server_module.health()

    assert result == {"status": "error", "detail": "Audiobookshelf connectivity failed"}


@pytest.mark.asyncio
async def test_batch_update_metadata_resolves_series_and_builds_payload(monkeypatch):
    client = SimpleNamespace(
        get_series=AsyncMock(return_value=[{"name": "Dune", "id": "series-1"}]),
        batch_update=AsyncMock(
            return_value={"updated": 1, "results": [{"success": True, "updates": 1}]}
        ),
    )
    monkeypatch.setattr(server_module, "_client", lambda: client)
    monkeypatch.setattr(server_module, "_series_cache", SeriesCache())

    result = await server_module.batch_update_metadata(
        "library-1",
        [
            {
                "id": "item-1",
                "title": "Dune",
                "authors": ["Frank Herbert"],
                "series": [{"name": "Dune", "sequence": "1"}],
            }
        ],
    )

    client.get_series.assert_awaited_with("library-1")
    client.batch_update.assert_awaited_once_with(
        [
            {
                "id": "item-1",
                "mediaPayload": {
                    "metadata": {
                        "title": "Dune",
                        "authors": [{"name": "Frank Herbert"}],
                        "series": [{"id": "series-1", "name": "Dune", "sequence": "1"}],
                    }
                },
            }
        ]
    )
    assert result == {"requested": 1, "updated": 1, "results": [{"success": True, "updates": 1}]}


@pytest.mark.asyncio
async def test_tool_detect_blobs_uses_config_defaults(monkeypatch):
    monkeypatch.setattr(server_module, "_permitted", lambda: ["/library"])
    monkeypatch.setattr(server_module.cfg, "blob_hours_threshold", 7.5)
    monkeypatch.setattr(server_module.cfg, "blob_file_count_threshold", 12)
    detect = MagicMock(return_value={"count": 1, "suspects": []})
    monkeypatch.setattr(server_module, "detect_blobs", detect)

    result = await server_module.tool_detect_blobs("/library")

    detect.assert_called_once_with("/library", ["/library"], 7.5, 12)
    assert result == {"count": 1, "suspects": []}


@pytest.mark.asyncio
async def test_tool_detect_blobs_honors_explicit_thresholds(monkeypatch):
    monkeypatch.setattr(server_module, "_permitted", lambda: ["/library"])
    detect = MagicMock(return_value={"count": 2, "suspects": [{"path": "/library/Book"}]})
    monkeypatch.setattr(server_module, "detect_blobs", detect)

    result = await server_module.tool_detect_blobs(
        "/library", hours_threshold=3.0, file_count_threshold=5
    )

    detect.assert_called_once_with("/library", ["/library"], 3.0, 5)
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_tool_detect_blobs_returns_jail_errors(monkeypatch):
    monkeypatch.setattr(server_module, "_permitted", lambda: ["/library"])
    monkeypatch.setattr(
        server_module,
        "detect_blobs",
        MagicMock(side_effect=PathJailError("outside permitted roots")),
    )

    result = await server_module.tool_detect_blobs("/escape")

    assert result == {"error": "outside permitted roots"}


@pytest.mark.asyncio
async def test_tool_fs_move_passes_configured_arguments(monkeypatch):
    monkeypatch.setattr(server_module, "_permitted", lambda: ["/library"])
    monkeypatch.setattr(server_module.cfg, "audit_log", "/audit/log.jsonl")
    move = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(server_module, "fs_move", move)

    result = await server_module.tool_fs_move("/library/source", "/library/dest", confirm=True)

    move.assert_called_once_with(
        "/library/source", "/library/dest", ["/library"], "/audit/log.jsonl", confirm=True
    )
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_tool_fs_move_returns_jail_errors(monkeypatch):
    monkeypatch.setattr(server_module, "_permitted", lambda: ["/library"])
    monkeypatch.setattr(
        server_module,
        "fs_move",
        MagicMock(side_effect=PathJailError("blocked move")),
    )

    result = await server_module.tool_fs_move("/escape", "/library/dest")

    assert result == {"error": "blocked move"}


@pytest.mark.asyncio
async def test_tool_fs_quarantine_passes_configured_arguments(monkeypatch):
    monkeypatch.setattr(server_module, "_permitted", lambda: ["/library"])
    monkeypatch.setattr(server_module.cfg, "quarantine_dir", "/quarantine")
    monkeypatch.setattr(server_module.cfg, "audit_log", "/audit/log.jsonl")
    quarantine = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(server_module, "fs_quarantine", quarantine)

    result = await server_module.tool_fs_quarantine("/library/book", confirm=True)

    quarantine.assert_called_once_with(
        "/library/book", ["/library"], "/quarantine", "/audit/log.jsonl", confirm=True
    )
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_tool_fs_quarantine_returns_jail_errors(monkeypatch):
    monkeypatch.setattr(server_module, "_permitted", lambda: ["/library"])
    monkeypatch.setattr(
        server_module,
        "fs_quarantine",
        MagicMock(side_effect=PathJailError("blocked quarantine")),
    )

    result = await server_module.tool_fs_quarantine("/escape")

    assert result == {"error": "blocked quarantine"}
