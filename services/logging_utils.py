from __future__ import annotations

import logging
from pathlib import Path


LOGGER_NAME = "youglish_korean_context_grabber"


def get_logger(addon_dir: Path) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_dir = addon_dir / "user_files"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "youglish_context.log"
    try:
        handler = logging.FileHandler(log_path, encoding="utf-8")
    except Exception:
        handler = logging.NullHandler()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger
