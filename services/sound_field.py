from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppendSoundResult:
    success: bool
    field_name: str
    media_filename: str = ""
    sound_tag: str = ""
    message: str = ""


def note_has_field(note: object, field_name: str) -> bool:
    keys = getattr(note, "keys", None)
    if not callable(keys):
        return False
    return field_name in set(note.keys())


def sound_tag(media_filename: str) -> str:
    return f"[sound:{media_filename}]"


def append_sound_tag_text(existing_value: str, tag: str) -> tuple[str, bool]:
    existing = existing_value or ""
    if tag in existing:
        return existing, False
    if not existing.strip():
        return tag, True
    if existing.endswith("\n"):
        return existing + tag, True
    return existing.rstrip() + "\n" + tag, True


def append_clip_to_note_field(
    *,
    note: object,
    clip_path: Path,
    col: object,
    field_name: str = "Sound",
) -> AppendSoundResult:
    if not note_has_field(note, field_name):
        return AppendSoundResult(
            success=False,
            field_name=field_name,
            message=f"Field '{field_name}' was not found on this note.",
        )
    if not clip_path.exists() or clip_path.stat().st_size <= 0:
        return AppendSoundResult(
            success=False,
            field_name=field_name,
            message="The extracted audio clip does not exist anymore.",
        )

    media = getattr(col, "media", None)
    add_file = getattr(media, "add_file", None)
    if not callable(add_file):
        return AppendSoundResult(
            success=False,
            field_name=field_name,
            message="Anki media import is not available in this collection.",
        )

    media_filename = str(add_file(str(clip_path)))
    tag = sound_tag(media_filename)
    updated_value, changed = append_sound_tag_text(str(note[field_name]), tag)
    if not changed:
        return AppendSoundResult(
            success=False,
            field_name=field_name,
            media_filename=media_filename,
            sound_tag=tag,
            message=f"This clip is already in '{field_name}'.",
        )

    note[field_name] = updated_value
    update_note = getattr(col, "update_note", None)
    flush = getattr(note, "flush", None)
    try:
        if callable(update_note):
            update_note(note)
        elif callable(flush):
            flush()
        else:
            return AppendSoundResult(
                success=False,
                field_name=field_name,
                media_filename=media_filename,
                sound_tag=tag,
                message="Anki note saving is not available in this context.",
            )
    except Exception as exc:
        return AppendSoundResult(
            success=False,
            field_name=field_name,
            media_filename=media_filename,
            sound_tag=tag,
            message=f"Could not save the note update: {exc}",
        )

    note_id = getattr(note, "id", 0)
    get_note = getattr(col, "get_note", None)
    if note_id and callable(get_note):
        try:
            persisted_note = get_note(int(note_id))
        except Exception as exc:
            return AppendSoundResult(
                success=False,
                field_name=field_name,
                media_filename=media_filename,
                sound_tag=tag,
                message=f"Saved the note, but could not verify it afterward: {exc}",
            )
        if not note_has_field(persisted_note, field_name) or tag not in str(persisted_note[field_name]):
            return AppendSoundResult(
                success=False,
                field_name=field_name,
                media_filename=media_filename,
                sound_tag=tag,
                message=f"Tried to append audio to '{field_name}', but the saved note did not contain the new sound tag.",
            )

    return AppendSoundResult(
        success=True,
        field_name=field_name,
        media_filename=media_filename,
        sound_tag=tag,
        message=f"Appended audio to '{field_name}'.",
    )
