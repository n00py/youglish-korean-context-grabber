from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

from ..config import AddonConfig
from ..provider.models import ContextCandidate


@dataclass
class NoteWriteResult:
    changed_fields: Dict[str, str] = field(default_factory=dict)
    skipped_fields: Dict[str, str] = field(default_factory=dict)
    missing_fields: Tuple[str, ...] = ()

    @property
    def updated(self) -> bool:
        return bool(self.changed_fields)


def _has_field(note: object, field_name: str) -> bool:
    keys = getattr(note, "keys", None)
    if callable(keys):
        return field_name in set(note.keys())
    return False


def _candidate_values(candidate: ContextCandidate, config: AddonConfig) -> Dict[str, str]:
    return {
        config.destination_fields.sentence: candidate.sentence_text,
        config.destination_fields.source: candidate.source_title or candidate.provider_name,
        config.destination_fields.url: candidate.source_url,
        config.destination_fields.timestamp: candidate.timestamp,
        config.destination_fields.translation: "",
    }


def plan_note_update(
    note: object,
    candidate: ContextCandidate,
    config: AddonConfig,
) -> NoteWriteResult:
    result = NoteWriteResult()
    changes = _candidate_values(candidate, config)
    missing_fields = []
    for field_name, new_value in changes.items():
        if not field_name:
            continue
        if not _has_field(note, field_name):
            missing_fields.append(field_name)
            continue
        if field_name == config.destination_fields.translation and not new_value:
            continue
        current_value = str(note[field_name]).strip()
        if current_value:
            if not config.overwrite_existing:
                if field_name in config.protected_fields:
                    result.skipped_fields[field_name] = "protected field preserved"
                else:
                    result.skipped_fields[field_name] = "existing value preserved"
                continue
        if current_value == new_value:
            result.skipped_fields[field_name] = "already up to date"
            continue
        result.changed_fields[field_name] = new_value
    result.missing_fields = tuple(missing_fields)
    return result


def apply_note_update(
    note: object,
    candidate: ContextCandidate,
    config: AddonConfig,
) -> NoteWriteResult:
    result = plan_note_update(note, candidate, config)
    for field_name, value in result.changed_fields.items():
        note[field_name] = value
    return result
