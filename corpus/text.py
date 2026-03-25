from __future__ import annotations

import html
import json
import re
from collections import Counter
from typing import Iterable


TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
HANGUL_BASE = 0xAC00
HANGUL_END = 0xD7A3
NUM_JUNG = 21
NUM_JONG = 28


def clean_text(text: str) -> str:
    cleaned = TAG_RE.sub("", html.unescape(text or ""))
    return SPACE_RE.sub(" ", cleaned).strip()


def normalize_text(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", clean_text(text))


def tokenize_text(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(clean_text(text))]


def tokenized_text_blob(text: str) -> str:
    return " ".join(tokenize_text(text))


def split_search_tokens(query: str) -> list[str]:
    pieces = []
    current = []
    for character in query:
        if character.isalnum() or "\uac00" <= character <= "\ud7a3":
            current.append(character.lower())
            continue
        if current:
            pieces.append("".join(current))
            current = []
    if current:
        pieces.append("".join(current))
    return pieces


def expand_search_forms(query: str) -> list[str]:
    normalized_query = normalize_text(query)
    forms = {normalized_query} if normalized_query else set()
    tokens = [token for token in split_search_tokens(query) if token]
    for token in tokens:
        forms.add(normalize_text(token))
    if len(tokens) == 1:
        for form in _expand_korean_lemma_token(tokens[0]):
            normalized_form = normalize_text(form)
            if normalized_form:
                forms.add(normalized_form)
    return sorted(forms, key=lambda value: (-len(value), value))


def _expand_korean_lemma_token(token: str) -> set[str]:
    if not token.endswith("다") or len(token) < 2 or not all("\uac00" <= ch <= "\ud7a3" for ch in token):
        return {token}
    stem = token[:-1]
    has_batchim = _has_final_consonant(stem[-1]) if stem else False
    forms = {token, stem}
    forms.update(
        {
            stem + "고",
            stem + ("으면" if has_batchim else "면"),
            stem + ("으니까" if has_batchim else "니까"),
            stem + "지만",
            stem + "다가",
            stem + "기",
        }
    )
    if token == "하다":
        forms.update(
            {
                "해",
                "해요",
                "합니다",
                "했다",
                "했어",
                "했어요",
                "해서",
                "하면",
                "하고",
                "하니까",
                "하던",
                "하는",
                "한",
                "할",
            }
        )
        return forms

    polite = _conjugate_polite_yo(stem)
    if polite:
        forms.add(polite)
    past = _conjugate_past(stem)
    if past:
        forms.update({past, past + "요"})
    present_modifier = _apply_modifier(stem, final_jamo=4, suffix_if_batchim="은")
    future_modifier = _apply_modifier(stem, final_jamo=8, suffix_if_batchim="을")
    if present_modifier:
        forms.add(present_modifier)
    if future_modifier:
        forms.add(future_modifier)
    if stem:
        forms.add(stem + "는")
    return forms


def _conjugate_polite_yo(stem: str) -> str:
    last = stem[-1:]
    if not last or not _is_hangul_syllable(last):
        return stem + "어요"
    choseong, jungseong, jongseong = _decompose_syllable(last)
    if jongseong == 0:
        if jungseong in {0, 4}:  # ㅏ, ㅓ
            return stem + "요"
        if jungseong == 8:  # ㅗ
            return stem[:-1] + _compose_syllable(choseong, 9, 0) + "요"  # ㅘ
        if jungseong == 13:  # ㅜ
            return stem[:-1] + _compose_syllable(choseong, 14, 0) + "요"  # ㅝ
        if jungseong == 20:  # ㅣ
            return stem[:-1] + _compose_syllable(choseong, 6, 0) + "요"  # ㅕ
    ending = "아요" if jungseong in {0, 8} else "어요"
    return stem + ending


def _conjugate_past(stem: str) -> str:
    polite = _conjugate_polite_yo(stem)
    if polite.endswith("요"):
        polite = polite[:-1]
    if polite.endswith("어"):
        return polite[:-1] + "었어"
    if polite.endswith("아"):
        return polite[:-1] + "았어"
    return stem + "었어"


def _apply_modifier(stem: str, *, final_jamo: int, suffix_if_batchim: str) -> str:
    last = stem[-1:]
    if not last or not _is_hangul_syllable(last):
        return stem + suffix_if_batchim
    choseong, jungseong, jongseong = _decompose_syllable(last)
    if jongseong == 0:
        return stem[:-1] + _compose_syllable(choseong, jungseong, final_jamo)
    return stem + suffix_if_batchim


def _has_final_consonant(value: str) -> bool:
    if not _is_hangul_syllable(value):
        return False
    return _decompose_syllable(value)[2] != 0


def _is_hangul_syllable(value: str) -> bool:
    if len(value) != 1:
        return False
    codepoint = ord(value)
    return HANGUL_BASE <= codepoint <= HANGUL_END


def _decompose_syllable(value: str) -> tuple[int, int, int]:
    codepoint = ord(value) - HANGUL_BASE
    choseong = codepoint // (NUM_JUNG * NUM_JONG)
    jungseong = (codepoint % (NUM_JUNG * NUM_JONG)) // NUM_JONG
    jongseong = codepoint % NUM_JONG
    return choseong, jungseong, jongseong


def _compose_syllable(choseong: int, jungseong: int, jongseong: int) -> str:
    return chr(HANGUL_BASE + (((choseong * NUM_JUNG) + jungseong) * NUM_JONG) + jongseong)


def dedupe_terms(texts: Iterable[str]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(tokenize_text(text))
    return dict(counter)


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
