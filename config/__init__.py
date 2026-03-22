from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Tuple

MIN_CANDIDATES = 3
MAX_CANDIDATES = 20


@dataclass(frozen=True)
class DestinationFieldMapping:
    sentence: str = "Context Sentence"
    source: str = "Context Source"
    url: str = "Context URL"
    timestamp: str = "Context Timestamp"
    translation: str = "Context Translation"

    def as_dict(self) -> Dict[str, str]:
        return {
            "sentence": self.sentence,
            "source": self.source,
            "url": self.url,
            "timestamp": self.timestamp,
            "translation": self.translation,
        }


@dataclass(frozen=True)
class AddonConfig:
    source_field_name: str = "Korean"
    sound_field_name: str = "Sound"
    destination_fields: DestinationFieldMapping = field(
        default_factory=DestinationFieldMapping
    )
    max_candidates: int = 5
    overwrite_existing: bool = False
    protected_fields: Tuple[str, ...] = (
        "Context Sentence",
        "Context Source",
        "Context URL",
        "Context Timestamp",
        "Context Translation",
    )
    exact_match_bias: bool = True
    exact_match_only: bool = False
    max_sentence_length: int = 120
    duplicate_detection_enabled: bool = True
    provider_order: Tuple[str, ...] = ("scrape_fallback", "youglish_widget")
    request_timeout_seconds: int = 12
    user_agent: str = "Anki YouGlish Korean Context Grabber/0.1"

    @property
    def effective_max_candidates(self) -> int:
        return clamp_max_candidates(self.max_candidates)

    def effective_max_candidates_for(self, requested_max_candidates: int | None) -> int:
        if requested_max_candidates is None:
            return self.effective_max_candidates
        return clamp_max_candidates(requested_max_candidates)


def clamp_max_candidates(value: int | str | float | None) -> int:
    try:
        numeric_value = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        numeric_value = 5
    return min(max(numeric_value, MIN_CANDIDATES), MAX_CANDIDATES)


def _tuple_from_iterable(value: Any, fallback: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return tuple(fallback)


def config_from_dict(payload: Mapping[str, Any] | None) -> AddonConfig:
    payload = payload or {}
    destination_payload = payload.get("destination_fields") or {}
    defaults = DestinationFieldMapping()
    destination_fields = DestinationFieldMapping(
        sentence=str(destination_payload.get("sentence", defaults.sentence)),
        source=str(destination_payload.get("source", defaults.source)),
        url=str(destination_payload.get("url", defaults.url)),
        timestamp=str(destination_payload.get("timestamp", defaults.timestamp)),
        translation=str(destination_payload.get("translation", defaults.translation)),
    )
    config = AddonConfig(
        source_field_name=str(payload.get("source_field_name", "Korean")),
        sound_field_name=str(payload.get("sound_field_name", "Sound")),
        destination_fields=destination_fields,
        max_candidates=int(payload.get("max_candidates", 5)),
        overwrite_existing=bool(payload.get("overwrite_existing", False)),
        protected_fields=_tuple_from_iterable(
            payload.get("protected_fields"), AddonConfig().protected_fields
        ),
        exact_match_bias=bool(payload.get("exact_match_bias", True)),
        exact_match_only=bool(payload.get("exact_match_only", False)),
        max_sentence_length=int(payload.get("max_sentence_length", 120)),
        duplicate_detection_enabled=bool(
            payload.get("duplicate_detection_enabled", True)
        ),
        provider_order=_tuple_from_iterable(
            payload.get("provider_order"), AddonConfig().provider_order
        ),
        request_timeout_seconds=int(payload.get("request_timeout_seconds", 12)),
        user_agent=str(
            payload.get(
                "user_agent",
                "Anki YouGlish Korean Context Grabber/0.1",
            )
        ),
    )
    return config
