from __future__ import annotations

import hashlib
import html
import logging
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .text import clean_text, normalize_text, tokenized_text_blob
from ..services.storage_paths import subtitle_cache_dir


SUPPORTED_COOKIE_BROWSERS = (
    "firefox",
    "chrome",
    "safari",
    "brave",
    "chromium",
    "edge",
    "opera",
    "vivaldi",
    "whale",
)

MACOS_APP_BROWSER_MAP = {
    "Firefox.app": "firefox",
    "Google Chrome.app": "chrome",
    "Safari.app": "safari",
    "Brave Browser.app": "brave",
    "Chromium.app": "chromium",
    "Microsoft Edge.app": "edge",
    "Opera.app": "opera",
    "Vivaldi.app": "vivaldi",
    "Naver Whale.app": "whale",
}

YTDLP_TIMEOUT_SECONDS = 60


class SubtitleFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubtitleCue:
    cue_index: int
    start_ms: int
    end_ms: int
    text: str
    normalized_text: str
    tokenized_text: str


@dataclass(frozen=True)
class SubtitleTrackResult:
    youtube_video_id: str
    language_code: str
    source_label: str
    checksum: str
    raw_subtitle_path: Path
    cues: list[SubtitleCue]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def resolve_binary(addon_dir: Path, binary_name: str, fallback: str) -> str:
    bundled = addon_dir / ".venv" / "bin" / binary_name
    if bundled.exists():
        return str(bundled)
    discovered = shutil.which(binary_name)
    if discovered:
        return discovered
    return fallback


def _decode_subprocess_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_command(command: list[str], *, timeout: int | None = None) -> CommandResult:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=False,
        check=False,
        timeout=timeout,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=_decode_subprocess_output(completed.stdout),
        stderr=_decode_subprocess_output(completed.stderr),
    )


def browser_cookie_order() -> tuple[str, ...]:
    if platform.system() != "Darwin":
        return SUPPORTED_COOKIE_BROWSERS
    ordered: list[str] = []
    for app_dir in (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications"):
        if not app_dir.exists():
            continue
        try:
            names = sorted(path.name for path in app_dir.iterdir())
        except Exception:
            continue
        for name in names:
            browser = MACOS_APP_BROWSER_MAP.get(name)
            if browser and browser not in ordered:
                ordered.append(browser)
    if ordered:
        return tuple(ordered)
    return SUPPORTED_COOKIE_BROWSERS


def parse_json3_cues(raw_payload: str) -> list[SubtitleCue]:
    import json

    try:
        payload = json.loads(raw_payload)
    except Exception:
        return []

    cues: list[SubtitleCue] = []
    cue_index = 0
    for event in payload.get("events") or []:
        segs = event.get("segs") or []
        if not segs:
            continue
        try:
            event_start = int(event.get("tStartMs", 0) or 0)
            event_duration = int(event.get("dDurationMs", 0) or 0)
        except (TypeError, ValueError):
            continue
        text_parts: list[str] = []
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            value = str(seg.get("utf8", "") or "")
            if value and value != "\n":
                text_parts.append(value)
        text = clean_text("".join(text_parts))
        if not text:
            continue
        start_ms = max(0, event_start)
        end_ms = max(start_ms + 1, event_start + max(1, event_duration))
        cues.append(
            SubtitleCue(
                cue_index=cue_index,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                normalized_text=normalize_text(text),
                tokenized_text=tokenized_text_blob(text),
            )
        )
        cue_index += 1
    return _dedupe_cues(cues)


def parse_vtt_cues(raw_payload: str) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    cue_index = 0
    blocks = re.split(r"\r?\n\r?\n+", raw_payload.strip())
    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].upper().startswith("WEBVTT") or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        match = re.match(
            r"(?P<start>\d{2}:\d{2}(?::\d{2})?\.\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}(?::\d{2})?\.\d{3})",
            lines[0],
        )
        if not match:
            continue
        text = clean_text(_strip_vtt_markup("\n".join(lines[1:])))
        if not text:
            continue
        start_ms = _parse_vtt_timestamp_ms(match.group("start"))
        end_ms = max(start_ms + 1, _parse_vtt_timestamp_ms(match.group("end")))
        cues.append(
            SubtitleCue(
                cue_index=cue_index,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                normalized_text=normalize_text(text),
                tokenized_text=tokenized_text_blob(text),
            )
        )
        cue_index += 1
    return _dedupe_cues(cues)


def _parse_vtt_timestamp_ms(value: str) -> int:
    parts = value.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds_ms = parts
    elif len(parts) == 3:
        hours, minutes, seconds_ms = parts
    else:
        return 0
    seconds, millis = seconds_ms.split(".", 1)
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(millis[:3].ljust(3, "0"))
    )


def _strip_vtt_markup(value: str) -> str:
    unescaped = html.unescape(value)
    without_tags = re.sub(r"<[^>]+>", "", unescaped)
    return without_tags.replace("&nbsp;", " ")


def _dedupe_cues(cues: Iterable[SubtitleCue]) -> list[SubtitleCue]:
    deduped: list[SubtitleCue] = []
    seen = set()
    for cue in cues:
        key = (cue.start_ms, cue.end_ms, cue.text)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cue)
    return deduped


