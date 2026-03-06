from __future__ import annotations

import logging
import sys
from pathlib import Path


def configure_logging(log_path: Path) -> logging.Logger:
    """
    Configure structured logging for the OFAC sanctions agent.

    Idempotent enough for CLI usage; subsequent calls will reuse handlers.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    date_fmt = "%Y-%m-%dT%H:%M:%S"

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler_file = logging.FileHandler(log_path, encoding="utf-8")
        handler_stream = logging.StreamHandler(sys.stdout)
        logging.basicConfig(
            level=logging.DEBUG,
            format=fmt,
            datefmt=date_fmt,
            handlers=[handler_file, handler_stream],
        )

        for lib in ("playwright", "asyncio", "urllib3"):
            logging.getLogger(lib).setLevel(logging.WARNING)

    return logging.getLogger("ofac_sanctions_agent")

