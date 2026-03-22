from __future__ import annotations

import html
import re
from typing import Iterable, List

from ..config import AddonConfig
from ..provider.models import ContextCandidate


TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
BRACKET_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")


def normalize_text(text: str) -> str:
    cleaned = TAG_RE.sub("", html.unescape(text or ""))
    return SPACE_RE.sub(" ", cleaned).strip()


def contains_exact_match(query: str, sentence: str) -> bool:
    query = normalize_text(query)
    sentence = normalize_text(sentence)
    return bool(query) and query in sentence


def subtitle_noise_score(sentence: str) -> int:
    sentence = normalize_text(sentence)
    noise = len(BRACKET_RE.findall(sentence))
    noise += sentence.count('"') + sentence.count("'")
    noise += sentence.count("~") + sentence.count("^") + sentence.count("_")
    noise += sentence.count("♪") * 2 + sentence.count("♫") * 2
    noise += sentence.count("...") * 2
    return noise


def score_candidate(
    candidate: ContextCandidate,
    query: str,
    config: AddonConfig,
) -> float:
    sentence = normalize_text(candidate.sentence_text)
    exact_match_bonus = 100 if config.exact_match_bias and contains_exact_match(query, sentence) else 0
    length_score = max(0, 80 - max(len(sentence) - 24, 0))
    cleanliness_score = max(0, 25 - (subtitle_noise_score(sentence) * 4))
    metadata_score = sum(
        2 for value in (candidate.source_title, candidate.source_url, candidate.timestamp, candidate.video_id) if value
    )
    duplicate_penalty = 30 if candidate.duplicate_note_ids else 0
    candidate.score = float(exact_match_bonus + length_score + cleanliness_score + metadata_score - duplicate_penalty)
    return candidate.score


def apply_candidate_filters(
    candidates: Iterable[ContextCandidate],
    query: str,
    config: AddonConfig,
) -> List[ContextCandidate]:
    filtered: List[ContextCandidate] = []
    for candidate in candidates:
        sentence = normalize_text(candidate.sentence_text)
        if not sentence:
            continue
        if len(sentence) > config.max_sentence_length:
            continue
        if config.exact_match_only and not contains_exact_match(query, sentence):
            continue
        filtered.append(candidate)
    return filtered


def rank_candidates(
    candidates: Iterable[ContextCandidate],
    query: str,
    config: AddonConfig,
) -> List[ContextCandidate]:
    prepared = apply_candidate_filters(candidates, query, config)
    for candidate in prepared:
        score_candidate(candidate, query, config)
    prepared.sort(
        key=lambda candidate: (-candidate.score, len(normalize_text(candidate.sentence_text)))
    )
    return prepared
