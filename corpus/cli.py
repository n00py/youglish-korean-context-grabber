from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .api import KimchiCorpusAPIServer
from .db import KimchiCorpusDatabase
from .ingest import KimchiCorpusIngestor
from ..services.storage_paths import corpus_db_path


def _addon_dir_from_args(args: argparse.Namespace) -> Path:
    if args.addon_dir:
        return Path(args.addon_dir).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kimchi corpus local tools")
    parser.add_argument("--addon-dir", default="", help="Path to the add-on root directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the local HTTP API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)

    backfill = subparsers.add_parser("backfill", help="Run or resume discovery+hydration+subtitle ingestion")
    backfill.add_argument("--max-pages", default=None, type=int)
    backfill.add_argument("--sleep-between-pages", default=0.5, type=float)
    backfill.add_argument("--sleep-between-items", default=0.25, type=float)
    backfill.add_argument("--retry-cooldown-seconds", default=1800, type=float)

    subparsers.add_parser("recheck-discovery", help="Rerun discovery traversal")
    subtitle_recheck = subparsers.add_parser("recheck-subtitles", help="Retry pending subtitle fetches")
    subtitle_recheck.add_argument("--limit", default=100, type=int)
    subtitle_recheck.add_argument("--sleep-between-items", default=0.25, type=float)
    subtitle_recheck.add_argument("--retry-cooldown-seconds", default=1800, type=float)

    subparsers.add_parser("stats", help="Print corpus stats")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    addon_dir = _addon_dir_from_args(args)
    db = KimchiCorpusDatabase(corpus_db_path(addon_dir))
    ingestor = KimchiCorpusIngestor(addon_dir, db)

    if args.command == "serve":
        server = KimchiCorpusAPIServer(
            addon_dir=addon_dir,
            db=db,
            ingestor=ingestor,
            host=args.host,
            port=args.port,
        )
        server.serve_forever()
        return 0
    if args.command == "backfill":
        try:
            summary = ingestor.backfill(
                progress_callback=lambda message: print(message, flush=True),
                max_pages=args.max_pages,
                sleep_between_pages=args.sleep_between_pages,
                sleep_between_items=args.sleep_between_items,
                retry_cooldown_seconds=args.retry_cooldown_seconds,
            )
        except KeyboardInterrupt:
            print(json.dumps({"status": "paused"}, ensure_ascii=False, indent=2))
            return 130
        print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
        return 0
    if args.command == "recheck-discovery":
        summary = ingestor.recheck_discovery(progress_callback=lambda message: print(message, flush=True))
        print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
        return 0
    if args.command == "recheck-subtitles":
        try:
            processed = ingestor.recheck_subtitles(
                progress_callback=lambda message: print(message, flush=True),
                limit=args.limit,
                sleep_between_items=args.sleep_between_items,
                retry_cooldown_seconds=args.retry_cooldown_seconds,
            )
        except KeyboardInterrupt:
            print(json.dumps({"status": "paused"}, ensure_ascii=False, indent=2))
            return 130
        print(json.dumps({"processed": processed}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "stats":
        print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
