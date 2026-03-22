from __future__ import annotations

from pathlib import Path

from aqt import mw
from aqt.qt import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import tooltip

from ..services.translation_service import (
    clear_deepl_api_key,
    deepl_api_key_path,
    load_deepl_api_key,
    save_deepl_api_key,
)


def _addon_dir() -> Path:
    return Path(__file__).resolve().parent.parent


class DeepLSettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent or mw)
        self._addon_dir = _addon_dir()
        self.setWindowTitle("YouGlish Context Settings")
        self.resize(560, 220)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Set your DeepL API key for English translations. "
            "The key is saved locally in this add-on's user_files folder and is not stored in git.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        path_label = QLabel(f"Local key file: {deepl_api_key_path(self._addon_dir)}", self)
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(path_label.textInteractionFlags())
        layout.addWidget(path_label)

        field_label = QLabel("DeepL API Key", self)
        layout.addWidget(field_label)

        field_row = QHBoxLayout()
        self.key_field = QLineEdit(self)
        self.key_field.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_field.setPlaceholderText("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx")
        self.key_field.setText(load_deepl_api_key(self._addon_dir))
        self.show_key_checkbox = QCheckBox("Show", self)
        qconnect(self.show_key_checkbox.toggled, self._toggle_key_visibility)
        field_row.addWidget(self.key_field, 1)
        field_row.addWidget(self.show_key_checkbox)
        layout.addLayout(field_row)

        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        self.status_label.setText(self._status_text())
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        self.save_button = QPushButton("Save Key", self)
        self.clear_button = QPushButton("Clear Key", self)
        self.close_button = QPushButton("Close", self)
        button_row.addWidget(self.save_button)
        button_row.addWidget(self.clear_button)
        button_row.addStretch(1)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        qconnect(self.save_button.clicked, self.save_key)
        qconnect(self.clear_button.clicked, self.clear_key)
        qconnect(self.close_button.clicked, self.accept)

    def _toggle_key_visibility(self, checked: bool) -> None:
        self.key_field.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _status_text(self) -> str:
        key = load_deepl_api_key(self._addon_dir)
        if key:
            return "A DeepL key is saved locally for this add-on."
        return "No DeepL key is saved yet."

    def save_key(self) -> None:
        key_text = self.key_field.text().strip()
        if not key_text:
            self.status_label.setText("Enter a DeepL key first, or click Clear Key to remove the saved one.")
            return
        save_deepl_api_key(self._addon_dir, key_text)
        self.status_label.setText("Saved the DeepL key locally for this add-on.")
        tooltip("Saved DeepL key.")

    def clear_key(self) -> None:
        clear_deepl_api_key(self._addon_dir)
        self.key_field.clear()
        self.status_label.setText("Cleared the local DeepL key.")
        tooltip("Cleared DeepL key.")


def open_deepl_settings_dialog(parent=None) -> int:
    dialog = DeepLSettingsDialog(parent=parent or mw)
    return dialog.exec()
