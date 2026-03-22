from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Sequence

from ..config import AddonConfig
from ..provider.base import BaseContextProvider, ProviderError
from ..provider.models import ContextCandidate, ContextFetchRequest
from ..provider.scrape_fallback import FallbackSettings, OptionalScrapeFallbackProvider
from ..provider.widget_provider import YouGlishProvider
from .duplicates import find_duplicate_note_ids
from .ranking import normalize_text, rank_candidates


class ContextServiceError(RuntimeError):
    pass


class YouGlishContextService:
    def __init__(self, config: AddonConfig, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)

    def fetch_candidates(
        self,
        query: str,
        col: object | None = None,
        ignore_note_id: int | None = None,
        max_candidates_override: int | None = None,
    ) -> List[ContextCandidate]:
        normalized_query = normalize_text(query)
        if not normalized_query:
            raise ContextServiceError("The configured source field is empty.")
        requested_max_candidates = self._config.effective_max_candidates_for(max_candidates_override)
        request = ContextFetchRequest(
            query=normalized_query,
            max_candidates=requested_max_candidates,
            exact_match_only=self._config.exact_match_only,
            max_sentence_length=self._config.max_sentence_length,
        )
        candidates: Sequence[ContextCandidate] = ()
        errors: List[str] = []
        for provider in self._providers():
            try:
                provider_candidates = provider.fetch_candidates(request)
            except ProviderError as exc:
                self._logger.warning("%s failed for %r: %s", provider.name, normalized_query, exc)
                errors.append(f"{provider.name}: {exc}")
                continue
            except Exception as exc:
                self._logger.exception("%s crashed for %r", provider.name, normalized_query)
                errors.append(f"{provider.name}: {exc}")
                continue
            if provider_candidates:
                candidates = provider_candidates
                break
        if not candidates and errors:
            raise ContextServiceError("; ".join(errors))
        prepared = self._deduplicate(candidates)
        if self._config.duplicate_detection_enabled and col is not None:
            for candidate in prepared:
                candidate.duplicate_note_ids = find_duplicate_note_ids(
                    col,
                    candidate.sentence_text,
                    ignore_note_id=ignore_note_id,
                )
        ranked = rank_candidates(prepared, normalized_query, self._config)
        return ranked[:requested_max_candidates]

    def _providers(self) -> Iterable[BaseContextProvider]:
        provider_map: Dict[str, BaseContextProvider] = {
            "youglish_widget": YouGlishProvider(),
            "scrape_fallback": OptionalScrapeFallbackProvider(
                FallbackSettings(
                    timeout_seconds=self._config.request_timeout_seconds,
                    user_agent=self._config.user_agent,
                )
            ),
        }
        for provider_name in self._config.provider_order:
            provider = provider_map.get(provider_name)
            if provider is not None:
                yield provider

    def _deduplicate(
        self, candidates: Sequence[ContextCandidate]
    ) -> List[ContextCandidate]:
        deduplicated: List[ContextCandidate] = []
        seen = set()
        for candidate in candidates:
            key = (
                normalize_text(candidate.sentence_text),
                candidate.video_id,
                candidate.timestamp,
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(candidate)
        return deduplicated
