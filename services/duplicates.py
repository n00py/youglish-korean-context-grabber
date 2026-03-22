from __future__ import annotations

from typing import Iterable, Tuple

from .ranking import normalize_text


def _candidate_note_values(note: object) -> Iterable[str]:
    keys = getattr(note, "keys", None)
    if callable(keys):
        for key in note.keys():
            try:
                yield str(note[key])
            except Exception:
                continue


def find_duplicate_note_ids(
    col: object,
    sentence: str,
    ignore_note_id: int | None = None,
    limit: int = 5,
) -> Tuple[int, ...]:
    normalized_sentence = normalize_text(sentence)
    if not normalized_sentence or col is None:
        return ()
    try:
        rough_matches = col.db.list(
            "select id from notes where flds like ? limit 50",
            f"%{normalized_sentence[:200]}%",
        )
    except Exception:
        return ()
    matches = []
    for note_id in rough_matches:
        if ignore_note_id is not None and int(note_id) == int(ignore_note_id):
            continue
        try:
            note = col.get_note(int(note_id))
        except Exception:
            continue
        for value in _candidate_note_values(note):
            if normalize_text(value) == normalized_sentence:
                matches.append(int(note_id))
                break
        if len(matches) >= limit:
            break
    return tuple(matches)
