"""Helpers for building and filtering ABS metadata payloads."""

from __future__ import annotations

import time
from typing import Any


def build_metadata_payload(
    *,
    title: str | None = None,
    authors: list[str] | None = None,
    narrators: list[str] | None = None,
    series: list[dict] | None = None,
    genres: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Return supplied fields; tags must be moved to the media payload by the caller."""
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if authors is not None:
        payload["authors"] = [{"name": a} if isinstance(a, str) else a for a in authors]
    if narrators is not None:
        payload["narrators"] = narrators
    if series is not None:
        # series items: {"name": "...", "sequence": "..."} or {"id": "...", "sequence": "..."}
        payload["series"] = series
    if genres is not None:
        payload["genres"] = genres
    if tags is not None:
        payload["tags"] = tags
    return payload


class SeriesCache:
    """Maps library-scoped series names to ABS series ids."""

    def __init__(
        self,
        *,
        ttl_seconds: float | None = 300.0,
        clock: Any = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cache: dict[str, dict[str, str]] = {}
        self._seeded_at: dict[str, float] = {}

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower()

    def _is_expired(self, library_id: str) -> bool:
        if self._ttl_seconds is None:
            return False
        seeded_at = self._seeded_at.get(library_id)
        if seeded_at is None:
            return True
        return (self._clock() - seeded_at) >= self._ttl_seconds

    def has_library(self, library_id: str) -> bool:
        if library_id not in self._cache:
            return False
        if self._is_expired(library_id):
            self.invalidate(library_id)
            return False
        return True

    def seed(self, library_id: str, series_list: list[dict]) -> None:
        library_cache: dict[str, str] = {}
        for s in series_list:
            name = self._normalize(s.get("name") or "")
            if name and s.get("id"):
                library_cache[name] = s["id"]
        self._cache[library_id] = library_cache
        self._seeded_at[library_id] = self._clock()

    def resolve(self, library_id: str, name: str, sequence: str = "") -> dict:
        key = self._normalize(name)
        if self.has_library(library_id) and key in self._cache[library_id]:
            # ABS 2.x resolves series by name within the item's library. An ID
            # alone is rejected, even if it belongs to the correct library.
            return {
                "id": self._cache[library_id][key],
                "name": name.strip(),
                "sequence": sequence,
            }
        return {"name": name.strip(), "sequence": sequence}

    def add(self, library_id: str, name: str, series_id: str) -> None:
        if not self.has_library(library_id):
            self._cache[library_id] = {}
        self._cache[library_id][self._normalize(name)] = series_id
        self._seeded_at[library_id] = self._clock()

    def invalidate(self, library_id: str | None = None) -> None:
        if library_id is None:
            self._cache.clear()
            self._seeded_at.clear()
            return
        self._cache.pop(library_id, None)
        self._seeded_at.pop(library_id, None)
