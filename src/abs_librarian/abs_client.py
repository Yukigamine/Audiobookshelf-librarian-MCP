"""Thin async HTTP client for the Audiobookshelf REST API."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

AUDIBLE = "audible"
_BATCH_SIZE = 100


def _validate_id(value: str, name: str) -> str:
    """Accept opaque raw IDs, but never URL syntax or pre-encoded input."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value in {".", ".."}
        or any(char in value for char in "/\\%?#")
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
    ):
        raise ValueError(
            f"Invalid {name}: expected a non-empty raw ID without dot segments, "
            "path/query/fragment delimiters, percent escapes, or control characters"
        )
    return value


def _id_segment(value: str, name: str) -> str:
    return quote(_validate_id(value, name), safe="")


class ABSClient:
    def __init__(self, base_url: str, token: str, library_items_limit: int = 5000) -> None:
        if library_items_limit <= 0:
            raise ValueError("library_items_limit must be greater than 0")
        self._base = base_url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._library_items_limit = library_items_limit

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{self._base}{path}", headers=self._headers, params=params)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: Any = None) -> Any:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{self._base}{path}", headers=self._headers, json=body)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {}

    async def _patch(self, path: str, body: Any) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.patch(f"{self._base}{path}", headers=self._headers, json=body)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {}

    async def _delete(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(f"{self._base}{path}", headers=self._headers)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {}

    # ------------------------------------------------------------------
    # Libraries
    # ------------------------------------------------------------------

    async def get_libraries(self) -> list[dict]:
        data = await self._get("/api/libraries")
        return data.get("libraries", [])

    async def get_library_items(self, library_id: str) -> list[dict]:
        library_id = _id_segment(library_id, "library_id")
        data = await self._get(
            f"/api/libraries/{library_id}/items", limit=self._library_items_limit
        )
        return data.get("results", [])

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    async def get_item(self, item_id: str) -> dict:
        item_id = _id_segment(item_id, "item_id")
        return await self._get(f"/api/items/{item_id}", expanded=1)

    async def patch_item_metadata(self, item_id: str, metadata: dict) -> dict:
        item_id = _id_segment(item_id, "item_id")
        return await self._patch(f"/api/items/{item_id}/media", {"metadata": metadata})

    async def batch_update(self, updates: list[dict]) -> dict:
        """Send chunks and retain ABS's actual changed-item counts and responses."""
        for update in updates:
            _validate_id(update.get("id"), "item_id")
        results: list[dict] = []
        updated = 0
        for i in range(0, len(updates), _BATCH_SIZE):
            chunk = updates[i : i + _BATCH_SIZE]
            resp = await self._post("/api/items/batch/update", chunk)
            if not isinstance(resp, dict) or resp.get("success") is not True:
                raise ValueError("ABS returned an unsuccessful or invalid batch update response")
            count = resp.get("updates")
            if type(count) is not int or not 0 <= count <= len(chunk):
                raise ValueError("ABS returned an invalid batch update count")
            updated += count
            results.append(resp)
        return {"updated": updated, "results": results}

    async def batch_quickmatch(
        self,
        item_ids: list[str],
        provider: str = AUDIBLE,
        override_cover: bool = False,
        override_details: bool = False,
    ) -> dict:
        for item_id in item_ids:
            _validate_id(item_id, "item_id")
        body = {
            "options": {
                "provider": provider,
                "overrideCover": override_cover,
                "overrideDetails": override_details,
            },
            "libraryItemIds": item_ids,
        }
        return await self._post("/api/items/batch/quickmatch", body)

    # ------------------------------------------------------------------
    # Covers
    # ------------------------------------------------------------------

    async def search_covers(self, title: str, author: str, provider: str = AUDIBLE) -> list[dict]:
        data = await self._get("/api/search/covers", title=title, author=author, provider=provider)
        return data if isinstance(data, list) else data.get("results", [])

    async def set_cover_url(self, item_id: str, url: str) -> dict:
        item_id = _id_segment(item_id, "item_id")
        return await self._post(f"/api/items/{item_id}/cover", {"url": url})

    # ------------------------------------------------------------------
    # Library maintenance
    # ------------------------------------------------------------------

    async def scan_library(self, library_id: str) -> dict:
        library_id = _id_segment(library_id, "library_id")
        return await self._post(f"/api/libraries/{library_id}/scan")

    async def get_library_items_missing(self, library_id: str) -> list[dict]:
        items = await self.get_library_items(library_id)
        return [i for i in items if i.get("isMissing")]

    async def delete_item(self, item_id: str) -> dict:
        """Deletes an item *record* from ABS (used only for confirmed missing items)."""
        item_id = _id_segment(item_id, "item_id")
        return await self._delete(f"/api/items/{item_id}")

    async def create_backup(self) -> dict:
        return await self._post("/api/backups")

    # ------------------------------------------------------------------
    # Series helpers
    # ------------------------------------------------------------------

    async def get_series(self, library_id: str) -> list[dict]:
        library_id = _id_segment(library_id, "library_id")
        data = await self._get(f"/api/libraries/{library_id}/series", limit=0)
        return data.get("results", [])
