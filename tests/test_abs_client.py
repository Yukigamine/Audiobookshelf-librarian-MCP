"""Mocked ABS API client tests."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from abs_librarian.abs_client import ABSClient

BASE = "http://abs.local"
TOKEN = "test-token"


@pytest.fixture()
def client():
    return ABSClient(BASE, TOKEN)


@pytest.mark.asyncio
async def test_get_libraries(client):
    mock_resp = {"libraries": [{"id": "lib1", "name": "Audiobooks"}]}
    mock_response = MagicMock()
    mock_response.json.return_value = mock_resp
    mock_response.raise_for_status = lambda: None
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        result = await client.get_libraries()
    assert result == [{"id": "lib1", "name": "Audiobooks"}]


@pytest.mark.asyncio
async def test_batch_update_chunks(client):
    """batch_update must split into chunks of 100."""
    items = [{"id": str(i)} for i in range(250)]
    posted_chunks = []

    async def fake_post(path, body=None):
        posted_chunks.append((path, body))
        return {"success": True, "updates": len(body)}

    with patch.object(client, "_post", new=AsyncMock(side_effect=fake_post)) as mock_post:
        result = await client.batch_update(items)

    assert len(posted_chunks) == 3  # 100 + 100 + 50
    assert [path for path, _ in posted_chunks] == ["/api/items/batch/update"] * 3
    assert [len(body) for _, body in posted_chunks] == [100, 100, 50]
    assert result == {
        "updated": 250,
        "results": [
            {"success": True, "updates": 100},
            {"success": True, "updates": 100},
            {"success": True, "updates": 50},
        ],
    }
    assert mock_post.await_count == 3


@pytest.mark.asyncio
async def test_set_cover_url(client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True}
    mock_response.content = b'{"success":true}'
    mock_response.raise_for_status = lambda: None
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await client.set_cover_url("item1", "http://example.com/cover.jpg")
    assert result == {"success": True}


ID_METHODS = [
    ("get_item", (), "item_id", "GET", "/api/items/", "", {"expanded": "1"}),
    ("patch_item_metadata", ({"title": "Book"},), "item_id", "PATCH", "/api/items/", "/media", {}),
    (
        "set_cover_url",
        ("https://covers.local/book.jpg",),
        "item_id",
        "POST",
        "/api/items/",
        "/cover",
        {},
    ),
    ("delete_item", (), "item_id", "DELETE", "/api/items/", "", {}),
    ("get_library_items", (), "library_id", "GET", "/api/libraries/", "/items", {"limit": "5000"}),
    (
        "get_library_items_missing",
        (),
        "library_id",
        "GET",
        "/api/libraries/",
        "/items",
        {"limit": "5000"},
    ),
    ("scan_library", (), "library_id", "POST", "/api/libraries/", "/scan", {}),
    ("get_series", (), "library_id", "GET", "/api/libraries/", "/series", {"limit": "0"}),
]


def test_library_items_limit_must_be_positive():
    with pytest.raises(ValueError, match="library_items_limit must be greater than 0"):
        ABSClient(BASE, TOKEN, library_items_limit=0)
INVALID_IDS = [
    "",
    " ",
    ".",
    "..",
    "/",
    "\\",
    "../backups",
    "item/../../backups",
    "item\\..\\backups",
    "item?expanded=0&admin=true",
    "item#fragment",
    "%2e%2e",
    "%2E%2E%2Fbackups",
    "item%2fmedia",
    "item%5Cmedia",
    "item%3Fexpanded=0",
    "item%23fragment",
    "%252e%252e%252fbackups",
    "%25%32%65",
    "item%",
    "item\n",
    "\titem",
    "item\r",
    "item\x00",
    "item\x1f",
    "item\x7f",
    "item\x85",
    None,
    123,
    [],
    {},
]


@pytest.mark.parametrize("bad_id", INVALID_IDS)
@pytest.mark.parametrize("method,args,name,verb,prefix,suffix,params", ID_METHODS)
async def test_invalid_path_ids_fail_before_client_creation(
    client, bad_id, method, args, name, verb, prefix, suffix, params
):
    with patch("httpx.AsyncClient") as http_client:
        with pytest.raises(ValueError, match=f"Invalid {name}:"):
            await getattr(client, method)(bad_id, *args)
        http_client.assert_not_called()


@pytest.fixture()
def requests():
    """Exercise HTTPX URL preparation while keeping all requests in memory."""
    captured = []

    def handle(request):
        captured.append(request)
        if request.url.path == "/api/items/batch/update":
            return httpx.Response(
                200, json={"success": True, "updates": len(json.loads(request.content))}
            )
        return httpx.Response(
            200, json={"success": True, "results": [{"id": "item1", "isMissing": True}]}
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)

    def make_client(**kwargs):
        return real_client(transport=transport, **kwargs)

    with patch("httpx.AsyncClient", side_effect=make_client):
        yield captured


@pytest.mark.parametrize(
    "valid_id,encoded",
    [
        ("f5b7f7aa-849d-4b37-84b5-fde57330c9fb", "f5b7f7aa-849d-4b37-84b5-fde57330c9fb"),
        ("li_8g04in7qv3h4q4sjhx", "li_8g04in7qv3h4q4sjhx"),
        ("lib_p8k4z7xvn4cxs9q9dz", "lib_p8k4z7xvn4cxs9q9dz"),
        ("item1", "item1"),
        ("opaque..id", "opaque..id"),
        ("opaque&key=value:+@", "opaque%26key%3Dvalue%3A%2B%40"),
        ("book name-é", "book%20name-%C3%A9"),
    ],
)
@pytest.mark.parametrize("method,args,name,verb,prefix,suffix,params", ID_METHODS)
async def test_valid_ids_stay_in_one_segment(
    client, requests, valid_id, encoded, method, args, name, verb, prefix, suffix, params
):
    result = await getattr(client, method)(valid_id, *args)

    assert len(requests) == 1
    request = requests[0]
    expected_path = f"{prefix}{encoded}{suffix}"
    expected_query = str(httpx.QueryParams(params))
    assert request.url.raw_path == (
        expected_path + (f"?{expected_query}" if expected_query else "")
    ).encode("ascii")
    assert request.method == verb
    assert request.url.host == "abs.local"
    assert request.url.fragment == ""
    assert dict(request.url.params) == params
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    if method == "patch_item_metadata":
        assert json.loads(request.content) == {"metadata": args[0]}
    elif method == "set_cover_url":
        assert json.loads(request.content) == {"url": args[0]}
    elif method in {"get_library_items", "get_library_items_missing", "get_series"}:
        assert result == [{"id": "item1", "isMissing": True}]
    else:
        assert result["success"] is True


@pytest.mark.parametrize("bad_id", INVALID_IDS)
@pytest.mark.parametrize("method", ["batch_update", "batch_quickmatch"])
async def test_invalid_batch_ids_fail_before_any_request(client, bad_id, method):
    # Put the invalid ID after a full chunk to prevent partial batch updates.
    ids = ["li_8g04in7qv3h4q4sjhx"] * 100 + [bad_id]
    payload = [{"id": item_id} for item_id in ids] if method == "batch_update" else ids
    with patch("httpx.AsyncClient") as http_client:
        with pytest.raises(ValueError, match="Invalid item_id:"):
            await getattr(client, method)(payload)
        http_client.assert_not_called()


async def test_batch_update_missing_id_fails_before_request(client):
    with patch("httpx.AsyncClient") as http_client:
        with pytest.raises(ValueError, match="Invalid item_id:"):
            await client.batch_update([{"mediaPayload": {"metadata": {"title": "Book"}}}])
        http_client.assert_not_called()


async def test_batch_ids_remain_raw_in_json(client, requests):
    ids = [
        "f5b7f7aa-849d-4b37-84b5-fde57330c9fb",
        "li_8g04in7qv3h4q4sjhx",
        "opaque&key=value:+@",
    ]
    updates = [{"id": item_id, "mediaPayload": {"metadata": {"title": "Book"}}} for item_id in ids]

    await client.batch_update(updates)
    await client.batch_quickmatch(ids, "google", override_cover=True, override_details=True)

    assert len(requests) == 2
    assert requests[0].url.raw_path == b"/api/items/batch/update"
    assert json.loads(requests[0].content) == updates
    assert requests[1].url.raw_path == b"/api/items/batch/quickmatch"
    assert json.loads(requests[1].content) == {
        "options": {"provider": "google", "overrideCover": True, "overrideDetails": True},
        "libraryItemIds": ids,
    }


async def test_get_library_items_uses_configured_limit(requests):
    client = ABSClient(BASE, TOKEN, library_items_limit=123)

    await client.get_library_items("lib1")

    assert len(requests) == 1
    assert dict(requests[0].url.params) == {"limit": "123"}
