from __future__ import annotations

import html
from pathlib import Path

from aqt import mw
from aqt.qt import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
    Qt,
    qconnect,
)
from aqt.utils import tooltip

from ..provider.models import ContextCandidate
from ..services.audio_clips import (
    AudioClipError,
    YouGlishAudioClipService,
    candidate_hit_label,
    candidate_range_label,
)
from ..services.logging_utils import get_logger
from ..services.sound_field import append_clip_to_note_field, note_has_field


RESULT_DONE = int(QDialog.DialogCode.Accepted)
RESULT_SKIP = 1001
RESULT_REFRESH = 1002


def _addon_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _highlight_html(sentence: str, query: str) -> str:
    if not query or query not in sentence:
        return html.escape(sentence)
    first_index = sentence.find(query)
    before = html.escape(sentence[:first_index])
    match = html.escape(sentence[first_index : first_index + len(query)])
    after = html.escape(sentence[first_index + len(query) :])
    return (
        before
        + "<span style='background-color:#ffe28a;font-weight:600;'>"
        + match
        + "</span>"
        + after
    )


class CandidateRow(QWidget):
    def __init__(self, candidate: ContextCandidate, query: str, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        sentence_label = QLabel(self)
        sentence_label.setWordWrap(True)
        sentence_label.setTextFormat(Qt.TextFormat.RichText)
        sentence_label.setText(_highlight_html(candidate.sentence_text, query))
        layout.addWidget(sentence_label)

        details = []
        if candidate.source_title:
            details.append(candidate.source_title)
        hit_label = candidate_hit_label(candidate)
        if hit_label:
            details.append("YouGlish " + hit_label)
        clip_range = candidate_range_label(candidate)
        if clip_range:
            details.append("Audio " + clip_range)
        if candidate.duplicate_note_ids:
            details.append(
                "Duplicate in note(s): " + ", ".join(str(note_id) for note_id in candidate.duplicate_note_ids)
            )
        detail_label = QLabel(" | ".join(details), self)
        detail_label.setWordWrap(True)
        detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_label.setVisible(bool(details))
        layout.addWidget(detail_label)


class CandidatePickerDialog(QDialog):
    def __init__(
        self,
        query: str,
        candidates: list[ContextCandidate],
        note_label: str,
        note: object | None = None,
        sound_field_name: str = "Sound",
        initial_max_candidates: int = 5,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._candidates = candidates
        self._query = query
        self._note = note
        self._sound_field_name = sound_field_name
        self._selected_index = 0
        self._logger = get_logger(_addon_dir())
        self._audio_service = YouGlishAudioClipService(_addon_dir(), self._logger)
        self._media_player = None
        self._audio_output = None
        self._audio_job_running = False
        self._current_clip_path: Path | None = None
        self._current_clip_candidate: ContextCandidate | None = None
        self._progress_lines: list[str] = []
        self._requested_max_candidates = initial_max_candidates

        self.setWindowTitle("YouGlish Context")
        self.resize(1080, 680)

        layout = QVBoxLayout(self)
        title = QLabel(f"Query: <b>{html.escape(query)}</b><br>{html.escape(note_label)}", self)
        title.setWordWrap(True)
        title.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter, 1)

        left_panel = QWidget(splitter)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        self.list_widget = QListWidget(self)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        left_layout.addWidget(self.list_widget, 1)

        if candidates:
            for index, candidate in enumerate(candidates):
                item = QListWidgetItem()
                row = CandidateRow(candidate, query, self.list_widget)
                item.setSizeHint(row.sizeHint())
                self.list_widget.addItem(item)
                self.list_widget.setItemWidget(item, row)
                if index == 0:
                    item.setSelected(True)
            qconnect(self.list_widget.itemSelectionChanged, self._sync_selection)
            qconnect(self.list_widget.itemDoubleClicked, lambda _item: self.play_selected())
        else:
            empty = QListWidgetItem()
            empty_label = QLabel("No results found for this query.", self.list_widget)
            empty_label.setWordWrap(True)
            empty.setSizeHint(empty_label.sizeHint())
            self.list_widget.addItem(empty)
            self.list_widget.setItemWidget(empty, empty_label)

        right_panel = QWidget(splitter)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        transcript_title = QLabel("Transcript", right_panel)
        transcript_title.setStyleSheet("font-weight: 600;")
        right_layout.addWidget(transcript_title)

        self.transcript_label = QLabel(right_panel)
        self.transcript_label.setWordWrap(True)
        self.transcript_label.setTextFormat(Qt.TextFormat.RichText)
        self.transcript_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.transcript_label.setMinimumHeight(96)
        right_layout.addWidget(self.transcript_label)

        self.meta_label = QLabel(right_panel)
        self.meta_label.setWordWrap(True)
        self.meta_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.meta_label.setOpenExternalLinks(True)
        right_layout.addWidget(self.meta_label)

        self.audio_panel = QLabel(right_panel)
        self.audio_panel.setWordWrap(True)
        self.audio_panel.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.audio_panel.setStyleSheet(
            "background:#faf4ea;border:1px solid #dbc6a8;border-radius:10px;padding:14px;"
        )
        right_layout.addWidget(self.audio_panel)

        self.player_hint_label = QLabel(right_panel)
        self.player_hint_label.setWordWrap(True)
        right_layout.addWidget(self.player_hint_label)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        button_row = QHBoxLayout()
        results_label = QLabel("Results", self)
        self.max_candidates_combo = QComboBox(self)
        for value in range(3, 21):
            self.max_candidates_combo.addItem(str(value), value)
        combo_index = self.max_candidates_combo.findData(int(initial_max_candidates))
        if combo_index >= 0:
            self.max_candidates_combo.setCurrentIndex(combo_index)
        self.play_button = QPushButton("Play Audio", self)
        self.play_button.setEnabled(bool(candidates))
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setEnabled(bool(candidates))
        self.copy_button = QPushButton("Copy Transcript", self)
        self.copy_button.setEnabled(bool(candidates))
        self.append_sound_button = QPushButton("Append to Sound", self)
        self.append_sound_button.setEnabled(False)
        self.done_button = QPushButton("Done", self)
        self.skip_button = QPushButton("Skip", self)
        self.refresh_button = QPushButton("Refresh Search", self)
        self.cancel_button = QPushButton("Cancel", self)
        button_row.addWidget(results_label)
        button_row.addWidget(self.max_candidates_combo)
        button_row.addSpacing(8)
        button_row.addWidget(self.play_button)
        button_row.addWidget(self.stop_button)
        button_row.addWidget(self.copy_button)
        button_row.addWidget(self.append_sound_button)
        button_row.addWidget(self.done_button)
        button_row.addWidget(self.skip_button)
        button_row.addWidget(self.refresh_button)
        button_row.addStretch(1)
        button_row.addWidget(self.cancel_button)
        layout.addLayout(button_row)

        qconnect(self.play_button.clicked, self.play_selected)
        qconnect(self.stop_button.clicked, self.stop_selected)
        qconnect(self.copy_button.clicked, self.copy_selected_to_clipboard)
        qconnect(self.append_sound_button.clicked, self.append_selected_to_sound)
        qconnect(self.done_button.clicked, self.accept)
        qconnect(self.skip_button.clicked, lambda: self.done(RESULT_SKIP))
        qconnect(self.refresh_button.clicked, lambda: self.done(RESULT_REFRESH))
        qconnect(self.cancel_button.clicked, self.reject)

        self._ensure_audio_player()
        self._update_preview()

    def selected_candidate(self) -> ContextCandidate | None:
        if not self._candidates:
            return None
        row = self.list_widget.currentRow()
        if row < 0:
            row = self._selected_index
        if row < 0 or row >= len(self._candidates):
            return None
        return self._candidates[row]

    def requested_max_candidates(self) -> int:
        selected_value = self.max_candidates_combo.currentData()
        if isinstance(selected_value, int):
            return selected_value
        try:
            return int(self.max_candidates_combo.currentText())
        except (TypeError, ValueError):
            return self._requested_max_candidates

    def _sync_selection(self) -> None:
        row = self.list_widget.currentRow()
        if row >= 0:
            self._selected_index = row
        self._update_preview()

    def play_selected(self) -> None:
        candidate = self.selected_candidate()
        if candidate is None:
            return
        if self._audio_job_running:
            self.player_hint_label.setText("Audio extraction is already running.")
            return

        self._audio_job_running = True
        self._current_clip_path = None
        self._current_clip_candidate = candidate
        self.play_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.append_sound_button.setEnabled(False)
        self._progress_lines = []
        self._append_progress_line(f"Selected transcript: {candidate.sentence_text}")
        self._update_progress_status("Starting audio extraction...")

        def task() -> Path:
            return self._audio_service.ensure_clip(candidate, progress_callback=self._worker_progress)

        def on_done(future) -> None:
            self._audio_job_running = False
            self.play_button.setEnabled(bool(self._candidates))
            self.stop_button.setEnabled(True)
            self._sync_append_button_state()
            try:
                clip_path = future.result()
            except AudioClipError as exc:
                self._logger.warning("Audio clip extraction failed for %s: %s", candidate.video_id, exc)
                self._update_progress_status(str(exc))
                return
            except Exception as exc:
                self._logger.exception("Unexpected audio playback failure for %s", candidate.video_id)
                self._update_progress_status(f"Audio playback failed: {exc}")
                return
            self._play_local_clip(candidate, clip_path)

        taskman = getattr(mw, "taskman", None)
        if taskman is not None and hasattr(taskman, "run_in_background"):
            taskman.run_in_background(task, on_done=on_done)
            return

        class _ImmediateFuture:
            def __init__(self, value=None, error: Exception | None = None) -> None:
                self._value = value
                self._error = error

            def result(self):
                if self._error is not None:
                    raise self._error
                return self._value

        try:
            clip_path = task()
            on_done(_ImmediateFuture(value=clip_path))
        except Exception as exc:
            on_done(_ImmediateFuture(error=exc))

    def stop_selected(self) -> None:
        if self._media_player is None:
            self.player_hint_label.setText("Inline audio playback is not available here.")
            return
        self._media_player.stop()
        self.player_hint_label.setText("Stopped the local audio clip.")

    def copy_selected_to_clipboard(self) -> None:
        candidate = self.selected_candidate()
        if candidate is None:
            self._update_progress_status("Select a transcript before copying it.")
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self._update_progress_status("Clipboard access is not available in this Anki build.")
            return
        clipboard.setText(candidate.sentence_text)
        self._update_progress_status("Copied the selected transcript to the clipboard.")
        tooltip("Copied transcript.")

    def append_selected_to_sound(self) -> None:
        candidate = self.selected_candidate()
        clip_path = self._current_clip_path
        if candidate is None or clip_path is None:
            self._update_progress_status("Extract a clip before appending it to the note.")
            return
        if self._note is None:
            self._update_progress_status("No note is available for this viewer.")
            return
        result = append_clip_to_note_field(
            note=self._note,
            clip_path=clip_path,
            col=mw.col,
            field_name=self._sound_field_name,
        )
        self._logger.info(
            "Append to Sound for note %s in field %s: %s",
            getattr(self._note, "id", "?"),
            self._sound_field_name,
            result.message,
        )
        self._update_progress_status(result.message)
        if result.success:
            self._refresh_note_views()
            tooltip(result.message)
        self._sync_append_button_state()

    def _refresh_note_views(self) -> None:
        note_id = getattr(self._note, "id", 0)

        editor = getattr(mw, "editor", None)
        if editor is not None and getattr(getattr(editor, "note", None), "id", None) == note_id:
            for method_name in ("loadNoteKeepingFocus", "loadNote"):
                method = getattr(editor, method_name, None)
                if callable(method):
                    try:
                        method()
                        break
                    except TypeError:
                        try:
                            method(editor.note)
                            break
                        except Exception:
                            self._logger.exception("Could not refresh editor note view")
                    except Exception:
                        self._logger.exception("Could not refresh editor note view")

        browser = getattr(mw, "browser", None)
        if browser is not None:
            for method_name in ("onSearchActivated", "refresh_notes"):
                method = getattr(browser, method_name, None)
                if callable(method):
                    try:
                        method()
                        break
                    except Exception:
                        self._logger.exception("Could not refresh browser note view")

        reviewer = getattr(mw, "reviewer", None)
        card = getattr(reviewer, "card", None) if reviewer is not None else None
        if card is not None and getattr(card, "nid", None) == note_id:
            for method_name in ("_redraw_current_card", "reload", "show"):
                method = getattr(reviewer, method_name, None)
                if callable(method):
                    try:
                        method()
                        break
                    except Exception:
                        self._logger.exception("Could not refresh reviewer card view")

        require_reset = getattr(mw, "requireReset", None)
        if callable(require_reset):
            try:
                require_reset()
            except Exception:
                self._logger.exception("Could not request an Anki UI reset after appending audio")

    def _ensure_audio_player(self) -> None:
        try:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
        except Exception:
            self._media_player = None
            self._audio_output = None
            self.player_hint_label.setText(
                "Select a transcript, then click Play Audio to extract the clip. "
                "This Anki build does not expose Qt multimedia for inline playback."
            )
            return

        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(1.0)
        self._media_player = QMediaPlayer(self)
        self._media_player.setAudioOutput(self._audio_output)

        if hasattr(self._media_player, "errorOccurred"):
            self._media_player.errorOccurred.connect(self._on_media_error)
        if hasattr(self._media_player, "mediaStatusChanged"):
            self._media_player.mediaStatusChanged.connect(self._on_media_status_changed)

        self.player_hint_label.setText(
            "Select a transcript, then click Play Audio to extract and play only that sentence."
        )

    def _on_media_error(self, error, error_string: str | None = None) -> None:
        if not error:
            return
        message = error_string or "Qt multimedia could not play the extracted audio clip."
        self.player_hint_label.setText(message)

    def _on_media_status_changed(self, status) -> None:
        status_name = getattr(status, "name", str(status))
        if status_name == "LoadedMedia":
            self.player_hint_label.setText("Loaded the extracted sentence audio.")
        elif status_name == "EndOfMedia":
            self.player_hint_label.setText("Finished the extracted sentence audio.")

    def _play_local_clip(self, candidate: ContextCandidate, clip_path: Path) -> None:
        self._current_clip_path = clip_path
        self._current_clip_candidate = candidate
        self.audio_panel.setText(
            "Prepared local audio clip:\n"
            + str(clip_path.name)
            + "\n\n"
            + candidate.sentence_text
        )
        self._sync_append_button_state()
        if self._media_player is None:
            self._update_progress_status(
                "The clip was extracted, but inline audio playback is not available in this Anki build."
            )
            return
        try:
            from aqt.qt import QUrl
        except Exception as exc:
            self._update_progress_status(f"Could not prepare the audio file URL: {exc}")
            return
        self._media_player.stop()
        self._media_player.setSource(QUrl.fromLocalFile(str(clip_path)))
        self._media_player.play()
        self._update_progress_status("Playing the extracted sentence audio.")

    def _worker_progress(self, message: str) -> None:
        taskman = getattr(mw, "taskman", None)
        if taskman is not None and hasattr(taskman, "run_on_main"):
            taskman.run_on_main(lambda: self._append_progress_line(message))
            return
        self._append_progress_line(message)

    def _append_progress_line(self, message: str) -> None:
        self._progress_lines.append(message)
        self._progress_lines = self._progress_lines[-10:]
        self.audio_panel.setText("\n".join(self._progress_lines))
        self._update_progress_status(message)

    def _update_progress_status(self, message: str) -> None:
        self.player_hint_label.setText(message)

    def _update_preview(self) -> None:
        candidate = self.selected_candidate()
        self._current_clip_path = None
        self._current_clip_candidate = candidate
        self._sync_append_button_state()
        if candidate is None:
            self.transcript_label.setText("No transcript available.")
            self.meta_label.setText("")
            self.audio_panel.setText("No audio clip is selected.")
            return

        self.transcript_label.setText(_highlight_html(candidate.sentence_text, self._query))

        metadata = []
        if candidate.source_title:
            metadata.append(html.escape(candidate.source_title))
        hit_label = candidate_hit_label(candidate)
        if hit_label:
            metadata.append(f"YouGlish hit: {html.escape(hit_label)}")
        clip_range = candidate_range_label(candidate)
        if clip_range:
            metadata.append(f"Sentence audio: {html.escape(clip_range)}")
        if candidate.source_url:
            escaped_url = html.escape(candidate.source_url, quote=True)
            metadata.append(f'<a href="{escaped_url}">Open YouGlish clip</a>')
        if candidate.duplicate_note_ids:
            metadata.append(
                "Also seen in note(s): "
                + ", ".join(str(note_id) for note_id in candidate.duplicate_note_ids)
            )
        self.meta_label.setText(" | ".join(metadata) if metadata else "No extra metadata available.")

        self.audio_panel.setText(
            "Ready to extract local audio for this sentence.\n"
            "Progress messages will appear here while yt-dlp tries each browser cookie source."
        )

    def _sync_append_button_state(self) -> None:
        can_append = (
            not self._audio_job_running
            and self._current_clip_path is not None
            and self._note is not None
            and note_has_field(self._note, self._sound_field_name)
        )
        self.append_sound_button.setEnabled(can_append)
        if self._note is not None and not note_has_field(self._note, self._sound_field_name):
            self.append_sound_button.setToolTip(
                f"This note does not have a '{self._sound_field_name}' field."
            )
        else:
            self.append_sound_button.setToolTip(
                f"Append the extracted clip into '{self._sound_field_name}'."
            )
