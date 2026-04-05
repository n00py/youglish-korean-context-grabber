from __future__ import annotations

import difflib
import json
import logging
import math
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..provider.models import ContextCandidate
from .storage_paths import audio_cache_dir, log_path


class AudioClipError(RuntimeError):
    pass


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

YTDLP_ATTEMPT_TIMEOUT_SECONDS = 30

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


@dataclass(frozen=True)
class SubtitleToken:
    text: str
    normalized_text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class SubtitleAlignment:
    start_seconds: float
    end_seconds: float
    score: float
    matched_words: int


@dataclass(frozen=True)
class SubtitleTrackSelection:
    path: Path
    source_kind: str


def _normalized_sentence_text(text: str) -> str:
    value = re.sub(r"\s+", "", text or "")
    return value.strip("\"'`.,!?[](){}<>")


def _normalized_caption_text(text: str) -> str:
    value = text or ""
    value = value.replace("\n", " ")
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"[^0-9A-Za-z가-힣]+", "", value)
    return value


def _candidate_words(text: str) -> list[str]:
    return [_normalized_caption_text(part) for part in re.findall(r"[0-9A-Za-z가-힣]+", text or "") if _normalized_caption_text(part)]


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return 0.95
    return difflib.SequenceMatcher(None, left, right).ratio()


def _candidate_seconds(candidate: ContextCandidate, field_name: str) -> int:
    raw_value = candidate.raw_payload.get(field_name)
    try:
        return max(0, int(float(raw_value)))
    except (TypeError, ValueError, AttributeError):
        if field_name != "start":
            return 0
    timestamp = (candidate.timestamp or "").strip()
    if not timestamp:
        return 0
    parts = timestamp.split(":")
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return 0
    if len(values) == 3:
        hours, minutes, seconds = values
        return hours * 3600 + minutes * 60 + seconds
    if len(values) == 2:
        minutes, seconds = values
        return minutes * 60 + seconds
    if len(values) == 1:
        return values[0]
    return 0


def candidate_start_seconds(candidate: ContextCandidate) -> int:
    return _candidate_seconds(candidate, "start")


