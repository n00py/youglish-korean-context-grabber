from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class ContextFetchRequest:
    query: str
    max_candidates: int
    exact_match_only: bool
    max_sentence_length: int


@dataclass
class ContextCandidate:
    sentence_text: str
    matched_term: str = ""
    source_title: str = ""
    source_url: str = ""
    timestamp: str = ""
    video_id: str = ""
    provider_name: str = ""
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    duplicate_note_ids: Tuple[int, ...] = ()
