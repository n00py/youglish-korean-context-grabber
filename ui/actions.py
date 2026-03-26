from __future__ import annotations

from pathlib import Path

from aqt import gui_hooks, mw
from aqt.reviewer import Reviewer
from aqt.qt import QAction, QDialog, qconnect
from aqt.utils import showWarning, tooltip

from ..config import AddonConfig, config_from_dict
from ..services.context_service import ContextServiceError, YouGlishContextService
from ..services.logging_utils import get_logger
from .picker import CandidatePickerDialog, RESULT_DONE, RESULT_REFRESH, RESULT_SKIP
from .settings_dialog import open_deepl_settings_dialog


EDITOR_BUTTON_LABEL = "BanGlish Context"
BROWSER_ACTION_LABEL = "Fetch BanGlish Context"
SETTINGS_ACTION_LABEL = "BanGlish Context Settings..."
ROOT_MODULE = "youglish_korean_context_grabber"
REVIEWER_BUTTON_URL = "banglish_review"
REVIEWER_BUTTON_MARKER = "banglish-review-overlay"


def install_hooks(module_name: str) -> None:
    global ROOT_MODULE
    ROOT_MODULE = module_name
    if getattr(mw, "_youglish_context_installed", False):
        return
    mw._youglish_context_installed = True
    if hasattr(gui_hooks, "editor_did_init_buttons"):
        gui_hooks.editor_did_init_buttons.append(_add_editor_button)
    if hasattr(gui_hooks, "browser_menus_did_init"):
        gui_hooks.browser_menus_did_init.append(_add_browser_menu_action)
    if hasattr(gui_hooks, "browser_will_show_context_menu"):
        gui_hooks.browser_will_show_context_menu.append(_add_browser_context_action)
    if hasattr(gui_hooks, "reviewer_will_show_context_menu"):
        gui_hooks.reviewer_will_show_context_menu.append(_add_reviewer_context_action)
    if hasattr(gui_hooks, "webview_will_set_content"):
        gui_hooks.webview_will_set_content.append(_inject_reviewer_overlay_button)
    if hasattr(gui_hooks, "webview_did_receive_js_message"):
        gui_hooks.webview_did_receive_js_message.append(_handle_reviewer_overlay_click)
    if hasattr(gui_hooks, "main_window_did_init"):
        gui_hooks.main_window_did_init.append(_install_settings_actions)
    else:
        _install_settings_actions()


def _addon_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _config() -> AddonConfig:
    payload = mw.addonManager.getConfig(ROOT_MODULE) or {}
    return config_from_dict(payload)


def _service() -> YouGlishContextService:
    return YouGlishContextService(_config(), get_logger(_addon_dir()))


def _note_label(note: object) -> str:
    try:
        note_type = note.note_type()["name"]
    except Exception:
        note_type = "Unknown note type"
    return f"Note ID {getattr(note, 'id', '?')} | {note_type}"


def _query_from_note(note: object, config: AddonConfig) -> str:
    if config.source_field_name not in set(note.keys()):
        raise ContextServiceError(
            f"Source field '{config.source_field_name}' was not found on this note."
        )
    value = str(note[config.source_field_name]).strip()
    if not value:
        raise ContextServiceError(
            f"Source field '{config.source_field_name}' is empty on this note."
        )
    return value


def _fetch_candidates_for_note(note: object, max_candidates_override: int | None = None):
    config = _config()
    query = _query_from_note(note, config)
    service = _service()
    requested_max_candidates = config.effective_max_candidates_for(max_candidates_override)
    mw.progress.start(
        immediate=True,
        label=f"Fetching BanGlish clips for {query} ({requested_max_candidates} results)...",
    )
    try:
        candidates = service.fetch_candidates(
            query,
            col=mw.col,
            ignore_note_id=note.id,
            max_candidates_override=requested_max_candidates,
        )
    finally:
        mw.progress.finish()
    return query, candidates


def _show_viewer(note: object) -> int:
    config = _config()
    max_candidates_override = config.effective_max_candidates
    while True:
        query, candidates = _fetch_candidates_for_note(note, max_candidates_override=max_candidates_override)
        dialog = CandidatePickerDialog(
            query,
            candidates,
            _note_label(note),
            note=note,
            sound_field_name=config.sound_field_name,
            initial_max_candidates=max_candidates_override,
            translation_enabled=config.translation_enabled,
            translation_provider=config.translation_provider,
            translation_target_language=config.translation_target_language,
            translation_timeout_seconds=config.translation_timeout_seconds,
            parent=mw,
        )
        result = dialog.exec()
        if result == RESULT_REFRESH:
            max_candidates_override = dialog.requested_max_candidates()
            continue
        return result


def _current_reviewer_note(reviewer) -> object | None:
    if mw.col is None:
        return None
    card = getattr(reviewer, "card", None)
    if card is None:
        return None
    try:
        return mw.col.get_note(card.nid)
    except Exception:
        return None