class ManualKoreanSubtitleFetcher:
    def __init__(self, addon_dir: Path, logger: logging.Logger | None = None) -> None:
        self._addon_dir = addon_dir
        self._logger = logger or logging.getLogger(__name__)
        self._cache_dir = subtitle_cache_dir(addon_dir)

    def fetch_for_video(
        self,
        youtube_video_id: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> SubtitleTrackResult:
        if not youtube_video_id:
            raise SubtitleFetchError("Missing YouTube video id.")
        existing = self._existing_track_result(youtube_video_id)
        if existing is not None:
            self._emit(progress_callback, f"Using cached subtitles for {youtube_video_id}.")
            return existing

        yt_dlp_path = resolve_binary(self._addon_dir, "yt-dlp", "/opt/homebrew/bin/yt-dlp")
        deno_path = resolve_binary(self._addon_dir, "deno", "/opt/homebrew/bin/deno")
        if not Path(yt_dlp_path).exists():
            raise SubtitleFetchError("yt-dlp is not installed on this system.")

        youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        output_template = self._cache_dir / f"{youtube_video_id}.%(ext)s"
        errors: list[str] = []

        commands = [self._build_command(yt_dlp_path, youtube_url, output_template, deno_path, browser=None)]
        for browser in browser_cookie_order():
            commands.append(
                self._build_command(
                    yt_dlp_path,
                    youtube_url,
                    output_template,
                    deno_path,
                    browser=browser,
                )
            )

        for label, command in commands:
            self._cleanup_prefix(youtube_video_id)
            self._emit(progress_callback, f"Trying Korean manual subtitles via {label}...")
            try:
                completed = _run_command(command, timeout=YTDLP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                errors.append(f"{label}: timed out")
                continue
            if completed.returncode == 0:
                result = self._existing_track_result(youtube_video_id)
                if result is not None:
                    self._emit(progress_callback, f"Fetched Korean manual subtitles via {label}.")
                    return result
            message = completed.stderr.strip() or completed.stdout.strip() or "unknown yt-dlp failure"
            errors.append(f"{label}: {message.splitlines()[-1]}")
            self._logger.warning("Subtitle fetch failed for %s via %s: %s", youtube_video_id, label, message)
        raise SubtitleFetchError(
            "Could not fetch Korean manual subtitles. Recent errors: " + " | ".join(errors)
        )

    def _build_command(
        self,
        yt_dlp_path: str,
        youtube_url: str,
        output_template: Path,
        deno_path: str,
        *,
        browser: str | None,
    ) -> tuple[str, list[str]]:
        label = browser or "no cookies"
        command = [
            yt_dlp_path,
            "--no-config-locations",
            "--no-update",
            "--no-playlist",
            "--no-progress",
            "--skip-download",
            "--write-subs",
            "--sub-langs",
            "ko.*,ko",
            "--sub-format",
            "vtt",
            "--convert-subs",
            "vtt",
            "--force-overwrites",
            "--output",
            str(output_template),
            youtube_url,
        ]
        if browser:
            command[1:1] = ["--cookies-from-browser", browser]
        if Path(deno_path).exists():
            command[1:1] = ["--js-runtimes", f"deno:{deno_path}"]
        command[1:1] = ["--remote-components", "ejs:npm"]
        return label, command

    def _cleanup_prefix(self, youtube_video_id: str) -> None:
        for path in self._cache_dir.glob(f"{youtube_video_id}*"):
            if path.is_file():
                path.unlink(missing_ok=True)

    def _existing_track_result(self, youtube_video_id: str) -> SubtitleTrackResult | None:
        for path in sorted(self._candidate_subtitle_paths(youtube_video_id)):
            if ".live_chat." in path.name or path.stat().st_size <= 0:
                continue
            raw_payload = path.read_text(encoding="utf-8", errors="replace")
            cues = self._parse_cues_for_path(path, raw_payload)
            if not cues:
                continue
            checksum = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
            language_code = self._language_code_from_path(path)
            return SubtitleTrackResult(
                youtube_video_id=youtube_video_id,
                language_code=language_code,
                source_label="yt-dlp-manual-ko",
                checksum=checksum,
                raw_subtitle_path=path,
                cues=cues,
            )
        return None

    def _candidate_subtitle_paths(self, youtube_video_id: str) -> Iterable[Path]:
        patterns = (
            f"{youtube_video_id}*.ko*.vtt",
            f"{youtube_video_id}*.ko*.json3",
        )
        for pattern in patterns:
            yield from self._cache_dir.glob(pattern)

    def _parse_cues_for_path(self, path: Path, raw_payload: str) -> list[SubtitleCue]:
        if path.suffix == ".vtt":
            return parse_vtt_cues(raw_payload)
        return parse_json3_cues(raw_payload)

    def _language_code_from_path(self, path: Path) -> str:
        match = re.search(r"\.(ko(?:-[A-Za-z0-9]+)?)\.(?:json3|vtt)$", path.name)
        if match:
            return match.group(1)
        return "ko"

    def _emit(self, progress_callback: Callable[[str], None] | None, message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)
