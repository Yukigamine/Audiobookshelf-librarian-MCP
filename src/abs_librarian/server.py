"""Audiobookshelf Librarian MCP server entry point."""

from __future__ import annotations

import logging
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .abs_client import AUDIBLE, ABSClient
from .audit import AuditLogError
from .config import Config
from .fs_tools import (
    detect_blobs,
    fs_flatten,
    fs_make_book_folders,
    fs_move,
    fs_quarantine,
    fs_tree,
)
from .jail import PathJailError
from .metadata import SeriesCache, build_metadata_payload
from .url_security import validate_cover_url

cfg = Config.from_env()
_series_cache = SeriesCache()
_TITLE_REGEX_MAX_LENGTH = 256
_TITLE_MATCH_MAX_LENGTH = 1024
_BACKREFERENCE_RE = re.compile(r"\\[1-9][0-9]*|\(\?P=[^)]+\)")
_NESTED_QUANTIFIER_RE = re.compile(
    r"\((?:\?[:=!]|"
    r"\?<[=!])?"
    r"(?:[^()\\]|\\.)*"
    r"(?:\*|\+|\{\d+(?:,\d*)?\})"
    r"(?:[^()\\]|\\.)*"
    r"\)"
    r"(?:\*|\+|\{\d+(?:,\d*)?\})"
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "Audiobookshelf Librarian",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=cfg.trusted_hosts,
        # Preserve the SDK's localhost Origin policy independently of LAN Host trust.
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    ),
)


def _client() -> ABSClient:
    return ABSClient(
        cfg.abs_url,
        cfg.abs_token,
        library_items_limit=cfg.abs_library_items_limit,
    )


def _permitted() -> list[str]:
    return cfg.library_roots


def _strip_character_classes(pattern: str) -> str:
    parts: list[str] = []
    in_class = False
    escaped = False
    for char in pattern:
        if escaped:
            if not in_class:
                parts.append(f"\\{char}")
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if in_class:
            if char == "]":
                in_class = False
            continue
        if char == "[":
            in_class = True
            continue
        parts.append(char)
    if escaped:
        parts.append("\\")
    return "".join(parts)


