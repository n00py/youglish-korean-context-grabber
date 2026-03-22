from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TranslationError(RuntimeError):
    pass


def deepl_api_key_path(addon_dir: Path) -> Path:
    user_files_dir = addon_dir / "user_files"
    user_files_dir.mkdir(parents=True, exist_ok=True)
    return user_files_dir / "deepl_api_key.txt"


def load_deepl_api_key(addon_dir: Path, logger: logging.Logger | None = None) -> str:
    key_path = deepl_api_key_path(addon_dir)
    try:
        return key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception as exc:
        if logger is not None:
            logger.warning("Could not read DeepL API key file: %s", exc)
        return ""


def save_deepl_api_key(addon_dir: Path, api_key: str) -> Path:
    key_path = deepl_api_key_path(addon_dir)
    key_path.write_text((api_key or "").strip() + "\n", encoding="utf-8")
    return key_path


def clear_deepl_api_key(addon_dir: Path) -> None:
    deepl_api_key_path(addon_dir).unlink(missing_ok=True)


class DeepLTranslationService:
    def __init__(
        self,
        addon_dir: Path,
        *,
        target_language: str = "EN-US",
        timeout_seconds: int = 15,
        logger: logging.Logger | None = None,
    ) -> None:
        self._addon_dir = addon_dir
        self._logger = logger or logging.getLogger(__name__)
        self._user_files_dir = addon_dir / "user_files"
        self._user_files_dir.mkdir(parents=True, exist_ok=True)
        self._key_path = deepl_api_key_path(addon_dir)
        self._cache_path = self._user_files_dir / "translation_cache.json"
        self._target_language = (target_language or "EN-US").strip().upper()
        self._timeout_seconds = max(1, int(timeout_seconds))
        self._cache = self._load_cache()

    def is_configured(self) -> bool:
        return bool(self._load_api_key())

    def translate_text(self, text: str) -> str:
        sentence = (text or "").strip()
        if not sentence:
            return ""
        cache_key = self._cache_key(sentence)
        cached = self._cache.get(cache_key)
        if isinstance(cached, str) and cached.strip():
            return cached

        api_key = self._load_api_key()
        if not api_key:
            raise TranslationError(
                "DeepL translation is not configured. Add your key to user_files/deepl_api_key.txt."
            )

        payload = urlencode(
            {
                "text": sentence,
                "source_lang": "KO",
                "target_lang": self._target_language,
            }
        ).encode("utf-8")
        request = Request(
            "https://api-free.deepl.com/v2/translate",
            data=payload,
            headers={
                "Authorization": f"DeepL-Auth-Key {api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            self._logger.warning("DeepL HTTP error %s: %s", exc.code, detail)
            raise TranslationError(f"DeepL request failed with HTTP {exc.code}.") from exc
        except URLError as exc:
            raise TranslationError(f"DeepL request failed: {exc.reason}") from exc
        except Exception as exc:
            raise TranslationError(f"DeepL request failed: {exc}") from exc

        translations = response_payload.get("translations")
        if not isinstance(translations, list) or not translations:
            raise TranslationError("DeepL returned no translation.")
        translated_text = str(translations[0].get("text", "")).strip()
        if not translated_text:
            raise TranslationError("DeepL returned an empty translation.")

        self._cache[cache_key] = translated_text
        self._save_cache()
        return translated_text

    def _cache_key(self, text: str) -> str:
        return f"deepl_free::{self._target_language}::{text}"

    def _load_api_key(self) -> str:
        return load_deepl_api_key(self._addon_dir, self._logger)

    def _load_cache(self) -> dict[str, str]:
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self._logger.warning("Could not load translation cache: %s", exc)
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items() if str(value).strip()}

    def _save_cache(self) -> None:
        try:
            self._cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            self._logger.exception("Could not save translation cache")
