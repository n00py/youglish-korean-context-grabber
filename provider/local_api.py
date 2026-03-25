from __future__ import annotations

import json
from typing import List
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..config import AddonConfig
from .base import BaseContextProvider, ProviderError
from .models import ContextCandidate, ContextFetchRequest


class LocalCorpusProvider(BaseContextProvider):
    name = "local_api"
    display_name = "BanGlish"

    def __init__(self, config: AddonConfig) -> None:
        self._config = config

    def fetch_candidates(self, request: ContextFetchRequest) -> List[ContextCandidate]:
        query_string = urlencode(
            {
                "q": request.query,
                "limit": request.max_candidates,
                "exact_only": "true" if request.exact_match_only else "false",
                "max_chars": request.max_sentence_length,
            }
        )
        url = self._config.local_api_base_url.rstrip("/") + "/search?" + query_string
        req = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": self._config.user_agent,
            },
            method="GET",
        )
        try:
            with urlopen(req, timeout=self._config.local_api_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except URLError as exc:
            raise ProviderError(f"Local corpus API is unavailable: {exc}") from exc
        except Exception as exc:
            raise ProviderError(f"Local corpus API failed: {exc}") from exc
        items = payload.get("items") or []
        candidates: List[ContextCandidate] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_payload = dict(item.get("raw_payload") or {})
            matched_term = str(item.get("matched_term", "") or "")
            if not matched_term and request.query in str(item.get("sentence_text", "")):
                matched_term = request.query
            candidates.append(
                ContextCandidate(
                    sentence_text=str(item.get("sentence_text", "") or ""),
                    matched_term=matched_term,
                    source_title=str(item.get("source_title", "") or ""),
                    source_url=str(item.get("source_url", "") or ""),
                    timestamp=str(item.get("timestamp", "") or ""),
                    video_id=str(item.get("video_id", "") or ""),
                    provider_name=str(item.get("provider_name", self.display_name) or self.display_name),
                    raw_payload=raw_payload,
                )
            )
        return candidates
