from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .db import KimchiCorpusDatabase
from .ingest import BackfillSummary, KimchiCorpusIngestor


def _safe_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class KimchiCorpusAPIServer:
    def __init__(
        self,
        addon_dir: Path,
        db: KimchiCorpusDatabase,
        ingestor: KimchiCorpusIngestor,
        logger: logging.Logger | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self._addon_dir = addon_dir
        self._db = db
        self._ingestor = ingestor
        self._logger = logger or logging.getLogger(__name__)
        self._host = host
        self._port = port
        self._jobs: dict[str, dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def serve_forever(self) -> None:
        handler = self._build_handler()
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        self._server.serve_forever()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def _build_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                outer._logger.info("Kimchi API " + fmt, *args)

            def do_GET(self) -> None:
                outer._handle_request(self)

            def do_POST(self) -> None:
                outer._handle_request(self)

        return Handler

    def _handle_request(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        route = parsed.path.rstrip("/") or "/"
        try:
            if handler.command == "GET" and route == "/health":
                self._json(handler, 200, {"ok": True, "stats": self._db.stats(), "jobs": self._jobs_snapshot()})
                return
            if handler.command == "GET" and route == "/admin/stats":
                self._json(handler, 200, {"stats": self._db.stats(), "jobs": self._jobs_snapshot()})
                return
            if handler.command == "GET" and route == "/search":
                qs = parse_qs(parsed.query)
                rows = self._db.search(
                    qs.get("q", [""])[0],
                    limit=max(1, min(50, _safe_int(qs.get("limit", [None])[0], 10))),
                    exact_only=qs.get("exact_only", ["false"])[0].lower() == "true",
                    max_chars=max(10, _safe_int(qs.get("max_chars", [None])[0], 120)),
                    min_stars=(
                        _safe_int(qs.get("min_stars", [None])[0], 0)
                        if qs.get("min_stars", [None])[0] is not None
                        else None
                    ),
                    group_id=qs.get("group_id", [None])[0],
                )
                self._json(handler, 200, {"items": [self._search_row_to_api_item(row) for row in rows]})
                return
            if handler.command == "GET" and route.startswith("/videos/"):
                youtube_video_id = route.split("/", 2)[2]
                payload = self._db.get_video(youtube_video_id)
                self._json(handler, 200 if payload else 404, payload or {"error": "not found"})
                return
            if handler.command == "GET" and route.startswith("/kimchi/"):
                kimchi_id = route.split("/", 2)[2]
                payload = self._db.get_kimchi_media(kimchi_id)
                self._json(handler, 200 if payload else 404, payload or {"error": "not found"})
                return
            if handler.command == "POST" and route == "/admin/discovery/backfill":
                accepted = self._start_job("backfill", self._run_backfill)
                self._json(handler, 202, accepted)
                return
            if handler.command == "POST" and route == "/admin/discovery/recheck":
                accepted = self._start_job("discovery_recheck", self._run_discovery_recheck)
                self._json(handler, 202, accepted)
                return
            if handler.command == "POST" and route == "/admin/subtitles/recheck":
                accepted = self._start_job("subtitle_recheck", self._run_subtitle_recheck)
                self._json(handler, 202, accepted)
                return
            self._json(handler, 404, {"error": "not found"})
        except Exception as exc:
            outer_logger = self._logger if hasattr(self, "_logger") else None
            if outer_logger:
                outer_logger.exception("Kimchi API request failed for %s", route)
            self._json(handler, 500, {"error": str(exc)})

    def _json(self, handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _search_row_to_api_item(self, row: dict[str, Any]) -> dict[str, Any]:
        start_ms = int(row["start_ms"])
        end_ms = int(row["end_ms"])
        start_seconds = start_ms // 1000
        minutes, seconds = divmod(start_seconds, 60)
        timestamp = f"{minutes}:{seconds:02d}"
        kimchi_id = row["kimchi_id"]
        youtube_video_id = row["youtube_video_id"]
        youtube_url = row["youtube_url"] or f"https://www.youtube.com/watch?v={youtube_video_id}"
        kimchi_url = f"https://kimchi-reader.app/media/{kimchi_id}"
        source_title = row.get("name_ko") or row.get("name_en") or youtube_video_id
        return {
            "sentence_text": row["sentence_text"],
            "matched_term": "",
            "source_title": source_title,
            "source_url": kimchi_url,
            "timestamp": timestamp,
            "video_id": youtube_video_id,
            "provider_name": "BanGlish",
            "raw_payload": {
                "kimchi_id": kimchi_id,
                "youtube_video_id": youtube_video_id,
                "youtube_url": youtube_url,
                "kimchi_url": kimchi_url,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start": start_ms / 1000.0,
                "end": end_ms / 1000.0,
                "stars": row.get("stars"),
                "complexity_score": row.get("complexity_score"),
                "lemma_count": row.get("lemma_count"),
                "group_id": row.get("group_id"),
                "group_name_ko": row.get("group_name_ko"),
            },
        }

    def _jobs_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._jobs_lock:
            return {name: dict(details) for name, details in self._jobs.items()}

    def _start_job(self, job_name: str, target) -> dict[str, Any]:
        with self._jobs_lock:
            current = self._jobs.get(job_name)
            if current and current.get("status") == "running":
                return {"ok": True, "job": job_name, "status": "already_running"}
            self._jobs[job_name] = {"status": "running", "messages": []}
        thread = threading.Thread(target=target, name=f"kimchi-{job_name}", daemon=True)
        thread.start()
        return {"ok": True, "job": job_name, "status": "started"}

    def _append_job_message(self, job_name: str, message: str) -> None:
        with self._jobs_lock:
            details = self._jobs.setdefault(job_name, {"status": "running", "messages": []})
            messages = list(details.get("messages", []))
            messages.append(message)
            details["messages"] = messages[-20:]

    def _complete_job(self, job_name: str, *, error: str | None = None, result: dict[str, Any] | None = None) -> None:
        with self._jobs_lock:
            details = self._jobs.setdefault(job_name, {"messages": []})
            details["status"] = "failed" if error else "completed"
            if error:
                details["error"] = error
            if result is not None:
                details["result"] = result

    def _run_backfill(self) -> None:
        try:
            summary = self._ingestor.backfill(progress_callback=lambda message: self._append_job_message("backfill", message))
        except Exception as exc:
            self._complete_job("backfill", error=str(exc))
            return
        self._complete_job("backfill", result=asdict(summary))

    def _run_discovery_recheck(self) -> None:
        try:
            summary = self._ingestor.recheck_discovery(
                progress_callback=lambda message: self._append_job_message("discovery_recheck", message)
            )
        except Exception as exc:
            self._complete_job("discovery_recheck", error=str(exc))
            return
        self._complete_job("discovery_recheck", result=asdict(summary))

    def _run_subtitle_recheck(self) -> None:
        try:
            processed = self._ingestor.recheck_subtitles(
                progress_callback=lambda message: self._append_job_message("subtitle_recheck", message)
            )
        except Exception as exc:
            self._complete_job("subtitle_recheck", error=str(exc))
            return
        self._complete_job("subtitle_recheck", result={"processed": processed})
