from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Dict, List
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

from .base import BaseContextProvider, ProviderError
from .models import ContextCandidate, ContextFetchRequest


JSON_DATA_PATTERN = re.compile(
    r"params\.jsonData\s*=\s*'((?:\\.|[^'])*)';",
    re.DOTALL,
)
VIDEO_DISPLAY_PATTERNS = (
    re.compile(r'video\.display\s*=\s*"((?:\\.|[^"])*)";', re.DOTALL),
    re.compile(r"video\.display\s*=\s*'((?:\\.|[^'])*)';", re.DOTALL),
)


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _decode_js_escape(match: re.Match[str]) -> str:
    try:
        return chr(int(match.group(1), 16))
    except Exception:
        return match.group(0)


def _decode_youglish_text(text: object) -> str:
    value = html.unescape(str(text or ""))
    if not value:
        return ""
    for _ in range(3):
        updated = re.sub(r"%u([0-9A-Fa-f]{4})", _decode_js_escape, value)
        updated = unquote(updated)
        if updated == value:
            break
        value = updated
    value = value.replace("[[[", "").replace("]]]", "")
    return _collapse_spaces(value)


def _format_timestamp(raw_seconds: str | int | float | None) -> str:
    if raw_seconds in (None, ""):
        return ""
    try:
        total_seconds = int(float(raw_seconds))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _youglish_clip_url(query: str, cid: str) -> str:
    return f"https://youglish.com/getbyid/{cid}/{quote(query, safe='')}/korean/all"


@dataclass(frozen=True)
class FallbackSettings:
    timeout_seconds: int
    user_agent: str


class OptionalScrapeFallbackProvider(BaseContextProvider):
    name = "scrape_fallback"
    display_name = "YouGlish"

    def __init__(self, settings: FallbackSettings) -> None:
        self._settings = settings

    def fetch_candidates(self, request: ContextFetchRequest) -> List[ContextCandidate]:
        url = f"https://youglish.com/pronounce/{quote(request.query, safe='')}/korean"
        response_html = self._fetch_html(url)
        payload = self._extract_bootstrap_payload(response_html)
        rows = payload.get("results") or []
        candidates: List[ContextCandidate] = []
        for row in rows:
            clip_url = (
                _youglish_clip_url(request.query, str(row.get("cid", "")))
                if row.get("cid")
                else url
            )
            sentence = self._fetch_exact_clip_transcript(clip_url) or _decode_youglish_text(
                row.get("display", "")
            )
            if not sentence:
                continue
            candidate = ContextCandidate(
                sentence_text=sentence,
                matched_term=request.query if request.query in sentence else "",
                source_title="",
                source_url=clip_url,
                timestamp=_format_timestamp(row.get("start")),
                video_id=str(row.get("vid", "") or ""),
                provider_name=self.display_name,
                raw_payload=dict(row),
            )
            candidates.append(candidate)
            if len(candidates) >= request.max_candidates:
                break
        return candidates

    def _fetch_html(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": self._settings.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise ProviderError(f"Fallback fetch failed: {exc}") from exc

    def _extract_bootstrap_payload(self, response_html: str) -> Dict[str, object]:
        match = JSON_DATA_PATTERN.search(response_html)
        if not match:
            raise ProviderError("Could not locate YouGlish bootstrap payload.")
        try:
            raw_json = bytes(match.group(1), "utf-8").decode("unicode_escape")
            payload = json.loads(raw_json)
        except Exception as exc:
            raise ProviderError(f"Bootstrap payload parse failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Unexpected YouGlish bootstrap payload type.")
        return payload

    def _fetch_exact_clip_transcript(self, clip_url: str) -> str:
        try:
            clip_html = self._fetch_html(clip_url)
        except Exception:
            return ""
        for pattern in VIDEO_DISPLAY_PATTERNS:
            match = pattern.search(clip_html)
            if match:
                return _decode_youglish_text(match.group(1))
        try:
            payload = self._extract_bootstrap_payload(clip_html)
        except Exception:
            return ""
        cid_track = payload.get("cid_track")
        if isinstance(cid_track, dict):
            return _decode_youglish_text(cid_track.get("display", ""))
        return ""
