from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .client import KimchiAPIClient, KimchiBrowseCursor, latest_browse_cursor, youtube_source_id
from .db import KimchiCorpusDatabase
from .subtitles import ManualKoreanSubtitleFetcher, SubtitleFetchError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class BackfillSummary:
    run_id: int
    pages_fetched: int
    items_discovered: int
    hydrated: int
    subtitle_ready: int


class KimchiCorpusIngestor:
    def __init__(
        self,
        addon_dir: Path,
        db: KimchiCorpusDatabase,
        logger: logging.Logger | None = None,
        client: KimchiAPIClient | None = None,
        subtitle_fetcher: ManualKoreanSubtitleFetcher | None = None,
    ) -> None:
        self._addon_dir = addon_dir
        self._db = db
        self._logger = logger or logging.getLogger(__name__)
        self._client = client or KimchiAPIClient(logger=self._logger)
        self._subtitle_fetcher = subtitle_fetcher or ManualKoreanSubtitleFetcher(addon_dir, self._logger)
        self._recipe_hash = hashlib.sha256(b"kimchi-browse-stars-youtube-v1").hexdigest()

    def backfill(
        self,
        *,
        progress_callback: Callable[[str], None] | None = None,
        max_pages: int | None = None,
        resume: bool = True,
        sleep_between_pages: float = 0.0,
        sleep_between_items: float = 0.0,
        retry_cooldown_seconds: float = 900.0,
    ) -> BackfillSummary:
        run_id = self._db.begin_discovery_run(self._recipe_hash, utc_now_iso(), resume=resume)
        cursor_state = self._db.latest_discovery_cursor(self._recipe_hash, active_only=True) if resume else None
        cursor = None
        if cursor_state and cursor_state.get("cursor_last_row_id"):
            cursor = KimchiBrowseCursor(
                last_row_id=str(cursor_state["cursor_last_row_id"]),
                last_star_count=cursor_state.get("cursor_last_star_count"),
                last_complexity_score=cursor_state.get("cursor_last_complexity_score"),
                last_comprehension_percentage=cursor_state.get("cursor_last_comprehension_percentage"),
            )

        pages_fetched = 0
        items_discovered = 0
        hydrated = 0
        subtitle_ready = 0
        try:
            while True:
                if max_pages is not None and pages_fetched >= max_pages:
                    break
                payload = self._client.browse_items(cursor)
                items = payload.get("items") or []
                if not isinstance(items, list) or not items:
                    break
                new_items = 0
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if not youtube_source_id(item):
                        continue
                    if self._db.upsert_browse_item(item):
                        new_items += 1
                next_cursor = latest_browse_cursor(item for item in items if isinstance(item, dict))
                self._db.discovery_checkpoint(
                    run_id,
                    cursor_last_row_id=next_cursor.last_row_id if next_cursor else None,
                    cursor_last_star_count=next_cursor.last_star_count if next_cursor else None,
                    cursor_last_complexity_score=next_cursor.last_complexity_score if next_cursor else None,
                    cursor_last_comprehension_percentage=(
                        next_cursor.last_comprehension_percentage if next_cursor else None
                    ),
                    pages_fetched_delta=1,
                    items_discovered_delta=len(items),
                )
                pages_fetched += 1
                items_discovered += len(items)
                self._emit(progress_callback, f"Fetched Kimchi browse page {pages_fetched} with {len(items)} items.")
                hydrated_delta, subtitle_delta = self._hydrate_and_fetch_subtitles(
                    progress_callback=progress_callback,
                    sleep_between_items=sleep_between_items,
                    retry_cooldown_seconds=retry_cooldown_seconds,
                )
                hydrated += hydrated_delta
                subtitle_ready += subtitle_delta
                self._sleep_with_jitter(sleep_between_pages)
                if next_cursor is None or next_cursor.last_row_id == (cursor.last_row_id if cursor else ""):
                    break
                cursor = next_cursor
            while self._db.pending_hydration_ids(1):
                hydrated_delta, subtitle_delta = self._hydrate_and_fetch_subtitles(
                    progress_callback=progress_callback,
                    sleep_between_items=sleep_between_items,
                    retry_cooldown_seconds=retry_cooldown_seconds,
                )
                if hydrated_delta == 0 and subtitle_delta == 0:
                    break
                hydrated += hydrated_delta
                subtitle_ready += subtitle_delta
            self._db.finish_discovery_run(run_id, utc_now_iso(), None)
        except KeyboardInterrupt as exc:
            self._db.pause_discovery_run(run_id, utc_now_iso(), str(exc) or "Interrupted by user")
            self._emit(progress_callback, "Paused discovery cleanly after interrupt.")
            raise
        except Exception as exc:
            self._db.finish_discovery_run(run_id, utc_now_iso(), str(exc))
            raise
        return BackfillSummary(
            run_id=run_id,
            pages_fetched=pages_fetched,
            items_discovered=items_discovered,
            hydrated=hydrated,
            subtitle_ready=subtitle_ready,
        )

    def recheck_discovery(self, *, progress_callback: Callable[[str], None] | None = None) -> BackfillSummary:
        return self.backfill(progress_callback=progress_callback, resume=False)

    def recheck_subtitles(
        self,
        *,
        progress_callback: Callable[[str], None] | None = None,
        limit: int = 100,
        sleep_between_items: float = 0.0,
        retry_cooldown_seconds: float = 900.0,
    ) -> int:
        processed = 0
        failed_retry_before = _iso_timestamp_seconds_ago(retry_cooldown_seconds) if retry_cooldown_seconds > 0 else None
        for youtube_video_id in self._db.pending_subtitle_video_ids(limit, failed_retry_before=failed_retry_before):
            self._emit(progress_callback, f"Rechecking subtitles for {youtube_video_id}...")
            checked_at = utc_now_iso()
            try:
                result = self._subtitle_fetcher.fetch_for_video(
                    youtube_video_id,
                    progress_callback=progress_callback,
                )
            except SubtitleFetchError as exc:
                self._db.record_subtitle_failure(youtube_video_id, checked_at, str(exc))
                self._logger.warning("Subtitle recheck failed for %s: %s", youtube_video_id, exc)
                continue
            self._db.store_subtitle_track(youtube_video_id, result, checked_at)
            processed += 1
            self._sleep_with_jitter(sleep_between_items)
        return processed

    def _hydrate_and_fetch_subtitles(
        self,
        *,
        progress_callback: Callable[[str], None] | None = None,
        batch_size: int = 50,
        sleep_between_items: float = 0.0,
        retry_cooldown_seconds: float = 900.0,
    ) -> tuple[int, int]:
        hydrated = 0
        subtitle_ready = 0
        failed_retry_before = _iso_timestamp_seconds_ago(retry_cooldown_seconds) if retry_cooldown_seconds > 0 else None
        for kimchi_id in self._db.pending_hydration_ids(batch_size):
            attempted_at = utc_now_iso()
            self._emit(progress_callback, f"Hydrating Kimchi item {kimchi_id}...")
            try:
                item_payload = self._client.get_media_item(kimchi_id)
                youtube_video_id = self._db.upsert_hydrated_item(item_payload, attempted_at)
                hydrated += 1
            except Exception as exc:
                self._db.record_hydration_failure(kimchi_id, attempted_at, str(exc))
                self._logger.warning("Hydration failed for %s: %s", kimchi_id, exc)
                continue
            if not youtube_video_id:
                self._sleep_with_jitter(sleep_between_items)
                continue
            if not self._db.subtitle_retry_allowed(
                youtube_video_id,
                failed_retry_before=failed_retry_before,
            ):
                self._emit(progress_callback, f"Skipping recent failed subtitle retry for {youtube_video_id}.")
                self._sleep_with_jitter(sleep_between_items)
                continue
            self._emit(progress_callback, f"Fetching Korean manual subtitles for {youtube_video_id}...")
            try:
                track_result = self._subtitle_fetcher.fetch_for_video(
                    youtube_video_id,
                    progress_callback=progress_callback,
                )
            except SubtitleFetchError as exc:
                self._db.record_subtitle_failure(youtube_video_id, utc_now_iso(), str(exc))
                self._logger.warning("Subtitle fetch failed for %s: %s", youtube_video_id, exc)
                self._sleep_with_jitter(sleep_between_items)
                continue
            self._db.store_subtitle_track(youtube_video_id, track_result, utc_now_iso())
            subtitle_ready += 1
            self._sleep_with_jitter(sleep_between_items)
        return hydrated, subtitle_ready

    def _emit(self, progress_callback: Callable[[str], None] | None, message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)

    def _sleep_with_jitter(self, seconds: float) -> None:
        delay = max(0.0, float(seconds))
        if delay <= 0:
            return
        time.sleep(delay + random.uniform(0.0, min(0.5, delay * 0.25)))


def _iso_timestamp_seconds_ago(seconds: float) -> str:
    cutoff = datetime.now(timezone.utc).timestamp() - max(0.0, seconds)
    return datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