def candidate_end_seconds(candidate: ContextCandidate) -> int:
    raw_end = _candidate_seconds(candidate, "end")
    if raw_end > 0:
        return raw_end
    start = candidate_start_seconds(candidate)
    sentence = (candidate.sentence_text or "").strip()
    estimated_duration = max(2, min(8, len(sentence) // 8))
    return start + estimated_duration


def _estimated_sentence_duration(candidate: ContextCandidate) -> float:
    sentence = _normalized_sentence_text(candidate.sentence_text)
    if not sentence:
        return 2.2
    return min(8.0, max(2.2, len(sentence) * 0.22))


def _parse_json3_subtitle_tokens(path: Path) -> list[SubtitleToken]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    raw_tokens: list[list[int | str]] = []
    for event in payload.get("events") or []:
        segs = event.get("segs") or []
        if not isinstance(segs, list) or not segs:
            continue
        try:
            event_start = int(event.get("tStartMs", 0) or 0)
            event_duration = int(event.get("dDurationMs", 0) or 0)
        except (TypeError, ValueError):
            continue
        content_segs = [seg for seg in segs if isinstance(seg, dict) and seg.get("utf8") not in (None, "", "\n")]
        for index, seg in enumerate(content_segs):
            text = str(seg.get("utf8", ""))
            normalized_text = _normalized_caption_text(text)
            if not normalized_text:
                continue
            try:
                offset_ms = int(seg.get("tOffsetMs", 0) or 0)
            except (TypeError, ValueError):
                offset_ms = 0
            start_ms = event_start + offset_ms
            end_ms = event_start + event_duration
            for later in content_segs[index + 1 :]:
                try:
                    later_offset = int(later.get("tOffsetMs", 0) or 0)
                except (TypeError, ValueError):
                    later_offset = 0
                if later_offset > offset_ms:
                    end_ms = event_start + later_offset
                    break
            raw_tokens.append([text, normalized_text, start_ms, max(start_ms + 1, end_ms)])

    raw_tokens.sort(key=lambda token: int(token[2]))
    tokens: list[SubtitleToken] = []
    for index, item in enumerate(raw_tokens):
        text, normalized_text, start_ms, end_ms = item
        if index + 1 < len(raw_tokens):
            next_start_ms = int(raw_tokens[index + 1][2])
            if next_start_ms > int(start_ms):
                end_ms = min(int(end_ms), next_start_ms)
        tokens.append(
            SubtitleToken(
                text=str(text),
                normalized_text=str(normalized_text),
                start_ms=int(start_ms),
                end_ms=max(int(start_ms) + 1, int(end_ms)),
            )
        )
    return tokens


def _align_candidate_to_subtitle_tokens(
    candidate: ContextCandidate,
    subtitle_tokens: list[SubtitleToken],
    *,
    source_kind: str,
) -> SubtitleAlignment | None:
    if not subtitle_tokens:
        return None

    sentence_words = _candidate_words(candidate.sentence_text)
    if not sentence_words:
        return None

    anchor_word = _normalized_caption_text(candidate.matched_term) or sentence_words[-1]
    anchor_word_index = max(
        (index for index, word in enumerate(sentence_words) if _similarity(anchor_word, word) >= 0.9),
        default=len(sentence_words) - 1,
    )

    anchor_token_indexes = [
        index
        for index, token in enumerate(subtitle_tokens)
        if _similarity(anchor_word, token.normalized_text) >= 0.72
    ]
    if not anchor_token_indexes:
        return None

    normalized_sentence = _normalized_caption_text(candidate.sentence_text)
    best_alignment: SubtitleAlignment | None = None
    best_score = 0.0

    for anchor_token_index in anchor_token_indexes:
        matched_token_indexes: list[int | None] = [None] * len(sentence_words)
        anchor_similarity = _similarity(anchor_word, subtitle_tokens[anchor_token_index].normalized_text)
        matched_token_indexes[anchor_word_index] = anchor_token_index
        match_score = anchor_similarity * 1.5
        matched_count = 1

        cursor = anchor_token_index
        for word_index in range(anchor_word_index - 1, -1, -1):
            best_index: int | None = None
            best_word_score = 0.0
            for token_index in range(max(0, cursor - 10), cursor):
                score = _similarity(sentence_words[word_index], subtitle_tokens[token_index].normalized_text)
                if score > best_word_score:
                    best_word_score = score
                    best_index = token_index
            if best_index is not None and best_word_score >= 0.68:
                matched_token_indexes[word_index] = best_index
                cursor = best_index
                matched_count += 1
                match_score += best_word_score

        cursor = anchor_token_index
        for word_index in range(anchor_word_index + 1, len(sentence_words)):
            best_index = None
            best_word_score = 0.0
            for token_index in range(cursor + 1, min(len(subtitle_tokens), cursor + 11)):
                score = _similarity(sentence_words[word_index], subtitle_tokens[token_index].normalized_text)
                if score > best_word_score:
                    best_word_score = score
                    best_index = token_index
            if best_index is not None and best_word_score >= 0.68:
                matched_token_indexes[word_index] = best_index
                cursor = best_index
                matched_count += 1
                match_score += best_word_score

        found_indexes = [index for index in matched_token_indexes if index is not None]
        if not found_indexes:
            continue

        start_token_index = min(found_indexes)
        end_token_index = max(found_indexes)
        span_text = "".join(token.normalized_text for token in subtitle_tokens[start_token_index : end_token_index + 1])
        span_ratio = difflib.SequenceMatcher(None, normalized_sentence, span_text).ratio()
        total_score = match_score + (matched_count / len(sentence_words)) * 2.5 + span_ratio * 2.0
        total_score -= max(0, end_token_index - start_token_index) * 0.04

        if matched_count < max(1, len(sentence_words) - 1) or span_ratio < 0.45:
            continue

        if source_kind == "manual":
            start_padding_ms = 60
            end_padding_ms = 100
            minimum_duration_ms = 900
        else:
            start_padding_ms = 120
            end_padding_ms = 180
            minimum_duration_ms = 1000

        start_ms = max(0, subtitle_tokens[start_token_index].start_ms - start_padding_ms)
        end_ms = subtitle_tokens[end_token_index].end_ms + end_padding_ms
        alignment = SubtitleAlignment(
            start_seconds=start_ms / 1000.0,
            end_seconds=max((start_ms + minimum_duration_ms) / 1000.0, end_ms / 1000.0),
            score=total_score,
            matched_words=matched_count,
        )
        if alignment.score > best_score:
            best_score = alignment.score
            best_alignment = alignment

    return best_alignment


def planned_clip_window(candidate: ContextCandidate) -> tuple[float, float]:
    raw_start = candidate_start_seconds(candidate)
    raw_end = _candidate_seconds(candidate, "end")
    raw_duration = raw_end - raw_start if raw_end > raw_start else 0
    expected_duration = _estimated_sentence_duration(candidate)

    if raw_duration >= max(2.0, expected_duration * 0.7):
        clip_start = max(0.0, raw_start - 0.35)
        clip_end = raw_end + 0.45
    else:
        anchor_start = raw_end if raw_end > raw_start else raw_start + 0.8
        clip_start = max(0.0, anchor_start - 0.25)
        clip_end = clip_start + max(1.8, expected_duration + 0.35)

    clip_start_seconds = max(0.0, clip_start)
    clip_end_seconds = max(clip_start_seconds + 1.0, clip_end)
    return clip_start_seconds, clip_end_seconds


def candidate_hit_label(candidate: ContextCandidate) -> str:
    return candidate.timestamp or format_seconds_label(candidate_start_seconds(candidate))


def candidate_youtube_timestamp_url(candidate: ContextCandidate) -> str:
    video_id = (candidate.video_id or "").strip()
    if not video_id:
        return ""
    return f"https://www.youtube.com/watch?v={video_id}&t={candidate_start_seconds(candidate)}s"


def format_seconds_label(total_seconds: float | int) -> str:
    if total_seconds < 0:
        total_seconds = 0
    whole_seconds = int(math.floor(float(total_seconds)))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def candidate_range_label(candidate: ContextCandidate) -> str:
    start_seconds, end_seconds = planned_clip_window(candidate)
    return f"{format_seconds_label(start_seconds)} - {format_seconds_label(math.ceil(end_seconds))}"


def _section_timestamp(total_seconds: float | int) -> str:
    if total_seconds < 0:
        total_seconds = 0
    total_milliseconds = int(round(float(total_seconds) * 1000))
    hours, remainder = divmod(total_milliseconds, 3600 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _clip_cache_key_seconds(total_seconds: float | int) -> int:
    return max(0, int(round(float(total_seconds) * 1000)))


def _browser_cookie_order() -> tuple[str, ...]:
    if platform.system() != "Darwin":
        return SUPPORTED_COOKIE_BROWSERS

    ordered: list[str] = []
    app_dirs = (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications")
    for app_dir in app_dirs:
        if not app_dir.exists():
            continue
        try:
            entries = sorted(path.name for path in app_dir.iterdir())
        except Exception:
            continue
        for entry in entries:
            browser = MACOS_APP_BROWSER_MAP.get(entry)
            if browser and browser not in ordered:
                ordered.append(browser)

    if ordered:
        return tuple(ordered)
    for browser in SUPPORTED_COOKIE_BROWSERS:
        ordered.append(browser)
    return tuple(ordered)


def _resolve_binary(addon_dir: Path, binary_name: str, fallback: str) -> str:
    bundled = addon_dir / ".venv" / "bin" / binary_name
    if bundled.exists():
        return str(bundled)
    discovered = shutil.which(binary_name)
    if discovered:
        return discovered
    return fallback


class YouGlishAudioClipService:
    def __init__(self, addon_dir: Path, logger: logging.Logger | None = None) -> None:
        self._addon_dir = addon_dir
        self._logger = logger or logging.getLogger(__name__)
        self._cache_dir = audio_cache_dir(addon_dir)

    def ensure_clip(
        self,
        candidate: ContextCandidate,
        progress_callback: Callable[[str], None] | None = None,
    ) -> Path:
        yt_dlp_path = _resolve_binary(self._addon_dir, "yt-dlp", "/opt/homebrew/bin/yt-dlp")
        ffmpeg_path = _resolve_binary(self._addon_dir, "ffmpeg", "/opt/homebrew/bin/ffmpeg")
        deno_path_text = _resolve_binary(self._addon_dir, "deno", "/opt/homebrew/bin/deno")
        deno_path = Path(deno_path_text) if Path(deno_path_text).exists() else None

        if not Path(yt_dlp_path).exists():
            raise AudioClipError("yt-dlp is not installed on this system.")
        if not Path(ffmpeg_path).exists():
            raise AudioClipError("ffmpeg is not installed on this system.")
        if not candidate.video_id:
            raise AudioClipError("This YouGlish result does not include a YouTube video id.")
        if deno_path is None:
            self._emit(
                progress_callback,
                "No JavaScript runtime was found for yt-dlp; Firefox extraction may be less reliable.",
            )
        else:
            self._logger.info("Using deno runtime for yt-dlp: %s", deno_path)

        youtube_url = f"https://www.youtube.com/watch?v={candidate.video_id}"
        raw_start_seconds = candidate_start_seconds(candidate)
        raw_end_seconds = candidate_end_seconds(candidate)
        clip_start_seconds, clip_end_seconds = self._resolve_clip_window(
            candidate=candidate,
            youtube_url=youtube_url,
            yt_dlp_path=Path(yt_dlp_path),
            deno_path=deno_path,
            progress_callback=progress_callback,
        )
        clip_key = (
            f"{candidate.video_id}_"
            f"{_clip_cache_key_seconds(clip_start_seconds)}_"
            f"{_clip_cache_key_seconds(clip_end_seconds)}"
        )
        final_path = self._cache_dir / f"{clip_key}.mp3"
        self._emit(
            progress_callback,
            f"Preparing sentence clip {format_seconds_label(clip_start_seconds)} - {format_seconds_label(math.ceil(clip_end_seconds))}.",
        )
        raw_range = f"{format_seconds_label(raw_start_seconds)} - {format_seconds_label(raw_end_seconds)}"
        extracted_range = (
            f"{format_seconds_label(clip_start_seconds)} - "
            f"{format_seconds_label(math.ceil(clip_end_seconds))}"
        )
        if raw_range != extracted_range:
            self._emit(
                progress_callback,
                f"YouGlish hit was {raw_range}; extracting a tighter sentence window {extracted_range}.",
            )

        if final_path.exists() and final_path.stat().st_size > 0:
            self._emit(progress_callback, f"Using cached clip {final_path.name}.")
            return final_path

        for stale_path in self._cache_dir.glob(f"{clip_key}.*"):
            if stale_path.exists() and stale_path.stat().st_size == 0:
                stale_path.unlink(missing_ok=True)

        source_audio = self._ensure_source_audio(
            youtube_url=youtube_url,
            video_id=candidate.video_id,
            yt_dlp_path=Path(yt_dlp_path),
            ffmpeg_path=Path(ffmpeg_path),
            deno_path=deno_path,
            progress_callback=progress_callback,
        )
        self._extract_clip(
            source_audio=source_audio,
            final_path=final_path,
            start_seconds=clip_start_seconds,
            end_seconds=clip_end_seconds,
            ffmpeg_path=Path(ffmpeg_path),
            progress_callback=progress_callback,
        )
        if final_path.exists() and final_path.stat().st_size > 0:
            self._emit(progress_callback, f"Clip ready: {final_path.name}")
            return final_path
        raise AudioClipError("ffmpeg finished, but no local sentence audio clip was created.")

    def _resolve_clip_window(
        self,
        candidate: ContextCandidate,
        youtube_url: str,
        yt_dlp_path: Path,
        deno_path: Path | None,
        progress_callback: Callable[[str], None] | None,
    ) -> tuple[float, float]:
        fallback_window = planned_clip_window(candidate)
        saw_any_subtitles = False
        for source_kind in ("manual", "auto"):
            subtitle_selection = self._ensure_subtitle_tokens(
                youtube_url=youtube_url,
                video_id=candidate.video_id,
                yt_dlp_path=yt_dlp_path,
                deno_path=deno_path,
                progress_callback=progress_callback,
                source_kind=source_kind,
            )
            if subtitle_selection is None:
                continue
            saw_any_subtitles = True

            alignment = _align_candidate_to_subtitle_tokens(
                candidate,
                _parse_json3_subtitle_tokens(subtitle_selection.path),
                source_kind=subtitle_selection.source_kind,
            )
            if alignment is None:
                self._emit(
                    progress_callback,
                    f"{subtitle_selection.source_kind.capitalize()} Korean subtitles did not align cleanly to this sentence.",
                )
                continue

            self._emit(
                progress_callback,
                f"Aligned sentence to {subtitle_selection.source_kind} Korean subtitle cues: "
                f"{format_seconds_label(alignment.start_seconds)} - "
                f"{format_seconds_label(math.ceil(alignment.end_seconds))} "
                f"({alignment.matched_words} cue matches).",
            )
            return alignment.start_seconds, alignment.end_seconds

        if saw_any_subtitles:
            self._emit(progress_callback, "Could not align the sentence to available subtitle cues; using YouGlish timing.")
        else:
            self._emit(progress_callback, "No Korean subtitle cues were available; using YouGlish timing.")
        return fallback_window

    def _ensure_subtitle_tokens(
        self,
        youtube_url: str,
        video_id: str,
        yt_dlp_path: Path,
        deno_path: Path | None,
        progress_callback: Callable[[str], None] | None,
        source_kind: str,
    ) -> SubtitleTrackSelection | None:
        subtitle_selection = self._find_cached_subtitle_file(video_id, source_kind=source_kind)
        if subtitle_selection is None:
            subtitle_selection = self._download_subtitle_file(
                youtube_url=youtube_url,
                video_id=video_id,
                yt_dlp_path=yt_dlp_path,
                deno_path=deno_path,
                progress_callback=progress_callback,
                source_kind=source_kind,
            )
        return subtitle_selection

    def _download_subtitle_file(
        self,
        youtube_url: str,
        video_id: str,
        yt_dlp_path: Path,
        deno_path: Path | None,
        progress_callback: Callable[[str], None] | None,
        source_kind: str,
    ) -> SubtitleTrackSelection | None:
        if source_kind == "manual":
            write_flag = "--write-subs"
        else:
            write_flag = "--write-auto-subs"

        self._emit(progress_callback, "Trying Korean subtitle cues for tighter alignment...")
        self._emit(progress_callback, f"Looking for {source_kind} Korean subtitles...")
        output_template = self._cache_dir / f"{video_id}_subs_{source_kind}.%(ext)s"
        for browser in _browser_cookie_order():
            self._emit(progress_callback, f"Trying {browser} {source_kind} subtitle cookies...")
            self._cleanup_prefix(f"{video_id}_subs_{source_kind}")
            command = [
                str(yt_dlp_path),
                "--no-config-locations",
                "--no-update",
                "--no-playlist",
                "--no-progress",
                "--skip-download",
                write_flag,
                "--sub-langs",
                "ko",
                "--sub-format",
                "json3",
                "--force-overwrites",
                "--cookies-from-browser",
                browser,
                "-o",
                str(output_template),
                youtube_url,
            ]
            if deno_path is not None:
                command[1:1] = ["--js-runtimes", f"deno:{deno_path}"]
            command[1:1] = ["--remote-components", "ejs:npm"]

            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=YTDLP_ATTEMPT_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                self._emit(
                    progress_callback,
                    f"{browser} {source_kind} subtitle cues timed out after {YTDLP_ATTEMPT_TIMEOUT_SECONDS}s.",
                )
                self._logger.warning(
                    "yt-dlp %s subtitle cue attempt timed out for %s after %s seconds",
                    source_kind,
                    browser,
                    YTDLP_ATTEMPT_TIMEOUT_SECONDS,
                )
                continue
            subtitle_selection = self._find_cached_subtitle_file(video_id, source_kind=source_kind)
            if completed.returncode == 0 and subtitle_selection is not None:
                self._emit(
                    progress_callback,
                    f"{browser} {source_kind} subtitle cues worked. Downloaded {subtitle_selection.path.name}.",
                )
                return subtitle_selection
            error_text = completed.stderr.strip() or completed.stdout.strip()
            if error_text:
                summary = error_text.splitlines()[-1]
                self._emit(progress_callback, f"{browser} {source_kind} subtitle cues failed: {summary}")
                self._logger.warning(
                    "yt-dlp %s subtitle cue attempt failed for %s [%s]: %s",
                    source_kind,
                    video_id,
                    browser,
                    error_text,
                )
        return None

    def _ensure_source_audio(
        self,
        youtube_url: str,
        video_id: str,
        yt_dlp_path: Path,
        ffmpeg_path: Path,
        deno_path: Path | None,
        progress_callback: Callable[[str], None] | None,
    ) -> Path:
        existing = self._find_cached_audio(f"{video_id}_full")
        if existing is not None:
            self._emit(progress_callback, f"Using cached source audio {existing.name}.")
            return existing

        output_template = self._cache_dir / f"{video_id}_full.%(ext)s"
        errors: list[str] = []
        browsers = _browser_cookie_order()
        self._emit(
            progress_callback,
            "Trying browser cookies in this order: " + ", ".join(browsers),
        )
        for browser in _browser_cookie_order():
            self._emit(progress_callback, f"Trying {browser} cookies...")
            self._cleanup_prefix(f"{video_id}_full")
            command = [
                str(yt_dlp_path),
                "--no-config-locations",
                "--no-update",
                "--no-playlist",
                "--no-progress",
                "--extractor-retries",
                "1",
                "--retries",
                "1",
                "--fragment-retries",
                "1",
                "--retry-sleep",
                "http:1",
                "--retry-sleep",
                "fragment:1",
                "--abort-on-unavailable-fragments",
                "--force-overwrites",
                "--no-part",
                "--ffmpeg-location",
                str(ffmpeg_path.parent),
                "--cookies-from-browser",
                browser,
                "-f",
                "bestaudio/best",
                "-o",
                str(output_template),
                youtube_url,
            ]
            if deno_path is not None:
                command[1:1] = ["--js-runtimes", f"deno:{deno_path}"]
            command[1:1] = ["--remote-components", "ejs:npm"]

            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=YTDLP_ATTEMPT_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                errors.append(
                    f"{browser}: timed out after {YTDLP_ATTEMPT_TIMEOUT_SECONDS} seconds"
                )
                self._logger.warning(
                    "yt-dlp browser cookie attempt timed out for %s after %s seconds",
                    browser,
                    YTDLP_ATTEMPT_TIMEOUT_SECONDS,
                )
                self._emit(
                    progress_callback,
                    f"{browser} timed out after {YTDLP_ATTEMPT_TIMEOUT_SECONDS}s.",
                )
                continue
            if completed.returncode == 0:
                source_audio = self._find_cached_audio(f"{video_id}_full")
                if source_audio is not None:
                    self._logger.info("yt-dlp succeeded with browser cookies: %s", browser)
                    self._emit(progress_callback, f"{browser} cookies worked. Downloaded {source_audio.name}.")
                    return source_audio
                errors.append(f"{browser}: yt-dlp succeeded but produced no audio file")
                self._emit(progress_callback, f"{browser} cookies returned success, but no audio file was created.")
                continue

            error_text = completed.stderr.strip() or completed.stdout.strip()
            errors.append(f"{browser}: {error_text}")
            self._logger.warning("yt-dlp browser cookie attempt failed for %s: %s", browser, error_text)
            summary = error_text.splitlines()[-1] if error_text else "Unknown yt-dlp failure."
            self._emit(progress_callback, f"{browser} failed: {summary}")

        joined_errors = " | ".join(errors) if errors else "No browser cookie attempts were made."
        raise AudioClipError(
            "Could not fetch the YouTube audio stream with any browser cookies. "
            "Try being signed into YouTube in Firefox, Chrome, or Safari. "
            f"See {log_path(self._addon_dir)} for details. "
            f"Recent errors: {joined_errors}"
        )

    def _extract_clip(
        self,
        source_audio: Path,
        final_path: Path,
        start_seconds: float,
        end_seconds: float,
        ffmpeg_path: Path,
        progress_callback: Callable[[str], None] | None,
    ) -> None:
        duration = max(1.0, end_seconds - start_seconds)
        final_path.unlink(missing_ok=True)
        self._emit(
            progress_callback,
            f"Cutting local clip with ffmpeg: {format_seconds_label(start_seconds)} - {format_seconds_label(math.ceil(end_seconds))}.",
        )
        command = [
            str(ffmpeg_path),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source_audio),
            "-ss",
            _section_timestamp(start_seconds),
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "160k",
            str(final_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            error_text = completed.stderr.strip() or completed.stdout.strip()
            self._logger.error(
                "ffmpeg clip extraction failed for %s [%s-%s]: %s",
                source_audio.name,
                start_seconds,
                end_seconds,
                error_text,
            )
            raise AudioClipError(
                "ffmpeg could not cut the selected sentence audio. "
                f"See {log_path(self._addon_dir)} for details."
            )
        self._emit(progress_callback, "ffmpeg finished cutting the sentence clip.")

    def _find_cached_audio(self, prefix: str) -> Path | None:
        for path in sorted(self._cache_dir.glob(f"{prefix}.*")):
            if path.suffix.lower() in {".m4a", ".mp3", ".aac", ".opus", ".wav", ".flac", ".webm", ".m4b"}:
                if path.is_file() and path.stat().st_size > 0:
                    return path
        return None

    def _find_cached_subtitle_file(self, video_id: str, *, source_kind: str) -> SubtitleTrackSelection | None:
        preferred_suffixes = (
            ".ko-orig.json3",
            ".ko.json3",
            ".ko-ko.json3",
        )
        for suffix in preferred_suffixes:
            path = self._cache_dir / f"{video_id}_subs_{source_kind}{suffix}"
            if path.is_file() and path.stat().st_size > 0:
                return SubtitleTrackSelection(path=path, source_kind=source_kind)
        for path in sorted(self._cache_dir.glob(f"{video_id}_subs_{source_kind}*.json3")):
            if path.is_file() and path.stat().st_size > 0:
                return SubtitleTrackSelection(path=path, source_kind=source_kind)
        return None

    def _cleanup_prefix(self, prefix: str) -> None:
        for path in self._cache_dir.glob(f"{prefix}.*"):
            path.unlink(missing_ok=True)

    def _emit(
        self,
        progress_callback: Callable[[str], None] | None,
        message: str,
    ) -> None:
        self._logger.info(message)
        if progress_callback is not None:
            try:
                progress_callback(message)
            except Exception:
                self._logger.exception("Progress callback failed")