def _run_editor_flow(editor) -> None:
    if mw.col is None or editor.note is None:
        showWarning("Open a note before fetching BanGlish context.")
        return
    try:
        result = _show_viewer(editor.note)
    except Exception as exc:
        get_logger(_addon_dir()).exception("Editor flow failed")
        showWarning(str(exc))
        return
    if result == int(QDialog.DialogCode.Rejected):
        return
    tooltip("BanGlish viewer closed.")


def _run_browser_flow(browser) -> None:
    if mw.col is None:
        showWarning("Open a collection before fetching BanGlish context.")
        return
    note_ids = list(browser.selectedNotes())
    if not note_ids:
        showWarning("Select at least one note in the browser.")
        return
    processed = 0
    viewed = 0
    for note_id in note_ids:
        try:
            note = mw.col.get_note(int(note_id))
        except Exception:
            continue
        processed += 1
        try:
            result = _show_viewer(note)
        except Exception as exc:
            get_logger(_addon_dir()).exception("Browser flow failed for note %s", note_id)
            showWarning(str(exc))
            continue
        if result == int(QDialog.DialogCode.Rejected):
            break
        if result in (RESULT_DONE, RESULT_SKIP):
            viewed += 1
    tooltip(
        "BanGlish Context finished. Opened %d of %d selected note(s)."
        % (viewed, processed)
    )


def _run_reviewer_flow(reviewer) -> None:
    note = _current_reviewer_note(reviewer)
    if note is None:
        showWarning("No active review note is available.")
        return
    try:
        result = _show_viewer(note)
    except Exception as exc:
        get_logger(_addon_dir()).exception("Reviewer flow failed")
        showWarning(str(exc))
        return
    if result == int(QDialog.DialogCode.Rejected):
        return
    tooltip("BanGlish viewer closed.")


def _open_settings_dialog(*_args, **_kwargs) -> int:
    return open_deepl_settings_dialog(parent=mw)


def _install_settings_actions(*_args, **_kwargs) -> None:
    if getattr(mw, "_youglish_context_settings_installed", False):
        return
    mw._youglish_context_settings_installed = True

    if hasattr(mw, "addonManager") and hasattr(mw.addonManager, "setConfigAction"):
        try:
            mw.addonManager.setConfigAction(ROOT_MODULE, _open_settings_dialog)
        except Exception:
            pass

    if hasattr(mw, "form") and hasattr(mw.form, "menuTools"):
        action = QAction(SETTINGS_ACTION_LABEL, mw)
        qconnect(action.triggered, lambda _checked=False: _open_settings_dialog())
        mw.form.menuTools.addAction(action)


def _reviewer_button_html() -> str:
    return (
        f"<div id=\"{REVIEWER_BUTTON_MARKER}\" "
        "style=\"position:fixed;left:24px;bottom:20px;z-index:9999;\">"
        f"<button onclick=\"pycmd('{REVIEWER_BUTTON_URL}')\" "
        "title=\"Open BanGlish Context\" "
        "style=\""
        "min-width:104px;"
        "height:36px;"
        "padding:0 16px;"
        "border-radius:999px;"
        "border:1px solid #cfcfcf;"
        "background:#ffffff;"
        "color:#222222;"
        "font-weight:600;"
        "font-size:13px;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "box-shadow:0 1px 2px rgba(0,0,0,0.08),0 3px 10px rgba(0,0,0,0.05);"
        "cursor:pointer;"
        "outline:none;"
        "\">BanGlish</button></div>"
    )


def _inject_reviewer_overlay_button(web_content, context) -> None:
    if not isinstance(context, Reviewer):
        return
    if REVIEWER_BUTTON_MARKER in getattr(web_content, "body", ""):
        return
    web_content.body += _reviewer_button_html()


def _handle_reviewer_overlay_click(handled, message: str, context):
    if message != REVIEWER_BUTTON_URL:
        return handled
    if not isinstance(context, Reviewer):
        return handled
    _run_reviewer_flow(context)
    return (True, None)


def _add_editor_button(buttons, editor):
    buttons.append(
        editor.addButton(
            icon=None,
            cmd="youglish_context",
            func=lambda ed=editor: _run_editor_flow(ed),
            tip=EDITOR_BUTTON_LABEL,
            label=EDITOR_BUTTON_LABEL,
        )
    )
    return buttons


def _browser_action(browser):
    return lambda _checked=False, br=browser: _run_browser_flow(br)


def _add_browser_menu_action(browser) -> None:
    action = QAction(BROWSER_ACTION_LABEL, browser)
    qconnect(action.triggered, _browser_action(browser))
    try:
        browser.form.menuEdit.addAction(action)
    except Exception:
        pass


def _add_browser_context_action(browser, menu) -> None:
    action = menu.addAction(BROWSER_ACTION_LABEL)
    qconnect(action.triggered, _browser_action(browser))


def _add_reviewer_context_action(reviewer, menu) -> None:
    if reviewer is None:
        return
    action = menu.addAction(EDITOR_BUTTON_LABEL)
    qconnect(action.triggered, lambda _checked=False, rev=reviewer: _run_reviewer_flow(rev))
