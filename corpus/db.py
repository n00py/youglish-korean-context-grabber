from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping

from .text import expand_search_forms, json_dumps, normalize_text


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS kimchi_media (
        kimchi_id TEXT PRIMARY KEY,
        youtube_video_id TEXT,
        youtube_url TEXT,
        name_ko TEXT,
        name_en TEXT,
        duration_sec INTEGER,
        stars INTEGER,
        starred INTEGER,
        lemma_count INTEGER,
        complexity_score REAL,
        release_date TEXT,
        hidden INTEGER,
        unrecognized_count INTEGER,
        updated_at TEXT,
        thumbnail_shape TEXT,
        has_content INTEGER,
        group_id TEXT,
        group_name_ko TEXT,
        group_name_en TEXT,
        media_stats_json TEXT,
        vocab_comp_scatterplot_json TEXT,
        last_hydrated_at TEXT,
        last_subtitle_check_at TEXT,
        status TEXT NOT NULL DEFAULT 'discovered',
        raw_browse_json TEXT,
        raw_item_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS youtube_videos (
        youtube_video_id TEXT PRIMARY KEY,
        youtube_url TEXT NOT NULL,
        duration_seconds INTEGER,
        subtitle_status TEXT NOT NULL DEFAULT 'unknown',
        availability_status TEXT NOT NULL DEFAULT 'unknown',
        first_seen_at TEXT NOT NULL,
        last_checked_at TEXT,
        preferred_kimchi_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subtitle_tracks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        youtube_video_id TEXT NOT NULL,
        kimchi_id TEXT,
        language_code TEXT NOT NULL,
        is_manual INTEGER NOT NULL DEFAULT 1,
        source_label TEXT NOT NULL,
        fetch_status TEXT NOT NULL,
        stars INTEGER,
        complexity_score REAL,
        lemma_count INTEGER,
        unrecognized_count INTEGER,
        checksum TEXT,
        fetched_at TEXT NOT NULL,
        raw_subtitle_path TEXT,
        is_active INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subtitle_cues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id INTEGER NOT NULL,
        cue_index INTEGER NOT NULL,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        text TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        tokenized_text TEXT NOT NULL
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS subtitle_cues_fts
    USING fts5(text, normalized_text, tokenized_text, content='subtitle_cues', content_rowid='id')
    """,
    """
    CREATE TABLE IF NOT EXISTS video_terms (
        youtube_video_id TEXT NOT NULL,
        normalized_term TEXT NOT NULL,
        term_count INTEGER NOT NULL,
        PRIMARY KEY (youtube_video_id, normalized_term)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kimchi_media_video_map (
        kimchi_id TEXT NOT NULL,
        youtube_video_id TEXT NOT NULL,
        is_primary INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (kimchi_id, youtube_video_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS discovery_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipe_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        cursor_last_row_id TEXT,
        cursor_last_star_count INTEGER,
        cursor_last_complexity_score TEXT,
        cursor_last_comprehension_percentage TEXT,
        pages_fetched INTEGER NOT NULL DEFAULT 0,
        items_discovered INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error_message TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hydration_runs (
        kimchi_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        last_attempted_at TEXT NOT NULL,
        last_succeeded_at TEXT,
        error_message TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subtitle_runs (
        youtube_video_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        last_attempted_at TEXT NOT NULL,
        last_succeeded_at TEXT,
        error_message TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kimchi_media_youtube_video_id ON kimchi_media(youtube_video_id)",
    "CREATE INDEX IF NOT EXISTS idx_subtitle_tracks_video_active ON subtitle_tracks(youtube_video_id, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_subtitle_cues_track_start ON subtitle_cues(track_id, start_ms)",
    "CREATE INDEX IF NOT EXISTS idx_video_terms_term ON video_terms(normalized_term)",
    "CREATE INDEX IF NOT EXISTS idx_kimchi_status ON kimchi_media(status)",
)


class KimchiCorpusDatabase:
    def __init__(self, path: Path, logger: logging.Logger | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self.path))
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _initialize(self) -> None:
        with self.connect() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            self._apply_schema_migrations(conn)

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            stats = {
                "kimchi_media": conn.execute("SELECT COUNT(*) FROM kimchi_media").fetchone()[0],
                "youtube_videos": conn.execute("SELECT COUNT(*) FROM youtube_videos").fetchone()[0],
                "subtitle_tracks": conn.execute("SELECT COUNT(*) FROM subtitle_tracks").fetchone()[0],
                "subtitle_cues": conn.execute("SELECT COUNT(*) FROM subtitle_cues").fetchone()[0],
                "eligible_videos": conn.execute(
                    "SELECT COUNT(*) FROM youtube_videos WHERE subtitle_status = 'ready'"
                ).fetchone()[0],
            }
            last_discovery = conn.execute(
                "SELECT status, started_at, finished_at, error_message FROM discovery_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last_discovery is not None:
                stats["last_discovery_run"] = dict(last_discovery)
            return stats

    def begin_discovery_run(self, recipe_hash: str, started_at: str, resume: bool) -> int:
        with self.connect() as conn:
            if resume:
                row = conn.execute(
                    """
                    SELECT id FROM discovery_runs
                    WHERE recipe_hash = ? AND status IN ('running', 'paused')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (recipe_hash,),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE discovery_runs SET status = 'running', error_message = NULL WHERE id = ?",
                        (row["id"],),
                    )
                    return int(row["id"])
            cursor = conn.execute(
                """
                INSERT INTO discovery_runs (recipe_hash, status, started_at)
                VALUES (?, 'running', ?)
                """,
                (recipe_hash, started_at),
            )
            return int(cursor.lastrowid)

    def discovery_checkpoint(
        self,
        run_id: int,
        *,
        cursor_last_row_id: str | None,
        cursor_last_star_count: int | None,
        cursor_last_complexity_score: str | None,
        cursor_last_comprehension_percentage: str | None,
        pages_fetched_delta: int,
        items_discovered_delta: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE discovery_runs
                SET cursor_last_row_id = ?,
                    cursor_last_star_count = ?,
                    cursor_last_complexity_score = ?,
                    cursor_last_comprehension_percentage = ?,
                    pages_fetched = pages_fetched + ?,
                    items_discovered = items_discovered + ?
                WHERE id = ?
                """,
                (
                    cursor_last_row_id,
                    cursor_last_star_count,
                    cursor_last_complexity_score,
                    cursor_last_comprehension_percentage,
                    pages_fetched_delta,
                    items_discovered_delta,
                    run_id,
                ),
            )

    def finish_discovery_run(self, run_id: int, finished_at: str, error_message: str | None = None) -> None:
        status = "failed" if error_message else "completed"
        with self.connect() as conn:
            conn.execute(
                "UPDATE discovery_runs SET status = ?, finished_at = ?, error_message = ? WHERE id = ?",
                (status, finished_at, error_message, run_id),
            )

    def latest_discovery_cursor(self, recipe_hash: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT cursor_last_row_id, cursor_last_star_count, cursor_last_complexity_score,
                       cursor_last_comprehension_percentage
                FROM discovery_runs
                WHERE recipe_hash = ? AND cursor_last_row_id IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """,
                (recipe_hash,),
            ).fetchone()
            return dict(row) if row is not None else None

    def upsert_browse_item(self, item: Mapping[str, Any]) -> bool:
        kimchi_id = str(item.get("id", "")).strip()
        if not kimchi_id:
            return False
        youtube_video_id = self._youtube_video_id_from_sources(item.get("sources") or [])
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT kimchi_id FROM kimchi_media WHERE kimchi_id = ?",
                (kimchi_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO kimchi_media (
                    kimchi_id, youtube_video_id, youtube_url, name_ko, name_en, duration_sec, stars,
                    starred, lemma_count, complexity_score, release_date, hidden, unrecognized_count,
                    updated_at, thumbnail_shape, status, raw_browse_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?)
                ON CONFLICT(kimchi_id) DO UPDATE SET
                    youtube_video_id = excluded.youtube_video_id,
                    youtube_url = excluded.youtube_url,
                    name_ko = excluded.name_ko,
                    name_en = excluded.name_en,
                    duration_sec = excluded.duration_sec,
                    stars = excluded.stars,
                    starred = excluded.starred,
                    lemma_count = excluded.lemma_count,
                    complexity_score = excluded.complexity_score,
                    release_date = excluded.release_date,
                    hidden = excluded.hidden,
                    unrecognized_count = excluded.unrecognized_count,
                    updated_at = excluded.updated_at,
                    thumbnail_shape = excluded.thumbnail_shape,
                    raw_browse_json = excluded.raw_browse_json,
                    status = CASE
                        WHEN kimchi_media.raw_browse_json != excluded.raw_browse_json THEN 'discovered'
                        ELSE kimchi_media.status
                    END
                """,
                (
                    kimchi_id,
                    youtube_video_id,
                    f"https://www.youtube.com/watch?v={youtube_video_id}" if youtube_video_id else "",
                    str(item.get("name_ko", "") or ""),
                    str(item.get("name_en", "") or ""),
                    int(item.get("duration_sec", 0) or 0),
                    int(item.get("stars", 0) or 0),
                    1 if item.get("starred") else 0,
                    int(item.get("lemma_count", 0) or 0),
                    float(item.get("complexity_score", 0) or 0),
                    str(item.get("release_date", "") or ""),
                    1 if item.get("hidden") else 0,
                    int(item.get("unrecognized_count", 0) or 0),
                    str(item.get("updated_at", "") or ""),
                    str(item.get("thumbnail_shape", "") or ""),
                    json_dumps(dict(item)),
                ),
            )
            return existing is None

    def upsert_hydrated_item(self, item: Mapping[str, Any], hydrated_at: str) -> str:
        kimchi_id = str(item.get("id", "")).strip()
        youtube_video_id = self._youtube_video_id_from_sources(item.get("sources") or [])
        youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}" if youtube_video_id else ""
        group = item.get("group") or {}
        media_stats = item.get("media_stats")
        vocab_scatter = item.get("vocab_comp_scatterplot")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO kimchi_media (
                    kimchi_id, youtube_video_id, youtube_url, name_ko, name_en, duration_sec,
                    stars, lemma_count, complexity_score, release_date, has_content, group_id,
                    group_name_ko, group_name_en, media_stats_json, vocab_comp_scatterplot_json,
                    updated_at, last_hydrated_at, status, raw_item_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'hydrated', ?)
                ON CONFLICT(kimchi_id) DO UPDATE SET
                    youtube_video_id = excluded.youtube_video_id,
                    youtube_url = excluded.youtube_url,
                    name_ko = excluded.name_ko,
                    name_en = excluded.name_en,
                    duration_sec = excluded.duration_sec,
                    stars = excluded.stars,
                    lemma_count = excluded.lemma_count,
                    complexity_score = excluded.complexity_score,
                    release_date = excluded.release_date,
                    has_content = excluded.has_content,
                    group_id = excluded.group_id,
                    group_name_ko = excluded.group_name_ko,
                    group_name_en = excluded.group_name_en,
                    media_stats_json = excluded.media_stats_json,
                    vocab_comp_scatterplot_json = excluded.vocab_comp_scatterplot_json,
                    updated_at = excluded.updated_at,
                    last_hydrated_at = excluded.last_hydrated_at,
                    status = 'hydrated',
                    raw_item_json = excluded.raw_item_json
                """,
                (
                    kimchi_id,
                    youtube_video_id,
                    youtube_url,
                    str(item.get("name_ko", "") or ""),
                    str(item.get("name_en", "") or ""),
                    int(item.get("duration_sec", 0) or 0),
                    int(item.get("stars", 0) or 0),
                    int(item.get("lemma_count", 0) or 0),
                    float(item.get("complexity_score", 0) or 0),
                    str(item.get("release_date", "") or ""),
                    1 if item.get("has_content") else 0,
                    str(group.get("id", "") or ""),
                    str(group.get("name_ko", "") or ""),
                    str(group.get("name_en", "") or ""),
                    json_dumps(media_stats),
                    json_dumps(vocab_scatter),
                    str(item.get("updated_at", "") or ""),
                    hydrated_at,
                    json_dumps(dict(item)),
                ),
            )
            now = hydrated_at
            conn.execute(
                """
                INSERT INTO youtube_videos (
                    youtube_video_id, youtube_url, duration_seconds, first_seen_at, last_checked_at, preferred_kimchi_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(youtube_video_id) DO UPDATE SET
                    youtube_url = excluded.youtube_url,
                    duration_seconds = excluded.duration_seconds,
                    last_checked_at = excluded.last_checked_at
                """,
                (
                    youtube_video_id,
                    youtube_url,
                    int(item.get("duration_sec", 0) or 0),
                    now,
                    now,
                    kimchi_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO kimchi_media_video_map (kimchi_id, youtube_video_id, is_primary)
                VALUES (?, ?, 0)
                ON CONFLICT(kimchi_id, youtube_video_id) DO NOTHING
                """,
                (kimchi_id, youtube_video_id),
            )
            self._refresh_primary_mapping(conn, youtube_video_id)
            self._record_hydration_run(conn, kimchi_id, "completed", hydrated_at, None)
        return youtube_video_id

    def record_hydration_failure(self, kimchi_id: str, attempted_at: str, error_message: str) -> None:
        with self.connect() as conn:
            self._record_hydration_run(conn, kimchi_id, "failed", attempted_at, error_message)

    def store_subtitle_track(
        self,
        youtube_video_id: str,
        track_result: Any,
        checked_at: str,
    ) -> int:
        with self.connect() as conn:
            metadata_row = conn.execute(
                """
                SELECT kimchi_id, stars, complexity_score, lemma_count, unrecognized_count
                FROM kimchi_media
                WHERE youtube_video_id = ?
                ORDER BY stars DESC, complexity_score ASC, updated_at DESC, kimchi_id ASC
                LIMIT 1
                """,
                (youtube_video_id,),
            ).fetchone()
            conn.execute(
                "UPDATE subtitle_tracks SET is_active = 0 WHERE youtube_video_id = ?",
                (youtube_video_id,),
            )
            cursor = conn.execute(
                """
                INSERT INTO subtitle_tracks (
                    youtube_video_id, kimchi_id, language_code, is_manual, source_label, fetch_status,
                    stars, complexity_score, lemma_count, unrecognized_count,
                    checksum, fetched_at, raw_subtitle_path, is_active
                ) VALUES (?, ?, ?, 1, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    youtube_video_id,
                    str(metadata_row["kimchi_id"]) if metadata_row is not None else None,
                    track_result.language_code,
                    track_result.source_label,
                    int(metadata_row["stars"]) if metadata_row is not None and metadata_row["stars"] is not None else None,
                    float(metadata_row["complexity_score"]) if metadata_row is not None and metadata_row["complexity_score"] is not None else None,
                    int(metadata_row["lemma_count"]) if metadata_row is not None and metadata_row["lemma_count"] is not None else None,
                    int(metadata_row["unrecognized_count"]) if metadata_row is not None and metadata_row["unrecognized_count"] is not None else None,
                    track_result.checksum,
                    checked_at,
                    str(track_result.raw_subtitle_path),
                ),
            )
            track_id = int(cursor.lastrowid)
            conn.execute("DELETE FROM subtitle_cues WHERE track_id = ?", (track_id,))
            for cue in track_result.cues:
                cue_cursor = conn.execute(
                    """
                    INSERT INTO subtitle_cues (
                        track_id, cue_index, start_ms, end_ms, text, normalized_text, tokenized_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        track_id,
                        cue.cue_index,
                        cue.start_ms,
                        cue.end_ms,
                        cue.text,
                        cue.normalized_text,
                        cue.tokenized_text,
                    ),
                )
                rowid = int(cue_cursor.lastrowid)
                conn.execute(
                    "INSERT INTO subtitle_cues_fts(rowid, text, normalized_text, tokenized_text) VALUES (?, ?, ?, ?)",
                    (rowid, cue.text, cue.normalized_text, cue.tokenized_text),
                )
            conn.execute("DELETE FROM video_terms WHERE youtube_video_id = ?", (youtube_video_id,))
            term_counts = self._cue_term_counts(track_result.cues)
            for term, term_count in term_counts.items():
                conn.execute(
                    "INSERT INTO video_terms (youtube_video_id, normalized_term, term_count) VALUES (?, ?, ?)",
                    (youtube_video_id, term, term_count),
                )
            conn.execute(
                """
                UPDATE youtube_videos
                SET subtitle_status = 'ready', last_checked_at = ?
                WHERE youtube_video_id = ?
                """,
                (checked_at, youtube_video_id),
            )
            conn.execute(
                """
                UPDATE kimchi_media
                SET status = 'ready', last_subtitle_check_at = ?
                WHERE youtube_video_id = ?
                """,
                (checked_at, youtube_video_id),
            )
            self._record_subtitle_run(conn, youtube_video_id, "completed", checked_at, None)
            return track_id

    def record_subtitle_failure(self, youtube_video_id: str, attempted_at: str, error_message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE youtube_videos SET subtitle_status = 'missing', last_checked_at = ? WHERE youtube_video_id = ?",
                (attempted_at, youtube_video_id),
            )
            conn.execute(
                "UPDATE kimchi_media SET status = 'ineligible', last_subtitle_check_at = ? WHERE youtube_video_id = ?",
                (attempted_at, youtube_video_id),
            )
            self._record_subtitle_run(conn, youtube_video_id, "failed", attempted_at, error_message)

    def search(
        self,
        query: str,
        *,
        limit: int,
        exact_only: bool,
        max_chars: int,
        min_stars: int | None = None,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        expanded_forms = [form for form in expand_search_forms(query) if form]
        normalized_query = normalize_text(query)
        if not normalized_query or not expanded_forms:
            return []
        query_tokens = expanded_forms
        params: list[Any] = []
        video_filter_sql = ""
        if query_tokens:
            placeholders = ", ".join("?" for _ in query_tokens)
            params.extend(query_tokens)
            video_filter_sql = f"""
                AND sv.youtube_video_id IN (
                    SELECT youtube_video_id
                    FROM video_terms
                    WHERE normalized_term IN ({placeholders})
                    GROUP BY youtube_video_id
                    HAVING COUNT(DISTINCT normalized_term) >= 1
                )
            """
        search_forms = [normalized_query] if exact_only else expanded_forms
        filter_clauses = ["INSTR(sc.normalized_text, ?) > 0" for _ in search_forms]
        exact_filter_sql = "AND (" + " OR ".join(filter_clauses) + ")"
        params.extend(search_forms)
        metadata_filters = ""
        if min_stars is not None:
            metadata_filters += " AND km.stars >= ?"
            params.append(min_stars)
        if group_id:
            metadata_filters += " AND km.group_id = ?"
            params.append(group_id)
        params.extend([max_chars, limit])
        sql = f"""
            SELECT
                sc.text AS sentence_text,
                sc.start_ms,
                sc.end_ms,
                sc.normalized_text,
                sc.tokenized_text,
                sv.youtube_video_id,
                km.kimchi_id,
                km.name_ko,
                km.name_en,
                COALESCE(st.stars, km.stars) AS stars,
                COALESCE(st.complexity_score, km.complexity_score) AS complexity_score,
                COALESCE(st.lemma_count, km.lemma_count) AS lemma_count,
                km.group_id,
                km.group_name_ko,
                COALESCE(st.unrecognized_count, km.unrecognized_count) AS unrecognized_count,
                km.youtube_url
            FROM subtitle_cues sc
            JOIN subtitle_tracks st ON st.id = sc.track_id AND st.is_active = 1
            JOIN youtube_videos sv ON sv.youtube_video_id = st.youtube_video_id AND sv.subtitle_status = 'ready'
            JOIN kimchi_media_video_map kmv ON kmv.youtube_video_id = sv.youtube_video_id AND kmv.is_primary = 1
            JOIN kimchi_media km ON km.kimchi_id = kmv.kimchi_id
            WHERE LENGTH(sc.text) <= ?
              {video_filter_sql}
              {exact_filter_sql}
              {metadata_filters}
            ORDER BY
              COALESCE(st.complexity_score, km.complexity_score, 9999999) ASC,
              LENGTH(sc.text) ASC,
              CASE WHEN sc.normalized_text = ? THEN 0 ELSE 1 END ASC,
              COALESCE(st.stars, km.stars, 0) DESC,
              COALESCE(st.unrecognized_count, km.unrecognized_count, 9999999) ASC,
              COALESCE(st.lemma_count, km.lemma_count, 9999999) ASC,
              sc.start_ms ASC
            LIMIT ?
        """
        # max_chars must be first positional param for the SQL above.
        ordered_params = [max_chars]
        ordered_params.extend(params[:-2])
        ordered_params.append(normalized_query)
        ordered_params.append(params[-1])
        with self.connect() as conn:
            rows = conn.execute(sql, ordered_params).fetchall()
        return [dict(row) for row in rows]

    def get_video(self, youtube_video_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT sv.*, km.kimchi_id, km.name_ko, km.name_en, km.stars, km.group_id, km.group_name_ko
                FROM youtube_videos sv
                LEFT JOIN kimchi_media_video_map kmv
                  ON kmv.youtube_video_id = sv.youtube_video_id AND kmv.is_primary = 1
                LEFT JOIN kimchi_media km ON km.kimchi_id = kmv.kimchi_id
                WHERE sv.youtube_video_id = ?
                """,
                (youtube_video_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def get_kimchi_media(self, kimchi_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM kimchi_media WHERE kimchi_id = ?", (kimchi_id,)).fetchone()
            return dict(row) if row is not None else None

    def pending_hydration_ids(self, limit: int) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT kimchi_id FROM kimchi_media
                WHERE (status = 'discovered' OR last_hydrated_at IS NULL)
                  AND youtube_video_id IS NOT NULL AND youtube_video_id != ''
                ORDER BY updated_at DESC, stars DESC, kimchi_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [str(row["kimchi_id"]) for row in rows]

    def pending_subtitle_video_ids(self, limit: int) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT youtube_video_id FROM youtube_videos
                WHERE youtube_video_id IS NOT NULL
                  AND youtube_video_id != ''
                ORDER BY
                  CASE WHEN subtitle_status = 'ready' THEN 1 ELSE 0 END ASC,
                  last_checked_at IS NOT NULL,
                  last_checked_at ASC,
                  youtube_video_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(row["youtube_video_id"]) for row in rows]

    def _apply_schema_migrations(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(subtitle_tracks)").fetchall()
        }
        if "kimchi_id" not in columns:
            conn.execute("ALTER TABLE subtitle_tracks ADD COLUMN kimchi_id TEXT")
        if "stars" not in columns:
            conn.execute("ALTER TABLE subtitle_tracks ADD COLUMN stars INTEGER")
        if "complexity_score" not in columns:
            conn.execute("ALTER TABLE subtitle_tracks ADD COLUMN complexity_score REAL")
        if "lemma_count" not in columns:
            conn.execute("ALTER TABLE subtitle_tracks ADD COLUMN lemma_count INTEGER")
        if "unrecognized_count" not in columns:
            conn.execute("ALTER TABLE subtitle_tracks ADD COLUMN unrecognized_count INTEGER")

    def _refresh_primary_mapping(self, conn: sqlite3.Connection, youtube_video_id: str) -> None:
        conn.execute(
            "UPDATE kimchi_media_video_map SET is_primary = 0 WHERE youtube_video_id = ?",
            (youtube_video_id,),
        )
        row = conn.execute(
            """
            SELECT kimchi_id FROM kimchi_media
            WHERE youtube_video_id = ?
            ORDER BY stars DESC, complexity_score ASC, updated_at DESC, kimchi_id ASC
            LIMIT 1
            """,
            (youtube_video_id,),
        ).fetchone()
        if row is None:
            return
        kimchi_id = str(row["kimchi_id"])
        conn.execute(
            """
            INSERT INTO kimchi_media_video_map (kimchi_id, youtube_video_id, is_primary)
            VALUES (?, ?, 1)
            ON CONFLICT(kimchi_id, youtube_video_id) DO UPDATE SET is_primary = 1
            """,
            (kimchi_id, youtube_video_id),
        )
        conn.execute(
            "UPDATE youtube_videos SET preferred_kimchi_id = ? WHERE youtube_video_id = ?",
            (kimchi_id, youtube_video_id),
        )

    def _record_hydration_run(
        self,
        conn: sqlite3.Connection,
        kimchi_id: str,
        status: str,
        attempted_at: str,
        error_message: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO hydration_runs (kimchi_id, status, last_attempted_at, last_succeeded_at, error_message)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(kimchi_id) DO UPDATE SET
                status = excluded.status,
                last_attempted_at = excluded.last_attempted_at,
                last_succeeded_at = excluded.last_succeeded_at,
                error_message = excluded.error_message
            """,
            (
                kimchi_id,
                status,
                attempted_at,
                attempted_at if status == "completed" else None,
                error_message,
            ),
        )

    def _record_subtitle_run(
        self,
        conn: sqlite3.Connection,
        youtube_video_id: str,
        status: str,
        attempted_at: str,
        error_message: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO subtitle_runs (youtube_video_id, status, last_attempted_at, last_succeeded_at, error_message)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(youtube_video_id) DO UPDATE SET
                status = excluded.status,
                last_attempted_at = excluded.last_attempted_at,
                last_succeeded_at = excluded.last_succeeded_at,
                error_message = excluded.error_message
            """,
            (
                youtube_video_id,
                status,
                attempted_at,
                attempted_at if status == "completed" else None,
                error_message,
            ),
        )

    def _cue_term_counts(self, cues: Iterable[Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cue in cues:
            for term in cue.tokenized_text.split():
                if not term:
                    continue
                counts[term] = counts.get(term, 0) + 1
        return counts

    def _youtube_video_id_from_sources(self, sources: Iterable[Mapping[str, Any]]) -> str:
        for source in sources:
            if str(source.get("source_type", "")) == "youtube_video":
                return str(source.get("value", "") or "").strip()
        return ""
