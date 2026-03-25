from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_KIMCHI_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://kimchi-reader.app",
    "Referer": "https://kimchi-reader.app/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:148.0) "
        "Gecko/20100101 Firefox/148.0"
    ),
    "X-Kimchi-Client": "webapp",
}


class KimchiAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class KimchiBrowseCursor:
    last_row_id: str
    last_star_count: int | None = None
    last_complexity_score: str | None = None
    last_comprehension_percentage: str | None = None


class KimchiAPIClient:
    def __init__(
        self,
        timeout_seconds: int = 20,
        logger: logging.Logger | None = None,
        base_url: str = "https://api.kimchi-reader.app",
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._logger = logger or logging.getLogger(__name__)
        self._base_url = base_url.rstrip("/")

    def browse_items(
        self,
        cursor: KimchiBrowseCursor | None = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ordering": "stars",
            "exclude_non_first_episode": False,
            "sources": ["youtube_video"],
            "show_hidden": False,
            "last_comprehension_percentage": None,
        }
        if cursor is not None:
            payload["last_row_id"] = cursor.last_row_id
            payload["last_star_count"] = cursor.last_star_count
            payload["last_complexity_score"] = cursor.last_complexity_score
            payload["last_comprehension_percentage"] = cursor.last_comprehension_percentage
        return self._json_request(
            path="/v2/media/browse/item",
            method="POST",
            payload=payload,
        )

    def browse_channel_groups(
        self,
        cursor: KimchiBrowseCursor | None = None,
        *,
        min_stars: int = 1,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ordering": "stars",
            "sources": ["youtube_channel"],
        }
        if cursor is not None:
            payload["last_row_id"] = cursor.last_row_id
            payload["last_star_count"] = cursor.last_star_count
            payload["last_complexity_score"] = cursor.last_complexity_score
            payload["last_comprehension_percentage"] = cursor.last_comprehension_percentage
        response = self._json_request(
            path="/v2/media/browse/unified",
            method="POST",
            payload=payload,
        )
        items = response.get("items")
        if isinstance(items, list):
            response["items"] = [
                item
                for item in items
                if isinstance(item, Mapping) and _group_star_count(item) >= min_stars
            ]
        return response

    def get_media_item(self, kimchi_id: str) -> Dict[str, Any]:
        return self._json_request(
            path=f"/v2/media/item/{kimchi_id}",
            method="GET",
        )

    def _json_request(
        self,
        path: str,
        method: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        url = self._base_url + path
        body = None
        headers = dict(DEFAULT_KIMCHI_HEADERS)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        else:
            headers.pop("Content-Type", None)
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                response_bytes = response.read()
        except HTTPError as exc:
            raise KimchiAPIError(f"Kimchi API returned HTTP {exc.code} for {path}") from exc
        except URLError as exc:
            raise KimchiAPIError(f"Kimchi API request failed for {path}: {exc}") from exc
        except Exception as exc:
            raise KimchiAPIError(f"Unexpected Kimchi API failure for {path}: {exc}") from exc

        try:
            parsed = json.loads(response_bytes.decode("utf-8"))
        except Exception as exc:
            raise KimchiAPIError(f"Kimchi API returned invalid JSON for {path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise KimchiAPIError(f"Kimchi API returned unexpected payload type for {path}")
        return parsed


def youtube_source_id(payload: Mapping[str, Any]) -> str:
    for source in payload.get("sources") or []:
        if not isinstance(source, Mapping):
            continue
        if str(source.get("source_type", "")) == "youtube_video":
            value = str(source.get("value", "")).strip()
            if value:
                return value
    return ""


def youtube_channel_source_id(payload: Mapping[str, Any]) -> str:
    for source in payload.get("sources") or []:
        if not isinstance(source, Mapping):
            continue
        if str(source.get("source_type", "")) == "youtube_channel":
            value = str(source.get("value", "")).strip()
            if value:
                return value
    return ""


def latest_browse_cursor(items: Iterable[Mapping[str, Any]]) -> KimchiBrowseCursor | None:
    last_item = None
    for item in items:
        last_item = item
    if not isinstance(last_item, Mapping):
        return None
    last_row_id = str(last_item.get("id", "")).strip()
    if not last_row_id:
        return None
    stars = last_item.get("stars")
    complexity_score = last_item.get("complexity_score")
    comprehension = last_item.get("comprehension_percentage")
    return KimchiBrowseCursor(
        last_row_id=last_row_id,
        last_star_count=int(stars) if stars not in (None, "") else None,
        last_complexity_score=str(complexity_score) if complexity_score not in (None, "") else None,
        last_comprehension_percentage=(
            str(comprehension) if comprehension not in (None, "") else None
        ),
    )


def _group_star_count(payload: Mapping[str, Any]) -> int:
    stars = payload.get("stars")
    if stars in (None, ""):
        return 0
    try:
        return int(stars)
    except (TypeError, ValueError):
        return 0