def _compile_title_regex(title_regex: str) -> re.Pattern[str]:
    if len(title_regex) > _TITLE_REGEX_MAX_LENGTH:
        raise ValueError(
            f"Invalid title_regex: pattern exceeds {_TITLE_REGEX_MAX_LENGTH} characters."
        )
    try:
        pattern = re.compile(title_regex, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid title_regex: {exc}") from exc

    simplified = _strip_character_classes(title_regex)
    if _BACKREFERENCE_RE.search(simplified) or _NESTED_QUANTIFIER_RE.search(simplified):
        raise ValueError(
            "Invalid title_regex: nested quantifiers and backreferences are not allowed."
        )
    return pattern
def _resolve_dry_run(dry_run: bool | None, confirm: bool | None = None) -> bool:
    if dry_run is not None and confirm is not None and dry_run != (not confirm):
        raise ValueError("dry_run conflicts with confirm")
    if dry_run is not None:
        return dry_run
    if confirm is not None:
        return not confirm
    return cfg.dry_run_default


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@mcp.tool()
async def health() -> dict:
    """Return server status and ABS connectivity."""
    client = _client()
    try:
        libs = await client.get_libraries()
        return {"status": "ok", "abs_libraries": len(libs)}
    except Exception:
        logger.exception("Audiobookshelf health check failed")
        return {"status": "error", "detail": "Audiobookshelf connectivity failed"}


# ---------------------------------------------------------------------------
# ABS: Library overview
# ---------------------------------------------------------------------------

@mcp.tool()
async def library_overview() -> dict:
    """Return compact counts per library: items, series, authors, missing."""
    client = _client()
    libs = await client.get_libraries()
    result = []
    for lib in libs:
        items = await client.get_library_items(lib["id"])
        authors: set[str] = set()
        series: set[str] = set()
        no_cover = 0
        no_series = 0
        missing = 0
        for item in items:
            media = item.get("media", {})
            meta = media.get("metadata", {})
            if item.get("isMissing"):
                missing += 1
            if not media.get("coverPath"):
                no_cover += 1
            series_name = (meta.get("seriesName") or "").strip()
            if not series_name:
                no_series += 1
            else:
                # seriesName may be "Series Name #N" — strip sequence for unique count
                series.add(series_name.rsplit(" #", 1)[0].strip())
            for a in (meta.get("authorName") or "").split(","):
                a = a.strip()
                if a:
                    authors.add(a)
        result.append({
            "id": lib["id"],
            "name": lib["name"],
            "items": len(items),
            "authors": len(authors),
            "series": len(series),
            "no_cover": no_cover,
            "no_series": no_series,
            "missing": missing,
        })
    return {"libraries": result}


# ---------------------------------------------------------------------------
# ABS: find_items
# ---------------------------------------------------------------------------

@mcp.tool()
async def find_items(
    library_id: str,
    title_regex: str | None = None,
    author: str | None = None,
    series: str | None = None,
    no_cover: bool = False,
    no_series: bool = False,
    missing: bool = False,
    min_duration_hours: float | None = None,
    max_duration_hours: float | None = None,
    min_file_count: int | None = None,
    max_file_count: int | None = None,
    limit: int = 200,
) -> dict:
    """Search library items with optional filters. Returns compact results."""
    pattern = _compile_title_regex(title_regex) if title_regex else None
    client = _client()
    items = await client.get_library_items(library_id)
    results = []

    for item in items:
        media = item.get("media", {})
        meta = media.get("metadata", {})

        if missing and not item.get("isMissing"):
            continue
        if no_cover and media.get("coverPath"):
            continue
        item_series_name = (meta.get("seriesName") or "").strip()
        if no_series and item_series_name:
            continue
        if series and series.lower() not in item_series_name.lower():
            continue
        if author:
            author_name = (meta.get("authorName") or "").lower()
            if author.lower() not in author_name:
                continue
        title = meta.get("title") or item.get("path", "").split("/")[-1]
        if pattern and not pattern.search(title[:_TITLE_MATCH_MAX_LENGTH]):
            continue

        duration = media.get("duration") or 0
        duration_hours = duration / 3600
        if min_duration_hours is not None and duration_hours < min_duration_hours:
            continue
        if max_duration_hours is not None and duration_hours > max_duration_hours:
            continue

        files = media.get("audioFiles") or media.get("tracks") or []
        file_count = len(files)
        if min_file_count is not None and file_count < min_file_count:
            continue
        if max_file_count is not None and file_count > max_file_count:
            continue

        author_str = meta.get("authorName") or ""
        parsed_authors = [a.strip() for a in author_str.split(",") if a.strip()]
        parsed_series = [{"name": item_series_name}] if item_series_name else []
        results.append({
            "id": item["id"],
            "title": title,
            "authors": parsed_authors,
            "series": parsed_series,
            "duration_hours": round(duration_hours, 2),
            "file_count": file_count,
            "path": item.get("path"),
            "has_cover": bool(item.get("media", {}).get("coverPath")),
            "is_missing": item.get("isMissing", False),
        })

        if len(results) >= limit:
            break

    return {"count": len(results), "items": results}


# ---------------------------------------------------------------------------
# ABS: get_item
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_item(item_id: str) -> dict:
    """Return full detail for a single item including file list."""
    client = _client()
    return await client.get_item(item_id)


# ---------------------------------------------------------------------------
# ABS: batch_update_metadata
# ---------------------------------------------------------------------------

@mcp.tool()
async def batch_update_metadata(
    library_id: str,
    updates: list[dict],
) -> dict:
    """
    Bulk-update metadata for multiple items.

    Each update dict: {id, title?, authors?, narrators?, series?, genres?, tags?}
    series items: {name, sequence?} — series names are cached to reuse existing ids.
    """
    client = _client()

    if not _series_cache.has_library(library_id):
        existing = await client.get_series(library_id)
        _series_cache.seed(library_id, existing)

    payloads = []
    series_updates_present = False
    for u in updates:
        item_id = u["id"]
        series_raw = u.get("series")
        resolved_series = None
        if series_raw is not None:
            series_updates_present = True
            resolved_series = [
                _series_cache.resolve(library_id, s["name"], s.get("sequence", ""))
                for s in series_raw
            ]
        meta = build_metadata_payload(
            title=u.get("title"),
            authors=u.get("authors"),
            narrators=u.get("narrators"),
            series=resolved_series,
            genres=u.get("genres"),
            tags=u.get("tags"),
        )
        media_payload = {"metadata": meta}
        if "tags" in meta:
            media_payload["tags"] = meta.pop("tags")
        payloads.append({"id": item_id, "mediaPayload": media_payload})

    results = await client.batch_update(payloads)
    if series_updates_present:
        _series_cache.invalidate(library_id)
        _series_cache.seed(library_id, await client.get_series(library_id))
    return {"requested": len(payloads), **results}


# ---------------------------------------------------------------------------
# ABS: quick_match
# ---------------------------------------------------------------------------

@mcp.tool()
async def quick_match(
    item_ids: list[str],
    provider: str = AUDIBLE,
    override_cover: bool = False,
    override_details: bool = False,
) -> dict:
    """Batch quick-match items against a metadata provider."""
    client = _client()
    result = await client.batch_quickmatch(item_ids, provider, override_cover, override_details)
    return {"matched": len(item_ids), "result": result}


# ---------------------------------------------------------------------------
# ABS: set_cover
# ---------------------------------------------------------------------------

@mcp.tool()
async def set_cover(
    item_id: str,
    url: str | None = None,
    search_title: str | None = None,
    search_author: str | None = None,
    provider: str = AUDIBLE,
) -> dict:
    """Set a cover from a URL, or search a provider and use the first result."""
    client = _client()
    if url:
        try:
            validate_cover_url(url, cfg.cover_url_allowed_hosts)
        except ValueError as exc:
            return {"error": str(exc), "item_id": item_id}
        result = await client.set_cover_url(item_id, url)
        return {"item_id": item_id, "source": "url", "result": result}
    if search_title:
        covers = await client.search_covers(search_title, search_author or "", provider)
        if not covers:
            return {"error": "no covers found", "item_id": item_id}
        first = covers[0]
        if isinstance(first, str):
            first_url = first
        else:
            first_url = first.get("image") or first.get("url")
        if not first_url:
            return {"error": "unexpected cover format", "raw": first}
        try:
            validate_cover_url(first_url, cfg.cover_url_allowed_hosts)
        except ValueError as exc:
            return {"error": str(exc), "item_id": item_id}
        result = await client.set_cover_url(item_id, first_url)
        return {"item_id": item_id, "source": "search", "cover_url": first_url, "result": result}
    return {"error": "provide url or search_title"}


# ---------------------------------------------------------------------------
# ABS: scan_library
# ---------------------------------------------------------------------------

@mcp.tool()
async def scan_library(library_id: str) -> dict:
    """Trigger a library scan."""
    client = _client()
    result = await client.scan_library(library_id)
    return {"library_id": library_id, "result": result}


# ---------------------------------------------------------------------------
# ABS: list_missing / purge_missing
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_missing(library_id: str) -> dict:
    """List items marked as missing in ABS."""
    client = _client()
    items = await client.get_library_items_missing(library_id)
    return {
        "count": len(items),
        "items": [
            {
                "id": i["id"],
                "path": i.get("path"),
                "title": i.get("media", {}).get("metadata", {}).get("title"),
            }
            for i in items
        ],
    }


@mcp.tool()
async def delete_item(
    item_id: str,
    dry_run: bool | None = None,
    confirm: bool | None = None,
) -> dict:
    """Delete a single ABS item record by ID (does NOT touch files)."""
    try:
        should_dry_run = _resolve_dry_run(dry_run, confirm)
    except ValueError as exc:
        return {"error": str(exc)}
    if should_dry_run:
        return {"dry_run": True, "would_delete": item_id}
    client = _client()
    try:
        await client.delete_item(item_id)
        return {"deleted": item_id}
    except Exception as exc:
        return {"error": str(exc), "item_id": item_id}


@mcp.tool()
async def purge_missing(
    library_id: str,
    dry_run: bool | None = None,
    confirm: bool | None = None,
) -> dict:
    """Delete ABS records for all missing items (does NOT touch files)."""
    client = _client()
    try:
        should_dry_run = _resolve_dry_run(dry_run, confirm)
    except ValueError as exc:
        return {"error": str(exc)}
    if should_dry_run:
        items = await client.get_library_items_missing(library_id)
        return {"dry_run": True, "would_purge": len(items), "items": [i["id"] for i in items]}
    items = await client.get_library_items_missing(library_id)
    deleted = []
    errors = []
    for item in items:
        try:
            await client.delete_item(item["id"])
            deleted.append(item["id"])
        except Exception as exc:
            errors.append({"id": item["id"], "error": str(exc)})
    return {"deleted": len(deleted), "errors": errors}


# ---------------------------------------------------------------------------
# ABS: create_backup
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_backup(
    dry_run: bool | None = None,
    confirm: bool | None = None,
) -> dict:
    """Trigger an ABS backup."""
    try:
        should_dry_run = _resolve_dry_run(dry_run, confirm)
    except ValueError as exc:
        return {"error": str(exc)}
    if should_dry_run:
        return {"dry_run": True, "would_create": "backup"}
    client = _client()
    return await client.create_backup()


# ---------------------------------------------------------------------------
# FS: fs_tree
# ---------------------------------------------------------------------------

@mcp.tool()
async def tool_fs_tree(path: str, max_depth: int = 3) -> dict:
    """Return a folder tree with audio-file counts (depth-limited)."""
    try:
        return fs_tree(path, _permitted(), max_depth)
    except PathJailError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# FS: detect_blobs
# ---------------------------------------------------------------------------

@mcp.tool()
async def tool_detect_blobs(
    path: str,
    hours_threshold: float | None = None,
    file_count_threshold: int | None = None,
) -> dict:
    """Heuristic scan for blob folders and duplicate suspects."""
    try:
        return detect_blobs(
            path,
            _permitted(),
            hours_threshold if hours_threshold is not None else cfg.blob_hours_threshold,
            file_count_threshold
            if file_count_threshold is not None
            else cfg.blob_file_count_threshold,
        )
    except PathJailError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# FS: fs_make_book_folders
# ---------------------------------------------------------------------------

@mcp.tool()
async def tool_fs_make_book_folders(
    path: str,
    dry_run: bool | None = None,
    confirm: bool | None = None,
) -> dict:
    """
    Split a blob folder: move each loose audio file into its own named subfolder.
    Omit dry_run to use DRY_RUN_DEFAULT; pass dry_run=False or confirm=True to execute.
    """
    try:
        return fs_make_book_folders(
            path,
            _permitted(),
            cfg.audit_log,
            confirm=not _resolve_dry_run(dry_run, confirm),
        )
    except (AuditLogError, PathJailError, ValueError) as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# FS: fs_flatten
# ---------------------------------------------------------------------------

@mcp.tool()
async def tool_fs_flatten(
    path: str,
    dry_run: bool | None = None,
    confirm: bool | None = None,
) -> dict:
    """
    Flatten disc/CD/part subfolders into the parent with prefixed filenames.
    Omit dry_run to use DRY_RUN_DEFAULT; pass dry_run=False or confirm=True to execute.
    """
    try:
        return fs_flatten(
            path,
            _permitted(),
            cfg.audit_log,
            confirm=not _resolve_dry_run(dry_run, confirm),
        )
    except (AuditLogError, PathJailError, ValueError) as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# FS: fs_move
# ---------------------------------------------------------------------------

@mcp.tool()
async def tool_fs_move(
    src: str,
    dest: str,
    dry_run: bool | None = None,
    confirm: bool | None = None,
) -> dict:
    """
    Move a file or folder within the library.
    No overwrite; creates parent dirs.
    Omit dry_run to use DRY_RUN_DEFAULT; pass dry_run=False or confirm=True to execute.
    """
    try:
        return fs_move(
            src,
            dest,
            _permitted(),
            cfg.audit_log,
            confirm=not _resolve_dry_run(dry_run, confirm),
        )
    except (PathJailError, ValueError) as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# FS: fs_quarantine
# ---------------------------------------------------------------------------

@mcp.tool()
async def tool_fs_quarantine(
    path: str,
    dry_run: bool | None = None,
    confirm: bool | None = None,
) -> dict:
    """
    Move a file or folder to the quarantine directory (preserves structure).
    Nothing is deleted.
    Omit dry_run to use DRY_RUN_DEFAULT; pass dry_run=False or confirm=True to execute.
    """
    try:
        return fs_quarantine(
            path,
            _permitted(),
            cfg.quarantine_dir,
            cfg.audit_log,
            confirm=not _resolve_dry_run(dry_run, confirm),
        )
    except (PathJailError, ValueError) as e:
        return {"error": str(e)}
