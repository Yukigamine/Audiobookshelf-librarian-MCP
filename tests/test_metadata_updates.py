"""Regression tests for the ABS 2.36.1 metadata request/response contract."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import httpx
import pytest

from abs_librarian.abs_client import ABSClient
from abs_librarian.metadata import SeriesCache

os.environ.setdefault("ABS_URL", "http://abs.invalid")
os.environ.setdefault("ABS_TOKEN", "fixture-token")
os.environ.setdefault("MCP_TOKEN", "fixture-mcp-token")

from abs_librarian import server  # noqa: E402


@pytest.fixture()
def abs_wire(monkeypatch):
    """Capture real HTTPX requests, never contacting a live library."""
    captured = []
    response = {"success": True, "updates": 1}
    real_client = httpx.AsyncClient

    def handle(request):
        captured.append(request)
        if request.method == "GET":
            library = request.url.path.split("/")[3]
            return httpx.Response(
                200, json={"results": [{"id": f"{library}-series", "name": "Shared Series"}]}
            )
        assert request.url.path == "/api/items/batch/update"
        return httpx.Response(200, json=response)

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )
    monkeypatch.setattr(server, "_client", lambda: ABSClient("http://abs.invalid", "fixture"))
    monkeypatch.setattr(server, "_series_cache", SeriesCache())
    return captured, response


async def test_metadata_wire_preserves_fields_and_places_tags_at_media_level(abs_wire):
    captured, _ = abs_wire
    result = await server.batch_update_metadata(
        "lib-A",
        [
            {
                "id": "item-A",
                "title": "Title",
                "authors": ["Author"],
                "narrators": ["Narrator A", "Narrator B"],
                "genres": ["Genre"],
                "tags": ["Tag"],
            }
        ],
    )
    post = next(r for r in captured if r.method == "POST")
    assert json.loads(post.content) == [
        {
            "id": "item-A",
            "mediaPayload": {
                "tags": ["Tag"],
                "metadata": {
                    "title": "Title",
                    "authors": [{"name": "Author"}],
                    "narrators": ["Narrator A", "Narrator B"],
                    "genres": ["Genre"],
                },
            },
        }
    ]
    assert result == {"requested": 1, "updated": 1, "results": [{"success": True, "updates": 1}]}


@pytest.mark.parametrize("narrators", [["Narrator"], ["One", "Two"], []])
async def test_narrators_remain_strings_including_explicit_clear(abs_wire, narrators):
    captured, _ = abs_wire
    await server.batch_update_metadata("lib-A", [{"id": "item-A", "narrators": narrators}])
    payload = json.loads(next(r for r in captured if r.method == "POST").content)[0]
    assert payload["mediaPayload"]["metadata"] == {"narrators": narrators}


async def test_empty_tags_clear_only_tags_while_omitted_fields_are_untouched(abs_wire):
    captured, _ = abs_wire
    await server.batch_update_metadata("lib-A", [{"id": "item-A", "tags": []}])
    payload = json.loads(next(r for r in captured if r.method == "POST").content)[0]
    assert payload["mediaPayload"] == {"metadata": {}, "tags": []}


@pytest.mark.parametrize("libraries", [("lib-A", "lib-B"), ("lib-B", "lib-A")])
async def test_cached_series_include_required_name_and_stay_library_scoped(abs_wire, libraries):
    captured, _ = abs_wire
    for library in libraries:
        await server.batch_update_metadata(
            library,
            [{"id": f"{library}-item", "series": [{"name": "Shared Series", "sequence": "2"}]}],
        )
    bodies = [json.loads(r.content)[0] for r in captured if r.method == "POST"]
    for library, body in zip(libraries, bodies, strict=True):
        assert body["mediaPayload"]["metadata"]["series"] == [
            {"id": f"{library}-series", "name": "Shared Series", "sequence": "2"}
        ]


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_tool_reports_actual_changes_not_requested_count(abs_wire, count):
    _, response = abs_wire
    response["updates"] = count
    result = await server.batch_update_metadata(
        "lib-A", [{"id": "one", "tags": []}, {"id": "two", "narrators": []}]
    )
    assert result["requested"] == 2
    assert result["updated"] == count
    assert result["results"] == [{"success": True, "updates": count}]


async def test_chunk_counts_are_summed_including_zero_and_partial_changes():
    client = ABSClient("http://abs.invalid", "fixture")
    responses = [{"success": True, "updates": n} for n in [0, 40, 3]]
    client._post = AsyncMock(side_effect=responses)
    result = await client.batch_update([{"id": f"item-{i}"} for i in range(250)])
    assert result == {"updated": 43, "results": responses}
    assert [len(call.args[1]) for call in client._post.await_args_list] == [100, 100, 50]


async def test_empty_batch_is_zero_changes_without_a_request():
    client = ABSClient("http://abs.invalid", "fixture")
    client._post = AsyncMock()
    assert await client.batch_update([]) == {"updated": 0, "results": []}
    client._post.assert_not_awaited()


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"success": False, "updates": 0},
        {"success": True},
        {"success": True, "updates": -1},
        {"success": True, "updates": 2},
        {"success": True, "updates": True},
        {"success": True, "updates": "1"},
    ],
)
async def test_invalid_batch_responses_never_claim_success(response):
    client = ABSClient("http://abs.invalid", "fixture")
    client._post = AsyncMock(return_value=response)
    with pytest.raises(ValueError, match="ABS returned"):
        await client.batch_update([{"id": "item-A"}])


async def test_http_failure_remains_an_error_instead_of_an_update_count(abs_wire, monkeypatch):
    client = ABSClient("http://abs.invalid", "fixture")
    failure = httpx.HTTPStatusError(
        "fixture failure",
        request=httpx.Request("POST", "http://abs.invalid"),
        response=httpx.Response(500),
    )
    monkeypatch.setattr(client, "_post", AsyncMock(side_effect=failure))
    monkeypatch.setattr(server, "_client", lambda: client)
    with pytest.raises(httpx.HTTPStatusError):
        await server.batch_update_metadata("lib-A", [{"id": "item-A", "title": "Title"}])
