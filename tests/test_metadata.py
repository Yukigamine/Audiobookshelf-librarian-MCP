"""Tests for metadata payload builder and series cache."""

import pytest

from abs_librarian.metadata import SeriesCache, build_metadata_payload


def test_build_metadata_only_supplied_fields():
    payload = build_metadata_payload(title="Dune", genres=["Sci-Fi"])
    assert payload == {"title": "Dune", "genres": ["Sci-Fi"]}
    assert "authors" not in payload
    assert "series" not in payload


def test_build_metadata_authors_as_strings():
    payload = build_metadata_payload(authors=["Frank Herbert"])
    assert payload["authors"] == [{"name": "Frank Herbert"}]


def test_series_cache_resolves_by_id():
    cache = SeriesCache()
    cache.seed("lib-1", [{"name": "Dune Chronicles", "id": "abc123"}])
    result = cache.resolve("lib-1", "Dune Chronicles", "1")
    assert result == {"id": "abc123", "name": "Dune Chronicles", "sequence": "1"}


def test_series_cache_falls_back_to_name():
    cache = SeriesCache()
    result = cache.resolve("lib-1", "New Series", "2")
    assert result == {"name": "New Series", "sequence": "2"}


def test_series_cache_case_insensitive():
    cache = SeriesCache()
    cache.seed("lib-1", [{"name": "The Expanse", "id": "xyz"}])
    result = cache.resolve("lib-1", "the expanse", "3")
    assert result["id"] == "xyz"


def test_series_cache_add():
    cache = SeriesCache()
    cache.add("lib-1", "Brand New Series", "newid")
    assert cache.resolve("lib-1", "Brand New Series")["id"] == "newid"


def test_series_cache_scopes_entries_per_library():
    cache = SeriesCache()
    cache.seed("lib-1", [{"name": "Shared Name", "id": "series-1"}])
    cache.seed("lib-2", [{"name": "Shared Name", "id": "series-2"}])

    assert cache.resolve("lib-1", "Shared Name")["id"] == "series-1"
    assert cache.resolve("lib-2", "Shared Name")["id"] == "series-2"


def test_series_cache_invalidate_library_only():
    cache = SeriesCache()
    cache.seed("lib-1", [{"name": "Series One", "id": "series-1"}])
    cache.seed("lib-2", [{"name": "Series Two", "id": "series-2"}])

    cache.invalidate("lib-1")

    assert cache.resolve("lib-1", "Series One") == {"name": "Series One", "sequence": ""}
    assert cache.resolve("lib-2", "Series Two")["id"] == "series-2"


def test_series_cache_expires_after_ttl():
    current_time = 0.0
    cache = SeriesCache(ttl_seconds=1.0, clock=lambda: current_time)
    cache.seed("lib-1", [{"name": "Series One", "id": "series-1"}])

    assert cache.resolve("lib-1", "Series One")["id"] == "series-1"
    current_time = 1.0
    assert cache.resolve("lib-1", "Series One") == {"name": "Series One", "sequence": ""}
    assert "lib-1" not in cache._cache


@pytest.mark.asyncio
async def test_batch_update_metadata_refreshes_series_cache_per_library(monkeypatch):
    import os

    os.environ.setdefault("ABS_URL", "http://abs.invalid")
    os.environ.setdefault("ABS_TOKEN", "abs-test-token")
    os.environ.setdefault("MCP_TOKEN", "test-token")

    from abs_librarian import server as server_module

    class FakeClient:
        def __init__(self):
            self.get_series_calls = []
            self.batch_payloads = []
            self._series_responses = {
                "lib-1": [
                    [{"name": "Existing", "id": "existing-1"}],
                    [
                        {"name": "Existing", "id": "existing-1"},
                        {"name": "New Series", "id": "new-1"},
                    ],
                    [
                        {"name": "Existing", "id": "existing-1"},
                        {"name": "New Series", "id": "new-1"},
                    ],
                ],
                "lib-2": [
                    [{"name": "Existing", "id": "existing-2"}],
                    [{"name": "Existing", "id": "existing-2"}],
                ],
            }

        async def get_series(self, library_id):
            self.get_series_calls.append(library_id)
            return self._series_responses[library_id].pop(0)

        async def batch_update(self, payloads):
            self.batch_payloads.append(payloads)
            return {
                "updated": len(payloads),
                "results": [{"success": True, "updates": len(payloads)}],
            }

    fake_client = FakeClient()
    monkeypatch.setattr(server_module, "_series_cache", SeriesCache(ttl_seconds=None))
    monkeypatch.setattr(server_module, "_client", lambda: fake_client)

    await server_module.batch_update_metadata(
        "lib-1",
        [{"id": "item-1", "series": [{"name": "New Series", "sequence": "1"}]}],
    )
    await server_module.batch_update_metadata(
        "lib-1",
        [{"id": "item-2", "series": [{"name": "New Series", "sequence": "2"}]}],
    )
    await server_module.batch_update_metadata(
        "lib-2",
        [{"id": "item-3", "series": [{"name": "Existing", "sequence": "3"}]}],
    )

    assert fake_client.get_series_calls == ["lib-1", "lib-1", "lib-1", "lib-2", "lib-2"]
    assert fake_client.batch_payloads[0][0]["mediaPayload"]["metadata"]["series"][0] == {
        "name": "New Series",
        "sequence": "1",
    }
    assert fake_client.batch_payloads[1][0]["mediaPayload"]["metadata"]["series"][0] == {
        "id": "new-1",
        "name": "New Series",
        "sequence": "2",
    }
    assert fake_client.batch_payloads[2][0]["mediaPayload"]["metadata"]["series"][0] == {
        "id": "existing-2",
        "name": "Existing",
        "sequence": "3",
    }
