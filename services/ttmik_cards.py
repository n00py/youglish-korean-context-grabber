from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..provider.models import ContextCandidate
from .sound_field import sound_tag


TTMIK_NOTE_TYPE_NAME = "Talk To Me In Korean"
TTMIK_PREFERRED_DECK_NAMES = (
    "2. Super-Common Korean Sentences",
    "Common Sentences",
)


@dataclass(frozen=True)
class CreateTTMIKCardResult:
    success: bool
    message: str
    note_id: int = 0
    deck_name: str = ""
    note_type_name: str = TTMIK_NOTE_TYPE_NAME
    media_filename: str = ""


def create_ttmik_card(
    *,
    col: object,
    candidate: ContextCandidate,
    clip_path: Path,
    english_text: str,
) -> CreateTTMIKCardResult:
    if not clip_path.exists() or clip_path.stat().st_size <= 0:
        return CreateTTMIKCardResult(
            success=False,
            message="The extracted audio clip is missing.",
        )
    translation = (english_text or "").strip()
    if not translation:
        return CreateTTMIKCardResult(
            success=False,
            message="An English translation is required to create a TTMIK card.",
        )

    note_type = _note_type_by_name(col, TTMIK_NOTE_TYPE_NAME)
    if note_type is None:
        return CreateTTMIKCardResult(
            success=False,
            message=f"Could not find note type '{TTMIK_NOTE_TYPE_NAME}'.",
        )

    deck_id, deck_name = _resolve_target_deck(col)
    if not deck_id:
        return CreateTTMIKCardResult(
            success=False,
            message="Could not find the Common Sentences deck for TTMIK card creation.",
        )

    media_filename = _import_media_file(col, clip_path)
    if not media_filename:
        return CreateTTMIKCardResult(
            success=False,
            message="Could not import the extracted audio clip into Anki media.",
        )

    note = _new_note(col, note_type)
    if note is None:
        return CreateTTMIKCardResult(
            success=False,
            message="Could not create a new TTMIK note in this Anki build.",
        )

    _set_field_if_present(note, "English", translation)
    _set_field_if_present(note, "Korean", candidate.sentence_text.strip())
    _set_field_if_present(note, "Hint", "")
    _set_field_if_present(note, "Image Grid", "")
    _set_field_if_present(note, "Sound", sound_tag(media_filename))
    _set_field_if_present(note, "Extra", _extra_text(candidate))

    add_note = getattr(col, "add_note", None)
    if not callable(add_note):
        return CreateTTMIKCardResult(
            success=False,
            message="This Anki collection does not support adding notes here.",
        )
    try:
        add_note(note, int(deck_id))
    except Exception as exc:
        return CreateTTMIKCardResult(
            success=False,
            message=f"Could not add the TTMIK note: {exc}",
        )

    return CreateTTMIKCardResult(
        success=True,
        message=f"Created a TTMIK card in '{deck_name}'.",
        note_id=int(getattr(note, "id", 0) or 0),
        deck_name=deck_name,
        media_filename=media_filename,
    )


def _extra_text(candidate: ContextCandidate) -> str:
    parts = []
    provider = (candidate.provider_name or "").strip()
    if provider:
        parts.append(f"Source: {provider}")
    if candidate.source_title:
        parts.append(f"Title: {candidate.source_title}")
    if candidate.timestamp:
        parts.append(f"Timestamp: {candidate.timestamp}")
    if candidate.source_url:
        parts.append(f"Clip: {candidate.source_url}")
    raw_payload = candidate.raw_payload or {}
    youtube_url = str(raw_payload.get("youtube_url", "") or "").strip()
    if youtube_url:
        parts.append(f"YouTube: {youtube_url}")
    return "\n".join(parts).strip()


def _set_field_if_present(note: object, field_name: str, value: str) -> None:
    keys = getattr(note, "keys", None)
    if not callable(keys):
        return
    if field_name not in set(note.keys()):
        return
    note[field_name] = value


def _import_media_file(col: object, clip_path: Path) -> str:
    media = getattr(col, "media", None)
    add_file = getattr(media, "add_file", None)
    if not callable(add_file):
        return ""
    try:
        return str(add_file(str(clip_path)))
    except Exception:
        return ""


def _note_type_by_name(col: object, name: str):
    models = getattr(col, "models", None)
    if models is None:
        return None
    for method_name in ("by_name", "byName"):
        method = getattr(models, method_name, None)
        if callable(method):
            try:
                note_type = method(name)
            except Exception:
                note_type = None
            if note_type:
                return note_type
    return None


def _new_note(col: object, note_type: object):
    new_note = getattr(col, "new_note", None)
    if callable(new_note):
        try:
            return new_note(note_type)
        except TypeError:
            try:
                return new_note(notetype=note_type)
            except Exception:
                return None
        except Exception:
            return None
    try:
        from anki.notes import Note
    except Exception:
        return None
    try:
        return Note(col, note_type)
    except Exception:
        return None


def _resolve_target_deck(col: object) -> tuple[int, str]:
    decks = getattr(col, "decks", None)
    if decks is None:
        return 0, ""
    for deck_name in TTMIK_PREFERRED_DECK_NAMES:
        deck_id = _deck_id_for_name(decks, deck_name)
        if deck_id:
            return deck_id, deck_name

    all_names = _all_deck_names(decks)
    for deck_name in all_names:
        normalized = deck_name.lower()
        if "common" in normalized and "sentence" in normalized:
            deck_id = _deck_id_for_name(decks, deck_name)
            if deck_id:
                return deck_id, deck_name
    return 0, ""


def _deck_id_for_name(decks: object, deck_name: str) -> int:
    for method_name in ("id_for_name", "id"):
        method = getattr(decks, method_name, None)
        if callable(method):
            try:
                deck_id = method(deck_name)
            except Exception:
                continue
            if deck_id:
                try:
                    return int(deck_id)
                except Exception:
                    continue
    return 0


def _all_deck_names(decks: object) -> list[str]:
    names_and_ids = getattr(decks, "all_names_and_ids", None)
    if callable(names_and_ids):
        try:
            payload = names_and_ids()
        except Exception:
            payload = []
        names: list[str] = []
        for item in payload or []:
            if isinstance(item, tuple) and item:
                names.append(str(item[0]))
            elif isinstance(item, dict) and "name" in item:
                names.append(str(item["name"]))
        return names
    all_names = getattr(decks, "all_names", None)
    if callable(all_names):
        try:
            return [str(name) for name in all_names() or []]
        except Exception:
            return []
    return []
