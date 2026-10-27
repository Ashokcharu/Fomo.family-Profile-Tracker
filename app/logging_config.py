from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def configure_file_logging(level: str = "INFO", log_path: Path | None = None) -> Path:
    log_path = log_path or Path(__file__).resolve().parent.parent / "data" / "logs" / "app.log"
    log_path = log_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    file_handler = next(
        (
            handler for handler in root_logger.handlers
            if isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename) == log_path
        ),
        None,
    )
    if file_handler is None:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
        ))
        root_logger.addHandler(file_handler)

    for logger_name in ("uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(logger_name)
        if not any(
            isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename) == log_path
            for handler in server_logger.handlers
        ):
            server_logger.addHandler(file_handler)
        server_logger.propagate = False
    return log_path